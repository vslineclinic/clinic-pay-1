"""환자 정보 없이 보관하는 일자별 정산 요약과 Sheets 저장 경계."""
import importlib.util
import copy
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

RUN_A = "a1111111-1111-4111-8111-111111111111"
RUN_B = "b2222222-2222-4222-8222-222222222222"
SYNTHETIC_CREDENTIALS = {
    "type": "service_account", "token_uri": "https://oauth2.googleapis.com/token",
    "private_key": "synthetic-only",
}
VERIFICATION_KEYS = (
    "유형B_차트번호오타", "유형C1_한쪽만존재",
    "유형C2_금액불일치", "유형C3_결제수단불일치",
)


def test_history_module_is_import_safe():
    assert importlib.util.find_spec("reconciliation_history") is not None


@pytest.fixture
def history():
    import reconciliation_history
    return reconciliation_history


def _record(history, app, *, totals=None, run_id=RUN_A, **kwargs):
    values = {
        "h_card": 100, "h_cash": 20,
        "d_card": 100, "d_cashxfer": 20, "d_plat": 10,
        "p_card": 100, "p_cashxfer": 20, "p_plat": 10,
        "_has_hansol": True,
    }
    if totals:
        values.update(totals)
    channel = kwargs.pop("channel_df", None)
    if channel is None:
        channel = app.compute_channel_recon(values)
    args = {
        "branch": "합성지점", "settlement_date": "2026-10-14",
        "totals": values, "channel_df": channel,
        "verification_counts": {key: 0 for key in VERIFICATION_KEYS},
        "unmatched_hansol": 0, "unmatched_daily": 0,
        "split_search_limited": False, "has_terminal": values["_has_hansol"],
        "run_id": run_id, "checked_at": "2026-10-14T10:00:00+00:00",
    }
    args.update(kwargs)
    return history.make_record(**args)


def test_summary_allowlist_drops_patient_names_and_raw_input(history, app):
    totals = {"환자": "실제 자료 대신 합성 이름", "차트번호": "synthetic-chart"}
    channel = app.compute_channel_recon(_record(history, app)["totals"])
    channel["메모"] = "비공개 원문을 저장하면 안 됨"
    counts = {key: 0 for key in VERIFICATION_KEYS}
    counts["원본"] = [{"이름": "비공개 합성 이름"}]
    record = _record(history, app, totals=totals, channel_df=channel, verification_counts=counts)
    serialized = json.dumps(record, ensure_ascii=False, allow_nan=False)
    assert "실제 자료 대신" not in serialized
    assert "synthetic-chart" not in serialized
    assert "비공개" not in serialized
    assert set(record["verification_counts"]) == set(VERIFICATION_KEYS)
    assert record["status"] == "일치"
    assert record["gap"] == 0


def test_signed_offsets_remain_review_when_total_gap_is_zero(history, app):
    record = _record(history, app, totals={"d_card": 80, "d_cashxfer": 40})
    assert record["gap"] == 0
    assert record["status"] == "검토 필요"
    assert [r["daily_minus_vegas"] for r in record["channels"]] == [-20, 20, 0]


@pytest.mark.parametrize("key", VERIFICATION_KEYS)
def test_any_verification_count_prevents_match(history, app, key):
    counts = {k: 0 for k in VERIFICATION_KEYS}
    counts[key] = 1
    assert _record(history, app, verification_counts=counts)["status"] == "검토 필요"


@pytest.mark.parametrize("field,value", [
    ("unmatched_hansol", 1), ("unmatched_daily", 1),
    ("split_search_limited", True), ("cross_check_warning", True),
    ("additional_card_review", 1), ("daily_parse_warning", True),
])
def test_incomplete_checks_prevent_match(history, app, field, value):
    assert _record(history, app, **{field: value})["status"] == "검토 필요"


def test_card_terminal_mismatch_counts_but_cash_receipt_scope_does_not(history, app):
    assert _record(history, app, totals={"h_card": 110})["status"] == "검토 필요"
    cash_reference = _record(history, app, totals={"h_cash": 999})
    assert cash_reference["status"] == "일치"
    assert cash_reference["channels"][1]["terminal_minus_vegas"] == 979


