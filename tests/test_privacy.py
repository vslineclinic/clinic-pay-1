"""개인정보 경계는 합성 자료와 대체 전송 객체만으로 검증한다."""
import json
import sys
import types
from datetime import date
from unittest.mock import Mock

import pandas as pd
import pytest


def test_shared_password_is_not_a_branch_or_hq_login(app, monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {"APP_PASSWORD": "synthetic-access-password"})
    monkeypatch.setattr(app, "_get_account_store", lambda: None)
    app.st.session_state.update(_access_digest="old-session", done=True, _ai_data="old-private-result")
    assert app.require_app_access() is False
    assert "_auth_principal" not in app.st.session_state
    assert "_access_digest" not in app.st.session_state and "done" not in app.st.session_state


def test_existing_sheet_link_defaults_and_explicit_empty_override(app, monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {})
    links = app.get_clinic_daily_sheets()
    assert set(links) == {"인천점", "잠실점", "엔디어트", "강남점", "일산점", "압구정"}
    assert sum(len(value) if isinstance(value, (list, tuple)) else 1 for value in links.values()) == 7
    assert links["엔디어트"][0][0] == "2026-06-12"
    assert links["엔디어트"][1][0] == ""
    monkeypatch.setattr(app.st, "secrets", {"clinic_daily_sheets": {}})
    assert app.get_clinic_daily_sheets() == {}


def test_private_sheet_requires_exact_tab_and_preserves_headers(app, monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {"gcp_service_account": {"type": "service_account"}})
    calls = []
    def get_json(path, timeout=20):
        calls.append(path)
        if "/values/" in path:
            return {"values": [["내원순서", "차트번호", "성명", "카드"], [1, "901", "합성환자", 900]]}
        return {"sheets": [{"properties": {"title": "26.06.02"}}]}
    monkeypatch.setattr(app, "_sheets_api_get", get_json)
    sid = "SYNTHETIC_SHEET_ID_00000001"
    raw, tab = app.load_gsheet_daily(sid, date(2026, 6, 2))
    assert tab == "26.06.02" and raw.iloc[0, 3] == "카드"
    with pytest.raises(LookupError):
        app.load_gsheet_daily(sid, date(2026, 6, 3))
    assert len([p for p in calls if "/values/" in p]) == 1


def test_private_sheet_auth_error_never_uses_public_fallback(app, monkeypatch):
    monkeypatch.setattr(app.st, "secrets", {"gcp_service_account": {"type": "service_account"}})
    monkeypatch.setattr(app, "_sheets_api_get", Mock(side_effect=PermissionError("denied")))
    public = Mock(side_effect=AssertionError("public fallback prohibited"))
    monkeypatch.setattr(app, "_fetch_url_bytes", public)
    with pytest.raises(PermissionError):
        app.load_gsheet_daily("SYNTHETIC_SHEET_ID_00000001", date(2026, 6, 2))
    public.assert_not_called()


def test_sheets_transport_uses_readonly_credentials_and_hides_errors(app, monkeypatch):
    from unittest.mock import MagicMock
    info = {"type": "service_account", "private_key": "SYNTHETIC_PRIVATE_KEY"}
    monkeypatch.setattr(app.st, "secrets", {"gcp_service_account": info})
    factory = Mock(return_value=object())
    oauth = types.ModuleType("google.oauth2")
    oauth.service_account = types.SimpleNamespace(Credentials=types.SimpleNamespace(from_service_account_info=factory))
    transport = types.ModuleType("google.auth.transport.requests")
    session = MagicMock()
    session.__enter__.return_value = session
    response = Mock(status_code=200)
    response.json.return_value = {"sheets": []}
    session.get.return_value = response
    transport.AuthorizedSession = Mock(return_value=session)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", transport)
    assert app._sheets_api_get("synthetic?fields=sheets.properties.title") == {"sheets": []}
    assert factory.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    assert session.get.call_args.args[0].startswith("https://sheets.googleapis.com/v4/spreadsheets/")
    response.status_code = 403
    with pytest.raises(PermissionError):
        app._sheets_api_get("synthetic")
    factory.side_effect = ValueError("SYNTHETIC_PRIVATE_KEY")
    with pytest.raises(ConnectionError) as caught:
        app._sheets_api_get("synthetic")
    assert "SYNTHETIC_PRIVATE_KEY" not in str(caught.value)


