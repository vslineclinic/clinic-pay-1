"""Same-origin B web API. Run ``uvicorn web_server:create_app --factory --workers 1``.

PUBLIC_ORIGIN, DATABASE_URL, HISTORY_SHEET_ID and HISTORY_SERVICE_ACCOUNT_JSON
are required deployment settings. Sessions and login limits are process-local:
use one worker; restart requires login again. Ledger data stays in PostgreSQL.
No patient input, passwords or credentials are logged by this module.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from branch_access import AccountStore, AccessError, CANONICAL_BRANCHES, LoginThrottle, Principal
from ledger_component import component_scope
from ledger_ui import dispatch_action, editor_rows, editor_summary, kst_today
from receipt_ledger import ClosedDayError, ConflictError, LedgerError, LedgerStore, PAY_CHANNELS, ValidationError
from reconciliation_history import HistoryStore
from staging import storage_config

COOKIE = '__Host-clinicpay-session'
MAX_BODY = 64 * 1024
SESSION_SECONDS = 8 * 60 * 60
_request_validation = ContextVar('web_account_validation', default=None)
_request_timing = ContextVar('web_request_timing', default=None)


class APIError(Exception):
    def __init__(self, status, code, message, current=None):
        self.status, self.code, self.message, self.current = status, code, message, current


def _error(status, code, message, current=None):
    error = {'code': code, 'message': message}
    if current is not None:
        error['current'] = _money_strings(current)
    return JSONResponse({'error': error}, status_code=status)


def _money_strings(value):
    """Only monetary payloads become strings; backup exports retain their checksum."""
    if isinstance(value, list):
        return [_money_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: ({channel: str(amount) if type(amount) is int else amount
                       for channel, amount in item.items()}
                      if key == 'payments' and isinstance(item, dict) else _money_strings(item))
                for key, item in value.items()}
    return value


@dataclass(frozen=True)
class Session:
    principal: Principal | None
    csrf_token: str
    expires_at: float


class Sessions:
    """Bounded opaque sessions. Anonymous sessions cannot evict signed-in users."""
    def __init__(self, clock=time.monotonic, max_sessions=512):
        self.clock, self.max_sessions = clock, max_sessions
        self.max_anonymous = min(64, max_sessions)
        self._items, self._lock = {}, threading.Lock()

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and len(token) <= 128 else ''

    def get(self, token):
        with self._lock:
            key = self._key(token)
            session = self._items.get(key)
            if session is not None and session.expires_at <= self.clock():
                self._items.pop(key, None)
                return None
            return session

    def replace(self, old_token=None, principal=None):
        with self._lock:
            now = self.clock()
            self._items = {key: value for key, value in self._items.items() if value.expires_at > now}
            old_key = self._key(old_token)
            anonymous = [key for key, value in self._items.items() if value.principal is None and key != old_key]
            remaining = len(self._items) - (old_key in self._items)
            if remaining >= self.max_sessions or (principal is None and len(anonymous) >= self.max_anonymous):
                if not anonymous:
                    raise APIError(503, 'session_capacity', '접속이 많습니다. 잠시 후 다시 시도해 주세요.')
                # Anonymous bootstraps cannot occupy every login slot for 15 minutes.
                self._items.pop(min(anonymous, key=lambda key: self._items[key].expires_at))
            self._items.pop(old_key, None)
            token = secrets.token_urlsafe(32)
            session = Session(principal, secrets.token_urlsafe(32), now + (SESSION_SECONDS if principal else 900))
            self._items[self._key(token)] = session
            return token, session

    def remove(self, token):
        with self._lock:
            self._items.pop(self._key(token), None)


class SecurityBoundary:
    """Limit bodies before parsing; initialize request-only auth caches and headers."""
    def __init__(self, app, public_origin):
        self.app, self.public_origin = app, public_origin

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        auth_token = _request_validation.set({})
        timing_token = _request_timing.set({'auth': 0.0, 'ledger': 0.0})
        api = scope['path'].startswith('/api/')
        response_started = False

        async def protected_send(message):
            nonlocal response_started
            if message['type'] == 'http.response.start':
                response_started = True
                headers = list(message.get('headers', []))
                timing = _request_timing.get()
                headers.extend([
                    (b'cache-control', b'no-store' if api else b'no-cache'),
                    (b'x-content-type-options', b'nosniff'),
                    (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'no-referrer'),
                    (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                    (b'strict-transport-security', b'max-age=31536000'),
                ])
                if api:
                    headers.append((b'server-timing', (
                        f"total;dur={(time.perf_counter() - started) * 1000:.1f}, "
                        f"auth;dur={timing['auth'] * 1000:.1f}, ledger;dur={timing['ledger'] * 1000:.1f}"
                    ).encode()))
                message = dict(message, headers=headers)
            await send(message)

        try:
            if api and scope['method'] not in ('GET', 'HEAD'):
                origins = [value.decode('latin-1') for key, value in scope['headers'] if key.lower() == b'origin']
                if origins != [self.public_origin]:
                    return await _error(403, 'origin', '같은 앱 화면에서 다시 시도해 주세요.')(scope, receive, protected_send)
                chunks, size = [], 0
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    size += len(chunk)
                    if size > MAX_BODY:
                        return await _error(413, 'body_too_large', '한 번에 저장할 입력이 너무 큽니다.')(scope, receive, protected_send)
                    chunks.append(chunk)
                    if not message.get('more_body'):
                        break
                body = b''.join(chunks)
                used = False

                async def bounded_receive():
                    nonlocal used
                    if not used:
                        used = True
                        return {'type': 'http.request', 'body': body, 'more_body': False}
                    return await receive()
                await self.app(scope, bounded_receive, protected_send)
            else:
                await self.app(scope, receive, protected_send)
        except Exception:
            # Do not let connection URLs, account responses or request content reach logs.
            if not response_started:
                await _error(503, 'unavailable', '요청을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.')(scope, receive, protected_send)
        finally:
            _request_validation.reset(auth_token)
            _request_timing.reset(timing_token)


def _origin(value):
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or value != f'{parts.scheme}://{parts.netloc}'):
            raise ValueError
        _ = parts.port
        return value
    except (TypeError, ValueError):
        raise RuntimeError('PUBLIC_ORIGIN must be the exact HTTPS origin, without a path.') from None


def _production_config():
    try:
        sheet_id = os.environ['HISTORY_SHEET_ID']
        account_info = json.loads(os.environ['HISTORY_SERVICE_ACCOUNT_JSON'])
        accounts = AccountStore(HistoryStore(sheet_id, account_info))
        config = storage_config(lambda name, default='': (
            os.environ.get(name, '').lower() == 'true' if name == 'ALLOW_LOCAL_LEDGER'
            else os.environ.get(name, default)))
        if config is None:
            raise ValueError
        return accounts, config
    except Exception:
        raise RuntimeError('Configure HISTORY_SHEET_ID, HISTORY_SERVICE_ACCOUNT_JSON and the dedicated ledger database.') from None


def create_app(*, accounts=None, store_factory=None, public_origin=None, static_dir=None,
               clock=time.monotonic, max_sessions=512):
    """Dependency injection permits real SQLite tests, never an implicit cloud fallback."""
    public_origin = _origin(public_origin or os.environ.get('PUBLIC_ORIGIN', ''))
    config = None
    if accounts is None:
        accounts, config = _production_config()
    if store_factory is None and config is None:
        raise RuntimeError('An explicit store factory is required for injected accounts.')

    def validate(principal):
        cache = _request_validation.get()
        if cache is not None and principal in cache:
            return cache[principal]
        started = time.perf_counter()
        try:
            current = accounts.validate(principal)
            if current != principal or not isinstance(current, Principal) or not current.is_admin or current.branch != 'all':
                raise AccessError()
            if cache is not None:
                cache[principal] = current
            return current
        finally:
            timing = _request_timing.get()
            if timing is not None:
                timing['auth'] += time.perf_counter() - started

    try:
        store = store_factory(validate) if store_factory else LedgerStore(config['target'], validate, local=config['local'])
    except Exception:
        raise RuntimeError('The dedicated ledger database is unavailable. No fallback storage was opened.') from None
    sessions = Sessions(clock, max_sessions)
    throttle = LoginThrottle()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityBoundary, public_origin=public_origin)
    app.state.sessions, app.state.store = sessions, store

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return _error(exc.status, exc.code, exc.message, exc.current)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        return _error(400, 'invalid_request', '요청 형식을 확인해 주세요.')

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return _error(exc.status_code, 'not_found' if exc.status_code == 404 else 'invalid_request', '요청한 화면 또는 방법을 확인해 주세요.')

    @app.exception_handler(AccessError)
    async def access_error(request, exc):
        sessions.remove(request.cookies.get(COOKIE))
        return _error(401, 'unauthorized', '로그인 또는 총괄 관리자 권한을 다시 확인해 주세요.')

    @app.exception_handler(LedgerError)
    async def ledger_error(request, exc):
        if isinstance(exc, ConflictError):
            return _error(409, 'conflict', str(exc), exc.current)
        if isinstance(exc, (ValidationError, ClosedDayError)):
            return _error(400, 'validation', str(exc))
        return _error(503, 'unavailable', '원장 저장을 확인하지 못했습니다. 같은 요청으로 다시 시도해 주세요.')

    def session(request):
        item = sessions.get(request.cookies.get(COOKIE))
        if item is None:
            raise APIError(401, 'unauthorized', '다시 로그인해 주세요.')
        return item

    def csrf(request, item):
        token = request.headers.get('x-csrf-token', '')
        if not token or not hmac.compare_digest(token.encode('utf-8'), item.csrf_token.encode('utf-8')):
            raise APIError(403, 'csrf', '화면을 새로 열고 다시 시도해 주세요.')

    def principal(request, *, mutation=False):
        item = session(request)
        if mutation:
            csrf(request, item)
        if item.principal is None:
            raise APIError(401, 'unauthorized', '로그인해 주세요.')
        return validate(item.principal)

    def scoped(actor, branch, business_date):
        try:
            if branch not in CANONICAL_BRANCHES or not isinstance(business_date, str) or date.fromisoformat(business_date).isoformat() != business_date:
                raise ValueError
        except (TypeError, ValueError):
            raise APIError(400, 'validation', '지점과 영업일을 확인해 주세요.') from None
        return component_scope(f'{actor.username}:{actor.revision}', branch, business_date)

    def command_scope(actor, body):
        branch, business_date = body.get('branch'), body.get('date')
        expected = scoped(actor, branch, business_date)
        if body.get('scope') != expected:
            raise APIError(403, 'scope', '지점 또는 날짜가 바뀌었습니다. 원래 화면에서 확인해 주세요.')
        return branch, business_date, expected

    def ledger(operation, *args, **kwargs):
        started = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            timing = _request_timing.get()
            if timing is not None:
                timing['ledger'] += time.perf_counter() - started

    def session_response(item, token=None):
        user = ({'username': item.principal.username, 'role': item.principal.role, 'branch': item.principal.branch}
                if item.principal else None)
        result = JSONResponse({'authenticated': user is not None, 'csrf_token': item.csrf_token,
                               'user': user, 'branches': list(CANONICAL_BRANCHES),
                               'channels': list(PAY_CHANNELS), 'today': kst_today().isoformat()})
        if token:
            result.set_cookie(COOKIE, token, max_age=SESSION_SECONDS if user else 900,
                              secure=True, httponly=True, samesite='strict', path='/')
        return result

    @app.get('/api/session')
    def get_session(request: Request):
        item = sessions.get(request.cookies.get(COOKIE))
        if item is None:
            token, item = sessions.replace()
            return session_response(item, token)
        if item.principal:
            validate(item.principal)
        return session_response(item)

    @app.post('/api/session')
    def login(request: Request, body: dict = Body(...)):
        item = session(request)
        csrf(request, item)
        username, password = body.get('username'), body.get('password')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 1024:
            raise APIError(401, 'unauthorized', '아이디·비밀번호 또는 접근 권한을 확인해 주세요.')
        if not throttle.allow(username):
            raise APIError(429, 'login_throttled', '로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.')
        started = time.perf_counter()
        try:
            actor = accounts.authenticate(username, password)
            if not isinstance(actor, Principal) or not actor.is_admin or actor.branch != 'all':
                raise AccessError()
        except AccessError:
            raise APIError(401, 'unauthorized', '아이디·비밀번호 또는 접근 권한을 확인해 주세요.') from None
        finally:
            _request_timing.get()['auth'] += time.perf_counter() - started
        throttle.success(username)
        token, item = sessions.replace(request.cookies.get(COOKIE), actor)
        return session_response(item, token)

    @app.delete('/api/session')
    def logout(request: Request):
        item = session(request)
        csrf(request, item)
        token, item = sessions.replace(request.cookies.get(COOKIE))
        return session_response(item, token)

    @app.get('/api/ledger')
    def workspace(request: Request, branch: str, date: str):
        actor = principal(request)
        scope = scoped(actor, branch, date)
        result = ledger(store.load_workspace, actor, branch, date)
        day, drafts = result['day'], result['drafts']
        return {'day': _money_strings(day), 'rows': editor_rows(day, drafts), 'summary': editor_summary(day),
                'scope': scope, 'pending_count': day['pending_draft_count']}

    @app.post('/api/ledger/actions')
    def actions(request: Request, body: dict = Body(...)):
        actor = principal(request, mutation=True)
        branch, business_date, scope = command_scope(actor, body)
        action = body.get('action')
        if not isinstance(action, dict):
            raise APIError(400, 'invalid_request', '저장 요청 형식을 확인해 주세요.')
        ack = ledger(dispatch_action, store, actor, branch, business_date, action, scope)
        return {'ack': ack}

    @app.get('/api/ledger/days')
    def days(request: Request, branch: str, start: str, end: str):
        actor = principal(request)
        scoped(actor, branch, start)
        scoped(actor, branch, end)
        if date.fromisoformat(end).toordinal() - date.fromisoformat(start).toordinal() > 366:
            raise APIError(400, 'validation', '기간은 최대 1년씩 조회해 주세요.')
        return {'days': ledger(store.list_days, actor, branch, start, end)}

    @app.get('/api/ledger/events')
    def events(request: Request, branch: str, date: str):
        actor = principal(request)
        scoped(actor, branch, date)
        return {'events': _money_strings(ledger(store.list_events, actor, branch, date))}

    @app.get('/api/ledger/snapshots')
    def snapshots(request: Request, branch: str, date: str):
        actor = principal(request)
        scoped(actor, branch, date)
        originals = ledger(store.list_snapshots, actor, branch, date)
        # Download these bytes directly: parsing/re-encoding in JS changes 64-bit money and its checksum.
        return {'snapshots': [dict(_money_strings(item), download_json=json.dumps(
            item, ensure_ascii=False, allow_nan=False)) for item in originals]}

    @app.post('/api/ledger/close')
    def close(request: Request, body: dict = Body(...)):
        actor = principal(request, mutation=True)
        branch, business_date, _ = command_scope(actor, body)
        result = ledger(store.close_day, actor, body.get('request_id'), branch, business_date, body.get('expected_revision'))
        return {'result': _money_strings(result)}

    @app.post('/api/ledger/reopen')
    def reopen(request: Request, body: dict = Body(...)):
        actor = principal(request, mutation=True)
        branch, business_date, _ = command_scope(actor, body)
        result = ledger(store.reopen_day, actor, body.get('request_id'), branch, business_date,
                        body.get('expected_revision'), body.get('reason'))
        return {'result': _money_strings(result)}

    @app.get('/api/ledger/backup')
    def backup(request: Request):
        actor = principal(request)
        return JSONResponse(ledger(store.export_backup, actor),
                            headers={'Content-Disposition': 'attachment; filename="clinic-pay-ledger-backup.json"'})

    # Static assets contain no credentials or data. There is no SPA catch-all for /api.
    root = Path(static_dir) if static_dir else Path(__file__).parent / 'web_static'
    if root.is_dir():
        app.mount('/', StaticFiles(directory=root, html=True), name='web')
    return app
