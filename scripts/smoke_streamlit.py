"""실제 Streamlit UI smoke. pytest의 Streamlit 스텁을 피하려 별도 프로세스로 실행한다.

    python scripts/smoke_streamlit.py [통합 저장소의 app.py]

실제 파일·환자·외부 시트·AI 호출 없이 합성 분석 결과를 세션에 넣어 화면을 확인한다.
"""
from pathlib import Path
import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import streamlit
from streamlit.testing.v1 import AppTest


APP_FILE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "app.py"
sys.path.insert(0, str(APP_FILE.parent))
spec = importlib.util.spec_from_file_location("clinic_smoke_app", APP_FILE)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

import branch_access as access
import reconciliation_history as history
import remember_browser
import remember_login


class SyntheticHistoryStore:
    def __init__(self, fail_first=False):
        self.records = []
        self.attempts = 0
        self.fail_first = fail_first

    def append_record(self, record):
        self.attempts += 1
        if self.fail_first and self.attempts == 1:
            raise history.HistoryError("합성 저장 실패")
        self.records.append(record)
        return True

    def list_month(self, month, branch=""):
        return [r for r in self.records if r["settlement_date"].startswith(month) and r["branch"] == branch]


class SyntheticAccounts:
    def __init__(self):
        self.users = {
            "synthetic-hq": access.Principal("synthetic-hq", "admin", "all", "11111111-1111-4111-8111-111111111111"),
            "synthetic-incheon": access.Principal("synthetic-incheon", "branch", "인천점", "22222222-2222-4222-8222-222222222222"),
            "synthetic-jamsil": access.Principal("synthetic-jamsil", "branch", "잠실점", "33333333-3333-4333-8333-333333333333"),
            "synthetic-magok": access.Principal("synthetic-magok", "branch", "마곡점", "44444444-4444-4444-8444-444444444444"),
        }
        self.passwords = {username: "synthetic-smoke-password" for username in self.users}

    def authenticate(self, username, password):
        if password != self.passwords.get(username) or username not in self.users:
            raise access.AccessError("아이디·비밀번호 또는 접근 권한을 확인해 주세요.")
        return self.users[username]

    def validate(self, principal):
        if principal != self.users.get(getattr(principal, "username", "")):
            raise access.AccessError("계정이 변경되었습니다. 다시 로그인해 주세요.")
        return principal

    def list_accounts(self, principal):
        self.validate(principal)
        if not principal.is_admin:
            raise access.AccessError("총괄관리자만 사용할 수 있습니다.")
        return [{"username": p.username, "role": p.role, "branch": p.branch,
                 "active": True, "revision": p.revision, "updated_at": "2026-10-15T00:00:00+00:00"}
                for p in self.users.values()]

    def save_branch_account(self, principal, username, branch, password=None, active=True, expected_revision=None):
        from uuid import uuid4
        self.validate(principal)
        if not principal.is_admin:
            raise access.AccessError("총괄관리자만 사용할 수 있습니다.")
        self.users[username] = access.Principal(username, "branch", branch, str(uuid4()))
        if password:
            self.passwords[username] = password


auth_store = SyntheticAccounts()
real_account_store, real_history_store = access.AccountStore, history.HistoryStore
access.AccountStore = lambda *_: auth_store
default_history_store = SyntheticHistoryStore()
history.HistoryStore = lambda *_: default_history_store


class SyntheticRemember:
    def __init__(self):
        self.tokens = {}
        self.revoked = set()
        self.fail_issue = False
        self.restore_calls = 0

    def issue(self, principal):
        from uuid import uuid4
        if self.fail_issue:
            raise access.AccessError("합성 기기 저장 실패")
        token = str(uuid4()) + "." + "a" * 43
        self.tokens[token] = principal
        return token

    def restore(self, token):
        self.restore_calls += 1
        if token in self.revoked or token not in self.tokens:
            raise access.AccessError("로그인 유지가 만료되었거나 해제되었습니다.")
        return auth_store.validate(self.tokens[token])

    def revoke(self, token):
        self.revoked.add(token)


class SyntheticCookieBridge:
    def __init__(self):
        self.cookie = ""
        self.blocked = False
        self.stale = None

    def render(self, command=None):
        if self.stale is not None:
            return self.stale
        ack = ""
        if command:
            ack = command["nonce"]
            if not self.blocked:
                self.cookie = command.get("token", "") if command["action"] == "set" else ""
        return {"ready": True, "token": self.cookie, "ack": ack, "ok": not self.blocked}


