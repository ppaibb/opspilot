"""
域名资产管理接口
"""
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session
import requests

from app.core.crypto import decrypt_secret
from app.core.security import get_current_user, require_permissions, require_risk_ack
from app.db.database import get_db
from app.models.asset import DomainRecord, DomainDNSRecord
from app.models.credential import Credential
from app.models.user import User
from app.services.credential_access import ensure_credential_access, filter_credentials_for_user

router = APIRouter()


DOMAIN_PROVIDER_REGISTRY: dict[str, dict] = {
    "aliyun": {
        "label": "Alibaba Cloud DNS",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "tencent": {
        "label": "Tencent Cloud DNSPod",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "aws": {
        "label": "AWS Route53",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "cloudflare": {
        "label": "Cloudflare DNS",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "godaddy": {
        "label": "GoDaddy DNS",
        "supports_acme": False,
        "supports_cert_distribution": False,
    },
    "huawei": {
        "label": "Huawei Cloud DNS",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "azure": {
        "label": "Azure DNS",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "gcp": {
        "label": "Google Cloud DNS",
        "supports_acme": True,
        "supports_cert_distribution": True,
    },
    "digitalocean": {
        "label": "DigitalOcean DNS",
        "supports_acme": True,
        "supports_cert_distribution": False,
    },
    "custom": {
        "label": "Custom Provider",
        "supports_acme": False,
        "supports_cert_distribution": False,
    },
}

DOMAIN_FALLBACK_CANDIDATES: dict[str, list[str]] = {
    "aliyun": ["opspilot.com", "opspilot.cn", "opspilot.net"],
    "tencent": ["example.cn", "example.net", "service.cn"],
    "aws": ["example.com", "example.org", "infra.example.com"],
    "cloudflare": ["example.io", "edge.example.io"],
    "huawei": ["example.cloud", "example.cn"],
    "azure": ["example.azure", "corp.example.com"],
    "gcp": ["example.gcp", "service.example.com"],
    "digitalocean": ["example.do", "edge.example.do"],
    "godaddy": ["example.com", "brand-example.com"],
    "custom": ["example.com"],
}

DOMAIN_ACCOUNT_ALLOWED_TYPES = {"accesskey", "token", "api token", "ak/sk", "aksk"}


class DomainProviderItem(BaseModel):
    key: str
    label: str
    supports_acme: bool
    supports_cert_distribution: bool


class DomainAccountItem(BaseModel):
    account_id: str
    account_name: str
    provider_key: str
    provider_label: str
    credential_type: str


class DomainAccountDomainsRequest(BaseModel):
    account_id: str


class DomainImportRequest(BaseModel):
    account_id: str
    domain: str
    remark: str = ""


class DomainResponse(BaseModel):
    id: int
    domain: str
    display_domain: str = ""
    dns_credential: str = ""
    root_domain: str
    record_type: str
    record_value: str
    provider: str
    provider_label: str = ""
    status: str
    account_id: str = ""
    account_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    expire_reminder_days: int = 30
    remark: str = ""
    acme_enabled: str = "no"
    cert_targets: str = ""
    records_count: int = 0

    class Config:
        from_attributes = True


class DomainCreate(BaseModel):
    domain: str
    dns_credential: str = ""
    root_domain: str = ""
    record_type: str = "A"
    record_value: str = "-"
    provider: str = "custom"
    status: str = "active"
    account_id: str = ""
    account_name: str = ""
    created_at: str = ""
    expires_at: str = ""
    expire_reminder_days: int = 30
    remark: str = ""
    acme_enabled: bool = False
    cert_targets: str = ""


class DomainUpdate(BaseModel):
    remark: str = ""
    expires_at: str = ""
    expire_reminder_days: int = 30
    status: str = "active"
    acme_enabled: bool = False
    cert_targets: str = ""
    # 域名账户：None=本次不改，""=解绑，非空=绑定到该凭证（编辑页可给已建域名补关联）
    account_id: str | None = None


class DomainBatchDeleteRequest(BaseModel):
    ids: List[int]


class DomainDNSRecordResponse(BaseModel):
    id: int
    domain_id: int
    host: str
    record_type: str
    record_value: str
    ttl: int
    remark: str = ""
    updated_at: str = ""


class DomainDNSRecordCreate(BaseModel):
    host: str = "@"
    record_type: str = "A"
    record_value: str
    ttl: int = 600
    remark: str = ""


class DomainDNSRecordUpdate(BaseModel):
    host: str = "@"
    record_type: str = "A"
    record_value: str
    ttl: int = 600
    remark: str = ""


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_utc_to_local(utc_str: str) -> str:
    """将 UTC 的 ISO 8601 时间格式转为当地的 YYYY-MM-DD HH:MM:SS 格式"""
    if not utc_str:
        return now_ts()
    try:
        utc_str = utc_str.replace("Z", "")
        if "T" in utc_str:
            parts = utc_str.split("T")
            date_part = parts[0]
            time_part = parts[1].split(".")[0]  # 去除毫秒
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(utc_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        # 阿里云等 API 使用的是 UTC 时间，转为东八区北京时间
        dt_local = dt + timedelta(hours=8)
        return dt_local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return now_ts()


def parse_aliyun_timestamp(ts_val) -> str:
    """解析阿里云返回的时间戳（支持毫秒时间戳、整型、浮点、ISO 8601等格式）"""
    if not ts_val:
        return now_ts()
    if isinstance(ts_val, (int, float)):
        try:
            dt = datetime.fromtimestamp(ts_val / 1000.0)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return now_ts()
    ts_str = str(ts_val).strip()
    if ts_str.isdigit():
        try:
            dt = datetime.fromtimestamp(int(ts_str) / 1000.0)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return now_ts()
    return parse_utc_to_local(ts_str)


def parse_expiry_from_whois(text: str) -> str | None:
    """从 WHOIS 文本中通过正则解析出域名过期时间"""
    if not text:
        return None
    lines = text.splitlines()
    for line in lines:
        line_lower = line.lower()
        if "expire" in line_lower or "expiry" in line_lower or "expires" in line_lower:
            if ":" in line:
                val = line.split(":", 1)[1].strip()
            elif "=" in line:
                val = line.split("=", 1)[1].strip()
            else:
                continue
            val_clean = re.sub(r'[\(\)]', '', val).strip()
            # 查找 YYYY-MM-DD 或 DD-MMM-YYYY 等常见日期模式
            date_match = re.search(r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{1,2}:\d{1,2})?|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{1,2}-\w{3}-\d{4})', val_clean)
            if date_match:
                date_str = date_match.group(1)
                for fmt in (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d",
                    "%Y.%m.%d",
                    "%d-%m-%Y",
                    "%d.%m.%Y",
                    "%d-%b-%Y",
                ):
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        return dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass
            try:
                t_str = val_clean.replace("Z", "")
                if "T" in t_str:
                    t_str = t_str.split("T")[0]
                dt = datetime.strptime(t_str, "%Y-%m-%d")
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return None


def query_whois_expiration(domain: str) -> str | None:
    """通过 socket 直接查询 WHOIS 服务器，获取域名的真实到期日期"""
    try:
        import socket
        # 1. 查询 iana 获取真实的 WHOIS 服务器
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(("whois.iana.org", 43))
        s.send(f"{domain}\r\n".encode("utf-8"))
        response = b""
        while True:
            data = s.recv(4096)
            if not data:
                break
            response += data
        s.close()
        text = response.decode("utf-8", errors="ignore")
        
        refer_server = ""
        for line in text.splitlines():
            if line.lower().startswith("refer:"):
                refer_server = line.split(":", 1)[1].strip()
                break
        
        whois_data = text
        if refer_server:
            # 2. 查询对应的顶级域名 WHOIS 服务器获取详细信息
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.settimeout(3.0)
            s2.connect((refer_server, 43))
            s2.send(f"{domain}\r\n".encode("utf-8"))
            response2 = b""
            while True:
                data2 = s2.recv(4096)
                if not data2:
                    break
                response2 += data2
            s2.close()
            whois_data = response2.decode("utf-8", errors="ignore")
        
        return parse_expiry_from_whois(whois_data)
    except Exception:
        return None


def query_rdap_expiration(domain: str) -> str | None:
    """通过 RDAP 协议 (HTTP JSON) 获取域名的注册到期日期"""
    url = f"https://rdap.org/domain/{domain.strip().lower()}"
    headers = {"Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
        if resp.status_code < 400:
            payload = resp.json()
            if isinstance(payload, dict):
                events = payload.get("events") or []
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    action = str(event.get("eventAction") or "").strip().lower()
                    if action == "expiration":
                        date_raw = str(event.get("eventDate") or "").strip()
                        if date_raw:
                            try:
                                t_str = date_raw.replace("Z", "")
                                if "T" in t_str:
                                    t_str = t_str.split("T")[0]
                                return datetime.strptime(t_str, "%Y-%m-%d").strftime("%Y-%m-%d")
                            except Exception:
                                pass
    except Exception:
        pass
    return None


def query_domain_expiration_any(domain: str) -> str | None:
    """尝试通过 RDAP 和 WHOIS 接口获取过期时间"""
    try:
        exp = query_rdap_expiration(domain)
        if exp:
            return exp
    except Exception:
        pass
        
    try:
        exp = query_whois_expiration(domain)
        if exp:
            return exp
    except Exception:
        pass
        
    return None


def add_aliyun_dns_record(
    access_key: str,
    secret_key: str,
    domain_name: str,
    host: str,
    record_type: str,
    value: str,
    ttl: int,
) -> str:
    """调用阿里云 API 添加解析记录"""
    endpoint = "https://alidns.aliyuncs.com/"
    params: dict[str, str] = {
        "Action": "AddDomainRecord",
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "DomainName": domain_name,
        "RR": host,
        "Type": record_type,
        "Value": value,
        "TTL": str(ttl),
    }
    params["Signature"] = sign_aliyun_rpc(params, secret_key, method="POST")
    try:
        resp = requests.post(endpoint, data=params, timeout=12)
        payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"添加阿里云解析记录失败：网络异常({str(e)})")

    if resp.status_code >= 400:
        message = payload.get("Message") if isinstance(payload, dict) else ""
        raise HTTPException(status_code=400, detail=f"添加阿里云解析记录失败：{message}")
        
    return str(payload.get("RecordId") or "")


def get_aliyun_record_id(
    access_key: str,
    secret_key: str,
    domain_name: str,
    host: str,
    record_type: str,
    value: str,
) -> str | None:
    """在阿里云上通过主机名、类型、值查找对应的 RecordId"""
    endpoint = "https://alidns.aliyuncs.com/"
    page_number = 1
    page_size = 100
    
    while True:
        params: dict[str, str] = {
            "Action": "DescribeDomainRecords",
            "Format": "JSON",
            "Version": "2015-01-09",
            "AccessKeyId": access_key,
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "DomainName": domain_name,
            "PageNumber": str(page_number),
            "PageSize": str(page_size),
        }
        params["Signature"] = sign_aliyun_rpc(params, secret_key)
        try:
            resp = requests.get(endpoint, params=params, timeout=12)
            payload = resp.json()
        except Exception:
            return None
            
        if resp.status_code >= 400 or not isinstance(payload, dict):
            return None
            
        records_node = payload.get("DomainRecords", {}) if isinstance(payload, dict) else {}
        records = records_node.get("Record", []) if isinstance(records_node, dict) else []
        for item in records:
            if not isinstance(item, dict):
                continue
            item_host = str(item.get("RR") or "@").strip().lower()
            item_type = str(item.get("Type") or "A").strip().upper()
            item_value = str(item.get("Value") or "").strip()
            
            if (
                item_host == host.strip().lower() and
                item_type == record_type.strip().upper() and
                item_value == value.strip()
            ):
                return str(item.get("RecordId") or "")
                
        total_count = int(records_node.get("TotalCount") or payload.get("TotalCount") or 0)
        if len(records) < page_size:
            break
        page_number += 1
        if page_number > 50:
            break
            
    return None


def delete_aliyun_dns_record_by_id(
    access_key: str,
    secret_key: str,
    record_id: str,
) -> None:
    """调用阿里云 API 删除解析记录"""
    endpoint = "https://alidns.aliyuncs.com/"
    params: dict[str, str] = {
        "Action": "DeleteDomainRecord",
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "RecordId": record_id,
    }
    params["Signature"] = sign_aliyun_rpc(params, secret_key, method="POST")
    try:
        resp = requests.post(endpoint, data=params, timeout=12)
        payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"删除阿里云解析记录失败：网络异常({str(e)})")

    if resp.status_code >= 400:
        message = payload.get("Message") if isinstance(payload, dict) else ""
        raise HTTPException(status_code=400, detail=f"删除阿里云解析记录失败：{message}")


def update_aliyun_dns_record_by_id(
    access_key: str,
    secret_key: str,
    record_id: str,
    host: str,
    record_type: str,
    value: str,
    ttl: int,
) -> None:
    """调用阿里云 API 修改解析记录"""
    endpoint = "https://alidns.aliyuncs.com/"
    params: dict[str, str] = {
        "Action": "UpdateDomainRecord",
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "RecordId": record_id,
        "RR": host,
        "Type": record_type,
        "Value": value,
        "TTL": str(ttl),
    }
    params["Signature"] = sign_aliyun_rpc(params, secret_key, method="POST")
    try:
        resp = requests.post(endpoint, data=params, timeout=12)
        payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"修改阿里云解析记录失败：网络异常({str(e)})")

    if resp.status_code >= 400:
        message = payload.get("Message") if isinstance(payload, dict) else ""
        raise HTTPException(status_code=400, detail=f"修改阿里云解析记录失败：{message}")


def fetch_aliyun_domain_expiration(access_key: str, secret_key: str, domain_name: str) -> str | None:
    """调用阿里云 DNS API 获取当前关联域名的到期时间"""
    endpoint = "https://alidns.aliyuncs.com/"
    params: dict[str, str] = {
        "Action": "DescribeDomains",
        "Format": "JSON",
        "Version": "2015-01-09",
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "KeyWord": domain_name,
        "SearchMode": "EXACT",
    }
    params["Signature"] = sign_aliyun_rpc(params, secret_key)
    try:
        resp = requests.get(endpoint, params=params, timeout=12)
        payload = resp.json()
    except Exception:
        return None

    if resp.status_code >= 400 or not isinstance(payload, dict):
        return None

    domains_node = payload.get("Domains", {}) if isinstance(payload, dict) else {}
    records = domains_node.get("Domain", []) if isinstance(domains_node, dict) else []
    for item in records:
        if not isinstance(item, dict):
            continue
        name = str(item.get("DomainName") or "").strip().lower()
        if name == domain_name.strip().lower():
            expire_val = item.get("ExpirationDate") or item.get("ExpirationTimestamp") or ""
            if expire_val:
                if isinstance(expire_val, (int, float)):
                    try:
                        return datetime.fromtimestamp(expire_val / 1000.0).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                elif isinstance(expire_val, str) and expire_val.isdigit():
                    try:
                        return datetime.fromtimestamp(int(expire_val) / 1000.0).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                elif isinstance(expire_val, str):
                    try:
                        t_str = expire_val.replace("Z", "")
                        if "T" in t_str:
                            t_str = t_str.split("T")[0]
                        return datetime.strptime(t_str, "%Y-%m-%d").strftime("%Y-%m-%d")
                    except Exception:
                        pass
    return None


def normalize_provider_key(provider: str) -> str:
    text = (provider or "").strip().lower()
    if text in DOMAIN_PROVIDER_REGISTRY:
        return text
    return text or "custom"


def provider_label(provider: str) -> str:
    key = normalize_provider_key(provider)
    item = DOMAIN_PROVIDER_REGISTRY.get(key)
    if item:
        return str(item["label"])
    return provider or "Unknown Provider"


def infer_provider_key(text: str) -> str:
    raw = (text or "").strip().lower()
    if any(token in raw for token in ["aliyun", "阿里", "alibaba"]):
        return "aliyun"
    if any(token in raw for token in ["tencent", "腾讯", "dnspod"]):
        return "tencent"
    if any(token in raw for token in ["aws", "route53"]):
        return "aws"
    if "cloudflare" in raw:
        return "cloudflare"
    if "huawei" in raw or "华为" in raw:
        return "huawei"
    if "azure" in raw:
        return "azure"
    if "gcp" in raw or "google cloud" in raw:
        return "gcp"
    if "digitalocean" in raw or "do " in f"{raw} ":
        return "digitalocean"
    if "godaddy" in raw:
        return "godaddy"
    return "custom"


def is_domain_account_credential(row: Credential) -> bool:
    ctype = (row.type or "").strip().lower()
    if ctype in DOMAIN_ACCOUNT_ALLOWED_TYPES:
        return True
    if "accesskey" in ctype or "token" in ctype:
        return True
    return False


def parse_root_domain(domain: str) -> str:
    parts = [p for p in (domain or "").split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


def aliyun_percent_encode(raw: str) -> str:
    text = str(raw or "")
    return quote(text, safe="~")


def parse_access_key_content(raw: str) -> tuple[str, str]:
    """兼容 JSON / key=value / key: value 形式的 AK/SK 内容。"""
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
                or parsed.get("access_key_id")
                or parsed.get("SecretId")
                or parsed.get("secret_id")
                or parsed.get("secretId")
                or ""
            ).strip()
            secret_key = str(
                parsed.get("secret_key")
                or parsed.get("secretKey")
                or parsed.get("AccessKeySecret")
                or parsed.get("SecretKey")
                or parsed.get("secret_key")
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
        or kv.get("secretid")
        or ""
    ).strip()
    secret_key = (
        kv.get("secret_key")
        or kv.get("secretkey")
        or kv.get("accesskeysecret")
        or ""
    ).strip()
    return access_key, secret_key


def resolve_account_credential(
    db: Session,
    account_id: str,
    current_user: User | None = None,
) -> tuple[Credential, str, str]:
    """解析域名账户凭证：返回（凭证行，AccessKey/SecretKey）。

    三家云厂商在凭证管理里的内容格式统一为 JSON {"access_key", "secret_key"}
    （见 AGENTS.md 凭证接口约定），SecretId/SecretKey 只是腾讯云的叫法，
    解析时同样兼容。
    """
    row = db.query(Credential).filter(Credential.cred_id == account_id).first()
    row = ensure_credential_access(
        row,
        current_user,
        not_found_message="域名账户不存在",
        denied_message="当前账号无权使用该域名账户",
    )
    if not is_domain_account_credential(row):
        raise HTTPException(status_code=400, detail="所选凭证不是可用于 DNS 的云 API 账户")
    raw = decrypt_secret(row.content or "")
    access_key, secret_key = parse_access_key_content(raw)
    if not access_key or not secret_key:
        raise HTTPException(status_code=400, detail="凭证内容无法解析 AccessKey/SecretKey，请在凭证管理按 AK/SK 结构保存")
    return row, access_key, secret_key


def resolve_account_keys(
    db: Session,
    account_id: str,
    current_user: User | None = None,
) -> tuple[str, str]:
    _, access_key, secret_key = resolve_account_credential(db, account_id, current_user)
    return access_key, secret_key


def sign_aliyun_rpc(params: dict[str, str], secret_key: str, method: str = "GET") -> str:
    sorted_params = sorted((k, str(v)) for k, v in params.items())
    canonicalized = "&".join(
        f"{aliyun_percent_encode(k)}={aliyun_percent_encode(v)}"
        for k, v in sorted_params
    )
    string_to_sign = f"{method}&%2F&{aliyun_percent_encode(canonicalized)}"
    key = f"{secret_key}&".encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def fetch_aliyun_domains(access_key: str, secret_key: str) -> list[str]:
    endpoint = "https://alidns.aliyuncs.com/"
    page_number = 1
    page_size = 100
    result: list[str] = []

    while True:
        params: dict[str, str] = {
            "Action": "DescribeDomains",
            "Format": "JSON",
            "Version": "2015-01-09",
            "AccessKeyId": access_key,
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "PageNumber": str(page_number),
            "PageSize": str(page_size),
        }
        params["Signature"] = sign_aliyun_rpc(params, secret_key)

        try:
            resp = requests.get(endpoint, params=params, timeout=12)
            payload = resp.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"拉取阿里云域名失败：网络异常({str(e)})")

        if resp.status_code >= 400:
            message = payload.get("Message") if isinstance(payload, dict) else ""
            raise HTTPException(status_code=400, detail=f"拉取阿里云域名失败：HTTP {resp.status_code} {message}")

        if isinstance(payload, dict) and payload.get("Code"):
            raise HTTPException(
                status_code=400,
                detail=f"拉取阿里云域名失败：{payload.get('Code')} {payload.get('Message') or ''}".strip(),
            )

        domains_node = payload.get("Domains", {}) if isinstance(payload, dict) else {}
        records = domains_node.get("Domain", []) if isinstance(domains_node, dict) else []
        for item in records:
            if not isinstance(item, dict):
                continue
            name = str(item.get("DomainName") or "").strip().lower()
            if name:
                result.append(name)

        total_count = int(domains_node.get("TotalCount") or payload.get("TotalCount") or 0)
        if len(result) >= total_count or len(records) < page_size:
            break
        page_number += 1
        if page_number > 50:
            break

    return sorted(list(dict.fromkeys(result)))


def fetch_aliyun_domain_records(access_key: str, secret_key: str, domain_name: str) -> list[dict]:
    endpoint = "https://alidns.aliyuncs.com/"
    page_number = 1
    page_size = 100
    result: list[dict] = []

    while True:
        params: dict[str, str] = {
            "Action": "DescribeDomainRecords",
            "Format": "JSON",
            "Version": "2015-01-09",
            "AccessKeyId": access_key,
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "DomainName": domain_name,
            "PageNumber": str(page_number),
            "PageSize": str(page_size),
        }
        params["Signature"] = sign_aliyun_rpc(params, secret_key)

        try:
            resp = requests.get(endpoint, params=params, timeout=12)
            payload = resp.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"拉取阿里云解析记录失败：网络异常({str(e)})")

        if resp.status_code >= 400:
            message = payload.get("Message") if isinstance(payload, dict) else ""
            raise HTTPException(status_code=400, detail=f"拉取阿里云解析记录失败：HTTP {resp.status_code} {message}")

        if isinstance(payload, dict) and payload.get("Code"):
            raise HTTPException(
                status_code=400,
                detail=f"拉取阿里云解析记录失败：{payload.get('Code')} {payload.get('Message') or ''}".strip(),
            )

        records_node = payload.get("DomainRecords", {}) if isinstance(payload, dict) else {}
        records = records_node.get("Record", []) if isinstance(records_node, dict) else []
        for item in records:
            if not isinstance(item, dict):
                continue
            host = str(item.get("RR") or "@").strip() or "@"
            record_type = str(item.get("Type") or "A").strip().upper() or "A"
            value = str(item.get("Value") or "").strip()
            ttl_raw = item.get("TTL") or 600
            remark = str(item.get("Remark") or "").strip()
            try:
                ttl = int(ttl_raw)
            except Exception:
                ttl = 600
            if not value:
                continue

            update_time_raw = item.get("UpdateTimestamp") or item.get("UpdateTime") or ""
            updated_at = parse_aliyun_timestamp(update_time_raw)

            result.append(
                {
                    "host": host,
                    "record_type": record_type,
                    "record_value": value,
                    "ttl": ttl,
                    "remark": remark,
                    "updated_at": updated_at,
                }
            )

        total_count = int(records_node.get("TotalCount") or payload.get("TotalCount") or 0)
        if len(result) >= total_count or len(records) < page_size:
            break
        page_number += 1
        if page_number > 50:
            break

    return result


def _tencent_dnspod_client(access_key: str, secret_key: str):
    """构建腾讯云 DNSPod 客户端（复用项目已有的 tencentcloud-sdk-python 依赖）。

    DNSPod 是全局服务、无 Region 概念；沿用 assets.py 里 CVM 全局接口的做法，
    region 固定传 ap-guangzhou，实际 endpoint 由 HttpProfile 指定。
    """
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.dnspod.v20210323 import dnspod_client

    cred = credential.Credential(access_key, secret_key)
    http_profile = HttpProfile()
    http_profile.endpoint = "dnspod.tencentcloudapi.com"
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    return dnspod_client.DnspodClient(cred, "ap-guangzhou", client_profile)


def fetch_tencent_domains(access_key: str, secret_key: str) -> list[str]:
    """调用 DNSPod DescribeDomainList 拉取账户下全部域名。"""
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
        TencentCloudSDKException,
    )
    from tencentcloud.dnspod.v20210323 import models as dnspod_models

    client = _tencent_dnspod_client(access_key, secret_key)
    result: list[str] = []
    offset = 0
    limit = 3000
    while True:
        req = dnspod_models.DescribeDomainListRequest()
        req.Type = "ALL"
        req.Offset = offset
        req.Limit = limit
        try:
            resp = client.DescribeDomainList(req)
        except TencentCloudSDKException as e:
            raise HTTPException(status_code=400, detail=f"拉取腾讯云域名失败：{e.code} {e.message}".strip())
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"拉取腾讯云域名失败：网络异常({str(e)})")

        items = list(getattr(resp, "DomainList", None) or [])
        for item in items:
            name = str(getattr(item, "Name", "") or "").strip().lower()
            if name:
                result.append(name)

        count_info = getattr(resp, "DomainCountInfo", None)
        total_count = int(getattr(count_info, "DomainTotal", 0) or 0) if count_info else 0
        offset += len(items)
        if not items or (total_count and offset >= total_count) or len(items) < limit:
            break
        if offset > 100000:
            break

    return sorted(list(dict.fromkeys(result)))


