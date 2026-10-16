"""Money, concurrency, permissions and recoverability use real database transactions."""
import copy
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from branch_access import AccessError, Principal
from receipt_ledger import (ClosedDayError, ConflictError, LedgerError, LedgerStore,
                            PAY_CHANNELS, ValidationError)

P = Principal('ledger-admin', 'admin', 'all', str(uuid.uuid4()))
B, D = '강남점', '2026-10-16'


def uid():
    return str(uuid.uuid4())


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request, tmp_path):
    if request.param == 'sqlite':
        return LedgerStore(str(tmp_path / 'ledger.sqlite'), lambda p: p, local=True)
    url = os.environ.get('LEDGER_TEST_DATABASE_URL') or (os.environ.get('DATABASE_URL') if os.environ.get('CI') else None)
    if not url:
        pytest.skip('dedicated PostgreSQL test database not configured')
    import psycopg
    from psycopg import sql
    schema = 'ledger_test_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query['options'] = '-csearch_path=' + schema
    target = urlunsplit(parsed._replace(query=urlencode(query)))
    try:
        yield_store = LedgerStore(target, lambda p: p)
        return yield_store
    finally:
        # Cleanup is registered below; never drop user schemas or unrelated tables.
        request.addfinalizer(lambda: _drop_schema(url, schema))


def _drop_schema(url, schema):
    import psycopg
    from psycopg import sql
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def values(amount=1000, **extra):
    return dict(chart_no='000123', patient_name='합성환자', payments={'카드': amount}, **extra)


def save(store, receipt_id=None, amount=1000, **extra):
    return store.save_receipt(P, uid(), receipt_id or uid(), 0, B, D, values(amount, **extra))


def test_explicit_local_only(tmp_path):
    with pytest.raises(LedgerError):
        LedgerStore(str(tmp_path / 'accidental.sqlite'), lambda p: p)


def test_receipt_idempotency_and_changed_request_rejected(store):
    request, receipt = uid(), uid()
    saved = store.save_receipt(P, request, receipt, 0, B, D, values())
    assert store.save_receipt(P, request, receipt, 0, B, D, values()) == saved
    with pytest.raises(ConflictError):
        store.save_receipt(P, request, receipt, 0, B, D, values(2000))
    day = store.load_day(P, B, D)
    assert len(day['receipts']) == len(store.list_events(P, B, D)) == 1
    assert day['receipts'][0]['payments']['카드'] == 1000
    assert set(day['receipts'][0]['payments']) == set(PAY_CHANNELS)


def test_revision_conflict_retains_current_and_audit(store):
    receipt = save(store)['receipt']
    edited = store.save_receipt(P, uid(), receipt['id'], 1, B, D, values(1200))
    with pytest.raises(ConflictError) as error:
        store.save_receipt(P, uid(), receipt['id'], 1, B, D, values(1500))
    assert error.value.current == edited['receipt']
    assert len(store.list_events(P, B, D)) == 2


@pytest.mark.parametrize('amount', [True, 1.0, '1,000', -1, 2**63, None])
def test_money_never_coerces_invalid_input(store, amount):
    with pytest.raises(ValidationError):
        save(store, amount=amount)
    assert not store.load_day(P, B, D)['receipts']


def test_unknown_channels_and_checkbox_types_rejected(store):
    with pytest.raises(ValidationError):
        store.save_receipt(P, uid(), uid(), 0, B, D, {'payments': {'mystery': 10}})
    with pytest.raises(ValidationError):
        save(store, yeoshin_used='true')
    item = save(store, yeoshin_used=True, gangnamunni_used=True)['receipt']
    assert sum(item['payments'].values()) == 1000


def test_authorization_and_revocation_at_read_write_boundaries(tmp_path):
    valid = True
    def validate(principal):
        if not valid:
            raise AccessError('revoked')
        return principal
    instance = LedgerStore(str(tmp_path / 'permissions.sqlite'), validate, local=True)
    branch_user = Principal('branch', 'branch', B, uid())
    with pytest.raises(AccessError):
        instance.load_day(branch_user, B, D)
    save(instance)
    valid = False
    for call in [lambda: instance.load_day(P, B, D), lambda: save(instance), lambda: instance.export_backup(P)]:
        with pytest.raises(AccessError):
            call()


def test_branch_and_date_scope_rejected(store):
    with pytest.raises(ValidationError):
        store.load_day(P, 'unknown', D)
    with pytest.raises(ValidationError):
        store.load_day(P, B, '2026-02-30')
    receipt = save(store)['receipt']
    with pytest.raises(ConflictError):
        store.save_receipt(P, uid(), receipt['id'], 1, '인천점', D, values())
    assert not store.load_day(P, '인천점', D)['receipts']


