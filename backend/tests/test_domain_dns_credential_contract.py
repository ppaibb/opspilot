"""P0-1 回归：dns_credential 前后端值域必须统一为 cred_id。

历史数据里 dns_credential 存的是凭证名（import/change_account 的旧行为），
而 update_domain 只按 cred_id 查，导致编辑云域名必 404；且旧调用点
ensure_credential_access(db, credential, user) 传了 3 个位置参数，
对着 2 参签名会直接 TypeError。契约：
- 用凭证名更新成功，且库里归一化为 cred_id；
- 用 cred_id 更新成功；
- 用不存在的值更新仍 404；
- import_domain 入库即存 cred_id。
"""
from dataclasses import replace
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
from app.services.dns_providers.base import DNSRecord, DNSZone
from app.services.dns_providers.registry import DNS_PROVIDERS

ADMIN = SimpleNamespace(id=1, role="管理员", permission_overrides="")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Asset, DomainRecord, DomainDNSRecord, Credential):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add(Credential(
            cred_id="CRED-TEST01", name="阿里云-闫东", type="AccessKey", status="active",
            content=encrypt_secret('{"access_key":"ak","secret_key":"sk"}'),
        ))
        yield session
    engine.dispose()


@pytest.fixture
def adapter(monkeypatch):
    api = Mock()
    api.get_zone.return_value = DNSZone("zone-1", "example.com")
    api.list_zones.return_value = [DNSZone("zone-1", "example.com")]
    api.list_records.return_value = [DNSRecord("record-1", "www", "A", ["203.0.113.1"])]
    monkeypatch.setitem(DNS_PROVIDERS, "aliyun", replace(DNS_PROVIDERS["aliyun"], factory=lambda *args: api))
    return api


def _legacy_row(db):
    """模拟旧数据：dns_credential 存的是凭证名。"""
    row = DomainRecord(
        domain="example.com", root_domain="example.com", record_type="A", record_value="-",
        provider="aliyun", status="active", account_id="CRED-TEST01",
        account_name="阿里云-闫东", dns_credential="阿里云-闫东",
        created_at="2026-11-09", updated_at="2026-11-09",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_update_with_legacy_credential_name_succeeds_and_normalizes(db):
    row = _legacy_row(db)
    result = domains.update_domain(
        row.id, domains.DomainUpdate(dns_credential="阿里云-闫东", remark="回填"), db, ADMIN,
    )
    assert result["dns_credential"] == "CRED-TEST01"
    assert db.query(DomainRecord).one().dns_credential == "CRED-TEST01"


def test_update_with_cred_id_succeeds(db):
    row = _legacy_row(db)
    result = domains.update_domain(
        row.id, domains.DomainUpdate(dns_credential="CRED-TEST01", remark="回填"), db, ADMIN,
    )
    assert result["dns_credential"] == "CRED-TEST01"
    assert db.query(DomainRecord).one().dns_credential == "CRED-TEST01"


def test_update_with_unknown_credential_still_404(db):
    row = _legacy_row(db)
    with pytest.raises(HTTPException) as caught:
        domains.update_domain(
            row.id, domains.DomainUpdate(dns_credential="不存在的凭证"), db, ADMIN,
        )
    assert caught.value.status_code == 404
    # 失败不改库：旧值原样保留
    assert db.query(DomainRecord).one().dns_credential == "阿里云-闫东"


def test_import_stores_cred_id_not_name(db, adapter):
    domain = service.import_domain(db, "aliyun", "CRED-TEST01", "example.com", "生产", ADMIN)
    assert domain.dns_credential == "CRED-TEST01"
    assert domain.account_id == "CRED-TEST01"
    assert domain.account_name == "阿里云-闫东"
