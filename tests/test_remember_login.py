"""가짜 Sheets와 합성 계정으로 장기 로그인 토큰의 서버 검증을 확인한다."""
import copy
import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from branch_access import ACCOUNT_HEADERS, AccountStore, Principal, AccessError


HEADERS = ["device_id", "username", "account_revision", "token_hash", "issued_at", "expires_at", "revoked_at"]
REVISION = "11111111-1111-4111-8111-111111111111"
HQ_REVISION = "22222222-2222-4222-8222-222222222222"
NEXT_REVISION = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)
PASSWORD_HASH = "pbkdf2-sha256:100000:000102030405060708090a0b0c0d0e0f:0f80ecb32361e1f1e6f3bee07f74bd853216c8c4c3eeacc5149ecaa709d2c512"


class Sheets:
    def __init__(self):
        self.tables = {
            "계정": [ACCOUNT_HEADERS[:],
                ["synthetic-staff", "branch", "인천점", PASSWORD_HASH, "1", REVISION, NOW.isoformat()],
                ["synthetic-hq", "admin", "all", PASSWORD_HASH, "1", HQ_REVISION, NOW.isoformat()]],
            "로그인기기": [HEADERS[:]],
        }
        self.calls = []
        self.updated_rows = 1

    def _request(self, method, suffix, **kwargs):
        self.calls.append((method, suffix, copy.deepcopy(kwargs)))
        tab = unquote(suffix).split("'")[1]
        if method == "GET":
            rows = copy.deepcopy(self.tables[tab])
            for row in rows:
                while row and row[-1] == "":
                    row.pop()  # Sheets omits trailing empty revoked_at cells.
            return {"values": rows}
        assert method == "POST" and ":append" in suffix
        self.tables[tab].extend(copy.deepcopy(kwargs["json"]["values"]))
        return {"updates": {"updatedRows": self.updated_rows}}


@pytest.fixture
def backend(monkeypatch):
    import remember_login
    monkeypatch.setattr(remember_login, "_now", lambda: NOW)
    sheets = Sheets()
    accounts = AccountStore(sheets)
    remember = remember_login.RememberStore(sheets, accounts)
    principal = Principal("synthetic-staff", "branch", "인천점", REVISION)
    return remember_login, sheets, accounts, remember, principal


def test_remember_module_is_available():
    assert importlib.util.find_spec("remember_login") is not None


def test_issue_stores_only_token_hash_and_restores_server_account(backend):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    row = sheets.tables["로그인기기"][-1]
    assert len(token.split(".")) == 2 and len(token) == 80
    assert row[:3] == [token.split(".")[0], principal.username, principal.revision]
    assert row[3] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert row[4:] == [NOW.isoformat(), (NOW + timedelta(days=180)).isoformat(), ""]
    assert token not in repr(sheets.tables)
    assert "synthetic-password" not in repr(sheets.tables["로그인기기"])
    assert remember.restore(token) == principal
    assert module.REMEMBER_MAX_AGE_SECONDS == 180 * 86400
    post = [kwargs for method, _, kwargs in sheets.calls if method == "POST"]
    assert post[0]["params"] == {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}


def test_each_device_gets_an_independent_random_secret(backend):
    _, sheets, _, remember, principal = backend
    first, second = remember.issue(principal), remember.issue(principal)
    assert first != second
    assert first.split(".")[0] != second.split(".")[0]
    remember.revoke(first)
    assert remember.restore(second) == principal
    assert len(sheets.tables["로그인기기"]) == 4


@pytest.mark.parametrize("token", [None, "", "raw-password", "x" * 10000, "11111111-1111-4111-8111-111111111111." + "!" * 43])
def test_malformed_token_is_rejected_before_any_network(backend, token):
    module, sheets, _, remember, _ = backend
    before = len(sheets.calls)
    with pytest.raises(module.RememberError) as error:
        remember.restore(token)
    assert str(error.value) == module.REMEMBER_ERROR_MESSAGE
    assert len(sheets.calls) == before


def test_tampered_or_unknown_token_has_fixed_safe_error(backend):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    changed = token[:-1] + ("A" if token[-1] != "A" else "B")
    for forged in [changed, "00000000-0000-4000-8000-000000000000." + token.split(".")[1]]:
        with pytest.raises(module.RememberError) as error:
            remember.restore(forged)
        assert str(error.value) == module.REMEMBER_ERROR_MESSAGE
        assert forged not in str(error.value)


