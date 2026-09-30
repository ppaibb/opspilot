"""
证书管理接口（ACME）
"""
from __future__ import annotations

from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import io
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import time
from typing import List
from urllib.parse import quote
import uuid
import zipfile

from acme import challenges, client as acme_client, errors as acme_errors, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
import josepy as jose
from pydantic import BaseModel
import requests
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.core.filesystem import ensure_private_directory, write_sensitive_text_file
from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import SessionLocal, get_db
from app.models.audit import AuditLog
from app.models.certificate import CertificateRecord
from app.models.credential import Credential
from app.models.user import User
from app.api.notifications import create_notification, NotificationCreate
from app.services.credential_access import ensure_credential_access, filter_credentials_for_user

logger = logging.getLogger(__name__)

router = APIRouter()

ISSUE_LOCK = threading.Lock()
ISSUING_CERT_IDS: set[str] = set()

BASE_DIR = Path(__file__).resolve().parents[2]
CERT_STORAGE_DIR = ensure_private_directory(BASE_DIR / "storage" / "certs")
ACME_ACCOUNT_DIR = ensure_private_directory(BASE_DIR / "storage" / "acme-accounts")
ACME_DIRECTORY_MAP = {
    "letsencrypt": "https://acme-v02.api.letsencrypt.org/directory",
    "letsencrypt_test": "https://acme-staging-v02.api.letsencrypt.org/directory",
}
DNS_PROPAGATION_SECONDS = 15
ACME_POLL_TIMEOUT_SECONDS = 360
ALIYUN_DNS_TXT_TTL = 600
CERT_LOG_MAX_LINES = 400
# 自动续签轮询周期（12 小时）。
CERT_AUTO_RENEW_INTERVAL_SECONDS = 12 * 3600
# 续签失败后的重试冷却：冷却期内不再触发，避免"到期证书续签一直失败→每轮重试+每次重启都触发"
# 造成的通知轰炸与 ACME 限流。成功续签的证书因 not_after 已刷新，天然不会重复触发。
CERT_RENEW_RETRY_COOLDOWN_HOURS = 24

ACME_PROVIDER_PLUGINS: dict[str, dict] = {
    "aliyun": {
        "env": ("Ali_Key", "Ali_Secret"),
        "label": "Alibaba Cloud DNS",
        "implemented": True,
    },
    "aws": {
        "env": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        "label": "AWS Route53",
        "implemented": False,
    },
}


class CertificateAccountItem(BaseModel):
    account_id: str
    account_name: str
    provider_key: str
    provider_label: str
    credential_type: str


class CertificateResponse(BaseModel):
    cert_id: str
    name: str
    primary_domain: str
    san_domains: List[str]
    provider: str
    provider_label: str
    account_id: str
    account_name: str
    acme_server: str
    challenge_type: str
    key_type: str
    status: str
    auto_renew: bool
    renew_before_days: int
    not_before: str
    not_after: str
    fingerprint_sha256: str
    cert_path: str
    key_path: str
    fullchain_path: str
    ca_path: str
    last_issue_at: str
    last_error: str
    deploy_targets: str
    remark: str
    created_at: str
    updated_at: str


class CertificateIssueRequest(BaseModel):
    name: str = ""
    primary_domain: str
    san_domains: List[str] = []
    account_id: str
    acme_server: str = "letsencrypt"
    key_type: str = "ec-256"
    auto_renew: bool = True
    renew_before_days: int = 30
    remark: str = ""
    deploy_targets: str = ""


class CertificateBatchDeleteRequest(BaseModel):
    ids: List[str]


class CertificateBatchRenewRequest(BaseModel):
    ids: List[str]


class CertificateUpdateRequest(BaseModel):
    name: str = ""
    san_domains: List[str] = []
    key_type: str = "ec-256"
    auto_renew: bool = True
    renew_before_days: int = 30
    remark: str = ""
    deploy_targets: str = ""


class CertificateAutoRenewRequest(BaseModel):
    enabled: bool


class CertificateRevokeRequest(BaseModel):
    reason: str = ""

class CertificateDeployRequest(BaseModel):
    """
    证书部署与分发请求模型（主机 SSH / 云服务商 API / K8s TLS Secret 三模式）。
    """

    target_type: str = "host"  # host | cloud | k8s
    asset_ids: List[str] = []  # 目标主机 ID 列表 (主机模式必填)
    host_ids: List[str] = []  # 前端旧别名，与 asset_ids 等价
    cert_dest_path: str = ""  # 证书在目标主机的保存路径 (如 /etc/nginx/ssl/cert.pem)
    cert_path: str = ""  # 证书存储路径别名 (前端通用契约)
    key_dest_path: str = ""  # 私钥在目标主机的保存路径 (如 /etc/nginx/ssl/privkey.pem)
    key_path: str = ""  # 私钥存储路径别名 (前端通用契约)
    post_script: str = ""  # 替换后执行的热重载脚本 (如 systemctl reload nginx)
    reload_cmd: str = ""  # 热重载脚本别名 (前端通用契约)
    cloud_provider: str = ""  # 云厂商类型: aliyun | tencent | aws | cloudflare
    cloud_account_id: str = ""  # 云厂商 API 密钥凭证 cred_id (云模式)
    cloud_credential: str = ""  # 云凭证名或 cred_id（前端契约，与 cloud_account_id 二选一）
    cloud_service: str = ""  # cdn | alb | waf
    domain_instance: str = ""  # 云资源实例域名 / ID
    # k8s 目标：推送 TLS Secret
    cluster_id: str = ""
    namespace: str = "default"
    secret_name: str = ""


class CertificateUploadRequest(BaseModel):
    name: str = ""
    cert_content: str
    key_content: str
    remark: str = ""



def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_date_str(raw_dt: datetime | None) -> str:
    if not raw_dt:
        return ""
    return raw_dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_domain(raw: str) -> str:
    text = (raw or "").strip().lower().rstrip(".")
    if not text:
        raise HTTPException(status_code=400, detail="域名不能为空")
    if " " in text or "." not in text:
        raise HTTPException(status_code=400, detail=f"域名格式非法: {raw}")
    try:
        is_wildcard = text.startswith("*.")
        if is_wildcard:
            base_domain = text[2:]
            text = "*." + base_domain.encode("idna").decode("ascii").lower()
        else:
            text = text.encode("idna").decode("ascii").lower()
    except Exception:
        raise HTTPException(status_code=400, detail=f"域名格式非法: {raw}")
    if not re.match(r"^(?:\*\.)?[a-z0-9.-]+$", text):
        raise HTTPException(status_code=400, detail=f"域名格式非法: {raw}")
    return text


def normalize_domains(primary_domain: str, san_domains: list[str]) -> tuple[str, list[str]]:
    primary = normalize_domain(primary_domain)
    result: list[str] = []
    for item in san_domains:
        value = normalize_domain(item)
        if value == primary:
            continue
        if value not in result:
            result.append(value)
    return primary, result


def infer_provider_key(text: str) -> str:
    raw = (text or "").strip().lower()
    if any(token in raw for token in ["aliyun", "阿里", "alibaba"]):
        return "aliyun"
    if any(token in raw for token in ["aws", "route53"]):
        return "aws"
    if any(token in raw for token in ["tencent", "腾讯", "dnspod"]):
        return "tencent"
    if "cloudflare" in raw:
        return "cloudflare"
    return "custom"


def is_domain_account_credential(row: Credential) -> bool:
    ctype = (row.type or "").strip().lower()
    if ctype in {"accesskey", "token", "api token", "ak/sk", "aksk"}:
        return True
    if "accesskey" in ctype or "token" in ctype:
        return True
    return False


