"""用户个人 IM 渠道 API（第一版：飞书群 @ 通知）。

【接口地图】
 个人（所有登录用户）：
   GET    /api/im/feishu/binding    读取我的绑定
   PUT    /api/im/feishu/binding    绑定/更新（手机号或邮箱 → 解析 open_id）
   DELETE /api/im/feishu/binding    解绑
   POST   /api/im/feishu/test       发一条测试 @ 消息
   GET    /api/im/users             可选通知用户列表（告警规则选人用）

 管理员（module.settings.system）：
   GET    /api/im/feishu/config     读取飞书应用配置（secret 不回明文）
   PUT    /api/im/feishu/config     保存飞书应用配置

【安全边界】
 1. open_id 只由后端解析，PUT 绑定不接受前端直填 open_id；
 2. 个人群机器人 webhook 仅允许飞书域名（open.feishu.cn），避免把用户
    输入的 URL 变成服务端任意地址请求（SSRF）；
 3. 测试/解绑等操作只作用于当前登录用户自己的绑定。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.user import User
from app.models.user_im_binding import UserImBinding
from app.services import im_feishu

router = APIRouter()

# 手机号（大陆 11 位）或邮箱
_MOBILE_RE = re.compile(r"^1\d{10}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 群自定义机器人仅允许飞书官方域名，防 SSRF
_ALLOWED_WEBHOOK_HOSTS = ("open.feishu.cn", "www.feishu.cn")
NOTIFY_MODES = ("group_at", "off")


# ── Schemas ───────────────────────────────────────────────────────────────────


class ImBindingOut(BaseModel):
    """我的 IM 绑定（open_id 脱敏）。"""

    bound: bool
    platform: str = "feishu"
    identity: str = ""
    open_id_masked: str = ""
    notify_mode: str = "group_at"
    webhook_url: str = ""
    bound_at: str = ""


class ImBindingUpdate(BaseModel):
    """绑定请求体。"""

    identity: str = Field(..., description="手机号或邮箱（须已加入企业飞书组织）")
    webhook_url: str = Field("", description="群自定义机器人地址，空则用系统默认")
    notify_mode: str = Field("group_at", description="group_at / off")


class ImTestRequest(BaseModel):
    """测试消息请求体。"""

    webhook_url: str = Field("", description="临时指定群机器人地址，空则用已绑定的")


class FeishuConfigOut(BaseModel):
    """飞书应用配置（secret 只回是否已配置）。"""

    app_id: str
    app_secret_set: bool
    default_webhook_url: str


class FeishuConfigUpdate(BaseModel):
    """飞书应用配置保存。"""

    app_id: str = ""
    app_secret: str = Field("", description="空字符串表示保持原值")
    default_webhook_url: str = ""


class ImUserItem(BaseModel):
    """可选通知用户（告警规则选人）。"""

    id: int
    username: str
    name: str
    im_bound: bool


def _mask(open_id: str) -> str:
    """open_id 脱敏：只露前 6 位。"""
    open_id = str(open_id or "")
    if len(open_id) <= 6:
        return open_id
    return f"{open_id[:6]}****"


def _validate_webhook(url: str) -> str:
    """校验个人群机器人地址：必须 https 且为飞书官方域名（防 SSRF）。"""
    url = str(url or "").strip()
    if not url:
        return ""
    if not url.startswith("https://"):
        raise HTTPException(status_code=422, detail="群机器人地址必须是 https:// 开头")
    host = url.split("//", 1)[1].split("/", 1)[0].lower()
    if not any(host == h or host.endswith(f".{h}") for h in _ALLOWED_WEBHOOK_HOSTS):
        raise HTTPException(status_code=422, detail="群机器人地址必须是飞书官方域名（open.feishu.cn）")
    return url[:500]


# ── 个人绑定 ──────────────────────────────────────────────────────────────────


@router.get("/feishu/binding", response_model=ImBindingOut)
def get_my_binding(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """读取当前用户的飞书绑定。"""
    binding = im_feishu.get_binding(db, current_user.id)
    if binding is None:
        return ImBindingOut(bound=False)
    return ImBindingOut(
        bound=True,
        platform=str(binding.platform or "feishu"),
        identity=str(binding.identity or ""),
        open_id_masked=_mask(binding.open_id),
        notify_mode=str(binding.notify_mode or "group_at"),
        webhook_url=str(binding.webhook_url or ""),
        bound_at=str(binding.bound_at or ""),
    )


@router.put("/feishu/binding", response_model=ImBindingOut)
def bind_my_feishu(
    payload: ImBindingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """绑定/更新我的飞书身份：手机号或邮箱 → 后端解析 open_id。"""
    identity = str(payload.identity or "").strip()
    if not (_MOBILE_RE.match(identity) or _EMAIL_RE.match(identity)):
        raise HTTPException(status_code=422, detail="请输入正确的手机号或邮箱")
    notify_mode = str(payload.notify_mode or "group_at").strip()
    if notify_mode not in NOTIFY_MODES:
        raise HTTPException(status_code=422, detail="通知方式仅支持 group_at / off")
    webhook_url = _validate_webhook(payload.webhook_url)

    cfg = im_feishu.read_feishu_config(db)
    if not cfg["app_id"] or not cfg["app_secret_set"]:
        raise HTTPException(status_code=422, detail="管理员尚未配置飞书应用凭证，暂时无法绑定")

    # 读明文 secret 做解析（read_feishu_config 刻意不回明文）
    secret = _read_secret(db)
    try:
        open_id = im_feishu.resolve_open_id(cfg["app_id"], secret, identity)
    except im_feishu.FeishuError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    binding = im_feishu.get_binding(db, current_user.id)
    if binding is None:
        binding = UserImBinding(
            user_id=current_user.id,
            platform="feishu",
            identity=identity,
            open_id=open_id,
            notify_mode=notify_mode,
            webhook_url=webhook_url,
            bound_at=now,
            updated_at=now,
        )
        db.add(binding)
    else:
        binding.identity = identity
        binding.open_id = open_id
        binding.notify_mode = notify_mode
        binding.webhook_url = webhook_url
        binding.updated_at = now
    db.commit()

    return ImBindingOut(
        bound=True,
        identity=identity,
        open_id_masked=_mask(open_id),
        notify_mode=notify_mode,
        webhook_url=webhook_url,
        bound_at=str(binding.bound_at or now),
    )


@router.delete("/feishu/binding")
def unbind_my_feishu(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """解除我的飞书绑定。"""
    binding = im_feishu.get_binding(db, current_user.id)
    if binding is not None:
        db.delete(binding)
        db.commit()
    return {"success": True, "message": "已解绑"}


@router.post("/feishu/test")
def test_my_feishu(
    payload: ImTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """发送一条测试 @ 消息，验证绑定与群机器人配置是否可用。"""
    binding = im_feishu.get_binding(db, current_user.id)
    if binding is None or not binding.open_id:
        raise HTTPException(status_code=422, detail="尚未绑定飞书身份")
    webhook_url = _validate_webhook(payload.webhook_url) or str(binding.webhook_url or "").strip()
    if not webhook_url:
        webhook_url = im_feishu.read_feishu_config(db)["default_webhook_url"]
    if not webhook_url:
        raise HTTPException(status_code=422, detail="未配置群机器人地址（个人或系统默认）")
    try:
        im_feishu.send_group_at(
            webhook_url,
            binding.open_id,
            "OpsPilot 通知测试",
            "这是一条测试消息。收到即表示你的飞书群 @ 通知已配置成功。",
            "/notifications",
        )
    except im_feishu.FeishuError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"success": True, "message": "测试消息已发送"}


# ── 可选通知用户（告警规则选人） ───────────────────────────────────────────────


@router.get("/users", response_model=list[ImUserItem])
def list_im_users(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """返回启用状态用户列表及飞书绑定情况，供告警规则选择通知人。"""
    users = db.query(User).filter(User.status == "active").order_by(User.id).all()
    bound_ids = {
        row.user_id
        for row in db.query(UserImBinding).filter(UserImBinding.platform == "feishu").all()
        if str(row.notify_mode or "") == "group_at" and str(row.open_id or "")
    }
    return [
        ImUserItem(
            id=u.id,
            username=str(u.username or ""),
            name=str(u.name or u.username or ""),
            im_bound=u.id in bound_ids,
        )
        for u in users
    ]


# ── 管理员：飞书应用配置 ───────────────────────────────────────────────────────


@router.get(
    "/feishu/config",
    response_model=FeishuConfigOut,
    dependencies=[Depends(require_permissions("module.settings.system"))],
)
def get_feishu_config(db: Session = Depends(get_db)):
    """读取飞书应用配置（App Secret 只回是否已配置）。"""
    cfg = im_feishu.read_feishu_config(db)
    return FeishuConfigOut(**cfg)


@router.put(
    "/feishu/config",
    response_model=FeishuConfigOut,
    dependencies=[Depends(require_permissions("module.settings.system"))],
)
def save_feishu_config(payload: FeishuConfigUpdate, db: Session = Depends(get_db)):
    """保存飞书应用配置与默认群机器人。"""
    default_webhook = _validate_webhook(payload.default_webhook_url)
    app_id = str(payload.app_id or "").strip()[:80]
    if not app_id and not _read_secret(db):
        raise HTTPException(status_code=422, detail="App ID 不能为空")
    cfg = im_feishu.save_feishu_config(
        db,
        app_id=app_id or _read_app_id(db),
        app_secret=payload.app_secret,
        default_webhook_url=default_webhook,
    )
    return FeishuConfigOut(**cfg)


# ── 内部工具 ──────────────────────────────────────────────────────────────────


def _read_secret(db: Session) -> str:
    """读取飞书 App Secret 明文（仅服务端内部使用）。"""
    import json

    from app.models.system_config import SystemConfig

    row = db.query(SystemConfig).filter(SystemConfig.config_key == im_feishu.IM_FEISHU_CONFIG_KEY).first()
    if row is None:
        return ""
    try:
        payload = json.loads(row.config_value or "{}")
    except Exception:  # noqa: BLE001
        return ""
    return str(payload.get("app_secret") or "").strip() if isinstance(payload, dict) else ""


def _read_app_id(db: Session) -> str:
    """读取已保存的 App ID（保存时空 app_id 表示保持原值）。"""
    return im_feishu.read_feishu_config(db)["app_id"]