def fetch_tencent_domain_records(access_key: str, secret_key: str, domain_name: str) -> list[dict]:
    """调用 DNSPod DescribeRecordList 拉取单个域名的全部解析记录。

    返回与 fetch_aliyun_domain_records 同构的 dict 列表：
    host / record_type / record_value / ttl / remark / updated_at。
    同一 (host, type, value) 在多条线路下重复出现时只保留第一条——本地
    domain_dns_records 表没有线路列，展开存会越同步越多（见 sync 注释）。
    """
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
        TencentCloudSDKException,
    )
    from tencentcloud.dnspod.v20210323 import models as dnspod_models

    client = _tencent_dnspod_client(access_key, secret_key)
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    offset = 0
    limit = 3000
    while True:
        req = dnspod_models.DescribeRecordListRequest()
        req.Domain = domain_name
        req.Offset = offset
        req.Limit = limit
        try:
            resp = client.DescribeRecordList(req)
        except TencentCloudSDKException as e:
            raise HTTPException(status_code=400, detail=f"拉取腾讯云解析记录失败：{e.code} {e.message}".strip())
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"拉取腾讯云解析记录失败：网络异常({str(e)})")

        items = list(getattr(resp, "RecordList", None) or [])
        for item in items:
            host = str(getattr(item, "Name", "") or "@").strip() or "@"
            record_type = str(getattr(item, "Type", "") or "A").strip().upper() or "A"
            value = str(getattr(item, "Value", "") or "").strip()
            if not value:
                continue
            key = (host.lower(), record_type, value)
            if key in seen:
                continue
            seen.add(key)
            try:
                ttl = int(getattr(item, "TTL", 0) or 600)
            except Exception:
                ttl = 600
            remark = str(getattr(item, "Remark", "") or "").strip()
            line = str(getattr(item, "Line", "") or "").strip()
            if line and line != "默认":
                remark = f"{remark} [线路:{line}]".strip()[:255]
            updated_at = str(getattr(item, "UpdatedOn", "") or "").strip() or now_ts()
            result.append(
                {
                    "host": host,
                    "record_type": record_type,
                    "record_value": value,
                    "ttl": ttl,
                    "remark": remark,
                    "updated_at": updated_at,
                }
            )

        count_info = getattr(resp, "RecordCountInfo", None)
        total_count = int(getattr(count_info, "TotalCount", 0) or 0) if count_info else 0
        offset += len(items)
        if not items or (total_count and offset >= total_count) or len(items) < limit:
            break
        if offset > 100000:
            break

    return result


