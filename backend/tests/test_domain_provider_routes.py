"""云域名编排与权限测试：真实 SQLite，仅替换云端 API。"""

from dataclasses import replace
import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import domains
from app.core.crypto import encrypt_secret
from app.models.asset import Asset, DomainDNSRecord, DomainRecord
from app.models.credential import Credential
from app.services import domain_resources as service
from app.services.dns_providers.base import DNSProviderError, DNSRecord, DNSZone
from app.services.dns_providers.registry import DNS_PROVIDERS

ADMIN = SimpleNamespace(id=1, role="管理员", permission_overrides="")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Asset, DomainRecord, DomainDNSRecord, Credential):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add(Credential(cred_id="cred", name="通用凭证名", type="AccessKey", status="active",
            content=encrypt_secret('{"access_key":"test-access-key","secret_key":"test-secret-key"}')))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def adapter(monkeypatch):
    api = Mock()
    api.get_zone.return_value = DNSZone("zone-1", "example.co.uk")
    api.list_zones.return_value = [DNSZone("zone-1", "example.co.uk")]
    api.list_records.return_value = [DNSRecord("record-1", "www", "A", ["203.0.113.1", "203.0.113.2"])]
    monkeypatch.setitem(DNS_PROVIDERS, "huawei", replace(DNS_PROVIDERS["huawei"], factory=lambda *args: api))
    return api


def imported(db, adapter):
    return service.import_domain(db, "huawei", "cred", "example.co.uk", "生产", ADMIN)


def test_explicit_provider_does_not_depend_on_credential_name(db, adapter):
    result = domains.get_account_domains(domains.DomainAccountDomainsRequest(account_id="cred", provider="huawei"), db, ADMIN)
    assert [row["domain"] for row in result["domains"]] == ["example.co.uk"]
    assert result["source"] == "huawei-api"


def test_unsupported_provider_never_returns_example_domains(db):
    with pytest.raises(HTTPException):
        domains.get_account_domains(domains.DomainAccountDomainsRequest(account_id="cred", provider="aws"), db, ADMIN)


def test_import_validates_cloud_ownership_before_local_write(db, adapter):
    adapter.get_zone.side_effect = DNSProviderError("无权访问该域名", 403)
    with pytest.raises(DNSProviderError):
        imported(db, adapter)
    assert db.query(DomainRecord).count() == 0


def test_import_stores_remote_ids_and_full_values_and_real_zone_root(db, adapter):
    domain = imported(db, adapter)
    record = db.query(DomainDNSRecord).one()
    assert domain.provider_zone_id == "zone-1"
    assert domain.root_domain == "example.co.uk"
    serialized = service.serialize_record(record)
    assert serialized["provider_record_id"] == "record-1"
    assert serialized["record_values"] == ["203.0.113.1", "203.0.113.2"]
    assert db.query(Credential).one().used_by == 1


def test_import_route_keeps_immediate_expiry_refresh(db, adapter, monkeypatch):
    def expiry_refresh(session, domain, user):
        domain.expires_at = "2028-01-01"
        session.commit()
    refresh = Mock(side_effect=expiry_refresh)
    monkeypatch.setattr(domains, "sync_domain_expiry", refresh)
    result = domains.import_domain(domains.DomainImportRequest(provider="huawei", account_id="cred", domain="example.co.uk"), db, ADMIN)
    refresh.assert_called_once()
    assert result["expires_at"] == "2028-01-01"


def test_sync_failure_keeps_cached_records(db, adapter):
    domain = imported(db, adapter)
    adapter.list_records.side_effect = DNSProviderError("第二页失败")
    with pytest.raises(DNSProviderError):
        service.sync_records(db, domain, ADMIN)
    assert db.query(DomainDNSRecord).count() == 1


def test_sync_upserts_by_remote_id_and_preserves_long_txt(db, adapter):
    domain = imported(db, adapter)
    record_id = db.query(DomainDNSRecord).one().id
    adapter.list_records.return_value = [DNSRecord("record-1", "www", "TXT", ["x" * 600, "y" * 600])]
    service.sync_records(db, domain, ADMIN)
    assert db.query(DomainDNSRecord).one().id == record_id
    assert service.serialize_record(db.query(DomainDNSRecord).one())["record_values"] == ["x" * 600, "y" * 600]


