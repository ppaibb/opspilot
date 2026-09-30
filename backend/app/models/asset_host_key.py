"""主机 SSH 指纹钉扎表（TOFU：首次连接信任并固定）。
遵循 Google Python Style Guide
"""
from sqlalchemy import Column, Integer, String, UniqueConstraint

from app.db.database import Base


class AssetHostKey(Base):
    """记录主机 host:port 的 SSH host key 指纹。

    TOFU（Trust On First Use）策略的数据面：
    - 首次连接：把服务器 host key 的 SHA256 指纹落库（钉扎）；
    - 再次连接：指纹一致放行并刷新 last_seen_at；
    - 指纹不一致：拒绝连接（疑似中间人），需人工重新确认。

    一台服务器可能同时宣告多种类型的 host key（ssh-rsa / ssh-ed25519 /
    ecdsa-sha2-nistp256），以 (host, port, key_type) 唯一；判定时对
    同一 (host, port) 下任一指纹命中即放行。
    """

    __tablename__ = "asset_host_keys"

    id = Column(Integer, primary_key=True, index=True)
    host = Column(String(255), nullable=False, index=True, comment="主机地址（IP 或域名）")
    port = Column(Integer, nullable=False, default=22, comment="SSH 端口")
    key_type = Column(String(32), nullable=False, comment="主机密钥类型（如 ssh-ed25519）")
    fingerprint = Column(String(128), nullable=False, comment="SHA256 指纹（OpenSSH 风格，SHA256:...）")
    first_seen_at = Column(String(30), default="", comment="首次记录时间")
    last_seen_at = Column(String(30), default="", comment="最近一次校验通过时间")

    __table_args__ = (
        UniqueConstraint("host", "port", "key_type", name="uq_asset_host_key_identity"),
    )