def test_encrypted_file_only_tries_explicitly_configured_password(app, monkeypatch):
    import io
    attempts = []
    class Office:
        def __init__(self, raw):
            pass
        def is_encrypted(self):
            return True
        def load_key(self, password):
            attempts.append(password)
            raise ValueError("synthetic encrypted input")
    monkeypatch.setattr(app.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(app.importlib, "import_module", lambda name: types.SimpleNamespace(OfficeFile=Office))
    monkeypatch.setattr(app, "_read_excel_auto", Mock(side_effect=ValueError("not plain Excel")))
    def source():
        f = io.BytesIO(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        f.name = "synthetic.xlsx"
        return f
    monkeypatch.setattr(app.st, "secrets", {})
    with pytest.raises(ValueError):
        app.load_file(source())
    assert attempts == []
    monkeypatch.setattr(app.st, "secrets", {"FILE_PASSWORD": "synthetic-file-password"})
    with pytest.raises(ValueError):
        app.load_file(source())
    assert attempts == ["synthetic-file-password"]


def test_private_payload_masks_ids_but_keeps_same_numeric_amount(app):
    daily = pd.DataFrame({"차트번호": ["901"], "성명": ["합성환자긴이름가나다라마바사"]})
    patient = pd.DataFrame({"차트번호": ["901"], "이름": ["합성환자긴이름가나다라마바사"],
                            "승인번호목록": [["123456789"]]})
    hansol = pd.DataFrame({"승인번호": ["123456789"], "카드번호": ["0000111122223333"]})
    short_name = "합성환자긴이름가나다라마바사"
    raw = (f"[일마front-only]\n차트#|이름|금액|내원순서\n901|{short_name}|901|1\n"
           f"[원시거래행]\n·차트#901 {short_name}\n  차트|카드|+901|승인123456789\n"
           "시각|금액|카드사|말미|승인번호|동일금액일마환자|오타패턴후보(×10/÷10)\n"
           "12:00:00|901|테스트|*3333|123456789|합성환자긴이름가나다라마바사?|-")
    text, question, token_map = app.prepare_ai_payload(
        raw, "차트901 합성환자긴이름가나다라마바사 승인123456789 검토", daily, patient, hansol)
    assert "P001|N001|901|1" in text
    assert "|+901|승인A001" in text
    for value in ["합성환자", "123456789", "*3333", "차트901", "차트#901"]:
        assert value not in text + question
    assert token_map["P001"] == "차트901"
    assert app.restore_ai_identifiers("P001 N001 A001", token_map).startswith("차트901 ")


def test_private_payload_covers_real_text_builder_sections(app):
    from factories import mismatch_three_files
    h_raw, d_raw, p_raw = mismatch_three_files()
    h = app.parse_hansol(h_raw)
    d, refund = app.parse_daily(d_raw)
    p_raw.iloc[2, -1] = "200222 합성메모사람 010-1111-2222"
    p = app.parse_patient(p_raw)
    totals = app.compute_totals(h, d, refund, p)
    channels = app.compute_channel_recon(totals)
    verif = app.build_verification(p, d, refund)
    raw = app.build_ai_text(h, d, refund, p, channels, pd.DataFrame(), h, d, {}, totals, verif)
    text, _, _ = app.prepare_ai_payload(raw, "", d, p, h)
    for name in d["성명"]:
        assert name not in text
    for appr in h["승인번호"]:
        assert str(appr) not in text
    assert "35,000" in text and "30,000" in text
    assert "합성메모사람" not in raw + text and "010-1111-2222" not in raw + text


def test_refund_only_identifiers_and_long_approvals_remain_distinct(app):
    empty = pd.DataFrame()
    refund = pd.DataFrame({"차트번호": ["909"], "성명": ["합성환불환자"]})
    patient = pd.DataFrame({"승인번호목록": [["112345678", "212345678"]]})
    text, question, mapping = app.prepare_ai_payload(
        "차트#|성명|카드|현금|이체|플랫폼\n909|합성환불환자|10,000|0|0|0\n"
        "승인112345678 승인212345678", "합성환불환자 차트번호909", empty, patient, empty, refund)
    assert "합성환불환자" not in text + question
    assert "승인A001 승인A002" in text
    assert mapping["A001"] != mapping["A002"]


def test_payload_keeps_c1_header_masks_approval_lists_and_preserves_money(app):
    daily = pd.DataFrame({"차트번호": ["901", "902"], "성명": ["합성갑", "합성을"]})
    patient = pd.DataFrame({"승인번호목록": [["11223344", "55667788"]]})
    raw = ("[검증]\n구분|차트#|환자|금액|결제수단|동일금액상대\n"
           "[일일마감에만 존재]|901|합성갑|901|카드:901|차트 합성을(차트902) — 동일건 의심\n"
           "[원시거래행]\n차트|카드|+901|승인11223344,55667788\n"
           "현금30,000≠차트35,000 / 현금901≠차트901 / 한솔 901 vs 차트901")
    text, _, mapping = app.prepare_ai_payload(raw, "", daily, patient, pd.DataFrame())
    assert "[일일마감에만 존재]|P001|N001|901" in text
    assert "차트#P002" in text
    assert "승인A001,A002" in text
    assert "현금30,000≠차트35,000 / 현금901≠차트901 / 한솔 901 vs 차트901" in text
    assert app.restore_ai_identifiers("N001의 P001은 A001을 확인", mapping) == "합성갑의 차트901은 승인11223344을 확인"


def test_names_with_shared_prefix_stay_distinct_and_five_digit_card_tail_is_hidden(app):
    first, second = "합성긴이름가나다라마다", "합성긴이름가나다라마"
    daily = pd.DataFrame({"차트번호": ["901", "902"], "성명": [first, second]})
    raw = f"환자|차이금액|단서\n{first}|1,000|말미87654\n{second}|2,000|말미7654"
    text, _, mapping = app.prepare_ai_payload(raw, "", daily, pd.DataFrame(), pd.DataFrame())
    assert "N001|1,000" in text and "N002|2,000" in text
    assert "87654" not in text and "7654" not in text
    assert mapping["N001"] == first and mapping["N002"] == second


def _fake_genai(monkeypatch, responses):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return types.SimpleNamespace(text=result)
    genai = types.ModuleType("google.genai")
    genai.Client = lambda **kwargs: types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate))
    genai.types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw, ThinkingConfig=lambda **kw: kw)
    google = types.ModuleType("google")
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    return calls


