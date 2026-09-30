"""AI 平台会话持久化模型。

提供「会话保存记录 / 新增 / 删除」能力(任务2),与现有 system_configs 配置存储分离。
- AIChatSession: 一次对话会话(归属用户)。
- AIChatMessage: 会话内的单条消息(user/assistant)。
删除会话时其消息一并删除(级联由应用层显式处理,避免依赖数据库外键)。
"""
from sqlalchemy import Column, Integer, String, Text
from app.db.database import Base


class AIChatSession(Base):
    """AI 对话会话表。"""
    __tablename__ = "ai_chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(64), unique=True, index=True, comment="会话业务ID")
    user_id = Column(Integer, index=True, comment="归属用户ID")
    title = Column(String(200), default="新会话", comment="会话标题")
    agent_id = Column(String(50), default="ops", comment="所用 Agent 标识")
    message_count = Column(Integer, default=0, comment="消息条数")
    created_at = Column(String(30), default="", comment="创建时间")
    updated_at = Column(String(30), default="", comment="最后更新时间")


class AIChatMessage(Base):
    """AI 对话消息表。"""
    __tablename__ = "ai_chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(64), index=True, comment="所属会话业务ID")
    role = Column(String(20), default="user", comment="user/assistant")
    content = Column(Text, default="", comment="消息正文")
    meta_json = Column(Text, default="", comment="附加元信息(模式/技能/provider等)JSON")
    created_at = Column(String(30), default="", comment="创建时间")
