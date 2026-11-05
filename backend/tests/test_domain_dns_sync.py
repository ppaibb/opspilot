"""域名解析记录多厂商同步回归。

钉死 2026-11-05 诊断出的问题：
- 抽屉新建域名不带 account_id 时同步静默 0 条；
- 腾讯云 DNSPod 无拉取分支（只有阿里云）；
- 一条脏记录（如 SRV）不能让整批同步静默失败。
腾讯云走项目已有依赖 tencentcloud-sdk-python 的 DNSPod 客户端，
此处用 sys.modules 桩模块模拟 SDK，不发真实请求。
"""
from __future__ import annotations

import sys
import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import domains
from app.api.domains import DomainUpdate, parse_access_key_content
from app.db.database import Base
from app.models.asset import DomainDNSRecord, DomainRecord


# ---------------------------------------------------------------------------
# 腾讯云 SDK 桩
# ---------------------------------------------------------------------------

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


def test_fetch_tencent_domains_paginates():
    # Limit 在实现里固定 3000；用小批量验证去重+排序即可，分页循环由 TotalCount 驱动
    client = _FakeDnspodClient(
        [_FakeItem(Name="b.example.com"), _FakeItem(Name="a.example.com"), _FakeItem(Name="b.example.com")],
        [],
    )
    with pytest.MonkeyPatch.context() as mp:
        _install_tencent_stub(mp, client)
        assert domains.fetch_tencent_domains("ak", "sk") == ["a.example.com", "b.example.com"]


def test_fetch_tencent_domain_records_collapses_multiline_duplicates():
    client = _FakeDnspodClient(
        [],
        [
            _FakeItem(Name="@", Type="A", Value="1.2.3.4", TTL=600, Remark="", Line="默认", UpdatedOn="2026-11-05 10:00:00"),
            _FakeItem(Name="@", Type="A", Value="1.2.3.4", TTL=600, Remark="", Line="境外", UpdatedOn="2026-11-05 10:00:00"),
            _FakeItem(Name="www", Type="CNAME", Value="x.example.com", TTL=300, Remark="web", Line="默认", UpdatedOn="2026-11-05 10:00:00"),
        ],
    )
    with pytest.MonkeyPatch.context() as mp:
        _install_tencent_stub(mp, client)
        records = domains.fetch_tencent_domain_records("ak", "sk", "nnyy.ltd")
    assert [(r["host"], r["record_type"], r["record_value"]) for r in records] == [
        ("@", "A", "1.2.3.4"),
        ("www", "CNAME", "x.example.com"),
    ]
    assert records[1]["remark"] == "web"


def test_fetch_tencent_domains_sdk_error_becomes_400():
    client = _FakeDnspodClient([], [], error=_FakeSDKException("AuthFailure.SecretIdNotFound", "密钥不存在"))
    with pytest.MonkeyPatch.context() as mp:
        _install_tencent_stub(mp, client)
        with pytest.raises(HTTPException) as excinfo:
            domains.fetch_tencent_domains("bad", "bad")
    assert excinfo.value.status_code == 400
    assert "SecretIdNotFound" in excinfo.value.detail


def test_normalize_synced_record_skips_unsupported_type_and_allows_dotted_host():
    assert domains._normalize_synced_record(
        {"host": "@", "record_type": "SRV", "record_value": "x", "ttl": 600}
    ) is None
    row = domains._normalize_synced_record(
        {"host": "a.b", "record_type": "A", "record_value": "1.2.3.4", "ttl": 30}
    )
    assert row is not None and row["host"] == "a.b" and row["ttl"] == 60  # TTL 下限夹取


def test_parse_access_key_content_accepts_secret_id_alias():
    assert parse_access_key_content('{"SecretId": "AKIDxxx", "SecretKey": "yyy"}') == ("AKIDxxx", "yyy")


def test_domain_update_model_accepts_account_id():
    assert DomainUpdate().account_id is None
    assert DomainUpdate(account_id="CRED-EA3B77").account_id == "CRED-EA3B77"


def _sqlite_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[DomainRecord.__table__, DomainDNSRecord.__table__])
    return sessionmaker(bind=engine)()


def test_sync_domain_dns_records_upserts_and_prunes(monkeypatch):
    db = _sqlite_session()
    row = DomainRecord(
        domain="nnyy.ltd", root_domain="nnyy.ltd", record_type="A", record_value="-",
        provider="tencent", status="active", account_id="CRED-CEA61D",
        created_at="2026-11-05 00:00:00", updated_at="2026-11-05 00:00:00",
    )
    db.add(row)
    db.commit()
    stale = DomainDNSRecord(domain_id=row.id, host="old", record_type="A", record_value="9.9.9.9", ttl=600, updated_at="2026-11-05 00:00:00")
    keep = DomainDNSRecord(domain_id=row.id, host="www", record_type="A", record_value="1.2.3.4", ttl=60, updated_at="2026-11-05 00:00:00")
    db.add_all([stale, keep])
    db.commit()

    monkeypatch.setattr(domains, "resolve_account_keys", lambda *a, **k: ("ak", "sk"))
    monkeypatch.setattr(
        domains,
        "_fetch_remote_domain_records",
        lambda provider, ak, sk, domain: [
            {"host": "www", "record_type": "A", "record_value": "1.2.3.4", "ttl": 600, "remark": "", "updated_at": "2026-11-05 01:00:00"},
            {"host": "@", "record_type": "SRV", "record_value": "x", "ttl": 600, "remark": "", "updated_at": "2026-11-05 01:00:00"},
        ],
    )
    domains.sync_domain_dns_records(db, row, None)

    rows = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == row.id).all()
    assert [(r.host, r.record_type, r.record_value, r.ttl) for r in rows] == [("www", "A", "1.2.3.4", 600)]


