"""凭证查询接口。"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections import Counter
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
from datetime import datetime, date
from pydantic import BaseModel, Field

from app.core.security import require_permissions
from app.core.rbac import list_role_templates
from app.db.database import get_db
from app.models.credential import Credential
from app.models.user import User
from app.core.security import get_current_user, require_risk_ack
from app.core.crypto import decrypt_secret, encrypt_secret
from app.services.credential_access import (
    credential_visible_to_user,
    parse_allowed_roles,
    user_can_manage_credentials,
)
from app.services.credential_usage import sync_credential_usage

router = APIRouter()

class CredentialResponse(BaseModel):
    cred_id: str
    name: str
    type: str
    scope: str
    allowed_roles: List[str] = Field(default_factory=list)
    created_at: str
    expires_at: str
    status: str
    used_by: int

    class Config:
        from_attributes = True

class CredentialCreate(BaseModel):
    name: str
    type: str
    content: str
    expires_at: str
    allowed_roles: List[str] = Field(default_factory=list)
    scope: Optional[str] = None


class CredentialUpdate(BaseModel):
    name: str
    type: str
    expires_at: str
    content: Optional[str] = None
    replace_content: bool = False
    allowed_roles: List[str] = Field(default_factory=list)
    scope: Optional[str] = None


class CredentialBatchDeleteRequest(BaseModel):
    ids: List[str]


class CredentialBatchExportRequest(BaseModel):
    ids: List[str]


class CredentialAccessRole(BaseModel):
    role: str
    title: str
    description: str


def _parse_allowed_roles(raw_value: str | None) -> List[str]:
    """解析凭证可用权限组。

    兼容策略：
    - 空字符串表示所有权限组可用；
    - 非法 JSON 直接按空处理，避免历史脏数据导致凭证列表不可用。
    """
    return parse_allowed_roles(raw_value)


def _normalize_allowed_roles(raw_roles: List[str] | None) -> List[str]:
    """校验并归一化凭证授权权限组。"""
    valid_roles = {item["role"] for item in list_role_templates()}
    normalized = list(dict.fromkeys([str(item or "").strip() for item in (raw_roles or []) if str(item or "").strip()]))
    unknown_roles = [item for item in normalized if item not in valid_roles]
    if unknown_roles:
        raise HTTPException(status_code=400, detail=f"未知权限组: {', '.join(unknown_roles)}")
    return normalized


def _dump_allowed_roles(roles: List[str] | None) -> str:
    """把权限组列表保存为 JSON 字符串；空列表表示所有权限组。"""
    normalized = _normalize_allowed_roles(roles)
    return json.dumps(normalized, ensure_ascii=False) if normalized else ""


def _can_manage_credentials(user: User) -> bool:
    """判断当前用户是否拥有凭证管理权限。"""
    return user_can_manage_credentials(user)


def _credential_visible_to_user(cred: Credential, user: User) -> bool:
    """判断凭证是否对当前用户可见/可用。"""
    return credential_visible_to_user(cred, user)


def _serialize_credential(cred: Credential) -> dict:
    """统一序列化凭证元数据，避免直接暴露密文内容。"""
    return {
        "cred_id": cred.cred_id,
        "name": cred.name,
        "type": cred.type,
        "scope": cred.scope,
        "allowed_roles": _parse_allowed_roles(getattr(cred, "allowed_roles", "") or ""),
        "created_at": cred.created_at,
        "expires_at": cred.expires_at,
        "status": cred.status,
        "used_by": cred.used_by or 0,
    }


def _safe_export_filename(name: str, cred_id: str, extension: str) -> str:
    """生成安全的导出文件名，避免特殊字符污染响应头。"""
    base = re.sub(r"[^0-9A-Za-z._-]+", "-", (name or "").strip()).strip("-")
    if not base:
        base = cred_id
    return f"{base}-{cred_id}.{extension}"


def _deduplicate_export_filename(filename: str, seen: Counter[str]) -> str:
    """避免批量导出 ZIP 内重名文件互相覆盖。"""
    seen[filename] += 1
    if seen[filename] == 1:
        return filename
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        return f"{filename}-{seen[filename]}"
    return f"{stem}-{seen[filename]}.{suffix}"


def _build_export_payload(cred: Credential) -> tuple[str, str, str]:
    """根据凭证类型生成导出内容。

    返回：
    - 文件内容
    - 媒体类型
    - 文件后缀
    """
    raw_content = decrypt_secret(cred.content or "")
    normalized_type = (cred.type or "").strip().lower()

    if normalized_type == "accesskey":
        try:
            parsed = json.loads(raw_content or "{}")
        except Exception:
            parsed = {"content": raw_content}
        export_payload = {
            "cred_id": cred.cred_id,
            "name": cred.name,
            "type": cred.type,
            "scope": cred.scope,
            "allowed_roles": _parse_allowed_roles(getattr(cred, "allowed_roles", "") or ""),
            "created_at": cred.created_at,
            "expires_at": cred.expires_at,
            "content": parsed,
        }
        return json.dumps(export_payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8", "json"

    if normalized_type == "docker config":
        return raw_content, "application/json; charset=utf-8", "json"

    if "certificate" in normalized_type:
        return raw_content, "application/x-pem-file; charset=utf-8", "pem"

    if "ssh key" in normalized_type:
        return raw_content, "application/x-pem-file; charset=utf-8", "pem"

    return raw_content, "text/plain; charset=utf-8", "txt"


def is_long_lived_key_type(credential_type: str) -> bool:
    text = (credential_type or "").strip().lower()
    return (
        text == "ssh key"
        or text == "accesskey"
        or "access key" in text
        or "ak/sk" in text
        or "云商密钥" in text
    )


def calc_credential_status(expires_at: str, credential_type: str = "") -> str:
    # 过期时间留空 = 长期有效（含 Password/Token；旧逻辑短寿命类型空串会落成 expiring，不合理）
    if not (expires_at or "").strip():
        return "active"
    # SSH Key / AccessKey 通常无硬性过期语义，默认按长期有效处理。
    if is_long_lived_key_type(credential_type):
        try:
            expire_date = datetime.strptime((expires_at or "").strip(), "%Y-%m-%d").date()
            days_left = (expire_date - date.today()).days
            if 0 <= days_left <= 30:
                return "expiring"
            return "active"
        except Exception:
            return "active"

    try:
        expire_date = datetime.strptime((expires_at or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return "expiring"
    days_left = (expire_date - date.today()).days
    if days_left < 0:
        return "expired"
    if days_left <= 30:
        return "expiring"
    return "active"


def validate_credential_input(name: str, ctype: str, expires_at: str) -> None:
    if not (name or "").strip():
        raise HTTPException(status_code=400, detail="凭证名称不能为空")
    if not (ctype or "").strip():
        raise HTTPException(status_code=400, detail="凭证类型不能为空")
    # 过期时间可留空（= 长期有效）；填了才校验格式
    if not (expires_at or "").strip():
        return
    try:
        datetime.strptime(expires_at.strip(), "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="过期时间格式错误，应为 YYYY-MM-DD")

@router.get("", response_model=List[CredentialResponse])
def get_credentials(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回保险箱所有的凭据列表"""
    sync_credential_usage(db)
    changed = False
    records = db.query(Credential).order_by(Credential.id.desc()).all()
    for cred in records:
        next_status = calc_credential_status(cred.expires_at, cred.type)
        if cred.status != next_status:
            cred.status = next_status
            changed = True
    if changed:
        db.commit()
    else:
        db.flush()
    if _can_manage_credentials(current_user):
        visible_records = records
    else:
        visible_records = [row for row in records if _credential_visible_to_user(row, current_user)]
    return [_serialize_credential(row) for row in visible_records]