def test_invalid_draft_preserved_private_and_clear_on_receipt_save(store):
    receipt_id = uid()
    raw = {'payments': {'카드': '12x'}, 'patient_name': '합성', 'unfinished': '-'}
    result = store.save_draft(P, uid(), receipt_id, 0, B, D, raw)
    assert result['draft']['values'] == raw
    other = Principal('other-admin', 'admin', 'all', uid())
    assert not store.list_drafts(other, B, D)
    with pytest.raises(ValidationError):
        store.close_day(P, uid(), B, D, result['day_revision'])
    store.save_receipt(P, uid(), receipt_id, 0, B, D, values(), expected_draft_revision=1)
    assert not store.list_drafts(P, B, D)


def test_draft_cas_and_delete(store):
    draft_id = uid()
    store.save_draft(P, uid(), draft_id, 0, B, D, {'bad': 'value'})
    with pytest.raises(ConflictError):
        store.save_draft(P, uid(), draft_id, 0, B, D, {'bad': 'overwritten'})
    store.delete_draft(P, uid(), draft_id, 1, B, D)
    assert not store.list_drafts(P, B, D)


def test_refunds_bounded_per_channel_and_original_immutable_under_refund(store):
    original = save(store)['receipt']
    refund = save(store, amount=700, kind='refund', refund_of=original['id'])['receipt']
    with pytest.raises(ValidationError):
        save(store, amount=301, kind='refund', refund_of=original['id'])
    with pytest.raises(ValidationError):
        store.save_receipt(P, uid(), uid(), 0, B, D,
                           {'payments': {'현금': 1}, 'kind': 'refund', 'refund_of': original['id']})
    with pytest.raises(ValidationError):
        store.save_receipt(P, uid(), original['id'], 1, B, D, values(600))
    with pytest.raises(ValidationError):
        store.void_receipt(P, uid(), original['id'], 1, B, D, '원거래 취소')
    store.void_receipt(P, uid(), refund['id'], 1, B, D, '환불 입력 오류')
    assert save(store, amount=1000, kind='refund', refund_of=original['id'])['receipt']['kind'] == 'refund'


def test_softvoid_requires_reason_and_audits_before_after(store):
    receipt = save(store)['receipt']
    with pytest.raises(ValidationError):
        store.void_receipt(P, uid(), receipt['id'], 1, B, D, '')
    result = store.void_receipt(P, uid(), receipt['id'], 1, B, D, '중복 입력')
    assert result['receipt']['voided'] is True
    events = store.list_events(P, B, D)
    assert events[-1]['before']['voided'] is False
    assert events[-1]['after']['void_reason'] == '중복 입력'


def test_close_cas_snapshot_reopen_and_old_retry_after_close(store):
    request, receipt = uid(), uid()
    first = store.save_receipt(P, request, receipt, 0, B, D, values())
    with pytest.raises(ConflictError):
        store.close_day(P, uid(), B, D, 0)
    close = store.close_day(P, uid(), B, D, first['day_revision'], {'chart': 'sha256'}, {'gap': 0})
    assert close['snapshot']['receipts'][0]['id'] == receipt
    assert store.load_day(P, B, D)['state'] == 'closed'
    assert store.save_receipt(P, request, receipt, 0, B, D, values()) == first
    with pytest.raises(ClosedDayError):
        save(store)
    day = store.load_day(P, B, D)
    reopened = store.reopen_day(P, uid(), B, D, day['revision'], '확인 후 정정')
    assert reopened['state'] == 'open'
    store.close_day(P, uid(), B, D, reopened['revision'])
    assert [x['close_sequence'] for x in store.list_snapshots(P, B, D)] == [1, 2]
    assert store.list_days(P, B, D, D)[0]['state'] == 'closed'


def test_same_row_concurrent_writers_cannot_lose_money(store):
    receipt = save(store)['receipt']
    def update(amount):
        try:
            return store.save_receipt(P, uid(), receipt['id'], 1, B, D, values(amount))
        except ConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [1200, 1500]))
    assert sum(item is not None for item in results) == 1
    assert len(store.list_events(P, B, D)) == 2


def test_close_and_save_are_consistent_under_race(store):
    save(store)
    def close():
        try:
            return store.close_day(P, uid(), B, D, 1)
        except ConflictError:
            return None
    def add():
        try:
            return save(store)
        except ClosedDayError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        closing, saving = pool.submit(close), pool.submit(add)
        snapshot, saved = closing.result(), saving.result()
    day = store.load_day(P, B, D)
    if snapshot:
        assert not saved
        assert day['state'] == 'closed'
        assert day['receipts'] == snapshot['snapshot']['receipts']
    else:
        assert saved
        assert day['state'] == 'open'
        assert len(day['receipts']) == 2


