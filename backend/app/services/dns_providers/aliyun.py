"""阿里云兼容适配器，复用原有签名实现，统一上层业务入口。"""

from fastapi import HTTPException

from app.services.dns_providers.base import DNSProviderError, DNSRecord, DNSZone, RECORD_TYPES, canonical_domain, writable


class AliyunDNS:
    def __init__(self, access_key: str, secret_key: str):
        self._access_key, self._secret_key = access_key, secret_key

    def _call(self, name: str, **kwargs):
        # 仅在运行时加载，保留已有函数的导入/mock兼容，不造成路由初始化循环。
        from app.api import domains

        try:
            return getattr(domains, name)(access_key=self._access_key, secret_key=self._secret_key, **kwargs)
        except HTTPException:
            raise DNSProviderError("阿里云 DNS 操作失败，请检查凭证授权、记录内容及网络") from None

    def list_zones(self) -> list[DNSZone]:
        return [DNSZone(name, canonical_domain(name)) for name in self._call("fetch_aliyun_domains")]

    def get_zone(self, name: str, zone_id: str = "") -> DNSZone:
        name = canonical_domain(name)
        found = next((zone for zone in self.list_zones() if zone.name == name), None)
        if not found or (zone_id and zone_id != found.id):
            raise DNSProviderError("所选凭证无法验证该域名的云端归属", 403)
        return found

    def list_records(self, zone: DNSZone) -> list[DNSRecord]:
        return [DNSRecord(item["provider_record_id"], item["host"], item["record_type"], [item["record_value"]],
                          item["ttl"], item["remark"], priority=item.get("priority"),
                          read_only=item["record_type"] not in RECORD_TYPES or (item["record_type"] == "NS" and item["host"] == "@"))
                for item in self._call("fetch_aliyun_domain_records", domain_name=zone.name)]

    def create_record(self, zone: DNSZone, record: DNSRecord) -> DNSRecord:
        record.id = self._call("add_aliyun_dns_record", domain_name=zone.name, host=record.host,
                               record_type=record.record_type, value=record.values[0], ttl=record.ttl, priority=record.priority)
        if not record.id:
            raise DNSProviderError("阿里云未返回新记录 ID，请同步核对")
        return record

    def _old(self, zone: DNSZone, record_id: str) -> DNSRecord:
        row = next((record for record in self.list_records(zone) if record.id == record_id), None)
        if row is None:
            raise DNSProviderError("云端解析记录不存在，请重新同步", 409)
        writable(row)
        return row

    def update_record(self, zone: DNSZone, record_id: str, record: DNSRecord) -> DNSRecord:
        self._old(zone, record_id)
        self._call("update_aliyun_dns_record_by_id", record_id=record_id, host=record.host,
                   record_type=record.record_type, value=record.values[0], ttl=record.ttl, priority=record.priority)
        record.id = record_id
        return record

    def delete_record(self, zone: DNSZone, record_id: str) -> None:
        self._old(zone, record_id)
        self._call("delete_aliyun_dns_record_by_id", record_id=record_id)