@router.get("/access-options", response_model=List[CredentialAccessRole])
def get_credential_access_options(
    _=Depends(require_permissions("action.assets.credentials.manage")),
):
    """返回凭证授权可选权限组。"""
    return [
        {
            "role": item["role"],
            "title": item["title"],
            "description": item.get("description") or "",
        }
        for item in list_role_templates()
    ]

@router.post("", response_model=CredentialResponse)
def create_credential(
    cred: CredentialCreate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.credentials.manage")),
):
    """签发/入库新凭证"""
    validate_credential_input(cred.name, cred.type, cred.expires_at)
    content = (cred.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="凭证内容不能为空")
    # SSH Key 类型保存即校验内容确为私钥：此前密码误存为 SSH Key 只会在测试 SSH 时
    # 报「私钥文书解析损坏」，用户无法定位（实测踩过）
    lowered = content.lower()
    if cred.type.strip() == "SSH Key" and not ("-----begin" in lowered and "private key-----" in lowered):
        raise HTTPException(
            status_code=400,
            detail="内容不是有效的 SSH 私钥（应包含 -----BEGIN ... PRIVATE KEY----- 头尾）。"
                   "若要存密码请把类型改为「密码凭证」",
        )

    new_cred = Credential(
        cred_id=f"CRED-{str(uuid.uuid4())[:6].upper()}",
        name=cred.name.strip(),
        type=cred.type.strip(),
        content=encrypt_secret(content),
        scope=(cred.scope or "").strip(),
        allowed_roles=_dump_allowed_roles(cred.allowed_roles),
        created_at=datetime.now().strftime("%Y-%m-%d"),
        expires_at=cred.expires_at.strip(),
        status=calc_credential_status(cred.expires_at, cred.type),
        used_by=0
    )
    db.add(new_cred)
    sync_credential_usage(db, {new_cred.cred_id})
    db.commit()
    db.refresh(new_cred)
    return _serialize_credential(new_cred)


