"""
证书管理模型
"""
from sqlalchemy import Column, Integer, String
from app.db.database import Base


class CertificateRecord(Base):
    """ACME 证书记录表"""
    __tablename__ = "certificates"

    id = Column(Integer, primary_key=True, index=True)
    cert_id = Column(String(50), unique=True, index=True, comment="证书业务ID")
    name = Column(String(120), default="", comment="证书名称")
    primary_domain = Column(String(255), index=True, comment="主域名")
    san_domains = Column(String(2000), default="", comment="附加域名，英文逗号分隔")
    provider = Column(String(50), default="custom", comment="DNS 提供商标识")
    account_id = Column(String(80), default="", comment="签发账户ID(凭证ID)")
    account_name = Column(String(120), default="", comment="签发账户名称")
    acme_server = Column(String(80), default="letsencrypt", comment="ACME 服务端")
    challenge_type = Column(String(30), default="dns-01", comment="挑战类型")
    key_type = Column(String(30), default="ec-256", comment="密钥类型")
    status = Column(String(30), default="pending", comment="pending/issuing/issued/renewing/failed/expiring/expired")
    auto_renew = Column(String(10), default="yes", comment="是否自动续签")
    renew_before_days = Column(Integer, default=30, comment="提前续签天数")
    not_before = Column(String(30), default="", comment="生效时间")
    not_after = Column(String(30), default="", comment="到期时间")
    fingerprint_sha256 = Column(String(128), default="", comment="证书指纹")
    cert_path = Column(String(300), default="", comment="cert.pem 文件路径")
    key_path = Column(String(300), default="", comment="privkey.pem 文件路径")
    fullchain_path = Column(String(300), default="", comment="fullchain.pem 文件路径")
    ca_path = Column(String(300), default="", comment="ca.pem 文件路径")
    last_issue_at = Column(String(30), default="", comment="最近签发时间")
    last_renew_attempt_at = Column(String(30), default="", comment="最近一次自动续签触发时间（失败重试冷却用）")
    last_error = Column(String(1000), default="", comment="最近错误")
    deploy_targets = Column(String(255), default="", comment="分发目标（预留）")
    remark = Column(String(255), default="", comment="备注")
    created_at = Column(String(30), default="", comment="创建时间")
    updated_at = Column(String(30), default="", comment="更新时间")
