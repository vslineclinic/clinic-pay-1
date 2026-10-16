"""정산 기록의 지점·수납일 고정과 저장 실패 안내를 검증한다."""
from datetime import date

import pandas as pd
import pytest


def _session(app):
    totals = {"h_card": None, "h_cash": None, "d_card": 100, "d_cashxfer": 0,
              "d_plat": 0, "p_card": 100, "p_cashxfer": 0, "p_plat": 0,
              "_has_hansol": False}
    return {
        "reconciliation_branch": "인천점", "reconciliation_run_id": "11111111-1111-4111-8111-111111111111",
        "reconciliation_checked_at": "2026-10-14T04:00:00+00:00",
        "reconciliation_date": "2026-10-12", "patient": pd.DataFrame({"날짜": ["2026-10-12"]}),
        "totals": totals, "channel_df": app.compute_channel_recon(totals),
        "match_df": pd.DataFrame(), "h_um": pd.DataFrame(), "d_um": pd.DataFrame(),
        "p1_diff": pd.DataFrame(), "has_hansol": False,
    }


def _verification():
    return {key: pd.DataFrame() for key in (
        "유형B_차트번호오타", "유형C1_한쪽만존재", "유형C2_금액불일치", "유형C3_결제수단불일치")}


def test_history_uses_analysis_identity_not_monthly_browse_selection(app):
    ss = _session(app)
    ss.update(gs_branch="압구정", history_branch="잠실점", gs_date=date(2026, 10, 14))
    record = app._history_snapshot_from_session(ss, _verification())
    assert record["branch"] == "인천점"
    assert record["settlement_date"] == "2026-10-12"


@pytest.mark.parametrize("days", [["2026-10-12", "2026-10-13"], [None], ["2026-10-11"], ["bad"]])
def test_history_refuses_unvalidated_or_changed_result_date(app, days):
    from reconciliation_history import HistoryError
    ss = _session(app)
    ss["patient"] = pd.DataFrame({"날짜": days})
    with pytest.raises(HistoryError):
        app._history_snapshot_from_session(ss, _verification())


def test_history_without_branch_does_not_guess(app):
    ss = _session(app)
    ss.pop("reconciliation_branch")
    assert app._history_snapshot_from_session(ss, _verification()) is None


def test_history_keeps_crosscheck_and_search_warnings(app):
    ss = _session(app)
    ss["cross_check_warning"] = True
    ss["match_df"].attrs["split_search_limited"] = True
    record = app._history_snapshot_from_session(ss, _verification())
    assert record["status"] == "검토 필요"
    assert record["cross_check_warning"] and record["split_search_limited"]
    row = app._history_month_table("2026-10", [record]).query("날짜 == '2026-10-12'").iloc[0]
    assert "탐색 한도" in row["검토 사유"] and "교차검증" in row["검토 사유"]


def test_history_keeps_zero_daily_parse_warning(app):
    ss = _session(app)
    ss["daily_parse_warning"] = True
    record = app._history_snapshot_from_session(ss, _verification())
    assert record["status"] == "검토 필요" and record["daily_parse_warning"]


def test_month_table_includes_unrecorded_days_without_claiming_missed_closing(app):
    record = app._history_snapshot_from_session(_session(app), _verification())
    frame = app._history_month_table("2026-10", [record])
    assert len(frame) == 31
    assert frame.loc[frame["날짜"] == "2026-10-12", "상태"].item() == "단말기 미검증"
    assert frame.loc[frame["날짜"] == "2026-10-13", "상태"].item() == "기록 없음"
    assert pd.isna(frame.loc[frame["날짜"] == "2026-10-13", "합계 차이"].item())


def test_month_table_preserves_channel_offsets_and_sign(app):
    ss = _session(app)
    ss["totals"].update(d_card=90, d_cashxfer=10)
    ss["channel_df"] = app.compute_channel_recon(ss["totals"])
    record = app._history_snapshot_from_session(ss, _verification())
    row = app._history_month_table("2026-10", [record]).query("날짜 == '2026-10-12'").iloc[0]
    assert row["합계 차이"] == 0
    assert row["카드 차이"] == -10 and row["현금+이체 차이"] == 10
    assert row["상태"] == "검토 필요"


def test_missing_history_configuration_never_uses_daily_reader_credentials(app, monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {"gcp_service_account": {"private_key": "daily-only"}})
    assert app._get_history_store() is None


@pytest.mark.parametrize("credentials", ["{secret-bad-json", "[]", "null", 123])
def test_bad_history_credentials_are_masked(app, monkeypatch, credentials):
    from reconciliation_history import HistoryError
    monkeypatch.setattr(app.st, "secrets", {"HISTORY_SHEET_ID": "sheet", "HISTORY_SERVICE_ACCOUNT_JSON": credentials})
    with pytest.raises(HistoryError) as caught:
        app._get_history_store()
    assert "secret-bad-json" not in str(caught.value)
