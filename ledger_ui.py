"""Receipt entry, review history and a strict adapter to the existing reconciliation engine."""
from copy import deepcopy
from datetime import datetime
import json
import re
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import pandas as pd

from branch_access import AccessError, CANONICAL_BRANCHES
from ledger_component import component_scope, ledger_editor, checkpoint_pending
from receipt_ledger import PAY_CHANNELS, MAX_MONEY, LedgerError, ValidationError, ConflictError


class DraftIncomplete(ValueError):
    """Raw text remains a draft until it can represent an integer-won receipt."""


def kst_today(now=None):
    return (now or datetime.now(ZoneInfo('Asia/Seoul'))).astimezone(ZoneInfo('Asia/Seoul')).date()


def parse_won(raw):
    if type(raw) is int and 0 <= raw <= MAX_MONEY:
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text == '':
            return 0
        if re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)', text):
            value = int(text.replace(',', ''))
            if value <= MAX_MONEY:
                return value
    raise DraftIncomplete('금액은 0 이상의 정수 원으로 입력하세요. 잘못된 값은 초안에 보관합니다.')


def normalize_raw(raw):
    if not isinstance(raw, dict):
        raise DraftIncomplete('행 입력 형식을 확인하세요.')
    payments = raw.get('payments', {})
    if not isinstance(payments, dict) or set(payments) - set(PAY_CHANNELS):
        raise DraftIncomplete('결제수단을 확인하세요.')
    result = {key: raw.get(key, '') for key in ('chart_no', 'patient_name', 'note')}
    if not all(isinstance(value, str) for value in result.values()):
        raise DraftIncomplete('차트번호·성명·메모는 문자로 입력하세요.')
    if not result['chart_no'].strip() or not result['patient_name'].strip():
        raise DraftIncomplete('차트번호와 성명을 입력하면 정식 수납으로 저장합니다.')
    result['payments'] = {channel: parse_won(payments.get(channel, '')) for channel in PAY_CHANNELS}
    if not any(result['payments'].values()):
        raise DraftIncomplete('결제 금액이 없는 행은 초안으로 보관합니다.')
    if sum(result['payments'].values()) > MAX_MONEY:
        raise DraftIncomplete('합계 금액이 너무 큽니다.')
    for key in ('yeoshin_used', 'gangnamunni_used'):
        result[key] = raw.get(key, False)
        if type(result[key]) is not bool:
            raise DraftIncomplete('사용 표시는 체크박스로 입력하세요.')
    result['kind'] = raw.get('kind', 'receipt')
    result['refund_of'] = raw.get('refund_of') or None
    if result['kind'] not in ('receipt', 'refund'):
        raise DraftIncomplete('수납 또는 연결 환불을 선택하세요.')
    if result['kind'] == 'refund' and not result['refund_of']:
        raise DraftIncomplete('환불할 원거래를 선택하세요.')
    return result


def _raw_receipt(receipt):
    raw = {key: deepcopy(receipt.get(key, default)) for key, default in (
        ('chart_no', ''), ('patient_name', ''), ('note', ''), ('payments', {}),
        ('yeoshin_used', False), ('gangnamunni_used', False), ('kind', 'receipt'), ('refund_of', None))}
    raw['payments'] = {key: str(value) for key, value in raw['payments'].items()}
    return raw


def editor_rows(day, drafts):
    rows = {receipt['id']: {'id': receipt['id'], 'raw': _raw_receipt(receipt),
                           'receipt_revision': receipt['revision'], 'draft_revision': 0,
                           'voided': receipt.get('voided', False), 'updated_by': receipt.get('updated_by', '')}
            for receipt in day['receipts']}
    for draft in drafts:
        values = deepcopy(draft['values'])
        base_revision = values.pop('_receipt_revision', rows.get(draft['id'], {}).get('receipt_revision', 0))
        row = rows.setdefault(draft['id'], {'id': draft['id'], 'voided': False})
        row.update(raw=values, receipt_revision=base_revision, draft_revision=draft['revision'])
    return list(rows.values())


