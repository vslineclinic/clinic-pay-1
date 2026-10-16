"""지점 경계는 위젯 선택값보다 서버 계정과 분석 소유권을 우선한다."""
from unittest.mock import Mock

import pytest


def principal(branch="인천점", username="synthetic-staff", revision="11111111-1111-4111-8111-111111111111", role="branch"):
    from branch_access import Principal
    return Principal(username, role, branch, revision)


def test_workspace_reset_preserves_only_current_identity(app):
    who = principal()
    app.st.session_state.update(_auth_principal=who, done=True, _gs_cache={"foreign": 1},
                                _ai_data="private", _access_digest="legacy", h=object(),
                                _history_transport=("synthetic-fingerprint", object()))
    app._reset_workspace()
    assert app.st.session_state == {"_auth_principal": who}
    app._reset_workspace(logout=True)
    assert all(key.startswith("_remember_") for key in app.st.session_state)
    assert app.st.session_state["_remember_command"]["action"] == "clear"


def test_history_oauth_transport_reused_without_caching_account_rows(app, monkeypatch):
    import reconciliation_history as history
    constructor = Mock(side_effect=lambda *_: object())
    monkeypatch.setattr(history, "HistoryStore", constructor)
    secrets = {"HISTORY_SHEET_ID": "synthetic-sheet", "HISTORY_SERVICE_ACCOUNT_JSON": '{"type":"service_account"}'}
    monkeypatch.setattr(app.st, "secrets", secrets)
    first = app._get_history_store()
    assert app._get_history_store() is first
    constructor.assert_called_once()
    secrets["HISTORY_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account","project_id":"synthetic-other"}'
    second = app._get_history_store()
    assert second is not first and constructor.call_count == 2
    assert "HISTORY_SERVICE_ACCOUNT_JSON" not in app.st.session_state
    secrets.clear()
    assert app._get_history_store() is None
    assert "_history_transport" not in app.st.session_state


def test_legacy_shared_password_never_grants_account_access(app, monkeypatch):
    app.st.session_state.update(_access_digest="legacy", done=True, patient="private")
    monkeypatch.setattr(app, "_get_account_store", lambda: None)
    assert app.require_app_access() is False
    assert "done" not in app.st.session_state
    assert "_access_digest" not in app.st.session_state


def test_login_callback_keeps_no_password_and_clears_previous_workspace(app, monkeypatch):
    who = principal()
    store = Mock()
    store.authenticate.return_value = who
    monkeypatch.setattr(app, "_get_account_store", lambda: store)
    app.st.session_state.update(_auth_input_username="synthetic-staff", _auth_input_password="synthetic-pw",
                                done=True, _gs_cache={"old": "private"})
    app._submit_login()
    store.authenticate.assert_called_once_with("synthetic-staff", "synthetic-pw")
    assert {k: v for k, v in app.st.session_state.items() if not k.startswith("_remember_")} == {"_auth_principal": who}


def test_failed_login_discards_previous_identity_and_password(app, monkeypatch):
    from branch_access import AccessError
    store = Mock()
    store.authenticate.side_effect = AccessError("아이디·비밀번호 또는 접근 권한을 확인해 주세요.")
    monkeypatch.setattr(app, "_get_account_store", lambda: store)
    app.st.session_state.update(_auth_principal=principal(), done=True, _auth_input_username="synthetic-staff",
                                _auth_input_password="synthetic-wrong-password", _ai_data="private")
    app._submit_login()
    assert {key for key in app.st.session_state if not key.startswith("_remember_")} == {"_auth_error"}
    assert "synthetic-wrong-password" not in repr(app.st.session_state)


def test_login_limit_stops_authentication_and_clears_submitted_password(app, monkeypatch):
    throttle = Mock()
    throttle.allow.return_value = False
    store = Mock()
    monkeypatch.setattr(app, "_get_login_throttle", lambda: throttle)
    monkeypatch.setattr(app, "_get_account_store", lambda: store)
    app.st.session_state.update(_auth_input_username="synthetic-staff", _auth_input_password="synthetic-password")
    app._submit_login()
    store.authenticate.assert_not_called()
    assert {key for key in app.st.session_state if not key.startswith("_remember_")} == {"_auth_error"}
    assert "15분" in app.st.session_state["_auth_error"]