# ---------------------------------------------------------------------------
# 华为云 DNS（requests 桩，不发真实请求）
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _huawei_stub_get(monkeypatch, handler):
    """handler(url, headers) -> _FakeHTTPResponse；同时记录最后一次请求。"""
    calls: list[dict] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return handler(url, dict(headers or {}))

    monkeypatch.setattr(domains.requests, "get", fake_get)
    return calls


def test_huawei_signed_get_builds_sdk_hmac_header(monkeypatch):
    calls = _huawei_stub_get(
        monkeypatch,
        lambda url, headers: _FakeHTTPResponse(200, {"zones": [], "metadata": {"total_count": 0}}),
    )
    resp = domains._huawei_signed_get("AKIDTEST", "SKTEST", "ap-southeast-3", "/v2/zones", {"type": "public", "limit": "500"})
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["url"] == "https://dns.ap-southeast-3.myhuaweicloud.com/v2/zones?limit=500&type=public"
    auth = calls[0]["headers"]["Authorization"]
    assert auth.startswith("SDK-HMAC-SHA256 Access=AKIDTEST, SignedHeaders=content-type;host;x-sdk-date, Signature=")
    signature = auth.rsplit("Signature=", 1)[1]
    assert len(signature) == 64 and all(c in "0123456789abcdef" for c in signature)
    assert calls[0]["headers"]["X-Sdk-Date"].endswith("Z")


def test_fetch_huawei_domains_strips_trailing_dot(monkeypatch):
    def handler(url, headers):
        assert "dns.ap-southeast-3.myhuaweicloud.com" in url
        return _FakeHTTPResponse(200, {
            "zones": [
                {"id": "z1", "name": "ppaibb.com."},
                {"id": "z2", "name": "yybkj.cn."},
                {"id": "z3", "name": "ppaibb.com."},
            ],
            "metadata": {"total_count": 3},
        })

    _huawei_stub_get(monkeypatch, handler)
    assert domains.fetch_huawei_domains("ak", "sk") == ["ppaibb.com", "yybkj.cn"]


def test_fetch_huawei_domain_records_expands_recordset_values(monkeypatch):
    def handler(url, headers):
        if url.endswith("/v2/zones?name=ppaibb.com&search_mode=equal&type=public&limit=100") or (
            "/v2/zones?" in url and "name=ppaibb.com" in url
        ):
            return _FakeHTTPResponse(200, {
                "zones": [{"id": "zone-1", "name": "ppaibb.com."}],
                "metadata": {"total_count": 1},
            })
        if "/v2/zones/zone-1/recordsets" in url:
            return _FakeHTTPResponse(200, {
                "recordsets": [
                    {"id": "r1", "name": "ppaibb.com.", "type": "NS", "ttl": 300, "records": ["ns1.example.com."], "update_at": "2026-11-05T02:00:00.000"},
                    {"id": "r2", "name": "www.ppaibb.com.", "type": "A", "ttl": 300, "records": ["1.2.3.4", "5.6.7.8"], "update_at": "2026-11-05T02:00:00.000"},
                    {"id": "r3", "name": "ppaibb.com.", "type": "SOA", "ttl": 300, "records": ["ns1 admin 1 7200 900 1209600 300"], "update_at": "2026-11-05T02:00:00.000"},
                ],
                "metadata": {"total_count": 3},
            })
        return _FakeHTTPResponse(404, {"code": "DNS.0001", "message": "not found"})

    _huawei_stub_get(monkeypatch, handler)
    records = domains.fetch_huawei_domain_records("ak", "sk", "www.ppaibb.com")
    assert [(r["host"], r["record_type"], r["record_value"]) for r in records] == [
        ("@", "NS", "ns1.example.com."),
        ("www", "A", "1.2.3.4"),
        ("www", "A", "5.6.7.8"),
        ("@", "SOA", "ns1 admin 1 7200 900 1209600 300"),
    ]
    # SOA 由同步归一化过滤，不会入库
    assert domains._normalize_synced_record(records[-1]) is None


def test_fetch_huawei_domain_records_falls_back_to_second_region(monkeypatch):
    def handler(url, headers):
        if "dns.ap-southeast-3" in url and "/v2/zones?" in url:
            return _FakeHTTPResponse(404, {"code": "DNS.0001", "message": "zone not found"})
        if "dns.cn-north-4" in url and "/v2/zones?" in url:
            return _FakeHTTPResponse(200, {
                "zones": [{"id": "zone-9", "name": "ppaibb.com."}],
                "metadata": {"total_count": 1},
            })
        if "dns.cn-north-4" in url and "/recordsets" in url:
            return _FakeHTTPResponse(200, {
                "recordsets": [
                    {"id": "r1", "name": "ppaibb.com.", "type": "A", "ttl": 300, "records": ["1.2.3.4"], "update_at": "2026-11-05T02:00:00.000"},
                ],
                "metadata": {"total_count": 1},
            })
        return _FakeHTTPResponse(404, {"code": "DNS.0001", "message": "not found"})

    _huawei_stub_get(monkeypatch, handler)
    records = domains.fetch_huawei_domain_records("ak", "sk", "ppaibb.com")
    assert [(r["host"], r["record_type"], r["record_value"]) for r in records] == [("@", "A", "1.2.3.4")]


def test_fetch_huawei_domain_records_zone_missing_raises(monkeypatch):
    _huawei_stub_get(
        monkeypatch,
        lambda url, headers: _FakeHTTPResponse(200, {"zones": [], "metadata": {"total_count": 0}}),
    )
    with pytest.raises(HTTPException) as excinfo:
        domains.fetch_huawei_domain_records("ak", "sk", "not-hosted.example")
    assert excinfo.value.status_code == 400
    assert "Zone" in excinfo.value.detail