def ledger_to_daily(day):
    """Return (daily, refunds), positive refund amounts, matching app.parse_daily columns.

    Only durable, non-void receipts enter analysis. Drafts never enter this adapter.
    Usage checkboxes are metadata and do not produce payment amounts.
    """
    columns = ['차트번호', '성명', '날짜', '내원순서', *PAY_CHANNELS, '플랫폼합', '총액',
               '여신사용', '강언사용', '메모', 'ledger_receipt_id', 'd_idx']
    groups = {'receipt': [], 'refund': []}
    for receipt in day['receipts']:
        if receipt.get('voided'):
            continue
        payments = receipt.get('payments', {})
        if not isinstance(payments, dict) or set(payments) - set(PAY_CHANNELS):
            raise ValueError('원장 결제수단 형식이 잘못되었습니다.')
        amounts = {channel: payments.get(channel, 0) for channel in PAY_CHANNELS}
        if any(type(value) is not int or not 0 <= value <= MAX_MONEY for value in amounts.values()):
            raise ValueError('원장 금액은 정수 원이어야 합니다.')
        kind = receipt.get('kind', 'receipt')
        if kind not in groups:
            raise ValueError('원장 거래 구분이 잘못되었습니다.')
        index = len(groups[kind])
        groups[kind].append({'차트번호': receipt['chart_no'], '성명': receipt['patient_name'],
                             '날짜': day['business_date'], '내원순서': index + 1, **amounts,
                             '플랫폼합': sum(amounts[channel] for channel in PAY_CHANNELS[3:]),
                             '총액': sum(amounts.values()), '여신사용': receipt.get('yeoshin_used', False),
                             '강언사용': receipt.get('gangnamunni_used', False), '메모': receipt.get('note', ''),
                             'ledger_receipt_id': receipt['id'], 'd_idx': index})
    frames = tuple(pd.DataFrame(groups[kind], columns=columns) for kind in ('receipt', 'refund'))
    for frame in frames:
        frame.attrs.update(ledger_revision=day['revision'], ledger_business_date=day['business_date'],
                           ledger_branch=day.get('branch'))
    return frames


def dispatch_action(store, principal, branch, business_date, action, scope):
    """Bind browser commands to the server-selected scope and retain invalid input as raw drafts."""
    ack = {'id': action.get('id'), 'request_id': action.get('request_id'), 'attempt': action.get('attempt', 0)}
    try:
        if action.get('scope') != scope:
            raise ValidationError('날짜 또는 지점이 바뀌었습니다. 원래 화면에서 저장 상태를 확인하세요.')
        request_id, receipt_id = action['request_id'], action['id']
        if str(UUID(request_id)) != request_id or str(UUID(receipt_id)) != receipt_id:
            raise ValueError
        revision, draft_revision = action.get('receipt_revision'), action.get('draft_revision')
        if any(type(value) is not int or value < 0 for value in (revision, draft_revision)):
            raise ValueError
        raw = action.get('raw')
        if not isinstance(raw, dict):
            raise ValueError
        operation = action.get('action')
        if operation == 'discard':
            if draft_revision:
                store.delete_draft(principal, request_id, receipt_id, draft_revision, branch, business_date)
            return dict(ack, status='discarded', receipt_revision=revision, draft_revision=0)
        if operation == 'void':
            saved = store.void_receipt(principal, request_id, receipt_id, revision, branch,
                                       business_date, raw.get('_void_reason', ''))
            return dict(ack, status='saved', receipt_revision=saved['receipt']['revision'], draft_revision=0,
                        voided=saved['receipt'].get('voided', False))
        if operation != 'save':
            raise ValueError
        try:
            values = normalize_raw(raw)
            saved = store.save_receipt(principal, request_id, receipt_id, revision, branch, business_date, values,
                                       expected_draft_revision=draft_revision)
            return dict(ack, status='saved', receipt_revision=saved['receipt']['revision'], draft_revision=0,
                        voided=saved['receipt'].get('voided', False))
        except (DraftIncomplete, ValidationError) as exc:
            # The request UUID for the fallback is deterministic, including on response loss/retry.
            draft_request = str(uuid5(UUID(request_id), 'raw-draft'))
            draft = store.save_draft(principal, draft_request, receipt_id, draft_revision,
                                     branch, business_date, {**raw, '_receipt_revision': revision})['draft']
            return dict(ack, status='draft', receipt_revision=revision, draft_revision=draft['revision'], message=str(exc))
    except ConflictError as exc:
        current = deepcopy(exc.current)
        if isinstance(current, dict) and 'payments' in current:
            current['payments'] = {key: str(value) for key, value in current['payments'].items()}
        return dict(ack, status='conflict', current=current, message=str(exc))
    except (LedgerError, AccessError) as exc:
        return dict(ack, status='error', message=str(exc))
    except (ValueError, TypeError, KeyError, AttributeError):
        return dict(ack, status='error', message='저장 요청 형식이 올바르지 않습니다. 내 입력을 보존했습니다.')
    except Exception:
        return dict(ack, status='error', message='서버 저장을 확인하지 못했습니다. 내 입력을 유지하고 다시 시도하세요.')