HUAWEI_DNS_REGIONS: tuple[str, ...] = ("ap-southeast-3", "cn-north-4")
"""华为云 DNS 的区域 endpoint 候选。

公网 Zone 是全局资源，但 API 本身是区域 endpoint；最新版官方文档要求
选 ap-southeast-3，旧版写的是 cn-north-4，两版说法不一致。实现上按顺序
尝试：先 ap-southeast-3，查不到/报错再试 cn-north-4。区域错了的典型症状
是 404 或空列表（不是 401），不要误判为 AK/SK 错误。
无区域主机 dns.myhuaweicloud.com 未出现在官方 SDK region 表里，不用。
"""


def _huawei_encode(value: str) -> str:
    """RFC 3986 编码：只保留 A-Z a-z 0-9 - _ . ~ 不编码，其余 %XY 大写。"""
    return quote(str(value or ""), safe="-_.~")


def _huawei_canonical_uri(path: str) -> str:
    uri = "/".join(_huawei_encode(seg) for seg in path.split("/"))
    return uri if uri.endswith("/") else uri + "/"


def _huawei_canonical_query_string(params: dict) -> str:
    items = sorted((_huawei_encode(k), _huawei_encode(v)) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in items)


def _huawei_signed_get(
    access_key: str,
    secret_key: str,
    region: str,
    path: str,
    params: dict,
) -> requests.Response:
    """华为云 SDK-HMAC-SHA256 签名 GET（无新增依赖，纯 stdlib + requests）。

    官方《API Request Signing Guide》：CanonicalRequest 六行、StringToSign
    三行，Signature = Hex(HMAC-SHA256(SK, StringToSign))，SK 直接做 key，
    没有 AWS SigV4 式多步派生。签名的 query string 与实际发出的 URL 共用
    同一份已编码字符串，避免 requests 二次编码导致签名不一致。
    """
    host = f"dns.{region}.myhuaweicloud.com"
    sdk_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    qs = _huawei_canonical_query_string(params)
    headers = {"content-type": "application/json", "host": host, "x-sdk-date": sdk_date}
    signed_headers = "content-type;host;x-sdk-date"
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
    canonical_request = "\n".join(
        [
            "GET",
            _huawei_canonical_uri(path),
            qs,
            canonical_headers,
            signed_headers,
            hashlib.sha256(b"").hexdigest(),  # GET 无 body
        ]
    )
    string_to_sign = "\n".join(
        [
            "SDK-HMAC-SHA256",
            sdk_date,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"https://{host}{path}" + ("?" + qs if qs else "")
    try:
        return requests.get(
            url,
            headers={
                "Content-Type": "application/json",
                "X-Sdk-Date": sdk_date,
                "Authorization": (
                    f"SDK-HMAC-SHA256 Access={access_key}, "
                    f"SignedHeaders={signed_headers}, Signature={signature}"
                ),
            },
            timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"华为云 DNS 请求失败：网络异常({str(e)})")


def _huawei_list_all(
    access_key: str,
    secret_key: str,
    region: str,
    path: str,
    params: dict,
    items_key: str,
) -> list[dict]:
    """华为云 DNS marker 分页拉全量：marker = 上一页最后一条资源的 id。"""
    out: list[dict] = []
    marker: str | None = None
    limit = int(params.get("limit", 500) or 500)
    for _ in range(50):
        page_params = dict(params)
        if marker:
            page_params["marker"] = marker
        resp = _huawei_signed_get(access_key, secret_key, region, path, page_params)
        if resp.status_code >= 400:
            payload = {}
            try:
                payload = resp.json()
            except Exception:
                pass
            code = payload.get("code", "") if isinstance(payload, dict) else ""
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            raise HTTPException(
                status_code=400,
                detail=f"华为云 DNS 请求失败：HTTP {resp.status_code} {code} {message}".strip(),
            )
        try:
            payload = resp.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"华为云 DNS 返回非 JSON：{str(e)}")
        page = payload.get(items_key, []) if isinstance(payload, dict) else []
        if not page:
            break
        out.extend(item for item in page if isinstance(item, dict))
        total = 0
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        if isinstance(metadata, dict):
            try:
                total = int(metadata.get("total_count") or 0)
            except Exception:
                total = 0
        if (total and len(out) >= total) or len(page) < limit:
            break
        marker = str(page[-1].get("id") or "")
        if not marker:
            break
    return out


def fetch_huawei_domains(access_key: str, secret_key: str) -> list[str]:
    """调用华为云 DNS ListZones 拉取账户下全部公网 Zone（域名）。"""
    result: list[str] = []
    last_error: Exception | None = None
    for region in HUAWEI_DNS_REGIONS:
        try:
            zones = _huawei_list_all(
                access_key, secret_key, region, "/v2/zones",
                {"type": "public", "limit": "500"}, "zones",
            )
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue
        for zone in zones:
            name = str(zone.get("name") or "").strip().lower().rstrip(".")
            if name:
                result.append(name)
        if result:
            break
    if not result and last_error is not None:
        raise last_error
    return sorted(list(dict.fromkeys(result)))


def _find_huawei_zone(
    access_key: str, secret_key: str, apex_domain: str
) -> tuple[dict, str] | None:
    """按 apex 精确查找公网 Zone，返回（zone, region）；各区域都找不到返回 None。"""
    last_error: Exception | None = None
    for region in HUAWEI_DNS_REGIONS:
        try:
            zones = _huawei_list_all(
                access_key, secret_key, region, "/v2/zones",
                {"type": "public", "name": apex_domain, "search_mode": "equal", "limit": "100"},
                "zones",
            )
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue
        for zone in zones:
            name = str(zone.get("name") or "").strip().lower().rstrip(".")
            if name == apex_domain:
                return zone, region
    if last_error is not None:
        raise last_error
    return None


def fetch_huawei_domain_records(access_key: str, secret_key: str, domain_name: str) -> list[dict]:
    """调用华为云 DNS ListRecordSets 拉取单个域名的全部解析记录。

    domain_name 可以是子域名（如 www.example.com）：先取 apex 找 Zone，
    再拉该 Zone 的 recordsets。recordset 的 name 是完整 FQDN（带结尾点号），
    去掉 zone 后缀即主机头，apex 本身对应 @；一个 recordset 的 records 是
    数组，展开成一值一行。SOA 等同步不支持的类型由 _normalize_synced_record
    过滤。Zone 找不到时抛错（而不是返回空列表），避免同步误删本地已有记录。
    """
    apex = parse_root_domain(domain_name)
    found = _find_huawei_zone(access_key, secret_key, apex)
    if found is None:
        raise HTTPException(
            status_code=400,
            detail=f"未在华为云 DNS 找到域名 {domain_name} 对应的 Zone，请确认域名已在华为云 DNS 托管，"
            "且 AK/SK 有 dns:zone:list / dns:recordset:list 权限",
        )
    zone, region = found
    zone_name = str(zone.get("name") or "").strip().lower().rstrip(".")
    zone_id = str(zone.get("id") or "")
    recordsets = _huawei_list_all(
        access_key, secret_key, region, f"/v2/zones/{zone_id}/recordsets",
        {"limit": "500"}, "recordsets",
    )
    result: list[dict] = []
    for item in recordsets:
        rs_name = str(item.get("name") or "").strip().lower().rstrip(".")
        if rs_name == zone_name:
            host = "@"
        elif rs_name.endswith("." + zone_name):
            host = rs_name[: -(len(zone_name) + 1)]
        else:
            host = rs_name
        record_type = str(item.get("type") or "").strip().upper()
        try:
            ttl = int(item.get("ttl") or 300)
        except Exception:
            ttl = 300
        updated_at = parse_utc_to_local(str(item.get("update_at") or ""))
        for value in item.get("records") or []:
            value_text = str(value or "").strip()
            if not value_text:
                continue
            result.append(
                {
                    "host": host,
                    "record_type": record_type,
                    "record_value": value_text,
                    "ttl": ttl,
                    "remark": "",
                    "updated_at": updated_at,
                }
            )
    return result


def validate_domain_format(domain: str) -> str:
    full_domain = (domain or "").strip().lower().rstrip(".")
    if not full_domain:
        raise HTTPException(status_code=400, detail="域名不能为空")
    if " " in full_domain:
        raise HTTPException(status_code=400, detail="域名不能包含空格")
    try:
        full_domain = full_domain.encode("idna").decode("ascii").lower()
    except Exception:
        raise HTTPException(status_code=400, detail="域名格式非法")
    if "." not in full_domain:
        raise HTTPException(status_code=400, detail="请输入完整域名")
    if not re.match(r"^[a-z0-9.-]+$", full_domain):
        raise HTTPException(status_code=400, detail="域名格式非法")
    return full_domain


def parse_expire_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except Exception:
        raise HTTPException(status_code=400, detail="到期时间格式错误，应为 YYYY-MM-DD")


def validate_domain_update(payload: DomainUpdate) -> None:
    _ = parse_expire_date(payload.expires_at)
    days = int(payload.expire_reminder_days or 0)
    if days < 0 or days > 365:
        raise HTTPException(status_code=400, detail="到期提醒天数需在 0-365 之间")
    status = (payload.status or "").strip().lower()
    if status not in {"active", "offline"}:
        raise HTTPException(status_code=400, detail="状态仅支持 active/offline")


def normalize_record_host(host: str) -> str:
    text = (host or "@").strip().lower()
    if not text:
        text = "@"
    if text == "*":
        return text
    if text == "@":
        return text
    if "." in text:
        raise HTTPException(status_code=400, detail="主机记录只需填写相对记录，如 www/api")
    if not re.match(r"^[a-z0-9_-]+$", text):
        raise HTTPException(status_code=400, detail="主机记录格式非法")
    return text


def validate_record_type(record_type: str) -> str:
    value = (record_type or "").strip().upper()
    allowed = {"A", "AAAA", "CNAME", "TXT", "MX", "NS"}
    if value not in allowed:
        raise HTTPException(status_code=400, detail=f"解析类型仅支持: {', '.join(sorted(allowed))}")
    return value


def validate_record_value(record_value: str) -> str:
    text = (record_value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="解析内容不能为空")
    if len(text) > 255:
        raise HTTPException(status_code=400, detail="解析内容过长，最多 255 字符")
    return text


def validate_ttl(ttl: int) -> int:
    value = int(ttl or 0)
    if value < 60 or value > 86400:
        raise HTTPException(status_code=400, detail="TTL 需在 60-86400 秒之间")
    return value


def serialize_dns_record(row: DomainDNSRecord) -> dict:
    return {
        "id": int(row.id),
        "domain_id": int(row.domain_id),
        "host": row.host or "@",
        "record_type": (row.record_type or "A").upper(),
        "record_value": row.record_value or "",
        "ttl": int(row.ttl or 600),
        "remark": row.remark or "",
        "updated_at": row.updated_at or "",
    }


def backfill_dns_records_from_legacy(db: Session, domain: DomainRecord) -> int:
    legacy_type = (domain.record_type or "").strip().upper() or "A"
    legacy_value = (domain.record_value or "").strip()
    if not legacy_value or legacy_value == "-":
        return 0

    row = DomainDNSRecord(
        domain_id=int(domain.id),
        host="@",
        record_type=legacy_type,
        record_value=legacy_value,
        ttl=600,
        remark=(domain.remark or "").strip()[:255],
        updated_at=now_ts(),
    )
    db.add(row)
    db.commit()
    return 1


def sync_domain_expiry(
    db: Session,
    domain: DomainRecord,
    current_user: User | None = None,
) -> None:
    """
    同步域名的到期时间 (expires_at)：优先调用云 API，若失败则回退到 RDAP/WHOIS 查询。

    注意：这是一个会发起公网请求（云 API + RDAP/WHOIS，最坏可达 10s+）的慢操作，
    域名到期时间极少变化（通常一年才变一次），因此严禁挂在用户的读接口上，
    只能由后台定时任务 / 域名新增导入时触发。
    """
    provider = normalize_provider_key(domain.provider or "")

    expires_at = None
    if provider == "aliyun" and domain.account_id:
        try:
            access_key, secret_key = resolve_account_keys(db, domain.account_id, current_user)
            expires_at = fetch_aliyun_domain_expiration(access_key, secret_key, domain.domain or "")
        except Exception:
            pass

    if not expires_at:
        expires_at = query_domain_expiration_any(domain.domain or "")

    if expires_at:
        domain.expires_at = expires_at
        domain.updated_at = now_ts()
        db.commit()


def _fetch_remote_domain_records(
    provider: str,
    access_key: str,
    secret_key: str,
    domain_name: str,
) -> list[dict] | None:
    """按厂商分派解析记录拉取。返回 None 表示该厂商未实现拉取。"""
    if provider == "aliyun":
        return fetch_aliyun_domain_records(access_key, secret_key, domain_name)
    if provider == "tencent":
        return fetch_tencent_domain_records(access_key, secret_key, domain_name)
    if provider == "huawei":
        return fetch_huawei_domain_records(access_key, secret_key, domain_name)
    return None


SYNCED_RECORD_TYPES = {"A", "AAAA", "CNAME", "TXT", "MX", "NS"}


def _normalize_synced_record(item: dict) -> dict | None:
    """归一化一条云端同步回来的解析记录；不支持的类型/非法值返回 None 跳过。

    与用户手动新建记录的严格校验不同：同步是批量操作，一条脏数据不能
    让整批同步静默失败（此前 validate_record_type 抛异常会被外层吞掉，
    导致整个域名 0 条记录）。主机头允许多级（如华为云 a.b 子域）。
    """
    host = str(item.get("host") or "@").strip().lower().rstrip(".") or "@"
    record_type = str(item.get("record_type") or "").strip().upper()
    if record_type not in SYNCED_RECORD_TYPES:
        return None
    record_value = str(item.get("record_value") or "").strip()
    if not record_value or len(record_value) > 255:
        return None
    try:
        ttl = int(item.get("ttl") or 600)
    except Exception:
        ttl = 600
    ttl = min(max(ttl, 60), 86400)
    remark = str(item.get("remark") or "").strip()[:255]
    updated_at = str(item.get("updated_at") or now_ts())
    return {
        "host": host,
        "record_type": record_type,
        "record_value": record_value,
        "ttl": ttl,
        "remark": remark,
        "updated_at": updated_at,
    }


def sync_domain_dns_records(
    db: Session,
    domain: DomainRecord,
    current_user: User | None = None,
) -> None:
    """
    同步 DNS 解析记录 (domain_dns_records)：获取云端真实记录，合并(upsert)并清理本地失效记录。

    要求域名已关联域名账户（account_id 非空）：只有拿着账户 AK/SK 才能调云 API。
    未关联账户、或厂商未实现拉取时不做任何事——调用方需引导用户先选账户。
    """
    provider = normalize_provider_key(domain.provider or "")

    if provider in {"aliyun", "tencent", "huawei"} and domain.account_id:
        try:
            access_key, secret_key = resolve_account_keys(db, domain.account_id, current_user)
            remote_records = _fetch_remote_domain_records(
                provider, access_key, secret_key, domain.domain or ""
            )
            if remote_records is not None:
                local_records = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain.id).all()
                local_map = {(r.host, r.record_type, r.record_value): r for r in local_records}

                remote_keys = set()
                for item in remote_records:
                    normalized = _normalize_synced_record(item)
                    if normalized is None:
                        continue
                    host = normalized["host"]
                    record_type = normalized["record_type"]
                    record_value = normalized["record_value"]
                    ttl = normalized["ttl"]
                    remark = normalized["remark"]
                    updated_at = normalized["updated_at"]

                    key = (host, record_type, record_value)
                    remote_keys.add(key)

                    if key in local_map:
                        row = local_map[key]
                        row.ttl = ttl
                        row.remark = remark
                        row.updated_at = updated_at
                    else:
                        row = DomainDNSRecord(
                            domain_id=int(domain.id),
                            host=host,
                            record_type=record_type,
                            record_value=record_value,
                            ttl=ttl,
                            remark=remark,
                            updated_at=updated_at,
                        )
                        db.add(row)

                # 清除云端已经不存在的记录
                for key, row in local_map.items():
                    if key not in remote_keys:
                        db.delete(row)

                domain.updated_at = now_ts()
                db.commit()
        except Exception as exc:  # noqa: BLE001
            # 同步失败不阻塞主流程，但要留日志——此前静默吞掉是 0 条记录查无可查的原因之一
            _logger.warning(f"[Domain DNS Sync] 域名 {domain.domain} 同步失败: {exc}")
    elif provider == "custom":
        local_count = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain.id).count()
        if local_count == 0:
            backfill_dns_records_from_legacy(db, domain)


