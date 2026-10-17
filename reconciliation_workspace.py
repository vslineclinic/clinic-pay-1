"""Native reconciliation controls presented in the full B workspace theme.

The calculation forms keep their native Streamlit upload and download controls.
This module owns only navigation and presentation; it does not read clinic data.
"""
from html import escape
from datetime import date
from pathlib import Path


PAGES = frozenset({
    "ledger", "workbench", "period", "closing", "history",
    "reconciliation-history", "analysis", "activity",
})

PAGE_LABELS = {
    "ledger": "수납 원장",
    "workbench": "하루 정밀 분석",
    "period": "기간 분석",
    "closing": "일일 마감",
    "history": "기간별 기록",
    "reconciliation-history": "정산 기록",
    "analysis": "오차 분석",
    "activity": "변경 이력",
}


def resolve_page(value, fallback="workbench"):
    """Accept only an internal workspace name, including for URL parameters."""
    if isinstance(value, str) and value in PAGES:
        return value
    return fallback if isinstance(fallback, str) and fallback in PAGES else "workbench"


def current_page():
    import streamlit as st

    return resolve_page(
        st.query_params.get("workspace"),
        st.session_state.get("_full_web_page", "workbench"),
    )


def navigate(page):
    """Keep all workspace navigation on the B URL and the current login session."""
    import streamlit as st

    page = resolve_page(page)
    next_mode = "period" if page == "period" else "day" if page in ("workbench", "analysis") else None
    previous_mode = st.session_state.get("_full_web_analysis_mode")
    if previous_mode not in ("period", "day"):
        previous_mode = ("period" if st.session_state.get("period_done") else
                         "day" if st.session_state.get("done") else
                         "period" if current_page() == "period" else "day")
    wrong_results = (next_mode == "period" and st.session_state.get("done") or
                     next_mode == "day" and st.session_state.get("period_done"))
    if next_mode and (previous_mode != next_mode or wrong_results):
        import app
        app._reset_workspace()
        context = st.session_state.get("_full_web_context")
        if context:
            st.session_state["_ledger_selection"] = dict(context)
    if next_mode:
        st.session_state["_full_web_analysis_mode"] = next_mode
    st.session_state["_full_web_page"] = page
    st.query_params["ledger_view"] = "full-web"
    st.query_params["workspace"] = page
    st.rerun()


def _seed_context(principal):
    """Initialize native widgets from the server-validated B ledger selection."""
    import streamlit as st

    context = st.session_state.get("_full_web_context")
    if not isinstance(context, dict):
        return
    branch = context.get("branch")
    try:
        business_date = date.fromisoformat(context["business_date"])
    except (ValueError, TypeError, KeyError):
        return
    from branch_access import CANONICAL_BRANCHES
    if branch not in CANONICAL_BRANCHES:
        return
    st.session_state.setdefault("upload_branch", branch)
    st.session_state.setdefault("history_branch", branch)
    st.session_state.setdefault("gs_date", business_date)
    st.session_state.setdefault("history_month", business_date)
    if "revision" in context:
        st.session_state.setdefault("_ledger_selection", dict(context))
        st.session_state.setdefault("daily_mode", "수납원장")
    import app
    configured = app.get_authorized_daily_sheets(principal, revalidate=False)
    if app.sheet_entry_configured(configured.get(branch)):
        st.session_state.setdefault("gs_branch", branch)
        st.session_state.setdefault("per_branch", branch)


def _nav_item(label, page, selected, *, child=False):
    import streamlit as st

    with st.container(key=f"fullweb_{'child' if child else 'nav'}_{page}"):
        if st.button(label, key=f"fullweb_go_{page}",
                     type="primary" if selected else "secondary",
                     use_container_width=True):
            navigate(page)


