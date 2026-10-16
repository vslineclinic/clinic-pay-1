"""The embedded full-page UI has the same authorization and ledger write contracts."""
from copy import deepcopy
import json
import shutil
import subprocess
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from branch_access import AccessError, Principal
from receipt_ledger import LedgerStore, MAX_MONEY, _digest
from web_bridge import dispatch_request


ADMIN = Principal('web-bridge-admin', 'admin', 'all', str(uuid4()))
STAFF = Principal('web-bridge-staff', 'branch', '강남점', str(uuid4()))
BRANCH, DAY = '강남점', '2026-10-16'


@pytest.fixture
def store(tmp_path):
    return LedgerStore(str(tmp_path / 'web.sqlite'), lambda principal: principal, local=True)


def call(store, path='/api/ledger', method='GET', body=None, principal=ADMIN, validate=lambda p: p):
    if method == 'GET' and path not in ('/api/session', '/api/ledger/backup') and '?' not in path:
        path += '?' + urlencode({'branch': BRANCH, 'date': DAY})
    return dispatch_request(store, principal, validate, {
        'request_id': str(uuid4()), 'method': method, 'path': path, 'body': body or {},
    })


def save_command(scope, *, receipt_id=None, amount='1000', revision=0):
    return {'branch': BRANCH, 'date': DAY, 'scope': scope, 'action': {
        'id': receipt_id or str(uuid4()), 'request_id': str(uuid4()), 'scope': scope,
        'attempt': 0, 'receipt_revision': revision, 'draft_revision': 0, 'action': 'save',
        'raw': {'chart_no': '000123', 'patient_name': '합성수납', 'payments': {'카드': amount}},
    }}


def test_full_web_preserves_money_idempotency_and_revision_conflict(store):
    workspace = call(store)['data']
    assert workspace['summary']['net'] == '0'
    command = save_command(workspace['scope'], amount='9007199254740993')
    saved = call(store, '/api/ledger/actions', 'POST', command)
    assert saved['data']['ack']['status'] == 'saved'
    assert call(store, '/api/ledger/actions', 'POST', command)['data'] == saved['data']
    workspace = call(store)['data']
    assert workspace['day']['receipts'][0]['payments']['카드'] == '9007199254740993'
    assert workspace['summary']['gross'] == '9007199254740993'
    stale = save_command(workspace['scope'], receipt_id=command['action']['id'], amount='1')
    ack = call(store, '/api/ledger/actions', 'POST', stale)['data']['ack']
    assert ack['status'] == 'conflict'
    assert ack['current']['payments']['카드'] == '9007199254740993'
    assert len(store.list_events(ADMIN, BRANCH, DAY)) == 1


def test_full_web_requires_current_admin_and_server_scope(store):
    assert call(store, principal=STAFF)['status'] == 403
    def revoked(_):
        raise AccessError('합성 권한 취소')
    assert call(store, validate=revoked)['status'] == 403
    command = save_command(call(store)['data']['scope'])
    command['scope'] = 'client-owned-scope'
    assert call(store, '/api/ledger/actions', 'POST', command)['status'] == 400
    assert not store.load_day(ADMIN, BRANCH, DAY)['receipts']
    command['scope'] = call(store)['data']['scope']
    command['action']['scope'] = 'wrong'
    assert call(store, '/api/ledger/actions', 'POST', command)['data']['ack']['status'] == 'error'
    assert not store.load_day(ADMIN, BRANCH, DAY)['receipts']


def test_full_web_drafts_block_close_and_backup_remains_verifiable(store):
    scope = call(store)['data']['scope']
    command = save_command(scope, amount='1,2')
    assert call(store, '/api/ledger/actions', 'POST', command)['data']['ack']['status'] == 'draft'
    workspace = call(store)['data']
    assert workspace['pending_count'] == 1
    close = {'branch': BRANCH, 'date': DAY, 'scope': scope, 'request_id': str(uuid4()),
             'expected_revision': workspace['day']['revision']}
    assert call(store, '/api/ledger/close', 'POST', close)['status'] == 400
    backup = call(store, '/api/ledger/backup')['data']
    assert LedgerStore.verify_backup(backup)['valid'] is True