@router.put("/{cred_id}", response_model=CredentialResponse)
def update_credential(
    cred_id: str,
    payload: CredentialUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.credentials.manage")),
):
    """更新凭证元信息，并可按需替换凭证内容。"""
    validate_credential_input(payload.name, payload.type, payload.expires_at)
    cred = db.query(Credential).filter(Credential.cred_id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭证未找到")

    cred.name = payload.name.strip()
    cred.type = payload.type.strip()
    if payload.scope is not None:
        cred.scope = (payload.scope or "").strip()
    cred.allowed_roles = _dump_allowed_roles(payload.allowed_roles)
    cred.expires_at = payload.expires_at.strip()
    cred.status = calc_credential_status(payload.expires_at, payload.type)

    if payload.replace_content:
        next_content = (payload.content or "").strip()
        if not next_content:
            raise HTTPException(status_code=400, detail="已选择替换内容，但新凭证内容为空")
        cred.content = encrypt_secret(next_content)

    sync_credential_usage(db, {cred.cred_id})
    db.commit()
    db.refresh(cred)
    return _serialize_credential(cred)


@router.post("/batch-delete")
def batch_delete_credentials(
    payload: CredentialBatchDeleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.credentials.manage")),
    _risk=Depends(require_risk_ack("action.assets.credentials.manage", "delete")),
):
    """批量删除凭证（高危：凭证明文不可恢复，需二次确认）。"""
    ids = list(dict.fromkeys([item.strip() for item in payload.ids if item and item.strip()]))
    if not ids:
        return {"success_count": 0, "failed": []}

    failed: List[dict] = []
    success_count = 0
    sync_credential_usage(db)

    for cred_id in ids:
        cred = db.query(Credential).filter(Credential.cred_id == cred_id).first()
        if not cred:
            failed.append({"id": cred_id, "reason": "凭证不存在"})
            continue
        if (cred.used_by or 0) > 0:
            failed.append({"id": cred_id, "reason": f"仍被 {cred.used_by} 个资产引用"})
            continue
        db.delete(cred)
        success_count += 1

    db.commit()
    return {"success_count": success_count, "failed": failed}


@router.post("/batch-export")
def batch_export_credentials(
    payload: CredentialBatchExportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.credentials.export")),
    _risk=Depends(require_risk_ack("action.assets.credentials.export", "export")),
):
    """批量导出凭证明文，返回 ZIP 附件。

    安全边界：
    - 与单条导出使用同一导出权限和高风险确认；
    - 每条凭证仍逐一检查当前账号是否可见，避免批量接口绕过权限组；
    - ZIP 内额外写入 manifest.json，便于管理员审计本次导出范围。
    """
    ids = list(dict.fromkeys([str(item or "").strip() for item in payload.ids if str(item or "").strip()]))
    if not ids:
        raise HTTPException(status_code=400, detail="请选择需要导出的凭证")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多导出 100 项凭证")

    rows = db.query(Credential).filter(Credential.cred_id.in_(ids)).all()
    row_map = {str(row.cred_id): row for row in rows}
    exported: list[dict] = []
    failed: list[dict] = []
    filename_counter: Counter[str] = Counter()
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for cred_id in ids:
            cred = row_map.get(cred_id)
            if not cred:
                failed.append({"cred_id": cred_id, "reason": "凭证不存在"})
                continue
            if not (_can_manage_credentials(current_user) or _credential_visible_to_user(cred, current_user)):
                failed.append({"cred_id": cred_id, "reason": "当前权限组未被授权使用该凭证"})
                continue

            content, _media_type, extension = _build_export_payload(cred)
            filename = _deduplicate_export_filename(
                _safe_export_filename(cred.name, cred.cred_id, extension),
                filename_counter,
            )
            archive.writestr(filename, content)
            exported.append({
                "cred_id": cred.cred_id,
                "name": cred.name,
                "type": cred.type,
                "filename": filename,
            })

        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "exported_count": len(exported),
                    "failed_count": len(failed),
                    "exported": exported,
                    "failed": failed,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    if not exported:
        raise HTTPException(status_code=400, detail="没有可导出的凭证")

    zip_buffer.seek(0)
    filename = f"opspilot-credentials-{datetime.now().strftime('%Y%m%d%H%M%S')}.zip"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "X-OpsPilot-Credential-Exported-Count": str(len(exported)),
        "X-OpsPilot-Credential-Failed-Count": str(len(failed)),
    }
    return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers=headers)


