"""합성 계정만으로 인증·취소·지점별 서버 경계를 검증한다."""
import copy
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from urllib.parse import unquote

import pytest


HEADERS = ["username", "role", "branch", "password_hash", "active", "revision", "updated_at"]
IMPORTED_HASH = "pbkdf2-sha256:100000:000102030405060708090a0b0c0d0e0f:0f80ecb32361e1f1e6f3bee07f74bd853216c8c4c3eeacc5149ecaa709d2c512"
REV_A = "11111111-1111-4111-8111-111111111111"
REV_B = "22222222-2222-4222-8222-222222222222"
REV_C = "33333333-3333-4333-8333-333333333333"
NOW = "2026-10-15T14:00:00+00:00"


def test_branch_access_module_exists():
    assert importlib.util.find_spec("branch_access") is not None


@pytest.fixture
def access():
    import branch_access
    return branch_access


def account(username="staff-one", role="branch", branch="인천점", revision=REV_B, active="1"):
    return [username, role, branch, IMPORTED_HASH, active, revision, NOW]


class SheetTransport:
    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows if rows is not None else [HEADERS, account("hq", "admin", "all", REV_A), account()])
        self.calls = []
        self.updated_rows = 1

    def _request(self, method, suffix, **kwargs):
        self.calls.append((method, suffix, kwargs))
        assert unquote(suffix).startswith("'계정'")
        if method == "GET":
            return {"values": copy.deepcopy(self.rows)}
        if method == "POST":
            self.rows.extend(copy.deepcopy(kwargs["json"]["values"]))
            return {"updates": {"updatedRows": self.updated_rows}}
        raise AssertionError("Unexpected account mutation")


def test_imported_node_webcrypto_password_hash_is_compatible(access):
    # Derived independently by Node crypto.pbkdf2Sync with 16 decoded salt bytes.
    assert access.verify_password("synthetic-password", IMPORTED_HASH)
    assert not access.verify_password("different-password", IMPORTED_HASH)
    hashed = access.hash_password("synthetic-password", salt_hex="000102030405060708090a0b0c0d0e0f", iterations=100000)
    assert hashed == IMPORTED_HASH


def test_new_passwords_use_random_salt_and_600000_iterations(access):
    first = access.hash_password("new-synthetic-password")
    second = access.hash_password("new-synthetic-password")
    assert first.startswith("pbkdf2-sha256:600000:")
    assert first != second
    assert access.verify_password("new-synthetic-password", first)


@pytest.mark.parametrize("bad_hash", [None, "", "raw-password", IMPORTED_HASH.replace(":100000:", ":1:"), IMPORTED_HASH.replace(":100000:", ":1000001:"), IMPORTED_HASH + ":extra", IMPORTED_HASH.replace("000102", "zz0102")])
def test_malformed_or_unbounded_hashes_never_authenticate(access, bad_hash):
    assert access.verify_password("synthetic-password", bad_hash) is False


def test_principal_and_account_list_never_contain_password_material(access):
    store = access.AccountStore(SheetTransport())
    principal = store.authenticate("hq", "synthetic-password")
    assert principal.is_admin
    assert set(asdict(principal)) == {"username", "role", "branch", "revision"}
    accounts = store.list_accounts(principal)
    assert len(accounts) == 2
    assert all(set(row) == {"username", "role", "branch", "active", "revision", "updated_at"} for row in accounts)
    assert all(type(row["active"]) is bool for row in accounts)
    assert "password" not in repr(accounts)
    assert "pbkdf2" not in repr(principal)


@pytest.mark.parametrize("username,password", [("unknown", "synthetic-password"), ("staff-one", "wrong-password"), ("staff-one", None), ("staff-one", "x" * 1025)])
def test_failed_login_has_fixed_safe_error(access, username, password):
    store = access.AccountStore(SheetTransport())
    with pytest.raises(access.AccessError) as error:
        store.authenticate(username, password)
    assert str(error.value) == access.AUTH_ERROR_MESSAGE
    assert username not in str(error.value)


