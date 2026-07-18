import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO
import re

st.set_page_config(page_title="병원 정산 3-Way 대사 시스템", layout="wide")

st.title("📊 병원 정산 3-Way 대사 시스템")
st.markdown(
    "한솔페이 · 일일마감 · 차트마감을 비교하여 **결제수단별 상세 차이**와 "
    "**환불 반영 불일치**를 자동으로 찾아냅니다."
)

# =============================================================================
# 1. 파일 로딩 (실무 파일 포맷 견고 처리)
#    - CSV(utf-8/cp949), 진짜 xlsx, 진짜 xls
#    - HTML이 .xls로 위장된 VAN 내보내기(섹타나인/KOVAN 등) → read_html
#    - 비밀번호로 암호화된 xls(베가스 차트 내보내기 옵션) → msoffcrypto 복호화
# =============================================================================

# 실무에서 자주 쓰이는 엑셀 기본/공용 암호. 사용자가 별도 비밀번호를 입력하면 우선 사용.
_DEFAULT_XLS_PASSWORDS = ["VelvetSweatshop", ""]


def _read_csv_any(raw):
    last_err = None
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            return pd.read_csv(BytesIO(raw), encoding=enc)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err


def _read_html_table(raw):
    """HTML 표(위장 xls) 중 가장 열이 많은 표를 선택."""
    tables = pd.read_html(BytesIO(raw))
    if not tables:
        raise ValueError("HTML에서 표를 찾지 못했습니다.")
    return max(tables, key=lambda d: d.shape[1])


def _decrypt_xls(raw, password):
    """msoffcrypto로 암호화 xls 복호화 → 평문 바이트."""
    import msoffcrypto  # 지연 import (미설치 환경 대비)

    office = msoffcrypto.OfficeFile(BytesIO(raw))
    office.load_key(password=password)
    out = BytesIO()
    office.decrypt(out)
    out.seek(0)
    return out.read()


def load_data(file, password=None):
    """업로드 파일을 포맷 자동 감지하여 DataFrame으로 반환."""
    name = file.name.lower()
    raw = file.read()
    file.seek(0)

    # 1) CSV
    if name.endswith(".csv"):
        return _read_csv_any(raw)

    head = raw[:2048].lstrip().lower()

    # 2) HTML이 .xls/.xlsx로 위장된 경우 (VAN사 거래내역 표준 내보내기)
    if head.startswith(b"<") or b"<table" in head or b"<html" in head:
        return _read_html_table(raw)

    # 3) 진짜 xlsx (ZIP 시그니처)
    if raw[:2] == b"PK":
        return pd.read_excel(BytesIO(raw), engine="openpyxl")

    # 4) OLE/BIFF (진짜 xls, 암호화 가능성 있음)
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        try:
            return pd.read_excel(BytesIO(raw), engine="xlrd")
        except Exception as e:  # noqa: BLE001 — 암호화 여부 판별
            if "encrypt" not in str(e).lower():
                raise
            passwords = ([password] if password else []) + _DEFAULT_XLS_PASSWORDS
            last_err = e
            for pw in passwords:
                try:
                    plain = _decrypt_xls(raw, pw)
                    # 복호화 결과가 다시 HTML/xlsx일 수도 있으므로 재귀적으로 판별
                    if plain[:2] == b"PK":
                        return pd.read_excel(BytesIO(plain), engine="openpyxl")
                    return pd.read_excel(BytesIO(plain), engine="xlrd")
                except Exception as de:  # noqa: BLE001
                    last_err = de
            raise ValueError(
                f"'{file.name}' 파일이 비밀번호로 보호되어 있습니다. "
                "좌측 사이드바에 열기 비밀번호를 입력하거나, 베가스에서 "
                "**비밀번호 없이 다시 내보내기** 후 업로드해 주세요."
            ) from last_err

    # 5) 최후의 수단
    return pd.read_excel(BytesIO(raw))


# =============================================================================
# 2. 공통 정제 유틸
# =============================================================================

def clean_money(x):
    if pd.isna(x):
        return 0
    try:
        return int(round(float(str(x).replace(",", "").replace(" ", "").replace("원", ""))))
    except Exception:  # noqa: BLE001
        return 0


