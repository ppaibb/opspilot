"""高危操作二次确认票据模型。

一次高风险操作的生命周期：
1. 前端调 `POST /api/auth/risk-challenge` 换取一次性票据（TTL 120 秒）；
2. 真正的业务请求把票据放进 `X-Risk-Ticket` 请求头；
3. `require_risk_ack` 依赖在数据库层原子消费票据（单条 UPDATE 抢行），
   消费过 / 过期 / 动作不匹配都拒绝。

时间字段沿用项目统一的 `%Y-%m-%d %H:%M:%S` 字符串（与 format_app_datetime
一致），字符串比较即时间比较，SQLite / PostgreSQL 行为一致。
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, Integer, String

from app.db.database import Base
from app.services.application_time import now_dt


def risk_ticket_now() -> str:
    """当前时间，格式与项目统一的应用时间一致。"""
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def new_ticket_id() -> str:
    """生成票据主键：uuid4 原文（带连字符），String(36) 恰好容纳。"""
    return str(uuid.uuid4())


class RiskAckTicket(Base):
    """一次高危操作二次确认的凭据，一次性消费。"""

    __tablename__ = "risk_ack_tickets"

    id = Column(String(36), primary_key=True, default=new_ticket_id)
    user_id = Column(Integer, nullable=False, index=True, comment="签发对象用户 ID，消费时必须一致")
    permission_key = Column(String(150), nullable=False, comment="签发时绑定的权限键（短键票据存短前缀）")
    action_id = Column(String(64), nullable=False, comment="签发时绑定的动作标识")
    resource_id = Column(String(128), nullable=True, comment="可选：绑定的具体资源 ID")
    short_key = Column(Boolean, nullable=False, default=False, comment="True=前端短键签发，消费时按别名匹配")
    created_at = Column(String(30), nullable=False, default=risk_ticket_now)
    expires_at = Column(String(30), nullable=False, comment="过期时间（严格大于才有效），超过即作废")
    consumed_at = Column(String(30), nullable=True, comment="消费时间；非空表示已使用，不可重放")
