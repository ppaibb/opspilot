"""DNS 厂商共用数据契约；不包含数据库、HTTP 路由或凭证明文持久化。"""

from dataclasses import dataclass
from typing import Protocol


class DNSProviderError(RuntimeError):
    """只允许放入可安全展示的错误，不携带原始响应或认证请求。"""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DNSZone:
    id: str
    name: str


@dataclass
class DNSRecord:
    """一个对象对应一个云端记录 ID；华为云多值记录集绝不拆成多行。"""

    id: str
    host: str
    record_type: str
    values: list[str]
    ttl: int = 600
    remark: str = ""
    proxied: bool | None = None
    priority: int | None = None
    read_only: bool = False


RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "NS")


def canonical_domain(value: str) -> str:
    """统一尾点、大小写和国际化域名，比较云端归属时使用完整 DNS 标签。"""
    try:
        return value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        raise DNSProviderError("云端域名格式不合法", 400) from None


def relative_host(name: str, zone: DNSZone) -> str:
    name, root = canonical_domain(name), canonical_domain(zone.name)
    if name == root:
        return "@"
    if not name.endswith("." + root):
        raise DNSProviderError("云端解析记录归属与当前域名不一致", 409)
    return name[:-(len(root) + 1)]


def full_name(host: str, zone: DNSZone) -> str:
    return zone.name if host == "@" else f"{host}.{zone.name}"


def writable(record: DNSRecord) -> None:
    if record.read_only:
        raise DNSProviderError("系统记录或暂不支持的记录类型为只读，请在云控制台管理", 400)


class DNSAdapter(Protocol):
    """适配器必须负责完整分页、归属校验、保留厂商语义和脱敏错误。"""

    def list_zones(self) -> list[DNSZone]: ...
    def get_zone(self, name: str, zone_id: str = "") -> DNSZone: ...
    def list_records(self, zone: DNSZone) -> list[DNSRecord]: ...
    def create_record(self, zone: DNSZone, record: DNSRecord) -> DNSRecord: ...
    def update_record(self, zone: DNSZone, record_id: str, record: DNSRecord) -> DNSRecord: ...
    def delete_record(self, zone: DNSZone, record_id: str) -> None: ...
