"""统一出站请求安全策略，阻断 SSRF、DNS rebinding 与无界响应。

默认只允许 HTTPS 且所有解析地址都必须是公网地址。确需访问企业内网的 K8s
API 或自建模型网关时，管理员必须通过主机名/CIDR 白名单显式授权。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from app.core.config import settings


class OutboundPolicyError(ValueError):
    """出站目标或响应违反安全策略；错误文本可安全返回给调用者。"""


Resolver = Callable[[str], Iterable[str]]


class _PinnedTLSAdapter(HTTPAdapter):
    """连接固定 IP，但用原始域名做 TLS SNI 与证书主机名校验。"""

    def __init__(self, server_hostname: str) -> None:
        self.server_hostname = server_hostname
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["assert_hostname"] = self.server_hostname
        pool_kwargs["server_hostname"] = self.server_hostname
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _default_resolver(host: str) -> set[str]:
    """解析主机全部 A/AAAA 地址，失败统一归类为目标不可解析。"""
    try:
        return {
            item[4][0]
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise OutboundPolicyError("出站目标域名无法解析") from exc


def _csv(raw: str) -> set[str]:
    return {item.strip().lower() for item in str(raw or "").split(",") if item.strip()}


class OutboundPolicy:
    """对 URL、DNS、重定向、超时和响应大小实施统一限制。"""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | None = None,
        allowed_cidrs: set[str] | None = None,
        resolver: Resolver | None = None,
        max_response_bytes: int = 4 * 1024 * 1024,
        timeout_seconds: float = 30,
        max_redirects: int = 3,
    ) -> None:
        self.allowed_hosts = {item.lower().rstrip(".") for item in (allowed_hosts or set())}
        try:
            self.allowed_networks = tuple(
                ipaddress.ip_network(item, strict=False) for item in (allowed_cidrs or set())
            )
        except ValueError as exc:
            raise OutboundPolicyError("出站 CIDR 白名单配置非法") from exc
        self.resolver = resolver or _default_resolver
        self.max_response_bytes = max(int(max_response_bytes), 1)
        self.timeout_seconds = max(float(timeout_seconds), 0.1)
        self.max_redirects = max(int(max_redirects), 0)

    @classmethod
    def from_settings(cls) -> "OutboundPolicy":
        """从集中配置创建策略，避免业务模块各自解释白名单。"""
        return cls(
            allowed_hosts=_csv(settings.OUTBOUND_ALLOWED_HOSTS),
            allowed_cidrs=_csv(settings.OUTBOUND_ALLOWED_CIDRS),
            max_response_bytes=settings.OUTBOUND_MAX_RESPONSE_BYTES,
            timeout_seconds=settings.OUTBOUND_TIMEOUT_SECONDS,
            max_redirects=settings.OUTBOUND_MAX_REDIRECTS,
        )

    def _resolve(self, host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            literal = ipaddress.ip_address(host)
            raw_addresses = {str(literal)}
        except ValueError:
            raw_addresses = set(self.resolver(host))
        if not raw_addresses:
            raise OutboundPolicyError("出站目标域名没有可用地址")
        try:
            return {ipaddress.ip_address(item) for item in raw_addresses}
        except ValueError as exc:
            raise OutboundPolicyError("出站目标解析结果非法") from exc

    def _address_allowed(self, host: str, address) -> bool:
        if host in self.allowed_hosts:
            return True
        if any(address in network for network in self.allowed_networks):
            return True
        return bool(address.is_global)

    def _validate_with_addresses(self, url: str) -> tuple[SplitResult, set]:
        """校验 URL 并返回当次 DNS 快照，供请求前后比较。"""
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme.lower() != "https":
            raise OutboundPolicyError("出站请求只允许 HTTPS")
        if not parsed.hostname:
            raise OutboundPolicyError("出站目标缺少主机名")
        if parsed.username is not None or parsed.password is not None:
            raise OutboundPolicyError("出站 URL 禁止包含用户信息")
        host = parsed.hostname.lower().rstrip(".")
        addresses = self._resolve(host)
        if any(not self._address_allowed(host, address) for address in addresses):
            raise OutboundPolicyError("出站目标指向内网、回环、链路本地或保留地址")
        return parsed, addresses

    def validate_url(self, url: str) -> SplitResult:
        """校验 URL 结构和当前 DNS 全部结果，并返回解析对象。"""
        parsed, _addresses = self._validate_with_addresses(url)
        return parsed

    @staticmethod
    def _origin(parsed: SplitResult) -> tuple[str, str, int]:
        port = parsed.port or 443
        return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), port

    def _cap_timeout(self, requested) -> float | tuple[float, float]:
        if isinstance(requested, tuple):
            return tuple(min(float(item), self.timeout_seconds) for item in requested)  # type: ignore[return-value]
        if requested is None:
            return self.timeout_seconds
        return min(float(requested), self.timeout_seconds)

    def _guard_stream(self, response) -> None:
        """给流式响应的迭代器加累计字节上限。"""
        original_iter_content = getattr(response, "iter_content", None)
        if callable(original_iter_content):
            def iter_content(*args, **kwargs):
                total = 0
                for chunk in original_iter_content(*args, **kwargs):
                    total += len(chunk or b"")
                    if total > self.max_response_bytes:
                        response.close()
                        raise OutboundPolicyError("出站响应过大，已中止读取")
                    yield chunk
            response.iter_content = iter_content

        original_iter_lines = getattr(response, "iter_lines", None)
        if callable(original_iter_lines):
            def iter_lines(*args, **kwargs):
                total = 0
                for line in original_iter_lines(*args, **kwargs):
                    total += len(line.encode("utf-8") if isinstance(line, str) else (line or b"")) + 1
                    if total > self.max_response_bytes:
                        response.close()
                        raise OutboundPolicyError("出站响应过大，已中止读取")
                    yield line
            response.iter_lines = iter_lines

    def _send_pinned(self, method: str, url: str, addresses: set, **kwargs):
        """把本次连接钉到已审核 IP，消除校验与实际连接之间的 DNS TOCTOU。"""
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        address = sorted(addresses, key=lambda item: (item.version, str(item)))[0]
        address_text = f"[{address}]" if address.version == 6 else str(address)
        port = parsed.port
        netloc = f"{address_text}:{port}" if port else address_text
        pinned_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        headers = dict(kwargs.pop("headers", {}) or {})
        if not any(key.lower() == "host" for key in headers):
            default_port = port in (None, 443)
            headers["Host"] = hostname if default_port else f"{hostname}:{port}"

        session = requests.Session()
        # 禁用进程环境代理，避免代理重新解析原目标或把内网目标带出安全边界。
        session.trust_env = False
        session.mount("https://", _PinnedTLSAdapter(hostname))
        try:
            response = session.request(method, pinned_url, headers=headers, **kwargs)
        except Exception:
            session.close()
            raise
        response.url = url
        # 流式响应关闭时一并释放专用 Session；非流式响应内容已读完，也可由
        # Response.close/垃圾回收释放，不共享连接池避免跨目标污染。
        original_close = response.close

        def close_with_session():
            try:
                original_close()
            finally:
                session.close()

        response.close = close_with_session
        return response

    def request(self, method: str, url: str, *, session=None, **kwargs):
        """发送受控请求；逐跳校验重定向并在响应后复查 DNS。"""
        current_url = str(url)
        original, current_ips = self._validate_with_addresses(current_url)
        current = original
        request_method = method.upper()
        for redirect_count in range(self.max_redirects + 1):
            options = dict(kwargs)
            options["allow_redirects"] = False
            options["timeout"] = self._cap_timeout(options.get("timeout"))
            if session is None:
                response = self._send_pinned(
                    request_method,
                    current_url,
                    current_ips,
                    **options,
                )
            else:
                response = session.request(request_method, current_url, **options)
            if getattr(response, "is_redirect", False):
                if redirect_count >= self.max_redirects:
                    response.close()
                    raise OutboundPolicyError("出站重定向次数过多")
                location = str(response.headers.get("location") or "").strip()
                next_url = urljoin(current_url, location)
                unvalidated_next = urlsplit(next_url)
                if self._origin(unvalidated_next) != self._origin(original):
                    response.close()
                    raise OutboundPolicyError("禁止跨域出站重定向")
                next_parsed, next_ips = self._validate_with_addresses(next_url)
                response.close()
                current_url = next_url
                current = next_parsed
                current_ips = next_ips
                if response.status_code == 303:
                    request_method = "GET"
                    kwargs.pop("json", None)
                    kwargs.pop("data", None)
                continue

            final_ips = self._resolve((current.hostname or "").lower().rstrip("."))
            if final_ips != current_ips:
                response.close()
                raise OutboundPolicyError("检测到 DNS 解析结果在请求期间发生变化")
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self.max_response_bytes:
                response.close()
                raise OutboundPolicyError("出站响应过大，已中止读取")
            if kwargs.get("stream"):
                self._guard_stream(response)
            elif len(response.content) > self.max_response_bytes:
                response.close()
                raise OutboundPolicyError("出站响应过大，已中止读取")
            return response
        raise OutboundPolicyError("出站重定向次数过多")


def outbound_request(method: str, url: str, **kwargs):
    """业务模块共用的便捷入口。"""
    return OutboundPolicy.from_settings().request(method, url, **kwargs)
