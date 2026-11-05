"""
域名资产管理接口
"""
from datetime import datetime, timedelta
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
from app.services import domain_resources
from app.services.dns_providers.base import DNSProviderError
from app.services.dns_providers.registry import DNS_PROVIDERS, provider_metadata

router = APIRouter()


DOMAIN_PROVIDER_REGISTRY = {item["key"]: item for item in provider_metadata()}

DOMAIN_ACCOUNT_ALLOWED_TYPES = {"accesskey", "token", "api token", "ak/sk", "aksk"}


class DomainProviderItem(BaseModel):
    key: str
    label: str
    supports_acme: bool
    supports_cert_distribution: bool
    supports_import: bool = False
    supports_dns_records: bool = False
    supports_record_write: bool = True
    credential_types: list[str] = []
    supports_multi_values: bool = False
    supports_proxy: bool = False
    supports_priority: bool = False
    can_rename_records: bool = True
    record_types: list[str] = []


class DomainAccountItem(BaseModel):
    account_id: str
    account_name: str
    provider_key: str
    provider_label: str
    credential_type: str


class DomainAccountDomainsRequest(BaseModel):
    account_id: str
    provider: str = ""


class DomainImportRequest(BaseModel):
    account_id: str
    domain: str
    provider: str = ""
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
    provider_zone_id: str = ""

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
    account_id: str | None = None
    provider: str | None = None
    remark: str = ""
    expires_at: str = ""
    expire_reminder_days: int = 30
    status: str = "active"
    acme_enabled: bool = False
    cert_targets: str = ""


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
    provider_record_id: str = ""
    record_values: list[str] = []
    proxied: bool | None = None
    priority: int | None = None
    read_only: bool = False


class DomainDNSRecordCreate(BaseModel):
    host: str = "@"
    record_type: str = "A"
    record_value: str = ""
    ttl: int = 600
    remark: str = ""
    record_values: list[str] | None = None
    proxied: bool | None = None
    priority: int | None = None


