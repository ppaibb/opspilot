"""统一 DNS 适配器行为测试，HTTP / SDK 边界替换为可核对的响应。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.dns_providers.base import DNSRecord, DNSZone, DNSProviderError
from app.services.dns_providers.cloudflare import CloudflareDNS
from app.services.dns_providers.huawei import HuaweiDNS
from app.services.dns_providers.registry import provider_metadata
from app.services.dns_providers.aliyun import AliyunDNS


def response(result, total_pages=1, status=200, success=True):
    return SimpleNamespace(status_code=status, json=lambda: {
        "success": success, "result": result, "result_info": {"total_pages": total_pages},
        "errors": [{"message": "secret-token-must-not-leak"}] if not success else [],
    })


def cf_record(**overrides):
    return dict(id="record-1", name="www.example.com", type="A", content="203.0.113.10",
                ttl=1, proxied=True, proxiable=True, comment="生产") | overrides


def test_cloudflare_zones_are_paginated_and_use_bearer_header():
    http = Mock()
    http.request.side_effect = [response([{"id": "z1", "name": "example.com"}], 2),
                                response([{"id": "z2", "name": "example.net"}], 2)]
    found = CloudflareDNS("fake-token", session=http).list_zones()
    assert [zone.id for zone in found] == ["z1", "z2"]
    assert http.request.call_args_list[1].kwargs["params"]["page"] == 2
    assert http.request.call_args.kwargs["headers"]["Authorization"] == "Bearer fake-token"
    assert http.request.call_args.kwargs["allow_redirects"] is False
    assert http.request.call_args.kwargs["verify"] is True


def test_cloudflare_list_keeps_proxy_auto_ttl_mx_and_unknown_types():
    http = Mock()
    http.request.return_value = response([cf_record(), cf_record(id="mx", type="MX", content="mail.example.com", priority=10, proxied=False),
                                          cf_record(id="srv", type="SRV", content="0 1 443 target.example.com")])
    found = CloudflareDNS("fake-token", session=http).list_records(DNSZone("zone-1", "example.com"))
    assert found[0].proxied and found[0].ttl == 1
    assert found[1].priority == 10
    assert found[2].read_only


def test_cloudflare_patch_keeps_proxy_enabled_when_not_overridden():
    http = Mock()
    http.request.side_effect = [response(cf_record()), response(cf_record(content="203.0.113.11"))]
    adapter = CloudflareDNS("fake-token", session=http)
    adapter.update_record(DNSZone("zone-1", "example.com"), "record-1",
                          DNSRecord("", "www", "A", ["203.0.113.11"], ttl=600))
    assert http.request.call_args.args[0] == "PATCH"
    assert http.request.call_args.kwargs["json"]["ttl"] == 1
    assert "proxied" not in http.request.call_args.kwargs["json"]


def test_cloudflare_unauthorized_errors_are_sanitized():
    http = Mock()
    http.request.return_value = response(None, status=403, success=False)
    with pytest.raises(DNSProviderError) as caught:
        CloudflareDNS("fake-token", session=http).list_zones()
    assert "secret-token" not in str(caught.value)
    assert "权限" in str(caught.value)


def test_cloudflare_does_not_accept_redirect_or_external_record_name():
    http = Mock()
    http.request.return_value = response([cf_record(name="elsewhere.net")])
    with pytest.raises(DNSProviderError, match="归属"):
        CloudflareDNS("fake-token", session=http).list_records(DNSZone("zone-1", "example.com"))
    http.request.return_value = response(None, status=302)
    with pytest.raises(DNSProviderError):
        CloudflareDNS("fake-token", session=http).list_zones()


def test_huawei_zones_use_offset_count_and_enterprise_scope(monkeypatch):
    api = Mock()
    api.list_public_zones.side_effect = [SimpleNamespace(zones=[SimpleNamespace(id="z1", name="example.com.")]),
                                        SimpleNamespace(zones=[])]
    adapter = HuaweiDNS("fake-ak", "fake-sk", client=api)
    monkeypatch.setattr("app.services.dns_providers.huawei.PAGE_SIZE", 1)
    assert adapter.list_zones() == [DNSZone("z1", "example.com")]
    calls = api.list_public_zones.call_args_list
    assert [call.args[0].offset for call in calls] == [0, 1]
    assert calls[0].args[0].enterprise_project_id == "all_granted_eps"


def hw_record(**overrides):
    return SimpleNamespace(**(dict(id="r1", zone_id="z1", name="www.example.com.", type="A", records=["203.0.113.1", "203.0.113.2"],
                                  ttl=600, description="生产", default=False) | overrides))


def test_huawei_multivalue_recordset_is_not_split_and_system_records_are_readonly():
    api = Mock()
    api.list_record_sets_by_zone.return_value = SimpleNamespace(recordsets=[hw_record(), hw_record(id="ns", name="example.com.", type="NS", default=True)])
    found = HuaweiDNS("ak", "sk", client=api).list_records(DNSZone("z1", "example.com"))
    assert found[0].values == ["203.0.113.1", "203.0.113.2"]
    assert found[1].read_only


def test_huawei_update_preserves_whole_value_set():
    api = Mock()
    api.show_record_set.return_value = hw_record()
    api.update_record_set.return_value = hw_record(records=["203.0.113.3", "203.0.113.4"])
    HuaweiDNS("ak", "sk", client=api).update_record(DNSZone("z1", "example.com"), "r1",
        DNSRecord("", "www", "A", ["203.0.113.3", "203.0.113.4"]))
    assert api.update_record_set.call_args.args[0].body.records == ["203.0.113.3", "203.0.113.4"]


def test_huawei_foreign_zone_and_system_records_cannot_be_deleted():
    api = Mock()
    adapter = HuaweiDNS("ak", "sk", client=api)
    api.show_record_set.return_value = hw_record(zone_id="another-zone")
    with pytest.raises(DNSProviderError, match="归属"):
        adapter.delete_record(DNSZone("z1", "example.com"), "r1")
    api.show_record_set.return_value = hw_record(default=True)
    with pytest.raises(DNSProviderError, match="只读"):
        adapter.delete_record(DNSZone("z1", "example.com"), "r1")
    api.delete_record_set.assert_not_called()


def test_metadata_only_advertises_implemented_capabilities():
    specs = {item["key"]: item for item in provider_metadata()}
    assert specs["huawei"]["supports_multi_values"]
    assert specs["cloudflare"]["supports_proxy"]
    assert not specs["cloudflare"]["supports_acme"]
    assert not specs["tencent"]["supports_import"]


def test_aliyun_adapter_keeps_remote_id_and_mx_priority_zero(monkeypatch):
    from app.api import domains

    payload = {"TotalCount": 1, "DomainRecords": {"Record": [{
        "RecordId": "ali-record-1", "RR": "@", "Type": "MX", "Value": "mail.example.com", "TTL": 600, "Priority": 0,
    }]}}
    monkeypatch.setattr(domains.requests, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: payload))
    records = AliyunDNS("ak", "sk").list_records(DNSZone("example.com", "example.com"))
    assert records[0].id == "ali-record-1"
    assert records[0].priority == 0


@pytest.mark.parametrize("action", ["create", "update", "delete"])
def test_aliyun_http200_business_error_is_not_success(action, monkeypatch):
    from app.api import domains

    monkeypatch.setattr(domains.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"Code": "InvalidPermission", "Message": "secret-must-not-leak"}))
    adapter = AliyunDNS("ak", "sk")
    old = DNSRecord("ali-record-1", "www", "A", ["203.0.113.1"])
    monkeypatch.setattr(adapter, "list_records", lambda zone: [old])
    with pytest.raises(DNSProviderError) as caught:
        if action == "create":
            adapter.create_record(DNSZone("example.com", "example.com"), old)
        elif action == "update":
            adapter.update_record(DNSZone("example.com", "example.com"), old.id, old)
        else:
            adapter.delete_record(DNSZone("example.com", "example.com"), old.id)
    assert "secret-must-not-leak" not in str(caught.value)
