"""Full B navigation and native reconciliation reuse with synthetic files only."""
from datetime import date
from io import BytesIO
import json
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
from ledger_component import component_scope
from receipt_ledger import LedgerStore

ADMIN = Principal('synthetic-reconciliation-admin', 'admin', 'all', str(uuid4()))
BRANCH, DAY = '인천점', '2026-10-16'


class Accounts:
    def authenticate(self, username, password):
        if username != ADMIN.username or password != 'synthetic-password':
            raise AccessError('합성 로그인 실패')
        return ADMIN

    def validate(self, principal):
        if principal != ADMIN:
            raise AccessError('합성 권한 취소')
        return principal


def forbidden_external(*args, **kwargs):
    raise AssertionError('합성 smoke에서 운영 저장소, Google 시트 또는 외부 AI를 호출할 수 없습니다.')


def upload(name, header, *rows):
    result = BytesIO(pd.DataFrame([header, *rows]).to_csv(
        index=False, header=False).encode('utf-8-sig'))
    result.name = name
    return result


def healthy(at):
    assert not at.exception, at.exception
    assert at.query_params['ledger_view'] == 'full-web', '대조 화면이 B 경로를 이탈했습니다.'


def button(at, label):
    return next(item for item in at.button if item.label == label)


def workbench(workspace):
    at = AppTest.from_string('import development\ndevelopment.main()')
    at.query_params.update(ledger_view='full-web', workspace=workspace)
    at.session_state['_auth_principal'] = ADMIN
    return at