def sync_domain_from_provider(
    db: Session,
    domain: DomainRecord,
    current_user: User | None = None,
) -> None:
    """
    同步域名的全部真实信息（到期时间 + 解析记录）。
    用于域名新增/导入、手动同步、后台定时任务等非读路径场景。
    """
    sync_domain_expiry(db, domain, current_user)
    sync_domain_dns_records(db, domain, current_user)


# DNS 解析记录同步的内存节流：{domain_id: 上次同步的时间戳(秒)}
# 避免用户每次打开解析记录抽屉都实时打一次云端 API
_DNS_SYNC_LAST_TS: dict[int, float] = {}
_DNS_SYNC_THROTTLE_SECONDS = 300  # 同一域名 5 分钟内不重复同步


def sync_domain_dns_records_throttled(
    db: Session,
    domain: DomainRecord,
    current_user: User | None = None,
) -> None:
    """带节流的解析记录同步：同一域名 5 分钟内只真正打一次云端，其余直接跳过走本地库。"""
    import time

    domain_id = int(domain.id)
    now = time.monotonic()
    last = _DNS_SYNC_LAST_TS.get(domain_id)
    if last is not None and (now - last) < _DNS_SYNC_THROTTLE_SECONDS:
        return
    _DNS_SYNC_LAST_TS[domain_id] = now
    sync_domain_dns_records(db, domain, current_user)