def clean_no(x):
    """차트번호·단일 승인번호에서 숫자만 추출 (소수점/문자 제거)."""
    if pd.isna(x) or str(x).strip() == "" or str(x).strip().lower() == "nan":
        return "-"
    val = str(x).split(".")[0]
    digits = re.sub(r"\D", "", val)
    return digits if digits else "-"


# 승인번호는 카드 6~8자리, 현금영수증 9자리 등 길이가 다양하다.
# 과거 \b\d{8}\b 정규식은 6·7자리 카드 승인을 전부 놓쳐 매칭률을 크게 떨어뜨렸다.
_APPR_TOKEN = re.compile(r"\d{5,12}")


def extract_appr_list(text):
    """콤마·공백·문자로 구분된 승인번호 문자열에서 승인번호 토큰 리스트를 추출."""
    if pd.isna(text):
        return []
    return _APPR_TOKEN.findall(str(text))


# =============================================================================
# 3. 소스별 전처리
# =============================================================================

def normalize_hansol(df):
    """한솔(VAN) 거래내역 정제.

    - 카드 매출(K/S=='S')과 현금영수증(K/S=='K') 분리
    - 승인거절/실패 행 제외  (예: 거래상태='승인거절')
    - 취소/환불 행은 원승인과 상계(net)
    반환: 정제된 카드 거래 DataFrame(승인번호, 금액, 부호),
          진단용 메타(dict)
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    meta = {"declined": [], "canceled": [], "raw_rows": len(df)}

    if "금액" not in df.columns:
        return pd.DataFrame(columns=["승인번호", "금액"]), meta
    df["금액"] = df["금액"].apply(clean_money)

    if "승인번호" in df.columns:
        df["승인번호"] = df["승인번호"].apply(clean_no)
    else:
        df["승인번호"] = "-"

    # 승인거절/실패 제외: 거래상태에 거절/실패/오류가 있거나, 카드매출인데 승인번호가 없는 행
    status = df.get("거래상태", pd.Series([""] * len(df))).astype(str)
    declined_mask = status.str.contains("거절|실패|오류|취소불가", na=False)
    if "K/S" in df.columns:
        no_appr_card = (df["K/S"].astype(str) == "S") & (df["승인번호"] == "-")
        declined_mask = declined_mask | no_appr_card
    meta["declined"] = df[declined_mask][["승인번호", "금액"]].to_dict("records")
    df = df[~declined_mask].copy()

    # 취소/환불 부호 판정
    cancel_cols = [c for c in ["거래구분", "매입형태", "구분", "결제유형"] if c in df.columns]
    if cancel_cols:
        cancel_text = df[cancel_cols].apply(lambda r: " ".join(map(str, r)), axis=1)
    else:
        cancel_text = pd.Series([""] * len(df), index=df.index)
    is_cancel = cancel_text.str.contains("취소|환불|반품", na=False) | (df["금액"] < 0)
    df["부호"] = np.where(is_cancel, -1, 1)
    df["실금액"] = df["부호"] * df["금액"].abs()
    meta["canceled"] = df[is_cancel][["승인번호", "금액"]].to_dict("records")

    # 카드 / 현금 분리
    if "K/S" in df.columns:
        card = df[df["K/S"].astype(str) == "S"].copy()
        cash = df[df["K/S"].astype(str) == "K"].copy()
    else:
        card, cash = df.copy(), df.iloc[0:0].copy()

    meta["card_gross"] = int(card.loc[card["부호"] > 0, "금액"].sum())
    meta["card_cancel"] = int(card.loc[card["부호"] < 0, "금액"].abs().sum())
    meta["card_net"] = int(card["실금액"].sum())
    meta["cash_net"] = int(cash["실금액"].sum())

    card = card[["승인번호", "실금액"]].rename(columns={"실금액": "금액"})
    return card, meta


def normalize_daily(df):
    """일일마감 장부 정제 → 차트번호별 수단 합계."""
    df = df.copy()
    # '내원' 헤더 행 탐지
    header_idx = df[df.apply(lambda r: r.astype(str).str.contains("내원").any(), axis=1)].index
    if len(header_idx) > 0:
        df.columns = df.iloc[header_idx[0]]
        df = df.iloc[header_idx[0] + 1:].reset_index(drop=True)
    df.columns = [str(c).replace("\n", "").strip() for c in df.columns]

    if "성명" in df.columns:
        df = df[df["성명"].notna() & ~df["성명"].astype(str).str.contains("합계|소계|총계", na=False)]
    if "차트번호" in df.columns:
        df["차트번호"] = df["차트번호"].apply(clean_no)
    else:
        df["차트번호"] = "-"

    plat_cols = ["강남언니", "여신티켓", "기타-지역화폐", "나만의닥터"]
    for col in ["카드", "현금", "이체"] + plat_cols:
        df[col] = df[col].apply(clean_money) if col in df.columns else 0
    df["플랫폼"] = df[plat_cols].sum(axis=1)

    if "성명" not in df.columns:
        df["성명"] = ""
    g = df.groupby("차트번호", as_index=False).agg(
        성명=("성명", "first"),
        카드=("카드", "sum"),
        현금=("현금", "sum"),
        이체=("이체", "sum"),
        플랫폼=("플랫폼", "sum"),
    )
    return g


def normalize_chart(df):
    """차트마감 데이터 정제 → 차트번호별 수단 합계 + 승인번호→차트 매핑."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df["차트번호"] = df["차트번호"].apply(clean_no) if "차트번호" in df.columns else "-"

    calc_cols = [c for c in ["비급여(과세총금액)", "비급여(비과세)", "본부금"] if c in df.columns]
    for c in calc_cols:
        df[c] = df[c].apply(clean_money)
    df["수납액"] = df[calc_cols].sum(axis=1) if calc_cols else 0

    method = df.get("결제수단", pd.Series([""] * len(df))).astype(str)
    df["분류"] = "기타"
    df.loc[method.str.contains("카드"), "분류"] = "카드"
    df.loc[method.str.contains("현금"), "분류"] = "현금"
    df.loc[method.str.contains("통장|이체"), "분류"] = "이체"
    df.loc[method.str.contains("강남|여신|닥터|지역화폐|플랫폼"), "분류"] = "플랫폼"

    # 승인번호 → 차트번호 매핑 (수정된 정규식)
    appr_to_chart = {}
    src_col = "승인번호" if "승인번호" in df.columns else ("결제메모" if "결제메모" in df.columns else None)
    if src_col:
        for _, row in df.iterrows():
            for appr in extract_appr_list(row[src_col]):
                appr_to_chart[appr] = row["차트번호"]

    name_col = "이름" if "이름" in df.columns else ("성명" if "성명" in df.columns else None)
    pivot = df.pivot_table(index="차트번호", columns="분류", values="수납액", aggfunc="sum").fillna(0)
    for c in ["카드", "현금", "이체", "플랫폼"]:
        if c not in pivot.columns:
            pivot[c] = 0
    pivot = pivot[["카드", "현금", "이체", "플랫폼"]].reset_index()
    if name_col:
        names = df.groupby("차트번호")[name_col].first()
        pivot["이름"] = pivot["차트번호"].map(names)
    else:
        pivot["이름"] = ""
    return pivot, appr_to_chart