def _selection(day):
    daily, refunds = ledger_to_daily(day)
    return {key: day[key] for key in ('branch', 'business_date', 'revision', 'state')} | {
        'daily': daily, 'daily_refund': refunds, 'day': deepcopy(day)}


def render_ledger(principal, store, validate, on_analyze=None):
    """Render admin ledger; build an analysis selection only on explicit request."""
    import streamlit as st
    principal = validate(principal)
    if principal.role != 'admin' or principal.branch != 'all':
        raise AccessError('총괄 관리자만 개발 원장에 접근할 수 있습니다.')
    st.subheader('수납 원장 · 개발본')
    st.caption('서버 저장 응답을 확인한 행만 저장완료로 표시합니다. 공동 접속자의 인원·상태는 추정해 표시하지 않습니다.')
    left, right = st.columns(2)
    branch = left.selectbox('원장 지점', CANONICAL_BRANCHES, key='ledger_branch')
    picked_date = right.date_input('KST 영업일', value=kst_today(), key='ledger_business_date')
    business_date = picked_date.isoformat()
    identity = component_scope(f'{principal.username}:{principal.revision}', '', '')
    scope = component_scope(f'{principal.username}:{principal.revision}', branch, business_date)
    st.session_state.setdefault(f'_ledger_scopes_{identity}', {})[scope] = (branch, business_date)
    ack_key = f'_ledger_ack_{scope}'
    processed_key = f'_ledger_processed_{scope}'

    @st.fragment(run_every='5s')
    def live_editor():
        current = principal
        workspace_key = f'_ledger_workspace_{scope}'
        action = st.session_state.get(f'ledger_editor_{identity}')
        if isinstance(action, dict) and action.get('kind') != 'checkpoint' and action.get('scope') == scope:
            # Deliver the ACK in this render, without rerunning the whole authenticated app.
            fingerprint = json.dumps({key: value for key, value in action.items() if key != 'checkpoint'}, sort_keys=True, ensure_ascii=False)
            processed = st.session_state.setdefault(processed_key, {})
            if fingerprint not in processed:
                ack = dispatch_action(store, current, branch, business_date, action, scope)
                processed[fingerprint] = ack
                if len(processed) > 100:
                    processed.pop(next(iter(processed)))
                st.session_state[ack_key] = ack
                # A failed refresh must not later pair the ACK with a pre-save workspace.
                st.session_state.pop(workspace_key, None)
        try:
            checkpoint_event = st.session_state.pop(f'_ledger_checkpoint_event_{identity}', False)
            if checkpoint_event and workspace_key in st.session_state:
                workspace = st.session_state[workspace_key]
            else:
                workspace = store.load_workspace(current, branch, business_date)
                st.session_state[workspace_key] = workspace
            day, drafts = workspace['day'], workspace['drafts']
        except AccessError as exc:
            st.error(str(exc))
            return
        except LedgerError as exc:
            st.error(str(exc))
            return
        except Exception:
            st.error('원장을 불러오지 못했습니다. 서버 연결을 확인하고 다시 시도하세요.')
            return
        state_label = '마감' if day['state'] == 'closed' else '입력 가능'
        st.caption(f"{branch} · {business_date} · 일자 개정 {day['revision']} · {state_label} · 다른 작업자의 변경은 약 5초 간격으로 확인")
        pending_count = day.get('pending_draft_count', len(drafts))
        if pending_count:
            st.warning(f'미완료 초안 전체 {pending_count}행 · 내 초안 {len(drafts)}행. 미완료 초안이 있으면 대조 분석과 마감을 시작할 수 없습니다.')
        refund_options = _render_refund_picker(current, store, branch, business_date, scope, day['state'] == 'closed')
        ledger_editor(identity=identity, scope=scope, rows=editor_rows(day, drafts), channels=PAY_CHANNELS,
                      ack=st.session_state.get(ack_key), closed=day['state'] == 'closed', refund_options=refund_options)
        local_pending = checkpoint_pending(st.session_state.get(f'_ledger_client_{identity}'))
        if local_pending:
            st.warning('이 세션에 서버 저장을 확인하지 못한 입력이 있습니다. 날짜·지점을 바꾸어도 보관되며, 돌아오면 입력과 재시도를 복구합니다. 분석·마감 전 모든 대기·실패·충돌을 해결하세요.')
        if st.button('이 원장을 대조 분석에 사용', key=f'ledger_analyze_{scope}', disabled=bool(pending_count) or local_pending or day['revision'] == 0):
            latest = store.load_day(current, branch, business_date)
            if latest.get('pending_draft_count', 0) or latest['revision'] == 0:
                st.warning('원장이 바뀌었거나 미완료 초안이 있습니다. 저장 상태를 확인하세요.')
                return
            selection = _selection(latest)
            st.session_state['_ledger_selection'] = selection
            if on_analyze:
                on_analyze(selection)
            st.rerun(scope='app')
        with st.expander('수납원장 마감 · 재개방'):
            st.caption('수납원장 마감은 수납 내역을 수동 검토한 시점의 스냅샷입니다. 대조 분석 완료 여부와는 별도이며 자동 검증 완료를 뜻하지 않습니다. 현재 개정과 모든 미완료 초안을 서버에서 다시 확인합니다.')
            _render_draft_cleanup(current, store, branch, business_date, scope, local_pending)
            if day['state'] == 'open':
                checked = st.checkbox('수납원장 내용을 수동 검토했습니다', key=f'ledger_review_{scope}')
                if st.button('현재 개정으로 마감', disabled=not checked or bool(pending_count) or local_pending, key=f'ledger_close_{scope}'):
                    try:
                        store.close_day(current, str(uuid4()), branch, business_date, day['revision'])
                        st.success('마감 스냅샷을 저장했습니다.')
                        st.rerun()
                    except (LedgerError, AccessError) as exc:
                        st.error(str(exc))
                    except Exception:
                        st.error('마감 응답을 확인하지 못했습니다. 새로고침 후 마감 이력을 확인하세요.')
            else:
                reason = st.text_input('재개방 사유', key=f'ledger_reopen_reason_{scope}')
                if st.button('사유를 남기고 재개방', disabled=not reason.strip(), key=f'ledger_reopen_{scope}'):
                    try:
                        store.reopen_day(current, str(uuid4()), branch, business_date, day['revision'], reason)
                        st.rerun()
                    except (LedgerError, AccessError) as exc:
                        st.error(str(exc))
                    except Exception:
                        st.error('재개방 응답을 확인하지 못했습니다. 이력을 확인하세요.')
    live_editor()
    _render_history(principal, store, branch, business_date, scope)
    _render_backup(principal, store, scope)