class DomainDNSRecordUpdate(BaseModel):
    host: str = "@"
    record_type: str = "A"
    record_value: str = ""
    ttl: int = 600
    remark: str = ""
    record_values: list[str] | None = None
    proxied: bool | None = None
    priority: int | None = None


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cloud_operation(operation):
    """适配器只抛可展示错误，在接口边界转换为真实HTTP失败，便于审计。"""
    try:
        return operation()
    except DNSProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


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
    priority: int | None = None,
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
    if priority is not None:
        params["Priority"] = str(priority)
    params["Signature"] = sign_aliyun_rpc(params, secret_key, method="POST")
    try:
        resp = requests.post(endpoint, data=params, timeout=12)
        payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"添加阿里云解析记录失败：网络异常({str(e)})")

    if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("Code")):
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

    if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("Code")):
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
    priority: int | None = None,
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
    if priority is not None:
        params["Priority"] = str(priority)
    params["Signature"] = sign_aliyun_rpc(params, secret_key, method="POST")
    try:
        resp = requests.post(endpoint, data=params, timeout=12)
        payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"修改阿里云解析记录失败：网络异常({str(e)})")

    if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("Code")):
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
                or parsed.get("SecretId")
                or parsed.get("accessKey")
                or parsed.get("AccessKeyId")
                or ""
            ).strip()
            secret_key = str(
                parsed.get("secret_key")
                or parsed.get("SecretKey")
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
        or kv.get("secretid")
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


def resolve_account_keys(
    db: Session,
    account_id: str,
    current_user: User | None = None,
) -> tuple[str, str]:
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
            raise HTTPException(status_code=502, detail="阿里云分页超过上限，已停止同步")

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
                    "provider_record_id": str(item.get("RecordId") or ""),
                    "priority": item.get("Priority"),
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
            raise HTTPException(status_code=502, detail="阿里云分页超过上限，已停止同步")

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
    return domain_resources.serialize_record(row)


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


def sync_domain_dns_records(db: Session, domain: DomainRecord, current_user: User | None = None) -> None:
    """云域名通过统一适配器完整同步；错误显式上抛，禁止吞错后假装成功。"""
    if normalize_provider_key(domain.provider) == "custom":
        if db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain.id).count() == 0:
            backfill_dns_records_from_legacy(db, domain)
        return
    domain_resources.sync_records(db, domain, current_user)


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
    sync_domain_dns_records(db, domain, current_user)
    _DNS_SYNC_LAST_TS[domain_id] = now


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
        "provider_zone_id": record.provider_zone_id or "",
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
    return provider_metadata()


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
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    account_id = payload.account_id.strip()
    account = next((item for item in build_domain_accounts(db, current_user) if item["account_id"] == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="域名账户不存在或无权使用")
    provider = normalize_provider_key(payload.provider or account["provider_key"])
    adapter, _credential = cloud_operation(lambda: domain_resources.load_adapter(db, provider, account_id, current_user))
    zones = cloud_operation(adapter.list_zones)
    existing = {row.domain.lower() for row in db.query(DomainRecord).all()}
    return {"account_id": account_id, "provider_key": provider, "provider_label": provider_label(provider),
            "source": f"{provider}-api", "domains": [
                {"domain": zone.name, "display_domain": decode_domain(zone.name), "zone_id": zone.id}
                for zone in zones if zone.name.lower() not in existing
            ]}


@router.post("/import", response_model=DomainResponse)
def import_domain(
    payload: DomainImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    name = validate_domain_format(payload.domain)
    account = next((item for item in build_domain_accounts(db, current_user) if item["account_id"] == payload.account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="域名账户不存在或无权使用")
    provider = normalize_provider_key(payload.provider or account["provider_key"])
    domain = cloud_operation(lambda: domain_resources.import_domain(db, provider, payload.account_id, name, payload.remark, current_user))
    # 保留导入后立即查询到期日期的原有行为，不让新纳管域名等24小时周期。
    # 查询失败保留未知值，由既有后台任务再次尝试，不伪造到期日期。
    try:
        sync_domain_expiry(db, domain, current_user)
    except Exception:
        db.rollback()
        logging.getLogger(__name__).warning("导入域名的到期日期暂未查询成功，将由周期任务重试")
    return serialize_domain(domain, db, current_user)


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
    domain_id: int, q: str = "", db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if domain is None:
        raise HTTPException(status_code=404, detail="域名记录未找到")
    if domain.account_id or domain.snapshot_credential_id:
        domain_resources.check_domain_access(db, domain, current_user)
    if domain.provider in DNS_PROVIDERS and domain.account_id:
        cloud_operation(lambda: sync_domain_dns_records_throttled(db, domain, current_user))
    elif domain.provider == "custom":
        sync_domain_dns_records(db, domain, current_user)
    query = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain_id)
    if q.strip():
        keyword = q.strip()
        query = query.filter(or_(DomainDNSRecord.host.ilike(f"%{keyword}%"),
            DomainDNSRecord.record_type.ilike(f"%{keyword}%"), DomainDNSRecord.record_values_json.ilike(f"%{keyword}%"),
            DomainDNSRecord.record_value.ilike(f"%{keyword}%"), DomainDNSRecord.remark.ilike(f"%{keyword}%")))
    return [serialize_dns_record(row) for row in query.order_by(DomainDNSRecord.id.desc()).all()]


@router.post("/{domain_id}/sync")
def refresh_domain_records(
    domain_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    """显式刷新云端快照，只同步DNS数据，不修改云端记录。"""
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if domain is None:
        raise HTTPException(status_code=404, detail="域名不存在")
    count = cloud_operation(lambda: domain_resources.sync_records(db, domain, current_user))
    import time

    _DNS_SYNC_LAST_TS[domain_id] = time.monotonic()
    return {"records_count": count}


@router.post("/{domain_id}/records", response_model=DomainDNSRecordResponse)
def create_domain_dns_record(
    domain_id: int, payload: DomainDNSRecordCreate, db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    if domain is None:
        raise HTTPException(status_code=404, detail="域名记录未找到")
    if domain.provider != "custom":
        row = cloud_operation(lambda: domain_resources.save_record(db, domain, payload, current_user))
        return serialize_dns_record(row)
    row = DomainDNSRecord(domain_id=domain_id, host=normalize_record_host(payload.host),
        record_type=validate_record_type(payload.record_type), record_value=validate_record_value(payload.record_value),
        ttl=validate_ttl(payload.ttl), remark=payload.remark.strip()[:255], updated_at=now_ts())
    db.add(row)
    domain.updated_at = now_ts()
    db.commit()
    return serialize_dns_record(row)


@router.put("/{domain_id}/records/{record_id}", response_model=DomainDNSRecordResponse)
def update_domain_dns_record(
    domain_id: int, record_id: int, payload: DomainDNSRecordUpdate, db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
):
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    row = db.query(DomainDNSRecord).filter(DomainDNSRecord.id == record_id, DomainDNSRecord.domain_id == domain_id).first()
    if domain is None or row is None:
        raise HTTPException(status_code=404, detail="域名或解析记录不存在")
    if domain.provider != "custom":
        row = cloud_operation(lambda: domain_resources.save_record(db, domain, payload, current_user, row))
        return serialize_dns_record(row)
    row.host, row.record_type = normalize_record_host(payload.host), validate_record_type(payload.record_type)
    row.record_value, row.ttl = validate_record_value(payload.record_value), validate_ttl(payload.ttl)
    row.record_values_json = ""
    row.remark, row.updated_at = payload.remark.strip()[:255], now_ts()
    domain.updated_at = now_ts()
    db.commit()
    return serialize_dns_record(row)


@router.delete("/{domain_id}/records/{record_id}")
def delete_domain_dns_record(
    domain_id: int, record_id: int, db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.domains.manage")),
    _risk=Depends(require_risk_ack("action.assets.domains.manage", "delete")),
):
    """删除真实云端记录；保留 domains:delete 一次性风险票据守卫。"""
    domain = db.query(DomainRecord).filter(DomainRecord.id == domain_id).first()
    row = db.query(DomainDNSRecord).filter(DomainDNSRecord.id == record_id, DomainDNSRecord.domain_id == domain_id).first()
    if domain is None or row is None:
        raise HTTPException(status_code=404, detail="域名或解析记录不存在")
    if domain.provider != "custom":
        cloud_operation(lambda: domain_resources.delete_record(db, domain, row, current_user))
    else:
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
    if provider_key != "custom":
        if payload.acme_enabled and not DOMAIN_PROVIDER_REGISTRY.get(provider_key, {}).get("supports_acme"):
            raise HTTPException(status_code=400, detail="该厂商暂未接入ACME证书签发")
        expires_at = parse_expire_date(payload.expires_at) if payload.expires_at else ""
        reminder_days = int(payload.expire_reminder_days or 30)
        if not 0 <= reminder_days <= 365:
            raise HTTPException(status_code=400, detail="到期提醒天数需在0-365之间")
        row = cloud_operation(lambda: domain_resources.import_domain(
            db, provider_key, payload.account_id, domain, payload.remark, current_user,
        ))
        row.expires_at, row.expire_reminder_days = expires_at, reminder_days
        row.status = payload.status
        row.acme_enabled = "yes" if payload.acme_enabled else "no"
        db.commit()
        return serialize_domain(row, db, current_user)
    created_at = (payload.created_at or "").strip() or now_ts()
    
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
    account_id = (payload.account_id or "").strip()
    if account_id:
        resolve_account_keys(db, account_id, current_user)

    row = DomainRecord(
        domain=domain,
        dns_credential=(payload.dns_credential or "").strip() or provider_label(provider_key),
        root_domain=(payload.root_domain or "").strip().lower() or parse_root_domain(domain),
        record_type=(payload.record_type or "A").strip().upper(),
        record_value=(payload.record_value or "-").strip(),
        provider=provider_key,
        status=(payload.status or "active").strip().lower(),
        account_id=account_id,
        account_name=(payload.account_name or "").strip(),
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

    provider = normalize_provider_key(payload.provider or row.provider)
    if payload.acme_enabled and not DOMAIN_PROVIDER_REGISTRY.get(provider, {}).get("supports_acme"):
        raise HTTPException(status_code=400, detail="该厂商暂未接入ACME证书签发")

    # 所有纯字段校验先完成，避免换绑成功后再因日期格式错误留下部分更新。
    expiry = parse_expire_date(payload.expires_at) if (payload.expires_at or "").strip() else None
    if payload.account_id is not None:
        account_id = payload.account_id.strip()
        if account_id != (row.account_id or "") or provider != row.provider:
            cloud_operation(lambda: domain_resources.change_account(db, row, provider, account_id, current_user))
    elif provider != row.provider:
        raise HTTPException(status_code=400, detail="更换厂商时请选择对应的域名账户")

    row.remark = (payload.remark or "").strip()[:255]
    # 到期时间留空 = 不修改（与凭证编辑「留空不改」口径一致）；
    # 不能走 parse_expire_date("") 的 +365 天默认，否则清空日期保存会把到期时间静默推后一年
    if expiry is not None:
        row.expires_at = expiry
    row.expire_reminder_days = int(payload.expire_reminder_days or 30)
    row.status = (payload.status or "active").strip().lower()
    row.acme_enabled = "yes" if payload.acme_enabled else "no"
    row.cert_targets = (payload.cert_targets or "").strip()[:255]
    row.updated_at = now_ts()

    db.commit()
    db.refresh(row)
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
    from app.services.credential_usage import sync_credential_usage
    sync_credential_usage(db)
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
    from app.services.credential_usage import sync_credential_usage
    sync_credential_usage(db)
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

