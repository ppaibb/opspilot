"""
资产管理模型
"""
from sqlalchemy import Column, Integer, String, Float
from app.db.database import Base

class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(String(50), unique=True, index=True)
    name = Column(String(100))
    ip = Column(String(50))
    hostname = Column(String(120), default="", comment="真实主机名（SSH 采集，hostname 命令返回值）")
    private_ip = Column(String(50), nullable=True, comment="内网IP地址")
    status = Column(String(100), comment="状态标签，英文逗号分隔")
    type = Column(String(50), comment="healthy/critical/pending 等健康状态")
    cpu = Column(Float, default=0.0)
    uptime = Column(String(50))
    provider = Column(String(50), default="on-premise", comment="云厂商标签: aliyun/aws/tencent/on-premise")
    region = Column(String(50), default="local", comment="地域如 cn-hangzhou")
    cloud_resource_type = Column(String(50), default="", comment="云资源类型: ECS/SWAS/CVM/Lighthouse/EC2/Lightsail")
    remark = Column(String(255), default="", comment="主机备注")
    
    # [Phase 2.3] 主机深潜及认证绑定字段
    os = Column(String(50), default="Linux", comment="操作系统类型")
    ssh_port = Column(Integer, default=22, comment="SSH 触达端口号")
    ssh_user = Column(String(50), default="root", comment="SSH 登录用户名")
    ssh_password = Column(String(200), nullable=True, comment="主机独立密码（如不使用统管凭证）")
    credential_ref = Column(String(50), nullable=True, comment="绑定的凭证ID(credentials)")
    cloud_credential_ref = Column(String(50), nullable=True, comment="导入该主机的云账号凭证ID(AK/SK)")
    cpu_cores = Column(Float, default=0.0, comment="CPU 核数（配置）")
    memory_gb = Column(Float, default=0.0, comment="内存容量 GB（配置）")
    system_disk_gb = Column(Float, default=0.0, comment="系统盘容量 GB（配置）")
    disks_json = Column(String(2000), default="[]", comment="磁盘明细JSON（系统盘+数据盘），仅保存盘名和容量")
    config_task_status = Column(String(20), default="idle", comment="配置采集任务状态: idle/collecting/success/failed")
    config_task_updated_at = Column(String(30), default="", comment="配置采集任务最近更新时间")
    config_task_error = Column(String(255), default="", comment="配置采集失败原因")
    health_last_checked_at = Column(String(30), default="", comment="巡检最近检查时间")
    group_id = Column(Integer, nullable=True, index=True, comment="所属资源分组ID(resource_groups.id)，NULL 表示未分组")

class DomainRecord(Base):
    """域名解析管理表"""
    __tablename__ = "domain_records"

    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String(255), unique=True, index=True, comment="完整域名，如 api.ops.com")
    root_domain = Column(String(255), index=True, comment="主域，如 ops.com")
    record_type = Column(String(20), comment="解析类型: A, CNAME, TXT等")
    record_value = Column(String(255), comment="解析目标值")
    dns_credential = Column(String(120), default="", comment="DNS 凭证/授权标识展示名")
    provider = Column(String(50), comment="托管云商，如 aliyun")
    status = Column(String(50), default="active")
    account_id = Column(String(80), default="", comment="域名账户ID（通常映射凭证ID）")
    account_name = Column(String(120), default="", comment="域名账户展示名")
    created_at = Column(String(30), default="", comment="加入系统时间")
    updated_at = Column(String(30), default="", comment="最近修改时间")
    expires_at = Column(String(20), default="", comment="到期时间 YYYY-MM-DD")
    expire_reminder_days = Column(Integer, default=30, comment="到期前提醒天数")
    remark = Column(String(255), default="", comment="备注")
    acme_enabled = Column(String(10), default="no", comment="是否启用 ACME 自动证书")
    cert_targets = Column(String(255), default="", comment="证书分发目标（预留）")


class DomainDNSRecord(Base):
    """域名解析记录表（每条 DNS 记录一行）"""
    __tablename__ = "domain_dns_records"

    id = Column(Integer, primary_key=True, index=True)
    domain_id = Column(Integer, index=True, comment="所属域名ID(domain_records.id)")
    host = Column(String(120), default="@", comment="主机记录，如 @ / www / api")
    record_type = Column(String(20), default="A", comment="解析类型: A/CNAME/TXT/AAAA/MX/NS")
    record_value = Column(String(255), default="", comment="解析值")
    ttl = Column(Integer, default=600, comment="TTL 秒")
    remark = Column(String(255), default="", comment="解析记录备注")
    updated_at = Column(String(30), default="", comment="最近更新时间")
