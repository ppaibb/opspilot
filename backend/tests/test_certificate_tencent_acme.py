"""腾讯云 DNS-01 ACME：插件表、账户下拉、TXT 创建/删除分发。

覆盖：ACME_PROVIDER_PLUGINS 含 tencent 且 implemented；
build_acme_accounts 把腾讯云凭证放进签发下拉、AWS 仍排除；
TencentDNS.create_txt_record / delete_txt_record 的 DNSPod 请求字段；
create/delete_dns_txt_record 按厂商分发，阿里云行为不变。
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import certificates
from app.core.crypto import encrypt_secret
from app.models.credential import Credential
from app.services.dns_providers.base import DNSProviderError, DNSZone
from app.services.dns_providers.registry import provider_metadata
from app.services.dns_providers.tencent import TencentDNS


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Credential.__table__.create(engine)
    with Session(engine) as session:
        for cred_id, name in (
            ("CRED-ALI", "阿里云-闫东"),
            ("CRED-TEC", "腾讯云-闫东"),
            ("CRED-AWS", "AWS-测试"),
        ):
            session.add(Credential(
                cred_id=cred_id, name=name, type="AccessKey", status="active",
                content=encrypt_secret('{"access_key":"ak","secret_key":"sk"}'),
            ))
        yield session
    engine.dispose()


def test_acme_plugin_table_includes_tencent():
    plugin = certificates.ACME_PROVIDER_PLUGINS["tencent"]
    assert plugin["implemented"] is True
    assert plugin["label"] == "Tencent Cloud DNS"
    assert certificates.ACME_PROVIDER_PLUGINS["aws"]["implemented"] is False


def test_build_acme_accounts_includes_tencent_excludes_aws(db):
    accounts = certificates.build_acme_accounts(db, None)
    by_id = {item["account_id"]: item for item in accounts}
    assert by_id["CRED-TEC"]["provider_key"] == "tencent"
    assert by_id["CRED-TEC"]["provider_label"] == "Tencent Cloud DNS"
    assert by_id["CRED-ALI"]["provider_key"] == "aliyun"
    assert "CRED-AWS" not in by_id


def test_registry_advertises_tencent_acme_but_not_record_write():
    specs = {item["key"]: item for item in provider_metadata()}
    assert specs["tencent"]["supports_acme"] is True
    assert specs["tencent"]["supports_record_write"] is False


def _mock_api():
    api = Mock()
    api.CreateRecord.return_value = SimpleNamespace(RecordId=12345)
    api.DeleteRecord.return_value = SimpleNamespace(RequestId="req-1")
    return api


def test_tencent_create_txt_record_sends_dnspod_fields():
    api = _mock_api()
    adapter = TencentDNS("SecretId", "SecretKey", client=api)
    record_id = adapter.create_txt_record("nnyy.ltd", "_acme-challenge", "challenge-token")
    assert record_id == "12345"
    request = api.CreateRecord.call_args.args[0]
    assert request.Domain == "nnyy.ltd"
    assert request.SubDomain == "_acme-challenge"
    assert request.RecordType == "TXT"
    assert request.RecordLine == "默认"
    assert request.Value == "challenge-token"
    assert request.TTL == 600


def test_tencent_delete_txt_record_sends_zone_and_numeric_id():
    api = _mock_api()
    adapter = TencentDNS("SecretId", "SecretKey", client=api)
    adapter.delete_txt_record("nnyy.ltd", "12345")
    request = api.DeleteRecord.call_args.args[0]
    assert request.Domain == "nnyy.ltd"
    assert request.RecordId == 12345
    assert isinstance(request.RecordId, int)


def test_tencent_delete_txt_record_ignores_empty_id():
    api = _mock_api()
    adapter = TencentDNS("SecretId", "SecretKey", client=api)
    adapter.delete_txt_record("nnyy.ltd", "")
    api.DeleteRecord.assert_not_called()


def test_tencent_create_txt_error_hides_raw_message():
    api = Mock()
    error = Exception("boom secret-must-not-leak")
    error.code = "AuthFailure.SecretIdNotFound"
    api.CreateRecord.side_effect = error
    adapter = TencentDNS("SecretId", "SecretKey", client=api)
    with pytest.raises(DNSProviderError) as caught:
        adapter.create_txt_record("nnyy.ltd", "_acme-challenge", "token")
    assert "secret-must-not-leak" not in str(caught.value)
    assert "AuthFailure.SecretIdNotFound" in str(caught.value)


def test_tencent_create_txt_record_without_record_id_fails():
    api = Mock()
    api.CreateRecord.return_value = SimpleNamespace(RecordId=None)
    adapter = TencentDNS("SecretId", "SecretKey", client=api)
    with pytest.raises(DNSProviderError, match="未返回新记录 ID"):
        adapter.create_txt_record("nnyy.ltd", "_acme-challenge", "token")


def _mock_tencent_client(monkeypatch, zones):
    client = Mock()
    client.list_zones.return_value = [DNSZone("z1", name) for name in zones]
    client.create_txt_record.return_value = "987"
    monkeypatch.setattr(
        "app.services.dns_providers.tencent.TencentDNS",
        lambda *args, **kwargs: client,
    )
    return client


def test_create_tencent_txt_record_resolves_zone_and_subdomain(monkeypatch):
    client = _mock_tencent_client(monkeypatch, ["nnyy.ltd", "example.com"])
    record_id, zone = certificates.create_tencent_txt_record(
        "ak", "sk", "_acme-challenge.www.nnyy.ltd", "token",
    )
    assert (record_id, zone) == ("987", "nnyy.ltd")
    client.create_txt_record.assert_called_once_with(
        "nnyy.ltd", "_acme-challenge.www", "token", ttl=600,
    )


def test_create_tencent_txt_record_root_domain(monkeypatch):
    client = _mock_tencent_client(monkeypatch, ["nnyy.ltd"])
    record_id, zone = certificates.create_tencent_txt_record(
        "ak", "sk", "_acme-challenge.nnyy.ltd", "token",
    )
    assert (record_id, zone) == ("987", "nnyy.ltd")
    client.create_txt_record.assert_called_once_with(
        "nnyy.ltd", "_acme-challenge", "token", ttl=600,
    )


def test_create_tencent_txt_record_unknown_zone_fails(monkeypatch):
    _mock_tencent_client(monkeypatch, ["example.com"])
    with pytest.raises(RuntimeError, match="腾讯云"):
        certificates.create_tencent_txt_record(
            "ak", "sk", "_acme-challenge.nnyy.ltd", "token",
        )


def test_fetch_dns_zones_dispatches_tencent(monkeypatch):
    _mock_tencent_client(monkeypatch, ["nnyy.ltd"])
    assert certificates.fetch_dns_zones("tencent", "ak", "sk") == ["nnyy.ltd"]


def test_fetch_dns_zones_rejects_unimplemented_provider():
    with pytest.raises(RuntimeError, match="不支持"):
        certificates.fetch_dns_zones("aws", "ak", "sk")


def test_create_dns_txt_record_dispatches_aliyun(monkeypatch):
    monkeypatch.setattr(
        certificates, "create_aliyun_txt_record", lambda *a, **k: "ali-1",
    )
    record_id, zone = certificates.create_dns_txt_record(
        "aliyun", "ak", "sk", "_acme-challenge.example.com", "token",
        zones=["example.com"],
    )
    assert (record_id, zone) == ("ali-1", "example.com")


def test_create_dns_txt_record_dispatches_tencent(monkeypatch):
    monkeypatch.setattr(
        certificates, "create_tencent_txt_record",
        lambda *a, **k: ("tec-1", "nnyy.ltd"),
    )
    assert certificates.create_dns_txt_record(
        "tencent", "ak", "sk", "_acme-challenge.nnyy.ltd", "token",
        zones=["nnyy.ltd"],
    ) == ("tec-1", "nnyy.ltd")


def test_create_dns_txt_record_rejects_unimplemented_provider():
    with pytest.raises(RuntimeError, match="不支持"):
        certificates.create_dns_txt_record(
            "aws", "ak", "sk", "_acme-challenge.example.com", "token",
            zones=["example.com"],
        )


def test_delete_dns_txt_record_dispatches_tencent(monkeypatch):
    client = Mock()
    monkeypatch.setattr(
        "app.services.dns_providers.tencent.TencentDNS",
        lambda *args, **kwargs: client,
    )
    certificates.delete_dns_txt_record("tencent", "ak", "sk", "nnyy.ltd", "12345")
    client.delete_txt_record.assert_called_once_with("nnyy.ltd", "12345")


def test_delete_dns_txt_record_dispatches_aliyun(monkeypatch):
    calls = []
    monkeypatch.setattr(
        certificates, "delete_aliyun_record", lambda *a: calls.append(a),
    )
    certificates.delete_dns_txt_record("aliyun", "ak", "sk", "", "ali-1")
    assert calls == [("ak", "sk", "ali-1")]


def test_resolve_best_zone_reports_provider_label():
    with pytest.raises(RuntimeError, match="腾讯云"):
        certificates.resolve_best_zone(
            "_acme-challenge.nnyy.ltd", ["example.com"], provider_label="腾讯云",
        )
    assert certificates.resolve_best_zone(
        "_acme-challenge.example.com", ["example.com"],
    ) == "example.com"
