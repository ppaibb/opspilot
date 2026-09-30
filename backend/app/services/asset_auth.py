"""主机资产 SSH 认证解析服务。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret
from app.models.asset import Asset
from app.models.credential import Credential
from app.models.user import User
from app.services.credential_access import ensure_credential_access


@dataclass(frozen=True)
class AssetSshAuth:
    """主机 SSH 认证材料。

    后端仅在内存中解密和传递该结构，不向前端返回密码或私钥内容。
    """

    password: str = ""
    pkey_content: str = ""
    notice: str = "未配置可用 SSH 认证"


def credential_type_supports_ssh_password(credential_type: str) -> bool:
    """判断凭证类型是否表示 SSH 密码。"""
    normalized = str(credential_type or "").strip().lower()
    return normalized in {"password", "ssh password", "ssh密码", "主机密码"} or "password" in normalized


def credential_type_supports_ssh_key(credential_type: str) -> bool:
    """判断凭证类型是否表示 SSH 私钥。"""
    normalized = str(credential_type or "").strip().lower()
    return "ssh" in normalized and "key" in normalized


def looks_like_private_key(content: str) -> bool:
    """根据 PEM 头判断内容是否像 SSH 私钥。"""
    lowered = str(content or "").strip().lower()
    return "-----begin" in lowered and "private key-----" in lowered


def resolve_credential_ssh_auth(credential: Credential | None, fallback_password: str = "") -> AssetSshAuth:
    """把凭证记录解析为 SSH 密码或 SSH 私钥。

    非 SSH 类型凭证会被忽略，避免云 AK/SK、Token、证书误参与主机登录。
    """
    password = str(fallback_password or "").strip()
    if credential is None:
        return AssetSshAuth(password=password, notice="SSH 密码" if password else "凭证不存在")

    content = decrypt_secret(str(credential.content or "")).strip()
    credential_ref = str(credential.cred_id or "").strip()
    credential_type = str(credential.type or "").strip()
    if not content:
        return AssetSshAuth(password=password, notice=f"凭证 {credential_ref} 内容为空")

    if looks_like_private_key(content) or credential_type_supports_ssh_key(credential_type):
        return AssetSshAuth(pkey_content=content, notice=f"凭证 {credential_ref}（SSH Key）")

    if credential_type_supports_ssh_password(credential_type):
        return AssetSshAuth(password=content, notice=f"凭证 {credential_ref}（Password）")

    return AssetSshAuth(password=password, notice=f"凭证 {credential_ref} 类型不适用于 SSH")


def load_credential_ssh_auth(
    db: Session,
    credential_ref: str,
    current_user: User | None = None,
    fallback_password: str = "",
) -> AssetSshAuth:
    """按凭证 ID 读取并解析 SSH 认证材料，同时执行权限组校验。"""
    credential_id = str(credential_ref or "").strip()
    if not credential_id:
        password = str(fallback_password or "").strip()
        return AssetSshAuth(password=password, notice="SSH 密码" if password else "未配置可用 SSH 认证")

    credential = db.query(Credential).filter(Credential.cred_id == credential_id).first()
    if current_user is not None:
        ensure_credential_access(
            credential,
            current_user,
            not_found_message="凭证不存在",
            denied_message="当前账号无权使用该凭证",
        )
    return resolve_credential_ssh_auth(credential, fallback_password=fallback_password)


def resolve_asset_ssh_auth(db: Session, asset: Asset, current_user: User | None = None) -> AssetSshAuth:
    """解析主机资产最终 SSH 认证材料。"""
    password = decrypt_secret(str(asset.ssh_password or "")).strip() if asset.ssh_password else ""
    credential_ref = str(asset.credential_ref or "").strip()
    if credential_ref:
        return load_credential_ssh_auth(
            db=db,
            credential_ref=credential_ref,
            current_user=current_user,
            fallback_password=password,
        )
    return AssetSshAuth(password=password, notice="SSH 密码" if password else "未配置可用 SSH 认证")
