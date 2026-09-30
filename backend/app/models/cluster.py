from sqlalchemy import Column, Integer, String
from app.db.database import Base

class Cluster(Base):
    """Kubernetes 容器集群信息表"""
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, index=True)
    cluster_id = Column(String(50), unique=True, index=True)
    name = Column(String(100))
    provider = Column(String(50)) # aliyun, aws, on-premise
    version = Column(String(50)) # 比如 v1.28.2
    node_count = Column(Integer, default=0)
    pod_capacity = Column(Integer, default=0)
    pod_usage = Column(Integer, default=0)
    cpu_cores = Column(Integer, default=0)
    mem_gb = Column(Integer, default=0)
    status = Column(String(50), default="running")
    kubeconfig_ref = Column(String(50)) # 关联 credentials 表 of ID
    created_at = Column(String(50), default="")