def test_backup_verification_restore_and_tampering(store, tmp_path):
    save(store)
    store.close_day(P, uid(), B, D, 1)
    backup = store.export_backup(P)
    assert LedgerStore.verify_backup(backup)['valid'] is True
    target = str(tmp_path / 'restored.sqlite')
    restored = store.restore_backup(P, backup, target)
    assert restored.load_day(P, B, D) == store.load_day(P, B, D)
    assert restored.list_snapshots(P, B, D) == store.list_snapshots(P, B, D)
    assert restored.list_events(P, B, D) == store.list_events(P, B, D)
    with pytest.raises(LedgerError):
        store.restore_backup(P, backup, target)
    corrupt = copy.deepcopy(backup)
    corrupt['payload']['tables']['ledger_receipts'][0]['payload_json'] = '{}'
    with pytest.raises(ValidationError):
        LedgerStore.verify_backup(corrupt)
    with pytest.raises(LedgerError):
        store.restore_backup(P, backup, 'postgresql://example.invalid/prod')


def test_prior_day_refund_cumulative_across_dates(store):
    original = save(store)['receipt']
    store.close_day(P, uid(), B, D, 1)
    for day, amount in [('2026-10-17', 600), ('2026-10-18', 400)]:
        store.save_receipt(P, uid(), uid(), 0, B, day, values(amount, kind='refund', refund_of=original['id']))
    with pytest.raises(ValidationError):
        store.save_receipt(P, uid(), uid(), 0, B, '2026-10-19', values(1, kind='refund', refund_of=original['id']))


def test_summary_isolated_idempotent_and_in_backup(store):
    from reconciliation_history import make_record
    totals = dict(h_card=1000, h_cash=0, d_card=1000, d_cashxfer=0, d_plat=0, p_card=1000, p_cashxfer=0, p_plat=0)
    channels = [dict(zip(('채널','한솔','일마','차트','일마-차트','한솔-차트','한솔-일마'), row)) for row in [('카드',1000,1000,1000,0,0,0),('현금+이체',0,0,0,0,0,0),('플랫폼',None,0,0,0,None,None)]]
    record = make_record(B, D, totals, channels, {}, 0, 0, False, True)
    assert store.append_summary(P, record) == record
    assert store.append_summary(P, record) == record
    assert store.list_summaries(P, B, '2026-10') == [record]
    assert len(store.export_backup(P)['payload']['tables']['analysis_summary']) == 1
    changed = dict(record, recorded_at='2026-10-17T00:00:00+00:00')
    with pytest.raises(ConflictError):
        store.append_summary(P, changed)


def test_concurrent_refunds_on_different_days_cannot_exceed_original(store):
    original = save(store)['receipt']
    def refund(day):
        try:
            return store.save_receipt(P, uid(), uid(), 0, B, day, values(700, kind='refund', refund_of=original['id']))
        except ValidationError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(refund, ['2026-10-17', '2026-10-18']))
    assert sum(result is not None for result in results) == 1


def test_stale_same_user_receipt_does_not_delete_newer_draft(store):
    receipt_id = uid()
    store.save_draft(P, uid(), receipt_id, 0, B, D, {'incomplete': '1x'})
    store.save_draft(P, uid(), receipt_id, 1, B, D, {'incomplete': 'newest'})
    with pytest.raises(ConflictError):
        store.save_receipt(P, uid(), receipt_id, 0, B, D, values(), expected_draft_revision=1)
    assert not store.load_day(P, B, D)['receipts']
    store.save_receipt(P, uid(), receipt_id, 0, B, D, values())
    assert store.list_drafts(P, B, D)[0]['values'] == {'incomplete': 'newest'}
    store.save_receipt(P, uid(), receipt_id, 1, B, D, values(), expected_draft_revision=2)
    assert not store.list_drafts(P, B, D)