def _render_draft_cleanup(principal, store, branch, business_date, scope, local_pending):
    import streamlit as st
    key = f'_ledger_pending_meta_{scope}'
    if st.button('전체 미완료 초안 목록 확인', key=f'ledger_pending_lookup_{scope}'):
        try:
            st.session_state[key] = store.list_pending_drafts(principal, branch, business_date)
        except (LedgerError, AccessError) as exc:
            st.error(str(exc))
    metadata = st.session_state.get(key, [])
    if metadata:
        st.caption('소유자·개정·서버 수정 시각만 표시합니다. 다른 사용자의 초안 입력 내용은 이 목록에 표시하지 않습니다.')
        st.dataframe(pd.DataFrame(metadata), hide_index=True, use_container_width=True)
        choices = {f"{row['owner']} · 개정 {row['revision']} · {row['updated_at']} · {row['id']}": row for row in metadata}
        choice = st.selectbox('정리할 미완료 초안', list(choices), key=f'ledger_pending_choice_{scope}')
        reason = st.text_input('미완료 초안 삭제 사유', key=f'ledger_pending_reason_{scope}')
        if st.button('사유를 남기고 미완료 초안 삭제', disabled=local_pending or not reason.strip(), key=f'ledger_pending_discard_{scope}'):
            row = choices[choice]
            try:
                store.discard_pending_draft(principal, str(uuid4()), row['id'], row['revision'],
                                            branch, business_date, row['owner'], reason)
                st.session_state.pop(key, None)
                st.rerun()
            except (LedgerError, AccessError) as exc:
                st.error(str(exc))
            except Exception:
                st.error('초안 삭제 응답을 확인하지 못했습니다. 목록을 다시 조회하세요.')
    elif key in st.session_state:
        st.info('미완료 초안이 없습니다.')