def test_ai_cache_is_session_scoped_expires_and_tracks_model_prompt(app, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    monkeypatch.setattr(app, "_gemini_wait", lambda **kw: None)
    calls = _fake_genai(monkeypatch, ["first", "model-change", "prompt-change", "expired", "new-session"])
    assert app.run_gemini("fake", "private-input", model="model-a", allow_fallback=False) == ("first", "model-a")
    assert app.run_gemini("fake", "private-input", model="model-a", allow_fallback=False) == ("first", "model-a")
    assert len(calls) == 1
    assert app.run_gemini("fake", "private-input", model="model-b", allow_fallback=False)[0] == "model-change"
    monkeypatch.setattr(app, "AI_SYSTEM", app.AI_SYSTEM + " revised")
    assert app.run_gemini("fake", "private-input", model="model-b", allow_fallback=False)[0] == "prompt-change"
    clock[0] += app.AI_CACHE_TTL_SECONDS + 1
    assert app.run_gemini("fake", "private-input", model="model-b", allow_fallback=False)[0] == "expired"
    app.st.session_state.clear()
    assert app.run_gemini("fake", "private-input", model="model-b", allow_fallback=False)[0] == "new-session"


def test_ai_cache_keeps_actual_fallback_model(app, monkeypatch):
    monkeypatch.setattr(app, "_gemini_wait", lambda **kw: None)
    calls = _fake_genai(monkeypatch, [RuntimeError("404 not found"), "fallback-report"])
    monkeypatch.setattr(app, "GEMINI_FALLBACK_ORDER", ["model-a", "model-b"])
    first = app.run_gemini("fake", "private-input", model="model-a")
    again = app.run_gemini("fake", "private-input", model="model-a")
    assert first == again == ("fallback-report", "model-b")
    assert len(calls) == 2