def test_account_changes_revoke_existing_sessions_on_next_validation(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    principal = store.authenticate("staff-one", "synthetic-password")
    assert store.validate(principal) == principal
    transport.rows.append(account(revision=REV_C, active="0"))
    with pytest.raises(access.AccessError):
        store.validate(principal)
    with pytest.raises(access.AccessError):
        store.authenticate("staff-one", "synthetic-password")


def test_changed_revision_or_branch_invalidates_old_principal(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    principal = store.authenticate("staff-one", "synthetic-password")
    transport.rows.append(account(branch="강남점", revision=REV_C))
    with pytest.raises(access.AccessError):
        store.validate(principal)


@pytest.mark.parametrize("bad_row", [
    account("STAFF-ONE", revision=REV_C),
    account("staff-one", revision=REV_B, active="0"),
    account("staff-two", branch="알수없는지점", revision=REV_C),
    account("staff-two", role="branch", branch="all", revision=REV_C),
    account("staff-two", role="admin", branch="인천점", revision=REV_C),
    account("=IMPORTDATA(x)", revision=REV_C),
    account("staff-two", active="yes", revision=REV_C),
    account("staff-two", revision="invalid"),
])
def test_ambiguous_or_malformed_accounts_fail_closed(access, bad_row):
    transport = SheetTransport()
    transport.rows.append(bad_row)
    with pytest.raises(access.AccessError):
        access.AccountStore(transport).authenticate("hq", "synthetic-password")


def test_header_or_transport_errors_are_masked(access):
    transport = SheetTransport(rows=[["wrong-header"]])
    with pytest.raises(access.AccessError) as error:
        access.AccountStore(transport).authenticate("hq", "synthetic-password")
    assert str(error.value) == access.ACCOUNT_ERROR_MESSAGE
    def fail(*args, **kwargs):
        raise RuntimeError("private-key-or-upstream-response")
    transport._request = fail
    with pytest.raises(access.AccessError) as error:
        access.AccountStore(transport).authenticate("hq", "synthetic-password")
    assert str(error.value) == access.ACCOUNT_ERROR_MESSAGE


def test_reused_account_revision_cannot_reinstate_old_permissions(access):
    transport = SheetTransport()
    original = copy.deepcopy(transport.rows[-1])
    transport.rows.append(account(revision=REV_C, active="0"))
    transport.rows.append(original)
    with pytest.raises(access.AccessError):
        access.AccountStore(transport).authenticate("staff-one", "synthetic-password")


def test_branch_users_cannot_list_or_manage_accounts(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    principal = store.authenticate("staff-one", "synthetic-password")
    for operation in [lambda: store.list_accounts(principal), lambda: store.save_branch_account(principal, "new-user", "인천점", "new-password")]:
        with pytest.raises(access.AccessError):
            operation()
    assert not any(method == "POST" for method, _, _ in transport.calls)


def test_admin_can_create_and_update_branch_account_with_raw_append(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    hq = store.authenticate("hq", "synthetic-password")
    created = store.save_branch_account(hq, "new-staff", "잠실점", "new-password")
    assert created["role"] == "branch"
    assert created["branch"] == "잠실점"
    assert created["active"] is True
    assert "password_hash" not in created
    assert store.authenticate("new-staff", "new-password").branch == "잠실점"
    previous_rows = copy.deepcopy(transport.rows)
    updated = store.save_branch_account(hq, "new-staff", "일산점", active=False, expected_revision=created["revision"])
    assert transport.rows[:-1] == previous_rows
    assert updated["revision"] != created["revision"]
    assert updated["active"] is False
    assert transport.rows[-1][3] == transport.rows[-2][3]
    writes = [kwargs for method, _, kwargs in transport.calls if method == "POST"]
    assert all(call["params"]["valueInputOption"] == "RAW" for call in writes)
    assert all(call["params"]["insertDataOption"] == "INSERT_ROWS" for call in writes)


def test_password_reset_revokes_old_password_and_session(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    hq = store.authenticate("hq", "synthetic-password")
    previous = store.authenticate("staff-one", "synthetic-password")
    store.save_branch_account(hq, "staff-one", "인천점", password="replacement-password", expected_revision=previous.revision)
    with pytest.raises(access.AccessError):
        store.validate(previous)
    with pytest.raises(access.AccessError):
        store.authenticate("staff-one", "synthetic-password")
    assert store.authenticate("staff-one", "replacement-password").branch == "인천점"


def test_admin_accounts_are_protected_and_stale_revision_cannot_overwrite(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    hq = store.authenticate("hq", "synthetic-password")
    for kwargs in [
        {"username": "hq", "branch": "인천점", "password": "replacement-password", "expected_revision": REV_A},
        {"username": "staff-one", "branch": "잠실점", "expected_revision": REV_C},
        {"username": "staff-one", "branch": "잠실점"},
        {"username": "new-staff", "branch": "잠실점"},
    ]:
        with pytest.raises(access.AccessError):
            store.save_branch_account(hq, **kwargs)
    assert not any(method == "POST" for method, _, _ in transport.calls)


def test_append_without_exact_success_response_is_not_reported_saved(access):
    transport = SheetTransport()
    store = access.AccountStore(transport)
    hq = store.authenticate("hq", "synthetic-password")
    transport.updated_rows = 0
    with pytest.raises(access.AccessError):
        store.save_branch_account(hq, "new-staff", "인천점", "new-password")


def test_sheet_choices_are_scoped_without_merging_missing_configuration(access):
    branch = access.Principal("staff-one", "branch", "인천점", REV_B)
    hq = access.Principal("hq", "admin", "all", REV_A)
    configured = {"인천점": "own", "강남점": "other"}
    assert access.authorized_sheets(branch, configured) == {"인천점": "own"}
    assert access.authorized_sheets(branch, {}) == {}
    assert access.authorized_sheets(branch, {"강남점": "other"}) == {}
    assert access.authorized_sheets(hq, configured) == configured
    with pytest.raises(access.AccessError):
        access.assert_branch(branch, "강남점")
    with pytest.raises(access.AccessError):
        access.assert_branch(branch, "")


@pytest.mark.parametrize("branch", ["인천점", "잠실점", "엔디어트", "강남점", "일산점", "압구정", "마곡점"])
def test_all_seven_clinic_branches_remain_authorized(access, branch):
    principal = access.Principal("synthetic-staff", "branch", branch, REV_B)
    assert access.assert_branch(principal, branch) == branch
    assert access.authorized_sheets(principal, {branch: "own-sheet"}) == {branch: "own-sheet"}


def test_excluded_company_cannot_log_in_or_appear_in_sheet_choices(access):
    assert "BW컨설팅" not in access.CANONICAL_BRANCHES
    with pytest.raises(access.AccessError):
        access.Principal("synthetic-staff", "branch", "BW컨설팅", REV_B)
    transport = SheetTransport(rows=[HEADERS, account(branch="BW컨설팅")])
    with pytest.raises(access.AccessError):
        access.AccountStore(transport).authenticate("staff-one", "synthetic-password")
    hq = access.Principal("synthetic-company-hq", "admin", "all", REV_A)
    assert hq.is_admin
    assert access.authorized_sheets(hq, {"인천점": "clinic", "BW컨설팅": "excluded"}) == {"인천점": "clinic"}


def test_excluded_branch_history_is_hidden_and_explicit_access_is_denied(access):
    records = [{"branch": "인천점", "gap": 0}, {"branch": "BW컨설팅", "gap": 100}]
    transport = HistoryTransport()
    transport.list_month = lambda month, branch="": records
    hq = access.Principal("synthetic-hq", "admin", "all", REV_A)
    scoped = access.ScopedHistoryStore(transport, hq, validate=lambda p: p)
    assert scoped.list_month("2026-10") == [{"branch": "인천점", "gap": 0}]
    with pytest.raises(access.AccessError):
        scoped.list_month("2026-10", "BW컨설팅")
    with pytest.raises(access.AccessError):
        scoped.append_record({"branch": "BW컨설팅"})
    assert records == [{"branch": "인천점", "gap": 0}, {"branch": "BW컨설팅", "gap": 100}]


class HistoryTransport:
    def __init__(self):
        self.calls = []

    def list_month(self, month, branch=""):
        self.calls.append(("list", month, branch))
        return [{"branch": "인천점", "gap": 0}, {"branch": "강남점", "gap": 100}]

    def append_record(self, record):
        self.calls.append(("append", copy.deepcopy(record)))
        return True


def test_branch_history_defaults_to_own_branch_and_filters_before_return(access):
    transport = HistoryTransport()
    principal = access.Principal("staff-one", "branch", "인천점", REV_B)
    checked = []
    scoped = access.ScopedHistoryStore(transport, principal, validate=lambda p: checked.append(p) or p)
    assert scoped.list_month("2026-10") == [{"branch": "인천점", "gap": 0}]
    assert transport.calls == [("list", "2026-10", "인천점")]
    assert checked == [principal]


def test_cross_branch_history_forgery_is_rejected_before_transport(access):
    transport = HistoryTransport()
    principal = access.Principal("staff-one", "branch", "인천점", REV_B)
    checked = []
    scoped = access.ScopedHistoryStore(transport, principal, validate=lambda p: checked.append(p) or p)
    for operation in [lambda: scoped.list_month("2026-10", "강남점"), lambda: scoped.append_record({"branch": "강남점"})]:
        with pytest.raises(access.AccessError):
            operation()
    assert transport.calls == []
    assert checked == []


def test_revoked_account_cannot_read_or_write_history(access):
    transport = HistoryTransport()
    principal = access.Principal("staff-one", "branch", "인천점", REV_B)
    def revoked(principal):
        raise access.AccessError(access.AUTH_ERROR_MESSAGE)
    scoped = access.ScopedHistoryStore(transport, principal, validate=revoked)
    for operation in [lambda: scoped.list_month("2026-10"), lambda: scoped.append_record({"branch": "인천점"})]:
        with pytest.raises(access.AccessError):
            operation()
    assert transport.calls == []


def test_hq_can_explicitly_view_and_save_each_canonical_branch(access):
    transport = HistoryTransport()
    principal = access.Principal("hq", "admin", "all", REV_A)
    scoped = access.ScopedHistoryStore(transport, principal, validate=lambda p: p)
    assert scoped.list_month("2026-10", "강남점") == [{"branch": "강남점", "gap": 100}]
    assert scoped.append_record({"branch": "강남점"}) is True
    with pytest.raises(access.AccessError):
        scoped.append_record({"branch": "없는지점"})


def test_login_attempt_limit_is_shared_across_sessions_and_case_variants(access):
    throttle = access.LoginThrottle()
    assert all(throttle.allow("staff-one", now=0) for _ in range(5))
    assert not throttle.allow(" STAFF-ONE ", now=1)
    assert throttle.allow("different-staff", now=1)
    assert not throttle.allow("staff-one", now=899)
    assert throttle.allow("staff-one", now=900)


def test_login_attempt_reservation_is_atomic_and_success_resets_limit(access):
    throttle = access.LoginThrottle()
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: throttle.allow("staff-one", now=0), range(24)))
    assert sum(results) == 5
    throttle.success("STAFF-ONE")
    assert throttle.allow("staff-one", now=1)


def test_login_attempt_memory_is_bounded_and_expired_entries_are_removed(access):
    throttle = access.LoginThrottle(max_keys=3)
    for index in range(10):
        assert throttle.allow("synthetic-" + str(index), now=index)
    assert len(throttle._attempts) == 3
    assert throttle.allow("next-user", now=1000)
    assert len(throttle._attempts) == 1