def _render_refund_picker(principal, store, branch, business_date, scope, closed):
    import streamlit as st
    key = f'_ledger_refund_originals_{scope}'
    with st.expander('이전 날짜 수납을 연결해 환불 입력'):
        original_date = st.date_input('원수납 영업일', value=kst_today(), key=f'ledger_refund_date_{scope}')
        if st.button('원수납 조회', key=f'ledger_refund_lookup_{scope}'):
            try:
                original_day = store.load_day(principal, branch, original_date.isoformat())
                st.session_state[key] = [row for row in original_day['receipts']
                                         if row['kind'] == 'receipt' and not row['voided']]
            except (LedgerError, AccessError) as exc:
                st.error(str(exc))
            except Exception:
                st.error('원수납을 불러오지 못했습니다. 다시 조회하세요.')
        originals = st.session_state.get(key, [])
        if originals:
            by_id = {row['id']: row for row in originals}
            selected = st.selectbox('환불 원수납', list(by_id),
                                    format_func=lambda identifier: f"{by_id[identifier]['business_date']} · {by_id[identifier]['chart_no']} · {by_id[identifier]['patient_name']}",
                                    key=f'ledger_refund_original_{scope}')
            if st.button('선택 원수납의 환불 초안 만들기', disabled=closed, key=f'ledger_refund_create_{scope}'):
                original = by_id[selected]
                raw = _raw_receipt(original)
                raw.update(kind='refund', refund_of=selected, payments={channel: '' for channel in PAY_CHANNELS}, _receipt_revision=0)
                try:
                    store.save_draft(principal, str(uuid4()), str(uuid4()), 0, branch, business_date, raw)
                    st.rerun()
                except (LedgerError, AccessError) as exc:
                    st.error(str(exc))
                except Exception:
                    st.error('환불 초안 저장을 확인하지 못했습니다. 원장 목록을 먼저 확인하세요.')
        elif key in st.session_state:
            st.info('선택 날짜에 환불로 연결할 수납이 없습니다.')
    return [{'id': row['id'], 'chart_no': row['chart_no'], 'patient_name': row['patient_name'], 'label': f"{row['business_date']} {row['chart_no']} {row['patient_name']}"} for row in originals]