remembered = SyntheticRemember()
cookie_bridge = SyntheticCookieBridge()
real_remember_store, real_cookie_render = remember_login.RememberStore, remember_browser.render
remember_login.RememberStore = lambda *_: remembered
remember_browser.render = cookie_bridge.render


def verify_download_revocation():
    """실제 Streamlit 저장소에서 로그아웃한 세션의 과거 다운로드를 폐기한다."""
    from streamlit.runtime.media_file_manager import MediaFileManager
    from streamlit.runtime.media_file_storage import MediaFileStorageError
    from streamlit.runtime.memory_media_file_storage import MemoryMediaFileStorage

    storage = MemoryMediaFileStorage("/media")
    manager = MediaFileManager(storage)
    with patch("streamlit.runtime.media_file_manager._get_session_id", return_value="synthetic-session-a"):
        private_a = manager.add(b"synthetic-branch-a-report", "text/csv", "a-report", is_for_static_download=True)
        shared = manager.add(b"synthetic-shared-report", "text/csv", "shared-report", is_for_static_download=True)
    with patch("streamlit.runtime.media_file_manager._get_session_id", return_value="synthetic-session-b"):
        private_b = manager.add(b"synthetic-branch-b-report", "text/csv", "b-report", is_for_static_download=True)
        assert manager.add(b"synthetic-shared-report", "text/csv", "shared-report", is_for_static_download=True) == shared
    assert storage.get_file(private_a.rsplit("/", 1)[1]).content == b"synthetic-branch-a-report"
    for session_id, removed in (("synthetic-session-a", [private_a]),
                                ("synthetic-session-b", [private_b, shared])):
        with patch("streamlit.runtime.exists", return_value=True), \
             patch("streamlit.runtime.get_instance", return_value=SimpleNamespace(media_file_mgr=manager)), \
             patch("streamlit.runtime.scriptrunner.get_script_run_ctx", return_value=SimpleNamespace(session_id=session_id)), \
             patch.object(streamlit, "session_state", {"patient": "synthetic-private-state"}):
            app._reset_workspace(logout=True)
            assert "patient" not in streamlit.session_state
            assert all(key.startswith("_remember_") for key in streamlit.session_state)
        # ScriptRunner's normal end-run cleanup is the second deletion pass.
        manager.remove_orphaned_files()
        for url in removed:
            try:
                storage.get_file(url.rsplit("/", 1)[1])
            except MediaFileStorageError:
                pass
            else:
                raise AssertionError("로그아웃 이후 과거 다운로드의 비공개 바이트가 남았습니다.")
        if session_id == "synthetic-session-a":
            assert storage.get_file(private_b.rsplit("/", 1)[1]).content == b"synthetic-branch-b-report"
            assert storage.get_file(shared.rsplit("/", 1)[1]).content == b"synthetic-shared-report"


verify_download_revocation()


def run_ok(at):
    at.run(timeout=40)
    assert not at.exception, [e.message for e in at.exception]
    return at


