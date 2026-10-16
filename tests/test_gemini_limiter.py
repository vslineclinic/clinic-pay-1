"""공유 키/모델 제한은 세션을 바꾸거나 429를 받아도 초기화되지 않는다."""
import concurrent.futures
import threading

import pytest


def test_shared_key_and_model_window_with_bounded_wait():
    from reconciliation_support import GeminiLimiter
    clock = [100.0]
    limiter = GeminiLimiter(clock=lambda: clock[0])
    assert limiter.reserve('key-a', 'model-a', 2) == 0
    assert limiter.reserve('key-a', 'model-a', 2) == 0
    assert limiter.reserve('key-a', 'model-a', 2) == 60
    assert limiter.reserve('key-b', 'model-a', 2) == 0
    assert limiter.reserve('key-a', 'model-b', 2) == 0
    clock[0] += 60
    assert limiter.reserve('key-a', 'model-a', 2) == 0
    assert 'key-a' not in repr(limiter.__dict__)


def test_429_keeps_attempts_and_exponential_cooldown():
    from reconciliation_support import GeminiLimiter
    clock = [100.0]
    limiter = GeminiLimiter(clock=lambda: clock[0])
    limiter.reserve('key', 'model', 10)
    limiter.penalize('key', 'model')
    assert limiter.reserve('key', 'model', 10) == 15
    clock[0] += 15
    limiter.reserve('key', 'model', 10)
    limiter.penalize('key', 'model')
    assert limiter.reserve('key', 'model', 10) == 30
    clock[0] += 30
    # Count the two unsuccessful calls rather than clearing the history on 429.
    assert limiter.reserve('key', 'model', 2) == 15
    for _ in range(10):
        limiter.penalize('key', 'model')
    assert limiter.reserve('key', 'model', 10) == 60


def test_parallel_reservations_cannot_both_take_last_slot():
    from reconciliation_support import GeminiLimiter
    limiter = GeminiLimiter(clock=lambda: 100.0)
    barrier = threading.Barrier(8)
    def reserve(_):
        barrier.wait()
        return limiter.reserve('key', 'model', 2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert results.count(0) == 2
    assert results.count(60) == 6


def test_second_browser_cannot_retry_same_key_model_during_429_backoff(app, monkeypatch):
    from reconciliation_support import GeminiLimiter
    from test_privacy import _fake_genai
    limiter = GeminiLimiter(clock=lambda: 100.0)
    monkeypatch.setattr(app, 'GEMINI_LIMITER', limiter)
    calls = _fake_genai(monkeypatch, [RuntimeError('429 quota')])
    with pytest.raises(RuntimeError, match='429'):
        app.run_gemini('shared-key', 'synthetic payload', model='synthetic-model', allow_fallback=False)
    app.st.session_state.clear()  # A different browser has no session cache or counter.
    with pytest.raises(RuntimeError, match='429'):
        app.run_gemini('shared-key', 'synthetic payload', model='synthetic-model', allow_fallback=False)
    assert len(calls) == 1
    assert limiter.reserve('shared-key', 'synthetic-model', 10) == 15