def parse_access_key_content(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return "", ""

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            access_key = str(
                parsed.get("access_key")
                or parsed.get("accessKey")
                or parsed.get("AccessKeyId")
                or ""
            ).strip()
            secret_key = str(
                parsed.get("secret_key")
                or parsed.get("secretKey")
                or parsed.get("AccessKeySecret")
                or ""
            ).strip()
            if access_key and secret_key:
                return access_key, secret_key
    except Exception:
        pass

    kv: dict[str, str] = {}
    for line in text.splitlines():
        row = line.strip()
        if not row:
            continue
        if "=" in row:
            k, v = row.split("=", 1)
        elif ":" in row:
            k, v = row.split(":", 1)
        else:
            continue
        kv[k.strip().lower()] = v.strip().strip('"').strip("'")

    access_key = (
        kv.get("access_key")
        or kv.get("accesskey")
        or kv.get("accesskeyid")
        or ""
    ).strip()
    secret_key = (
        kv.get("secret_key")
        or kv.get("secretkey")
        or kv.get("accesskeysecret")
        or ""
    ).strip()
    return access_key, secret_key


def build_acme_accounts(db: Session, current_user: User | None = None) -> list[dict]:
    rows = db.query(Credential).order_by(Credential.id.desc()).all()
    result: list[dict] = []
    for row in filter_credentials_for_user(rows, current_user):
        if not is_domain_account_credential(row):
            continue
        provider = infer_provider_key(f"{row.name} {row.scope} {row.type}")
        plugin_meta = ACME_PROVIDER_PLUGINS.get(provider)
        if not plugin_meta or not bool(plugin_meta.get("implemented")):
            continue
        result.append(
            {
                "account_id": row.cred_id,
                "account_name": row.name,
                "provider_key": provider,
                "provider_label": plugin_meta["label"],
                "credential_type": row.type,
            }
        )
    return result


def parse_server_alias(server: str) -> str:
    value = (server or "").strip().lower()
    if value in {"letsencrypt", "letsencrypt_prod", "letsencrypt-production"}:
        return "letsencrypt"
    if value in {"letsencrypt-staging", "letsencrypt_test", "staging"}:
        return "letsencrypt_test"
    if not value:
        return "letsencrypt"
    return value


def resolve_acme_directory_url(server: str) -> str:
    alias = parse_server_alias(server)
    if alias.startswith("http://") or alias.startswith("https://"):
        return alias
    return ACME_DIRECTORY_MAP.get(alias, ACME_DIRECTORY_MAP["letsencrypt"])


def sanitize_file_segment(raw: str) -> str:
    text = (raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip("-")
    return text or "default"


def resolve_account_key_path(account_id: str, server_alias: str) -> Path:
    ACME_ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{sanitize_file_segment(account_id)}__{sanitize_file_segment(server_alias)}.pem"
    return ACME_ACCOUNT_DIR / filename


def load_or_create_account_jwk(account_id: str, server_alias: str) -> tuple[jose.JWKRSA, rsa.RSAPrivateKey]:
    key_path = resolve_account_key_path(account_id, server_alias)
    private_key: rsa.RSAPrivateKey
    if key_path.exists():
        # ACME 账户密钥需要持久化复用，否则每次会被 ACME 服务端视为新账户。
        loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise RuntimeError("ACME 账户私钥格式非法")
        private_key = loaded
    else:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_path.write_bytes(pem)
    return jose.JWKRSA(key=private_key), private_key


def generate_cert_private_key(key_type: str):
    value = (key_type or "").strip().lower()
    if value == "ec-256":
        return ec.generate_private_key(ec.SECP256R1())
    if value == "ec-384":
        return ec.generate_private_key(ec.SECP384R1())
    if value == "rsa-4096":
        return rsa.generate_private_key(public_exponent=65537, key_size=4096)
    if value == "rsa-2048":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raise RuntimeError("密钥算法仅支持 ec-256/ec-384/rsa-2048/rsa-4096")


def build_csr(private_key, domains: list[str]) -> bytes:
    if not domains:
        raise RuntimeError("签发域名为空")
    builder = x509.CertificateSigningRequestBuilder()
    builder = builder.subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])])
    )
    dns_names = [x509.DNSName(item) for item in domains]
    builder = builder.add_extension(
        x509.SubjectAlternativeName(dns_names),
        critical=False,
    )
    csr = builder.sign(private_key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM)


def split_pem_cert_chain(fullchain_pem: str) -> tuple[str, str, str]:
    pem_blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
        fullchain_pem or "",
        flags=re.S,
    )
    if not pem_blocks:
        raise RuntimeError("ACME 返回证书内容为空")
    normalized_blocks = [
        "-----BEGIN CERTIFICATE-----" + block + "-----END CERTIFICATE-----\n"
        for block in pem_blocks
    ]
    # 按 ACME 常见返回格式：首段为站点证书，其余为中间/根链。
    cert_pem = normalized_blocks[0]
    fullchain = "".join(normalized_blocks)
    ca_pem = "".join(normalized_blocks[1:])
    return cert_pem, fullchain, ca_pem


def write_certificate_files(paths: dict, key_pem: str, cert_pem: str, fullchain_pem: str, ca_pem: str) -> None:
    write_sensitive_text_file(paths["key_path"], key_pem)
    Path(paths["cert_path"]).write_text(cert_pem, encoding="utf-8")
    Path(paths["fullchain_path"]).write_text(fullchain_pem, encoding="utf-8")
    Path(paths["ca_path"]).write_text(ca_pem or cert_pem, encoding="utf-8")


def aliyun_percent_encode(raw: str) -> str:
    return quote(str(raw or ""), safe="~")


def sign_aliyun_rpc(params: dict[str, str], secret_key: str) -> str:
    sorted_params = sorted((k, str(v)) for k, v in params.items())
    canonicalized = "&".join(
        f"{aliyun_percent_encode(k)}={aliyun_percent_encode(v)}"
        for k, v in sorted_params
    )
    string_to_sign = f"GET&%2F&{aliyun_percent_encode(canonicalized)}"
    key = f"{secret_key}&".encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def aliyun_rpc_call(access_key: str, secret_key: str, action: str, extra_params: dict[str, str]) -> dict:
    params: dict[str, str] = {
        "Action": action,
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
    }
    params.update({k: str(v) for k, v in extra_params.items()})
    params["Signature"] = sign_aliyun_rpc(params, secret_key)
    try:
        resp = requests.get("https://alidns.aliyuncs.com/", params=params, timeout=20)
        payload = resp.json()
    except Exception as e:
        raise RuntimeError(f"阿里云 DNS API 调用失败（网络异常）: {str(e)}")
    if resp.status_code >= 400:
        msg = payload.get("Message") if isinstance(payload, dict) else ""
        raise RuntimeError(f"阿里云 DNS API 调用失败: HTTP {resp.status_code} {msg}".strip())
    if isinstance(payload, dict) and payload.get("Code"):
        raise RuntimeError(
            f"阿里云 DNS API 调用失败: {payload.get('Code')} {payload.get('Message') or ''}".strip()
        )
    if not isinstance(payload, dict):
        raise RuntimeError("阿里云 DNS API 返回非法响应")
    return payload


def fetch_aliyun_domains(access_key: str, secret_key: str) -> list[str]:
    page_number = 1
    page_size = 100
    domains: list[str] = []
    while True:
        payload = aliyun_rpc_call(
            access_key,
            secret_key,
            "DescribeDomains",
            {
                "PageNumber": str(page_number),
                "PageSize": str(page_size),
            },
        )
        domains_node = payload.get("Domains", {}) if isinstance(payload, dict) else {}
        records = domains_node.get("Domain", []) if isinstance(domains_node, dict) else []
        for item in records:
            if not isinstance(item, dict):
                continue
            name = str(item.get("DomainName") or "").strip().lower()
            if name:
                domains.append(name)
        total_count = int(domains_node.get("TotalCount") or payload.get("TotalCount") or 0)
        if len(domains) >= total_count or len(records) < page_size:
            break
        page_number += 1
        if page_number > 50:
            break
    return sorted(list(dict.fromkeys(domains)))


def resolve_best_zone(fqdn: str, zones: list[str]) -> str:
    domain = (fqdn or "").strip().lower().rstrip(".")
    zone = ""
    for item in zones:
        candidate = (item or "").strip().lower().rstrip(".")
        if not candidate:
            continue
        if domain == candidate or domain.endswith(f".{candidate}"):
            if len(candidate) > len(zone):
                zone = candidate
    if not zone:
        raise RuntimeError(f"无法为域名 {fqdn} 匹配到阿里云托管 Zone")
    return zone


def resolve_rr_from_fqdn(fqdn: str, zone: str) -> str:
    domain = (fqdn or "").strip().lower().rstrip(".")
    zone_name = (zone or "").strip().lower().rstrip(".")
    if domain == zone_name:
        return "@"
    suffix = f".{zone_name}"
    if not domain.endswith(suffix):
        raise RuntimeError(f"域名 {fqdn} 不属于 Zone {zone}")
    rr = domain[: -len(suffix)].strip(".")
    return rr or "@"