def test_missing_terminal_is_explicit_and_daily_gap_takes_priority(history, app):
    totals = {"h_card": None, "h_cash": None, "_has_hansol": False}
    record = _record(history, app, totals=totals)
    assert record["status"] == "단말기 미검증"
    assert record["has_terminal"] is False
    assert record["channels"][0]["terminal"] is None
    record = _record(history, app, totals={**totals, "d_card": 90})
    assert record["gap"] == -10
    assert record["status"] == "검토 필요"
    assert record["has_terminal"] is False


def test_zero_totals_keep_daily_parser_uncertainty_after_storage_validation(history, app):
    totals = {key: 0 for key in ("h_card", "h_cash", "d_card", "d_cashxfer", "d_plat", "p_card", "p_cashxfer", "p_plat")}
    record = _record(history, app, totals=totals, daily_parse_warning=True)
    assert record["gap"] == 0
    assert record["status"] == "검토 필요"
    assert record["daily_parse_warning"] is True
    assert history.project_month([record], "2026-10") == [record]


def test_integral_numpy_money_is_json_safe(history, app):
    record = _record(history, app, totals={"d_card": np.int64(100), "p_card": np.float64(100)})
    assert type(record["totals"]["d_card"]) is int
    assert json.loads(json.dumps(record, allow_nan=False))["gap"] == 0


@pytest.mark.parametrize("value", [True, "100", 1.2, float("nan"), float("inf")])
def test_invalid_money_is_rejected_without_values(history, app, value):
    channel = app.compute_channel_recon(_record(history, app)["totals"])
    with pytest.raises(history.HistoryError) as error:
        _record(history, app, totals={"d_card": value}, channel_df=channel)
    assert str(error.value) == history.INVALID_RECORD_MESSAGE


@pytest.mark.parametrize("kwargs", [
    {"branch": ""}, {"branch": "가" * 81}, {"branch": "가\n나"},
    {"settlement_date": "2026-02-30"}, {"settlement_date": "26-10-14"},
    {"run_id": "raw-patient-name"}, {"checked_at": "2026-10-14"},
    {"checked_at": datetime(2026, 10, 14)},
    {"unmatched_daily": -1}, {"split_search_limited": "false"}, {"daily_parse_warning": "false"},
    {"has_terminal": False}, {"verification_counts": {"유형B_차트번호오타": -1}},
])
def test_invalid_record_boundary_is_masked(history, app, kwargs):
    with pytest.raises(history.HistoryError) as error:
        _record(history, app, **kwargs)
    assert str(error.value) == history.INVALID_RECORD_MESSAGE


def test_channel_summary_must_agree_with_totals(history, app):
    channel = app.compute_channel_recon(_record(history, app)["totals"])
    channel.loc[0, "일마-차트"] = 99
    with pytest.raises(history.HistoryError):
        _record(history, app, channel_df=channel)


def test_checked_time_is_normalized_to_utc_and_run_id_is_generated(history, app):
    record = _record(history, app, run_id=None, checked_at="2026-10-14T19:00:00+09:00")
    assert record["recorded_at"] == "2026-10-14T10:00:00+00:00"
    assert len(record["run_id"]) == 36


def test_month_projection_uses_server_analysis_time_and_ignores_retries(history, app):
    first = _record(history, app, totals={"d_card": 90}, checked_at="2026-10-14T10:00:00+00:00")
    latest = _record(history, app, run_id=RUN_B, checked_at="2026-10-14T20:00:00+00:00")
    records = [first, latest, copy.deepcopy(first)]
    assert history.project_month(records, "2026-10") == [latest]
    assert len(records) == 3


def test_month_projection_parses_fractional_time_and_uses_last_row_for_ties(history, app):
    older = _record(history, app, totals={"d_card": 90}, checked_at="2026-10-14T10:00:00+00:00")
    newer = _record(history, app, run_id=RUN_B, checked_at="2026-10-14T10:00:00.000001+00:00")
    assert history.project_month([newer, older], "2026-10") == [newer]
    tied = _record(history, app, run_id=RUN_B, checked_at=older["recorded_at"])
    assert history.project_month([older, tied], "2026-10") == [tied]