def test_backup_rejects_rechecksummed_orphan_and_excess_refund(store):
    import hashlib
    original = save(store)['receipt']
    save(store, amount=500, kind='refund', refund_of=original['id'])
    backup = store.export_backup(P)
    def checksum(item):
        item['sha256'] = hashlib.sha256(json.dumps(item['payload'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    orphan = copy.deepcopy(backup)
    orphan['payload']['tables']['ledger_days'] = []
    checksum(orphan)
    with pytest.raises(ValidationError):
        LedgerStore.verify_backup(orphan)
    excess = copy.deepcopy(backup)
    row = next(row for row in excess['payload']['tables']['ledger_receipts'] if row['kind'] == 'refund')
    payload = json.loads(row['payload_json'])
    payload['payments']['카드'] = 2000
    row['payload_json'] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    checksum(excess)
    with pytest.raises(ValidationError):
        LedgerStore.verify_backup(excess)


def test_missing_payments_is_not_an_explicit_zero_receipt(store):
    for missing in ({}, {'chart_no': '123'}, {'payments': {}}):
        with pytest.raises(ValidationError):
            store.save_receipt(P, uid(), uid(), 0, B, D, missing)
    assert save(store, amount=0)['receipt']['payments']['카드'] == 0


def test_other_admin_incomplete_drafts_count_blocks_close_without_exposing_contents(store):
    other = Principal('other-admin', 'admin', 'all', uid())
    store.save_draft(other, uid(), uid(), 0, B, D, {'private': 'unfinished'})
    day = store.load_day(P, B, D)
    assert day['pending_draft_count'] == 1
    assert store.list_drafts(P, B, D) == []
    assert 'unfinished' not in json.dumps(day)
    with pytest.raises(ValidationError):
        store.close_day(P, uid(), B, D, day['revision'])


def test_concurrent_different_receipts_both_survive(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda amount: save(store, amount=amount), [1200, 1500]))
    day = store.load_day(P, B, D)
    assert len(results) == len(day['receipts']) == 2
    assert sum(item['payments']['카드'] for item in day['receipts']) == 2700
    assert day['revision'] == 2


def test_same_request_concurrent_retry_has_one_effect(store):
    request_id, receipt_id = uid(), uid()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.save_receipt, P, request_id, receipt_id, 0, B, D, values()) for _ in range(2)]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    assert len(store.load_day(P, B, D)['receipts']) == 1
    assert len(store.list_events(P, B, D)) == 1


@pytest.mark.parametrize('changed', [{'chart_no': '999'}, {'patient_name': '다른합성환자'}])
def test_refund_identity_must_match_original_and_rollback(store, changed):
    original = save(store)['receipt']
    payload = values(500, kind='refund', refund_of=original['id']) | changed
    with pytest.raises(ValidationError):
        store.save_receipt(P, uid(), uid(), 0, B, D, payload)
    assert len(store.load_day(P, B, D)['receipts']) == 1
    assert len(store.list_events(P, B, D)) == 1


def test_original_identity_cannot_change_under_active_refunds(store):
    original = save(store)['receipt']
    refund = save(store, amount=500, kind='refund', refund_of=original['id'])['receipt']
    with pytest.raises(ValidationError):
        store.save_receipt(P, uid(), original['id'], 1, B, D, values() | {'chart_no': '999'})
    store.void_receipt(P, uid(), refund['id'], 1, B, D, '환불 입력 오류 정리')
    assert store.save_receipt(P, uid(), original['id'], 1, B, D, values() | {'chart_no': '999'})['receipt']['chart_no'] == '999'


def test_admin_discards_orphan_draft_using_metadata_reason_and_cas(store):
    departed = Principal('departed-admin', 'admin', 'all', uid())
    draft_id, request_id = uid(), uid()
    store.save_draft(departed, uid(), draft_id, 0, B, D, {'private': 'unfinished'})
    pending = store.list_pending_drafts(P, B, D)
    assert pending == [dict(id=draft_id, owner=departed.username, branch=B, business_date=D, revision=1, updated_at=pending[0]['updated_at'])]
    assert 'unfinished' not in json.dumps(pending)
    with pytest.raises(ValidationError):
        store.discard_pending_draft(P, uid(), draft_id, 1, B, D, departed.username, '')
    with pytest.raises(ConflictError) as error:
        store.discard_pending_draft(P, uid(), draft_id, 2, B, D, departed.username, '퇴사자 미완료 초안 정리')
    assert 'values' not in error.value.current
    result = store.discard_pending_draft(P, request_id, draft_id, 1, B, D, departed.username, '퇴사자 미완료 초안 정리')
    assert result['deleted'] is True
    assert store.list_pending_drafts(P, B, D) == []
    events = store.list_events(P, B, D)
    assert events[-1]['actor'] == P.username
    assert events[-1]['before']['owner'] == departed.username
    assert events[-1]['after']['reason'] == '퇴사자 미완료 초안 정리'
    store.close_day(P, uid(), B, D, result['day_revision'])
    assert store.discard_pending_draft(P, request_id, draft_id, 1, B, D, departed.username, '퇴사자 미완료 초안 정리') == result


