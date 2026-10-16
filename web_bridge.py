"""Full-page web ledger transport inside the authenticated Streamlit development app.

Only API commands cross the component boundary. Editing, filtering and opening a
dialog stay in the browser; durable writes still use the existing LedgerStore.
"""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from branch_access import AccessError, CANONICAL_BRANCHES
from ledger_component import component_scope
from ledger_ui import dispatch_action, editor_rows, editor_summary, kst_today
from receipt_ledger import ConflictError, LedgerError, PAY_CHANNELS, ValidationError
from staging import require_preview


def session_payload(principal):
    return {
        'authenticated': principal is not None, 'csrf_token': '',
        'user': ({'username': principal.username, 'role': principal.role, 'branch': principal.branch}
                 if principal else None),
        'branches': list(CANONICAL_BRANCHES), 'channels': list(PAY_CHANNELS),
        'today': kst_today().isoformat(), 'transport': 'streamlit',
    }


def _wire(value):
    """Keep won amounts exact when JavaScript receives an existing store result."""
    if isinstance(value, list):
        return [_wire(item) for item in value]
    if isinstance(value, dict):
        return {key: ({channel: str(amount) for channel, amount in item.items()}
                      if key == 'payments' and isinstance(item, dict) else _wire(item))
                for key, item in value.items()}
    return value


def _scope(principal, branch, business_date):
    if (branch not in CANONICAL_BRANCHES or not isinstance(business_date, str)
            or date.fromisoformat(business_date).isoformat() != business_date):
        raise ValidationError('지점과 영업일을 확인해 주세요.')
    return component_scope(f'{principal.username}:{principal.revision}', branch, business_date)


def dispatch_request(store, principal, validate, request):
    """Authorize and dispatch one same-session component request; never accept a client principal."""
    response = {'request_id': request.get('request_id') if isinstance(request, dict) else None}
    try:
        if (not isinstance(request, dict) or not isinstance(request.get('request_id'), str)
                or str(UUID(request['request_id'])) != request['request_id']
                or len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) > 200_000):
            raise ValueError
        principal = require_preview(validate(principal))
        method = request.get('method', 'GET')
        url = urlsplit(request.get('path', ''))
        if method not in ('GET', 'POST', 'DELETE') or url.scheme or url.netloc or url.fragment:
            raise ValueError
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        query = {key: values[0] for key, values in query.items()}
        body = request.get('body') or {}
        if not isinstance(body, dict):
            raise ValueError
        path = url.path
        if path == '/api/session' and method == 'GET':
            data = session_payload(principal)
        elif path == '/api/session' and method == 'DELETE':
            data = session_payload(None)
            response['control'] = 'logout'
        elif path == '/api/navigation' and method == 'POST':
            target = body.get('target')
            if target not in ('table', 'analysis'):
                raise ValueError
            response['control'] = target
            data = {'ok': True}
        elif path == '/api/ledger/backup' and method == 'GET':
            # Keep the checksum-bearing envelope byte-for-byte equivalent to the store export.
            data = store.export_backup(principal)
        elif path == '/api/ledger/days' and method == 'GET':
            _scope(principal, query.get('branch'), query.get('start'))
            _scope(principal, query.get('branch'), query.get('end'))
            data = {'days': store.list_days(principal, query['branch'], query['start'], query['end'])}
        else:
            source = query if method == 'GET' else body
            branch, business_date = source.get('branch'), source.get('date')
            scope = _scope(principal, branch, business_date)
            if method != 'GET' and source.get('scope') != scope:
                raise ValidationError('지점·날짜 또는 로그인 정보가 바뀌었습니다. 입력 내용을 확인해 주세요.')
            if path == '/api/ledger' and method == 'GET':
                workspace = store.load_workspace(principal, branch, business_date)
                day = workspace['day']
                data = {'day': _wire(day), 'rows': editor_rows(day, workspace['drafts']),
                        'summary': editor_summary(day), 'scope': scope,
                        'pending_count': day.get('pending_draft_count', 0)}
            elif path == '/api/ledger/actions' and method == 'POST':
                action = body.get('action')
                if not isinstance(action, dict):
                    raise ValueError
                data = {'ack': dispatch_action(store, principal, branch, business_date, action, scope)}
            elif path == '/api/ledger/events' and method == 'GET':
                data = {'events': _wire(store.list_events(principal, branch, business_date))}
            elif path == '/api/ledger/snapshots' and method == 'GET':
                snapshots = store.list_snapshots(principal, branch, business_date)
                data = {'snapshots': [{**_wire(item), 'download_json': json.dumps(
                    item, ensure_ascii=False, allow_nan=False)} for item in snapshots]}
            elif path == '/api/ledger/close' and method == 'POST':
                data = {'result': _wire(store.close_day(principal, body['request_id'], branch,
                                                       business_date, body['expected_revision']))}
            elif path == '/api/ledger/reopen' and method == 'POST':
                data = {'result': _wire(store.reopen_day(principal, body['request_id'], branch,
                                                        business_date, body['expected_revision'], body['reason']))}
            else:
                raise ValidationError('지원하지 않는 요청입니다.')
        return {**response, 'status': 200, 'data': data}
    except ConflictError as exc:
        status, code, message, current = 409, 'conflict', str(exc), _wire(deepcopy(exc.current))
    except AccessError as exc:
        status, code, message, current = 403, 'forbidden', str(exc), None
    except LedgerError as exc:
        status, code, message, current = 400, 'validation', str(exc), None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        status, code, message, current = 400, 'validation', '요청 형식을 확인해 주세요.', None
    except Exception:
        status, code, message, current = 503, 'unavailable', '서버 응답을 확인하지 못했습니다. 입력을 유지하고 다시 시도해 주세요.', None
    error = {'code': code, 'message': message}
    if current is not None:
        error['current'] = current
    return {**response, 'status': status, 'data': {'error': error}}


