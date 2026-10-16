"""로컬 Streamlit v1 컴포넌트로 기기 쿠키만 읽고 쓴다."""
from pathlib import Path


def render(command=None):
    from streamlit import runtime
    if runtime.exists() is not True:
        return None
    import streamlit.components.v1 as components

    component = components.declare_component(
        "clinic_pay_remember", path=str(Path(__file__).resolve().parent / "assets" / "remember_cookie"),
    )
    return component(command=command, default=None, key="_remember_browser_component")
