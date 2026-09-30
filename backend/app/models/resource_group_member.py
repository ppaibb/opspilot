"""
资源分组成员表(多态归属关系)

方案 B:用一张统一的关系表记录「哪个资源属于哪个分组」,覆盖所有资源类型
(主机/虚机/集群/域名/证书…),业务表本身不需要新增 group_id 列。

- group_id:指向 resource_groups.id。
- resource_type:资源类型枚举字符串(host/vm/cluster/domain/cert)。
- resource_id:资源的业务标识(主机=instance_id,虚机=vm_id,集群=cluster_id,
  证书=cert_id,域名=domain),统一用字符串存放。
- 一个资源至多归属一个分组:(resource_type, resource_id) 唯一。
- 删除分组时连带删除其成员行(资源本身不动,安全可逆)。

主机为兼容既有前端仍同步维护 assets.group_id(双写),但成员表是新的归属事实来源。
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from app.db.database import Base

# 资源类型枚举(与前端 resourceType 约定一致)
RESOURCE_TYPE_HOST = "host"
RESOURCE_TYPE_VM = "vm"
RESOURCE_TYPE_CLUSTER = "cluster"
RESOURCE_TYPE_DOMAIN = "domain"
RESOURCE_TYPE_CERT = "cert"
RESOURCE_TYPE_APPLICATION = "application"

VALID_RESOURCE_TYPES = {
    RESOURCE_TYPE_HOST,
    RESOURCE_TYPE_VM,
    RESOURCE_TYPE_CLUSTER,
    RESOURCE_TYPE_DOMAIN,
    RESOURCE_TYPE_CERT,
    RESOURCE_TYPE_APPLICATION,
}


class ResourceGroupMember(Base):
    __tablename__ = "resource_group_members"
    __table_args__ = (
        UniqueConstraint(
            "resource_type", "resource_id", name="uq_rgm_resource"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, index=True, nullable=False, comment="所属分组ID(resource_groups.id)")
    resource_type = Column(String(20), index=True, nullable=False, comment="资源类型: host/vm/cluster/domain/cert")
    resource_id = Column(String(120), index=True, nullable=False, comment="资源业务标识(主机=instance_id,虚机=vm_id 等)")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
