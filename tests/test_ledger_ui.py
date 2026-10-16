"""Raw drafts must not become zero money or contaminate the legacy daily adapter."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest


def test_kst_business_day_changes_before_utc_midnight():
    from ledger_ui import kst_today
    assert kst_today(datetime(2026, 10, 15, 15, 1, tzinfo=timezone.utc)).isoformat() == "2026-10-16"


@pytest.mark.parametrize("raw", ["1.5", "abc", "-1", "1,00", "1e4", True, 1.2, None])
def test_invalid_money_never_coerces_to_zero(raw):
    from ledger_ui import parse_won, DraftIncomplete
    with pytest.raises(DraftIncomplete):
        parse_won(raw)


def test_integer_won_and_blank_optional_channel():
    from ledger_ui import parse_won
    assert parse_won("1,234") == 1234
    assert parse_won(0) == 0
    assert parse_won("") == 0


def test_usage_checkboxes_do_not_create_platform_money():
    from ledger_ui import normalize_raw
    value = normalize_raw({"chart_no": "00123", "patient_name": "김테스트", "payments": {"카드": "3000"},
                           "yeoshin_used": True, "gangnamunni_used": True})
    assert value["payments"]["여신티켓"] == value["payments"]["강남언니"] == 0
    assert value["payments"]["카드"] == 3000
    assert value["yeoshin_used"] and value["gangnamunni_used"]


def test_missing_patient_or_empty_payment_stays_draft():
    from ledger_ui import normalize_raw, DraftIncomplete
    with pytest.raises(DraftIncomplete):
        normalize_raw({"chart_no": "123", "patient_name": "", "payments": {"카드": "100"}})
    with pytest.raises(DraftIncomplete):
        normalize_raw({"chart_no": "123", "patient_name": "김테스트", "payments": {}})


def test_adapter_splits_refund_excludes_void_and_preserves_identity():
    from ledger_ui import ledger_to_daily
    def receipt(kind="receipt", **extra):
        return {"id": str(uuid4()), "chart_no": "00123", "patient_name": "김테스트", "kind": kind,
                "payments": {"카드": 3000, "현금": 50, "여신티켓": 100}, "voided": False, **extra}
    sale, refund = receipt(), receipt("refund")
    daily, refunds = ledger_to_daily({"business_date": "2026-10-16", "revision": 7,
                                     "receipts": [sale, refund, receipt(voided=True)]})
    assert len(daily) == len(refunds) == 1
    assert daily.iloc[0]["차트번호"] == "00123"
    assert daily.iloc[0]["날짜"] == "2026-10-16"
    assert daily.iloc[0]["총액"] == refunds.iloc[0]["총액"] == 3150
    assert daily.iloc[0]["플랫폼합"] == 100
    assert daily.attrs["ledger_revision"] == 7
    assert daily.iloc[0]["ledger_receipt_id"] == sale["id"]


def test_adapter_rejects_corrupt_money_instead_of_normal_zero():
    from ledger_ui import ledger_to_daily
    with pytest.raises(ValueError):
        ledger_to_daily({"business_date": "2026-10-16", "revision": 1, "receipts": [
            {"id": "bad", "chart_no": "123", "patient_name": "김", "payments": {"카드": "oops"}}]})


def test_component_scope_includes_actor_branch_and_date():
    from ledger_component import component_scope
    assert len({component_scope("a", "인천점", "2026-10-16"),
                component_scope("a", "잠실점", "2026-10-16"),
                component_scope("a", "인천점", "2026-10-15"),
                component_scope("b", "인천점", "2026-10-16")}) == 4


@pytest.fixture
def ledger(tmp_path):
    from branch_access import Principal
    from receipt_ledger import LedgerStore
    principal = Principal('admin', 'admin', 'all', str(uuid4()))
    store = LedgerStore(str(tmp_path / 'ledger.sqlite'), lambda p: p, local=True)
    return principal, store


def command(scope, receipt_id=None, **changes):
    return {"scope": scope, "request_id": str(uuid4()), "id": receipt_id or str(uuid4()),
            "receipt_revision": 0, "draft_revision": 0, "action": "save",
            "raw": {"chart_no": "123", "patient_name": "김테스트", "payments": {"카드": "100"}}, **changes}


def test_autosave_persists_invalid_raw_then_promotes_same_id(ledger):
    from ledger_ui import dispatch_action
    principal, store = ledger
    action = command('scope', raw={"chart_no": "123", "patient_name": "김테스트", "payments": {"카드": "1,00"}})
    ack = dispatch_action(store, principal, '인천점', '2026-10-16', action, 'scope')
    assert ack['status'] == 'draft'
    assert store.list_drafts(principal, '인천점', '2026-10-16')[0]['values']['payments']['카드'] == '1,00'
    assert store.load_day(principal, '인천점', '2026-10-16')['receipts'] == []
    action = command('scope', action['id'], draft_revision=ack['draft_revision'])
    saved = dispatch_action(store, principal, '인천점', '2026-10-16', action, 'scope')
    assert saved['status'] == 'saved'
    assert store.list_drafts(principal, '인천점', '2026-10-16') == []
    assert len(store.load_day(principal, '인천점', '2026-10-16')['receipts']) == 1
    retry = dispatch_action(store, principal, '인천점', '2026-10-16', action, 'scope')
    assert retry == saved


def test_stale_edit_returns_current_and_keeps_local_raw(ledger):
    from ledger_ui import dispatch_action
    principal, store = ledger
    initial = command('scope')
    assert dispatch_action(store, principal, '인천점', '2026-10-16', initial, 'scope')['status'] == 'saved'
    stale = command('scope', initial['id'])
    stale['raw']['patient_name'] = '내 입력'
    ack = dispatch_action(store, principal, '인천점', '2026-10-16', stale, 'scope')
    assert ack['status'] == 'conflict'
    assert ack['current']['patient_name'] == '김테스트'
    assert stale['raw']['patient_name'] == '내 입력'


def test_event_from_another_day_never_writes_current_scope(ledger):
    from ledger_ui import dispatch_action
    principal, store = ledger
    ack = dispatch_action(store, principal, '인천점', '2026-10-16', command('old-day'), 'new-day')
    assert ack['status'] == 'error'
    assert store.load_day(principal, '인천점', '2026-10-16')['revision'] == 0


def test_draft_rows_keep_original_receipt_revision_until_conflict_resolution():
    from ledger_ui import editor_rows
    rows = editor_rows({'receipts': [{'id': 'a', 'revision': 4, 'chart_no': '123',
                                     'patient_name': '서버', 'payments': {'카드': 100}}]},
                       [{'id': 'a', 'revision': 2, 'values': {'_receipt_revision': 1,
                        'chart_no': '123', 'patient_name': '내 초안', 'payments': {'카드': 'oops'}}}])
    assert rows[0]['receipt_revision'] == 1
    assert rows[0]['draft_revision'] == 2
    assert rows[0]['raw']['patient_name'] == '내 초안'


def test_editor_transports_large_integer_money_as_text():
    from ledger_ui import editor_rows
    amount = 2**53 + 1
    row = editor_rows({'receipts': [{'id': 'a', 'revision': 1, 'payments': {'카드': amount}}]}, [])[0]
    assert row['raw']['payments']['카드'] == str(amount)


def test_real_streamlit_ledger_render_history_close_reopen_and_backup(tmp_path):
    # The project intentionally stubs Streamlit globally; AppTest must use a clean process.
    import subprocess
    import sys
    import textwrap
    source = textwrap.dedent(f'''
        import streamlit as st
        from branch_access import Principal
        from receipt_ledger import LedgerStore
        from ledger_ui import render_ledger
        p = Principal('synthetic', 'admin', 'all', '11111111-1111-4111-8111-111111111111')
        s = LedgerStore({str(tmp_path / 'app-test.sqlite')!r}, lambda p:p, local=True)
        render_ledger(p, s, lambda p:p)
    ''')
    blocked_source = source.replace('render_ledger(p, s, lambda p:p)', "\n".join([
        "other = Principal('other-admin', 'admin', 'all', '22222222-2222-4222-8222-222222222222')",
        "s.save_draft(other, '33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444', 0, '인천점', '2026-10-16', {'patient_name':'합성 초안'})",
        "render_ledger(p,s,lambda p:p)",
    ]))
    checkpoint_source = source.replace('render_ledger(p, s, lambda p:p)', "if not st.session_state.get('hide_ledger'):\n    render_ledger(p,s,lambda p:p)")
    driver = textwrap.dedent(f'''
        from datetime import date
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_string({source!r}).run()
        def ok():
            at.run()
            assert not at.exception, [e.message for e in at.exception]
        assert not at.exception
        assert len(at.selectbox(key='ledger_branch').options) == 7
        at.date_input(key='ledger_business_date').set_value(date(2026,10,16)); ok()
        next(c for c in at.checkbox if c.label == '수납원장 내용을 수동 검토했습니다').check(); ok()
        next(b for b in at.button if b.label == '현재 개정으로 마감').click(); ok()
        assert any('마감' in c.value for c in at.caption)
        assert any(t.label == '재개방 사유' for t in at.text_input)
        next(t for t in at.text_input if t.label == '재개방 사유').set_value('합성 재개방'); ok()
        next(b for b in at.button if b.label == '사유를 남기고 재개방').click(); ok()
        assert any('입력 가능' in c.value for c in at.caption)
        next(b for b in at.button if b.label == '이력 조회').click(); ok()
        assert len(at.dataframe) >= 2
        next(b for b in at.button if b.label == '검증된 백업 생성').click(); ok()
        assert any(b.proto.label == '전체 백업 JSON 다운로드' for b in at.get('download_button'))
        next(b for b in at.button if b.label == '이 원장을 대조 분석에 사용').click(); ok()
        assert at.session_state['_ledger_selection']['business_date'] == '2026-10-16'
        assert at.session_state['_ledger_selection']['revision'] == 2
        at.selectbox(key='ledger_branch').set_value('잠실점'); ok()
        assert any('잠실점' in c.value and '개정 0' in c.value for c in at.caption)
        blocked = AppTest.from_string({blocked_source!r}).run()
        assert not blocked.exception, [e.message for e in blocked.exception]
        assert next(b for b in blocked.button if b.label == '이 원장을 대조 분석에 사용').disabled
        assert any('전체 1행' in w.value and '내 초안 0행' in w.value for w in blocked.warning)
        import json
        from streamlit.proto.WidgetStates_pb2 import WidgetStates
        cp = AppTest.from_string({checkpoint_source!r}).run()
        component = cp.get('component_instance')[0].proto
        args = json.loads(component.json_args)
        value = {{'kind':'checkpoint', 'scope':args['scope'], 'checkpoint':{{'serial':1,'models':{{args['scope']:{{'scope':args['scope'],'active':None,'rows':[{{'id':'55555555-5555-4555-8555-555555555555','raw':{{'chart_no':'123','patient_name':'미전송','payments':{{'카드':'1,00'}}}},'dirty':True,'status':'editing'}}]}}}}}}}}
        states = cp._tree.get_widget_states()
        item = states.widgets.add(); item.id = component.id; item.json_value = json.dumps(value)
        cp._run(states)
        assert not cp.exception
        assert cp.session_state['_ledger_client_'+args['identity']]['models'][args['scope']]['rows'][0]['raw']['patient_name'] == '미전송'
        assert next(b for b in cp.button if b.label == '이 원장을 대조 분석에 사용').disabled
        cp.selectbox(key='ledger_branch').set_value('잠실점'); cp.run()
        assert cp.get('component_instance')[0].proto.id == component.id, 'scope changes must keep iframe identity'
        cp.session_state['hide_ledger'] = True; cp.run(); assert not cp.get('component_instance')
        cp.session_state['hide_ledger'] = False; cp.run()
        restored = json.loads(cp.get('component_instance')[0].proto.json_args)['checkpoint']
        assert restored['models'][args['scope']]['rows'][0]['raw']['payments']['카드'] == '1,00'
        assert cp.session_state['_ledger_client_'+args['identity']]['serial'] == 1
        print('AppTest ledger lifecycle and pre-debounce session checkpoint recovery passed')
    ''')
    result = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_void_ack_marks_row_read_only(ledger):
    from ledger_ui import dispatch_action
    principal, store = ledger
    initial = command('scope')
    dispatch_action(store, principal, '인천점', '2026-10-16', initial, 'scope')
    action = command('scope', initial['id'], action='void', receipt_revision=1,
                     raw={'_void_reason': '잘못 입력한 수납'})
    ack = dispatch_action(store, principal, '인천점', '2026-10-16', action, 'scope')
    assert ack['status'] == 'saved'
    assert ack['voided'] is True


def test_session_checkpoint_keeps_queued_raw_only_for_server_issued_scopes():
    from ledger_component import merge_checkpoint
    pending = {'serial': 1, 'models': {'day-a': {'scope': 'day-a', 'rows': [
        {'id': str(uuid4()), 'raw': {'payments': {'카드': '1,00'}}, 'dirty': True}], 'active': None},
        'not-issued': {'scope': 'not-issued', 'rows': [], 'active': None}}}
    saved = merge_checkpoint(None, pending, {'day-a', 'day-b'})
    assert set(saved['models']) == {'day-a'}
    assert saved['models']['day-a']['rows'][0]['raw']['payments']['카드'] == '1,00'
    later = {'serial': 2, 'models': {'day-b': {'scope': 'day-b', 'rows': [], 'active': None}}}
    updated = merge_checkpoint(saved, later, {'day-a', 'day-b'})
    assert set(updated['models']) == {'day-a', 'day-b'}
    assert merge_checkpoint(updated, pending, {'day-a', 'day-b'}) == updated


def test_local_checkpoint_blocks_close_and_analysis_before_debounce():
    from ledger_component import checkpoint_pending
    checkpoint = {'models': {'scope': {'rows': [{'dirty': True}], 'active': None}}}
    assert checkpoint_pending(checkpoint)
    assert not checkpoint_pending({'models': {'scope': {'rows': [{'dirty': False, 'status': 'saved'}], 'active': None}}})
    assert checkpoint_pending({'models': {'scope': {'rows': [{'status': 'error'}], 'active': None}}})


def test_autosave_ack_arrives_without_extra_app_run_or_eager_analysis(tmp_path):
    import subprocess
    import sys
    import textwrap
    source = textwrap.dedent(f'''
        import streamlit as st
        from branch_access import Principal
        from receipt_ledger import LedgerStore
        from ledger_ui import render_ledger
        st.session_state['full_runs'] = st.session_state.get('full_runs', 0) + 1
        p = Principal('synthetic', 'admin', 'all', '11111111-1111-4111-8111-111111111111')
        s = LedgerStore({str(tmp_path / 'autosave.sqlite')!r}, lambda p:p, local=True)
        render_ledger(p, s, lambda p:p)
    ''')
    driver = textwrap.dedent(f'''
        import json
        from uuid import uuid4
        from streamlit.testing.v1 import AppTest
        import ledger_ui
        conversions = []
        original = ledger_ui._selection
        def selection(day):
            conversions.append(day['revision'])
            return original(day)
        ledger_ui._selection = selection
        at = AppTest.from_string({source!r}).run()
        assert not at.exception
        component = at.get('component_instance')[0].proto
        args = json.loads(component.json_args)
        receipt_id = str(uuid4())
        command = {{'scope':args['scope'], 'id':receipt_id, 'request_id':str(uuid4()),
                    'action':'save', 'receipt_revision':0, 'draft_revision':0,
                    'raw':{{'chart_no':'TEST-LATENCY', 'patient_name':'합성시험', 'payments':{{'카드':'1000'}}}}}}
        def deliver(value):
            states = at._tree.get_widget_states()
            item = states.widgets.add()
            item.id = component.id
            item.json_value = json.dumps(value)
            at._run(states)
            assert not at.exception, [e.message for e in at.exception]
            return json.loads(at.get('component_instance')[0].proto.json_args)
        before = at.session_state['full_runs']
        saved = deliver(command)
        assert at.session_state['full_runs'] == before + 1, 'autosave must not request another full-app run'
        assert saved['ack']['status'] == 'saved'
        assert saved['ack']['receipt_revision'] == 1
        assert saved['rows'][0]['raw']['payments']['카드'] == '1000'
        assert not conversions, 'editing and polling must not build analysis DataFrames'
        replay = deliver(command)
        assert replay['ack'] == saved['ack']
        assert replay['rows'][0]['receipt_revision'] == 1, 'replayed delivery must not duplicate a write'
        command.update(request_id=str(uuid4()), receipt_revision=1)
        command['raw']['payments']['카드'] = '1200'
        updated = deliver(command)
        assert updated['ack']['receipt_revision'] == 2
        assert updated['rows'][0]['raw']['payments']['카드'] == '1200'
        next(b for b in at.button if b.label == '이 원장을 대조 분석에 사용').click()
        at.run()
        assert not at.exception
        assert at.session_state['_ledger_selection']['daily'].iloc[0]['총액'] == 1200
        assert conversions == [2], 'only explicit analysis should build its selection'
        print('Autosave ACK, replay, fresh revision and lazy analysis passed')
    ''')
    result = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