def test_month_projection_keeps_branch_and_date_separate(history, app):
    first = _record(history, app)
    second = _record(history, app, branch="다른 합성지점", run_id=RUN_B)
    assert len(history.project_month([first, second], "2026-10")) == 2
    assert history.project_month([first, second], "2026-10", branch="합성지점") == [first]
    assert history.project_month([first, second], "2026-09") == []
    with pytest.raises(history.HistoryError):
        history.project_month([first], "2026-13")


@pytest.mark.parametrize("tamper", [
    lambda r: r.update(status="일치"),
    lambda r: r.update(gap=0),
    lambda r: r.update(raw_patient="비공개 합성 이름"),
    lambda r: r["channels"][0].update(daily_minus_vegas=0),
])
def test_loaded_summary_does_not_trust_tampered_derived_values(history, app, tamper):
    record = _record(history, app, totals={"d_card": 90})
    tamper(record)
    with pytest.raises(history.HistoryError) as error:
        history.project_month([record], "2026-10")
    assert str(error.value) == history.INVALID_RECORD_MESSAGE


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _MemorySheets:
    """HTTP 경계만 대체하고 실제 행 추기와 다시 읽기를 재현한다."""
    def __init__(self, header):
        self.rows = [list(header)]
        self.calls = []
        self.fail = None
        self.append_then_fail = False
        self.fail_before_append = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.fail:
            raise RuntimeError(self.fail)
        if method == "GET":
            return _Response({"values": copy.deepcopy(self.rows)})
        assert method == "POST"
        if self.fail_before_append:
            self.fail_before_append = False
            raise RuntimeError("private-key and https://private.example pre-write timeout")
        self.rows.extend(copy.deepcopy(kwargs["json"]["values"]))
        if self.append_then_fail:
            self.append_then_fail = False
            raise RuntimeError("private-key and https://private.example leaked timeout")
        return _Response({"updates": {"updatedRows": 1}})


@pytest.fixture
def sheets(history, monkeypatch):
    memory = _MemorySheets(history.HEADERS)
    credentials_calls = []
    def credentials(info, scopes):
        credentials_calls.append((info, scopes))
        return object()
    monkeypatch.setattr(history.service_account.Credentials, "from_service_account_info", credentials)
    monkeypatch.setattr(history, "AuthorizedSession", lambda _: memory)
    store = history.HistoryStore("synthetic_history_sheet_id", SYNTHETIC_CREDENTIALS)
    return store, memory, credentials_calls


def test_append_is_raw_and_same_run_retries_without_duplicate(history, app, sheets):
    store, memory, credentials_calls = sheets
    record = _record(history, app, branch="=FORMULA_SAFE_BRANCH")
    assert store.append_record(record) is True
    assert store.append_record(record) is False
    assert len(memory.rows) == 2
    post = next(c for c in memory.calls if c[0] == "POST")
    assert post[2]["params"]["valueInputOption"] == "RAW"
    assert post[2]["params"]["insertDataOption"] == "INSERT_ROWS"
    assert post[2]["json"]["values"][0][0] == "=FORMULA_SAFE_BRANCH"
    assert credentials_calls[0][1] == ["https://www.googleapis.com/auth/spreadsheets"]
    assert store.list_month("2026-10") == [record]


def test_append_timeout_can_retry_without_duplicate_or_data_loss(history, app, sheets):
    store, memory, _ = sheets
    record = _record(history, app)
    memory.append_then_fail = True
    with pytest.raises(history.HistoryError) as error:
        store.append_record(record)
    assert str(error.value) == history.STORAGE_ERROR_MESSAGE
    assert store.append_record(record) is False
    assert store.list_month("2026-10") == [record]
    assert len(memory.rows) == 2


def test_revised_day_appends_snapshot_and_new_store_reads_latest(history, app, sheets):
    store, memory, _ = sheets
    first = _record(history, app, totals={"d_card": 90})
    latest = _record(history, app, run_id=RUN_B)
    store.append_record(first)
    store.append_record(latest)
    restarted = history.HistoryStore("synthetic_history_sheet_id", SYNTHETIC_CREDENTIALS)
    assert restarted.list_month("2026-10") == [latest]
    assert len(memory.rows) == 3


