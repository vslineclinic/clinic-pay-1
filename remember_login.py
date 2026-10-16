"""브라우저에는 임의 기기 토큰, 비공개 Sheets에는 그 해시만 보관한다."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID, uuid4

from branch_access import AccessError


REMEMBER_MAX_AGE_SECONDS = 180 * 86400
REMEMBER_TAB = "로그인기기"
REMEMBER_HEADERS = ["device_id", "username", "account_revision", "token_hash", "issued_at", "expires_at", "revoked_at"]
REMEMBER_ERROR_MESSAGE = "저장된 로그인을 확인할 수 없습니다. 아이디와 비밀번호로 다시 로그인해 주세요."
_TOKEN = re.compile(r"([0-9a-f-]{36})\.[A-Za-z0-9_-]{43}")
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,79}")


class RememberError(AccessError):
    """토큰·검증값·계정·원본 오류를 노출하지 않는 사용자용 오류."""


def _deny():
    raise RememberError(REMEMBER_ERROR_MESSAGE) from None


def _now():
    return datetime.now(timezone.utc)


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        _deny()
    return value


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _deny()
    return parsed.astimezone(timezone.utc)


def _token_parts(raw_token):
    try:
        match = _TOKEN.fullmatch(raw_token) if isinstance(raw_token, str) else None
        if match is None:
            _deny()
        return _uuid(match[1]), hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    except RememberError:
        raise
    except Exception:
        _deny()


class RememberStore:
    """기기 토큰도 매 요청에 비공개 저장소와 최신 계정 개정을 검증한다."""

    def __init__(self, transport, accounts):
        self._transport, self._accounts = transport, accounts

    def _read_devices(self):
        try:
            rows = self._transport._request("GET", quote("'" + REMEMBER_TAB + "'", safe="")).get("values", [])
            if not isinstance(rows, list) or not rows or rows[0] != REMEMBER_HEADERS:
                _deny()
            devices = {}
            for source in rows[1:]:
                if not source or isinstance(source, list) and all(value == "" for value in source):
                    continue
                if not isinstance(source, list) or not 6 <= len(source) <= 7 or not all(isinstance(value, str) for value in source):
                    _deny()
                row = source + [""] * (7 - len(source))
                device_id = _uuid(row[0])
                if not _USERNAME.fullmatch(row[1]) or not re.fullmatch(r"[0-9a-f]{64}", row[3]):
                    _deny()
                _uuid(row[2])
                issued, expires = _timestamp(row[4]), _timestamp(row[5])
                if not timedelta(0) < expires - issued <= timedelta(seconds=REMEMBER_MAX_AGE_SECONDS):
                    _deny()
                if row[6] and _timestamp(row[6]) < issued:
                    _deny()
                previous = devices.get(device_id)
                if previous and previous[:6] != row[:6]:
                    _deny()
                # 중지 행 뒤에 과거 발급 행이 다시 추가되어도 토큰을 되살리지 않는다.
                if previous is None or not previous[6]:
                    devices[device_id] = row
            return devices
        except RememberError:
            raise
        except Exception:
            _deny()

    def _append(self, row):
        try:
            result = self._transport._request("POST", quote("'" + REMEMBER_TAB + "'!A:G", safe="") + ":append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": [row]})
            updates = result.get("updates")
            if not isinstance(updates, dict) or type(updates.get("updatedRows")) is not int or updates["updatedRows"] != 1:
                _deny()
        except RememberError:
            raise
        except Exception:
            _deny()

    def _device(self, raw_token):
        device_id, token_hash = _token_parts(raw_token)
        row = self._read_devices().get(device_id)
        if row is None or not hmac.compare_digest(token_hash, row[3]):
            _deny()
        return row

    def issue(self, principal):
        try:
            principal = self._accounts.validate(principal)
            devices = self._read_devices()
            device_id = str(uuid4())
            if device_id in devices:
                _deny()
            raw_token = device_id + "." + secrets.token_urlsafe(32)
            now = _now()
            self._append([
                device_id, principal.username, principal.revision,
                hashlib.sha256(raw_token.encode("ascii")).hexdigest(),
                now.isoformat(), (now + timedelta(seconds=REMEMBER_MAX_AGE_SECONDS)).isoformat(), "",
            ])
            return raw_token
        except RememberError:
            raise
        except Exception:
            _deny()

    def restore(self, raw_token):
        try:
            row = self._device(raw_token)
            if row[6] or not _timestamp(row[4]) <= _now() < _timestamp(row[5]):
                _deny()
            return self._accounts.get_principal(row[1], row[2])
        except RememberError:
            raise
        except Exception:
            _deny()

    def revoke(self, raw_token):
        row = self._device(raw_token)
        if not row[6]:
            self._append(row[:6] + [_now().isoformat()])

    # shortcut: 기기 이력을 모두 읽는다. 계정·기기 수로 지연되면 인덱스 DB로 옮긴다.
