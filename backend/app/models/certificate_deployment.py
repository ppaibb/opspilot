"""
证书部署历史模型

一次 POST /certificates/{id}/deploy 对应一条记录：接口校验通过后先建
running 记录再把执行交给后台任务，任务结束按逐目标结果写
success / partial / failed。旧版本部署过的只有 issue.log 里的一两行，
无法回补成本表记录。
"""
from sqlalchemy import Column, Integer, String, Text
from app.db.database import Base


class CertificateDeployment(Base):
    """证书部署历史表"""
    __tablename__ = "certificate_deployments"

    id = Column(Integer, primary_key=True, index=True)
    deploy_id = Column(String(50), unique=True, index=True, comment="部署任务业务ID")
    cert_id = Column(String(50), index=True, comment="证书业务ID")
    target_type = Column(String(20), default="host", comment="host/cloud/k8s")
    targets = Column(String(500), default="", comment="部署目标摘要")
    status = Column(String(20), default="running", comment="running/success/partial/failed")
    results_json = Column(Text, default="[]", comment="每台/每个目标的 success + message，JSON 数组")
    triggered_by = Column(String(120), default="", comment="触发人用户名")
    started_at = Column(String(30), default="", comment="开始时间")
    finished_at = Column(String(30), default="", comment="结束时间")