# =============================================================================
# 4. 대사 + 환불/오류 추론 엔진
# =============================================================================

def link_hansol_to_chart(hansol_card, appr_to_chart):
    """한솔 카드 거래를 승인번호로 차트번호에 연결 → 차트번호별 한솔 카드합계."""
    hansol_card = hansol_card.copy()
    hansol_card["차트번호"] = hansol_card["승인번호"].map(appr_to_chart).fillna("(미연결)")
    linked = hansol_card.groupby("차트번호", as_index=False)["금액"].sum().rename(columns={"금액": "한솔카드"})
    unlinked_amt = int(hansol_card.loc[hansol_card["차트번호"] == "(미연결)", "금액"].sum())
    return linked, unlinked_amt


def classify_row(d, c, h, tol=0):
    """세 소스(일마·차트·한솔)의 카드 금액을 비교해 불일치 원인을 추론.

    2-of-3 합의 규칙:
      · 한솔==차트 이면서 일마가 낮음  → 환불이 일일마감에만 반영 (차트·한솔 미반영)  ← 잠실 케이스
      · 일마==한솔 이면서 차트가 높음  → 환불이 차트마감에 미반영 (카드취소는 정상)
      · 일마==차트 이면서 한솔이 높음  → 한솔에 승인거절/취소 미차감 or 이중승인
      · 한솔만 0                       → 한솔 매입 누락 or 승인번호 미연결
      · 셋 다 상이                     → 복합 오류(수기 검토)
    반환: (판정라벨, 심각도, 추정환불액)
    """
    def eq(a, b):
        return abs(a - b) <= tol

    diff_dc = d - c   # 일마 - 차트
    if eq(d, c) and eq(c, h):
        return ("✅ 일치", 0, 0)

    # 한솔 미연결(0)인 경우: 승인번호 매칭 실패 가능성 우선
    if h == 0 and (d > 0 or c > 0):
        return ("🔌 한솔 미연결/누락 (승인번호 매칭 실패 or 매입 누락)", 2, 0)

    if eq(h, c) and d < c:
        return (f"💸 환불 미반영: 일일마감만 환불 반영 (차트·한솔 그대로) · 추정환불 {c - d:,}원", 3, c - d)
    if eq(d, h) and c > h:
        return (f"🩹 차트 환불 미반영: 카드취소·일마는 정상, 차트만 총액 · 추정환불 {c - h:,}원", 3, c - h)
    if eq(d, c) and h > c:
        return (f"🧾 한솔 과다: 승인거절/취소 미차감 or 이중승인 · +{h - c:,}원", 2, 0)
    if eq(h, c) and d > c:
        return (f"➕ 일일마감 과다 입력 · +{d - c:,}원", 2, 0)

    return (f"⚠️ 복합 불일치 (일마 {d:,} / 차트 {c:,} / 한솔 {h:,})", 2, 0)


