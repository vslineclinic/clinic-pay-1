"""비공개 계정 시트의 인증과 지점별 서버 접근 경계."""
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID, uuid4


CANONICAL_BRANCHES = ("인천점", "잠실점", "엔디어트", "강남점", "일산점", "압구정", "마곡점")
ACCOUNT_TAB = "계정"
ACCOUNT_HEADERS = ["username", "role", "branch", "password_hash", "active", "revision", "updated_at"]
AUTH_ERROR_MESSAGE = "아이디·비밀번호 또는 접근 권한을 확인해 주세요."
ACCOUNT_ERROR_MESSAGE = "계정 설정을 확인할 수 없습니다. 총괄 관리자에게 문의해 주세요."
CONFLICT_ERROR_MESSAGE = "계정이 변경되었습니다. 목록을 새로고침한 뒤 다시 시도해 주세요."
_HASH = re.compile(r"pbkdf2-sha256:([1-9][0-9]{5,6}):([0-9a-f]{32}):([0-9a-f]{64})")
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,79}")
_DUMMY_HASH = "pbkdf2-sha256:100000:000102030405060708090a0b0c0d0e0f:0f80ecb32361e1f1e6f3bee07f74bd853216c8c4c3eeacc5149ecaa709d2c512"


class AccessError(ValueError):
    """계정·암호·원본 HTTP 내용을 포함하지 않는 고정 오류."""


class LoginThrottle:
    """동일 프로세스의 여러 브라우저에서 시도 횟수를 공유한다."""

    def __init__(self, limit=5, window_seconds=900, max_keys=1024):
        self._limit, self._window, self._max_keys = limit, window_seconds, max_keys
        self._attempts, self._lock = {}, threading.Lock()

    @staticmethod
    def _key(username):
        return username.strip().casefold()[:80] if isinstance(username, str) else ""

    def allow(self, username, now=None):
        """허용과 동시에 한 번 예약하므로 병렬 로그인도 상한을 넘지 않는다."""
        now = time.monotonic() if now is None else now
        key = self._key(username)
        with self._lock:
            self._attempts = {name: item for name, item in self._attempts.items() if now - item[0] < self._window}
            started, count = self._attempts.get(key, (now, 0))
            if count >= self._limit:
                return False
            if key not in self._attempts and len(self._attempts) >= self._max_keys:
                oldest = min(self._attempts, key=lambda name: self._attempts[name][0])
                del self._attempts[oldest]
            self._attempts[key] = (started, count + 1)
            return True

    def success(self, username):
        with self._lock:
            self._attempts.pop(self._key(username), None)

    # shortcut: 재시작·별도 worker·키 대량 순환은 공유하지 않는다. 규모가 커지면 공용 제한기로 옮긴다.


def _deny(message=AUTH_ERROR_MESSAGE):
    raise AccessError(message) from None


def _username(value):
    if not isinstance(value, str) or not _USERNAME.fullmatch(value):
        _deny(ACCOUNT_ERROR_MESSAGE)
    return value