def render_full_web(principal, store, validate):
    import streamlit as st
    import streamlit.components.v1 as components
    import app

    identity = component_scope(f'{principal.username}:{principal.revision}', '', '')
    widget_key, reply_key = f'full_web_{identity}', f'_full_web_reply_{identity}'
    component = components.declare_component('clinic_full_web_ledger', path=str(Path(__file__).parent / 'web_static'))

    @st.fragment
    def web_component():
        request = st.session_state.get(widget_key)
        previous = st.session_state.get(reply_key)
        if (isinstance(request, dict) and request.get('request_id')
                and (previous or {}).get('request_id') != request['request_id']):
            response = dispatch_request(store, principal, validate, request)
            control = response.pop('control', None)
            st.session_state[reply_key] = response
            if control == 'logout':
                app._reset_workspace(logout=True)
                st.rerun(scope='app')
            elif control in ('table', 'analysis'):
                st.query_params['ledger_view'] = 'table'
                st.session_state['workspace_view'] = '수납원장' if control == 'table' else '마감 대조'
                st.rerun(scope='app')
            elif response['status'] == 403:
                app._reset_workspace(logout=True)
                st.session_state['_auth_error'] = response['data']['error']['message']
                st.rerun(scope='app')
        component(identity=identity, session=session_payload(principal),
                  response=st.session_state.get(reply_key), height=1000,
                  key=widget_key, default=None)

    web_component()


def main():
    """Reuse the development app login and secrets without starting another server port."""
    import streamlit as st
    import app

    st.session_state['_development_mode'] = True
    st.set_page_config(page_title='clinic pay · 수납원장 B', layout='wide', initial_sidebar_state='collapsed')
    if not app.require_app_access():
        return
    principal = st.session_state['_auth_principal']
    try:
        store = app._get_development_store()
        if store is None:
            st.warning('수납원장 데이터베이스가 연결되지 않았습니다. 전용 DATABASE_URL을 확인해 주세요.')
            return
        st.markdown('''<style>
        [data-testid="stHeader"], [data-testid="stSidebar"], [data-testid="stStatusWidget"] {display:none!important}
        [data-testid="stMainBlockContainer"] {padding:0!important;margin:0!important;max-width:none!important}
        [data-testid="stMain"] {overflow:hidden}
        [data-testid="stVerticalBlock"] {gap:0!important}
        iframe.stCustomComponentV1 {width:100%;height:100dvh!important;display:block;border:0}
        </style>''', unsafe_allow_html=True)
        render_full_web(principal, store, app._validated_ledger_principal)
    except AccessError as error:
        app._reset_workspace(logout=True)
        st.error(str(error))
    except (ValueError, LedgerError) as error:
        st.error(str(error))