def reconcile(daily, chart_pivot, hansol_linked):
    """차트번호 기준으로 세 소스를 합쳐 카드 대사 + 판정."""
    d = daily.rename(columns={"성명": "이름_d", "카드": "일마카드", "현금": "일마현금",
                              "이체": "일마이체", "플랫폼": "일마플랫폼"})
    c = chart_pivot.rename(columns={"이름": "이름_c", "카드": "차트카드", "현금": "차트현금",
                                    "이체": "차트이체", "플랫폼": "차트플랫폼"})

    m = pd.merge(d, c, on="차트번호", how="outer")
    m = pd.merge(m, hansol_linked, on="차트번호", how="outer")

    # 이름은 일마 우선, 없으면 차트에서 보완
    name_d = m["이름_d"].fillna("") if "이름_d" in m.columns else pd.Series([""] * len(m), index=m.index)
    name_c = m["이름_c"].fillna("") if "이름_c" in m.columns else pd.Series([""] * len(m), index=m.index)
    m["이름"] = name_d.where(name_d != "", name_c)
    for col in ["일마카드", "차트카드", "한솔카드", "일마현금", "차트현금", "일마이체", "차트이체", "일마플랫폼", "차트플랫폼"]:
        if col not in m.columns:
            m[col] = 0
        m[col] = m[col].fillna(0).round().astype(int)

    judged = m.apply(lambda r: classify_row(r["일마카드"], r["차트카드"], r["한솔카드"]), axis=1)
    m["판정"] = [j[0] for j in judged]
    m["심각도"] = [j[1] for j in judged]
    m["추정환불액"] = [j[2] for j in judged]
    return m


# =============================================================================
# 5. Streamlit UI
# =============================================================================

with st.sidebar:
    st.header("⚙️ 옵션")
    xls_password = st.text_input("암호화 엑셀 열기 비밀번호 (선택)", type="password",
                                 help="베가스 차트 파일이 비밀번호로 보호된 경우 입력")
    tol = st.number_input("허용 오차(원)", min_value=0, value=0, step=100,
                          help="반올림·수수료 등으로 인한 미세 차이를 무시할 금액")

st.info("👇 3개의 파일을 업로드한 후 **[분석 시작]** 버튼을 눌러주세요.")
col1, col2, col3 = st.columns(3)
with col1:
    file_hansol = st.file_uploader("📥 1. [한솔] 한솔페이 내역", type=["csv", "xlsx", "xls"])
with col2:
    file_daily = st.file_uploader("📥 2. [일마] 일일마감 장부", type=["csv", "xlsx", "xls"])
with col3:
    file_patient = st.file_uploader("📥 3. [차트] 차트마감 데이터", type=["csv", "xlsx", "xls"])

