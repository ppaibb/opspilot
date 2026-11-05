"""华为云公网 DNS：采用官方 SDK，按完整记录集保留多值语义。"""

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.http.http_config import HttpConfig
from huaweicloudsdkdns.v2 import (
    CreateRecordSetRequest, CreateRecordSetRequestBody, DeleteRecordSetRequest, DnsClient,
    ListPublicZonesRequest, ListRecordSetsByZoneRequest, ShowPublicZoneRequest,
    ShowRecordSetRequest, UpdateRecordSetReq, UpdateRecordSetRequest,
)
from huaweicloudsdkdns.v2.region.dns_region import DnsRegion

from app.services.dns_providers.base import (
    DNSProviderError, DNSRecord, DNSZone, RECORD_TYPES, canonical_domain,
    full_name, relative_host, writable,
)

PAGE_SIZE = 100
MAX_PAGES = 200
DNS_REGIONS = ("cn-north-4", "ap-southeast-3")


class HuaweiDNS:
    def __init__(self, access_key: str, secret_key: str, client=None):
        self._access_key, self._secret_key = access_key, secret_key
        self._api = client
        self._injected_client = client is not None
        self._region = DNS_REGIONS[0]

    def _client(self):
        if self._api is None:
            config = HttpConfig.get_default_config()
            config.timeout, config.ignore_ssl_verification, config.allow_redirects = (5, 15), False, False
            # 公网域名从固定官方地域入口管理；认证方式与官方 DNS SDK 示例一致。
            credentials = BasicCredentials(self._access_key, self._secret_key).with_iam_endpoint(
                f"https://iam.{self._region}.myhuaweicloud.com"
            )
            self._api = (DnsClient.new_builder().with_credentials(credentials)
                         .with_region(DnsRegion.static_fields[self._region]).with_http_config(config).build())
        return self._api

    def _call(self, method: str, request):
        try:
            return getattr(self._client(), method)(request)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise DNSProviderError("华为云 DNS 认证或权限不足，请检查 IAM 项目和 DNS 授权", 403) from None
            if status == 404:
                raise DNSProviderError("华为云域名或记录不存在，请重新同步", 409) from None
            raise DNSProviderError("华为云 DNS 请求失败，无法确认云端结果，请同步后核对") from None

    def list_zones(self) -> list[DNSZone]:
        """保留另一分支的区域回退能力，仍由官方SDK签名，不再维护手写HMAC。"""
        if self._injected_client:
            return self._list_zones_current()
        last_error, succeeded = None, False
        for region in DNS_REGIONS:
            if self._region != region:
                self._region, self._api = region, None
            try:
                zones = self._list_zones_current()
                succeeded = True
                if zones:
                    return zones
            except DNSProviderError as exc:
                last_error = exc
        if succeeded:
            return []
        raise last_error or DNSProviderError("华为云DNS区域查询失败")

    def _list_zones_current(self) -> list[DNSZone]:
        result, seen = [], set()
        for page in range(MAX_PAGES):
            response = self._call("list_public_zones", ListPublicZonesRequest(
                type="public", offset=page * PAGE_SIZE, limit=PAGE_SIZE, enterprise_project_id="all_granted_eps",
            ))
            rows = response.zones or []
            for row in rows:
                if not row.id or row.id in seen:
                    raise DNSProviderError("华为云域名分页返回重复 ID，已停止同步")
                seen.add(row.id)
                result.append(DNSZone(row.id, canonical_domain(row.name)))
            if len(rows) < PAGE_SIZE:
                return result
        raise DNSProviderError("华为云域名超过单次分页上限")

    def get_zone(self, name: str, zone_id: str = "") -> DNSZone:
        name = canonical_domain(name)
        if zone_id:
            regions = (self._region,) if self._injected_client else tuple(dict.fromkeys((self._region, *DNS_REGIONS)))
            for region in regions:
                if self._region != region:
                    self._region, self._api = region, None
                try:
                    row = self._call("show_public_zone", ShowPublicZoneRequest(zone_id=zone_id))
                    if row.id == zone_id and canonical_domain(row.name) == name:
                        return DNSZone(zone_id, name)
                except DNSProviderError:
                    if self._injected_client:
                        raise
        else:
            found = next((zone for zone in self.list_zones() if zone.name == name), None)
            if found:
                return found
        raise DNSProviderError("所选凭证无法验证该域名的云端归属", 403)

    @staticmethod
    def _record(zone: DNSZone, row) -> DNSRecord:
        if getattr(row, "zone_id", None) not in (None, "", zone.id):
            raise DNSProviderError("云端记录集归属与当前域名不一致", 409)
        host = relative_host(row.name, zone)
        return DNSRecord(row.id, host, row.type, list(row.records or []), int(row.ttl), row.description or "",
                         read_only=bool(getattr(row, "default", False)) or row.type not in RECORD_TYPES
                         or (row.type == "NS" and host == "@"))

    def list_records(self, zone: DNSZone) -> list[DNSRecord]:
        result, seen = [], set()
        for page in range(MAX_PAGES):
            response = self._call("list_record_sets_by_zone", ListRecordSetsByZoneRequest(
                zone_id=zone.id, limit=PAGE_SIZE, offset=page * PAGE_SIZE,
            ))
            rows = response.recordsets or []
            for row in rows:
                if not row.id or row.id in seen:
                    raise DNSProviderError("华为云记录集分页返回重复 ID，已停止同步")
                seen.add(row.id)
                result.append(self._record(zone, row))
            if len(rows) < PAGE_SIZE:
                return result
        raise DNSProviderError("华为云记录集超过单次分页上限")

    def create_record(self, zone: DNSZone, record: DNSRecord) -> DNSRecord:
        response = self._call("create_record_set", CreateRecordSetRequest(zone_id=zone.id, body=CreateRecordSetRequestBody(
            name=full_name(record.host, zone) + ".", type=record.record_type,
            records=record.values, ttl=record.ttl, description=record.remark,
        )))
        return self._record(zone, response)

    def update_record(self, zone: DNSZone, record_id: str, record: DNSRecord) -> DNSRecord:
        old = self._record(zone, self._call("show_record_set", ShowRecordSetRequest(zone_id=zone.id, recordset_id=record_id)))
        writable(old)
        if (old.host, old.record_type) != (record.host, record.record_type):
            raise DNSProviderError("华为云现有记录集的名称和类型不可在此修改，请新增记录后删除旧记录", 400)
        response = self._call("update_record_set", UpdateRecordSetRequest(zone_id=zone.id, recordset_id=record_id, body=UpdateRecordSetReq(
            ttl=record.ttl, records=record.values, description=record.remark,
        )))
        return self._record(zone, response)

    def delete_record(self, zone: DNSZone, record_id: str) -> None:
        old = self._record(zone, self._call("show_record_set", ShowRecordSetRequest(zone_id=zone.id, recordset_id=record_id)))
        writable(old)
        self._call("delete_record_set", DeleteRecordSetRequest(zone_id=zone.id, recordset_id=record_id))