def test_failed_old_save_retried_after_new_analysis_never_replaces_latest(history, app, sheets):
    store, memory, _ = sheets
    older = _record(history, app, totals={"d_card": 90}, checked_at="2026-10-14T10:00:00+00:00")
    newer = _record(history, app, run_id=RUN_B, checked_at="2026-10-14T11:00:00+00:00")
    memory.fail_before_append = True
    with pytest.raises(history.HistoryError):
        store.append_record(older)
    assert store.append_record(newer) is True
    assert store.append_record(older) is True
    assert store.list_month("2026-10") == [newer]
    assert len(memory.rows) == 3


def test_same_run_id_with_different_payload_is_rejected(history, app, sheets):
    store, memory, _ = sheets
    store.append_record(_record(history, app))
    with pytest.raises(history.HistoryError):
        store.append_record(_record(history, app, totals={"d_card": 90}))
    assert len(memory.rows) == 2


@pytest.mark.parametrize("bad_header", [[], ["branch"], ["branch", "settlement_date", "run_id", "recorded_at", "payload_json", "extra"]])
def test_header_drift_prevents_reads_and_writes(history, app, sheets, bad_header):
    store, memory, _ = sheets
    memory.rows = [bad_header]
    with pytest.raises(history.HistoryError) as error:
        store.append_record(_record(history, app))
    assert str(error.value) == history.HEADER_ERROR_MESSAGE
    assert not any(c[0] == "POST" for c in memory.calls)
    with pytest.raises(history.HistoryError):
        store.list_month("2026-10")


def test_corrupt_sheet_row_is_not_silently_dropped(history, app, sheets):
    store, memory, _ = sheets
    store.append_record(_record(history, app))
    memory.rows.append(["합성지점", "2026-10-14", RUN_B, "2026-10-14T10:00:00+00:00", "not-json"])
    with pytest.raises(history.HistoryError):
        store.list_month("2026-10")


@pytest.mark.parametrize("failure", [
    "403 https://sheets.googleapis.com/private-sheet SECRET_KEY",
    "private_key unauthorized permission denied",
])
def test_storage_failures_never_expose_remote_urls_or_credentials(history, app, sheets, failure):
    store, memory, _ = sheets
    memory.fail = failure
    with pytest.raises(history.HistoryError) as error:
        store.append_record(_record(history, app))
    assert str(error.value) == history.STORAGE_ERROR_MESSAGE
    assert "https" not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("bad_result", [{}, {"updates": None}, {"updates": "private response"}])
def test_malformed_append_response_is_masked(history, app, sheets, monkeypatch, bad_result):
    store, memory, _ = sheets
    original_request = memory.request
    def request(method, url, **kwargs):
        if method == "POST":
            return _Response(bad_result)
        return original_request(method, url, **kwargs)
    monkeypatch.setattr(memory, "request", request)
    with pytest.raises(history.HistoryError) as error:
        store.append_record(_record(history, app))
    assert str(error.value) == history.STORAGE_ERROR_MESSAGE


@pytest.mark.parametrize("sheet_id,credentials", [
    ("https://private.example/sheet", {}), ("synthetic_history_sheet_id", {}),
])
def test_missing_or_invalid_credentials_do_not_expose_configuration(history, sheet_id, credentials):
    with pytest.raises(history.HistoryError) as error:
        history.HistoryStore(sheet_id, credentials)
    assert str(error.value) == history.STORAGE_ERROR_MESSAGE


@pytest.mark.parametrize("overrides", [
    {"type": "authorized_user"}, {"token_uri": "https://private.example/steal-jwt"},
    {"token_uri": "https://oauth2.googleapis.com/token?redirect=private"},
])
def test_wrong_credential_type_or_token_endpoint_never_constructs_auth_or_transport(history, monkeypatch, overrides):
    calls = []
    def credentials(*args, **kwargs):
        calls.append("credentials")
        return object()
    def session(*args):
        calls.append("transport")
        return object()
    monkeypatch.setattr(history.service_account.Credentials, "from_service_account_info", credentials)
    monkeypatch.setattr(history, "AuthorizedSession", session)
    with pytest.raises(history.HistoryError) as error:
        history.HistoryStore("synthetic_history_sheet_id", {**SYNTHETIC_CREDENTIALS, **overrides})
    assert str(error.value) == history.STORAGE_ERROR_MESSAGE
    assert calls == []
