"""기존 링크 연동의 전송 경계. 원본 Google 요청 없이 합성 응답만 사용한다."""
from urllib.error import HTTPError, URLError
from urllib.request import Request
from unittest.mock import MagicMock

import pytest


URL = "https://docs.google.com/spreadsheets/d/SYNTHETIC_SHEET_ID_00000001/export?format=csv&gid=1234"


def mock_opener(monkeypatch, *, final_url=URL, status=200, body=b"header\nsynthetic\n"):
    import urllib.request

    response = MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.geturl.return_value = final_url
    response.read.return_value = body
    opener = MagicMock()
    opener.open.return_value = response
    factory = MagicMock(return_value=opener)
    monkeypatch.setattr(urllib.request, "build_opener", factory)
    return factory, opener, response


def test_link_reader_sends_only_get_with_timeout_and_preserves_bytes(app, monkeypatch):
    factory, opener, _ = mock_opener(
        monkeypatch, final_url="https://doc-0a-sheets.googleusercontent.com/export/synthetic")
    assert app._fetch_url_bytes(URL, timeout=7) == b"header\nsynthetic\n"
    request = opener.open.call_args.args[0]
    assert request.full_url == URL
    assert request.get_method() == "GET" and request.data is None
    assert opener.open.call_args.kwargs == {"timeout": 7}
    # Google의 CSV 다운로드 이동은 기존 GET 요청을 유지한다.
    handler = factory.call_args.args[0]
    redirected = handler.redirect_request(request, None, 302, "Found", {},
        "https://doc-0a-sheets.googleusercontent.com/export/synthetic")
    assert redirected.get_method() == "GET" and redirected.data is None


@pytest.mark.parametrize("url", [
    "http://docs.google.com/spreadsheets/d/synthetic/edit",
    "https://example.com/spreadsheets/d/synthetic/edit",
    "https://docs.google.com.evil.example/spreadsheets/d/synthetic/edit",
    "https://docs.google.com:8443/spreadsheets/d/synthetic/edit",
    "https://user:password@docs.google.com/spreadsheets/d/synthetic/edit",
    "https://docs.google.com/not-a-sheet",
    "https://doc-0a-sheets.googleusercontent.com/export/synthetic",
])
def test_link_reader_rejects_unapproved_input_before_network(app, monkeypatch, url):
    factory, opener, _ = mock_opener(monkeypatch)
    with pytest.raises(PermissionError):
        app._fetch_url_bytes(url)
    factory.assert_not_called()
    opener.open.assert_not_called()


@pytest.mark.parametrize("destination", [
    "https://evil.example/synthetic",
    "http://docs.google.com/spreadsheets/d/synthetic/edit",
    "https://accounts.google.com/ServiceLogin",
    "https://googleusercontent.com.evil.example/export",
    "https://user:password@doc-0a-sheets.googleusercontent.com/export",
])
def test_link_redirect_is_rejected_before_destination_request(app, monkeypatch, destination):
    factory, _, _ = mock_opener(monkeypatch)
    app._fetch_url_bytes(URL)
    handler = factory.call_args.args[0]
    with pytest.raises(PermissionError):
        handler.redirect_request(Request(URL), None, 302, "Found", {}, destination)


def test_link_reader_rechecks_final_host_before_reading(app, monkeypatch):
    _, _, response = mock_opener(monkeypatch, final_url="https://evil.example/synthetic")
    with pytest.raises(PermissionError):
        app._fetch_url_bytes(URL)
    response.read.assert_not_called()


@pytest.mark.parametrize("status, expected", [
    (401, PermissionError), (403, PermissionError), (404, LookupError), (500, ConnectionError),
])
def test_link_http_errors_hide_upstream_url_and_body(app, monkeypatch, status, expected):
    _, opener, _ = mock_opener(monkeypatch)
    opener.open.side_effect = HTTPError(
        URL, status, "SYNTHETIC_PRIVATE_UPSTREAM_ERROR", {}, None)
    with pytest.raises(expected) as caught:
        app._fetch_url_bytes(URL)
    assert "SYNTHETIC" not in str(caught.value) and URL not in str(caught.value)


@pytest.mark.parametrize("error", [
    TimeoutError("SYNTHETIC_UPSTREAM_TIMEOUT"),
    URLError("SYNTHETIC_UPSTREAM_NETWORK"),
    ConnectionError("SYNTHETIC_UPSTREAM_CONNECTION"),
])
def test_link_connection_errors_are_masked(app, monkeypatch, error):
    _, opener, _ = mock_opener(monkeypatch)
    opener.open.side_effect = error
    with pytest.raises(ConnectionError) as caught:
        app._fetch_url_bytes(URL)
    assert "SYNTHETIC" not in str(caught.value)


def test_link_reader_rejects_non_200_success(app, monkeypatch):
    _, _, response = mock_opener(monkeypatch, status=204)
    with pytest.raises(ConnectionError):
        app._fetch_url_bytes(URL)
    response.read.assert_not_called()
