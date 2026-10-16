"""새 Streamlit 개발 앱의 전용 진입점."""
import app


def main():
    """Full B is a separate comparison route; the existing app remains the default."""
    import streamlit as st
    if st.query_params.get('ledger_view') == 'full-web':
        from web_bridge import main as full_web_main
        full_web_main()
    else:
        app.main(development=True)


if __name__ == "__main__":
    main()