def test_workspace_authenticates_once_and_keeps_one_database_snapshot(store, monkeypatch):
    calls, triggered = [], False
    original_validate, original_view = store._validate, store._view
    def counted(principal):
        calls.append(principal)
        return original_validate(principal)
    monkeypatch.setattr(store, '_validate', counted)
    def view_then_concurrent_change(connection, branch, business_date):
        nonlocal triggered
        day = original_view(connection, branch, business_date)
        if not triggered:
            triggered = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(store.save_draft, P, uid(), uid(), 0, B, D, {'unfinished': '1x'}).result()
        return day
    monkeypatch.setattr(store, '_view', view_then_concurrent_change)
    first = store.load_workspace(P, B, D)
    assert first['day']['pending_draft_count'] == 0
    assert first['drafts'] == []
    assert len(calls) == 2  # One workspace auth and one concurrent mutation auth.
    calls.clear()
    next_read = store.load_workspace(P, B, D)
    assert len(calls) == 1
    assert next_read['day']['pending_draft_count'] == len(next_read['drafts']) == 1


def test_new_admin_apis_recheck_account_and_reject_branch_users(store):
    branch_user = Principal('branch-user', 'branch', B, uid())
    for call in [lambda: store.load_workspace(branch_user, B, D), lambda: store.list_pending_drafts(branch_user, B, D), lambda: store.discard_pending_draft(branch_user, uid(), uid(), 1, B, D, P.username, '정리')]:
        with pytest.raises(AccessError):
            call()


def test_analysis_summary_source_is_durable_idempotent_and_scoped(store, tmp_path):
    from reconciliation_history import make_record
    totals = dict(h_card=1000, h_cash=0, d_card=1000, d_cashxfer=0, d_plat=0, p_card=1000, p_cashxfer=0, p_plat=0)
    channels = [dict(zip(('채널','한솔','일마','차트','일마-차트','한솔-차트','한솔-일마'), row)) for row in [('카드',1000,1000,1000,0,0,0),('현금+이체',0,0,0,0,0,0),('플랫폼',None,0,0,0,None,None)]]
    record = make_record(B, D, totals, channels, {}, 0, 0, False, True)
    revision = save(store)['day_revision']
    manifest = {'branch': B, 'business_date': D, 'revision': revision}
    assert store.append_summary(P, record, source_manifest=manifest) == record
    assert store.append_summary(P, record, source_manifest=manifest) == record
    assert store.get_summary_source(P, record['run_id']) == manifest
    assert store.list_summaries(P, B, '2026-10') == [record]
    with pytest.raises(ConflictError):
        store.append_summary(P, record)
    wrong_scope = dict(manifest, business_date='2026-10-17')
    with pytest.raises(ValidationError):
        store.append_summary(P, dict(record, run_id=uid()), source_manifest=wrong_scope)
    backup = store.export_backup(P)
    assert LedgerStore.verify_backup(backup)['valid']
    restored = store.restore_backup(P, backup, str(tmp_path / 'source-restore.sqlite'))
    assert restored.get_summary_source(P, record['run_id']) == manifest
    assert len(backup['payload']['tables']['analysis_summary_source']) == 1


def test_summary_source_transaction_rejects_stale_or_pending_but_replays_saved_run(store):
    from reconciliation_history import make_record
    totals = dict(h_card=1000, h_cash=0, d_card=1000, d_cashxfer=0, d_plat=0, p_card=1000, p_cashxfer=0, p_plat=0)
    channels = [dict(zip(('채널','한솔','일마','차트','일마-차트','한솔-차트','한솔-일마'), row)) for row in [('카드',1000,1000,1000,0,0,0),('현금+이체',0,0,0,0,0,0),('플랫폼',None,0,0,0,None,None)]]
    record = make_record(B, D, totals, channels, {}, 0, 0, False, True)
    revision = save(store)['day_revision']
    manifest = {'branch': B, 'business_date': D, 'revision': revision}
    store.append_summary(P, record, source_manifest=manifest)
    save(store)
    assert store.append_summary(P, record, source_manifest=manifest) == record
    with pytest.raises(ConflictError):
        store.append_summary(P, dict(record, run_id=uid()), source_manifest=manifest)
    other = Principal('other-admin', 'admin', 'all', uid())
    result = store.save_draft(other, uid(), uid(), 0, B, D, {'unfinished': 'bad'})
    with pytest.raises(ValidationError):
        store.append_summary(P, dict(record, run_id=uid()), source_manifest=dict(manifest, revision=result['day_revision']))
    assert store.list_summaries(P, B, '2026-10') == [record]