def test_full_web_max_won_backup_survives_browser_json_roundtrip(store):
    if not shutil.which('node'):
        pytest.skip('Node is required to check the JavaScript download boundary')
    scope = call(store)['data']['scope']
    command = save_command(scope, amount=str(MAX_MONEY))
    assert call(store, '/api/ledger/actions', 'POST', command)['data']['ack']['status'] == 'saved'
    backup = call(store, '/api/ledger/backup')['data']
    javascript = "let raw='';process.stdin.on('data',chunk=>raw+=chunk);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(raw))));"
    downloaded = subprocess.run(['node', '-e', javascript], input=json.dumps(backup),
                                text=True, capture_output=True, check=True, timeout=10)
    envelope = json.loads(downloaded.stdout)
    assert LedgerStore.verify_backup(envelope)['valid'] is True
    receipt = json.loads(envelope['payload']['tables']['ledger_receipts'][0]['payload_json'])
    assert receipt['payments']['카드'] == MAX_MONEY


def test_full_web_snapshot_download_keeps_hash_and_max_won(store):
    scope = call(store)['data']['scope']
    command = save_command(scope, amount=str(MAX_MONEY))
    assert call(store, '/api/ledger/actions', 'POST', command)['data']['ack']['status'] == 'saved'
    close = {'branch': BRANCH, 'date': DAY, 'scope': scope, 'request_id': str(uuid4()), 'expected_revision': 1}
    assert call(store, '/api/ledger/close', 'POST', close)['status'] == 200
    wire = call(store, '/api/ledger/snapshots')['data']['snapshots'][0]
    assert wire['snapshot']['receipts'][0]['payments']['카드'] == str(MAX_MONEY)
    # The UI display uses strings; its download must preserve the original signed document.
    downloaded = json.loads(wire['download_json'])
    assert downloaded['snapshot']['receipts'][0]['payments']['카드'] == MAX_MONEY
    assert _digest(downloaded['snapshot']) == downloaded['sha256']


def test_full_web_close_reopen_history_and_sanitized_errors(store):
    scope = call(store)['data']['scope']
    call(store, '/api/ledger/actions', 'POST', save_command(scope))
    close = {'branch': BRANCH, 'date': DAY, 'scope': scope, 'request_id': str(uuid4()), 'expected_revision': 1}
    assert call(store, '/api/ledger/close', 'POST', close)['status'] == 200
    assert len(call(store, '/api/ledger/snapshots')['data']['snapshots']) == 1
    stale = deepcopy(close)
    stale.update(request_id=str(uuid4()), reason='합성 재개방')
    assert call(store, '/api/ledger/reopen', 'POST', stale)['status'] == 409
    stale['expected_revision'] = 2
    assert call(store, '/api/ledger/reopen', 'POST', stale)['status'] == 200
    assert len(call(store, '/api/ledger/events')['data']['events']) == 3
    def broken(_):
        raise RuntimeError('postgresql://do-not-expose-this')
    error = call(store, validate=broken)
    assert error['status'] == 503
    assert 'postgresql' not in str(error)


@pytest.mark.parametrize('path', ['https://evil.invalid/api/ledger', '/api/ledger?branch=x&branch=y',
                                 '/api/ledger?branch=x&date=invalid', '/api/ledger#ignored'])
def test_full_web_rejects_ambiguous_request_paths(store, path):
    assert call(store, path)['status'] == 400


def test_full_web_session_and_navigation_do_not_accept_client_credentials(store):
    assert call(store, '/api/session')['data']['user']['username'] == ADMIN.username
    assert call(store, '/api/session', 'POST', {'username': ADMIN.username, 'password': 'ignored'})['status'] == 400
    assert call(store, '/api/navigation', 'POST', {'target': 'https://evil.invalid'})['status'] == 400
    assert call(store, '/api/navigation', 'POST', {'target': 'table'})['control'] == 'table'
    assert call(store, '/api/session', 'DELETE')['control'] == 'logout'
