"""별도 비공개 Google Sheet에 환자 없는 일자별 정산 요약만 누적한다."""
import json
import math
import re
from datetime import date, datetime, timezone
from numbers import Real
from urllib.parse import quote
from uuid import UUID, uuid4

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

HEADERS = ["branch", "settlement_date", "run_id", "recorded_at", "payload_json"]
SHEET_TAB = "정산기록"
INVALID_RECORD_MESSAGE = "정산 기록 형식을 확인할 수 없습니다. 원본 요약과 저장소를 확인해 주세요."
HEADER_ERROR_MESSAGE = "정산 기록 시트의 열 구성이 다릅니다. 운영자에게 저장소 확인을 요청해 주세요."
STORAGE_ERROR_MESSAGE = "정산 기록 저장소에 연결하지 못했습니다. 설정과 접근 권한을 확인한 뒤 다시 시도해 주세요."
_TOTAL_KEYS = ("h_card", "h_cash", "d_card", "d_cashxfer", "d_plat", "p_card", "p_cashxfer", "p_plat")
_VERIFICATION_KEYS = ("유형B_차트번호오타", "유형C1_한쪽만존재", "유형C2_금액불일치", "유형C3_결제수단불일치")
_CHANNEL_FIELDS = ("채널", "한솔", "일마", "차트", "일마-차트", "한솔-차트", "한솔-일마")
_SAVED_CHANNEL_FIELDS = ("channel", "terminal", "daily", "vegas", "daily_minus_vegas", "terminal_minus_vegas", "terminal_minus_daily")


class HistoryError(ValueError):
    """원본 자료·주소·인증 정보를 포함하지 않는 사용자용 오류."""


def _invalid():
    raise HistoryError(INVALID_RECORD_MESSAGE) from None


def _money(value, optional=False):
    if optional and (value is None or isinstance(value, Real) and math.isnan(value)):
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        _invalid()
    if not math.isfinite(value) or int(value) != value:
        _invalid()
    return int(value)


def _count(value):
    result = _money(value)
    if result < 0:
        _invalid()
    return result


def _bool(value):
    if type(value) is not bool:
        _invalid()
    return value


