"""저장된 기기 토큰은 암호와 분리하고 쿠키 응답을 권한으로 신뢰하지 않는다."""
from unittest.mock import Mock

import pytest


def who(branch="인천점", revision="11111111-1111-4111-8111-111111111111"):
    from branch_access import Principal
    return Principal("synthetic-staff", "branch", branch, revision)


@pytest.fixture
def stores(app, monkeypatch):
    import remember_browser
    accounts, remembered = Mock(), Mock()
    accounts.authenticate.return_value = accounts.validate.return_value = who()
    remembered.issue.return_value = "synthetic-opaque-token"
    remembered.restore.return_value = who()
    monkeypatch.setattr(app, "_get_account_store", lambda: accounts)
    monkeypatch.setattr(app, "_get_remember_store", lambda: remembered)
    monkeypatch.setattr(remember_browser, "render", lambda command=None: None)
    return accounts, remembered


def test_regular_reset_retains_remember_login_but_clears_patient_state(app, stores):
    app.st.session_state.update(_auth_principal=who(), _remember_token="synthetic-opaque-token",
                                _remember_command={"action": "set", "nonce": "synthetic-nonce"},
                                patient="private", _ai_data="private", _gs_cache={"private": 1})
    app._reset_workspace()
    assert app.st.session_state["_auth_principal"] == who()
    assert app.st.session_state["_remember_token"] == "synthetic-opaque-token"
    assert "patient" not in app.st.session_state and "_gs_cache" not in app.st.session_state


def test_logout_revokes_token_and_waits_for_matching_delete_ack(app, stores, monkeypatch):
    import remember_browser
    app.st.session_state.update(_auth_principal=who(), _remember_token="synthetic-opaque-token", patient="private")
    app._reset_workspace(logout=True)
    stores[1].revoke.assert_called_once_with("synthetic-opaque-token")
    command = app.st.session_state["_remember_command"]
    assert command["action"] == "clear" and app.st.session_state["_remember_block_restore"]
    monkeypatch.setattr(remember_browser, "render", lambda command=None: {
        "ready": True, "token": "synthetic-opaque-token", "ack": "stale-nonce", "ok": True})
    app._sync_remember_browser()
    stores[1].restore.assert_not_called()
    assert app.st.session_state["_remember_command"] == command
    assert "_auth_principal" not in app.st.session_state and "patient" not in app.st.session_state
    monkeypatch.setattr(remember_browser, "render", lambda command=None: {
        "ready": True, "token": "", "ack": command["nonce"], "ok": True})
    app._sync_remember_browser()
    assert "_remember_command" not in app.st.session_state
    assert "_auth_principal" not in app.st.session_state


def test_fresh_session_restores_principal_from_server_only(app, stores, monkeypatch):
    import remember_browser
    monkeypatch.setattr(remember_browser, "render", lambda command=None: {
        "ready": True, "token": "synthetic-opaque-token", "ack": "", "ok": True,
        "branch": "잠실점", "role": "admin"})
    app._sync_remember_browser()
    stores[1].restore.assert_called_once_with("synthetic-opaque-token")
    assert app.st.session_state["_auth_principal"] == who()


def test_remembered_session_rechecks_server_token_on_every_rerun(app, stores):
    app.st.session_state.update(_auth_principal=who(), _remember_token="synthetic-opaque-token")
    assert app.require_app_access() is True
    assert app.require_app_access() is True
    assert stores[1].restore.call_count == 2


def test_token_expiry_or_revocation_clears_authenticated_workspace(app, stores):
    from branch_access import AccessError
    stores[1].restore.side_effect = AccessError("로그인 유지가 만료되었습니다.")
    app.st.session_state.update(_auth_principal=who(), _remember_token="synthetic-opaque-token", patient="private")
    assert app.require_app_access() is False
    assert "_auth_principal" not in app.st.session_state and "patient" not in app.st.session_state
    assert app.st.session_state["_remember_command"]["action"] == "clear"


def test_revoked_remember_token_blocks_account_callback_before_main(app, stores):
    from branch_access import AccessError, Principal
    admin = Principal("synthetic-hq", "admin", "all", "11111111-1111-4111-8111-111111111111")
    stores[0].validate.return_value = admin
    stores[1].restore.side_effect = AccessError("로그인 유지가 해제되었습니다.")
    app.st.session_state.update(_auth_principal=admin, _remember_token="synthetic-opaque-token",
                                synthetic_branch="인천점", synthetic_password="synthetic-password", synthetic_active=True)
    app._submit_account_change("synthetic-staff", "old-revision", "synthetic_branch", "synthetic_password", "synthetic_active")
    stores[0].save_branch_account.assert_not_called()
    assert app.st.session_state["synthetic_password"] == ""


def test_optional_token_issue_failure_keeps_successful_password_login(app, stores):
    from branch_access import AccessError
    stores[1].issue.side_effect = AccessError("합성 저장 오류")
    app.st.session_state.update(_auth_input_username="synthetic-staff", _auth_input_password="synthetic-password",
                                _auth_remember=True)
    app._submit_login()
    assert app.st.session_state["_auth_principal"] == who()
    assert "_remember_token" not in app.st.session_state
    assert "synthetic-password" not in repr(app.st.session_state)
    assert "_remember_notice" in app.st.session_state


def test_blocked_cookie_falls_back_to_regular_login_without_retry_loop(app, stores, monkeypatch):
    import remember_browser
    command = {"action": "set", "nonce": "synthetic-nonce", "token": "synthetic-opaque-token", "max_age": 100}
    app.st.session_state.update(_auth_principal=who(), _remember_token="synthetic-opaque-token", _remember_command=command)
    monkeypatch.setattr(remember_browser, "render", lambda command=None: {
        "ready": True, "token": "", "ack": "synthetic-nonce", "ok": False})
    app._sync_remember_browser()
    assert app.st.session_state["_auth_principal"] == who()
    assert "_remember_token" not in app.st.session_state and "_remember_command" not in app.st.session_state
    stores[1].revoke.assert_called_once_with("synthetic-opaque-token")
    app._sync_remember_browser()
    assert stores[1].revoke.call_count == 1 and "_remember_notice" in app.st.session_state


def test_failed_revocation_survives_another_login_and_retries_all_tokens(app, stores):
    from branch_access import AccessError
    stores[1].revoke.side_effect = AccessError("합성 연결 실패")
    app.st.session_state.update(_auth_principal=who(), _remember_token="synthetic-old-token")
    app._reset_workspace(logout=True)
    app.st.session_state.update(_auth_input_username="synthetic-staff", _auth_input_password="synthetic-password")
    app._submit_login()
    assert app.st.session_state["_remember_revoke_pending"] == ["synthetic-old-token"]
    app.st.session_state["_remember_token"] = "synthetic-new-token"
    app._reset_workspace(logout=True)
    assert app.st.session_state["_remember_revoke_pending"] == ["synthetic-old-token", "synthetic-new-token"]
    stores[1].revoke.side_effect = None
    app._sync_remember_browser()
    assert "_remember_revoke_pending" not in app.st.session_state
    assert stores[1].revoke.call_args_list[-2].args == ("synthetic-old-token",)
    assert stores[1].revoke.call_args_list[-1].args == ("synthetic-new-token",)