if file_hansol and file_daily and file_patient:
    if st.button("🚀 정산 데이터 분석 시작하기", type="primary"):
        try:
            with st.spinner("파일을 읽고 결제수단별로 대사 중입니다..."):
                df_h = load_data(file_hansol, password=xls_password or None)
                df_d = load_data(file_daily, password=xls_password or None)
                df_p = load_data(file_patient, password=xls_password or None)

                hansol_card, h_meta = normalize_hansol(df_h)
                daily = normalize_daily(df_d)
                chart_pivot, appr_to_chart = normalize_chart(df_p)
                hansol_linked, unlinked_amt = link_hansol_to_chart(hansol_card, appr_to_chart)
                result = reconcile(daily, chart_pivot, hansol_linked)
        except Exception as e:  # noqa: BLE001
            st.error(f"❌ 처리 중 오류: {e}")
            st.stop()

        # ---- 1단계: 총액 대사 (가장 빠른 1차 점검) ----
        st.subheader("① 총액 대사 (하루 전체가 맞는지 먼저 확인)")
        tot_daily_card = int(daily["카드"].sum())
        tot_chart_card = int(chart_pivot["카드"].sum())
        tot_hansol_card = h_meta.get("card_net", 0)
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("일마 카드합계", f"{tot_daily_card:,}")
        mc2.metric("차트 카드합계", f"{tot_chart_card:,}", delta=f"{tot_chart_card - tot_daily_card:,}")
        mc3.metric("한솔 카드순매출", f"{tot_hansol_card:,}", delta=f"{tot_hansol_card - tot_daily_card:,}")
        mc4.metric("한솔 미연결액", f"{unlinked_amt:,}")

        if h_meta.get("declined"):
            st.warning(f"⛔ 한솔에서 **승인거절/무효 {len(h_meta['declined'])}건** 을 합계에서 제외했습니다: "
                       + ", ".join(f"{r['금액']:,}원" for r in h_meta["declined"]))
        if h_meta.get("card_cancel"):
            st.warning(f"↩️ 한솔 카드 **취소·환불 {h_meta['card_cancel']:,}원** 을 상계 처리했습니다.")

        # ---- 2단계: 환불 반영 불일치 (핵심) ----
        st.subheader("② 🔴 환불 반영 불일치 (최우선 검토)")
        refund_issues = result[result["추정환불액"] > 0].sort_values("추정환불액", ascending=False)
        if len(refund_issues):
            st.error(f"환불 반영이 어긋난 건이 **{len(refund_issues)}건** 발견되었습니다. "
                     "일일마감과 차트/한솔의 환불 처리 시점이 달라 생긴 차이입니다.")
            st.dataframe(
                refund_issues[["차트번호", "이름", "판정", "일마카드", "차트카드", "한솔카드", "추정환불액"]],
                use_container_width=True,
            )
        else:
            st.success("환불 반영 불일치는 발견되지 않았습니다.")

        # ---- 3단계: 전체 카드 불일치 ----
        st.subheader("③ 카드 3-Way 불일치 전체")
        mism = result[result["판정"] != "✅ 일치"].sort_values("심각도", ascending=False)
        st.dataframe(
            mism[["차트번호", "이름", "판정", "일마카드", "차트카드", "한솔카드"]],
            use_container_width=True,
        )

        # ---- 4단계: 수단별(현금/이체/플랫폼) 차이 ----
        st.subheader("④ 기타 수단별 차이 (현금·이체·플랫폼)")
        result["현금차이"] = result["일마현금"] - result["차트현금"]
        result["이체차이"] = result["일마이체"] - result["차트이체"]
        result["플랫폼차이"] = result.get("일마플랫폼", 0) - result.get("차트플랫폼", 0)
        other = result[(result["현금차이"] != 0) | (result["이체차이"] != 0) | (result["플랫폼차이"] != 0)]
        st.dataframe(
            other[["차트번호", "이름", "일마현금", "차트현금", "현금차이",
                   "일마이체", "차트이체", "이체차이", "플랫폼차이"]],
            use_container_width=True,
        )

        # ---- 결과 다운로드 ----
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            result.to_excel(writer, sheet_name="대사결과", index=False)
        st.download_button("📥 대사결과 엑셀 다운로드", buf.getvalue(),
                           file_name="reconciliation_result.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