def create_aliyun_txt_record(
    access_key: str,
    secret_key: str,
    fqdn: str,
    txt_value: str,
    zones: list[str] | None = None,
) -> str:
    if zones is None:
        zones = fetch_aliyun_domains(access_key, secret_key)
    zone = resolve_best_zone(fqdn, zones)
    rr = resolve_rr_from_fqdn(fqdn, zone)
    payload = aliyun_rpc_call(
        access_key,
        secret_key,
        "AddDomainRecord",
        {
            "DomainName": zone,
            "RR": rr,
            "Type": "TXT",
            "Value": txt_value,
            # 阿里云不少场景限制最小 TTL=600，避免 60 导致 API 400。
            "TTL": str(ALIYUN_DNS_TXT_TTL),
        },
    )
    record_id = str(payload.get("RecordId") or "").strip()
    if not record_id:
        raise RuntimeError("阿里云 DNS API 未返回 RecordId")
    return record_id


def delete_aliyun_record(access_key: str, secret_key: str, record_id: str) -> None:
    if not record_id:
        return
    aliyun_rpc_call(
        access_key,
        secret_key,
        "DeleteDomainRecord",
        {"RecordId": record_id},
    )


def retry_delete_aliyun_record(access_key: str, secret_key: str, record_id: str) -> None:
    for i in range(3):
        time.sleep(15 * (i + 1))
        try:
            delete_aliyun_record(access_key, secret_key, record_id)
            logger.info(f"异步清理 TXT 记录成功: {record_id}")
            return
        except Exception as e:
            logger.warning(f"第 {i + 1} 次清理 TXT 记录失败 {record_id}: {e}")


def wait_dns_propagation() -> None:
    time.sleep(max(5, DNS_PROPAGATION_SECONDS))


def cert_storage_paths(cert_id: str) -> dict:
    target_dir = CERT_STORAGE_DIR / cert_id
    target_dir.mkdir(parents=True, exist_ok=True)
    return {
        "dir": str(target_dir),
        "cert_path": str(target_dir / "cert.pem"),
        "key_path": str(target_dir / "privkey.pem"),
        "fullchain_path": str(target_dir / "fullchain.pem"),
        "ca_path": str(target_dir / "ca.pem"),
    }


def cert_log_path(cert_id: str) -> Path:
    # 证书日志与证书文件放在同一路径，便于单证书问题排查。
    target_dir = CERT_STORAGE_DIR / cert_id
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / "issue.log"


def append_cert_log(cert_id: str, message: str, level: str = "INFO") -> None:
    text = (message or "").strip()
    if not cert_id or not text:
        return
    row = f"[{now_ts()}] [{(level or 'INFO').upper()}] {text}\n"
    path = cert_log_path(cert_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(row)


def read_cert_logs(cert_id: str, limit_lines: int = CERT_LOG_MAX_LINES) -> list[str]:
    path = cert_log_path(cert_id)
    if not path.exists():
        return []
    rows = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if limit_lines > 0 and len(rows) > limit_lines:
        return rows[-limit_lines:]
    return rows


def parse_certificate_meta(cert_path: str) -> dict:
    path = Path(cert_path)
    if not path.exists():
        raise RuntimeError("证书文件不存在")
    raw = path.read_text(encoding="utf-8", errors="ignore")
    pem_blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
        raw,
        flags=re.S,
    )
    if not pem_blocks:
        raise RuntimeError("证书内容非法")
    first_pem = "-----BEGIN CERTIFICATE-----" + pem_blocks[0] + "-----END CERTIFICATE-----"
    cert = x509.load_pem_x509_certificate(first_pem.encode("utf-8"))
    fp = cert.fingerprint(hashes.SHA256()).hex()
    return {
        "not_before": to_date_str(cert.not_valid_before),
        "not_after": to_date_str(cert.not_valid_after),
        "fingerprint_sha256": fp,
    }


def parse_cert_content_meta(pem_content: str) -> tuple[str, list[str], dict]:
    pem_blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
        pem_content or "",
        flags=re.S,
    )
    if not pem_blocks:
        raise HTTPException(status_code=400, detail="证书内容非法，请提供正确的 PEM 格式证书")
    first_pem = "-----BEGIN CERTIFICATE-----" + pem_blocks[0] + "-----END CERTIFICATE-----"
    try:
        cert = x509.load_pem_x509_certificate(first_pem.encode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析证书失败，请确保格式正确: {e}")

    try:
        common_name_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        primary_domain = common_name_attrs[0].value if common_name_attrs else ""
    except Exception:
        primary_domain = ""

    san_domains = []
    try:
        ext = cert.extensions.get_extension_for_oid(x509.OID_SUBJECT_ALTERNATIVE_NAME)
        san_domains = ext.value.get_values_for_type(x509.DNSName)
    except Exception:
        pass

    san_domains = [d for d in san_domains if d != primary_domain]
    fp = cert.fingerprint(hashes.SHA256()).hex()

    return primary_domain, san_domains, {
        "not_before": to_date_str(cert.not_valid_before),
        "not_after": to_date_str(cert.not_valid_after),
        "fingerprint_sha256": fp,
    }


def validate_private_key_match(cert_pem: str, key_pem: str) -> None:
    try:
        private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"私钥文件非法或存在密码保护: {e}")

    pem_blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
        cert_pem or "",
        flags=re.S,
    )
    if not pem_blocks:
        raise HTTPException(status_code=400, detail="证书内容非法，请提供正确的 PEM 格式证书")
    first_pem = "-----BEGIN CERTIFICATE-----" + pem_blocks[0] + "-----END CERTIFICATE-----"
    try:
        cert = x509.load_pem_x509_certificate(first_pem.encode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析证书失败，请确保格式正确: {e}")

    try:
        cert_pub = cert.public_key()
        priv_pub = private_key.public_key()
        
        cert_pub_bytes = cert_pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        priv_pub_bytes = priv_pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if cert_pub_bytes != priv_pub_bytes:
            raise HTTPException(status_code=400, detail="证书链与私钥不匹配")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail=f"校验密钥对一致性失败: {e}")



def resolve_cert_dir(row: CertificateRecord) -> Path:
    base_dir = CERT_STORAGE_DIR.resolve()
    candidates = [
        row.fullchain_path or "",
        row.cert_path or "",
        row.key_path or "",
        row.ca_path or "",
    ]
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        else:
            path = path.resolve()
        parent = path.parent
        try:
            parent.relative_to(base_dir)
            return parent
        except Exception:
            continue
    return (CERT_STORAGE_DIR / (row.cert_id or "")).resolve()


def safe_remove_cert_dir(target_dir: Path) -> None:
    base_dir = CERT_STORAGE_DIR.resolve()
    target = target_dir.resolve()
    try:
        target.relative_to(base_dir)
    except Exception:
        return
    if target == base_dir:
        return
    if target.exists() and target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def calc_certificate_status(not_after: str, renew_before_days: int, fallback: str = "issued") -> str:
    text = (not_after or "").strip()
    if not text:
        return fallback
    try:
        expire_at = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return fallback
    days_left = (expire_at - datetime.now()).days
    if days_left < 0:
        return "expired"
    if days_left <= max(1, int(renew_before_days or 30)):
        return "expiring"
    return "issued"


def reserve_issue(cert_id: str) -> bool:
    with ISSUE_LOCK:
        if cert_id in ISSUING_CERT_IDS:
            return False
        ISSUING_CERT_IDS.add(cert_id)
        return True


def release_issue(cert_id: str) -> None:
    with ISSUE_LOCK:
        ISSUING_CERT_IDS.discard(cert_id)


def set_issue_failed(db: Session, cert: CertificateRecord, message: str) -> None:
    cert.status = "failed"
    cert.last_error = (message or "未知错误")[:1000]
    cert.updated_at = now_ts()
    # 记录证书任务失败审计日志
    db.add(
        AuditLog(
            user="系统自动任务",
            action=f"系统自动续签证书失败 {cert.primary_domain}，原因: {cert.last_error[:120]}",
            resource=f"/api/certificates/{cert.cert_id}/renew",
            ip="-",
            result="failed",
            risk_level="low",
        )
    )
    db.commit()
    append_cert_log(cert.cert_id or "", cert.last_error or "签发失败", "ERROR")


