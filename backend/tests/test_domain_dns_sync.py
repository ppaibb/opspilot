"""合并分支回归：保留DNSPod同步与账户绑定意图，迁移到统一适配器接口。

手写华为云签名已由官方SDK替代；记录集不再拆行，未知类型只读保留。
"""
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import types

import pytest

from app.api import domains
from app.services.dns_providers.base import DNSProviderError, DNSRecord, DNSZone
from app.services.dns_providers.tencent import TencentDNS
from app.services.dns_providers.huawei import HuaweiDNS

class _FakeSDKException(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _FakeItem:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeCountInfo:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeListResponse:
    def __init__(self, items, total, count_attr):
        setattr(self, "DomainList" if count_attr == "DomainTotal" else "RecordList", items)
        setattr(
            self,
            "DomainCountInfo" if count_attr == "DomainTotal" else "RecordCountInfo",
            _FakeCountInfo(**{count_attr: total}),
        )


class _FakeDnspodClient:
    """按 Offset/Limit 切片返回预设数据，模拟分页。"""

    def __init__(self, domains_data, records_data, error=None):
        self._domains = domains_data
        self._records = records_data
        self._error = error

    def DescribeDomainList(self, req):
        if self._error:
            raise self._error
        page = self._domains[req.Offset:req.Offset + req.Limit]
        return _FakeListResponse(page, len(self._domains), "DomainTotal")

    def DescribeRecordList(self, req):
        if self._error:
            raise self._error
        page = self._records[req.Offset:req.Offset + req.Limit]
        return _FakeListResponse(page, len(self._records), "TotalCount")


def _install_tencent_stub(monkeypatch, client):
    """往 sys.modules 注入最小 tencentcloud 桩，供 domains.py 内的懒 import 使用。"""
    tencentcloud = types.ModuleType("tencentcloud")
    common = types.ModuleType("tencentcloud.common")
    credential_mod = types.ModuleType("tencentcloud.common.credential")
    profile = types.ModuleType("tencentcloud.common.profile")
    client_profile_mod = types.ModuleType("tencentcloud.common.profile.client_profile")
    http_profile_mod = types.ModuleType("tencentcloud.common.profile.http_profile")
    exception_mod = types.ModuleType("tencentcloud.common.exception")
    sdk_exc_mod = types.ModuleType("tencentcloud.common.exception.tencent_cloud_sdk_exception")
    dnspod_pkg = types.ModuleType("tencentcloud.dnspod")
    dnspod_ver = types.ModuleType("tencentcloud.dnspod.v20210323")
    dnspod_client_mod = types.ModuleType("tencentcloud.dnspod.v20210323.dnspod_client")
    dnspod_models_mod = types.ModuleType("tencentcloud.dnspod.v20210323.models")

    class Credential:
        def __init__(self, access_key, secret_key):
            self.access_key = access_key
            self.secret_key = secret_key

    class HttpProfile:
        def __init__(self):
            self.endpoint = ""

    class ClientProfile:
        def __init__(self):
            self.httpProfile = None

    class DescribeDomainListRequest:
        Type = "ALL"
        Offset = 0
        Limit = 3000

    class DescribeRecordListRequest:
        Domain = ""
        Offset = 0
        Limit = 3000

    credential_mod.Credential = Credential
    http_profile_mod.HttpProfile = HttpProfile
    client_profile_mod.ClientProfile = ClientProfile
    sdk_exc_mod.TencentCloudSDKException = _FakeSDKException
    dnspod_client_mod.DnspodClient = lambda cred, region, profile=None: client
    dnspod_models_mod.DescribeDomainListRequest = DescribeDomainListRequest
    dnspod_models_mod.DescribeRecordListRequest = DescribeRecordListRequest

    for name, mod in {
        "tencentcloud": tencentcloud,
        "tencentcloud.common": common,
        "tencentcloud.common.credential": credential_mod,
        "tencentcloud.common.profile": profile,
        "tencentcloud.common.profile.client_profile": client_profile_mod,
        "tencentcloud.common.profile.http_profile": http_profile_mod,
        "tencentcloud.common.exception": exception_mod,
        "tencentcloud.common.exception.tencent_cloud_sdk_exception": sdk_exc_mod,
        "tencentcloud.dnspod": dnspod_pkg,
        "tencentcloud.dnspod.v20210323": dnspod_ver,
        "tencentcloud.dnspod.v20210323.dnspod_client": dnspod_client_mod,
        "tencentcloud.dnspod.v20210323.models": dnspod_models_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_tencent_domains_paginates(monkeypatch):
    import app.services.dns_providers.tencent as module
    monkeypatch.setattr(module, "PAGE_SIZE", 1)
    api = _FakeDnspodClient([_FakeItem(Name="a.example.com", DomainId=1), _FakeItem(Name="b.example.com", DomainId=2)], [])
    _install_tencent_stub(monkeypatch, api)
    assert TencentDNS("ak", "sk").list_zones() == [DNSZone("1", "a.example.com"), DNSZone("2", "b.example.com")]


def test_tencent_same_value_on_different_lines_preserves_remote_ids(monkeypatch):
    api = _FakeDnspodClient([], [
        _FakeItem(RecordId=1, Name="@", Type="A", Value="1.2.3.4", TTL=600, Line="默认"),
        _FakeItem(RecordId=2, Name="@", Type="A", Value="1.2.3.4", TTL=600, Line="境外"),
    ])
    _install_tencent_stub(monkeypatch, api)
    rows = TencentDNS("ak", "sk").list_records(DNSZone("z1", "example.com"))
    assert [row.id for row in rows] == ["1", "2"]
    assert all(row.read_only for row in rows)
    assert "境外" in rows[1].remark


def test_tencent_sdk_error_is_sanitized(monkeypatch):
    api = _FakeDnspodClient([], [], _FakeSDKException("AuthFailure.SecretIdNotFound", "private-secret-must-not-leak"))
    _install_tencent_stub(monkeypatch, api)
    with pytest.raises(DNSProviderError) as caught:
        TencentDNS("ak", "sk").list_zones()
    assert "SecretIdNotFound" in str(caught.value)
    assert "private-secret" not in str(caught.value)


def test_tencent_unknown_type_and_dotted_host_remain_readonly(monkeypatch):
    api = _FakeDnspodClient([], [_FakeItem(RecordId=7, Name="a.b", Type="SRV", Value="0 1 443 target.example.com", TTL=600)])
    _install_tencent_stub(monkeypatch, api)
    rows = TencentDNS("ak", "sk").list_records(DNSZone("z1", "example.com"))
    assert rows[0].host == "a.b" and rows[0].record_type == "SRV" and rows[0].read_only


def test_parse_access_key_content_accepts_secret_id_alias():
    assert domains.parse_access_key_content('{"SecretId":"AKIDxxx","SecretKey":"yyy"}') == ("AKIDxxx", "yyy")
    assert domains.parse_access_key_content('SecretId=AKIDxxx\nSecretKey=yyy') == ("AKIDxxx", "yyy")


def test_domain_update_model_accepts_optional_account_and_provider():
    assert domains.DomainUpdate().account_id is None
    assert domains.DomainUpdate(account_id="cred").account_id == "cred"
    assert domains.DomainUpdate(account_id="", provider="huawei").provider == "huawei"


def test_tencent_sync_does_not_silently_succeed_on_write():
    with pytest.raises(DNSProviderError, match="仅接入读取"):
        TencentDNS("ak", "sk").delete_record(DNSZone("1", "example.com"), "r1")


def test_huawei_official_sdk_client_enforces_tls_and_timeout(monkeypatch):
    import app.services.dns_providers.huawei as module
    builder = Mock()
    for method in ("with_credentials", "with_region", "with_http_config"):
        getattr(builder, method).return_value = builder
    monkeypatch.setattr(module.DnsClient, "new_builder", lambda: builder)
    HuaweiDNS("ak", "sk")._client()
    config = builder.with_http_config.call_args.args[0]
    assert config.timeout == (5, 15)
    assert config.ignore_ssl_verification is False and config.allow_redirects is False


def test_huawei_zones_strip_trailing_dot():
    api = Mock()
    api.list_public_zones.return_value = SimpleNamespace(zones=[SimpleNamespace(id="z1", name="example.com.")])
    assert HuaweiDNS("ak", "sk", client=api).list_zones() == [DNSZone("z1", "example.com")]


def test_huawei_multivalue_and_unknown_types_are_not_discarded():
    api = Mock()
    api.list_record_sets_by_zone.return_value = SimpleNamespace(recordsets=[
        SimpleNamespace(id="a", zone_id="z1", name="www.example.com.", type="A", records=["1.2.3.4", "5.6.7.8"], ttl=300, description="", default=False),
        SimpleNamespace(id="soa", zone_id="z1", name="example.com.", type="SOA", records=["ns admin 1 7200 900 1209600 300"], ttl=300, description="", default=True),
    ])
    rows = HuaweiDNS("ak", "sk", client=api).list_records(DNSZone("z1", "example.com"))
    assert rows[0].values == ["1.2.3.4", "5.6.7.8"] and rows[0].id == "a"
    assert rows[1].record_type == "SOA" and rows[1].read_only


def test_huawei_falls_back_to_second_official_region(monkeypatch):
    adapter = HuaweiDNS("ak", "sk")
    first, second = Mock(), Mock()
    first.list_public_zones.return_value = SimpleNamespace(zones=[])
    second.list_public_zones.return_value = SimpleNamespace(zones=[SimpleNamespace(id="z1", name="example.com.")])
    monkeypatch.setattr(adapter, "_client", lambda: first if adapter._region == "cn-north-4" else second)
    assert adapter.list_zones() == [DNSZone("z1", "example.com")]
    assert adapter._region == "ap-southeast-3"


def test_huawei_missing_zone_raises_without_returning_empty_snapshot():
    api = Mock()
    api.list_public_zones.return_value = SimpleNamespace(zones=[])
    with pytest.raises(DNSProviderError, match="归属"):
        HuaweiDNS("ak", "sk", client=api).get_zone("not-hosted.example")