def sync_aliyun_dns_records_if_empty(
    db: Session,
    domain: DomainRecord,
    current_user: User | None = None,
) -> int:
    """兼容旧接口调用，直接代理到新的 sync_domain_from_provider"""
    try:
        sync_domain_from_provider(db, domain, current_user)
        return db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain.id).count()
    except Exception:
        return 0


def decode_domain(domain: str) -> str:
    if not domain:
        return ""
    try:
        if domain.startswith("*."):
            return "*." + domain[2:].encode("ascii").decode("idna")
        return domain.encode("ascii").decode("idna")
    except Exception:
        return domain


def serialize_domain(record: DomainRecord, db: Session = None, current_user: User = None) -> dict:
    records_count = 0
    if db:
        records_count = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == record.id).count()

    return {
        "id": int(record.id),
        "domain": record.domain or "",
        "display_domain": decode_domain(record.domain or ""),
        "dns_credential": record.dns_credential or "",
        "root_domain": record.root_domain or "",
        "record_type": record.record_type or "A",
        "record_value": record.record_value or "-",
        "provider": normalize_provider_key(record.provider or "custom"),
        "provider_label": provider_label(record.provider or "custom"),
        "status": record.status or "active",
        "account_id": (getattr(record, "account_id", "") or ""),
        "account_name": (getattr(record, "account_name", "") or ""),
        "created_at": (getattr(record, "created_at", "") or ""),
        "updated_at": record.updated_at or "",
        "expires_at": (getattr(record, "expires_at", "") or ""),
        "expire_reminder_days": int(getattr(record, "expire_reminder_days", 30) or 30),
        "remark": (getattr(record, "remark", "") or ""),
        "acme_enabled": (getattr(record, "acme_enabled", "no") or "no"),
        "cert_targets": (getattr(record, "cert_targets", "") or ""),
        "records_count": records_count,
    }


