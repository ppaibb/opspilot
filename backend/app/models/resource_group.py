"""
资源分组模型

一个分组可跨资源类型聚合(主机/虚机/集群/域名/证书等),用于按业务/项目/团队维度
统计告警与费用。资源与分组为一对多关系:每个资源至多归属一个分组(group_id 可空,
NULL 表示未分组)。删除分组时不删除资源,仅把资源的 group_id 置空。
"""
from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime

from app.db.database import Base


class ResourceGroup(Base):
    __tablename__ = "resource_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), unique=True, index=True, comment="分组名称，唯一")
    color = Column(String(20), default="#0052d9", comment="分组色值(前端标记用)")
    description = Column(String(255), default="", comment="分组说明")
    sort_order = Column(Integer, default=0, comment="排序，越小越靠前")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
