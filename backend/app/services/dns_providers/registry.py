"""厂商能力与构造入口。页面只读取真实能力；新增 DNS 厂商只改适配器与注册项。"""

from dataclasses import dataclass
from typing import Callable

from app.services.dns_providers.base import DNSAdapter, DNSProviderError, RECORD_TYPES
from app.services.dns_providers.cloudflare import CloudflareDNS
from app.services.dns_providers.huawei import HuaweiDNS
from app.services.dns_providers.tencent import TencentDNS


@dataclass(frozen=True)
class DNSProviderSpec:
    key: str
    label: str
    credential_types: tuple[str, ...]
    factory: Callable[..., DNSAdapter]
    supports_multi_values: bool = False
    supports_proxy: bool = False
    supports_priority: bool = False
    can_rename_records: bool = True
    supports_acme: bool = False
    supports_record_write: bool = True


def _aliyun_factory(access_key: str, secret_key: str):
    from app.services.dns_providers.aliyun import AliyunDNS

    return AliyunDNS(access_key, secret_key)


DNS_PROVIDERS = {
    spec.key: spec for spec in (
        DNSProviderSpec("aliyun", "阿里云 DNS", ("AccessKey",), _aliyun_factory, supports_priority=True, supports_acme=True),
        DNSProviderSpec("huawei", "华为云 DNS", ("AccessKey",), HuaweiDNS, supports_multi_values=True, can_rename_records=False),
        DNSProviderSpec("cloudflare", "Cloudflare DNS", ("Token",), CloudflareDNS, supports_proxy=True, supports_priority=True),
        DNSProviderSpec("tencent", "腾讯云 DNSPod", ("AccessKey",), TencentDNS, supports_priority=True, supports_record_write=False, supports_acme=True),
    )
}


def get_provider(key: str) -> DNSProviderSpec:
    if key not in DNS_PROVIDERS:
        raise DNSProviderError("该厂商尚未接入云端域名 API，请使用自定义本地域名或接入适配器", 400)
    return DNS_PROVIDERS[key]


def provider_metadata() -> list[dict]:
    result = [{
        "key": spec.key, "label": spec.label, "supports_import": True, "supports_dns_records": True,
        "credential_types": list(spec.credential_types), "supports_multi_values": spec.supports_multi_values,
        "supports_proxy": spec.supports_proxy, "supports_priority": spec.supports_priority,
        "can_rename_records": spec.can_rename_records, "record_types": list(RECORD_TYPES),
        "supports_acme": spec.supports_acme, "supports_cert_distribution": spec.key == "aliyun",
        "supports_record_write": spec.supports_record_write,
    } for spec in DNS_PROVIDERS.values()]
    # 历史资产可以继续展示其厂商，但不能把尚未实现的能力标记为可用。
    for key, label in (("aws", "AWS Route53"), ("azure", "Azure DNS"),
                       ("gcp", "Google Cloud DNS"), ("digitalocean", "DigitalOcean DNS"),
                       ("godaddy", "GoDaddy DNS"), ("custom", "自定义（本地记录）")):
        result.append({"key": key, "label": label, "supports_import": False,
                       "supports_dns_records": key == "custom", "credential_types": [],
                       "supports_multi_values": False, "supports_proxy": False, "supports_priority": False,
                       "can_rename_records": True, "record_types": list(RECORD_TYPES),
                       "supports_acme": False, "supports_cert_distribution": False})
        result[-1]["supports_record_write"] = key == "custom"
    return result