def build_domain_accounts(db: Session, current_user: User | None = None) -> list[dict]:
    rows = db.query(Credential).order_by(Credential.id.desc()).all()
    result: list[dict] = []
    for row in filter_credentials_for_user(rows, current_user):
        if not is_domain_account_credential(row):
            continue
        marker = f"{row.name} {row.scope} {row.type}"
        key = infer_provider_key(marker)
        result.append(
            {
                "account_id": row.cred_id,
                "account_name": row.name,
                "provider_key": key,
                "provider_label": provider_label(key),
                "credential_type": row.type,
            }
        )
    return result


@router.get("/providers", response_model=List[DomainProviderItem])
def get_domain_providers():
    return [
        {
            "key": key,
            "label": item["label"],
            "supports_acme": bool(item["supports_acme"]),
            "supports_cert_distribution": bool(item["supports_cert_distribution"]),
        }
        for key, item in DOMAIN_PROVIDER_REGISTRY.items()
    ]


@router.get("/accounts", response_model=List[DomainAccountItem])
def get_domain_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return build_domain_accounts(db, current_user)


@router.post("/account-domains")
def get_account_domains(
    payload: DomainAccountDomainsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account_id = (payload.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id 不能为空")

    accounts = build_domain_accounts(db, current_user)
    target = next((a for a in accounts if a["account_id"] == account_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="域名账户不存在")

    provider = target["provider_key"]
    candidates: list[str] = []
    source = "fallback"
    if provider in {"aliyun", "tencent", "huawei"}:
        access_key, secret_key = resolve_account_keys(db, account_id, current_user)
        if provider == "aliyun":
            candidates = fetch_aliyun_domains(access_key, secret_key)
        elif provider == "tencent":
            candidates = fetch_tencent_domains(access_key, secret_key)
        else:
            candidates = fetch_huawei_domains(access_key, secret_key)
        source = f"{provider}-api"
    else:
        candidates = list(DOMAIN_FALLBACK_CANDIDATES.get(provider, DOMAIN_FALLBACK_CANDIDATES["custom"]))
        candidates.append(f"managed-{account_id.lower()[-4:]}.example.com")

    existing = {
        (row.domain or "").strip().lower()
        for row in db.query(DomainRecord).all()
    }
    final_list = sorted(list({d for d in candidates if d and d.lower() not in existing}))
    return {
        "account_id": account_id,
        "provider_key": provider,
        "provider_label": provider_label(provider),
        "source": source,
        "domains": [{"domain": d, "display_domain": decode_domain(d)} for d in final_list],
    }


@router.post("/import", response_model=DomainResponse)
def import_domain(
    payload: DomainImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    account_id = (payload.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="请选择域名账户")

    domain = validate_domain_format(payload.domain)
    exists = db.query(DomainRecord).filter(DomainRecord.domain == domain).first()
    if exists:
        raise HTTPException(status_code=400, detail="该域名已存在")

    accounts = build_domain_accounts(db, current_user)
    target = next((a for a in accounts if a["account_id"] == account_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="域名账户不存在")

    created_at = now_ts()
    new_row = DomainRecord(
        domain=domain,
        dns_credential=target["account_name"],
        root_domain=parse_root_domain(domain),
        record_type="A",
        record_value="-",
        provider=target["provider_key"],
        status="active",
        account_id=target["account_id"],
        account_name=target["account_name"],
        created_at=created_at,
        updated_at=created_at,
        expires_at="",
        expire_reminder_days=30,
        remark=(payload.remark or "").strip()[:255],
        acme_enabled="no",
        cert_targets="",
    )
    db.add(new_row)
    db.commit()
    db.refresh(new_row)

    # 导入时立即从云端或 WHOIS 同步到期时间及解析记录
    try:
        sync_domain_from_provider(db, new_row, current_user)
    except Exception:
        pass

    return serialize_domain(new_row, db, current_user)


@router.get("", response_model=List[DomainResponse])
def get_domains(
    q: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(DomainRecord)
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                DomainRecord.domain.ilike(f"%{keyword}%"),
                DomainRecord.record_value.ilike(f"%{keyword}%"),
                DomainRecord.root_domain.ilike(f"%{keyword}%"),
                DomainRecord.provider.ilike(f"%{keyword}%"),
                DomainRecord.dns_credential.ilike(f"%{keyword}%"),
                DomainRecord.remark.ilike(f"%{keyword}%"),
            )
        )
    rows = query.order_by(DomainRecord.id.desc()).all()
    return [serialize_domain(row, db, current_user) for row in rows]


@router.get("/{domain_id}/records", response_model=List[DomainDNSRecordResponse])
def get_domain_dns_records(
    domain_id: int,
    q: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="域名记录未找到")

    query = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain_id)
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(
            or_(
                DomainDNSRecord.host.ilike(f"%{keyword}%"),
                DomainDNSRecord.record_type.ilike(f"%{keyword}%"),
                DomainDNSRecord.record_value.ilike(f"%{keyword}%"),
                DomainDNSRecord.remark.ilike(f"%{keyword}%"),
            )
        )
    # 查看解析记录时只做「解析记录」的增量同步（带节流），
    # 不再同步域名到期时间——到期时间由后台定时任务维护，避免阻塞读接口。
    try:
        sync_domain_dns_records_throttled(db, domain, current_user)
    except Exception:
        pass

    rows = query.order_by(DomainDNSRecord.id.desc()).all()
    return [serialize_dns_record(row) for row in rows]


@router.post("/{domain_id}/records", response_model=DomainDNSRecordResponse)
def create_domain_dns_record(
    domain_id: int,
    payload: DomainDNSRecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="域名记录未找到")

    host = normalize_record_host(payload.host)
    record_type = validate_record_type(payload.record_type)
    record_value = validate_record_value(payload.record_value)
    ttl = validate_ttl(payload.ttl)

    # 如果是阿里云托管域名，在创建本地记录前调用阿里云 API 新增解析记录
    if normalize_provider_key(domain.provider or "") == "aliyun" and domain.account_id:
        access_key, secret_key = resolve_account_keys(db, domain.account_id, current_user)
        add_aliyun_dns_record(
            access_key=access_key,
            secret_key=secret_key,
            domain_name=domain.domain,
            host=host,
            record_type=record_type,
            value=record_value,
            ttl=ttl,
        )

    row = DomainDNSRecord(
        domain_id=domain_id,
        host=host,
        record_type=record_type,
        record_value=record_value,
        ttl=ttl,
        remark=(payload.remark or "").strip()[:255],
        updated_at=now_ts(),
    )
    db.add(row)
    domain.updated_at = now_ts()
    db.commit()
    db.refresh(row)
    return serialize_dns_record(row)


@router.put("/{domain_id}/records/{record_id}", response_model=DomainDNSRecordResponse)
def update_domain_dns_record(
    domain_id: int,
    record_id: int,
    payload: DomainDNSRecordUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="域名记录未找到")

    row = db.query(DomainDNSRecord).filter(
        DomainDNSRecord.id == record_id,
        DomainDNSRecord.domain_id == domain_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="解析记录不存在")

    new_host = normalize_record_host(payload.host)
    new_record_type = validate_record_type(payload.record_type)
    new_record_value = validate_record_value(payload.record_value)
    new_ttl = validate_ttl(payload.ttl)

    # 如果是阿里云托管域名，调用阿里云 API 修改解析记录
    if normalize_provider_key(domain.provider or "") == "aliyun" and domain.account_id:
        access_key, secret_key = resolve_account_keys(db, domain.account_id, current_user)
        ali_record_id = get_aliyun_record_id(
            access_key=access_key,
            secret_key=secret_key,
            domain_name=domain.domain,
            host=row.host,
            record_type=row.record_type,
            value=row.record_value,
        )
        if not ali_record_id:
            raise HTTPException(status_code=400, detail="未在阿里云上找到此解析记录对应的 ID，修改失败。")
        update_aliyun_dns_record_by_id(
            access_key=access_key,
            secret_key=secret_key,
            record_id=ali_record_id,
            host=new_host,
            record_type=new_record_type,
            value=new_record_value,
            ttl=new_ttl,
        )

    row.host = new_host
    row.record_type = new_record_type
    row.record_value = new_record_value
    row.ttl = new_ttl
    row.remark = (payload.remark or "").strip()[:255]
    row.updated_at = now_ts()
    domain.updated_at = now_ts()
    db.commit()
    db.refresh(row)
    return serialize_dns_record(row)


@router.delete("/{domain_id}/records/{record_id}")
def delete_domain_dns_record(
    domain_id: int,
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
    _risk=Depends(require_risk_ack("action.assets.domains.manage", "delete")),
):
    """删除解析记录（与域名删除同键同动作：domains:delete 二次确认）。"""
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="域名记录未找到")

    row = db.query(DomainDNSRecord).filter(
        DomainDNSRecord.id == record_id,
        DomainDNSRecord.domain_id == domain_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="解析记录不存在")

    # 如果是阿里云托管域名，在删除本地记录前，调用阿里云 API 删除解析记录
    if normalize_provider_key(domain.provider or "") == "aliyun" and domain.account_id:
        access_key, secret_key = resolve_account_keys(db, domain.account_id, current_user)
        ali_record_id = get_aliyun_record_id(
            access_key=access_key,
            secret_key=secret_key,
            domain_name=domain.domain,
            host=row.host,
            record_type=row.record_type,
            value=row.record_value,
        )
        if ali_record_id:
            delete_aliyun_dns_record_by_id(
                access_key=access_key,
                secret_key=secret_key,
                record_id=ali_record_id,
            )

    db.delete(row)
    domain.updated_at = now_ts()
    db.commit()
    return {"message": "success"}


@router.post("", response_model=DomainResponse)
def create_domain(
    payload: DomainCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    domain = validate_domain_format(payload.domain)
    exists = db.query(DomainRecord).filter(DomainRecord.domain == domain).first()
    if exists:
        raise HTTPException(status_code=400, detail="该域名已存在")

    provider_key = normalize_provider_key(payload.provider)
    created_at = (payload.created_at or "").strip() or now_ts()

    # 先解析账户：账户决定了服务商（前端选账户时已同步 provider，后端再兜底一次，
    # 避免账户与服务商不一致导致同步走错分支）；同时拿到展示名。
    account_id = (payload.account_id or "").strip()
    account_name = (payload.account_name or "").strip()
    if account_id:
        cred_row, _, _ = resolve_account_credential(db, account_id, current_user)
        account_name = account_name or cred_row.name
        provider_key = infer_provider_key(f"{cred_row.name} {cred_row.scope} {cred_row.type}")

    expires_at = (payload.expires_at or "").strip()
    if not expires_at:
        # 如果未传入到期时间，且有云商授权，则尝试从云商获取；否则使用 WHOIS
        if provider_key == "aliyun" and payload.account_id:
            try:
                access_key, secret_key = resolve_account_keys(db, payload.account_id, current_user)
                expires_at = fetch_aliyun_domain_expiration(access_key, secret_key, domain)
            except Exception:
                pass
        if not expires_at:
            expires_at = query_domain_expiration_any(domain)
        # 云商/WHOIS 都查不到时留空：伪造 +365 天会污染到期告警与统计，
        # 前端显示「未录入」引导用户补录真实到期日
    else:
        expires_at = parse_expire_date(expires_at)

    reminder_days = int(payload.expire_reminder_days or 30)
    if reminder_days < 0 or reminder_days > 365:
        raise HTTPException(status_code=400, detail="到期提醒天数需在 0-365 之间")

    row = DomainRecord(
        domain=domain,
        dns_credential=(payload.dns_credential or "").strip() or account_name or provider_label(provider_key),
        root_domain=(payload.root_domain or "").strip().lower() or parse_root_domain(domain),
        record_type=(payload.record_type or "A").strip().upper(),
        record_value=(payload.record_value or "-").strip(),
        provider=provider_key,
        status=(payload.status or "active").strip().lower(),
        account_id=account_id,
        account_name=account_name,
        created_at=created_at,
        updated_at=now_ts(),
        expires_at=expires_at,
        expire_reminder_days=reminder_days,
        remark=(payload.remark or "").strip()[:255],
        acme_enabled="yes" if payload.acme_enabled else "no",
        cert_targets=(payload.cert_targets or "").strip()[:255],
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # 创建完成后，如果是云商授权，自动同步一次以加载解析记录
    try:
        sync_domain_from_provider(db, row, current_user)
    except Exception:
        pass

    return serialize_domain(row, db, current_user)


@router.put("/{domain_id}", response_model=DomainResponse)
def update_domain(
    domain_id: int,
    payload: DomainUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    validate_domain_update(payload)
    row = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="域名记录未找到")

    row.remark = (payload.remark or "").strip()[:255]
    # 到期时间留空 = 不修改（与凭证编辑「留空不改」口径一致）；
    # 不能走 parse_expire_date("") 的 +365 天默认，否则清空日期保存会把到期时间静默推后一年
    if (payload.expires_at or "").strip():
        row.expires_at = parse_expire_date(payload.expires_at)
    row.expire_reminder_days = int(payload.expire_reminder_days or 30)
    row.status = (payload.status or "active").strip().lower()
    row.acme_enabled = "yes" if payload.acme_enabled else "no"
    row.cert_targets = (payload.cert_targets or "").strip()[:255]
    row.updated_at = now_ts()

    account_changed = False
    if payload.account_id is not None:
        new_account_id = (payload.account_id or "").strip()
        if new_account_id:
            cred_row, _, _ = resolve_account_credential(db, new_account_id, current_user)
            account_changed = new_account_id != (row.account_id or "")
            row.account_id = new_account_id
            row.account_name = cred_row.name
            # 账户决定服务商，与新建保持一致，避免账户与服务商错配导致同步走错分支
            row.provider = infer_provider_key(f"{cred_row.name} {cred_row.scope} {cred_row.type}")
            if not (row.dns_credential or "").strip():
                row.dns_credential = cred_row.name
        else:
            account_changed = bool(row.account_id)
            row.account_id = ""
            row.account_name = ""

    db.commit()
    db.refresh(row)

    # 绑定/换绑账户后立即同步一次：已建域名不用删了重加，编辑保存即补拉解析记录
    if account_changed:
        try:
            sync_domain_from_provider(db, row, current_user)
        except Exception:
            pass

    return serialize_domain(row, db, current_user)


@router.post("/batch-delete")
def batch_delete_domains(
    payload: DomainBatchDeleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.domains.manage")),
    _risk=Depends(require_risk_ack("action.assets.domains.manage", "delete")),
):
    """批量删除域名（与单删同键同动作：domains:delete 二次确认）。"""
    ids: List[int] = []
    for item in payload.ids:
        try:
            value = int(item)
        except Exception:
            continue
        if value > 0:
            ids.append(value)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {"success_count": 0, "failed": []}

    failed: List[dict] = []
    success_count = 0
    for domain_id in ids:
        row = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
        if not row:
            failed.append({"id": domain_id, "reason": "域名记录不存在"})
            continue
        db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain_id).delete()
        db.delete(row)
        success_count += 1
    db.commit()
    return {"success_count": success_count, "failed": failed}


@router.delete("/{domain_id}")
def delete_domain(
    domain_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.domains.manage")),
    _risk=Depends(require_risk_ack("action.assets.domains.manage", "delete")),
):
    row = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="域名记录未找到")
    db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain_id).delete()
    db.delete(row)
    db.commit()
    return {"message": "success"}


# ---------------------------------------------------------------------------
# 后台定时任务：同步域名到期时间
# ---------------------------------------------------------------------------

_logger = logging.getLogger("opspilot.domains")

# 域名到期时间同步周期：24 小时一次（到期时间极少变化，一年才变一次）
DOMAIN_EXPIRY_SYNC_INTERVAL_SECONDS = 24 * 60 * 60


def run_domain_expiry_sync_cycle() -> None:
    """遍历所有域名，刷新本地缓存的到期时间 (expires_at)。"""
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        domains = db.query(DomainRecord).all()
        _logger.info(f"[Domain Expiry Sync] 开始同步 {len(domains)} 个域名的到期时间")
        ok = 0
        for domain in domains:
            try:
                sync_domain_expiry(db, domain, None)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                _logger.warning(f"[Domain Expiry Sync] 域名 {domain.domain} 同步失败: {exc}")
        _logger.info(f"[Domain Expiry Sync] 本轮完成，成功 {ok}/{len(domains)}")
    except Exception as exc:  # noqa: BLE001
        _logger.warning(f"[Domain Expiry Sync] 周期执行异常: {exc}")
    finally:
        db.close()


async def start_domain_expiry_sync_runner() -> None:
    """常驻后台任务：每 24 小时同步一次所有域名的到期时间。"""
    import asyncio

    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "domain_expiry_sync",
        "域名到期同步",
        "每 24 小时从注册商/云平台刷新一次域名到期时间，读接口只读本地缓存保持秒开",
        DOMAIN_EXPIRY_SYNC_INTERVAL_SECONDS,
    )
    # 启动后延迟一段时间再首次执行，避免与应用启动争抢资源
    await asyncio.sleep(60)
    while True:
        with track_run("domain_expiry_sync"):
            try:
                await asyncio.to_thread(run_domain_expiry_sync_cycle)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(f"[Domain Expiry Sync] runner 异常: {exc}")
        await asyncio.sleep(DOMAIN_EXPIRY_SYNC_INTERVAL_SECONDS)

