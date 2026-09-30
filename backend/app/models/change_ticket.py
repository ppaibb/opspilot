"""通用变更工单：把高危操作从「确认框」升级为申请-审批-执行闭环。

与数据库工单（DbTicket）同构的多级审批，但对象是任意变更：
主机删除、证书吊销、K8s drain、批量操作等。执行动作由 change_type 注册表分发。
"""

from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Integer, String, Text

from app.db.database import Base


class ChangeTicket(Base):
    __tablename__ = "change_tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_no = Column(String(40), unique=True, index=True, comment="工单号 CHG-YYYYMMDD-XXXX")
    title = Column(String(200), nullable=False, comment="变更标题")
    change_type = Column(
        String(40),
        nullable=False,
        index=True,
        comment="变更类型: host.delete/host.batch_delete/certificate.delete/k8s.drain/k8s.delete/custom",
    )
    risk_level = Column(String(20), default="high", comment="medium/high")

    resource_type = Column(String(40), default="", comment="目标资源类型")
    resource_id = Column(String(80), default="", comment="目标资源业务 ID")
    resource_name = Column(String(200), default="", comment="目标资源名（快照）")

    impact = Column(Text, default="", comment="影响面说明")
    rollback_plan = Column(Text, default="", comment="回滚方案")
    detail = Column(JSON, default=dict, comment="操作参数快照 JSON")

    status = Column(
        String(20),
        default="pending",
        index=True,
        comment="pending/approved/executing/success/failed/rejected/canceled",
    )

    # 变更窗口（可选，空=不限制）
    window_start = Column(String(30), default="", comment="窗口开始 YYYY-MM-DD HH:MM")
    window_end = Column(String(30), default="", comment="窗口结束 YYYY-MM-DD HH:MM")

    submitter = Column(String(100), default="", comment="提交人")
    reviewer = Column(String(100), default="", comment="最终审批人")
    executor = Column(String(100), default="", comment="执行人")
    review_note = Column(String(500), default="", comment="最终审批意见")

    approval_steps = Column(JSON, default=list, comment="审批链快照：[角色名, ...]")
    current_step = Column(Integer, default=1, comment="当前审批级（1 起）；链空时恒为 1")
    approval_log = Column(JSON, default=list, comment="逐级审批记录")

    exec_result = Column(JSON, default=dict, comment="执行结果 {ok, message, raw}")

    created_at = Column(DateTime, nullable=True, comment="创建时间")
    approved_at = Column(DateTime, nullable=True, comment="终审通过时间")
    started_at = Column(DateTime, nullable=True, comment="执行开始")
    finished_at = Column(DateTime, nullable=True, comment="执行结束")
