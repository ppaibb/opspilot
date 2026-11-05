"""域名资源编排：授权、真实云端身份、完整缓存和 DNS 写操作。

所有远程读取完成后再更新缓存；云写操作成功后才更新本地。
远程请求超时可能处于“云端已成功、本地未确认”状态，此时提示用户同步核对，
不自动重试写操作，避免重复创建或误删。
"""

from datetime import datetime
import json
import re

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.models.asset import DomainDNSRecord, DomainRecord
from app.models.credential import Credential
from app.services.credential_access import ensure_credential_access
from app.services.credential_usage import sync_credential_usage
from app.services.dns_providers.base import DNSProviderError, DNSRecord, DNSZone, canonical_domain, writable
from app.services.dns_providers.registry import get_provider


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_adapter(db: Session, provider: str, credential_id: str, user):
    """先授权再解密；provider 是调用者明确选择的厂商，不从凭证名字猜测。"""
    spec = get_provider(provider)
    credential = ensure_credential_access(db.query(Credential).filter(Credential.cred_id == credential_id).first(), user)
    if credential.status == "expired":
        raise HTTPException(status_code=400, detail="所选 DNS 凭证已过期，请更新后重试")
    ctype = re.sub(r"[\s_/-]", "", credential.type or "").lower()
    allowed = {re.sub(r"[\s_/-]", "", item).lower() for item in spec.credential_types}
    if ctype not in allowed and not (ctype == "aksk" and "accesskey" in allowed):
        raise HTTPException(status_code=400, detail=f"{spec.label} 需要 {' / '.join(spec.credential_types)} 类型凭证")
    raw = decrypt_secret(credential.content or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="DNS 凭证内容为空或无法解密")
    if ctype == "token":
        return spec.factory(raw), credential
    # 兼容已有 AK/SK 序列化格式，不向页面回传解密结果。
    from app.api.domains import parse_access_key_content

    access_key, secret_key = parse_access_key_content(raw)
    if not access_key or not secret_key:
        raise HTTPException(status_code=400, detail="凭证缺少有效 AK/SK，请在凭证管理重新保存")
    return spec.factory(access_key, secret_key), credential


def check_domain_access(db: Session, domain: DomainRecord, user) -> None:
    """即使返回缓存也必须检查凭证权限，避免同步节流绕过授权。"""
    if domain.provider == "custom" and not domain.account_id:
        return
    credential = ensure_credential_access(db.query(Credential).filter(Credential.cred_id == domain.account_id).first(), user)
    if credential.status == "expired":
        raise HTTPException(status_code=400, detail="所选 DNS 凭证已过期，请更新后重试")


def record_values(row: DomainDNSRecord) -> list[str]:
    if row.record_values_json:
        try:
            values = json.loads(row.record_values_json)
            if isinstance(values, list) and all(isinstance(value, str) for value in values):
                return values
        except (ValueError, TypeError):
            pass
    return [row.record_value or ""]


def serialize_record(row: DomainDNSRecord) -> dict:
    values = record_values(row)
    return {
        "id": row.id, "domain_id": row.domain_id, "host": row.host or "@",
        "record_type": row.record_type, "record_value": "\n".join(values), "record_values": values,
        "ttl": row.ttl, "remark": row.remark or "", "updated_at": row.updated_at or "",
        "provider_record_id": row.provider_record_id or "", "proxied": row.proxied,
        "priority": row.priority, "read_only": bool(row.read_only),
    }


def _store_record(row: DomainDNSRecord, record: DNSRecord) -> None:
    row.provider_record_id = record.id
    row.host, row.record_type = record.host, record.record_type
    row.record_values_json = json.dumps(record.values, ensure_ascii=False)
    # 历史单值列容量仅255字符；完整值只读新Text列，不能截断真正的TXT内容。
    row.record_value = (record.values[0] if record.values else "")[:255]
    row.ttl, row.remark = record.ttl, record.remark[:255]
    row.proxied, row.priority, row.read_only = record.proxied, record.priority, record.read_only
    row.updated_at = now_ts()


def _replace_records(db: Session, domain: DomainRecord, records: list[DNSRecord]) -> None:
    """完整快照校验后按远程ID合并；不依赖可能变化的记录值当主键。"""
    identifiers = [record.id for record in records]
    if any(not value for value in identifiers) or len(identifiers) != len(set(identifiers)):
        raise DNSProviderError("云端记录列表包含重复或空 ID，已停止同步")
    previous = db.query(DomainDNSRecord).filter(DomainDNSRecord.domain_id == domain.id).all()
    by_id = {row.provider_record_id: row for row in previous if row.provider_record_id}
    kept = set()
    for record in records:
        row = by_id.get(record.id)
        if row is None:
            # 仅用于首次接管旧单值缓存；之后全部依赖云端ID。
            row = next((item for item in previous if not item.provider_record_id and item.id not in kept
                        and (item.host, item.record_type, record_values(item)) == (record.host, record.record_type, record.values)), None)
        if row is None:
            row = DomainDNSRecord(domain_id=domain.id)
            db.add(row)
        else:
            kept.add(row.id)
        _store_record(row, record)
    for row in previous:
        if row.id not in kept:
            db.delete(row)