def _branch(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        _invalid()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        _invalid()
    return value.strip()


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _invalid()
    return date.fromisoformat(value).isoformat()


def _timestamp(value):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(timezone.utc).isoformat()


def _channels(totals, has_terminal):
    result = []
    for channel, suffix, h_key in (("카드", "card", "h_card"), ("현금+이체", "cashxfer", "h_cash"), ("플랫폼", "plat", None)):
        terminal = totals[h_key] if has_terminal and h_key else None
        daily, vegas = totals["d_" + suffix], totals["p_" + suffix]
        result.append(dict(zip(_SAVED_CHANNEL_FIELDS, (
            channel, terminal, daily, vegas, daily - vegas,
            terminal - vegas if terminal is not None else None,
            terminal - daily if terminal is not None else None,
        ))))
    return result


def make_record(branch, settlement_date, totals, channel_df, verification_counts,
                unmatched_hansol, unmatched_daily, split_search_limited, has_terminal,
                run_id=None, checked_at=None, *, cross_check_warning=False,
                additional_card_review=0, daily_parse_warning=False):
    """집계·건수만 허용한다. 원본 이름·번호·메모·AI 문장은 보관하지 않는다."""
    try:
        has_terminal = _bool(has_terminal)
        if "_has_hansol" in totals and _bool(totals["_has_hansol"]) != has_terminal:
            _invalid()
        clean_totals = {key: _money(totals[key], optional=key.startswith("h_")) for key in _TOTAL_KEYS}
        if any((clean_totals[key] is not None) != has_terminal for key in ("h_card", "h_cash")):
            _invalid()
        channels = _channels(clean_totals, has_terminal)
        source_rows = channel_df.to_dict(orient="records") if hasattr(channel_df, "to_dict") else channel_df
        if not isinstance(source_rows, list) or len(source_rows) != 3:
            _invalid()
        source_by_channel = {row["채널"]: row for row in source_rows}
        if set(source_by_channel) != {"카드", "현금+이체", "플랫폼"}:
            _invalid()
        for expected in channels:
            source = source_by_channel[expected["channel"]]
            for original, saved in zip(_CHANNEL_FIELDS[1:], _SAVED_CHANNEL_FIELDS[1:]):
                if _money(source[original], optional=expected[saved] is None) != expected[saved]:
                    _invalid()
        counts = {key: _count(verification_counts.get(key, 0)) for key in _VERIFICATION_KEYS}
        h_unmatched, d_unmatched = _count(unmatched_hansol), _count(unmatched_daily)
        limited, cross_warning = _bool(split_search_limited), _bool(cross_check_warning)
        parse_warning = _bool(daily_parse_warning)
        card_review = _count(additional_card_review)
        daily_total = sum(clean_totals[key] for key in ("d_card", "d_cashxfer", "d_plat"))
        vegas_total = sum(clean_totals[key] for key in ("p_card", "p_cashxfer", "p_plat"))
        review = any(row["daily_minus_vegas"] for row in channels)
        # 현금영수증은 현금+이체 전체와 범위가 달라 참고값으로만 남긴다.
        review |= has_terminal and any(channels[0][key] for key in ("terminal_minus_vegas", "terminal_minus_daily"))
        review |= any(counts.values()) or bool(h_unmatched or d_unmatched or card_review or limited or cross_warning or parse_warning)
        run_id = str(uuid4()) if run_id is None else run_id
        if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
            _invalid()
        return {
            "schema_version": 1, "branch": _branch(branch), "settlement_date": _date(settlement_date),
            "run_id": run_id, "recorded_at": _timestamp(checked_at),
            "status": "검토 필요" if review else "일치" if has_terminal else "단말기 미검증",
            "daily_total": daily_total, "vegas_total": vegas_total, "gap": daily_total - vegas_total,
            "totals": clean_totals, "channels": channels, "verification_counts": counts,
            "unmatched_hansol": h_unmatched, "unmatched_daily": d_unmatched,
            "split_search_limited": limited, "has_terminal": has_terminal,
            "cross_check_warning": cross_warning, "additional_card_review": card_review,
            "daily_parse_warning": parse_warning,
        }
    except HistoryError:
        raise
    except Exception:
        _invalid()


def _json(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_record(record):
    try:
        channels = record["channels"]
        if any(set(row) != set(_SAVED_CHANNEL_FIELDS) for row in channels):
            _invalid()
        original_channels = [dict(zip(_CHANNEL_FIELDS, (row[key] for key in _SAVED_CHANNEL_FIELDS))) for row in channels]
        rebuilt = make_record(
            record["branch"], record["settlement_date"], record["totals"], original_channels,
            record["verification_counts"], record["unmatched_hansol"], record["unmatched_daily"],
            record["split_search_limited"], record["has_terminal"], record["run_id"], record["recorded_at"],
            cross_check_warning=record["cross_check_warning"], additional_card_review=record["additional_card_review"],
            daily_parse_warning=record["daily_parse_warning"],
        )
        if _json(rebuilt) != _json(record):
            _invalid()
        return rebuilt
    except HistoryError:
        raise
    except Exception:
        _invalid()


def project_month(records, month, branch=""):
    """서버 분석 완료 시각이 최신인 결과를 선택하고 같은 시각이면 마지막 행을 선택한다."""
    try:
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
            _invalid()
        date.fromisoformat(month + "-01")
        branch = _branch(branch) if branch else ""
        latest, seen = {}, {}
        for item in records:
            record = _validate_record(item)
            run_id, serialized = record["run_id"], _json(record)
            if run_id in seen:
                if seen[run_id] != serialized:
                    _invalid()
                continue
            seen[run_id] = serialized
            if record["settlement_date"].startswith(month + "-") and (not branch or record["branch"] == branch):
                key = (record["branch"], record["settlement_date"])
                previous = latest.get(key)
                if previous is None or datetime.fromisoformat(record["recorded_at"]) >= datetime.fromisoformat(previous["recorded_at"]):
                    latest[key] = record
        return sorted(latest.values(), key=lambda r: (r["settlement_date"], r["branch"]))
    except HistoryError:
        raise
    except Exception:
        _invalid()


class HistoryStore:
    """기록 전용 서비스 계정 인증을 받는다. 기존 일일마감 링크는 쓰거나 바꾸지 않는다."""

    def __init__(self, sheet_id, service_account_info):
        try:
            if not isinstance(sheet_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", sheet_id):
                raise ValueError
            info = dict(service_account_info)
            if info.get("type") != "service_account" or info.get("token_uri") != "https://oauth2.googleapis.com/token":
                raise ValueError
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
            self._session = AuthorizedSession(credentials)
            self._url = "https://sheets.googleapis.com/v4/spreadsheets/" + sheet_id + "/values/"
        except Exception:
            raise HistoryError(STORAGE_ERROR_MESSAGE) from None

    def _request(self, method, suffix, **kwargs):
        try:
            response = self._session.request(method, self._url + suffix, timeout=20, **kwargs)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise HistoryError(STORAGE_ERROR_MESSAGE) from None

    def _read_records(self):
        rows = self._request("GET", quote("'" + SHEET_TAB + "'", safe="")).get("values", [])
        if not isinstance(rows, list) or not rows or rows[0] != HEADERS:
            raise HistoryError(HEADER_ERROR_MESSAGE) from None
        records = []
        try:
            for row in rows[1:]:
                if not row or all(value == "" for value in row):
                    continue
                if not isinstance(row, list) or len(row) != 5:
                    _invalid()
                record = _validate_record(json.loads(row[4]))
                if row[:4] != [record[key] for key in HEADERS[:4]]:
                    _invalid()
                records.append(record)
            return records
        except HistoryError:
            raise
        except Exception:
            _invalid()

    def append_record(self, record):
        record = _validate_record(record)
        serialized = _json(record)
        for existing in self._read_records():
            if existing["run_id"] == record["run_id"]:
                if _json(existing) != serialized:
                    _invalid()
                return False
        # 동시 요청의 동일 run_id 중복 행은 월별 투영에서 제거한다. 원자적 upsert가 아니다.
        suffix = quote("'" + SHEET_TAB + "'!A:E", safe="") + ":append"
        result = self._request("POST", suffix,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": [[record[key] for key in HEADERS[:4]] + [serialized]]})
        updates = result.get("updates")
        if not isinstance(updates, dict) or updates.get("updatedRows") != 1:
            raise HistoryError(STORAGE_ERROR_MESSAGE) from None
        return True

    def list_month(self, month, branch=""):
        # shortcut: 전체 이력을 읽는다. 저장소 규모로 조회가 지연되면 DB 인덱스로 전환한다.
        return project_month(self._read_records(), month, branch)
