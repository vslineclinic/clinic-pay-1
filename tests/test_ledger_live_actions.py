"""Live component commands must keep authorization, scope and refresh failures safe."""
import subprocess
import sys
import textwrap


def run_live_case(tmp_path, checks):
    # conftest installs a global Streamlit stub; exercise real callbacks in a clean process.
    database = str(tmp_path / 'live-actions.sqlite')
    source = textwrap.dedent(f'''
        from datetime import date
        import streamlit as st
        from branch_access import AccessError, Principal
        from receipt_ledger import LedgerStore
        from ledger_ui import render_ledger

        def validate_store(principal):
            if st.session_state.get('revoked'):
                raise AccessError('합성 계정 권한이 취소되었습니다.')
            return principal

        class ProbeStore(LedgerStore):
            def load_workspace(self, *args):
                st.session_state['workspace_reads'] = st.session_state.get('workspace_reads', 0) + 1
                if st.session_state.pop('fail_workspace_once', False):
                    raise OSError('synthetic connection interruption')
                return super().load_workspace(*args)

        p = Principal('synthetic', 'admin', 'all', '11111111-1111-4111-8111-111111111111')
        s = ProbeStore({database!r}, validate_store, local=True)
        st.session_state.setdefault('ledger_business_date', date(2026, 10, 15))
        # Simulate the identity already validated before a command reaches the fragment.
        render_ledger(p, s, lambda p: p)
    ''')
    driver = textwrap.dedent(f'''
        import json
        from datetime import date
        from uuid import uuid4
        from streamlit.testing.v1 import AppTest
        from branch_access import Principal
        from receipt_ledger import LedgerStore

        at = AppTest.from_string({source!r}).run()
        assert not at.exception, [e.message for e in at.exception]
        component = at.get('component_instance')[0].proto
        initial = json.loads(component.json_args)
        scope = initial['scope']
        workspace_key = '_ledger_workspace_' + scope
        assert workspace_key in at.session_state
        assert initial['rows'] == []
        receipt_id = str(uuid4())
        raw = {{'chart_no': 'TEST-LIVE', 'patient_name': '합성시험', 'payments': {{'카드': '1000'}}}}

        def checkpoint(serial):
            return {{'serial': serial, 'models': {{scope: {{'scope': scope, 'active': None,
                'rows': [{{'id': receipt_id, 'raw': raw, 'dirty': True, 'status': 'editing'}}]}}}}}}

        command = {{'scope': scope, 'id': receipt_id, 'request_id': str(uuid4()), 'action': 'save',
                   'receipt_revision': 0, 'draft_revision': 0, 'raw': raw, 'checkpoint': checkpoint(1)}}

        def deliver(value):
            states = at._tree.get_widget_states()
            item = next((w for w in states.widgets if w.id == component.id), None)
            if item is None:
                item = states.widgets.add()
                item.id = component.id
            item.json_value = json.dumps(value)
            at._run(states)
            assert not at.exception, [e.message for e in at.exception]
            rendered = at.get('component_instance')
            return json.loads(rendered[0].proto.json_args) if rendered else None

        principal = Principal('synthetic', 'admin', 'all', '11111111-1111-4111-8111-111111111111')
        inspect = LedgerStore({database!r}, lambda p: p, local=True)
    ''') + textwrap.dedent(checks)
    result = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_saved_command_survives_failed_refresh_without_stale_workspace_or_double_write(tmp_path):
    run_live_case(tmp_path, '''
        before_reads = at.session_state['workspace_reads']
        at.session_state['fail_workspace_once'] = True
        assert deliver(command) is None
        assert any('원장을 불러오지 못했습니다' in error.value for error in at.error)
        assert workspace_key not in at.session_state, 'a saved ACK must not retain the pre-save workspace'
        ack = at.session_state['_ledger_ack_' + scope]
        assert ack['status'] == 'saved'
        assert at.session_state['workspace_reads'] == before_reads + 1
        assert len(inspect.list_events(principal, '인천점', '2026-10-15')) == 1

        at.run()  # Retry the interrupted read after the component was omitted from the failed render.
        assert not at.exception, [e.message for e in at.exception]
        restored = json.loads(at.get('component_instance')[0].proto.json_args)
        assert at.session_state['workspace_reads'] == before_reads + 2
        assert restored['ack'] == ack
        assert restored['rows'][0]['id'] == receipt_id
        assert restored['rows'][0]['raw']['payments']['카드'] == '1000'
        assert restored['rows'][0]['receipt_revision'] == 1

        fresh = deliver({'kind': 'checkpoint', 'scope': scope, 'checkpoint': checkpoint(2)})
        assert fresh['rows'] == restored['rows']
        command['checkpoint'] = checkpoint(3)
        assert deliver(command)['ack'] == ack
        assert len(inspect.list_events(principal, '인천점', '2026-10-15')) == 1
        assert inspect.load_day(principal, '인천점', '2026-10-15')['revision'] == 1
    ''')


def test_revoked_store_authorization_blocks_command_and_cached_component(tmp_path):
    run_live_case(tmp_path, '''
        at.session_state['revoked'] = True
        assert deliver(command) is None
        assert any('권한이 취소되었습니다' in error.value for error in at.error)
        assert workspace_key not in at.session_state
        assert at.session_state['_ledger_ack_' + scope]['status'] == 'error'
        assert inspect.load_day(principal, '인천점', '2026-10-15')['receipts'] == []
        assert inspect.list_events(principal, '인천점', '2026-10-15') == []
        at.run()
        assert not at.exception
        assert not at.get('component_instance'), 'a later refresh must not restore a revoked editor'
    ''')


def test_stale_command_with_date_change_never_writes_selected_day(tmp_path):
    run_live_case(tmp_path, '''
        at.date_input(key='ledger_business_date').set_value(date(2026, 10, 16))
        current = deliver(command)  # An old iframe command and the changed date arrive together.
        assert current['scope'] != scope
        assert at.get('component_instance')[0].proto.id == component.id
        assert current['rows'] == []
        assert current['ack'] is None
        assert any('2026-10-16' in caption.value for caption in at.caption)
        for day in ('2026-10-15', '2026-10-16'):
            assert inspect.load_day(principal, '인천점', day)['receipts'] == []
            assert inspect.list_events(principal, '인천점', day) == []
    ''')