@router.delete("/{cred_id}")
def delete_credential(
    cred_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_permissions("action.assets.credentials.manage")),
    _risk=Depends(require_risk_ack("action.assets.credentials.manage", "delete")),
):
    """销毁凭证（高危：凭证明文不可恢复，需二次确认）"""
    cred = db.query(Credential).filter(Credential.cred_id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭证未找到")
    sync_credential_usage(db, {cred_id})
    db.flush()
    if (cred.used_by or 0) > 0:
        raise HTTPException(status_code=400, detail=f"该凭证仍被 {cred.used_by} 个资产引用，无法删除")
    db.delete(cred)
    db.commit()
    return {"message": "success"}


@router.get("/{cred_id}/export")
def export_credential(
    cred_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_permissions("action.assets.credentials.export")),
    _risk=Depends(require_risk_ack("action.assets.credentials.export", "export")),
):
    """导出单条凭证的明文内容。

    安全边界：
    1. 只允许管理员主动下载；
    2. 不在列表接口回传明文；
    3. 通过附件下载触发，避免浏览器直接把敏感内容渲染在页面里。
    """
    cred = db.query(Credential).filter(Credential.cred_id == cred_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="凭证未找到")
    if not (_can_manage_credentials(current_user) or _credential_visible_to_user(cred, current_user)):
        raise HTTPException(status_code=403, detail="当前权限组未被授权使用该凭证")

    content, media_type, extension = _build_export_payload(cred)
    filename = _safe_export_filename(cred.name, cred.cred_id, extension)
    quoted_filename = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}",
        "X-OpsPilot-Credential-Id": cred.cred_id,
    }
    return Response(content=content, media_type=media_type, headers=headers)