def test_account_reset_callback_clears_new_password_even_if_save_fails(app, monkeypatch):
    from branch_access import AccessError
    who = principal(branch="all", role="admin")
    store = Mock()
    store.validate.return_value = who
    store.save_branch_account.side_effect = AccessError("계정이 변경되었습니다.")
    monkeypatch.setattr(app, "_get_account_store", lambda: store)
    app.st.session_state.update(_auth_principal=who, synthetic_branch="인천점",
                                synthetic_password="synthetic-reset-password", synthetic_active=True)
    app._submit_account_change("synthetic-staff", "old-revision", "synthetic_branch",
                               "synthetic_password", "synthetic_active")
    assert app.st.session_state["synthetic_password"] == ""
    assert "synthetic-reset-password" not in repr(app.st.session_state)
    assert "_account_error" in app.st.session_state


def test_live_account_revocation_clears_results_before_render(app, monkeypatch):
    from branch_access import AccessError
    store = Mock()
    store.validate.side_effect = AccessError("접근할 수 없습니다.")
    monkeypatch.setattr(app, "_get_account_store", lambda: store)
    app.st.session_state.update(_auth_principal=principal(), done=True, patient="private", _ai_data="old")
    assert app.require_app_access() is False
    assert "done" not in app.st.session_state and "_auth_principal" not in app.st.session_state


def test_authorized_reader_rechecks_scope_before_network(app, monkeypatch):
    from branch_access import AccessError
    who = principal()
    store = Mock()
    store.validate.return_value = who
    monkeypatch.setattr(app, "_get_account_store", lambda: store)
    monkeypatch.setattr(app, "get_clinic_daily_sheets", lambda: {"인천점": "own", "잠실점": "foreign"})
    assert app.get_authorized_daily_sheets(who) == {"인천점": "own"}
    assert app._authorized_sheet_entry(who, "인천점") == "own"
    with pytest.raises(AccessError):
        app._authorized_sheet_entry(who, "잠실점")
    monkeypatch.setattr(app, "get_clinic_daily_sheets", lambda: {})
    assert app.get_authorized_daily_sheets(who) == {}
    with pytest.raises(AccessError):
        app._authorized_sheet_entry(who, "인천점")


@pytest.mark.parametrize("change", ["username", "revision", "branch"])
def test_old_analysis_cannot_follow_account_or_branch_changes(app, change):
    from branch_access import AccessError
    who = principal()
    ss = {"done": True, "reconciliation_branch": "인천점",
          "_day_owner": app._analysis_owner(who, "인천점")}
    kwargs = {"username": "synthetic-staff", "revision": "11111111-1111-4111-8111-111111111111", "branch": "인천점"}
    kwargs[change] = {"username": "other-staff", "revision": "22222222-2222-4222-8222-222222222222", "branch": "잠실점"}[change]
    with pytest.raises(AccessError):
        app._assert_result_scope(principal(**kwargs), ss)


def test_forged_day_or_period_branch_is_rejected_not_relabelled(app):
    from branch_access import AccessError
    who = principal()
    for ss in (
        {"done": True, "reconciliation_branch": "잠실점", "_day_owner": app._analysis_owner(who, "인천점")},
        {"period_done": True, "per_gs_branch": "잠실점", "_period_owner": app._analysis_owner(who, "인천점")},
        {"done": True, "reconciliation_branch": "인천점"},
    ):
        with pytest.raises(AccessError):
            app._assert_result_scope(who, ss)


def test_hq_period_without_google_still_binds_identity(app):
    who = principal(branch="all", username="synthetic-hq", role="admin")
    ss = {"period_done": True, "per_gs_branch": None, "_period_owner": app._analysis_owner(who, "")}
    app._assert_result_scope(who, ss)
