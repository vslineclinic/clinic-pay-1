"""Transactional receipt ledger. SQLite is explicit local development only.

Public methods take a current Principal first. Money never accepts coercion.
PostgreSQL and SQLite share short day-locked transactions, receipt CAS and an
atomic request log. Cloud deployment must supply a separate PostgreSQL URI.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from branch_access import AccessError, CANONICAL_BRANCHES, Principal

PAY_CHANNELS = ('카드', '현금', '이체', '여신티켓', '강남언니', '나만의닥터',
                '제로페이', '기타지역화폐', '알리페이', '위챗페이', '카카오페이',
                '간편결제', '바비톡', '닥터나우')
MAX_MONEY = 2**63 - 1
TABLES = {
    'ledger_days': ('branch', 'business_date', 'revision', 'state', 'close_sequence', 'updated_at', 'updated_by'),
    'ledger_receipts': ('id', 'branch', 'business_date', 'revision', 'kind', 'refund_of', 'payload_json', 'voided', 'void_reason', 'created_at', 'created_by', 'updated_at', 'updated_by'),
    'ledger_drafts': ('id', 'owner', 'branch', 'business_date', 'revision', 'payload_json', 'updated_at'),
    'ledger_events': ('event_id', 'request_id', 'request_hash', 'branch', 'business_date', 'day_revision', 'operation', 'actor', 'occurred_at', 'before_json', 'after_json', 'result_json'),
    'ledger_snapshots': ('id', 'branch', 'business_date', 'close_sequence', 'day_revision', 'payload_json', 'sha256', 'closed_at', 'closed_by'),
    'analysis_summary': ('run_id', 'branch', 'business_date', 'payload_json'),
    'analysis_summary_source': ('run_id', 'branch', 'business_date', 'manifest_json'),
}
DDL = (
    'CREATE TABLE IF NOT EXISTS ledger_days (branch TEXT NOT NULL, business_date TEXT NOT NULL, revision BIGINT NOT NULL, state TEXT NOT NULL, close_sequence BIGINT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, PRIMARY KEY (branch,business_date))',
    'CREATE TABLE IF NOT EXISTS ledger_receipts (id TEXT PRIMARY KEY, branch TEXT NOT NULL, business_date TEXT NOT NULL, revision BIGINT NOT NULL, kind TEXT NOT NULL, refund_of TEXT, payload_json TEXT NOT NULL, voided INTEGER NOT NULL, void_reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS receipt_scope ON ledger_receipts(branch,business_date)',
    'CREATE INDEX IF NOT EXISTS receipt_refund ON ledger_receipts(refund_of)',
    'CREATE TABLE IF NOT EXISTS ledger_drafts (id TEXT NOT NULL, owner TEXT NOT NULL, branch TEXT NOT NULL, business_date TEXT NOT NULL, revision BIGINT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(id,owner))',
    'CREATE INDEX IF NOT EXISTS draft_scope ON ledger_drafts(branch,business_date)',
    'CREATE TABLE IF NOT EXISTS ledger_events (event_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, branch TEXT NOT NULL, business_date TEXT NOT NULL, day_revision BIGINT NOT NULL, operation TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, result_json TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS event_scope ON ledger_events(branch,business_date,day_revision)',
    'CREATE TABLE IF NOT EXISTS ledger_snapshots (id TEXT PRIMARY KEY, branch TEXT NOT NULL, business_date TEXT NOT NULL, close_sequence BIGINT NOT NULL, day_revision BIGINT NOT NULL, payload_json TEXT NOT NULL, sha256 TEXT NOT NULL, closed_at TEXT NOT NULL, closed_by TEXT NOT NULL, UNIQUE(branch,business_date,close_sequence))',
    'CREATE TABLE IF NOT EXISTS analysis_summary (run_id TEXT PRIMARY KEY, branch TEXT NOT NULL, business_date TEXT NOT NULL, payload_json TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS analysis_summary_source (run_id TEXT PRIMARY KEY, branch TEXT NOT NULL, business_date TEXT NOT NULL, manifest_json TEXT NOT NULL)',
)


class LedgerError(ValueError):
    """Sanitized error, with no database URI, raw SQL or patient data in message."""


class ValidationError(LedgerError):
    pass


class ConflictError(LedgerError):
    def __init__(self, message='다른 변경이 먼저 저장되었습니다. 내 입력을 유지하고 최신 내용을 확인해 주세요.', *, current=None):
        super().__init__(message)
        self.current = current


class ClosedDayError(LedgerError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValidationError('저장할 값의 형식을 확인해 주세요.') from None


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('거래 또는 요청 식별자가 올바르지 않습니다.') from None
    return value


def _integer(value, *, zero=True):
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_MONEY:
        raise ValidationError('금액과 개정은 음수가 아닌 정수여야 합니다.')
    return value


def _scope(branch, business_date):
    try:
        if branch not in CANONICAL_BRANCHES or not isinstance(business_date, str) or date.fromisoformat(business_date).isoformat() != business_date:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError('지점과 영업일을 확인해 주세요.') from None
    return branch, business_date


def _text(value, maximum=2000):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValidationError('문자 입력 형식 또는 길이를 확인해 주세요.')
    return value


def _reason(value):
    if not _text(value).strip():
        raise ValidationError('처리 사유를 입력해 주세요.')
    return value.strip()


def _raw(values):
    if not isinstance(values, dict) or len(_json(values).encode('utf-8')) > 100_000:
        raise ValidationError('초안 또는 자료의 크기와 형식을 확인해 주세요.')
    return json.loads(_json(values))


def _values(values):
    allowed = {'chart_no', 'patient_name', 'note', 'payments', 'yeoshin_used', 'gangnamunni_used', 'kind', 'refund_of'}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValidationError('수납 항목을 확인해 주세요.')
    payments = values.get('payments')
    if not isinstance(payments, dict) or not payments or set(payments) - set(PAY_CHANNELS):
        raise ValidationError('결제수단을 확인해 주세요.')
    clean = {key: _integer(payments.get(key, 0)) for key in PAY_CHANNELS}
    if sum(clean.values()) > MAX_MONEY:
        raise ValidationError('합계 금액이 너무 큽니다.')
    result = {key: _text(values.get(key, ''), 80 if key == 'chart_no' else 200 if key == 'patient_name' else 2000)
              for key in ('chart_no', 'patient_name', 'note')}
    result['payments'] = clean
    for key in ('yeoshin_used', 'gangnamunni_used'):
        result[key] = values.get(key, False)
        if type(result[key]) is not bool:
            raise ValidationError('사용 표시는 체크박스 값이어야 합니다.')
    result['kind'] = values.get('kind', 'receipt')
    result['refund_of'] = values.get('refund_of')
    if result['kind'] not in ('receipt', 'refund'):
        raise ValidationError('수납 또는 연결 환불을 선택해 주세요.')
    if result['kind'] == 'refund':
        _uuid(result['refund_of'])
        if not sum(clean.values()):
            raise ValidationError('환불 금액을 입력해 주세요.')
    elif result['refund_of'] is not None:
        raise ValidationError('일반 수납에는 원거래 연결을 넣을 수 없습니다.')
    return result


def _summary_manifest(value, branch, business_date):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'branch', 'business_date', 'revision'}:
        raise ValidationError('분석 원장의 지점·영업일·개정을 확인해 주세요.')
    if (value['branch'], value['business_date']) != (branch, business_date):
        raise ValidationError('분석 요약과 원장 출처의 범위가 다릅니다.')
    _scope(branch, business_date)
    _integer(value['revision'])
    return dict(value)


def _identity(receipt):
    return receipt['chart_no'].strip(), receipt['patient_name'].strip()


class LedgerStore:
    def __init__(self, target, validate, *, local=False):
        if not callable(validate) or not isinstance(target, str) or not target:
            raise LedgerError('원장 저장소 설정을 확인해 주세요.')
        self._validate = validate
        self.local = local is True
        self._postgres = target.startswith(('postgresql://', 'postgres://'))
        if self._postgres == self.local or (not self._postgres and (target == ':memory:' or '://' in target)):
            raise LedgerError('원격 PostgreSQL 또는 명시적 로컬 시험 경로가 필요합니다.')
        self._target = target
        with self._transaction(write=True) as connection:
            if self._postgres:
                self._exec(connection, 'SELECT pg_advisory_xact_lock(?)', (812340221,))
            for statement in DDL:
                self._exec(connection, statement)

    def _auth(self, principal):
        if not isinstance(principal, Principal):
            raise AccessError('총괄 관리자만 접근할 수 있습니다.')
        current = self._validate(principal)
        if not isinstance(current, Principal) or current != principal or current.role != 'admin' or current.branch != 'all':
            raise AccessError('총괄 관리자만 접근할 수 있습니다.')
        return current

    @contextmanager
    def _transaction(self, *, write=False, snapshot=False):
        connection = None
        try:
            if self._postgres:
                import psycopg
                from psycopg.rows import dict_row
                connection = psycopg.connect(self._target, row_factory=dict_row, connect_timeout=10)
                if snapshot:
                    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            else:
                connection = sqlite3.connect(self._target, timeout=15, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA busy_timeout=15000')
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            connection.commit()
        except (LedgerError, AccessError):
            if connection is not None:
                connection.rollback()
            raise
        except Exception:
            if connection is not None:
                connection.rollback()
            raise LedgerError('원장 저장소에 연결하거나 저장을 확인하지 못했습니다. 같은 요청으로 다시 시도해 주세요.') from None
        finally:
            if connection is not None:
                connection.close()

    def _exec(self, connection, query, params=()):
        return connection.execute(query.replace('?', '%s') if self._postgres else query, params)

    def _one(self, connection, query, params=()):
        row = self._exec(connection, query, params).fetchone()
        return dict(row) if row is not None else None

    def _all(self, connection, query, params=()):
        return [dict(row) for row in self._exec(connection, query, params).fetchall()]

    def _insert(self, connection, table, row):
        columns = TABLES[table]
        self._exec(connection, f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row[key] for key in columns))

    def _day(self, connection, branch, business_date, *, lock=False):
        if lock:
            self._exec(connection, 'INSERT INTO ledger_days (branch,business_date,revision,state,close_sequence,updated_at,updated_by) VALUES (?,?,0,?,0,?,?) ON CONFLICT (branch,business_date) DO NOTHING', (branch, business_date, 'open', '', ''))
        suffix = ' FOR UPDATE' if lock and self._postgres else ''
        return self._one(connection, 'SELECT * FROM ledger_days WHERE branch=? AND business_date=?' + suffix, (branch, business_date)) or dict(branch=branch, business_date=business_date, revision=0, state='open', close_sequence=0, updated_at='', updated_by='')

    @staticmethod
    def _receipt(row):
        return {key: row[key] for key in TABLES['ledger_receipts'] if key != 'payload_json' and key != 'voided'} | json.loads(row['payload_json']) | {'voided': bool(row['voided'])}

    @staticmethod
    def _draft(row):
        return {key: row[key] for key in TABLES['ledger_drafts'] if key != 'payload_json'} | {'values': json.loads(row['payload_json'])}

    def _view(self, connection, branch, business_date):
        day = self._day(connection, branch, business_date)
        day['pending_draft_count'] = self._one(connection, 'SELECT COUNT(*) AS count FROM ledger_drafts WHERE branch=? AND business_date=?', (branch, business_date))['count']
        day['receipts'] = [self._receipt(row) for row in self._all(connection, 'SELECT * FROM ledger_receipts WHERE branch=? AND business_date=? ORDER BY created_at,id', (branch, business_date))]
        return day

    def load_day(self, principal, branch, business_date):
        self._auth(principal)
        _scope(branch, business_date)
        with self._transaction(snapshot=True) as connection:
            return self._view(connection, branch, business_date)

    def load_workspace(self, principal, branch, business_date):
        principal = self._auth(principal)
        _scope(branch, business_date)
        with self._transaction(snapshot=True) as connection:
            day = self._view(connection, branch, business_date)
            drafts = [self._draft(row) for row in self._all(connection, 'SELECT * FROM ledger_drafts WHERE owner=? AND branch=? AND business_date=? ORDER BY updated_at,id', (principal.username, branch, business_date))]
            return {'day': day, 'drafts': drafts}

    def list_days(self, principal, branch, start_date, end_date):
        self._auth(principal)
        _scope(branch, start_date)
        _scope(branch, end_date)
        if start_date > end_date:
            raise ValidationError('조회 날짜 순서를 확인해 주세요.')
        with self._transaction() as connection:
            return self._all(connection, 'SELECT * FROM ledger_days WHERE branch=? AND business_date>=? AND business_date<=? ORDER BY business_date', (branch, start_date, end_date))

    def _mutate(self, principal, request_id, branch, business_date, operation, arguments, apply, *, allow_closed=False):
        principal = self._auth(principal)
        _uuid(request_id)
        _scope(branch, business_date)
        fingerprint = _digest({'actor': principal.username, 'branch': branch, 'date': business_date, 'operation': operation, 'arguments': arguments})
        with self._transaction(write=True) as connection:
            day = self._day(connection, branch, business_date, lock=True)
            previous = self._one(connection, 'SELECT * FROM ledger_events WHERE request_id=?', (request_id,))
            if previous:
                if previous['request_hash'] != fingerprint:
                    raise ConflictError('같은 요청 번호에 다른 내용이 있습니다. 저장 상태를 확인해 주세요.')
                return json.loads(previous['result_json'])
            if day['state'] == 'closed' and not allow_closed:
                raise ClosedDayError('마감된 영업일입니다. 사유를 남기고 재개방한 뒤 수정해 주세요.')
            before, after, result = apply(connection, day, principal)
            day['revision'] += 1
            day['updated_at'], day['updated_by'] = _now(), principal.username
            self._exec(connection, 'UPDATE ledger_days SET revision=?,state=?,close_sequence=?,updated_at=?,updated_by=? WHERE branch=? AND business_date=?', (day['revision'], day['state'], day['close_sequence'], day['updated_at'], day['updated_by'], branch, business_date))
            # Result builders execute after the new day revision and timestamp exist.
            if callable(result):
                result = result(connection, day)
            elif isinstance(result, dict):
                result['day_revision'] = day['revision']
            self._insert(connection, 'ledger_events', dict(event_id=str(uuid4()), request_id=request_id, request_hash=fingerprint, branch=branch, business_date=business_date, day_revision=day['revision'], operation=operation, actor=principal.username, occurred_at=day['updated_at'], before_json=_json(before), after_json=_json(after), result_json=_json(result)))
            return result

    def _get_receipt(self, connection, receipt_id, branch, business_date, expected_revision):
        row = self._one(connection, 'SELECT * FROM ledger_receipts WHERE id=?' + (' FOR UPDATE' if self._postgres else ''), (receipt_id,))
        current = self._receipt(row) if row else None
        if (row and (row['branch'], row['business_date']) != (branch, business_date)):
            raise ConflictError('거래의 지점 또는 영업일이 다릅니다.')
        if (row['revision'] if row else 0) != expected_revision:
            raise ConflictError(current=current)
        return row, current

    def _check_refunds(self, connection, branch, business_date, candidate):
        original_id = candidate['refund_of'] if candidate['kind'] == 'refund' else candidate['id']
        # All days refunding one original serialize on that original row.
        row = self._one(connection, 'SELECT * FROM ledger_receipts WHERE id=?' + (' FOR UPDATE' if self._postgres else ''), (original_id,))
        original = candidate if candidate['kind'] == 'receipt' else self._receipt(row) if row else None
        if original is None or original['branch'] != branch or original['kind'] != 'receipt':
            raise ValidationError('같은 지점의 유효한 원수납을 선택해 주세요.')
        refunds = [self._receipt(row) for row in self._all(connection, 'SELECT * FROM ledger_receipts WHERE refund_of=?', (original_id,))]
        refunds = [item for item in refunds if item['id'] != candidate['id']]
        if candidate['kind'] == 'refund':
            refunds.append(candidate)
        totals = dict.fromkeys(PAY_CHANNELS, 0)
        for item in refunds:
            if item['voided']:
                continue
            if original['voided'] or item['business_date'] < original['business_date']:
                raise ValidationError('취소한 원수납 또는 원수납 이전 영업일에는 환불할 수 없습니다.')
            if _identity(item) != _identity(original):
                raise ValidationError('환불의 차트번호와 이름은 원수납과 같아야 합니다. 활성 환불이 있으면 원수납 식별정보를 변경할 수 없습니다.')
            for channel, amount in item['payments'].items():
                totals[channel] += amount
                if totals[channel] > original['payments'][channel]:
                    raise ValidationError('결제수단별 누적 환불이 원수납 금액을 넘습니다.')

    def save_receipt(self, principal, request_id, receipt_id, expected_revision, branch, business_date, values, *, expected_draft_revision=None):
        _uuid(receipt_id)
        _integer(expected_revision)
        clean = _values(values)
        if expected_draft_revision is not None:
            _integer(expected_draft_revision)
        def apply(connection, day, actor):
            row, before = self._get_receipt(connection, receipt_id, branch, business_date, expected_revision)
            if row and row['voided']:
                raise ValidationError('취소한 수납은 수정할 수 없습니다. 새 수납을 입력해 주세요.')
            if before and (before['kind'], before['refund_of']) != (clean['kind'], clean['refund_of']):
                raise ValidationError('저장한 거래의 수납·환불 종류와 원거래는 바꿀 수 없습니다.')
            if expected_draft_revision is not None:
                draft = self._one(connection, 'SELECT * FROM ledger_drafts WHERE id=? AND owner=?', (receipt_id, actor.username))
                if draft and (draft['branch'], draft['business_date']) != (branch, business_date):
                    raise ConflictError('초안의 지점 또는 날짜가 다릅니다.')
                if (draft['revision'] if draft else 0) != expected_draft_revision:
                    raise ConflictError(current=self._draft(draft) if draft else None)
            timestamp = _now()
            stored = dict(id=receipt_id, branch=branch, business_date=business_date, revision=expected_revision + 1, kind=clean['kind'], refund_of=clean['refund_of'], payload_json=_json(clean), voided=0, void_reason='', created_at=row['created_at'] if row else timestamp, created_by=row['created_by'] if row else actor.username, updated_at=timestamp, updated_by=actor.username)
            after = self._receipt(stored)
            self._check_refunds(connection, branch, business_date, after)
            if row:
                self._exec(connection, 'UPDATE ledger_receipts SET revision=?,payload_json=?,updated_at=?,updated_by=? WHERE id=?', (stored['revision'], stored['payload_json'], timestamp, actor.username, receipt_id))
            else:
                self._insert(connection, 'ledger_receipts', stored)
            if expected_draft_revision is not None:
                self._exec(connection, 'DELETE FROM ledger_drafts WHERE id=? AND owner=? AND branch=? AND business_date=?', (receipt_id, actor.username, branch, business_date))
            return before, after, {'receipt': after}
        return self._mutate(principal, request_id, branch, business_date, 'save_receipt', {'id': receipt_id, 'revision': expected_revision, 'values': values, 'draft_revision': expected_draft_revision}, apply)

    def void_receipt(self, principal, request_id, receipt_id, expected_revision, branch, business_date, reason):
        _uuid(receipt_id)
        _integer(expected_revision, zero=False)
        reason = _reason(reason)
        def apply(connection, day, actor):
            row, before = self._get_receipt(connection, receipt_id, branch, business_date, expected_revision)
            if row is None or row['voided']:
                raise ValidationError('취소할 수납을 확인해 주세요.')
            after = before | {'revision': expected_revision + 1, 'voided': True, 'void_reason': reason, 'updated_at': _now(), 'updated_by': actor.username}
            self._check_refunds(connection, branch, business_date, after)
            self._exec(connection, 'UPDATE ledger_receipts SET revision=?,voided=1,void_reason=?,updated_at=?,updated_by=? WHERE id=?', (after['revision'], reason, after['updated_at'], actor.username, receipt_id))
            return before, after, {'receipt': after}
        return self._mutate(principal, request_id, branch, business_date, 'void_receipt', {'id': receipt_id, 'revision': expected_revision, 'reason': reason}, apply)

    def save_draft(self, principal, request_id, draft_id, expected_revision, branch, business_date, values):
        _uuid(draft_id)
        _integer(expected_revision)
        raw = _raw(values)
        def apply(connection, day, actor):
            row = self._one(connection, 'SELECT * FROM ledger_drafts WHERE id=? AND owner=?', (draft_id, actor.username))
            if row and (row['branch'], row['business_date']) != (branch, business_date):
                raise ConflictError('초안의 지점 또는 날짜가 다릅니다.')
            if (row['revision'] if row else 0) != expected_revision:
                raise ConflictError(current=self._draft(row) if row else None)
            stored = dict(id=draft_id, owner=actor.username, branch=branch, business_date=business_date, revision=expected_revision + 1, payload_json=_json(raw), updated_at=_now())
            if row:
                self._exec(connection, 'UPDATE ledger_drafts SET revision=?,payload_json=?,updated_at=? WHERE id=? AND owner=?', (stored['revision'], stored['payload_json'], stored['updated_at'], draft_id, actor.username))
            else:
                self._insert(connection, 'ledger_drafts', stored)
            after = self._draft(stored)
            return self._draft(row) if row else None, after, {'draft': after}
        return self._mutate(principal, request_id, branch, business_date, 'save_draft', {'id': draft_id, 'revision': expected_revision, 'values': raw}, apply)

    def list_drafts(self, principal, branch, business_date):
        principal = self._auth(principal)
        _scope(branch, business_date)
        with self._transaction() as connection:
            return [self._draft(row) for row in self._all(connection, 'SELECT * FROM ledger_drafts WHERE owner=? AND branch=? AND business_date=? ORDER BY updated_at,id', (principal.username, branch, business_date))]

    def delete_draft(self, principal, request_id, draft_id, expected_revision, branch, business_date):
        _uuid(draft_id)
        _integer(expected_revision, zero=False)
        def apply(connection, day, actor):
            row = self._one(connection, 'SELECT * FROM ledger_drafts WHERE id=? AND owner=? AND branch=? AND business_date=?', (draft_id, actor.username, branch, business_date))
            if not row or row['revision'] != expected_revision:
                raise ConflictError(current=self._draft(row) if row else None)
            self._exec(connection, 'DELETE FROM ledger_drafts WHERE id=? AND owner=?', (draft_id, actor.username))
            return self._draft(row), None, {'deleted': True}
        return self._mutate(principal, request_id, branch, business_date, 'delete_draft', {'id': draft_id, 'revision': expected_revision}, apply)

    def list_pending_drafts(self, principal, branch, business_date):
        self._auth(principal)
        _scope(branch, business_date)
        with self._transaction() as connection:
            return self._all(connection, 'SELECT id,owner,branch,business_date,revision,updated_at FROM ledger_drafts WHERE branch=? AND business_date=? ORDER BY updated_at,id,owner', (branch, business_date))

    def discard_pending_draft(self, principal, request_id, draft_id, expected_revision, branch, business_date, owner, reason):
        _uuid(draft_id)
        _integer(expected_revision, zero=False)
        owner, reason = _text(owner, 80), _reason(reason)
        if not owner:
            raise ValidationError('초안 소유자를 확인해 주세요.')
        def apply(connection, day, actor):
            row = self._one(connection, 'SELECT id,owner,branch,business_date,revision,updated_at FROM ledger_drafts WHERE id=? AND owner=? AND branch=? AND business_date=?', (draft_id, owner, branch, business_date))
            if not row or row['revision'] != expected_revision:
                raise ConflictError(current=row)
            self._exec(connection, 'DELETE FROM ledger_drafts WHERE id=? AND owner=?', (draft_id, owner))
            return row, {'deleted': True, 'id': draft_id, 'owner': owner, 'reason': reason}, {'deleted': True}
        return self._mutate(principal, request_id, branch, business_date, 'discard_pending_draft', {'id': draft_id, 'revision': expected_revision, 'owner': owner, 'reason': reason}, apply)

    def close_day(self, principal, request_id, branch, business_date, expected_day_revision, source_manifest=None, analysis_summary=None):
        _integer(expected_day_revision)
        source_manifest, analysis_summary = _raw(source_manifest or {}), _raw(analysis_summary or {})
        def apply(connection, day, actor):
            if day['revision'] != expected_day_revision:
                raise ConflictError(current=self._view(connection, branch, business_date))
            if self._one(connection, 'SELECT id FROM ledger_drafts WHERE branch=? AND business_date=? LIMIT 1', (branch, business_date)):
                raise ValidationError('미완료 초안이 있습니다. 저장하거나 초안을 삭제한 뒤 마감해 주세요.')
            before = dict(day)
            day['state'], day['close_sequence'] = 'closed', day['close_sequence'] + 1
            view = self._view(connection, branch, business_date)
            view.update(state='closed', revision=day['revision'] + 1, close_sequence=day['close_sequence'])
            snapshot = {'schema_version': 1, 'rules_version': 'receipt-ledger-v1', **view, 'source_manifest': source_manifest, 'analysis_summary': analysis_summary}
            saved = dict(id=str(uuid4()), branch=branch, business_date=business_date, close_sequence=day['close_sequence'], day_revision=day['revision'] + 1, payload_json=_json(snapshot), sha256=_digest(snapshot), closed_at=_now(), closed_by=actor.username)
            self._insert(connection, 'ledger_snapshots', saved)
            after = self._snapshot(saved)
            return before, after, lambda connection, current_day: after
        return self._mutate(principal, request_id, branch, business_date, 'close_day', {'revision': expected_day_revision, 'source_manifest': source_manifest, 'analysis_summary': analysis_summary}, apply)

    def reopen_day(self, principal, request_id, branch, business_date, expected_day_revision, reason):
        _integer(expected_day_revision)
        reason = _reason(reason)
        def apply(connection, day, actor):
            if day['revision'] != expected_day_revision:
                raise ConflictError(current=self._view(connection, branch, business_date))
            if day['state'] != 'closed':
                raise ValidationError('마감된 영업일만 재개방할 수 있습니다.')
            before = dict(day)
            day['state'] = 'open'
            return before, {'state': 'open', 'reason': reason}, lambda connection, current_day: self._view(connection, branch, business_date)
        return self._mutate(principal, request_id, branch, business_date, 'reopen_day', {'revision': expected_day_revision, 'reason': reason}, apply, allow_closed=True)

    @staticmethod
    def _snapshot(row):
        return {key: row[key] for key in TABLES['ledger_snapshots'] if key != 'payload_json'} | {'snapshot': json.loads(row['payload_json'])}

    def list_snapshots(self, principal, branch, business_date):
        self._auth(principal)
        _scope(branch, business_date)
        with self._transaction() as connection:
            return [self._snapshot(row) for row in self._all(connection, 'SELECT * FROM ledger_snapshots WHERE branch=? AND business_date=? ORDER BY close_sequence', (branch, business_date))]

    def list_events(self, principal, branch, business_date):
        self._auth(principal)
        _scope(branch, business_date)
        with self._transaction() as connection:
            rows = self._all(connection, 'SELECT * FROM ledger_events WHERE branch=? AND business_date=? ORDER BY day_revision', (branch, business_date))
            return [{key: value for key, value in row.items() if not key.endswith('_json')} | {'before': json.loads(row['before_json']), 'after': json.loads(row['after_json'])} for row in rows]

    def append_summary(self, principal, record, *, source_manifest=None):
        self._auth(principal)
        from reconciliation_history import _validate_record
        record = _validate_record(record)
        branch, business_date = _scope(record['branch'], record['settlement_date'])
        manifest = _summary_manifest(source_manifest, branch, business_date)
        with self._transaction(write=True) as connection:
            day = self._day(connection, branch, business_date, lock=True) if manifest is not None else None
            existing = self._one(connection, 'SELECT payload_json FROM analysis_summary WHERE run_id=?', (record['run_id'],))
            if existing:
                source = self._one(connection, 'SELECT manifest_json FROM analysis_summary_source WHERE run_id=?', (record['run_id'],))
                if existing['payload_json'] != _json(record) or (json.loads(source['manifest_json']) if source else None) != manifest:
                    raise ConflictError('같은 분석 번호의 내용 또는 원장 출처가 다릅니다.')
                return record
            if manifest is not None:
                if manifest['revision'] != day['revision']:
                    raise ConflictError('분석 후 원장이 변경되었습니다. 다시 분석해 주세요.', current=self._view(connection, branch, business_date))
                if self._one(connection, 'SELECT id FROM ledger_drafts WHERE branch=? AND business_date=? LIMIT 1', (branch, business_date)):
                    raise ValidationError('미완료 초안이 있어 분석 요약을 저장할 수 없습니다.')
            inserted = self._exec(connection, 'INSERT INTO analysis_summary (run_id,branch,business_date,payload_json) VALUES (?,?,?,?) ON CONFLICT (run_id) DO NOTHING', (record['run_id'], branch, business_date, _json(record))).rowcount
            if inserted and manifest is not None:
                self._insert(connection, 'analysis_summary_source', dict(run_id=record['run_id'], branch=branch, business_date=business_date, manifest_json=_json(manifest)))
            row = self._one(connection, 'SELECT payload_json FROM analysis_summary WHERE run_id=?', (record['run_id'],))
            source = self._one(connection, 'SELECT manifest_json FROM analysis_summary_source WHERE run_id=?', (record['run_id'],))
            if row['payload_json'] != _json(record) or (json.loads(source['manifest_json']) if source else None) != manifest:
                raise ConflictError('같은 분석 번호의 내용 또는 원장 출처가 다릅니다.')
        return record

    def get_summary_source(self, principal, run_id):
        self._auth(principal)
        _uuid(run_id)
        with self._transaction() as connection:
            row = self._one(connection, 'SELECT manifest_json FROM analysis_summary_source WHERE run_id=?', (run_id,))
            return json.loads(row['manifest_json']) if row else None

    def list_summaries(self, principal, branch, month):
        self._auth(principal)
        from reconciliation_history import project_month
        if not isinstance(month, str) or len(month) != 7:
            raise ValidationError('조회 월을 확인해 주세요.')
        _scope(branch, month + '-01')
        with self._transaction(snapshot=True) as connection:
            rows = self._all(connection, 'SELECT payload_json FROM analysis_summary WHERE branch=? AND business_date>=? AND business_date<? ORDER BY business_date,run_id', (branch, month + '-01', month + '-32'))
            return project_month([json.loads(row['payload_json']) for row in rows], month, branch)

    def export_backup(self, principal):
        self._auth(principal)
        with self._transaction(snapshot=True) as connection:
            tables = {table: self._all(connection, f"SELECT * FROM {table} ORDER BY {','.join(TABLES[table][:2])}") for table in TABLES}
            payload = {'schema_version': 1, 'exported_at': _now(), 'tables': tables}
        return {'payload': payload, 'sha256': _digest(payload)}

    @staticmethod
    def verify_backup(envelope):
        try:
            if not isinstance(envelope, dict) or set(envelope) != {'payload', 'sha256'}:
                raise ValueError
            payload = envelope['payload']
            if envelope['sha256'] != _digest(payload) or set(payload) != {'schema_version', 'exported_at', 'tables'} or payload['schema_version'] != 1 or set(payload['tables']) != set(TABLES):
                raise ValueError
            for table, columns in TABLES.items():
                if not isinstance(payload['tables'][table], list):
                    raise ValueError
                for row in payload['tables'][table]:
                    if not isinstance(row, dict) or set(row) != set(columns):
                        raise ValueError
                    _scope(row['branch'], row['business_date'])
                    for key in ('revision', 'day_revision', 'close_sequence'):
                        if key in row:
                            _integer(row[key])
                    for key in columns:
                        if key.endswith('_json'):
                            json.loads(row[key])
                    if table == 'ledger_receipts':
                        _uuid(row['id'])
                        _values(json.loads(row['payload_json']))
                        if row['voided'] not in (0, 1):
                            raise ValueError
                    elif table == 'ledger_snapshots':
                        if _digest(json.loads(row['payload_json'])) != row['sha256']:
                            raise ValueError
                    elif table == 'analysis_summary':
                        from reconciliation_history import _validate_record
                        record = _validate_record(json.loads(row['payload_json']))
                        if [row['run_id'], row['branch'], row['business_date']] != [record['run_id'], record['branch'], record['settlement_date']]:
                            raise ValueError
            LedgerStore._verify_relations(payload['tables'])
            return {'valid': True, 'schema_version': 1, 'sha256': envelope['sha256'], 'counts': {table: len(rows) for table, rows in payload['tables'].items()}}
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ValidationError('백업의 형식 또는 검사값이 올바르지 않습니다.') from None

    @staticmethod
    def _verify_relations(tables):
        # A checksum detects damaged bytes; these checks also reject invalid ledger state.
        days = {(row['branch'], row['business_date']): row for row in tables['ledger_days']}
        if len(days) != len(tables['ledger_days']):
            raise ValueError
        unique_columns = {'ledger_receipts': ('id',), 'ledger_drafts': ('id', 'owner'),
                          'ledger_events': ('request_id',), 'ledger_snapshots': ('id',),
                          'analysis_summary': ('run_id',), 'analysis_summary_source': ('run_id',)}
        for table, columns in unique_columns.items():
            rows = tables[table]
            if len({tuple(row[key] for key in columns) for row in rows}) != len(rows):
                raise ValueError
        events, snapshots = {}, {}
        receipts = {}
        for table in ('ledger_receipts', 'ledger_drafts', 'ledger_events', 'ledger_snapshots'):
            for row in tables[table]:
                key = (row['branch'], row['business_date'])
                if key not in days:
                    raise ValueError
                if table in ('ledger_receipts', 'ledger_drafts'):
                    _integer(row['revision'], zero=False)
                    _uuid(row['id'])
                if table == 'ledger_receipts':
                    clean = _values(json.loads(row['payload_json']))
                    if json.loads(row['payload_json']) != clean or (row['kind'], row['refund_of']) != (clean['kind'], clean['refund_of']) or type(row['voided']) is not int:
                        raise ValueError
                    if row['voided']:
                        _reason(row['void_reason'])
                    elif row['void_reason'] != '':
                        raise ValueError
                    receipts[row['id']] = LedgerStore._receipt(row)
                elif table == 'ledger_drafts':
                    _text(row['owner'], 80)
                    _raw(json.loads(row['payload_json']))
                elif table == 'ledger_events':
                    _uuid(row['event_id'])
                    _uuid(row['request_id'])
                    _integer(row['day_revision'], zero=False)
                    events.setdefault(key, []).append(row['day_revision'])
                elif table == 'ledger_snapshots':
                    _uuid(row['id'])
                    _integer(row['close_sequence'], zero=False)
                    content = json.loads(row['payload_json'])
                    if (content['branch'], content['business_date'], content['revision'], content['close_sequence'], content['state']) != (*key, row['day_revision'], row['close_sequence'], 'closed'):
                        raise ValueError
                    if row['day_revision'] > days[key]['revision']:
                        raise ValueError
                    snapshots.setdefault(key, []).append(row['close_sequence'])
        for key, day in days.items():
            revisions, sequences = sorted(events.get(key, [])), sorted(snapshots.get(key, []))
            if day['state'] not in ('open', 'closed') or len(revisions) != day['revision'] or len(sequences) != day['close_sequence']:
                raise ValueError
            if any(value != index for index, value in enumerate(revisions, 1)) or any(value != index for index, value in enumerate(sequences, 1)):
                raise ValueError
            if day['state'] == 'closed' and not day['close_sequence']:
                raise ValueError
        summaries = {row['run_id']: row for row in tables['analysis_summary']}
        for row in tables['analysis_summary_source']:
            manifest = _summary_manifest(json.loads(row['manifest_json']), row['branch'], row['business_date'])
            summary = summaries.get(row['run_id'])
            key = (row['branch'], row['business_date'])
            if summary is None or (summary['branch'], summary['business_date']) != key or key not in days or manifest['revision'] > days[key]['revision']:
                raise ValueError
        refunded = {}
        for item in receipts.values():
            if item['kind'] != 'refund' or item['voided']:
                continue
            original = receipts.get(item['refund_of'])
            if original is None or original['voided'] or original['kind'] != 'receipt' or original['branch'] != item['branch'] or original['business_date'] > item['business_date'] or _identity(original) != _identity(item):
                raise ValueError
            totals = refunded.setdefault(original['id'], dict.fromkeys(PAY_CHANNELS, 0))
            for channel, amount in item['payments'].items():
                totals[channel] += amount
                if totals[channel] > original['payments'][channel]:
                    raise ValueError

    def restore_backup(self, principal, envelope, target):
        self._auth(principal)
        self.verify_backup(envelope)
        if not isinstance(target, str) or not target or '://' in target or target == ':memory:':
            raise LedgerError('복원은 새로운 로컬 시험 파일에서만 가능합니다.')
        created = False
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created = True
            restored = LedgerStore(target, self._validate, local=True)
            with restored._transaction(write=True) as connection:
                for table, rows in envelope['payload']['tables'].items():
                    for row in rows:
                        restored._insert(connection, table, row)
            return restored
        except FileExistsError:
            raise LedgerError('복원 대상이 이미 있습니다. 새 파일 경로를 선택해 주세요.') from None
        except Exception:
            if created:
                for suffix in ('', '-wal', '-shm'):
                    Path(target + suffix).unlink(missing_ok=True)
            raise LedgerError('백업을 새 로컬 시험 파일에 복원하지 못했습니다.') from None