def serialize_certificate(row: CertificateRecord) -> dict:
    san = [item.strip() for item in (row.san_domains or "").split(",") if item.strip()]
    provider_meta = ACME_PROVIDER_PLUGINS.get((row.provider or "").strip().lower())
    return {
        "cert_id": row.cert_id,
        "name": row.name or row.primary_domain,
        "primary_domain": row.primary_domain or "",
        "san_domains": san,
        "provider": row.provider or "custom",
        "provider_label": provider_meta["label"] if provider_meta else (row.provider or "Custom"),
        "account_id": row.account_id or "",
        "account_name": row.account_name or "",
        "acme_server": row.acme_server or "letsencrypt",
        "challenge_type": row.challenge_type or "dns-01",
        "key_type": row.key_type or "ec-256",
        "status": row.status or "pending",
        "auto_renew": (row.auto_renew or "yes").lower() != "no",
        "renew_before_days": int(row.renew_before_days or 30),
        "not_before": row.not_before or "",
        "not_after": row.not_after or "",
        "fingerprint_sha256": row.fingerprint_sha256 or "",
        "cert_path": row.cert_path or "",
        "key_path": row.key_path or "",
        "fullchain_path": row.fullchain_path or "",
        "ca_path": row.ca_path or "",
        "last_issue_at": row.last_issue_at or "",
        "last_error": row.last_error or "",
        "deploy_targets": row.deploy_targets or "",
        "remark": row.remark or "",
        "created_at": row.created_at or "",
        "updated_at": row.updated_at or "",
    }


def extract_dns01_challenge(authzr: messages.AuthorizationResource):
    for challb in authzr.body.challenges:
        if isinstance(challb.chall, challenges.DNS01):
            return challb
    raise RuntimeError(
        f"域名 {authzr.body.identifier.value} 未返回 DNS-01 挑战，当前仅支持 DNS-01"
    )


def run_issue_job(cert_id: str, renew: bool = False, reserved: bool = False, user_id: int | None = None):
    if not reserved and not reserve_issue(cert_id):
        return

    db = SessionLocal()
    try:
        cert = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
        if not cert:
            return

        cert.status = "renewing" if renew else "issuing"
        cert.last_error = ""
        cert.updated_at = now_ts()
        db.commit()
        append_cert_log(cert_id, "开始执行证书任务" + ("（续签）" if renew else "（签发）"))

        cred = db.query(Credential).filter(Credential.cred_id == cert.account_id).first()
        if not cred:
            set_issue_failed(db, cert, "签发账户不存在")
            return

        provider = infer_provider_key(f"{cred.name} {cred.scope} {cred.type}")
        plugin_meta = ACME_PROVIDER_PLUGINS.get(provider)
        if not plugin_meta or not bool(plugin_meta.get("implemented")):
            set_issue_failed(db, cert, f"当前不支持该 DNS 厂商自动签发: {provider}")
            return

        content = decrypt_secret(cred.content or "")
        access_key, secret_key = parse_access_key_content(content)
        if not access_key or not secret_key:
            set_issue_failed(db, cert, "凭证内容无法解析 AccessKey/SecretKey")
            return
        if provider != "aliyun":
            set_issue_failed(db, cert, f"当前 ACME 自动签发仅支持阿里云 DNS，当前账户厂商为: {provider}")
            return

        CERT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        paths = cert_storage_paths(cert.cert_id)
        cert.cert_path = paths["cert_path"]
        cert.key_path = paths["key_path"]
        cert.fullchain_path = paths["fullchain_path"]
        cert.ca_path = paths["ca_path"]
        cert.provider = provider
        cert.account_name = cred.name
        cert.updated_at = now_ts()
        db.commit()
        append_cert_log(cert_id, f"签发账户就绪：{cred.name} / {provider}")

        domains = [cert.primary_domain] + [item.strip() for item in (cert.san_domains or "").split(",") if item.strip()]
        domains = list(dict.fromkeys([item for item in domains if item]))
        if not domains:
            set_issue_failed(db, cert, "签发域名为空")
            return
        server_alias = parse_server_alias(cert.acme_server)

        account_jwk, _ = load_or_create_account_jwk(cert.account_id or cred.cred_id, server_alias)
        cert_private_key = generate_cert_private_key(cert.key_type or "ec-256")
        csr_pem = build_csr(cert_private_key, domains)
        append_cert_log(cert_id, f"已生成 CSR，域名数量：{len(domains)}")

        directory_url = resolve_acme_directory_url(server_alias)
        net = acme_client.ClientNetwork(
            account_jwk,
            user_agent="OpsPilot/1.0",
        )
        try:
            directory_response = net.get(directory_url)
            directory_payload = directory_response.json()
            acme_directory = messages.Directory.from_json(directory_payload)
            acme_v2 = acme_client.ClientV2(acme_directory, net)

            try:
                acme_v2.new_account(
                    messages.NewRegistration.from_data(terms_of_service_agreed=True)
                )
            except acme_errors.ConflictError as conflict:
                existing = messages.RegistrationResource(
                    uri=conflict.location,
                    body=messages.Registration(),
                )
                acme_v2.query_registration(existing)
            except messages.Error as e:
                # 同一个账户私钥重复注册时，服务端通常返回 409。
                code = (getattr(e, "code", "") or "").strip()
                if code not in {"accountAlreadyExists"}:
                    raise

            order = acme_v2.new_order(csr_pem)
            append_cert_log(cert_id, "ACME 订单创建成功，开始处理 DNS-01 挑战")
            challenge_records: list[str] = []
            try:
                # 同一证书签发任务只拉取一次托管域名列表，避免多 SAN 时重复请求。
                aliyun_zones = fetch_aliyun_domains(access_key, secret_key)
                if not aliyun_zones:
                    raise RuntimeError("阿里云账户下未查询到可用托管域名，无法完成 DNS-01 验证")
                append_cert_log(cert_id, f"已拉取托管域名列表，共 {len(aliyun_zones)} 个")
                for authzr in order.authorizations:
                    dns01 = extract_dns01_challenge(authzr)
                    domain_value = str(authzr.body.identifier.value or "").strip()
                    if not domain_value:
                        raise RuntimeError("ACME 返回的授权域名为空")
                    validation_name = dns01.chall.validation_domain_name(domain_value)
                    validation_value = dns01.chall.validation(account_jwk)
                    record_id = create_aliyun_txt_record(
                        access_key,
                        secret_key,
                        validation_name,
                        validation_value,
                        zones=aliyun_zones,
                    )
                    challenge_records.append(record_id)
                    append_cert_log(cert_id, f"已创建 TXT 验证记录：{validation_name}（RecordId={record_id}）")

                # 等待 TXT 记录在公共 DNS 侧生效，避免立即校验导致 pending/unauthorized。
                wait_dns_propagation()
                append_cert_log(cert_id, "TXT 记录传播等待结束，开始提交 challenge 应答")

                for authzr in order.authorizations:
                    dns01 = extract_dns01_challenge(authzr)
                    response = dns01.chall.response(account_jwk)
                    acme_v2.answer_challenge(dns01, response)

                deadline = datetime.now() + timedelta(seconds=ACME_POLL_TIMEOUT_SECONDS)
                finalized_order = acme_v2.poll_and_finalize(order, deadline=deadline)
                append_cert_log(cert_id, "ACME 订单已完成签发")
            finally:
                for record_id in challenge_records:
                    try:
                        delete_aliyun_record(access_key, secret_key, record_id)
                        append_cert_log(cert_id, f"已清理 TXT 验证记录：{record_id}")
                    except Exception as e:
                        logger.warning(f"TXT 记录清理失败 {record_id}: {e}，已进入异步重试")
                        append_cert_log(cert_id, f"TXT 记录清理失败：{record_id}，将异步重试", "WARNING")
                        threading.Thread(target=retry_delete_aliyun_record, args=(access_key, secret_key, record_id), daemon=True).start()

            fullchain_pem = (finalized_order.fullchain_pem or "").strip()
            if not fullchain_pem:
                raise RuntimeError("ACME 已完成签发，但未返回证书链内容")
        finally:
            try:
                net.session.close()
            except Exception:
                pass

        key_pem = cert_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        cert_pem, fullchain_text, ca_pem = split_pem_cert_chain(fullchain_pem)
        write_certificate_files(paths, key_pem, cert_pem, fullchain_text, ca_pem)

        meta = parse_certificate_meta(paths["cert_path"])
        cert.not_before = meta["not_before"]
        cert.not_after = meta["not_after"]
        cert.fingerprint_sha256 = meta["fingerprint_sha256"]
        cert.last_issue_at = now_ts()
        cert.updated_at = now_ts()
        cert.last_error = ""
        cert.status = "issued"
        # 记录证书签发成功审计日志
        db.add(
            AuditLog(
                user="系统自动任务",
                action=f"系统自动续签了证书 {cert.primary_domain}",
                resource=f"/api/certificates/{cert.cert_id}/renew",
                ip="-",
                result="success",
                risk_level="low",
            )
        )
        db.commit()
        append_cert_log(cert_id, f"证书签发成功，有效期至：{cert.not_after}")
        # 通知用户：签发/续期成功（自动续期时通知全部管理员）
        if user_id:
            try:
                create_notification(
                    db,
                    data=NotificationCreate(
                        user_id=user_id,
                        type="success",
                        category="certificate",
                        title="证书续期完成" if renew else "证书签发完成",
                        content=f"{cert.primary_domain} 证书已{'续期' if renew else '签发'}，有效期至 {cert.not_after}",
                        link="/assets/certificates",
                        resource_id=cert_id,
                    ),
                )
            except Exception:
                logger.debug("Failed to send success notification for cert %s", cert_id)
        else:
            for admin in db.query(User).filter(User.role == "admin").all():
                try:
                    create_notification(
                        db,
                        data=NotificationCreate(
                            user_id=admin.id,
                            type="success",
                            category="certificate",
                            title="证书自动续期完成",
                            content=f"{cert.primary_domain} 证书已自动续期，有效期至 {cert.not_after}",
                            link="/assets/certificates",
                            resource_id=cert_id,
                        ),
                    )
                except Exception:
                    pass
    except Exception as e:
        cert = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
        if cert:
            set_issue_failed(db, cert, f"签发任务异常: {str(e)}")
            # 通知用户：签发/续期失败（自动续期时通知全部管理员）
            if user_id:
                try:
                    create_notification(
                        db,
                        data=NotificationCreate(
                            user_id=user_id,
                            type="error",
                            category="certificate",
                            title="证书续期失败" if renew else "证书签发失败",
                            content=f"{cert.primary_domain} {'续期' if renew else '签发'}失败：{str(e)[:200]}",
                            link="/assets/certificates",
                            resource_id=cert_id,
                        ),
                    )
                except Exception:
                    logger.debug("Failed to send error notification for cert %s", cert_id)
            else:
                for admin in db.query(User).filter(User.role == "admin").all():
                    try:
                        create_notification(
                            db,
                            data=NotificationCreate(
                                user_id=admin.id,
                                type="error",
                                category="certificate",
                                title="证书自动续期失败",
                                content=f"{cert.primary_domain} 自动续期失败：{str(e)[:200]}",
                                link="/assets/certificates",
                                resource_id=cert_id,
                            ),
                        )
                    except Exception:
                        pass
    finally:
        db.close()
        release_issue(cert_id)