def render_shell(principal, has_results=False):
    """Render B navigation and return the existing app's workspace and mode."""
    import streamlit as st

    page = current_page()
    target_mode = "period" if page == "period" else "day" if page in ("workbench", "analysis") else None
    previous_mode = st.session_state.get("_full_web_analysis_mode")
    wrong_results = (target_mode == "period" and st.session_state.get("done") or
                     target_mode == "day" and st.session_state.get("period_done"))
    if target_mode and (wrong_results or previous_mode in ("day", "period") and previous_mode != target_mode):
        # URL changes and B web-menu requests also enter here without a native click.
        navigate(page)
    st.session_state["_full_web_page"] = page
    if target_mode:
        st.session_state["_full_web_analysis_mode"] = target_mode
    _seed_context(principal)
    label = PAGE_LABELS[page]
    role = "총괄 관리자" if principal.is_admin else "지점 계정"
    branch = getattr(principal, "branch", "")
    if branch == "all" or not branch:
        context = st.session_state.get("_full_web_context") or {}
        branch = context.get("branch") or st.session_state.get("_full_web_branch") or "전체 지점"
    username = escape(str(principal.username))
    branch = escape(str(branch))

    css = (Path(__file__).resolve().parent / "assets" /
           "reconciliation-workspace.css").read_text()
    st.markdown("<style>" + css + "</style>", unsafe_allow_html=True)

    with st.sidebar:
        with st.container(key="fullweb_sidebar"):
            st.markdown(
                '<div class="fullweb-brand" aria-label="clinic pay">'
                '<span class="fullweb-brand-icon" aria-hidden="true">c<span>+</span></span>'
                '<span>clinic<span class="fullweb-brand-pay">pay</span>'
                '<small>CLINIC OPERATIONS</small></span></div>'
                '<div class="fullweb-workspace-chip"><span class="fullweb-workspace-avatar">B</span>'
                f'<div>{branch}<small>수납 · 정산 관리</small></div></div>'
                '<div class="fullweb-app-switch"><a href="https://pay.bwdot.com" '
                'target="_blank" rel="noopener">지출앱<small>별도 로그인 ↗</small></a>'
                '<span class="selected">정산앱<small>수납 · 마감 대조</small></span></div>'
                '<div class="fullweb-nav-label">정산 관리</div>',
                unsafe_allow_html=True,
            )

            _nav_item("▤  수납 원장", "ledger", page == "ledger")
            _nav_item("⊞  대조 작업대", "workbench",
                      page in ("workbench", "period"))
            if page in ("workbench", "period", "analysis"):
                # Distinct widget keys keep the parent and daily child accessible.
                with st.container(key="fullweb_subnav"):
                    if st.button("하루 정밀 분석", key="fullweb_go_daily",
                                 type="primary" if page == "workbench" else "secondary",
                                 use_container_width=True):
                        navigate("workbench")
                    _nav_item("기간 분석", "period", page == "period", child=True)
            _nav_item("◷  일일 마감", "closing", page == "closing")
            _nav_item("▦  기간별 기록", "history", page == "history")
            _nav_item("▣  정산 기록", "reconciliation-history", page == "reconciliation-history")
            _nav_item("⌕  오차 분석", "analysis", page == "analysis")
            st.markdown('<div class="fullweb-nav-divider"></div>', unsafe_allow_html=True)
            _nav_item("◇  변경 이력", "activity", page == "activity")

            with st.container(key="fullweb_sidebar_bottom"):
                st.markdown(
                    '<div class="fullweb-safe-note"><span aria-hidden="true">◈</span>'
                    '<div>자료는 한곳에서 대조<small>파일 업로드 · 차이 확인 · 결과 저장</small></div></div>'
                    '<div class="fullweb-profile"><span class="fullweb-profile-avatar">관</span>'
                    f'<div>{username}<small>{role}</small></div></div>',
                    unsafe_allow_html=True,
                )
                if st.button("로그아웃", key="fullweb_logout", use_container_width=True):
                    import app
                    app._reset_workspace(logout=True)
                    st.rerun()

    title = "마감 대조 결과" if page == "workbench" and has_results else label
    descriptions = {
        "period": "기간 자료를 날짜별로 비교하고, 확인할 날과 거래를 살펴보세요.",
        "reconciliation-history": "지점·월별로 저장된 정산 결과와 차이를 확인하세요.",
        "analysis": "채널과 환자별 차이의 근거를 확인하고 AI 설명을 함께 검토하세요.",
    }
    description = descriptions.get(
        page, "단말기 마감·일일마감·베가스 수납 파일을 비교하고 차이를 확인하세요.")
    st.markdown(
        '<div class="fullweb-topbar"><span>진료 운영 <i>/</i> '
        f'<strong>{escape(label)}</strong></span><span class="fullweb-topbar-status">'
        '수납 · 정산 관리 <b>B</b></span></div>'
        '<div class="fullweb-page-heading"><span>RECONCILIATION WORKSPACE</span>'
        f'<h1>{escape(title)}</h1><p>{escape(description)}</p></div>',
        unsafe_allow_html=True,
    )
    workspace = "정산 기록" if page == "reconciliation-history" else "마감 대조"
    return workspace, "기간 분석" if page == "period" else "하루 정밀 분석"
