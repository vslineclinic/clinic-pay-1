"""개발 진입점은 운영 계정/기기/정산 쓰기를 호출하지 않는다."""
from unittest.mock import Mock
from uuid import uuid4

import pytest

from branch_access import AccessError, Principal


def who(admin=True):
    return Principal('synthetic-hq' if admin else 'synthetic-staff', 'admin' if admin else 'branch',
                     'all' if admin else '인천점', str(uuid4()))


@pytest.mark.parametrize('logout', [False, True])
def test_development_reset_retains_mode_without_touching_cookie_store(app, monkeypatch, logout):
    tripwire = Mock(side_effect=AssertionError('operating remember store'))
    monkeypatch.setattr(app, '_get_remember_store', tripwire)
    app.st.session_state.update(_development_mode=True, _auth_principal=who(),
                                _remember_token='legacy', done=True, patient='private')
    app._reset_workspace(logout=logout)
    assert app.st.session_state['_development_mode'] is True
    assert 'patient' not in app.st.session_state
    assert '_remember_token' not in app.st.session_state
    assert ('_auth_principal' not in app.st.session_state) == logout
    tripwire.assert_not_called()


def test_development_login_rejects_branch_before_storing_identity_or_issuing_token(app, monkeypatch):
    accounts = Mock()
    accounts.authenticate.return_value = who(False)
    tripwire = Mock(side_effect=AssertionError('remember writes forbidden'))
    monkeypatch.setattr(app, '_get_account_store', lambda: accounts)
    monkeypatch.setattr(app, '_get_remember_store', tripwire)
    app.st.session_state.update(_development_mode=True, _auth_input_username='synthetic',
                                _auth_input_password='synthetic-secret', _auth_remember=True)
    app._submit_login()
    assert '_auth_principal' not in app.st.session_state
    assert '총괄관리자' in app.st.session_state['_auth_error']
    assert 'synthetic-secret' not in repr(app.st.session_state)
    tripwire.assert_not_called()


def test_development_fresh_auth_ignores_cookies_and_denies_branch(app, monkeypatch):
    accounts, browser = Mock(), Mock(side_effect=AssertionError('cookie auth forbidden'))
    principal = who(False)
    accounts.validate.return_value = principal
    monkeypatch.setattr(app, '_get_account_store', lambda: accounts)
    monkeypatch.setattr(app, '_sync_remember_browser', browser)
    app.st.session_state.update(_development_mode=True, _auth_principal=principal)
    assert app.require_app_access() is False
    assert '_auth_principal' not in app.st.session_state
    browser.assert_not_called()


def test_development_account_callback_clears_password_but_never_mutates_operating_accounts(app, monkeypatch):
    accounts = Mock()
    accounts.validate.return_value = who()
    monkeypatch.setattr(app, '_get_account_store', lambda: accounts)
    app.st.session_state.update(_development_mode=True, branch='인천점', pw='synthetic-secret', active=True)
    app._submit_account_change('staff', 'revision', 'branch', 'pw', 'active')
    accounts.save_branch_account.assert_not_called()
    assert app.st.session_state['pw'] == ''
    assert '개발' in app.st.session_state['_account_error']


def test_development_history_uses_only_separate_store(app, monkeypatch):
    principal, separate = who(), Mock()
    separate.append_summary.return_value = True
    monkeypatch.setattr(app, '_validated_principal', lambda p=None: principal)
    monkeypatch.setattr(app, '_get_development_store', lambda: separate)
    operating = Mock(side_effect=AssertionError('operating history forbidden'))
    monkeypatch.setattr(app, '_get_history_store', operating)
    app.st.session_state['_development_mode'] = True
    store = app._get_scoped_history_store(principal)
    assert store.append_record({'synthetic': True}) is True
    separate.append_summary.assert_called_once_with(principal, {'synthetic': True}, source_manifest=None)
    operating.assert_not_called()