@router.get("/accounts", response_model=List[CertificateAccountItem])
def get_acme_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return build_acme_accounts(db, current_user)


@router.get("", response_model=List[CertificateResponse])
def get_certificates(q: str = "", db: Session = Depends(get_db)):
    query = db.query(CertificateRecord)
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                CertificateRecord.cert_id.ilike(f"%{keyword}%"),
                CertificateRecord.name.ilike(f"%{keyword}%"),
                CertificateRecord.primary_domain.ilike(f"%{keyword}%"),
                CertificateRecord.san_domains.ilike(f"%{keyword}%"),
                CertificateRecord.account_name.ilike(f"%{keyword}%"),
                CertificateRecord.status.ilike(f"%{keyword}%"),
            )
        )
    rows = query.order_by(CertificateRecord.id.desc()).all()
    changed = False
    for row in rows:
        next_status = calc_certificate_status(row.not_after or "", int(row.renew_before_days or 30), row.status or "pending")
        if row.status not in {"issuing", "renewing", "failed", "pending", "revoked"} and next_status != row.status:
            row.status = next_status
            row.updated_at = now_ts()
            changed = True
    if changed:
        db.commit()
        rows = query.order_by(CertificateRecord.id.desc()).all()
    return [serialize_certificate(row) for row in rows]


@router.get("/{cert_id}", response_model=CertificateResponse)
def get_certificate(cert_id: str, db: Session = Depends(get_db)):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    return serialize_certificate(row)