def import_domain(db: Session, provider: str, credential_id: str, name: str, remark: str, user) -> DomainRecord:
    adapter, credential = load_adapter(db, provider, credential_id, user)
    name = canonical_domain(name)
    if db.query(DomainRecord).filter(DomainRecord.domain == name).first():
        raise HTTPException(status_code=409, detail="该域名已纳管")
    zone = adapter.get_zone(name)
    records = adapter.list_records(zone)
    # 此时尚未写库，任意云端权限/分页错误均不会创建虚假的纳管记录。
    domain = DomainRecord(domain=name, root_domain=zone.name, provider=provider, provider_zone_id=zone.id,
        record_type="A", record_value="-", status="active", account_id=credential_id,
        account_name=credential.name, dns_credential=credential.name, created_at=now_ts(), updated_at=now_ts(),
        expires_at="", expire_reminder_days=30, remark=remark.strip()[:255], acme_enabled="no", cert_targets="")
    try:
        db.add(domain)
        db.flush()
        _replace_records(db, domain, records)
        sync_credential_usage(db)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该域名已被其他请求纳管，请刷新列表") from None
    except Exception:
        db.rollback()
        raise
    return domain


def sync_records(db: Session, domain: DomainRecord, user) -> int:
    adapter, _ = load_adapter(db, domain.provider, domain.account_id, user)
    zone = adapter.get_zone(domain.domain, domain.provider_zone_id or "")
    records = adapter.list_records(zone)
    try:
        _replace_records(db, domain, records)
        domain.provider_zone_id, domain.updated_at = zone.id, now_ts()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(records)


def _record_input(payload, provider: str) -> DNSRecord:
    spec = get_provider(provider)
    host = (payload.host or "@").strip().lower()
    if host != "@" and (len(host) > 120 or any(not re.fullmatch(r"[a-z0-9_-]{1,63}|\*", label) for label in host.split("."))):
        raise HTTPException(status_code=400, detail="主机记录格式非法，请填写相对记录名")
    record_type = payload.record_type.strip().upper()
    from app.services.dns_providers.base import RECORD_TYPES

    if record_type not in RECORD_TYPES:
        raise HTTPException(status_code=400, detail="暂不支持编辑该解析记录类型")
    if host == "@" and record_type == "NS":
        raise HTTPException(status_code=400, detail="根域名 NS 记录请在云控制台管理")
    values = payload.record_values if payload.record_values is not None else [payload.record_value]
    if not values or len(values) > 100 or any(not value.strip() or len(value) > 16384 for value in values):
        raise HTTPException(status_code=400, detail="解析值不能为空，每个值最多16384字符，每组最多100个值")
    if not spec.supports_multi_values and len(values) != 1:
        raise HTTPException(status_code=400, detail="该厂商每条记录只接受一个值")
    ttl = payload.ttl
    if not ((spec.supports_proxy and ttl == 1) or 60 <= ttl <= 86400):
        raise HTTPException(status_code=400, detail="TTL须为60至86400秒；Cloudflare可用1表示自动")
    if payload.priority is not None and not 0 <= payload.priority <= 65535:
        raise HTTPException(status_code=400, detail="MX优先级须为0至65535")
    return DNSRecord("", host, record_type, values, ttl, (payload.remark or "").strip()[:255],
                     payload.proxied if spec.supports_proxy else None, payload.priority)


def _remote_id(adapter, zone: DNSZone, row: DomainDNSRecord) -> str:
    if row.provider_record_id:
        return row.provider_record_id
    # 历史缓存缺少ID时必须从云端精确找回；找不到不允许只修改本地假装成功。
    matches = [record.id for record in adapter.list_records(zone) if
               (record.host, record.record_type, record.values) == (row.host, row.record_type, record_values(row))]
    if len(matches) != 1:
        raise DNSProviderError("无法唯一定位云端解析记录，请先同步后重试", 409)
    return matches[0]


def save_record(db: Session, domain: DomainRecord, payload, user, row: DomainDNSRecord | None = None) -> DomainDNSRecord:
    record = _record_input(payload, domain.provider)
    adapter, _ = load_adapter(db, domain.provider, domain.account_id, user)
    zone = adapter.get_zone(domain.domain, domain.provider_zone_id or "")
    if row is None:
        remote = adapter.create_record(zone, record)
        row = DomainDNSRecord(domain_id=domain.id)
    else:
        writable(DNSRecord(row.provider_record_id or "", row.host, row.record_type, record_values(row), read_only=bool(row.read_only)))
        remote = adapter.update_record(zone, _remote_id(adapter, zone, row), record)
    if not remote.id:
        raise DNSProviderError("云端未返回记录 ID，请同步核对")
    try:
        db.add(row)
        _store_record(row, remote)
        domain.provider_zone_id, domain.updated_at = zone.id, now_ts()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return row


def delete_record(db: Session, domain: DomainRecord, row: DomainDNSRecord, user) -> None:
    adapter, _ = load_adapter(db, domain.provider, domain.account_id, user)
    writable(DNSRecord(row.provider_record_id or "", row.host, row.record_type, record_values(row), read_only=bool(row.read_only)))
    zone = adapter.get_zone(domain.domain, domain.provider_zone_id or "")
    adapter.delete_record(zone, _remote_id(adapter, zone, row))
    try:
        db.delete(row)
        domain.updated_at = now_ts()
        db.commit()
    except Exception:
        db.rollback()
        raise
