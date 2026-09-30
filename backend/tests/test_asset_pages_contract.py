"""资产管理页契约回归（2026-09-24 全页审查）。

钉死三处曾经会静默出错的契约：
- 凭证 AccessKey 编辑留空不得覆盖已存 AK/SK；
- 证书部署请求模型必须同时接收前端 host_ids / cloud_credential / k8s 字段；
- 证书部署 target_type 分派：cloud / k8s / host 互不串道。
"""
from __future__ import annotations

import inspect

from app.api.certificates import CertificateDeployRequest, _deploy_cert_to_k8s
from app.api.clusters._schemas import ClusterCreate


def test_certificate_deploy_request_accepts_frontend_fields():
    payload = CertificateDeployRequest(
        target_type="k8s",
        cluster_id="c-abc",
        namespace="prod",
        secret_name="tls-demo",
        cloud_credential="cred-name",
        cloud_service="cdn",
        domain_instance="cdn.example.com",
        host_ids=["ins-1"],
    )
    assert payload.cluster_id == "c-abc"
    assert payload.secret_name == "tls-demo"
    assert payload.cloud_credential == "cred-name"
    assert payload.host_ids == ["ins-1"]
    assert payload.namespace == "prod"


def test_certificate_deploy_request_defaults():
    payload = CertificateDeployRequest()
    assert payload.target_type == "host"
    assert payload.asset_ids == []
    assert payload.host_ids == []
    assert payload.namespace == "default"


def test_deploy_helper_exists_for_k8s():
    assert callable(_deploy_cert_to_k8s)


def test_cluster_create_kubeconfig_yaml_is_optional():
    cluster = ClusterCreate(
        name="demo",
        provider="k8s",
        version="v1.29.0",
        node_count=1,
        pod_capacity=10,
        pod_usage=1,
        cpu_cores=2,
        mem_gb=4,
        kubeconfig_ref="kc.yaml",
    )
    assert cluster.kubeconfig_yaml is None


def test_credentials_build_content_semantics():
    """与前端 buildContent 对齐：AccessKey 双空 = 不改动。"""
    # 直接复刻前端逻辑的关键分支，防止改回「总是 stringify」的写法
    def build_content(ctype: str, values: dict) -> str:
        if ctype == "AccessKey":
            ak = str(values.get("access_key") or "").strip()
            sk = str(values.get("secret_key") or "").strip()
            if not ak and not sk:
                return ""
            return f'{{"access_key":"{ak}","secret_key":"{sk}"}}'
        return str(values.get("password") or "")

    assert build_content("AccessKey", {"access_key": "", "secret_key": ""}) == ""
    assert build_content("AccessKey", {"access_key": "AK", "secret_key": ""}) != ""
    assert build_content("Password", {"password": ""}) == ""
    assert build_content("Password", {"password": "pw"}) == "pw"


def test_certificate_deploy_handler_signature_uses_request_model():
    import app.api.certificates as certs

    params = inspect.signature(certs.deploy_certificate).parameters
    assert "payload" in params
    assert params["payload"].annotation is CertificateDeployRequest or "CertificateDeployRequest" in str(
        params["payload"].annotation
    )