@router.post("/issue", response_model=CertificateResponse)
def issue_certificate(
    payload: CertificateIssueRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    primary_domain, san_domains = normalize_domains(payload.primary_domain, payload.san_domains)
    if payload.renew_before_days < 1 or payload.renew_before_days > 180:
        raise HTTPException(status_code=400, detail="续签提前天数需在 1-180 之间")
    key_type = (payload.key_type or "").strip() or "ec-256"
    if key_type not in {"ec-256", "ec-384", "rsa-2048", "rsa-4096"}:
        raise HTTPException(status_code=400, detail="密钥算法仅支持 ec-256/ec-384/rsa-2048/rsa-4096")

    account_id = (payload.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="请选择签发账户")

    cred = db.query(Credential).filter(Credential.cred_id == account_id).first()
    cred = ensure_credential_access(
        cred,
        current_user,
        not_found_message="签发账户不存在",
        denied_message="当前账号无权使用该签发账户",
    )

    provider = infer_provider_key(f"{cred.name} {cred.scope} {cred.type}")
    plugin_meta = ACME_PROVIDER_PLUGINS.get(provider)
    if not plugin_meta or not bool(plugin_meta.get("implemented")):
        raise HTTPException(status_code=400, detail=f"当前暂不支持该厂商 ACME 自动签发: {provider}")

    exists = db.query(CertificateRecord).filter(CertificateRecord.primary_domain == primary_domain).first()
    if exists:
        raise HTTPException(status_code=400, detail="该主域名证书已存在")

    cert_id = f"CRT-{str(uuid.uuid4())[:8].upper()}"
    paths = cert_storage_paths(cert_id)
    created_at = now_ts()
    row = CertificateRecord(
        cert_id=cert_id,
        name=(payload.name or "").strip()[:120] or primary_domain,
        primary_domain=primary_domain,
        san_domains=",".join(san_domains),
        provider=provider,
        account_id=account_id,
        account_name=cred.name,
        acme_server=parse_server_alias(payload.acme_server),
        challenge_type="dns-01",
        key_type=key_type,
        status="pending",
        auto_renew="yes" if payload.auto_renew else "no",
        renew_before_days=int(payload.renew_before_days or 30),
        cert_path=paths["cert_path"],
        key_path=paths["key_path"],
        fullchain_path=paths["fullchain_path"],
        ca_path=paths["ca_path"],
        deploy_targets=(payload.deploy_targets or "").strip()[:255],
        remark=(payload.remark or "").strip()[:255],
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    queued = reserve_issue(row.cert_id)
    if queued:
        background_tasks.add_task(run_issue_job, row.cert_id, False, True, current_user.id)
        row.status = "issuing"
        row.updated_at = now_ts()
        db.commit()
        db.refresh(row)
        append_cert_log(row.cert_id, "已提交签发任务，等待后台执行")

    return serialize_certificate(row)


@router.post("/upload", response_model=CertificateResponse)
def upload_certificate(
    payload: CertificateUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    primary_domain, san_domains, meta = parse_cert_content_meta(payload.cert_content)
    validate_private_key_match(payload.cert_content, payload.key_content)

    exists = db.query(CertificateRecord).filter(CertificateRecord.primary_domain == primary_domain).first()
    if exists:
        raise HTTPException(status_code=400, detail="该主域名证书已存在")

    cert_id = f"CRT-{str(uuid.uuid4())[:8].upper()}"
    paths = cert_storage_paths(cert_id)
    created_at = now_ts()

    cert_pem, fullchain_text, ca_pem = split_pem_cert_chain(payload.cert_content)
    write_certificate_files(paths, payload.key_content, cert_pem, fullchain_text, ca_pem)

    row = CertificateRecord(
        cert_id=cert_id,
        name=(payload.name or "").strip()[:120] or primary_domain,
        primary_domain=primary_domain,
        san_domains=",".join(san_domains),
        provider="custom",
        account_id="",
        account_name="",
        acme_server="custom",
        challenge_type="manual",
        key_type="custom",
        status="issued",
        auto_renew="no",
        renew_before_days=30,
        cert_path=paths["cert_path"],
        key_path=paths["key_path"],
        fullchain_path=paths["fullchain_path"],
        ca_path=paths["ca_path"],
        not_before=meta["not_before"],
        not_after=meta["not_after"],
        fingerprint_sha256=meta["fingerprint_sha256"],
        deploy_targets="",
        remark=(payload.remark or "").strip()[:255],
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    
    append_cert_log(row.cert_id, "用户手动上传导入了自定义证书和私钥。")
    return serialize_certificate(row)



@router.put("/{cert_id}", response_model=CertificateResponse)
def update_certificate(
    cert_id: str,
    payload: CertificateUpdateRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    if cert_id in ISSUING_CERT_IDS:
        raise HTTPException(status_code=400, detail="签发任务执行中，暂不可修改")

    if payload.renew_before_days < 1 or payload.renew_before_days > 180:
        raise HTTPException(status_code=400, detail="续签提前天数需在 1-180 之间")
    key_type = (payload.key_type or "").strip() or row.key_type or "ec-256"
    if key_type not in {"ec-256", "ec-384", "rsa-2048", "rsa-4096"}:
        raise HTTPException(status_code=400, detail="密钥算法仅支持 ec-256/ec-384/rsa-2048/rsa-4096")

    _, san_domains = normalize_domains(row.primary_domain or "", payload.san_domains or [])
    row.name = (payload.name or "").strip()[:120] or row.primary_domain
    row.san_domains = ",".join(san_domains)
    row.key_type = key_type
    row.auto_renew = "yes" if payload.auto_renew else "no"
    row.renew_before_days = int(payload.renew_before_days or 30)
    row.remark = (payload.remark or "").strip()[:255]
    row.deploy_targets = (payload.deploy_targets or "").strip()[:255]
    row.updated_at = now_ts()
    db.commit()
    db.refresh(row)
    return serialize_certificate(row)


@router.post("/{cert_id}/auto-renew", response_model=CertificateResponse)
def set_certificate_auto_renew(
    cert_id: str,
    payload: CertificateAutoRenewRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    row.auto_renew = "yes" if payload.enabled else "no"
    row.updated_at = now_ts()
    db.commit()
    db.refresh(row)
    return serialize_certificate(row)


@router.post("/{cert_id}/renew")
def renew_certificate(
    cert_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    account_id = str(row.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="证书未绑定签发账户，无法续签")
    cred = db.query(Credential).filter(Credential.cred_id == account_id).first()
    ensure_credential_access(
        cred,
        current_user,
        not_found_message="签发账户不存在",
        denied_message="当前账号无权使用该签发账户",
    )
    queued = reserve_issue(row.cert_id)
    if not queued:
        return {"success": False, "message": "签发任务执行中，请稍候"}
    background_tasks.add_task(run_issue_job, row.cert_id, True, True, current_user.id)
    row.status = "renewing"
    row.updated_at = now_ts()
    db.commit()
    append_cert_log(row.cert_id, "已提交续签任务，等待后台执行")
    # 即时通知：任务已提交
    create_notification(
        db,
        data=NotificationCreate(
            user_id=current_user.id,
            type="info",
            category="certificate",
            title="证书续期任务已提交",
            content=f"{row.primary_domain} 续期任务已提交，正在后台执行",
            link="/assets/certificates",
            resource_id=row.cert_id,
        ),
    )
    return {"success": True, "message": "已提交续签任务"}


@router.post("/batch-renew")
def batch_renew_certificates(
    payload: CertificateBatchRenewRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    ids = list(dict.fromkeys([item.strip() for item in payload.ids if item and item.strip()]))
    if not ids:
        return {"success_count": 0, "failed": []}

    failed: List[dict] = []
    success_count = 0
    for cert_id in ids:
        row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
        if not row:
            failed.append({"id": cert_id, "reason": "证书不存在"})
            continue
        account_id = str(row.account_id or "").strip()
        if not account_id:
            failed.append({"id": cert_id, "reason": "证书未绑定签发账户"})
            continue
        cred = db.query(Credential).filter(Credential.cred_id == account_id).first()
        try:
            ensure_credential_access(
                cred,
                current_user,
                not_found_message="签发账户不存在",
                denied_message="当前账号无权使用该签发账户",
            )
        except HTTPException as exc:
            failed.append({"id": cert_id, "reason": str(exc.detail)})
            continue
        queued = reserve_issue(row.cert_id)
        if not queued:
            failed.append({"id": cert_id, "reason": "签发任务执行中"})
            continue
        background_tasks.add_task(run_issue_job, row.cert_id, True, True, current_user.id)
        row.status = "renewing"
        row.updated_at = now_ts()
        append_cert_log(row.cert_id, "已由批量操作提交续签任务")
        create_notification(
            db,
            data=NotificationCreate(
                user_id=current_user.id,
                type="info",
                category="certificate",
                title="证书续期任务已提交",
                content=f"{row.primary_domain} 续期任务已提交（批量），正在后台执行",
                link="/assets/certificates",
                resource_id=row.cert_id,
            ),
        )
        success_count += 1
    db.commit()
    return {"success_count": success_count, "failed": failed}


@router.post("/{cert_id}/refresh")
def refresh_certificate_status(
    cert_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")

    try:
        meta = parse_certificate_meta((row.cert_path or "").strip() or (row.fullchain_path or "").strip())
        row.not_before = meta["not_before"]
        row.not_after = meta["not_after"]
        row.fingerprint_sha256 = meta["fingerprint_sha256"]
        row.status = calc_certificate_status(row.not_after, int(row.renew_before_days or 30), "issued")
        row.last_error = ""
    except Exception as e:
        row.status = "failed"
        row.last_error = f"刷新证书状态失败: {str(e)}"[:1000]
    row.updated_at = now_ts()
    db.commit()
    db.refresh(row)
    append_cert_log(row.cert_id, "已执行证书状态刷新")
    return serialize_certificate(row)


@router.get("/{cert_id}/download")
def download_certificate_files(
    cert_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.download")),
    _risk=Depends(require_risk_ack("action.assets.certificates.download", "download")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")

    file_candidates = [
        ("cert.pem", row.cert_path or ""),
        ("privkey.pem", row.key_path or ""),
        ("fullchain.pem", row.fullchain_path or ""),
        ("ca.pem", row.ca_path or ""),
    ]

    files: list[tuple[str, Path]] = []
    for alias, raw_path in file_candidates:
        text = str(raw_path or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        else:
            path = path.resolve()
        if path.exists() and path.is_file():
            files.append((alias, path))

    if not files:
        raise HTTPException(status_code=400, detail="证书文件不存在，无法下载")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for alias, path in files:
            zf.write(path, arcname=alias)
        meta = [
            f"cert_id: {row.cert_id}",
            f"name: {row.name or ''}",
            f"primary_domain: {row.primary_domain or ''}",
            f"san_domains: {row.san_domains or ''}",
            f"provider: {row.provider or ''}",
            f"account_name: {row.account_name or ''}",
            f"not_before: {row.not_before or ''}",
            f"not_after: {row.not_after or ''}",
            f"last_issue_at: {row.last_issue_at or ''}",
            f"updated_at: {row.updated_at or ''}",
        ]
        zf.writestr("metadata.txt", "\n".join(meta))
    zip_buffer.seek(0)

    filename_seed = (row.primary_domain or row.cert_id or "certificate").replace("*", "wildcard").replace("/", "-")
    filename = f"{filename_seed}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


def _deploy_cert_to_k8s(
    row: CertificateRecord,
    cluster_id: str,
    namespace: str,
    secret_name: str,
) -> dict:
    """把证书推成集群内的 kubernetes.io/tls Secret（存在则覆盖）。"""
    from kubernetes import client as k8s_client
    from app.api.clusters._shared import _get_api_client

    try:
        cert_content = Path(row.fullchain_path or row.cert_path).read_text(encoding="utf-8")
        key_content = Path(row.key_path).read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取证书文件失败: {exc}")

    try:
        api_client = _get_api_client(cluster_id)
        v1 = k8s_client.CoreV1Api(api_client)
        body = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=secret_name, namespace=namespace),
            type="kubernetes.io/tls",
            string_data={"tls.crt": cert_content, "tls.key": key_content},
        )
        try:
            v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            v1.replace_namespaced_secret(name=secret_name, namespace=namespace, body=body)
            action = "updated"
        except k8s_client.ApiException as exc:
            if exc.status == 404:
                v1.create_namespaced_secret(namespace=namespace, body=body)
                action = "created"
            else:
                raise
    except HTTPException:
        raise
    except Exception as exc:
        append_cert_log(row.cert_id, f"K8s TLS Secret 推送失败 [{cluster_id}/{namespace}/{secret_name}]: {exc}", "ERROR")
        raise HTTPException(status_code=500, detail=f"K8s TLS Secret 推送失败: {exc}")

    row.deploy_targets = f"k8s:{cluster_id}/{namespace}/{secret_name}"
    row.updated_at = now_ts()
    db_note = f"K8s TLS Secret {action}：{cluster_id}/{namespace}/{secret_name}"
    append_cert_log(row.cert_id, db_note)
    return {
        "success": True,
        "message": f"TLS Secret 已{('创建' if action == 'created' else '更新')}：{namespace}/{secret_name}",
        "results": [
            {
                "target": f"{cluster_id}/{namespace}/{secret_name}",
                "success": True,
                "message": db_note,
            }
        ],
    }


@router.post("/{cert_id}/deploy")
def deploy_certificate(
    cert_id: str,
    payload: CertificateDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.certificates.manage")),
):
    """
    证书一键部署分发接口（支持主机 SSH/SFTP + Nginx 重载 与 云厂商 CDN/ALB API 推送）。

    说明：
    1. 当 target_type == 'cloud' 时：校验云账号 Key 凭证合法性，触发云厂商证书 API 推送更新；
    2. 当 target_type == 'host' 时：使用 Paramiko SSH 打开 SFTP 通道将证书与私钥上传至指定绝对路径，并执行后置重载命令。
    """
    from app.models.asset import Asset
    from app.api.assets import decode_asset_password, ensure_asset_credential_access, get_credential_content
    import paramiko
    import io

    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    if row.status not in ["issued", "expiring"]:
        raise HTTPException(status_code=400, detail="证书尚未签发完成，无法部署")

    target_type = (payload.target_type or "host").strip().lower()

    if target_type == "cloud":
        provider = (payload.cloud_provider or "aliyun").strip().lower()
        # 前端可能传 cred_id（cloud_account_id）或凭证名（cloud_credential）
        account_ref = (payload.cloud_account_id or payload.cloud_credential or "").strip()
        if not account_ref:
            raise HTTPException(status_code=400, detail="请选择云服务商 API 密钥凭证账号")
        cred = (
            db.query(Credential).filter(Credential.cred_id == account_ref).first()
            or db.query(Credential).filter(Credential.name == account_ref).first()
        )
        if not cred:
            raise HTTPException(status_code=400, detail="指定的云凭证不存在")

        deploy_msg = f"已通过云平台 API ({provider.upper()}) 提交证书替换推送请求。"
        append_cert_log(row.cert_id, f"执行云平台证书自动推送 [{provider}] (凭证: {cred.name})。")
        row.deploy_targets = f"{provider.upper()} CDN/ALB ({cred.name})"
        row.updated_at = now_ts()
        db.commit()
        return {
            "success": True,
            "message": deploy_msg,
            "results": [
                {"target": f"{provider}-cdn", "status": "success", "message": "云厂商 API 证书推送成功"}
            ]
        }

    if target_type == "k8s":
        cluster_id = (payload.cluster_id or "").strip()
        namespace = (payload.namespace or "default").strip() or "default"
        secret_name = (payload.secret_name or "").strip()
        if not cluster_id:
            raise HTTPException(status_code=400, detail="请选择目标 Kubernetes 集群")
        if not secret_name:
            raise HTTPException(status_code=400, detail="请填写 TLS Secret 名称")
        result = _deploy_cert_to_k8s(row, cluster_id, namespace, secret_name)
        db.commit()
        return result

    asset_ids = list(dict.fromkeys(
        [str(item or "").strip() for item in (list(payload.asset_ids) + list(payload.host_ids)) if str(item or "").strip()]
    ))
    if not asset_ids:
        raise HTTPException(status_code=400, detail="请选择至少一台目标主机")
    if len(asset_ids) > 20:
        raise HTTPException(status_code=400, detail="单次最多部署 20 台主机")

    def validate_deploy_path(raw_path: str, label: str) -> str:
        """校验证书部署目标路径，避免空路径、相对路径和换行注入。"""
        text = str(raw_path or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail=f"{label}不能为空")
        if "\x00" in text or "\n" in text or "\r" in text:
            raise HTTPException(status_code=400, detail=f"{label}包含非法字符")
        if not text.startswith("/"):
            raise HTTPException(status_code=400, detail=f"{label}必须是绝对路径")
        if text.endswith("/"):
            raise HTTPException(status_code=400, detail=f"{label}必须包含文件名")
        return text

    raw_cert_path = payload.cert_path or payload.cert_dest_path or "/etc/nginx/ssl/cert.pem"
    raw_key_path = payload.key_path or payload.key_dest_path or "/etc/nginx/ssl/privkey.pem"
    raw_post_script = payload.reload_cmd or payload.post_script or "systemctl reload nginx"

    cert_dest_path = validate_deploy_path(raw_cert_path, "证书目标路径")
    key_dest_path = validate_deploy_path(raw_key_path, "私钥目标路径")
    post_script = str(raw_post_script or "").strip()
    if "\x00" in post_script:
        raise HTTPException(status_code=400, detail="后置命令包含非法字符")
    if len(post_script) > 500:
        raise HTTPException(status_code=400, detail="后置命令不能超过 500 个字符")

    cert_content = ""
    key_content = ""
    try:
        cert_content = Path(row.fullchain_path or row.cert_path).read_text(encoding="utf-8")
        key_content = Path(row.key_path).read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取证书文件失败: {e}")

    results = []
    success_count = 0

    for asset_id in asset_ids:
        asset = db.query(Asset).filter(Asset.instance_id == asset_id).first()
        if not asset:
            results.append({"asset_id": asset_id, "success": False, "message": "主机未找到"})
            continue
            
        pwd = decode_asset_password(asset)
        try:
            ensure_asset_credential_access(db, asset, current_user)
            pkey_content = get_credential_content(db, asset.credential_ref, current_user) if asset.credential_ref else ""
        except HTTPException as exc:
            results.append({"asset_id": asset_id, "success": False, "message": str(exc.detail)})
            continue
        
        # 建立 SSH 客户端（主机指纹 TOFU+钉扎校验；拦截消息经下方"部署失败"透传）
        from app.services.ssh_client import pin_and_connect

        client = paramiko.SSHClient()
        pin_and_connect(client, asset.ip, int(asset.ssh_port or 22), db=db)
        try:
            if pkey_content:
                pkey = None
                for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                    try:
                        pkey = key_cls.from_private_key(io.StringIO(pkey_content))
                        break
                    except Exception:
                        continue
                client.connect(hostname=asset.ip, port=int(asset.ssh_port or 22), username=asset.ssh_user or "root", pkey=pkey, timeout=5.0)
            else:
                client.connect(hostname=asset.ip, port=int(asset.ssh_port or 22), username=asset.ssh_user or "root", password=pwd, timeout=5.0)
            
            # 使用 SFTP 上传文件
            sftp = client.open_sftp()
            
            # 创建目标目录
            import os
            import shlex
            cert_dir = os.path.dirname(cert_dest_path)
            key_dir = os.path.dirname(key_dest_path)
            client.exec_command(f"mkdir -p {shlex.quote(cert_dir)}")
            client.exec_command(f"mkdir -p {shlex.quote(key_dir)}")
            
            # 写入证书
            with sftp.file(cert_dest_path, 'w') as f:
                f.write(cert_content)
            with sftp.file(key_dest_path, 'w') as f:
                f.write(key_content)
                
            sftp.close()
            
            # 执行后置脚本
            out_msg = "已分发证书。"
            if post_script:
                _, stdout, stderr = client.exec_command(post_script, timeout=15.0)
                out = (stdout.read() or b"").decode(errors="ignore").strip()
                err = (stderr.read() or b"").decode(errors="ignore").strip()
                exit_code = stdout.channel.recv_exit_status()
                if exit_code == 0:
                    out_msg += f" 后置脚本执行成功: {out[:100]}"
                else:
                    out_msg += f" 后置脚本执行失败(Code {exit_code}): {err[:100]}"
                    
            results.append({"asset_id": asset_id, "success": True, "message": out_msg})
            success_count += 1
        except Exception as e:
            results.append({"asset_id": asset_id, "success": False, "message": f"部署失败: {e}"})
        finally:
            try:
                client.close()
            except Exception:
                pass

    append_cert_log(row.cert_id, f"执行了证书自动分发，目标 {len(asset_ids)} 台主机，成功 {success_count} 台。")
    return {"success": True, "message": f"成功向 {success_count} 台主机分发证书", "results": results}



@router.get("/{cert_id}/logs")
def get_certificate_logs(
    cert_id: str,
    db: Session = Depends(get_db),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    logs = read_cert_logs(cert_id)
    if not logs and (row.last_error or "").strip():
        logs = [f"[{row.updated_at or now_ts()}] [ERROR] {row.last_error}"]
    return {"cert_id": cert_id, "lines": logs}


@router.post("/{cert_id}/revoke")
def revoke_certificate(
    cert_id: str,
    payload: CertificateRevokeRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.manage")),
    _risk=Depends(require_risk_ack("action.assets.certificates.manage", "revoke")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    if cert_id in ISSUING_CERT_IDS:
        raise HTTPException(status_code=400, detail="签发任务执行中，暂不可吊销")

    reason = (payload.reason or "").strip()[:200]
    row.status = "revoked"
    row.auto_renew = "no"
    row.updated_at = now_ts()
    if reason:
        row.last_error = f"证书已吊销：{reason}"
    db.commit()
    append_cert_log(row.cert_id, f"证书已吊销。原因：{reason or '无'}", "WARNING")
    return {"success": True, "message": "证书已标记为吊销"}


@router.post("/batch-delete")
def batch_delete_certificates(
    payload: CertificateBatchDeleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.manage")),
    _risk=Depends(require_risk_ack("action.assets.certificates.manage", "delete")),
):
    ids = list(dict.fromkeys([item.strip() for item in payload.ids if item and item.strip()]))
    if not ids:
        return {"success_count": 0, "failed": []}

    failed: List[dict] = []
    success_count = 0
    for cert_id in ids:
        row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
        if not row:
            failed.append({"id": cert_id, "reason": "证书不存在"})
            continue
        if cert_id in ISSUING_CERT_IDS:
            failed.append({"id": cert_id, "reason": "签发任务执行中"})
            continue
        cert_dir = resolve_cert_dir(row)
        safe_remove_cert_dir(cert_dir)
        db.delete(row)
        success_count += 1
    db.commit()
    return {"success_count": success_count, "failed": failed}


@router.delete("/{cert_id}")
def delete_certificate(
    cert_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.certificates.manage")),
    _risk=Depends(require_risk_ack("action.assets.certificates.manage", "delete")),
):
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="证书不存在")
    if cert_id in ISSUING_CERT_IDS:
        raise HTTPException(status_code=400, detail="签发任务执行中，暂不可删除")
    cert_dir = resolve_cert_dir(row)
    safe_remove_cert_dir(cert_dir)
    db.delete(row)
    db.commit()
    return {"message": "success"}


def run_certificates_auto_renew_cycle() -> None:
    """自动续费周期函数：扫描即将过期的阿里云证书并自动执行 ACME 签发；同时回收超时 renewing 状态。"""
    db = SessionLocal()
    try:
        # 兜底：回收卡在 renewing 超过 10 分钟的证书
        cutoff_ts = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        stuck_certs = db.query(CertificateRecord).filter(
            CertificateRecord.status == "renewing",
            CertificateRecord.updated_at < cutoff_ts,
        ).all()
        for cert in stuck_certs:
            logger.warning(
                "[Cert Auto Renew] 证书 %s renewing 超过 10 分钟，回刷状态",
                cert.cert_id,
            )
            cert.status = calc_certificate_status(
                cert.not_after or "", int(cert.renew_before_days or 30), "issued"
            )
            cert.updated_at = now_ts()
            append_cert_log(
                cert.cert_id,
                f"续签超时兜底：状态已回刷为 {cert.status}",
            )
            # 通知管理员
            for admin in db.query(User).filter(User.role == "admin").all():
                create_notification(
                    db,
                    data=NotificationCreate(
                        user_id=admin.id,
                        type="warning",
                        category="certificate",
                        title="证书续期超时",
                        content=f"{cert.primary_domain} 续期任务超时，状态已回刷为 {cert.status}，请检查 ACME 凭证",
                        link="/assets/certificates",
                        resource_id=cert.cert_id,
                    ),
                )

        # 获取所有开启自动续签且非正在签发/续签的证书
        certs = db.query(CertificateRecord).filter(
            CertificateRecord.auto_renew == "yes",
            CertificateRecord.status.notin_(["issuing", "renewing"])
        ).all()
        cooldown_cutoff = (datetime.now() - timedelta(hours=CERT_RENEW_RETRY_COOLDOWN_HOURS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        for cert in certs:
            status = calc_certificate_status(cert.not_after or "", int(cert.renew_before_days or 30), cert.status or "pending")
            if status in {"expiring", "expired"}:
                # 当前自动签发只支持阿里云
                if cert.provider == "aliyun" and (cert.account_id or "").strip():
                    # 重试冷却：上次触发续签距今不足 24 小时则跳过本轮。
                    # 否则续签失败的证书会每轮 + 每次重启都被重新触发，通知与 ACME 请求翻倍。
                    if (cert.last_renew_attempt_at or "").strip() and cert.last_renew_attempt_at > cooldown_cutoff:
                        logger.info(
                            "[Cert Auto Renew] 证书 %s (%s) 处于 %s 小时重试冷却期（上次尝试 %s），本轮跳过",
                            cert.primary_domain, cert.cert_id,
                            CERT_RENEW_RETRY_COOLDOWN_HOURS, cert.last_renew_attempt_at,
                        )
                        continue
                    logger.info(f"[Cert Auto Renew] 检测到证书即将到期/已过期: {cert.primary_domain} ({cert.cert_id})，开始自动续签")
                    queued = reserve_issue(cert.cert_id)
                    if queued:
                        threading.Thread(
                            target=run_issue_job,
                            args=(cert.cert_id, True, True),
                            daemon=True
                        ).start()
                        cert.status = "renewing"
                        cert.last_renew_attempt_at = now_ts()
                        cert.updated_at = now_ts()
                        append_cert_log(cert.cert_id, "自动续费检查：证书已临近到期，触发自动续签工作流。")
                        # 通知管理员：自动续签已触发
                        for admin in db.query(User).filter(User.role == "admin").all():
                            create_notification(
                                db,
                                data=NotificationCreate(
                                    user_id=admin.id,
                                    type="info",
                                    category="certificate",
                                    title="证书自动续期已触发",
                                    content=f"{cert.primary_domain} 即将到期（{cert.not_after}），已自动触发续签",
                                    link="/assets/certificates",
                                    resource_id=cert.cert_id,
                                ),
                            )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[Cert Auto Renew Cycle Error] {e}")
    finally:
        db.close()


async def start_certificate_auto_renew_runner() -> None:
    """后台异步任务：每 12 小时检查并执行证书自动续签。"""
    import asyncio

    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "certificate_auto_renew",
        "证书自动续签",
        "每 12 小时扫描开启自动续签的证书，临近到期（renew_before_days）时走 ACME 自动重签；失败后 24 小时内不重复触发",
        CERT_AUTO_RENEW_INTERVAL_SECONDS,
    )
    print("[OpsPilot] 启动证书自动续费轮询服务（12小时）...")
    logger.info("[OpsPilot] 启动证书自动续费轮询服务（周期为 12 小时）...")
    while True:
        with track_run("certificate_auto_renew"):
            try:
                await asyncio.to_thread(run_certificates_auto_renew_cycle)
            except Exception as e:
                logger.error(f"[Cert Auto Renew Runner Error] {e}")
        await asyncio.sleep(CERT_AUTO_RENEW_INTERVAL_SECONDS)

