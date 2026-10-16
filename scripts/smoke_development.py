"""실제 Streamlit 개발 진입점의 격리·원장 연결을 합성 자료로 검증한다."""
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from branch_access import AccessError, Principal
from receipt_ledger import LedgerStore
from ledger_ui import kst_today

ADMIN = Principal('synthetic-hq', 'admin', 'all', str(uuid4()))
STAFF = Principal('synthetic-staff', 'branch', '인천점', str(uuid4()))


class Accounts:
    def authenticate(self, username, password):
        if password != 'synthetic-password':
            raise AccessError('합성 로그인 실패')
        return ADMIN if username == ADMIN.username else STAFF

    def validate(self, principal):
        if principal not in (ADMIN, STAFF):
            raise AccessError('합성 권한 취소')
        return principal


accounts = Accounts()
settings = {'GOOGLE_API_KEY': 'synthetic-api-key'}
app._get_account_store = lambda: accounts
app._secret = lambda name, default=None: settings.get(name, default)


def operating_write_forbidden(*args, **kwargs):
    raise AssertionError('운영 기기/정산 저장소에 접근하면 안 됩니다.')


app._get_remember_store = operating_write_forbidden
app._get_history_store = operating_write_forbidden
app._sync_remember_browser = operating_write_forbidden


def login(at, principal):
    at.text_input(key='_auth_input_username').set_value(principal.username)
    at.text_input(key='_auth_input_password').set_value('synthetic-password')
    next(button for button in at.button if button.label == '로그인').click()
    at.run(timeout=20)
    assert not at.exception, at.exception


at = AppTest.from_string('import app\napp.main(development=True)')
at.run(timeout=20)
assert not at.exception, at.exception
assert not at.checkbox
login(at, STAFF)
assert any('총괄관리자' in error.value for error in at.error)
assert '_auth_principal' not in at.session_state
login(at, ADMIN)
assert at.radio(key='workspace_view').options == ['수납원장', '마감 대조', '정산 기록']
assert any('데이터베이스가 연결되지' in warning.value for warning in at.warning)
assert not any(widget.label == '로그인 유지 · 최대 180일' for widget in at.checkbox)

with tempfile.TemporaryDirectory(prefix='clinic-pay-dev-smoke-') as temporary:
    target = str(Path(temporary) / 'ledger.sqlite')
    settings.update(LOCAL_LEDGER_PATH=target, ALLOW_LOCAL_LEDGER=True)
    store = LedgerStore(target, accounts.validate, local=True)
    today = kst_today().isoformat()
    store.save_receipt(ADMIN, str(uuid4()), str(uuid4()), 0, '인천점', today,
                       {'chart_no': '100', 'patient_name': '합성환자', 'payments': {'카드': 50000}})
    at.run(timeout=20)
    assert not at.exception, at.exception
    next(button for button in at.button if button.label == '이 원장을 대조 분석에 사용').click()
    at.run(timeout=20)
    assert not at.exception, at.exception
    assert at.radio(key='workspace_view').value == '마감 대조'
    assert at.radio(key='daily_mode').value == '수납원장'
    assert at.session_state['_ledger_selection']['revision'] == 1
    assert next(button for button in at.button if button.label == '마감 대조 시작').disabled

    # AppTest does not provide file-upload control; return a synthetic BytesIO at that boundary.
    raw = pd.DataFrame([
        ['수납일', '차트번호', '이름', '결제수단', '비급여(과세총금액)', '결제메모'],
        [today, '100', '합성환자', '카드(삼성)', 45000, ''],
    ])
    upload = BytesIO(raw.to_csv(index=False, header=False).encode('utf-8-sig'))
    upload.name = 'synthetic-vegas.csv'
    def file_upload(label, **kwargs):
        return upload if kwargs.get('key') == 'p' else None
    with patch.object(app.st, 'file_uploader', file_upload):
        at.run(timeout=20)
        assert not at.exception, at.exception
        next(button for button in at.button if button.label == '마감 대조 시작').click()
        at.run(timeout=20)
        assert not at.exception, at.exception
    assert at.session_state['done'] is True
    assert at.session_state['_analysis_ledger']['revision'] == 1
    assert at.session_state['totals']['d_card'] == 50000
    assert at.session_state['totals']['p_card'] == 45000
    assert store.list_summaries(ADMIN, '인천점', today[:7]), '분석 요약이 별도 DB에 저장되지 않음'
    assert not at.get('download_button')
    next(button for button in at.button if button.key == 'verification_excel_prepare').click()
    at.run(timeout=20)
    assert not at.exception, at.exception
    assert at.get('download_button'), '요청 후 XLSX가 없음'

    with patch.object(app, 'run_gemini', return_value=(
            '{"findings":[{"evidence_id":"E0001","amount":5000,"explanation":"양쪽 원본을 검토하세요"}]}', 'synthetic-model')):
        next(button for button in at.button if button.label == '🚀 AI 분석 시작').click()
        at.run(timeout=20)
        assert not at.exception, at.exception
        assert '[E0001]' in at.session_state['_ai_result']
    with patch.object(app, 'run_gemini', return_value=(
            '{"findings":[{"evidence_id":"E9999","amount":5000,"explanation":"검토"}]}', 'synthetic-model')):
        next(button for button in at.button if button.label == '🚀 AI 분석 시작').click()
        at.run(timeout=20)
        assert not at.exception, at.exception
        assert '_ai_result' not in at.session_state
        assert any('근거 또는 금액' in error.value for error in at.error)
    assert store.load_day(ADMIN, '인천점', today)['revision'] == 1, 'AI가 원장 금액을 변경함'

    store.save_receipt(ADMIN, str(uuid4()), str(uuid4()), 0, '인천점', today,
                       {'chart_no': '200', 'patient_name': '합성추가', 'payments': {'현금': 1000}})
    at.run(timeout=20)
    assert any('변경' in warning.value and '이전 개정' in warning.value for warning in at.warning)
    next(button for button in at.button if button.key == 'logout').click()
    at.run(timeout=20)
    assert not at.exception, at.exception
    assert at.session_state['_development_mode'] is True
    assert 'patient' not in at.session_state
    assert '_auth_principal' not in at.session_state

print('Development AppTest: admin-only login, no operating writes, explicit database, ledger analysis, lazy XLSX, validated/rejected AI evidence, stale revision and logout passed')
