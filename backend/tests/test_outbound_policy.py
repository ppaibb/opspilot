"""统一 SSRF 出站策略契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import pytest


def _resolver(mapping):
    def resolve(host: str):
        return mapping[host]

    return resolve


@pytest.mark.parametrize(
    "url, addresses",
    [
        ("https://loop.example/api", ["127.0.0.1"]),
        ("https://link.example/api", ["169.254.169.254"]),
        ("https://private.example/api", ["10.0.0.8"]),
        ("http://public.example/api", ["203.0.113.8"]),
        ("https://user:pass@public.example/api", ["203.0.113.8"]),
    ],
)
def test_blocks_unsafe_urls(url, addresses):
    from app.core.outbound_policy import OutboundPolicy, OutboundPolicyError

    policy = OutboundPolicy(resolver=_resolver({urlsplit(url).hostname: addresses}))
    with pytest.raises(OutboundPolicyError):
        policy.validate_url(url)


def test_explicit_host_and_cidr_allow_private_targets():
    from app.core.outbound_policy import OutboundPolicy

    resolver = _resolver({"cluster.internal": ["10.1.2.3"], "db.internal": ["172.16.5.4"]})
    by_host = OutboundPolicy(resolver=resolver, allowed_hosts={"cluster.internal"})
    by_cidr = OutboundPolicy(resolver=resolver, allowed_cidrs={"172.16.0.0/16"})
    assert by_host.validate_url("https://cluster.internal:6443").hostname == "cluster.internal"
    assert by_cidr.validate_url("https://db.internal/api").hostname == "db.internal"


@dataclass
class _Response:
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b"{}"
    url: str = ""

    @property
    def is_redirect(self):
        return self.status_code in {301, 302, 303, 307, 308}

    def close(self):
        return None


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)

    def request(self, _method, url, **_kwargs):
        response = self.responses.pop(0)
        response.url = url
        return response


def test_dns_rebinding_is_rejected_after_request():
    from app.core.outbound_policy import OutboundPolicy, OutboundPolicyError

    answers = iter([["8.8.8.8"], ["127.0.0.1"]])
    policy = OutboundPolicy(resolver=lambda _host: next(answers))
    with pytest.raises(OutboundPolicyError, match="DNS"):
        policy.request("GET", "https://public.example/api", session=_Session([_Response()]))


def test_cross_origin_redirect_is_rejected():
    from app.core.outbound_policy import OutboundPolicy, OutboundPolicyError

    policy = OutboundPolicy(resolver=_resolver({"a.example": ["8.8.8.8"]}))
    response = _Response(status_code=302, headers={"location": "https://b.example/secret"})
    with pytest.raises(OutboundPolicyError, match="重定向"):
        policy.request("GET", "https://a.example/start", session=_Session([response]))


def test_oversized_response_is_rejected():
    from app.core.outbound_policy import OutboundPolicy, OutboundPolicyError

    policy = OutboundPolicy(
        resolver=_resolver({"public.example": ["8.8.8.8"]}),
        max_response_bytes=8,
    )
    with pytest.raises(OutboundPolicyError, match="过大"):
        policy.request(
            "GET",
            "https://public.example/api",
            session=_Session([_Response(content=b"0123456789")]),
        )


def test_kubeconfig_server_is_checked_by_same_policy():
    from app.api.clusters.cluster_mgmt import validate_kubeconfig_endpoints
    from app.core.outbound_policy import OutboundPolicy, OutboundPolicyError

    kubeconfig = """
apiVersion: v1
clusters:
  - name: prod
    cluster:
      server: https://10.0.0.8:6443
"""
    with pytest.raises(OutboundPolicyError):
        validate_kubeconfig_endpoints(kubeconfig, policy=OutboundPolicy())
    allowed = OutboundPolicy(allowed_cidrs={"10.0.0.0/8"})
    validate_kubeconfig_endpoints(kubeconfig, policy=allowed)
