"""합성 자료로 날짜 완전성·실행 캐시·AI 경계를 재현한다."""
import json
from unittest.mock import Mock

import pandas as pd
import pytest
import factories as F


@pytest.mark.parametrize('label', ['단말기 마감', '베가스 마감'])
@pytest.mark.parametrize('bad', ['', None, '2026-02-30', 'bad'])
def test_period_refuses_even_one_undated_transaction(app, label, bad):
    h_raw, p_raw = F.period_raw_files(
        [(100, '111111', '정상승인', '카드'), (200, '222222', '정상승인', '카드')],
        [('2026-05-11', '100', '합성가', '카드', 100, '111111'),
         ('2026-05-11', '200', '합성나', '카드', 200, '222222')])
    frames = {'단말기 마감': app.parse_hansol(h_raw), '베가스 마감': app.parse_patient(p_raw)}
    frames[label].loc[1, '날짜'] = bad
    with pytest.raises(ValueError, match=label + '.*1개 행'):
        app.compute_period_recon(frames['단말기 마감'], frames['베가스 마감'])


def test_run_artifacts_reused_until_new_run_and_reset(app):
    app.st.session_state['reconciliation_run_id'] = 'run-one'
    build = Mock(side_effect=[{'private': 1}, {'private': 2}])
    first = app._run_artifact('verification', build)
    assert app._run_artifact('verification', build) is first
    assert build.call_count == 1
    app.st.session_state['_ai_result'] = 'old private explanation'
    app.st.session_state['reconciliation_run_id'] = 'run-two'
    assert app._run_artifact('verification', build) == {'private': 2}
    assert '_ai_result' not in app.st.session_state
    app._reset_workspace()
    assert '_analysis_artifacts' not in app.st.session_state


def test_already_saved_result_display_does_not_read_account_or_store(app, monkeypatch):
    from branch_access import Principal
    who = Principal('synthetic-hq', 'admin', 'all', '11111111-1111-4111-8111-111111111111')
    ss = {'reconciliation_branch': '인천점', 'history_attempted_run': 'run', 'history_saved_run': 'run'}
    fresh = Mock(side_effect=AssertionError('display must reuse this run principal'))
    monkeypatch.setattr(app, '_validated_principal', fresh)
    monkeypatch.setattr(app, '_get_scoped_history_store', fresh)
    monkeypatch.setattr(app, '_history_snapshot_from_session', lambda *_: {
        'run_id': 'run', 'branch': '인천점', 'settlement_date': '2026-10-12', 'status': '검토 필요'})
    app._render_history_save(ss, {}, principal=who)
    fresh.assert_not_called()


def test_remember_restore_already_reads_latest_account(app, monkeypatch):
    from branch_access import Principal
    who = Principal('synthetic-hq', 'admin', 'all', '11111111-1111-4111-8111-111111111111')
    accounts, devices = Mock(), Mock()
    devices.restore.return_value = who
    monkeypatch.setattr(app, '_get_account_store', lambda: accounts)
    monkeypatch.setattr(app, '_get_remember_store', lambda: devices)
    app.st.session_state.update(_auth_principal=who, _remember_token='synthetic')
    assert app._validated_principal() == who
    accounts.validate.assert_not_called()
    devices.restore.assert_called_once_with('synthetic')


def test_evidence_response_rejects_unknown_id_changed_amount_and_tokens(app):
    evidence = {'E0001': {'amount': -5000, 'label': '카드 일-차'}}
    good = {'findings': [{'evidence_id': 'E0001', 'amount': -5000, 'explanation': '검토 권고'}]}
    assert 'E0001' in app.validate_ai_evidence(json.dumps(good), evidence, {})
    for change in ({'evidence_id': 'E9999'}, {'amount': 5000}, {'amount': True},
                   {'explanation': 'P999의 원본 검토'}):
        bad = {'findings': [{**good['findings'][0], **change}]}
        with pytest.raises(ValueError):
            app.validate_ai_evidence(json.dumps(bad), evidence, {})


def test_ai_input_lists_caps_and_actual_text_truncation(app):
    from test_outputs import _full_pipeline
    ctx = _full_pipeline(app, F.mismatch_three_files)
    verif = app.build_verification(ctx['P'], ctx['D'], ctx['DR'], hansol=ctx['H'])
    verif['유형C2_금액불일치'] = pd.concat([verif['유형C2_금액불일치']] * 55, ignore_index=True)
    args = [ctx[k] for k in ('H', 'D', 'DR', 'P', 'channel', 'p1_full', 'h_um', 'd_um', 'suspects')]
    text = app.build_ai_text(*args, totals=ctx['totals'], verif=verif)
    assert '유형C2: 전체55/선택50/생략5' in text
    assert '[AI입력범위]' in text and '전수 검토' in text
    assert '코드추출·오차0' not in text
    short = app.build_ai_text(*args, totals=ctx['totals'], verif=verif, max_chars=200)
    assert '문자 잘림 있음' in short
    assert len(short) <= 208


def test_ai_evidence_amounts_come_from_server_not_the_model(app):
    channel = pd.DataFrame([{'채널': '카드', '일마-차트': -5000}])
    verification = {'유형C2_금액불일치': pd.DataFrame([{'차트번호': '200', '차이': -5000}])}
    evidence = app.build_ai_evidence(verification, channel)
    assert len(evidence) == 2
    assert all(row['amount'] == -5000 for row in evidence.values())
    assert any('차트#200' in row['label'] for row in evidence.values())


def test_lazy_excel_only_builds_on_request_then_reuses_bytes(app, monkeypatch):
    app.st.session_state['reconciliation_run_id'] = 'run'
    pressed = [False, True, False]
    monkeypatch.setattr(app.st, 'button', lambda *args, **kw: pressed.pop(0))
    download = Mock()
    monkeypatch.setattr(app.st, 'download_button', download)
    build = Mock(return_value=b'private workbook')
    app._lazy_excel('검증', 'xlsx', build, 'result.xlsx')
    build.assert_not_called()
    download.assert_not_called()
    app._lazy_excel('검증', 'xlsx', build, 'result.xlsx')
    app._lazy_excel('검증', 'xlsx', build, 'result.xlsx')
    build.assert_called_once()
    assert download.call_count == 2


def test_question_only_anonymization_reuses_existing_tokens_without_collision(app):
    empty = pd.DataFrame()
    raw, _, mapping = app.prepare_ai_payload('차트#999 승인123456789', '', empty, empty, empty)
    _, question, combined = app.prepare_ai_payload('', '차트888 승인987654321', empty, empty, empty,
                                                  base_token_map=mapping)
    assert 'P001' in raw and 'P002' in question
    assert 'A001' in raw and 'A002' in question
    assert combined['P001'] == '차트999' and combined['P002'] == '차트888'
