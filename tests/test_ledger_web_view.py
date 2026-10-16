"""Web and table layouts must describe the same durable ledger."""
from tests.test_ledger_live_actions import run_live_case


def test_confirmed_summary_separates_refunds_voids_and_large_integer_amounts():
    from ledger_ui import editor_summary
    amount = 2**53 + 1
    day = {'receipts': [
        {'kind': 'receipt', 'payments': {'카드': amount, '제로페이': 13}},
        {'kind': 'refund', 'payments': {'카드': 7}},
        {'kind': 'receipt', 'payments': {'카드': 999}, 'voided': True},
    ]}
    assert editor_summary(day) == {
        'gross': str(amount + 13), 'refund': '7', 'net': str(amount + 6),
        'receipt_count': 1, 'refund_count': 1,
    }


def test_web_link_keeps_component_identity_and_summary_excludes_invalid_draft(tmp_path):
    run_live_case(tmp_path, '''
        assert initial['initial_layout'] == 'table'
        assert initial['totals']['net'] == '0'
        at.query_params['ledger_view'] = 'web'
        at.run()
        web = json.loads(at.get('component_instance')[0].proto.json_args)
        assert web['initial_layout'] == 'web'
        assert at.get('component_instance')[0].proto.id == component.id
        assert web['context'] == {'branch': '인천점', 'business_date': '2026-10-15'}
        saved = deliver(command)
        assert saved['totals']['gross'] == saved['totals']['net'] == '1000'
        assert saved['totals']['receipt_count'] == 1
        command.update(request_id=str(uuid4()), receipt_revision=1)
        raw['payments']['카드'] = '1,00'
        draft = deliver(command)
        assert draft['ack']['status'] == 'draft'
        assert draft['rows'][0]['raw']['payments']['카드'] == '1,00'
        assert draft['totals']['net'] == '1000', 'unfinished input is not a saved financial total'
        assert len(draft['channels']) == 14, 'all current channels reach both layouts'
    ''')