def test_selecting_ledger_for_analysis_clears_old_results_and_keeps_only_scope(app, monkeypatch):
    principal = who()
    monkeypatch.setattr(app, '_validated_principal', lambda p=None: principal)
    app.st.session_state.update(_development_mode=True, _auth_principal=principal, done=True,
                                patient='old private', _ai_result='old result')
    selection = {'branch': '인천점', 'business_date': '2026-10-16', 'revision': 8, 'daily': 'browser must not choose amounts'}
    app._select_ledger_analysis(selection)
    assert app.st.session_state['_development_mode'] is True
    assert app.st.session_state['_auth_principal'] == principal
    assert app.st.session_state['_ledger_selection'] == {k: selection[k] for k in ('branch', 'business_date', 'revision')}
    assert app.st.session_state['workspace_view'] == '마감 대조'
    assert app.st.session_state['daily_mode'] == '수납원장'
    assert 'patient' not in app.st.session_state and '_ai_result' not in app.st.session_state


def test_bad_development_database_is_a_history_error_not_operating_fallback(app, monkeypatch):
    from receipt_ledger import LedgerError
    from reconciliation_history import HistoryError
    principal = who()
    monkeypatch.setattr(app, '_validated_principal', lambda p=None: principal)
    monkeypatch.setattr(app, '_get_development_store', Mock(side_effect=LedgerError('합성 DB 오류')))
    operating = Mock(side_effect=AssertionError('operating fallback'))
    monkeypatch.setattr(app, '_get_history_store', operating)
    app.st.session_state['_development_mode'] = True
    with pytest.raises(HistoryError):
        app._get_scoped_history_store(principal)
    operating.assert_not_called()


def test_revocation_during_ledger_freshness_clears_patient_state(app, monkeypatch):
    principal, store = who(), Mock()
    store.load_day.side_effect = AccessError('합성 권한 취소')
    monkeypatch.setattr(app, '_get_development_store', lambda: store)
    app.st.session_state.update(_development_mode=True, _auth_principal=principal, patient='private',
                                _analysis_ledger={'branch': '인천점', 'business_date': '2026-10-16', 'revision': 1})
    app._render_ledger_freshness(app.st.session_state, principal)
    assert 'patient' not in app.st.session_state
    assert '_auth_principal' not in app.st.session_state
    assert app.st.session_state['_development_mode'] is True


def test_development_history_carries_only_matching_ledger_scope(app, monkeypatch):
    import staging
    principal, separate = who(), Mock()
    monkeypatch.setattr(app, '_validated_principal', lambda p=None: principal)
    monkeypatch.setattr(app, '_get_development_store', lambda: separate)
    constructor = Mock(return_value='separate history')
    monkeypatch.setattr(staging, 'DevelopmentHistory', constructor)
    source = {'branch': '인천점', 'business_date': '2026-10-16', 'revision': 8}
    app.st.session_state.update(_development_mode=True, reconciliation_branch='인천점',
                                reconciliation_date='2026-10-16', _analysis_ledger=source)
    assert app._get_scoped_history_store(principal) == 'separate history'
    constructor.assert_called_once_with(separate, principal, source_manifest=source)
    app.st.session_state['reconciliation_branch'] = '잠실점'
    from reconciliation_history import HistoryError
    with pytest.raises(HistoryError):
        app._get_scoped_history_store(principal)


def test_reset_preserves_only_current_actor_unsent_queue_and_logout_clears_it(app):
    from ledger_component import component_scope
    principal = who()
    actor = component_scope(f'{principal.username}:{principal.revision}', '', '')
    own = '_ledger_client_' + actor
    scopes = '_ledger_scopes_' + actor
    app.st.session_state.update(_development_mode=True, _auth_principal=principal,
                                **{own: {'raw': 'unsent'}, scopes: {'scope'},
                                   '_ledger_client_foreign': {'raw': 'foreign'}, '_ai_data': 'private'})
    app._reset_workspace()
    assert app.st.session_state[own] == {'raw': 'unsent'}
    assert app.st.session_state[scopes] == {'scope'}
    assert '_ledger_client_foreign' not in app.st.session_state
    assert '_ai_data' not in app.st.session_state
    app._reset_workspace(logout=True)
    assert set(app.st.session_state) == {'_development_mode'}


def test_ledger_validator_clears_raw_queue_on_poll_revocation(app, monkeypatch):
    principal = who()
    monkeypatch.setattr(app, '_validated_principal', Mock(side_effect=AccessError('취소됨')))
    app.st.session_state.update(_development_mode=True, _auth_principal=principal,
                                _ledger_client_private='private')
    with pytest.raises(AccessError):
        app._validated_ledger_principal(principal)
    assert set(app.st.session_state) == {'_development_mode'}