def test_remote_failure_does_not_modify_local_record(db, adapter):
    domain = imported(db, adapter)
    row = db.query(DomainDNSRecord).one()
    adapter.update_record.side_effect = DNSProviderError("API 拒绝")
    with pytest.raises(DNSProviderError):
        service.save_record(db, domain, domains.DomainDNSRecordUpdate(record_value="203.0.113.3", host="www"), ADMIN, row)
    assert service.serialize_record(row)["record_values"] == ["203.0.113.1", "203.0.113.2"]


def test_expired_or_unauthorized_credential_rejected_before_cache_read(db, adapter):
    domain = imported(db, adapter)
    cred = db.query(Credential).one()
    cred.allowed_roles = '["管理员"]'
    db.commit()
    with pytest.raises(HTTPException) as caught:
        service.check_domain_access(db, domain, SimpleNamespace(role="只读", permission_overrides=""))
    assert caught.value.status_code == 403
    cred.status = "expired"
    db.commit()
    with pytest.raises(HTTPException):
        service.check_domain_access(db, domain, ADMIN)


def test_cloudflare_rejects_accesskey_credential_before_decryption(db):
    with pytest.raises(HTTPException, match="Token"):
        service.load_adapter(db, "cloudflare", "cred", ADMIN)


def test_record_write_uses_saved_zone_and_record_ids(db, adapter):
    domain = imported(db, adapter)
    row = db.query(DomainDNSRecord).one()
    adapter.update_record.return_value = DNSRecord("record-1", "www", "A", ["203.0.113.3", "203.0.113.4"])
    result = service.save_record(db, domain, domains.DomainDNSRecordUpdate(host="www", record_value="203.0.113.3",
                                 record_values=["203.0.113.3", "203.0.113.4"]), ADMIN, row)
    assert adapter.get_zone.call_args.args == ("example.co.uk", "zone-1")
    assert adapter.update_record.call_args.args[1] == "record-1"
    assert service.serialize_record(result)["record_values"] == ["203.0.113.3", "203.0.113.4"]


def test_rebind_validates_new_account_then_replaces_cache(db, adapter):
    domain = imported(db, adapter)
    db.add(Credential(cred_id="new-cred", name="更换后的凭证", type="AccessKey", status="active",
        content=encrypt_secret('{"access_key":"new-access-key","secret_key":"new-secret-key"}')))
    db.commit()
    adapter.list_records.return_value = [DNSRecord("new-record", "api", "A", ["203.0.113.9"])]
    service.change_account(db, domain, "huawei", "new-cred", ADMIN)
    db.commit()
    assert domain.account_id == "new-cred"
    assert domain.provider_zone_id == "zone-1"
    assert db.query(DomainDNSRecord).one().provider_record_id == "new-record"


def test_failed_rebind_preserves_old_binding_and_cache(db, adapter):
    domain = imported(db, adapter)
    db.add(Credential(cred_id="new-cred", name="新凭证", type="AccessKey", status="active",
        content=encrypt_secret('{"access_key":"new-access-key","secret_key":"new-secret-key"}')))
    db.commit()
    adapter.get_zone.side_effect = DNSProviderError("新账户没有此域名", 403)
    with pytest.raises(DNSProviderError):
        service.change_account(db, domain, "huawei", "new-cred", ADMIN)
    assert domain.account_id == "cred"
    assert db.query(DomainDNSRecord).one().provider_record_id == "record-1"


def test_unbind_keeps_snapshot_and_clears_cloud_identity(db, adapter):
    domain = imported(db, adapter)
    service.change_account(db, domain, "huawei", "", ADMIN)
    db.commit()
    assert domain.account_id == domain.account_name == domain.provider_zone_id == ""
    assert db.query(DomainDNSRecord).count() == 1


