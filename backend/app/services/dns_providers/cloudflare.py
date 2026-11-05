"""Cloudflare DNS 适配器：固定 v4 官方 API，只接受最小权限 API Token。"""

from urllib.parse import quote

import requests

from app.services.dns_providers.base import (
    DNSProviderError, DNSRecord, DNSZone, RECORD_TYPES, canonical_domain,
    full_name, relative_host, writable,
)

PAGE_SIZE = 50
MAX_PAGES = 200


class CloudflareDNS:
    def __init__(self, token: str, session=None):
        self._token = token
        self._session = session or requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """凭证仅放 header；不跟随重定向，也不把 API 原始错误反射给用户。"""
        try:
            response = self._session.request(
                method, "https://api.cloudflare.com/client/v4" + path,
                headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
                timeout=(5, 15), allow_redirects=False, verify=True, **kwargs,
            )
            if response.status_code in (401, 403):
                raise DNSProviderError("Cloudflare 认证或权限不足，请检查 Token 的 Zone Read / DNS Read / DNS Edit 授权", 403)
            if response.status_code == 404:
                raise DNSProviderError("Cloudflare 域名或记录不存在，请重新同步", 409)
            if response.status_code == 429:
                raise DNSProviderError("Cloudflare 请求受限，请稍后重试", 429)
            if not 200 <= response.status_code < 300:
                raise DNSProviderError("Cloudflare API 调用失败，请检查网络和请求配置")
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                raise DNSProviderError("Cloudflare 拒绝了该操作，请检查记录内容和 Token 授权", 400)
            return body
        except DNSProviderError:
            raise
        except (requests.RequestException, ValueError, TypeError):
            raise DNSProviderError("Cloudflare 请求失败，无法确认云端结果，请同步后核对") from None

    def _pages(self, path: str) -> list[dict]:
        result, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            body = self._request("GET", path, params={"page": page, "per_page": PAGE_SIZE})
            rows = body.get("result")
            if not isinstance(rows, list):
                raise DNSProviderError("Cloudflare 列表响应不完整，已停止同步")
            for row in rows:
                if not row.get("id") or row["id"] in seen:
                    raise DNSProviderError("Cloudflare 分页返回重复或缺失 ID，已停止同步")
                seen.add(row["id"])
                result.append(row)
            pages = (body.get("result_info") or {}).get("total_pages")
            if (pages is not None and page >= int(pages)) or (pages is None and len(rows) < PAGE_SIZE):
                return result
        raise DNSProviderError("Cloudflare 超过单次分页上限，请缩小 Token 的域名授权范围")

    def list_zones(self) -> list[DNSZone]:
        return [DNSZone(row["id"], canonical_domain(row["name"])) for row in self._pages("/zones")]

    def get_zone(self, name: str, zone_id: str = "") -> DNSZone:
        name = canonical_domain(name)
        if zone_id:
            row = self._request("GET", f"/zones/{quote(zone_id, safe='')}")["result"]
            if row.get("id") == zone_id and canonical_domain(row.get("name")) == name:
                return DNSZone(zone_id, name)
        else:
            found = next((zone for zone in self.list_zones() if zone.name == name), None)
            if found:
                return found
        raise DNSProviderError("所选凭证无法验证该域名的云端归属", 403)

    @staticmethod
    def _path(zone: DNSZone, record_id: str = "") -> str:
        path = f"/zones/{quote(zone.id, safe='')}/dns_records"
        return path + ("/" + quote(record_id, safe="") if record_id else "")

    @staticmethod
    def _record(zone: DNSZone, row: dict) -> DNSRecord:
        host = relative_host(row["name"], zone)
        record_type = row["type"]
        return DNSRecord(
            row["id"], host, record_type, [str(row.get("content") or "")], int(row["ttl"]),
            row.get("comment") or "", row.get("proxied"), row.get("priority"),
            record_type not in RECORD_TYPES or (record_type == "NS" and host == "@"),
        )

    def list_records(self, zone: DNSZone) -> list[DNSRecord]:
        return [self._record(zone, row) for row in self._pages(self._path(zone))]

    @staticmethod
    def _body(zone: DNSZone, record: DNSRecord, old: DNSRecord | None = None) -> dict:
        if len(record.values) != 1:
            raise DNSProviderError("Cloudflare 每条记录只接受一个值，请分别添加", 400)
        if record.proxied and record.record_type not in ("A", "AAAA", "CNAME"):
            raise DNSProviderError("只有 A / AAAA / CNAME 记录支持 Cloudflare 代理", 400)
        proxied = record.proxied if record.proxied is not None else (old.proxied if old else False)
        body = {"name": full_name(record.host, zone), "type": record.record_type,
                "content": record.values[0], "ttl": 1 if proxied else record.ttl, "comment": record.remark}
        if record.proxied is not None:
            body["proxied"] = record.proxied
        if record.record_type == "MX":
            body["priority"] = record.priority if record.priority is not None else (old.priority if old else 10)
        return body

    def create_record(self, zone: DNSZone, record: DNSRecord) -> DNSRecord:
        body = self._request("POST", self._path(zone), json=self._body(zone, record))
        return self._record(zone, body["result"])

    def update_record(self, zone: DNSZone, record_id: str, record: DNSRecord) -> DNSRecord:
        old = self._record(zone, self._request("GET", self._path(zone, record_id))["result"])
        writable(old)
        body = self._request("PATCH", self._path(zone, record_id), json=self._body(zone, record, old))
        return self._record(zone, body["result"])

    def delete_record(self, zone: DNSZone, record_id: str) -> None:
        old = self._record(zone, self._request("GET", self._path(zone, record_id))["result"])
        writable(old)
        self._request("DELETE", self._path(zone, record_id))
