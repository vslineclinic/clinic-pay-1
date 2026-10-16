"""Standalone web boundary tests use synthetic accounts and real SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from branch_access import AccessError, Principal
from receipt_ledger import LedgerStore
from web_server import COOKIE, MAX_BODY, Sessions, create_app


ORIGIN = 'https://ledger.example.test'
BRANCH, DAY = '강남점', '2026-10-16'


class Accounts:
    def __init__(self):
        self.admin = Principal('synthetic-admin', 'admin', 'all', str(uuid4()))
        self.staff = Principal('synthetic-staff', 'branch', BRANCH, str(uuid4()))
        self.active = True
        self.validations = 0
        self.authentications = 0

    def authenticate(self, username, password):
        self.authentications += 1
        if self.active and password == 'synthetic-password':
            if username == self.admin.username:
                return self.admin
            if username == self.staff.username:
                return self.staff
        raise AccessError()

    def validate(self, principal):
        self.validations += 1
        if self.active and principal == self.admin:
            return principal
        raise AccessError()


@pytest.fixture
def server(tmp_path):
    accounts = Accounts()
    app = create_app(accounts=accounts,
                     store_factory=lambda validate: LedgerStore(str(tmp_path / 'web.sqlite'), validate, local=True),
                     public_origin=ORIGIN, static_dir=tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        yield app, accounts, client


def login(client, username='synthetic-admin', password='synthetic-password'):
    bootstrap = client.get('/api/session')
    assert bootstrap.status_code == 200
    result = client.post('/api/session', json={'username': username, 'password': password},
                         headers={'Origin': ORIGIN, 'X-CSRF-Token': bootstrap.json()['csrf_token']})
    if result.status_code == 200:
        client.headers.update({'Origin': ORIGIN, 'X-CSRF-Token': result.json()['csrf_token']})
    return result


def workspace(client, branch=BRANCH, day=DAY):
    result = client.get('/api/ledger', params={'branch': branch, 'date': day})
    assert result.status_code == 200, result.text
    return result.json()


def action(scope, *, receipt_id=None, request_id=None, revision=0, draft_revision=0, amount='1000', **raw):
    return {'branch': BRANCH, 'date': DAY, 'scope': scope, 'action': {
        'scope': scope, 'action': 'save', 'id': receipt_id or str(uuid4()),
        'request_id': request_id or str(uuid4()), 'receipt_revision': revision,
        'draft_revision': draft_revision, 'attempt': 1,
        'raw': {'chart_no': '000123', 'patient_name': '합성환자', 'payments': {'카드': amount}, **raw},
    }}


def send(client, payload):
    result = client.post('/api/ledger/actions', json=payload)
    assert result.status_code == 200, result.text
    return result.json()['ack']


def test_session_cookie_rotation_and_logout_invalidates_old_token(server):
    app, accounts, client = server
    bootstrap = client.get('/api/session')
    assert bootstrap.json()['authenticated'] is False
    anonymous = client.cookies.get(COOKIE)
    result = login(client)
    cookie = result.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie and 'Path=/' in cookie
    token = client.cookies.get(COOKIE)
    assert token != anonymous and app.state.sessions.get(anonymous) is None
    assert result.json()['user'] == {'username': accounts.admin.username, 'role': 'admin', 'branch': 'all'}
    assert len(result.json()['channels']) == 14
    result = client.delete('/api/session')
    assert result.json()['authenticated'] is False
    assert app.state.sessions.get(token) is None
    assert client.get('/api/ledger', params={'branch': BRANCH, 'date': DAY}).status_code == 401


@pytest.mark.parametrize('origin', [None, 'https://attacker.example', ORIGIN + '/', 'null'])
def test_login_requires_exact_origin_before_authentication(server, origin):
    _, accounts, client = server
    csrf = client.get('/api/session').json()['csrf_token']
    headers = {'X-CSRF-Token': csrf}
    if origin:
        headers['Origin'] = origin
    response = client.post('/api/session', json={'username': accounts.admin.username, 'password': 'synthetic-password'}, headers=headers)
    assert response.status_code == 403
    assert accounts.authentications == 0


def test_login_and_actions_require_session_csrf(server):
    _, accounts, client = server
    client.get('/api/session')
    response = client.post('/api/session', json={'username': accounts.admin.username, 'password': 'synthetic-password'}, headers={'Origin': ORIGIN})
    assert response.status_code == 403 and accounts.authentications == 0
    login(client)
    scope = workspace(client)['scope']
    response = client.post('/api/ledger/actions', json=action(scope), headers={'X-CSRF-Token': 'wrong'})
    assert response.status_code == 403
    assert workspace(client)['rows'] == []


def test_only_admin_and_fresh_permissions_on_every_request(server):
    _, accounts, client = server
    assert login(client, accounts.staff.username).status_code == 401
    assert login(client).status_code == 200
    accounts.validations = 0
    scope = workspace(client)['scope']
    assert accounts.validations == 1  # endpoint + store share this request's fresh validation
    assert send(client, action(scope))['status'] == 'saved'
    assert accounts.validations == 2
    workspace(client)
    assert accounts.validations == 3
    accounts.active = False
    denied = client.post('/api/ledger/actions', json=action(scope))
    assert denied.status_code == 401
    accounts.active = True
    assert client.get('/api/ledger', params={'branch': BRANCH, 'date': DAY}).status_code == 401


def test_login_throttle_shared_between_sessions(server):
    app, accounts, client = server
    for _ in range(5):
        assert login(client, password='wrong').status_code == 401
    with TestClient(app, base_url=ORIGIN) as other:
        assert login(other).status_code == 429
    assert accounts.authentications == 5


def test_body_limit_and_safe_validation_errors(server):
    _, accounts, client = server
    bootstrap = client.get('/api/session')
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': bootstrap.json()['csrf_token'], 'Content-Type': 'application/json'}
    response = client.post('/api/session', content=b'{' + b'x' * MAX_BODY, headers=headers)
    assert response.status_code == 413 and accounts.authentications == 0
    response = client.post('/api/session', content='{"password":"secret-must-not-echo",', headers=headers)
    assert response.status_code == 400
    assert 'secret-must-not-echo' not in response.text


def test_scope_date_and_branch_cannot_be_substituted(server):
    _, _, client = server
    login(client)
    scope = workspace(client)['scope']
    payload = action(scope)
    payload['date'] = '2026-10-17'
    assert client.post('/api/ledger/actions', json=payload).status_code == 403
    payload['date'] = DAY
    payload['branch'] = 'not-a-branch'
    assert client.post('/api/ledger/actions', json=payload).status_code == 400
    assert client.get('/api/ledger', params={'branch': BRANCH, 'date': '2026-1-1'}).status_code == 400
    assert workspace(client)['rows'] == []


def test_saved_actions_idempotent_and_old_revision_conflicts(server):
    _, _, client = server
    login(client)
    scope = workspace(client)['scope']
    first = action(scope)
    saved = send(client, first)
    assert saved['status'] == 'saved' and saved['receipt_revision'] == 1
    assert send(client, first) == saved
    update = action(scope, receipt_id=first['action']['id'], revision=1, amount='1200')
    assert send(client, update)['receipt_revision'] == 2
    stale = action(scope, receipt_id=first['action']['id'], revision=1, amount='1500')
    conflicted = send(client, stale)
    assert conflicted['status'] == 'conflict'
    assert conflicted['current']['payments']['카드'] == '1200'
    day = workspace(client)
    assert day['day']['revision'] == 2 and day['summary']['gross'] == '1200'
    events = client.get('/api/ledger/events', params={'branch': BRANCH, 'date': DAY}).json()['events']
    assert len(events) == 2


def test_parallel_edit_keeps_one_winner(server):
    app, _, client = server
    login(client)
    scope = workspace(client)['scope']
    first = action(scope)
    send(client, first)
    with TestClient(app, base_url=ORIGIN) as other:
        login(other)
        a = action(scope, receipt_id=first['action']['id'], revision=1, amount='2000')
        b = action(scope, receipt_id=first['action']['id'], revision=1, amount='3000')
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda pair: send(*pair), [(client, a), (other, b)]))
    assert sorted(item['status'] for item in results) == ['conflict', 'saved']
    assert workspace(client)['day']['revision'] == 2


def test_incomplete_input_draft_survives_reload_then_promotes_once(server):
    _, _, client = server
    login(client)
    scope = workspace(client)['scope']
    incomplete = action(scope, amount='12x')
    draft = send(client, incomplete)
    assert draft['status'] == 'draft'
    assert send(client, incomplete) == draft
    loaded = workspace(client)
    assert loaded['rows'][0]['raw']['payments']['카드'] == '12x'
    assert loaded['pending_count'] == 1 and loaded['summary']['gross'] == '0'
    complete = action(scope, receipt_id=incomplete['action']['id'], draft_revision=1, amount='1200')
    assert send(client, complete)['status'] == 'saved'
    assert workspace(client)['pending_count'] == 0


def test_big_money_roundtrips_without_javascript_precision_loss_and_backup_verifies(server):
    _, _, client = server
    login(client)
    scope = workspace(client)['scope']
    value = str(2 ** 53 + 1)
    assert send(client, action(scope, amount=value))['status'] == 'saved'
    loaded = workspace(client)
    assert loaded['rows'][0]['raw']['payments']['카드'] == value
    assert loaded['day']['receipts'][0]['payments']['카드'] == value
    assert loaded['summary']['net'] == value
    response = client.get('/api/ledger/backup')
    assert response.status_code == 200 and 'attachment' in response.headers['content-disposition']
    LedgerStore.verify_backup(response.json())


def test_close_snapshot_reopen_history_and_stale_day_revision(server):
    _, _, client = server
    login(client)
    scope = workspace(client)['scope']
    send(client, action(scope))
    command = {'branch': BRANCH, 'date': DAY, 'scope': scope, 'request_id': str(uuid4()), 'expected_revision': 0}
    assert client.post('/api/ledger/close', json=command).status_code == 409
    command.update(request_id=str(uuid4()), expected_revision=1)
    closed = client.post('/api/ledger/close', json=command)
    assert closed.status_code == 200 and closed.json()['result']['snapshot']['state'] == 'closed'
    assert client.post('/api/ledger/close', json=command).json() == closed.json()
    assert send(client, action(scope))['status'] == 'error'
    command.update(request_id=str(uuid4()), expected_revision=2, reason='합성 재개방 확인')
    assert client.post('/api/ledger/reopen', json=command).status_code == 200
    assert workspace(client)['day']['state'] == 'open'
    snapshots = client.get('/api/ledger/snapshots', params={'branch': BRANCH, 'date': DAY}).json()['snapshots']
    assert len(snapshots) == 1
    days = client.get('/api/ledger/days', params={'branch': BRANCH, 'start': DAY, 'end': '2026-10-31'}).json()['days']
    assert len(days) == 1 and days[0]['revision'] == 3


@pytest.mark.parametrize('amount', [1000, 2 ** 63 - 1])
def test_snapshot_download_preserves_original_money_and_checksum(server, amount):
    _, _, client = server
    login(client)
    scope = workspace(client)['scope']
    assert send(client, action(scope, amount=str(amount)))['status'] == 'saved'
    closed = client.post('/api/ledger/close', json={
        'branch': BRANCH, 'date': DAY, 'scope': scope,
        'request_id': str(uuid4()), 'expected_revision': 1,
    })
    assert closed.status_code == 200
    item = client.get('/api/ledger/snapshots', params={'branch': BRANCH, 'date': DAY}).json()['snapshots'][0]
    assert item['snapshot']['receipts'][0]['payments']['카드'] == str(amount)
    original = json.loads(item['download_json'])
    assert original['snapshot']['receipts'][0]['payments']['카드'] == amount
    assert type(original['snapshot']['receipts'][0]['payments']['카드']) is int
    payload = json.dumps(original['snapshot'], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    assert hashlib.sha256(payload).hexdigest() == original['sha256'] == item['sha256']


def test_headers_timing_and_unexpected_errors_do_not_leak(server, monkeypatch):
    app, _, client = server
    login(client)
    response = client.get('/api/ledger', params={'branch': BRANCH, 'date': DAY})
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert all(part in response.headers['server-timing'] for part in ('total;dur=', 'auth;dur=', 'ledger;dur='))
    def broken(*args):
        raise RuntimeError('postgresql://private-secret@internal.invalid/customer')
    monkeypatch.setattr(app.state.store, 'load_workspace', broken)
    response = client.get('/api/ledger', params={'branch': BRANCH, 'date': DAY})
    assert response.status_code == 503
    assert 'private-secret' not in response.text and 'Traceback' not in response.text


def test_sessions_expire_without_refresh_and_are_bounded():
    now = [0]
    sessions = Sessions(clock=lambda: now[0], max_sessions=1)
    old, anonymous = sessions.replace()
    fresh, anonymous = sessions.replace()
    assert sessions.get(old) is None
    token, authenticated = sessions.replace(fresh, Principal('test-admin', 'admin', 'all', str(uuid4())))
    from web_server import APIError
    with pytest.raises(APIError):
        sessions.replace()
    now[0] = 8 * 60 * 60 - 1
    assert sessions.get(token) == authenticated
    now[0] += 1
    assert sessions.get(token) is None
    assert sessions.replace()[1].principal is None


def test_anonymous_bootstrap_churn_cannot_fill_authenticated_capacity():
    sessions = Sessions(max_sessions=70)
    actor = Principal('test-admin', 'admin', 'all', str(uuid4()))
    authenticated_token, authenticated = sessions.replace(principal=actor)
    first_anonymous, _ = sessions.replace()
    for _ in range(600):
        latest, _ = sessions.replace()
    assert sessions.get(first_anonymous) is None
    assert sessions.get(authenticated_token) == authenticated
    assert len(sessions._items) == 65
    new_token, new_session = sessions.replace(latest, actor)
    assert sessions.get(new_token) == new_session and new_session.principal == actor


@pytest.mark.parametrize('origin', ['', 'http://example.test', ORIGIN + '/', ORIGIN + '/path', 'https://user:password@example.test', ORIGIN + '?x=1'])
def test_invalid_public_origin_fails_closed(origin):
    with pytest.raises(RuntimeError, match='PUBLIC_ORIGIN'):
        create_app(public_origin=origin)


def test_missing_production_config_never_opens_fallback(monkeypatch):
    for key in ('HISTORY_SHEET_ID', 'HISTORY_SERVICE_ACCOUNT_JSON', 'DATABASE_URL', 'LOCAL_LEDGER_PATH', 'ALLOW_LOCAL_LEDGER'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match='Configure'):
        create_app(public_origin=ORIGIN)
