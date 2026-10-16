from uuid import uuid4
import pytest
from branch_access import Principal, AccessError


def principal(role='admin', branch='all'):
    return Principal('tester', role, branch, str(uuid4()))


def test_preview_access_requires_current_global_role():
    import staging
    assert staging.require_preview(principal()).is_admin
    with pytest.raises(AccessError):
        staging.require_preview(principal('branch', '인천점'))


def test_missing_database_does_not_fall_back_to_temporary_disk():
    import staging
    assert staging.storage_config({}.get) is None
    with pytest.raises(ValueError):
        staging.storage_config({'DATABASE_URL': 'sqlite:///tmp/ledger.db'}.get)


def test_local_database_requires_explicit_opt_in():
    import staging
    with pytest.raises(ValueError):
        staging.storage_config({'LOCAL_LEDGER_PATH': '/tmp/ledger.db'}.get)
    result = staging.storage_config({'LOCAL_LEDGER_PATH': '/tmp/ledger.db', 'ALLOW_LOCAL_LEDGER': True}.get)
    assert result['local'] is True


def test_remote_database_requires_tls_and_does_not_put_secrets_in_label():
    import staging
    result = staging.storage_config({'DATABASE_URL': 'postgresql://user:secret@db.example.test/clinic'}.get)
    assert 'sslmode=require' in result['target']
    assert 'secret' not in result['label']
    with pytest.raises(ValueError):
        staging.storage_config({'DATABASE_URL': 'postgresql://u:p@db.example.test/clinic?sslmode=disable'}.get)


def test_analysis_revision_rejects_changed_ledger():
    import staging
    class Store:
        def load_day(self, p, branch, business_date):
            return {'revision': 8}
    with pytest.raises(ValueError, match='변경'):
        staging.load_analysis_day(Store(), principal(), {'branch': '인천점', 'business_date': '2026-10-16', 'revision': 7})


def test_analysis_blocks_other_owners_unresolved_drafts():
    import staging
    class Store:
        def load_day(self, p, branch, business_date):
            return {'revision': 7, 'pending_draft_count': 1}
        def list_drafts(self, *args):
            return []
    with pytest.raises(ValueError, match='초안'):
        staging.load_analysis_day(Store(), principal(), {'branch': '인천점', 'business_date': '2026-10-16', 'revision': 7})


def test_revocation_during_summary_save_remains_access_error():
    import staging
    class Store:
        def append_summary(self, *args, **kwargs):
            raise AccessError('revoked')
    with pytest.raises(AccessError):
        staging.DevelopmentHistory(Store(), principal()).append_record({})
