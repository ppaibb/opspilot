"""SSH 主机指纹校验（TOFU + 钉扎）。

全平台 SSH 客户端统一用 PinnedHostPolicy 取代 AutoAddPolicy：
1. 首次连接：记录 host key 指纹（TOFU，Trust On First Use）并放行；
2. 再次连接：同一 (host, port) 下任一已钉扎指纹命中即放行，并刷新 last_seen_at；
3. 指纹不一致：拒绝连接并给出明确提示（疑似中间人），需人工在主机管理中
   重新确认指纹后才能恢复。

指纹数据落 asset_host_keys 表（AssetHostKey）。一个 (host, port) 可能有多条
记录（服务器同时宣告 ssh-rsa / ssh-ed25519 等多种 host key），按指纹集合判定。

用法（各 SSH 连接点替换 AutoAddPolicy 的一行）::

    client = paramiko.SSHClient()
    pin_and_connect(client, hostname=ip, port=port, db=db)  # db 可省略
    client.connect(...)
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Iterable

import paramiko
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession

from app.db.database import SessionLocal
from app.models.asset_host_key import AssetHostKey

# 指纹不一致时的统一报错文案（策略内抛 SSHException，各调用点透传给用户）。
HOST_KEY_MISMATCH_MESSAGE = (
    "主机指纹与首次记录不一致，可能存在中间人风险，已在系统拦截。"
    "如确认服务器合法，请在主机管理中重新确认指纹"
)


def decide_host_key_action(stored: set[str], presented: str) -> str:
    """纯函数三态判定：首见钉扎 / 已存放行 / 不一致拒绝。

    Args:
        stored: 该 host:port 已钉扎的指纹集合（可含多种 key type）。
        presented: 服务器本次出示的指纹。

    Returns:
        "pin_first_time"（首次见到，落库放行）/
        "accept"（命中已钉扎指纹，放行）/
        "reject"（与首次记录不一致，拦截）。
    """
    if not stored:
        return "pin_first_time"
    return "accept" if presented in stored else "reject"


def fingerprint_of(key: paramiko.PKey) -> str:
    """计算 host key 的 OpenSSH 风格 SHA256 指纹（形如 SHA256:xxxx）。

    不读 paramiko 的 ``key.fingerprint`` 属性：该属性各版本返回值不一致
    （旧版本是 MD5 摘要 bytes，新版本才是 SHA256 字符串），拼出来的指纹
    随 paramiko 版本漂移，会让钉扎库整体失配。这里显式自算：对公钥原始
    字节做 SHA256，再走 base64（去掉 padding），与 ``ssh-keygen -lf`` 输出
    逐字符一致。
    """
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def key_type_of(key: paramiko.PKey) -> str:
    """取 host key 类型名（如 ssh-ed25519 / ssh-rsa）。"""
    return str(key.get_name() or "unknown")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_pinned_fingerprints(db: SASession, hostname: str, port: int) -> set[str]:
    """加载某 host:port 已钉扎的全部指纹。"""
    rows = (
        db.query(AssetHostKey)
        .filter(AssetHostKey.host == hostname, AssetHostKey.port == int(port))
        .all()
    )
    return {row.fingerprint for row in rows}


def _upsert_host_key(
    db: SASession,
    *,
    hostname: str,
    port: int,
    key_type: str,
    fingerprint: str,
) -> None:
    """按 (host, port, key_type) 幂等落库/更新指纹（钉扎与刷新共用）。

    首连并发竞态：两个会话同时对同一 (host, port, key_type) 首次钉扎，
    后提交的一方会撞唯一约束 uq_asset_host_key_identity。捕获 IntegrityError
    后回滚重查裁决——已存在且指纹一致只是首连双写，放行并刷新时间；
    指纹不一致仍按中间人拒绝，绝不静默覆盖既有钉扎。
    """
    now_text = _now_text()
    for _attempt in range(2):
        row = (
            db.query(AssetHostKey)
            .filter(
                AssetHostKey.host == hostname,
                AssetHostKey.port == int(port),
                AssetHostKey.key_type == key_type,
            )
            .first()
        )
        if row is None:
            db.add(AssetHostKey(
                host=hostname,
                port=int(port),
                key_type=key_type,
                fingerprint=fingerprint,
                first_seen_at=now_text,
                last_seen_at=now_text,
            ))
            try:
                db.commit()
                return
            except IntegrityError:
                # 并发对手抢先提交了同一身份的钉扎行：回滚后回循环重查，
                # 由既有行的指纹决定放行还是拒绝。
                db.rollback()
                continue
        # 幂等：同一 key type 已有记录时不改动指纹（一致场景只刷时间）。
        if row.fingerprint != fingerprint:
            raise paramiko.SSHException(HOST_KEY_MISMATCH_MESSAGE)
        row.last_seen_at = now_text
        db.commit()
        return
    # 两轮都写不进去（理论不可达）：宁可断连也不能跳过指纹裁决。
    raise paramiko.SSHException("主机指纹记录写入失败（并发冲突），请重试连接")


class PinnedHostPolicy(paramiko.MissingHostKeyPolicy):
    """TOFU + 钉扎主机密钥策略。

    paramiko 未在 known_hosts 找到主机时回调 missing_host_key；本策略不加载
    任何系统 host 文件，因此每次连接都会进入该回调，由 asset_host_keys 表
    统一裁决。db 由调用方传入（复用请求会话）；缺省时内部开 SessionLocal 短
    会话，用完即关。
    """

    def __init__(self, hostname: str, port: int, db: SASession | None = None):
        self._hostname = str(hostname or "").strip()
        self._port = int(port or 22)
        self._db = db
        self._owns_db = db is None

    def missing_host_key(self, client, hostname, key) -> None:  # noqa: ANN001
        fingerprint = fingerprint_of(key)
        key_type = key_type_of(key)
        db = self._db or SessionLocal()
        try:
            stored = load_pinned_fingerprints(db, self._hostname, self._port)
            action = decide_host_key_action(stored, fingerprint)
            if action == "reject":
                raise paramiko.SSHException(HOST_KEY_MISMATCH_MESSAGE)
            # pin_first_time：首次钉扎落库；accept：命中后幂等刷新 last_seen_at。
            _upsert_host_key(
                db,
                hostname=self._hostname,
                port=self._port,
                key_type=key_type,
                fingerprint=fingerprint,
            )
        finally:
            if self._owns_db:
                db.close()


def pin_and_connect(
    client: paramiko.SSHClient,
    hostname: str,
    port: int,
    db: SASession | None = None,
) -> set[str]:
    """给 SSHClient 挂上钉扎策略（替代 AutoAddPolicy 的统一入口）。

    Args:
        client: 待配置的 paramiko.SSHClient。
        hostname: 目标主机（IP 或域名）。
        port: SSH 端口。
        db: 可选的现有会话；缺省时策略内部自开 SessionLocal 短会话。

    Returns:
        该 host:port 当前已钉扎的指纹集合（首连为空集合）。
    """
    hostname = str(hostname or "").strip()
    port = int(port or 22)
    owned_db = db is None
    session = db or SessionLocal()
    try:
        stored = load_pinned_fingerprints(session, hostname, port)
    finally:
        if owned_db:
            session.close()
    client.set_missing_host_key_policy(PinnedHostPolicy(hostname, port, db=db))
    return stored


def pin_host_keys(
    db: SASession,
    *,
    hostname: str,
    port: int,
    fingerprints: Iterable[tuple[str, str]],
) -> None:
    """人工重新确认指纹的写入口（管理端"重置钉扎"用）：覆盖式落库。

    Args:
        fingerprints: (key_type, fingerprint) 对。
    """
    for key_type, fingerprint in fingerprints:
        row = (
            db.query(AssetHostKey)
            .filter(
                AssetHostKey.host == hostname,
                AssetHostKey.port == int(port),
                AssetHostKey.key_type == key_type,
            )
            .first()
        )
        now_text = _now_text()
        if row is None:
            db.add(AssetHostKey(
                host=hostname,
                port=int(port),
                key_type=key_type,
                fingerprint=fingerprint,
                first_seen_at=now_text,
                last_seen_at=now_text,
            ))
        else:
            row.fingerprint = fingerprint
            row.last_seen_at = now_text
    db.commit()
