"""
告警根因反馈闭环表（塔斯汀模式的"三元组"：告警上下文 + AI 诊断 + 人工根因与评价）。

设计：一条记录 = 一次告警的完整处置沉淀：
- context：告警快照（名称/标签/严重度/时间），事后仍可追溯；
- ai_diagnosis：AI 当时的诊断（JSON，含 root_cause/impact/suggestions 与模式）；
- human_root_cause / human_resolution：人填的"真正发生了什么、怎么解决的"；
- ai_rating：人对 AI 的评价（accurate/partial/inaccurate/pending）——
  积累后按 alert_name 统计准确率，反哺提示词优化与场景化评估。
"""
from sqlalchemy import Column, Integer, String, Text, JSON, Index

from app.db.database import Base


class AlertRootCause(Base):
    """告警根因记录（AI 诊断 + 人工反馈三元组）。"""

    __tablename__ = "alert_root_causes"
    __table_args__ = (
        Index("idx_arc_alert_name", "alert_name"),
        Index("idx_arc_rating", "ai_rating"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # 告警定位与快照
    fingerprint = Column(String(120), default="", comment="告警 fingerprint")
    alert_name = Column(String(200), default="", comment="告警规则名，根因库检索主键")
    service = Column(String(200), default="", comment="service 标签（业务维度）")
    instance = Column(String(200), default="", comment="instance 标签")
    severity = Column(String(20), default="", comment="告警级别")
    context_snapshot = Column(JSON, comment="告警标签/注释快照")
    # AI 侧
    ai_diagnosis = Column(JSON, comment="AI 诊断 {root_cause, impact, suggestions[], mode}")
    ai_model = Column(String(100), default="", comment="诊断所用模型")
    # 人工侧
    human_root_cause = Column(Text, default="", comment="人工根因（真正发生了什么）")
    human_resolution = Column(Text, default="", comment="处理方式（怎么解决的）")
    ai_rating = Column(String(20), default="pending", comment="AI 评价: accurate/partial/inaccurate/pending")
    # 元信息
    username = Column(String(100), default="", comment="记录人")
    created_at = Column(String(50), comment="创建时间")