def test_account_revision_change_or_deactivation_invalidates_copied_sessions(backend):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    assert remember.restore(token) == principal
    row = sheets.tables["계정"][1][:]
    row[4], row[5] = "0", NEXT_REVISION
    sheets.tables["계정"].append(row)
    for _ in range(2):
        with pytest.raises(module.RememberError):
            remember.restore(token)


def test_active_password_or_branch_change_requires_new_password_login(backend):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    row = sheets.tables["계정"][1][:]
    row[2], row[5] = "잠실점", NEXT_REVISION
    sheets.tables["계정"].append(row)
    with pytest.raises(module.RememberError):
        remember.restore(token)


def test_restore_uses_current_server_role_and_branch_not_browser_claims(backend):
    _, _, _, remember, principal = backend
    restored = remember.restore(remember.issue(principal))
    assert restored.role == "branch" and restored.branch == "인천점"
    assert not restored.is_admin


def test_expiry_is_absolute_and_cannot_slide_with_activity(backend, monkeypatch):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    initial = copy.deepcopy(sheets.tables["로그인기기"])
    monkeypatch.setattr(module, "_now", lambda: NOW + timedelta(days=180) - timedelta(seconds=1))
    assert remember.restore(token) == principal
    assert sheets.tables["로그인기기"] == initial
    monkeypatch.setattr(module, "_now", lambda: NOW + timedelta(days=180))
    with pytest.raises(module.RememberError):
        remember.restore(token)


def test_future_issued_token_is_rejected(backend, monkeypatch):
    module, _, _, remember, principal = backend
    token = remember.issue(principal)
    monkeypatch.setattr(module, "_now", lambda: NOW - timedelta(seconds=1))
    with pytest.raises(module.RememberError):
        remember.restore(token)


def test_logout_append_revocation_invalidates_other_session_and_replay(backend):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    issued = sheets.tables["로그인기기"][-1][:]
    assert remember.restore(token) == principal
    remember.revoke(token)
    revoked = sheets.tables["로그인기기"][-1]
    assert revoked[:6] == issued[:6]
    assert revoked[6] == NOW.isoformat()
    with pytest.raises(module.RememberError):
        remember.restore(token)
    count = len(sheets.tables["로그인기기"])
    remember.revoke(token)
    assert len(sheets.tables["로그인기기"]) == count
    sheets.tables["로그인기기"].append(issued)
    with pytest.raises(module.RememberError):
        remember.restore(token)


def test_tampered_token_cannot_revoke_the_legitimate_device(backend):
    module, _, _, remember, principal = backend
    token = remember.issue(principal)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(module.RememberError):
        remember.revoke(forged)
    assert remember.restore(token) == principal


def test_issue_requires_fresh_account_validation_before_writing(backend):
    module, sheets, _, remember, principal = backend
    row = sheets.tables["계정"][1][:]
    row[4], row[5] = "0", NEXT_REVISION
    sheets.tables["계정"].append(row)
    with pytest.raises(module.RememberError):
        remember.issue(principal)
    assert sheets.tables["로그인기기"] == [HEADERS]


@pytest.mark.parametrize("mutation", ["missing", "header", "identity", "expiry", "extra-column"])
def test_invalid_registry_fails_safe_without_exposing_sheet_contents(backend, mutation):
    module, sheets, _, remember, principal = backend
    token = remember.issue(principal)
    if mutation == "missing":
        del sheets.tables["로그인기기"]
    elif mutation == "header":
        sheets.tables["로그인기기"][0] = ["private-upstream-value"]
    elif mutation == "identity":
        altered = sheets.tables["로그인기기"][-1][:]
        altered[1] = "another-staff"
        sheets.tables["로그인기기"].append(altered)
    elif mutation == "expiry":
        sheets.tables["로그인기기"][-1][5] = (NOW + timedelta(days=181)).isoformat()
    else:
        sheets.tables["로그인기기"][-1].append("private-upstream-value")
    with pytest.raises(module.RememberError) as error:
        remember.restore(token)
    assert str(error.value) == module.REMEMBER_ERROR_MESSAGE
    assert "private" not in str(error.value)


def test_registry_write_failure_does_not_return_a_cookie_token(backend):
    module, sheets, _, remember, principal = backend
    sheets.updated_rows = 0
    with pytest.raises(module.RememberError):
        remember.issue(principal)


def test_public_account_lookup_returns_only_current_active_principal(backend):
    _, sheets, accounts, _, principal = backend
    assert accounts.get_principal(principal.username, principal.revision) == principal
    with pytest.raises(AccessError):
        accounts.get_principal(principal.username, NEXT_REVISION)
    assert not hasattr(accounts.get_principal(principal.username, principal.revision), "password_hash")