def _revision(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            _deny(ACCOUNT_ERROR_MESSAGE)
        return value
    except (ValueError, TypeError, AttributeError):
        _deny(ACCOUNT_ERROR_MESSAGE)


def _hash_parts(value):
    match = _HASH.fullmatch(value) if isinstance(value, str) else None
    if match is None or not 100000 <= int(match[1]) <= 1000000:
        return None
    return int(match[1]), bytes.fromhex(match[2]), bytes.fromhex(match[3])


def _password_bytes(password):
    if not isinstance(password, str) or not password or len(password) > 1024:
        return None
    try:
        encoded = password.encode("utf-8")
        return encoded if len(encoded) <= 4096 else None
    except UnicodeError:
        return None


def hash_password(password, salt_hex=None, iterations=600000):
    """새 암호는 600,000회, 이전 WebCrypto 100,000회 해시는 그대로 검증한다."""
    encoded = _password_bytes(password)
    if encoded is None or len(password) < 6 or type(iterations) is not int or not 100000 <= iterations <= 1000000:
        _deny(ACCOUNT_ERROR_MESSAGE)
    salt_hex = secrets.token_hex(16) if salt_hex is None else salt_hex
    if not isinstance(salt_hex, str) or not re.fullmatch(r"[0-9a-f]{32}", salt_hex):
        _deny(ACCOUNT_ERROR_MESSAGE)
    digest = hashlib.pbkdf2_hmac("sha256", encoded, bytes.fromhex(salt_hex), iterations).hex()
    return f"pbkdf2-sha256:{iterations}:{salt_hex}:{digest}"


def verify_password(password, password_hash):
    encoded, parts = _password_bytes(password), _hash_parts(password_hash)
    if encoded is None or parts is None:
        return False
    iterations, salt, expected = parts
    actual = hashlib.pbkdf2_hmac("sha256", encoded, salt, iterations)
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    branch: str
    revision: str

    def __post_init__(self):
        _username(self.username)
        _revision(self.revision)
        if not (self.role == "admin" and self.branch == "all" or self.role == "branch" and self.branch in CANONICAL_BRANCHES):
            _deny(ACCOUNT_ERROR_MESSAGE)

    @property
    def is_admin(self):
        return self.role == "admin"


def assert_branch(principal, branch):
    """UI 선택값을 신뢰하지 않고 모든 읽기·쓰기 직전에 적용한다."""
    if not isinstance(principal, Principal) or branch not in CANONICAL_BRANCHES:
        _deny()
    if not principal.is_admin and principal.branch != branch:
        _deny()
    return branch


def authorized_sheets(principal, configured):
    if not isinstance(principal, Principal) or not isinstance(configured, dict):
        _deny()
    return {branch: value for branch, value in configured.items()
            if branch in CANONICAL_BRANCHES and (principal.is_admin or branch == principal.branch)}


def _public(account):
    return {key: value for key, value in account.items() if key != "password_hash"}


class AccountStore:
    """기록 전용 인증을 재사용하되 계정 탭은 별도로 읽고 append만 한다."""

    def __init__(self, transport):
        self._transport = transport

    def _read_accounts(self):
        try:
            rows = self._transport._request("GET", quote("'" + ACCOUNT_TAB + "'", safe="")).get("values", [])
            if not isinstance(rows, list) or not rows or rows[0] != ACCOUNT_HEADERS:
                _deny(ACCOUNT_ERROR_MESSAGE)
            latest, spellings, revisions = {}, {}, {}
            for row in rows[1:]:
                if not row or isinstance(row, list) and all(value == "" for value in row):
                    continue
                if not isinstance(row, list) or len(row) != len(ACCOUNT_HEADERS):
                    _deny(ACCOUNT_ERROR_MESSAGE)
                account = dict(zip(ACCOUNT_HEADERS, row))
                username = _username(account["username"])
                Principal(username, account["role"], account["branch"], account["revision"])
                if _hash_parts(account["password_hash"]) is None:
                    _deny(ACCOUNT_ERROR_MESSAGE)
                active = account["active"]
                if type(active) is bool:
                    account["active"] = active
                elif isinstance(active, str) and active in ("1", "0"):
                    account["active"] = active == "1"
                else:
                    _deny(ACCOUNT_ERROR_MESSAGE)
                timestamp = datetime.fromisoformat(account["updated_at"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    _deny(ACCOUNT_ERROR_MESSAGE)
                key = username.casefold()
                if key in spellings and spellings[key] != username:
                    _deny(ACCOUNT_ERROR_MESSAGE)
                revision = account["revision"]
                if revision in revisions:
                    _deny(ACCOUNT_ERROR_MESSAGE)
                spellings[key], revisions[revision], latest[key] = username, account, account
            return latest
        except AccessError:
            raise
        except Exception:
            _deny(ACCOUNT_ERROR_MESSAGE)

    def authenticate(self, username, password):
        accounts = self._read_accounts()
        key = username.strip().casefold() if isinstance(username, str) else ""
        account = accounts.get(key)
        valid = verify_password(password, account["password_hash"] if account else _DUMMY_HASH)
        if not valid or account is None or not account["active"]:
            _deny()
        return Principal(*(account[key] for key in ("username", "role", "branch", "revision")))

    def validate(self, principal):
        if not isinstance(principal, Principal):
            _deny()
        account = self._read_accounts().get(principal.username.casefold())
        if account is None or not account["active"]:
            _deny()
        current = Principal(*(account[key] for key in ("username", "role", "branch", "revision")))
        if current != principal:
            _deny()
        return current

    def get_principal(self, username, expected_revision):
        """유효한 기기 토큰을 검증한 서버 호출만 현재 계정 권한을 복원한다."""
        username = _username(username)
        account = self._read_accounts().get(username.casefold())
        if account is None or not account["active"] or account["username"] != username or account["revision"] != expected_revision:
            _deny()
        return Principal(*(account[key] for key in ("username", "role", "branch", "revision")))

    def list_accounts(self, principal):
        if not isinstance(principal, Principal) or not principal.is_admin:
            _deny()
        self.validate(principal)
        return sorted((_public(account) for account in self._read_accounts().values()), key=lambda row: row["username"].casefold())

    def save_branch_account(self, principal, username, branch, password=None, active=True, expected_revision=None):
        if not isinstance(principal, Principal) or not principal.is_admin:
            _deny()
        self.validate(principal)
        username = _username(username)
        assert_branch(principal, branch)
        if type(active) is not bool:
            _deny(ACCOUNT_ERROR_MESSAGE)
        existing = self._read_accounts().get(username.casefold())
        if existing:
            if existing["role"] == "admin" or existing["username"] != username:
                _deny()
            if expected_revision != existing["revision"]:
                _deny(CONFLICT_ERROR_MESSAGE)
        elif expected_revision is not None or password is None:
            _deny(ACCOUNT_ERROR_MESSAGE)
        password_hash = existing["password_hash"] if password is None and existing else hash_password(password)
        account = dict(zip(ACCOUNT_HEADERS, (
            username, "branch", branch, password_hash, active, str(uuid4()),
            datetime.now(timezone.utc).isoformat(),
        )))
        row = [account[key] for key in ACCOUNT_HEADERS]
        row[4] = "1" if active else "0"
        # shortcut: Sheets에는 원자적 CAS가 없다. 동시 수정이 잦아지면 트랜잭션 DB로 옮긴다.
        try:
            result = self._transport._request("POST", quote("'" + ACCOUNT_TAB + "'!A:G", safe="") + ":append",
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"majorDimension": "ROWS", "values": [row]})
            updates = result.get("updates")
            if not isinstance(updates, dict) or type(updates.get("updatedRows")) is not int or updates["updatedRows"] != 1:
                _deny(ACCOUNT_ERROR_MESSAGE)
        except AccessError:
            raise
        except Exception:
            _deny(ACCOUNT_ERROR_MESSAGE)
        return _public(account)


class ScopedHistoryStore:
    """호출할 때마다 세션을 재검증하고 지점 범위를 강제한다."""

    def __init__(self, transport, principal, validate):
        self._transport, self._principal, self._validate = transport, principal, validate

    def append_record(self, record):
        branch = record.get("branch") if isinstance(record, dict) else None
        assert_branch(self._principal, branch)
        principal = self._validate(self._principal)
        assert_branch(principal, branch)
        return self._transport.append_record(record)

    def list_month(self, month, branch=""):
        if not isinstance(self._principal, Principal):
            _deny()
        branch = branch or ("" if self._principal.is_admin else self._principal.branch)
        if branch:
            assert_branch(self._principal, branch)
        principal = self._validate(self._principal)
        if branch:
            assert_branch(principal, branch)
        elif not isinstance(principal, Principal) or not principal.is_admin:
            _deny()
        records = self._transport.list_month(month, branch)
        return [record for record in records if isinstance(record, dict)
                and record.get("branch") in CANONICAL_BRANCHES
                and (not branch or record["branch"] == branch)
                and (principal.is_admin or record["branch"] == principal.branch)]
