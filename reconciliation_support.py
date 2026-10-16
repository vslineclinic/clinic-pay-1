"""정산 보조 경계: 프로세스 공용 요청 제한과 AI 근거 검증. 환자 자료는 공유하지 않는다."""
import hashlib
import json
import re
import threading
import time


class GeminiBusy(RuntimeError):
    """짧은 대기 예산을 넘는 호출은 다음 사용자 요청까지 미룬다."""


class GeminiLimiter:
    # shortcut: 단일 Python 프로세스의 키/모델별 제한. 다중 서버에서는 공유 저장소가 필요하다.
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._windows = {}

    def _window(self, api_key, model, now):
        key = (hashlib.sha256(api_key.encode()).hexdigest(), model)
        for old_key, window in list(self._windows.items()):
            if now - window['touched'] > 3600:
                del self._windows[old_key]
        window = self._windows.setdefault(key, {'times': [], 'retry_at': 0, 'failures': 0, 'touched': now})
        window['times'] = [stamp for stamp in window['times'] if now - stamp < 60]
        window['touched'] = now
        return window

    def reserve(self, api_key, model, rpm):
        with self._lock:
            now = self._clock()
            window = self._window(api_key, model, now)
            wait = max(0, window['retry_at'] - now)
            if len(window['times']) >= rpm:
                wait = max(wait, 60 - (now - window['times'][0]))
            if not wait:
                window['times'].append(now)
            return wait

    def penalize(self, api_key, model):
        with self._lock:
            now = self._clock()
            window = self._window(api_key, model, now)
            window['failures'] = min(window['failures'] + 1, 3)
            window['retry_at'] = max(window['retry_at'], now + min(60, 15 * 2 ** (window['failures'] - 1)))


GEMINI_LIMITER = GeminiLimiter()


def validate_ai_evidence(response, evidence, token_map):
    """허용된 근거·부호 있는 정수 금액만 출력한다. 설명의 원인 추정은 보증하지 않는다."""
    message = 'AI 응답의 근거 또는 금액을 검증하지 못했습니다. 결과를 적용하지 않고 원본 검토를 권합니다.'
    try:
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', str(response).strip())
        payload = json.loads(cleaned)
        if not isinstance(payload, dict) or set(payload) != {'findings'}:
            raise ValueError(message)
        findings = payload['findings']
        if not isinstance(findings, list) or len(findings) > 30:
            raise ValueError(message)
        lines = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {'evidence_id', 'amount', 'explanation'}:
                raise ValueError(message)
            ident, amount, explanation = (finding[key] for key in ('evidence_id', 'amount', 'explanation'))
            item = evidence.get(ident) if isinstance(ident, str) else None
            if item is None or type(amount) is not int or amount != item['amount']:
                raise ValueError(message)
            if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 1200:
                raise ValueError(message)
            if any(token not in token_map for token in re.findall(r'(?<![A-Za-z0-9_])[PNAC]\d{3,}(?![A-Za-z0-9_])', explanation)):
                raise ValueError(message)
            # 설명 속 금액도 같은 근거 금액만 허용한다. 다른 수치는 서버가 만든 표에서 본다.
            for money in re.findall(r'(?<![\dA-Za-z_])([+-]?\d[\d,]*)\s*원', explanation):
                if abs(int(money.replace(',', ''))) != abs(amount):
                    raise ValueError(message)
            lines.append(f"- [{ident}] {item['label']} · {amount:+,}원 — {explanation.strip()}")
        return '\n'.join(lines) or '전달한 근거에서 추가 설명을 제시하지 못했습니다. 결정론적 검증 표를 확인해 주세요.'
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(message) from error