def authenticated(username="synthetic-hq", configured=None, remember=False):
    at = AppTest.from_file(str(APP_FILE))
    at.secrets["APP_PASSWORD"] = "synthetic-smoke-password"
    at.secrets["GOOGLE_API_KEY"] = "synthetic-smoke-api-key"
    at.secrets["clinic_daily_sheets"] = {} if configured is None else configured
    at.secrets["HISTORY_SHEET_ID"] = "synthetic-sheet"
    at.secrets["HISTORY_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
    run_ok(at)
    assert not at.get("file_uploader"), "로그인 전 업로더가 노출됨"
    assert at.text_input(key="_auth_input_username").proto.autocomplete == "username"
    assert at.text_input(key="_auth_input_password").proto.autocomplete == "current-password"
    assert at.checkbox(key="_auth_remember").value is False
    at.text_input(key="_auth_input_username").set_value(username)
    at.text_input(key="_auth_input_password").set_value("synthetic-smoke-password")
    if remember:
        at.checkbox(key="_auth_remember").set_value(True)
    next(b for b in at.button if b.label == "로그인").click()
    run_ok(at)
    assert "_auth_input_password" not in at.session_state
    return at


def fixture():
    daily, _ = app.parse_daily(pd.DataFrame([
        ["내원순서", "차트번호", "성명", "카드", "현금", "이체", "카카오페이"],
        [1, "101", "예시 A", 100000, 0, 0, 0],
        [2, "102", "예시 B", 50000, 0, 0, 0],
        [3, "103", "예시 C", 0, 20000, 10000, 0],
        [4, "104", "예시 D", 0, 0, 0, 30000],
    ]))
    refund = daily.iloc[[0]].copy()
    refund["카드"] = refund["총액"] = 20000
    patient = app.parse_patient(pd.DataFrame([
        ["차트번호", "이름", "결제수단", "비급여(과세총금액)", "본부금", "결제메모", "수납일"],
        ["101", "예시 A", "카드", 70000, 0, "", "2026-10-12"],
        ["102", "예시 B", "카드", 60000, 0, "", "2026-10-12"],
        ["103", "예시 C", "현금", 20000, 0, "", "2026-10-12"],
        ["103", "예시 C", "계좌이체", 10000, 0, "", "2026-10-12"],
        ["104", "예시 D", "카카오페이", 30000, 0, "", "2026-10-12"],
    ]))
    hansol = app.parse_hansol(pd.DataFrame({
        "금액": [70000, 60000, 30000], "승인번호": ["101001", "102001", "103001"],
        "거래시간": ["101000", "111000", "121000"], "거래일": ["2026-10-12"] * 3,
        "거래상태": ["정상승인"] * 3, "구분": ["카드", "카드", "현금"],
        "매입사": ["예시카드", "예시카드", "현금영수증"], "카드번호": ["1111", "2222", ""],
    }))
    return daily, refund, patient, hansol


# Startup and both upload layouts use real controls on the minimum supported release.
at = authenticated()
assert any(item.label == "자료 준비 안내" for item in at.expander)
assert len(at.get("file_uploader")) == 2
assert next(b for b in at.button if b.label == "마감 대조 시작").disabled
at.radio(key="daily_mode").set_value("파일 업로드")
run_ok(at)
assert len(at.get("file_uploader")) == 3
assert at.selectbox(key="upload_branch").value == "지점을 선택해 주세요"
assert "BW컨설팅" not in at.selectbox(key="upload_branch").options
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
assert next(m for m in at.metric if m.label == "정산 기록").value == "0일"
at.radio(key="workspace_view").set_value("마감 대조")
run_ok(at)
at.radio(key="analysis_mode").set_value("기간 분석")
run_ok(at)
assert len(at.get("file_uploader")) == 2
assert next(b for b in at.button if b.label == "기간 대조 시작").disabled

# An aggregate zero still has two offsetting patient card differences.
at = authenticated()
daily, refund, patient, hansol = fixture()
empty = pd.DataFrame()
totals = app.compute_totals(empty, daily, refund, patient)
channels = app.compute_channel_recon(totals)
assert channels["일마-차트"].eq(0).all()
for key, value in {
    "done": True, "has_hansol": False, "hansol": empty, "daily": daily,
    "daily_refund": refund, "patient": patient, "match_df": empty, "matched_h": set(),
    "p1_full": empty, "p1_diff": empty, "h_um": empty, "d_um": empty,
    "totals": totals, "channel_df": channels, "suspects_by_channel": {},
    "reconciliation_branch": "인천점",
    "reconciliation_date": "2026-10-12",
    "reconciliation_run_id": "55555555-5555-4555-8555-555555555555",
    "reconciliation_checked_at": "2026-10-15T00:00:00+00:00",
    "_day_owner": app._analysis_owner(auth_store.users["synthetic-hq"], "인천점"),
}.items():
    at.session_state[key] = value
run_ok(at)
assert not any(item.label == "자료 준비 안내" for item in at.expander)
assert any("합계는 일치하지만 환자별 검증" in w.value for w in at.warning)
assert not any("추가 분석 불필요" in v.value for v in at.success)
assert {"차이 근거", "거래 대조", "AI 설명", "환자별 검증"}.issubset({t.label for t in at.tabs})
key_widget = at.text_input(key="_api_key")
assert key_widget.value == "synthetic-smoke-api-key", "감사 #12 API 키 기본값 동작이 변경됨"
assert key_widget.proto.type == key_widget.proto.PASSWORD
assert any("차이(일일마감-베가스)" in frame.value.columns for frame in at.dataframe)
channel_view = next(frame.value for frame in at.dataframe if "한솔-차트" in frame.value.columns)
assert channel_view["한솔"].eq("—").all()
assert at.session_state["channel_df"]["한솔"].isna().all(), "미제공 표시가 원본 값을 변경함"
assert "예시 A" not in at.session_state["_ai_data"]
assert "예시 B" not in at.session_state["_ai_data"]

# Native channel selection updates one evidence panel and only that channel's candidates.
at.session_state["suspects_by_channel"] = {
    channel: [{"출처": f"선택 확인 {channel}", "환자": "합성 선택 사례", "금액": 100,
               "단서": "화면 전환 검증용 합성 후보", "조치": "검토"}]
    for channel in ("카드", "현금+이체", "플랫폼")
}
for channel in ("카드", "현금+이체", "플랫폼"):
    at.radio(key="review_channel").set_value(channel)
    run_ok(at)
    panels = [m.value for m in at.markdown if '<div class="clinic-channel-evidence">' in m.value]
    assert len(panels) == 1
    assert f"{channel} 마감 대조" in panels[0]
    assert ("비교대상 아님" if channel == "플랫폼" else "미제공") in panels[0]
    candidate_tables = [frame.value for frame in at.dataframe if "출처" in frame.value.columns]
    assert len(candidate_tables) == 1
    assert candidate_tables[0]["출처"].tolist() == [f"선택 확인 {channel}"]
run_ok(at)
assert at.radio(key="review_channel").value == "플랫폼", "재실행에서 채널 선택이 초기화됨"

# Channel offsets cannot turn a zero signed total into a clean review state.
offset_daily = daily.copy()
offset_daily.loc[offset_daily["차트번호"] == "101", ["카드", "총액"]] += 10000
offset_daily.loc[offset_daily["차트번호"] == "103", ["현금", "총액"]] -= 10000
offset_totals = app.compute_totals(empty, offset_daily, refund, patient)
at.session_state["daily"] = offset_daily
at.session_state["totals"] = offset_totals
at.session_state["channel_df"] = app.compute_channel_recon(offset_totals)
at.session_state["suspects_by_channel"] = {}
run_ok(at)
assert any("전체 합계가 같아도 채널별 차이는 남아" in w.value for w in at.warning)
assert any("합계는 일치하지만 환자별 검증" in w.value for w in at.warning)
summary = next(m.value for m in at.markdown if '<div class="clinic-summary-grid">' in m.value)
assert '일일마감 − 베가스' in summary and '>+0<small>원</small>' in summary

# The top difference is signed daily-minus-Vegas, not an absolute spread.
offset_daily.loc[offset_daily["차트번호"] == "103", ["현금", "총액"]] -= 10000
negative_totals = app.compute_totals(empty, offset_daily, refund, patient)
at.session_state["daily"] = offset_daily
at.session_state["totals"] = negative_totals
at.session_state["channel_df"] = app.compute_channel_recon(negative_totals)
run_ok(at)
summary = next(m.value for m in at.markdown if '<div class="clinic-summary-grid">' in m.value)
assert '>-10,000<small>원</small>' in summary

# Reset must clear records and AI state while retaining authentication.
next(b for b in at.button if "새 파일로 다시" in b.label).click()
run_ok(at)
assert "done" not in at.session_state
assert "_ai_data" not in at.session_state
assert "review_channel" not in at.session_state
assert "_auth_principal" in at.session_state
assert at.get("file_uploader")
assert any(item.label == "자료 준비 안내" for item in at.expander)

# Platform-only date gaps remain visible in period overview and detail selection.
at = authenticated()
day = "2026-10-12"
table = app.compute_period_recon(hansol, patient)
for key, value in {
    "일마-차트카드차이": 0, "일마-차트현금차이": 0, "일마-차트플랫폼차이": -20000,
    "일마검증건수": 3, "일마비고": "",
}.items():
    table[key] = value
period_daily = daily.copy()
period_daily.loc[period_daily["차트번호"] == "104", ["카카오페이", "플랫폼합", "총액"]] = 10000
for key, value in {
    "period_done": True, "per_table": table, "per_hansol": hansol, "per_patient": patient,
    "per_gs_branch": "인천점", "per_gs_skipped": {}, "per_gs_daily": {day: (period_daily, refund)},
    "_period_owner": app._analysis_owner(auth_store.users["synthetic-hq"], "인천점"),
}.items():
    at.session_state[key] = value
run_ok(at)
assert day in at.selectbox(key="per_day").options
assert next(m for m in at.metric if m.label == "플랫폼 차이 발생일").value == "1일"
assert at.dataframe

# Missing Google data does not turn an otherwise equal period into a success state.
for column in table.columns:
    if "차이" in column or "건수" in column:
        table[column] = 0
at.session_state["per_table"] = table
at.session_state["per_gs_skipped"] = {day: "합성 조회 실패"}
at.session_state["per_gs_daily"] = {}
run_ok(at)
assert any("일부 날짜는 미검증" in item.value for item in at.info)
assert not any("검증 대상 거래를 찾지 못했습니다" in item.value for item in at.success)
assert any("합성 조회 실패" in item.value for item in at.warning)

# Storage failures keep analysis visible; retry and navigation do not append duplicates.
# The shared in-memory backend is synthetic: no live Google credentials or API calls.
from datetime import date
store = SyntheticHistoryStore(fail_first=True)
history.HistoryStore = lambda *_: store
at = authenticated()
at.secrets["HISTORY_SHEET_ID"] = "synthetic-sheet"
at.secrets["HISTORY_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
totals = app.compute_totals(empty, daily, refund, patient)
for key, value in {
    "done": True, "has_hansol": False, "hansol": empty, "daily": daily,
    "daily_refund": refund, "patient": patient, "match_df": empty, "matched_h": set(),
    "p1_full": empty, "p1_diff": empty, "h_um": empty, "d_um": empty,
    "totals": totals, "channel_df": app.compute_channel_recon(totals), "suspects_by_channel": {},
    "reconciliation_branch": "인천점", "reconciliation_date": "2026-10-12",
    "reconciliation_run_id": "11111111-1111-4111-8111-111111111111",
    "reconciliation_checked_at": "2026-10-14T04:00:00+00:00",
    "_day_owner": app._analysis_owner(auth_store.users["synthetic-hq"], "인천점"),
}.items():
    at.session_state[key] = value
run_ok(at)
assert any("합성 저장 실패" in w.value for w in at.warning)
assert at.session_state["done"] and store.records == []
assert not any("누적 저장됨" in c.value for c in at.caption)
assert at.button(key="history_retry"), "최초 저장 실패 화면에 재시도 버튼이 없음"
run_ok(at)
at.button(key="history_retry").click()
run_ok(at)
assert len(store.records) == 1 and store.records[0]["status"] == "검토 필요"
assert any("누적 저장됨" in c.value for c in at.caption)
run_ok(at)
assert store.attempts == 2
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
at.date_input(key="history_month").set_value(date(2026, 10, 1))
run_ok(at)
monthly = next(frame.value for frame in at.dataframe if "확인 시각" in frame.value.columns)
assert monthly.loc[monthly["날짜"] == "2026-10-12", "환자별 검증"].item() > 0
assert monthly.loc[monthly["날짜"] == "2026-10-13", "상태"].item() == "기록 없음"
assert at.session_state["done"] and store.attempts == 2
at.radio(key="workspace_view").set_value("마감 대조")
run_ok(at)
assert store.attempts == 2

# Fresh browser session reads the stored summary without uploading the source files.
at = authenticated()
at.secrets["HISTORY_SHEET_ID"] = "synthetic-sheet"
at.secrets["HISTORY_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
at.date_input(key="history_month").set_value(date(2026, 10, 1))
run_ok(at)
assert next(m for m in at.metric if m.label == "정산 기록").value == "1일"
assert not at.get("file_uploader")

# Branch staff cannot choose another branch, switch to daily uploads or manage accounts.
configured = {"인천점": "synthetic-own-google", "잠실점": "synthetic-foreign-google"}
at = authenticated("synthetic-incheon", configured)
assert not any(r.key == "daily_mode" for r in at.radio)
assert not any(s.key in {"gs_branch", "upload_branch"} for s in at.selectbox)
assert "계정 관리" not in at.radio(key="workspace_view").options
assert len(at.get("file_uploader")) == 2
at.session_state["gs_branch"] = "잠실점"
at.session_state["daily_mode"] = "파일 업로드"
run_ok(at)
assert at.radio(key="workspace_view").value == "마감 대조"
assert len(at.get("file_uploader")) == 2
at.radio(key="analysis_mode").set_value("기간 분석")
run_ok(at)
assert not any(s.key == "per_branch" for s in at.selectbox)
assert any("소속 지점의 구글 일일마감으로 자동 연결" in c.value for c in at.caption)
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
at.date_input(key="history_month").set_value(date(2026, 10, 1))
run_ok(at)
assert not any(s.key == "history_branch" for s in at.selectbox)
assert next(m for m in at.metric if m.label == "정산 기록").value == "1일"
assert any("인천점" in m.value for m in at.markdown)

# Forged or stale result ownership clears uploaded data, caches and AI before output.
at.session_state["done"] = True
at.session_state["reconciliation_branch"] = "잠실점"
at.session_state["_day_owner"] = app._analysis_owner(auth_store.users["synthetic-incheon"], "인천점")
at.session_state["patient"] = "synthetic-foreign-patient"
at.session_state["_ai_data"] = "synthetic-foreign-ai"
at.session_state["_gs_cache"] = {"foreign": "synthetic-foreign-source"}
run_ok(at)
assert "done" not in at.session_state and "patient" not in at.session_state
assert "_ai_data" not in at.session_state and "_gs_cache" not in at.session_state
assert at.session_state["_auth_principal"].branch == "인천점"
assert not at.get("download_button")
next(b for b in at.button if b.key == "logout").click()
run_ok(at)
assert "_auth_principal" not in at.session_state and not at.get("file_uploader")
at.text_input(key="_auth_input_username").set_value("synthetic-jamsil")
at.text_input(key="_auth_input_password").set_value("synthetic-smoke-password")
next(b for b in at.button if b.label == "로그인").click()
run_ok(at)
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
at.date_input(key="history_month").set_value(date(2026, 10, 1))
run_ok(at)
assert next(m for m in at.metric if m.label == "정산 기록").value == "0일"

# Authoritative account revision changes revoke an already signed-in browser.
auth_store.users["synthetic-jamsil"] = access.Principal(
    "synthetic-jamsil", "branch", "잠실점", "66666666-6666-4666-8666-666666666666")
at.session_state["_ai_data"] = "old-analysis"
run_ok(at)
assert "_auth_principal" not in at.session_state and "_ai_data" not in at.session_state
assert not at.dataframe and not at.get("file_uploader")

# An unlinked branch can read its own history without borrowing a default Google link.
at = authenticated("synthetic-magok", configured)
assert any("소속 지점의 구글 일일마감 연결이 없습니다" in w.value for w in at.warning)
assert next(b for b in at.button if b.label == "마감 대조 시작").disabled
assert not any(s.key == "gs_branch" for s in at.selectbox)
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
assert next(m for m in at.metric if m.label == "정산 기록").value == "0일"

# HQ sees all canonical branch histories, including unlinked branches, and safe account metadata.
at = authenticated()
at.radio(key="workspace_view").set_value("정산 기록")
run_ok(at)
assert set(at.selectbox(key="history_branch").options) == set(access.CANONICAL_BRANCHES)
assert len(at.selectbox(key="history_branch").options) == 7
assert "BW컨설팅" not in at.selectbox(key="history_branch").options
at.selectbox(key="history_branch").set_value("마곡점")
run_ok(at)
assert next(m for m in at.metric if m.label == "정산 기록").value == "0일"
at.radio(key="workspace_view").set_value("계정 관리")
run_ok(at)
account_table = next(f.value for f in at.dataframe if "아이디" in f.value.columns)
assert set(account_table.columns) == {"아이디", "권한", "지점", "사용"}
assert not any("hash" in c or "비밀번호" in c for c in account_table.columns)
at.radio(key="account_edit_mode").set_value("새 지점 계정 추가")
run_ok(at)
assert len(at.selectbox(key="account_branch_new").options) == 7
assert "BW컨설팅" not in at.selectbox(key="account_branch_new").options
at.text_input(key="new_account_username").set_value("synthetic-new-staff")
at.selectbox(key="account_branch_new").set_value("일산점")
at.text_input(key="account_password_new").set_value("synthetic-new-password")
next(b for b in at.button if b.label == "계정 저장").click()
run_ok(at)
assert at.text_input(key="account_password_new").value == ""
assert auth_store.users["synthetic-new-staff"].branch == "일산점"
assert any("지점 계정을 저장했습니다" in s.value for s in at.success)

# Login limits are shared by separate browser sessions in this worker.
for attempt in range(6):
    limited = AppTest.from_file(str(APP_FILE))
    limited.secrets["HISTORY_SHEET_ID"] = "synthetic-sheet"
    limited.secrets["HISTORY_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
    run_ok(limited)
    limited.text_input(key="_auth_input_username").set_value("synthetic-rate-limit")
    limited.text_input(key="_auth_input_password").set_value("synthetic-wrong-password")
    next(b for b in limited.button if b.label == "로그인").click()
    run_ok(limited)
    assert limited.text_input(key="_auth_input_password").value == ""
    assert not limited.get("file_uploader")
assert any("15분" in e.value for e in limited.error)


def fresh_browser(configured=None):
    fresh = AppTest.from_file(str(APP_FILE))
    fresh.secrets["HISTORY_SHEET_ID"] = "synthetic-sheet"
    fresh.secrets["HISTORY_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
    fresh.secrets["clinic_daily_sheets"] = configured or {}
    return run_ok(fresh)


# Opt-in remembers an opaque token, then a new Streamlit session restores only its own branch.
at = authenticated("synthetic-incheon", configured, remember=True)
token = cookie_bridge.cookie
assert token and "synthetic-smoke-password" not in token
assert at.session_state["_remember_token"] == token
calls = remembered.restore_calls
run_ok(at)
assert remembered.restore_calls > calls, "기존 기억된 로그인에서 토큰 유효성을 재검증하지 않음"
reconnected = fresh_browser(configured)
assert reconnected.session_state["_auth_principal"].branch == "인천점"
assert not any(s.key == "gs_branch" for s in reconnected.selectbox)
assert not any(t.key == "_auth_input_password" for t in reconnected.text_input)

# Remote device revocation also clears an already open browser before data can render.
reconnected.session_state["_ai_data"] = "synthetic-old-private-analysis"
remembered.revoke(token)
run_ok(reconnected)
assert "_auth_principal" not in reconnected.session_state and "_ai_data" not in reconnected.session_state
assert not reconnected.get("file_uploader") and cookie_bridge.cookie == ""

# An old component response cannot restore login while logout awaits its exact clear ACK.
at = authenticated("synthetic-incheon", configured, remember=True)
token = cookie_bridge.cookie
cookie_bridge.stale = {"ready": True, "token": token, "ack": "synthetic-old-ack", "ok": True}
calls = remembered.restore_calls
next(b for b in at.button if b.key == "logout").click()
run_ok(at)
assert token in remembered.revoked and "_auth_principal" not in at.session_state
assert at.session_state["_remember_command"]["action"] == "clear"
run_ok(at)
assert remembered.restore_calls == calls and not at.get("file_uploader")
cookie_bridge.stale = None
run_ok(at)
assert cookie_bridge.cookie == "" and "_remember_command" not in at.session_state
assert "_auth_principal" not in fresh_browser(configured).session_state

# Cookie blocking and token-write failure keep ordinary password login usable.
cookie_bridge.blocked = True
at = authenticated("synthetic-incheon", configured, remember=True)
assert at.session_state["_auth_principal"].branch == "인천점"
assert "_remember_token" not in at.session_state
assert any("쿠키를 저장하지 못했습니다" in i.value for i in at.info)
calls = remembered.restore_calls
run_ok(at)
assert remembered.restore_calls == calls and at.get("file_uploader")
cookie_bridge.blocked = False
remembered.fail_issue = True
at = authenticated("synthetic-incheon", configured, remember=True)
assert at.session_state["_auth_principal"].branch == "인천점" and "_remember_token" not in at.session_state
assert any("기기 저장에 실패했습니다" in i.value for i in at.info)
remembered.fail_issue = False
remember_login.RememberStore = real_remember_store
remember_browser.render = real_cookie_render
history.HistoryStore = real_history_store
access.AccountStore = real_account_store
print(f"Streamlit {streamlit.__version__}: reconciliation, branch access, media cleanup and remembered login reconnect/revocation/stale ACK/blocked-cookie fallback passed")