def _render_history(principal, store, branch, business_date, scope):
    import streamlit as st
    with st.expander('날짜별 이력 · 변경 내역 · 마감 비교'):
        dates = st.date_input('조회 기간', value=(kst_today().replace(day=1), kst_today()), key=f'ledger_range_{scope}')
        if st.button('이력 조회', key=f'ledger_history_{scope}'):
            try:
                if len(dates) != 2:
                    st.warning('시작일과 종료일을 선택하세요.')
                    return
                days = store.list_days(principal, branch, dates[0].isoformat(), dates[1].isoformat())
                snapshots = store.list_snapshots(principal, branch, business_date)
                events = store.list_events(principal, branch, business_date)
                st.session_state[f'_ledger_history_{scope}'] = (days, snapshots, events)
            except (LedgerError, AccessError) as exc:
                st.error(str(exc))
            except Exception:
                st.error('이력을 불러오지 못했습니다. 다시 조회하세요.')
        history = st.session_state.get(f'_ledger_history_{scope}')
        if history:
            days, snapshots, events = history
            st.caption('기록이 없는 날짜는 0건 확정으로 표시하지 않습니다. 아래 변경·스냅샷은 선택한 영업일 기준입니다.')
            st.dataframe(pd.DataFrame(days), hide_index=True, use_container_width=True)
            st.caption(f'{business_date} 변경 {len(events)}건 · 마감 스냅샷 {len(snapshots)}개')
            if events:
                st.dataframe(pd.DataFrame([{'개정': e['day_revision'], '변경자': e['actor'], '작업': e['operation'],
                                           '서버 시각(UTC)': e['occurred_at'],
                                           '변경 전': json.dumps(e['before'], ensure_ascii=False),
                                           '변경 후': json.dumps(e['after'], ensure_ascii=False)} for e in events]),
                             hide_index=True, use_container_width=True)
            if snapshots:
                summaries = []
                previous = None
                for snapshot in snapshots:
                    daily, refunds = ledger_to_daily(snapshot['snapshot'])
                    net = int(daily['총액'].sum() - refunds['총액'].sum())
                    summaries.append({'마감 회차': snapshot['close_sequence'], '일자 개정': snapshot['day_revision'],
                                      '마감자': snapshot['closed_by'], '순수납': net,
                                      '이전 마감 차이': net - previous if previous is not None else None,
                                      'SHA256': snapshot['sha256']})
                    previous = net
                st.dataframe(pd.DataFrame(summaries), hide_index=True, use_container_width=True)
                for snapshot in snapshots:
                    st.download_button(f"마감 {snapshot['close_sequence']} 원본 JSON", json.dumps(snapshot, ensure_ascii=False, indent=2),
                                       file_name=f"ledger-snapshot-{business_date}-{snapshot['close_sequence']}.json",
                                       mime='application/json', key=f"ledger_snapshot_{scope}_{snapshot['id']}")


def _render_backup(principal, store, scope):
    import streamlit as st
    with st.expander('전체 원장 백업 내보내기'):
        st.caption('모든 지점의 원장·이력·초안을 포함하는 일관된 JSON과 SHA256을 생성합니다. 독립 원격 자동 백업은 별도 연결이 필요합니다.')
        key = f'_ledger_backup_{scope}'
        if st.button('검증된 백업 생성', key=f'ledger_backup_prepare_{scope}'):
            try:
                backup = store.export_backup(principal)
                store.verify_backup(backup)
                st.session_state[key] = json.dumps(backup, ensure_ascii=False, indent=2)
            except (LedgerError, AccessError) as exc:
                st.error(str(exc))
            except Exception:
                st.error('백업 생성 또는 검증을 완료하지 못했습니다. 다시 시도하세요.')
        if key in st.session_state:
            st.download_button('전체 백업 JSON 다운로드', st.session_state[key],
                               file_name=f'clinic-pay-ledger-backup-{kst_today().isoformat()}.json',
                               mime='application/json', key=f'ledger_backup_download_{scope}')