def test_unbound_snapshot_preserves_credential_permissions(db, adapter):
    domain = imported(db, adapter)
    credential = db.query(Credential).one()
    credential.allowed_roles = '["管理员"]'
    db.commit()
    service.change_account(db, domain, "huawei", "", ADMIN)
    db.commit()
    outsider = SimpleNamespace(role="只读", permission_overrides="")
    assert credential.used_by == 1
    with pytest.raises(HTTPException) as caught:
        domains.get_domain_dns_records(domain.id, db=db, current_user=outsider)
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException):
        service.change_account(db, domain, "huawei", "cred", outsider)
    credential.status = "expired"
    db.commit()
    assert len(domains.get_domain_dns_records(domain.id, db=db, current_user=ADMIN)) == 1


def test_snapshot_acl_migration_is_idempotent(monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261105_dns_snapshot_acl.py"
    spec = importlib.util.spec_from_file_location("dns_snapshot_acl_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE domain_records (id INTEGER PRIMARY KEY)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert len(inspect(connection).get_columns("domain_records")) == 2
        migration.downgrade()
        migration.downgrade()
        assert len(inspect(connection).get_columns("domain_records")) == 1
    engine.dispose()


def test_protected_record_cannot_be_deleted(db, adapter):
    domain = imported(db, adapter)
    row = db.query(DomainDNSRecord).one()
    row.read_only = True
    with pytest.raises(DNSProviderError, match="只读"):
        service.delete_record(db, domain, row, ADMIN)
    adapter.delete_record.assert_not_called()
    assert db.query(DomainDNSRecord).count() == 1


def test_cached_records_do_not_bypass_credential_authorization(db, adapter, monkeypatch):
    domain = imported(db, adapter)
    credential = db.query(Credential).one()
    credential.allowed_roles = '["管理员"]'
    db.commit()
    monkeypatch.setitem(domains._DNS_SYNC_LAST_TS, domain.id, time.monotonic())
    with pytest.raises(HTTPException) as caught:
        domains.get_domain_dns_records(domain.id, db=db, current_user=SimpleNamespace(role="只读", permission_overrides=""))
    assert caught.value.status_code == 403
    assert adapter.list_records.call_count == 1  # 仅导入时读取过云端。


def api_client(db, role="管理员"):
    from fastapi import FastAPI
    from app.core.security import get_current_user
    from app.db.database import get_db
    from tests.asgi_client import ASGITestClient

    app = FastAPI()
    app.include_router(domains.router, prefix="/api/domains")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, role=role, permission_overrides="")
    return ASGITestClient(app)


def test_http_import_requires_manage_permission(db, adapter):
    result = api_client(db, "只读").post("/api/domains/import", json={"provider": "huawei", "account_id": "cred", "domain": "example.co.uk"})
    assert result.status_code == 403
    adapter.get_zone.assert_not_called()


def test_http_remote_failure_is_failure_status_and_does_not_mutate_cache(db, adapter):
    domain = imported(db, adapter)
    row = db.query(DomainDNSRecord).one()
    adapter.update_record.side_effect = DNSProviderError("模拟云端拒绝", 502)
    result = api_client(db).put(f"/api/domains/{domain.id}/records/{row.id}", json={"host": "www", "record_value": "203.0.113.3"})
    assert result.status_code == 502
    assert service.serialize_record(row)["record_values"] == ["203.0.113.1", "203.0.113.2"]


@pytest.mark.parametrize("present", [False, True])
def test_dns_migration_is_idempotent_with_old_and_current_schema(present, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261105_dns_provider_ids.py"
    spec = importlib.util.spec_from_file_location("dns_provider_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE domain_records (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE domain_dns_records (id INTEGER PRIMARY KEY)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        if present:
            migration.upgrade()
        migration.upgrade()
        migration.upgrade()
        assert len(inspect(connection).get_columns("domain_dns_records")) == 6
        migration.downgrade()
        migration.downgrade()
        assert [column["name"] for column in inspect(connection).get_columns("domain_records")] == ["id"]
    engine.dispose()