def run():
    settings = {'GOOGLE_API_KEY': 'synthetic-api-key'}
    accounts = Accounts()
    with tempfile.TemporaryDirectory(prefix='clinic-reconciliation-workspace-') as temporary:
        store = LedgerStore(str(Path(temporary) / 'ledger.sqlite'), accounts.validate, local=True)
        store.save_receipt(ADMIN, str(uuid4()), str(uuid4()), 0, BRANCH, DAY,
                           {'chart_no': '100', 'patient_name': '합성환자', 'payments': {'카드': 50000}})
        with patch.object(app, '_get_account_store', return_value=accounts), \
                patch.object(app, '_get_development_store', return_value=store), \
                patch.object(app, '_secret', side_effect=lambda name, default=None: settings.get(name, default)), \
                patch.object(app, '_get_history_store', side_effect=forbidden_external), \
                patch.object(app, '_get_remember_store', side_effect=forbidden_external), \
                patch.object(app, '_sync_remember_browser', side_effect=forbidden_external), \
                patch.object(app, 'load_gsheet_daily', side_effect=forbidden_external), \
                patch.object(app, 'run_gemini', side_effect=forbidden_external):
            # Login in the full B route, then exercise its actual component navigation request.
            at = AppTest.from_string('import development\ndevelopment.main()')
            at.query_params['ledger_view'] = 'full-web'
            at.run(timeout=20)
            healthy(at)
            at.text_input(key='_auth_input_username').set_value(ADMIN.username)
            at.text_input(key='_auth_input_password').set_value('synthetic-password')
            button(at, '로그인').click()
            at.run(timeout=20)
            healthy(at)
            assert at.get('component_instance'), 'B 수납원장 컴포넌트가 없습니다.'
            identity = component_scope(f'{ADMIN.username}:{ADMIN.revision}', '', '')
            at.session_state[f'full_web_{identity}'] = {
                'request_id': str(uuid4()), 'method': 'POST', 'path': '/api/navigation',
                'body': {'target': 'analysis', 'branch': BRANCH, 'date': DAY},
            }
            at.run(timeout=20)
            healthy(at)
            assert at.query_params['workspace'] in ('workbench', 'analysis')
            assert any(item.label == '마감 대조 시작' for item in at.button), 'B 안에서 대조 입력이 열리지 않았습니다.'
            assert at.session_state['_ledger_selection'] == {
                'branch': BRANCH, 'business_date': DAY, 'revision': 1,
            }, 'B에서 선택한 서버 원장 지점·영업일·개정이 전달되지 않았습니다.'

            # A direct URL must render the same workflow without the old-app link or another login.
            day = workbench('workbench')
            day.session_state['daily_mode'] = '파일 업로드'
            day.session_state['upload_branch'] = BRANCH
            h = upload('synthetic-terminal.csv',
                       ['금액', '승인번호', '거래일', '거래상태', '구분', '매입사', '거래시간'],
                       [50000, '100111', '261016', '정상승인', '카드', '삼성카드', '100000'],
                       [30000, '200222', '261016', '정상승인', '카드', '현대카드', '110000'])
            d = upload('synthetic-daily.csv',
                       ['내원순서', '차트번호', '성명', '카드', '현금', '이체'],
                       [1, '100', '합성첫째', 50000, 0, 0],
                       [2, '200', '합성둘째', 30000, 0, 0])
            p = upload('synthetic-vegas.csv',
                       ['수납일', '차트번호', '이름', '결제수단', '비급여(과세총금액)', '결제메모'],
                       [DAY, '100', '합성첫째', '카드(삼성)', 50000, '100111'],
                       [DAY, '200', '합성둘째', '카드(현대)', 35000, '200222'])

            def day_files(label, **kwargs):
                value = {'h': h, 'd': d, 'p': p}.get(kwargs.get('key'))
                if value is not None:
                    value.seek(0)
                return value

            with patch.object(app.st, 'file_uploader', side_effect=day_files):
                day.run(timeout=20)
                healthy(day)
                assert not button(day, '마감 대조 시작').disabled
                button(day, '마감 대조 시작').click()
                day.run(timeout=20)
            healthy(day)
            assert day.session_state['done'] is True
            assert day.session_state['totals']['h_card'] == 80000
            assert day.session_state['totals']['d_card'] == 80000
            assert day.session_state['totals']['p_card'] == 85000
            assert day.session_state['reconciliation_branch'] == BRANCH
            assert day.session_state['reconciliation_date'] == DAY
            assert {'차이 근거', '거래 대조', 'AI 설명', '환자별 검증'}.issubset(
                {tab.label for tab in day.tabs})
            summaries = store.list_summaries(ADMIN, BRANCH, DAY[:7])
            assert len(summaries) == 1 and summaries[0]['gap'] == -5000
            button(day, '📥 검증 결과 엑셀 다운로드 (요약 + 유형B / C1 / C2 / C3) 준비').click()
            day.run(timeout=20)
            healthy(day)
            assert day.get('download_button'), '검증 XLSX를 준비했지만 다운로드가 없습니다.'

            def synthetic_ai(api_key, data_text, question='', **kwargs):
                assert api_key == 'synthetic-api-key'
                assert '합성첫째' not in data_text and '합성둘째' not in data_text
                return json.dumps({'findings': [{'evidence_id': 'E0001', 'amount': -5000,
                                                  'explanation': '양쪽 원본을 확인하세요.'}]}), 'synthetic-model'

            with patch.object(app, 'run_gemini', side_effect=synthetic_ai):
                button(day, '🚀 AI 분석 시작').click()
                day.run(timeout=20)
            healthy(day)
            assert '[E0001]' in day.session_state['_ai_result']
            assert store.load_day(ADMIN, BRANCH, DAY)['revision'] == 1, 'AI가 원장에 쓰기를 수행했습니다.'

            day.button(key='fullweb_go_period').click()
            day.run(timeout=20)
            healthy(day)
            assert day.query_params['workspace'] == 'period'
            assert 'done' not in day.session_state, '기간 화면에 이전 하루 결과가 남았습니다.'
            assert any(item.label == '기간 대조 시작' for item in day.button)

            # Saved reconciliation summaries are separate from ledger history and remain reachable in B.
            history = workbench('reconciliation-history')
            history.session_state['history_branch'] = BRANCH
            history.session_state['history_month'] = date.fromisoformat(DAY)
            history.run(timeout=20)
            healthy(history)
            assert any(metric.label == '정산 기록' and metric.value == '1일' for metric in history.metric)
            assert any('합계 차이' in frame.value.columns for frame in history.dataframe)

            period = workbench('period')
            period.session_state['_full_web_context'] = {
                'branch': BRANCH, 'business_date': DAY, 'revision': 1,
            }
            period.session_state['per_branch'] = app._PER_NO_GSHEET
            per_h = upload('synthetic-period-terminal.csv',
                           ['금액', '승인번호', '거래일', '거래상태', '구분', '매입사', '거래시간'],
                           [50000, '300333', '261015', '정상승인', '카드', '삼성카드', '100000'],
                           [40000, '400444', '261016', '정상승인', '카드', '삼성카드', '100000'])
            per_p = upload('synthetic-period-vegas.csv',
                           ['수납일', '차트번호', '이름', '결제수단', '비급여(과세총금액)', '결제메모'],
                           ['2026-10-15', '300', '합성셋째', '카드(삼성)', 50000, '300333'],
                           [DAY, '400', '합성넷째', '카드(삼성)', 49000, '400444'])

            def period_files(label, **kwargs):
                value = {'per_h_file': per_h, 'per_p_file': per_p}.get(kwargs.get('key'))
                if value is not None:
                    value.seek(0)
                return value

            with patch.object(app.st, 'file_uploader', side_effect=period_files):
                period.run(timeout=20)
                healthy(period)
                assert not button(period, '기간 대조 시작').disabled
                button(period, '기간 대조 시작').click()
                period.run(timeout=20)
            healthy(period)
            assert period.session_state['period_done'] is True
            table = period.session_state['per_table']
            assert table['날짜'].tolist() == ['2026-10-15', DAY]
            assert table['카드차이'].tolist() == [0, -9000]
            assert period.selectbox(key='per_day').value == DAY
            assert period.get('download_button'), '기간 CSV 다운로드가 없습니다.'

            # Returning through the B component must reset the previous analysis mode too.
            period.button(key='fullweb_go_ledger').click()
            period.run(timeout=20)
            healthy(period)
            assert period.get('component_instance')
            period.session_state[f'full_web_{identity}'] = {
                'request_id': str(uuid4()), 'method': 'POST', 'path': '/api/navigation',
                'body': {'target': 'workbench', 'branch': BRANCH, 'date': DAY},
            }
            period.run(timeout=20)
            healthy(period)
            assert period.query_params['workspace'] == 'workbench'
            assert 'period_done' not in period.session_state, 'B 원장을 거쳐 온 하루 화면에 이전 기간 결과가 남았습니다.'
            assert any(item.label == '마감 대조 시작' for item in period.button)
            period.button(key='fullweb_go_ledger').click()
            period.run(timeout=20)
            healthy(period)
            assert period.query_params['workspace'] == 'ledger'
            assert period.get('component_instance'), '대조 화면에서 B 수납원장으로 돌아오지 못했습니다.'

    print('Full B reconciliation AppTest passed: same-route navigation and direct URLs, real three-file '
          'day comparison, AI evidence, XLSX, isolated summary save/history, two-day period comparison and CSV; '
          'no operating writes or external Google/AI calls.')


if __name__ == '__main__':
    run()
