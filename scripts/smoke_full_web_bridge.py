"""Real Streamlit rendering and component dispatch with synthetic local storage."""
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
from urllib.parse import urlencode

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from branch_access import AccessError, Principal
from receipt_ledger import LedgerStore

ADMIN = Principal('synthetic-web-admin', 'admin', 'all', str(uuid4()))


class Accounts:
    def authenticate(self, username, password):
        if username == ADMIN.username and password == 'synthetic-password':
            return ADMIN
        raise AccessError('합성 로그인 실패')

    def validate(self, principal):
        if principal != ADMIN:
            raise AccessError('합성 권한 취소')
        return principal


app._get_account_store = lambda: Accounts()
with tempfile.TemporaryDirectory(prefix='clinic-full-web-') as temporary:
    store = LedgerStore(str(Path(temporary) / 'ledger.sqlite'), Accounts().validate, local=True)
    app._get_development_store = lambda: store
    at = AppTest.from_string('import development\ndevelopment.main()')
    at.query_params['ledger_view'] = 'full-web'
    at.run(timeout=20)
    assert not at.exception, at.exception
    at.text_input(key='_auth_input_username').set_value(ADMIN.username)
    at.text_input(key='_auth_input_password').set_value('synthetic-password')
    next(button for button in at.button if button.label == '로그인').click()
    at.run(timeout=20)
    assert not at.exception, at.exception
    component = at.get('component_instance')[0]
    assert component.proto.component_name.endswith('clinic_full_web_ledger')
    assert not at.radio, 'The full B route must not show the old Streamlit navigation.'
    from ledger_component import component_scope
    key = 'full_web_' + component_scope(f'{ADMIN.username}:{ADMIN.revision}', '', '')
    request_id = str(uuid4())
    at.session_state[key] = {'request_id': request_id, 'path': '/api/ledger?' + urlencode({
        'branch': '강남점', 'date': '2026-10-16'}), 'method': 'GET', 'body': {}}
    at.run(timeout=20)
    assert not at.exception, at.exception
    reply_key = '_full_web_reply_' + component_scope(f'{ADMIN.username}:{ADMIN.revision}', '', '')
    assert at.session_state[reply_key]['request_id'] == request_id
    assert at.session_state[reply_key]['data']['summary']['net'] == '0'
    at.session_state[key] = {'request_id': str(uuid4()), 'path': '/api/session', 'method': 'DELETE', 'body': {}}
    at.run(timeout=20)
    assert not at.exception, at.exception
    assert '_auth_principal' not in at.session_state
    assert not at.get('component_instance'), 'Logout must remove the private component.'

print('Full B AppTest: login, isolated full-page component, request dispatch and logout passed')
