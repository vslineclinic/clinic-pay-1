"""clinic-pay-1 개발 배포의 권한·저장 경계. 운영 앱의 기본 동작은 바꾸지 않는다."""
from pathlib import Path
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from branch_access import AccessError, assert_branch
from reconciliation_history import HistoryError


def require_preview(principal):
    # Streamlit may reload authentication while this module remains cached.
    from branch_access import AccessError, Principal

    if not isinstance(principal, Principal) or not principal.is_admin:
        raise AccessError("개발 중인 정산 앱은 총괄관리자만 이용할 수 있습니다.")
    return principal


def storage_config(secret):
    target = secret("DATABASE_URL", "")
    if target:
        try:
            parts = urlsplit(target)
            if parts.scheme not in ("postgres", "postgresql") or not parts.hostname or not parts.path.strip("/"):
                raise ValueError
            query = parse_qsl(parts.query, keep_blank_values=True)
            local_host = parts.hostname in ("localhost", "127.0.0.1", "::1")
            modes = [v for k, v in query if k == "sslmode"]
            if not local_host:
                if any(v not in ("require", "verify-ca", "verify-full") for v in modes):
                    raise ValueError
                if not modes:
                    query.append(("sslmode", "require"))
            return {"target": urlunsplit(parts._replace(query=urlencode(query))),
                    "local": False, "label": "전용 PostgreSQL"}
        except (TypeError, ValueError):
            raise ValueError("전용 데이터베이스 연결 설정을 확인해 주세요. 원격 연결에는 TLS가 필요합니다.") from None
    path = secret("LOCAL_LEDGER_PATH", "")
    if path:
        if secret("ALLOW_LOCAL_LEDGER", False) is not True or not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("로컬 시험 저장은 절대 경로와 ALLOW_LOCAL_LEDGER=true를 명시해야 합니다.")
        return {"target": path, "local": True, "label": "로컬 개발 저장 · 클라우드 영구 보존 아님"}
    return None


def get_store(secret, validate):
    config = storage_config(secret)
    if config is None:
        return None
    return _configured_store(config["target"], config["local"], validate)


@lru_cache(maxsize=4)
def _configured_store(target, local, validate):
    # No principal, receipt data or open connection is retained. Each operation
    # validates the caller and opens its own transaction. Schema setup runs once.
    from receipt_ledger import LedgerStore
    return LedgerStore(target, validate, local=local)


def load_analysis_day(store, principal, selection):
    require_preview(principal)
    assert_branch(principal, selection["branch"])
    day = store.load_day(principal, selection["branch"], selection["business_date"])
    if day["revision"] != selection["revision"]:
        raise ValueError("수납원장이 변경되었습니다. 최신 원장에서 대조 분석을 다시 시작해 주세요.")
    if day.get("pending_draft_count", 0) or store.list_drafts(principal, selection["branch"], selection["business_date"]):
        raise ValueError("저장 중이거나 확인이 필요한 초안이 있습니다. 수납원장에서 먼저 정리해 주세요.")
    return day


class DevelopmentHistory:
    """개발 분석 요약을 운영 Google 기록에 쓰지 않는다."""
    def __init__(self, store, principal, source_manifest=None):
        self.store, self.principal = store, require_preview(principal)
        self.source_manifest = source_manifest

    def append_record(self, record):
        if self.store is None:
            raise HistoryError("개발 데이터베이스가 연결되지 않아 분석 기록을 저장하지 않았습니다.")
        from receipt_ledger import LedgerError
        try:
            if self.source_manifest is not None:
                if (record.get("branch"), record.get("settlement_date")) != (self.source_manifest["branch"], self.source_manifest["business_date"]):
                    raise HistoryError("분석 결과와 원장의 지점·날짜가 다릅니다. 다시 분석해 주세요.")
            return self.store.append_summary(self.principal, record, source_manifest=self.source_manifest)
        except AccessError:
            raise
        except LedgerError:
            raise HistoryError("개발 분석 기록을 저장하지 못했습니다. 결과를 유지하고 다시 시도해 주세요.") from None
        except ValueError:
            raise HistoryError("분석 이후 원장이 변경되었거나 미완료 초안이 있습니다. 다시 분석한 뒤 기록해 주세요.") from None

    def list_month(self, month, branch):
        if self.store is None:
            raise HistoryError("개발 데이터베이스를 연결하면 날짜별 분석 기록을 확인할 수 있습니다.")
        from receipt_ledger import LedgerError
        try:
            return self.store.list_summaries(self.principal, branch, month)
        except LedgerError:
            raise HistoryError("개발 분석 기록을 조회하지 못했습니다. 잠시 후 다시 시도해 주세요.") from None

