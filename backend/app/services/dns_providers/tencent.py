"""合入账户同步分支的腾讯云 DNSPod 读取能力；记录按云端ID与线路保留。"""

import re

from app.services.dns_providers.base import DNSProviderError, DNSRecord, DNSZone, canonical_domain

PAGE_SIZE = 3000
MAX_PAGES = 100


class TencentDNS:
    def __init__(self, access_key: str, secret_key: str, client=None):
        self._access_key, self._secret_key, self._api = access_key, secret_key, client

    def _client(self):
        if self._api is None:
            from tencentcloud.common.credential import Credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.dnspod.v20210323.dnspod_client import DnspodClient

            http = HttpProfile()
            http.endpoint, http.reqTimeout = "dnspod.tencentcloudapi.com", 15
            profile = ClientProfile()
            profile.httpProfile = http
            self._api = DnspodClient(Credential(self._access_key, self._secret_key), "ap-guangzhou", profile)
        return self._api

    def _call(self, method: str, request):
        try:
            return getattr(self._client(), method)(request)
        except Exception as exc:
            code = getattr(exc, "code", "")
            # 仅展示结构化的标准错误码，不透传可能含密钥的 SDK message。
            safe_code = code if re.fullmatch(r"(?:AuthFailure|ResourceNotFound|FailedOperation|InvalidParameter|RequestLimitExceeded)(?:\.[A-Za-z0-9]+)*", code) else ""
            if method == "DescribeRecordList" and code == "ResourceNotFound.NoDataOfRecord":
                return None
            raise DNSProviderError(f"腾讯云 DNSPod 查询失败，请检查凭证权限和网络{f'（{safe_code}）' if safe_code else ''}", 400) from None

    def list_zones(self) -> list[DNSZone]:
        from tencentcloud.dnspod.v20210323.models import DescribeDomainListRequest

        result, seen = [], set()
        for page in range(MAX_PAGES):
            request = DescribeDomainListRequest()
            request.Type, request.Offset, request.Limit = "ALL", page * PAGE_SIZE, PAGE_SIZE
            response = self._call("DescribeDomainList", request)
            rows = list(response.DomainList or [])
            for row in rows:
                name = canonical_domain(row.Name)
                identifier = str(getattr(row, "DomainId", None) or getattr(row, "Id", None) or name)
                if identifier in seen:
                    raise DNSProviderError("腾讯云域名分页返回重复ID，已停止同步")
                seen.add(identifier)
                result.append(DNSZone(identifier, name))
            if len(rows) < PAGE_SIZE:
                return result
        raise DNSProviderError("腾讯云域名分页超过单次上限，已停止同步")

    def get_zone(self, name: str, zone_id: str = "") -> DNSZone:
        name = canonical_domain(name)
        found = next((zone for zone in self.list_zones() if zone.name == name and (not zone_id or zone.id == zone_id)), None)
        if found is None:
            raise DNSProviderError("所选凭证无法验证腾讯云域名归属", 403)
        return found

    def list_records(self, zone: DNSZone) -> list[DNSRecord]:
        from tencentcloud.dnspod.v20210323.models import DescribeRecordListRequest

        result, seen = [], set()
        for page in range(MAX_PAGES):
            request = DescribeRecordListRequest()
            request.Domain, request.Offset, request.Limit = zone.name, page * PAGE_SIZE, PAGE_SIZE
            response = self._call("DescribeRecordList", request)
            rows = list(response.RecordList or []) if response is not None else []
            for row in rows:
                identifier = str(getattr(row, "RecordId", "") or "")
                if not identifier or identifier in seen:
                    raise DNSProviderError("腾讯云记录分页返回缺失或重复ID，已停止同步")
                seen.add(identifier)
                remark = str(getattr(row, "Remark", "") or "")
                line = str(getattr(row, "Line", "") or "")
                if line and line != "默认":
                    remark = f"{remark} [线路:{line}]".strip()
                # 同名同值不同线路是不同云端记录，不能折叠掉其中一条。
                result.append(DNSRecord(identifier, row.Name or "@", row.Type, [row.Value],
                    int(row.TTL), remark[:255], priority=getattr(row, "MX", None) if row.Type == "MX" else None,
                    read_only=True))
            if len(rows) < PAGE_SIZE:
                return result
        raise DNSProviderError("腾讯云记录分页超过单次上限，已停止同步")

    def create_record(self, zone: DNSZone, record: DNSRecord) -> DNSRecord:
        raise DNSProviderError("腾讯云DNSPod本次仅接入读取和同步，写操作请在云控制台完成", 400)

    def update_record(self, zone: DNSZone, record_id: str, record: DNSRecord) -> DNSRecord:
        return self.create_record(zone, record)

    def delete_record(self, zone: DNSZone, record_id: str) -> None:
        raise DNSProviderError("腾讯云DNSPod本次仅接入读取和同步，写操作请在云控制台完成", 400)
