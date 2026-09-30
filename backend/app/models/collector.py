"""
Alloy Collector Fleet 数据模型。
"""
from sqlalchemy import Column, Integer, String, Text

from app.db.database import Base


class CollectorAgent(Base):
    """被 OpsPilot 纳管的 Alloy Agent。"""

    __tablename__ = "collector_agents"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String(80), unique=True, index=True, nullable=False)
    asset_id = Column(String(50), default="", index=True, nullable=False)
    hostname = Column(String(120), default="", nullable=False)
    ip = Column(String(64), default="", nullable=False)
    os_type = Column(String(30), default="linux", nullable=False)
    labels_json = Column(Text, default="{}", nullable=False)
    status = Column(String(20), default="offline", index=True, nullable=False)
    current_version_id = Column(String(50), default="", nullable=False)
    last_seen_at = Column(String(30), default="", nullable=False)
    last_error = Column(String(1000), default="", nullable=False)
    token_hash = Column(String(128), default="", nullable=False)
    created_at = Column(String(30), default="", nullable=False)
    updated_at = Column(String(30), default="", nullable=False)


class CollectorGroup(Base):
    """Agent 分组，使用标签选择器匹配 Agent。"""

    __tablename__ = "collector_groups"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(120), default="", nullable=False)
    selector_json = Column(Text, default="{}", nullable=False)
    description = Column(String(500), default="", nullable=False)
    created_at = Column(String(30), default="", nullable=False)
    updated_at = Column(String(30), default="", nullable=False)


class CollectorPolicy(Base):
    """控制台维护的结构化采集策略。"""

    __tablename__ = "collector_policies"

    id = Column(Integer, primary_key=True, index=True)
    policy_id = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(120), default="", nullable=False)
    scope_type = Column(String(20), default="global", nullable=False)
    scope_ref = Column(String(80), default="", nullable=False)
    metrics_json = Column(Text, default="{}", nullable=False)
    logs_json = Column(Text, default="{}", nullable=False)
    labels_json = Column(Text, default="{}", nullable=False)
    redaction_json = Column(Text, default="{}", nullable=False)
    enabled = Column(String(10), default="true", nullable=False)
    created_at = Column(String(30), default="", nullable=False)
    updated_at = Column(String(30), default="", nullable=False)


class CollectorConfigVersion(Base):
    """一次可发布的 Alloy 配置版本。"""

    __tablename__ = "collector_config_versions"

    id = Column(Integer, primary_key=True, index=True)
    version_id = Column(String(50), unique=True, index=True, nullable=False)
    policy_id = Column(String(50), index=True, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    config_hash = Column(String(64), default="", nullable=False)
    alloy_config = Column(Text, default="", nullable=False)
    status = Column(String(20), default="draft", index=True, nullable=False)
    created_by = Column(String(100), default="", nullable=False)
    created_at = Column(String(30), default="", nullable=False)


class CollectorRelease(Base):
    """策略版本发布记录。"""

    __tablename__ = "collector_releases"

    id = Column(Integer, primary_key=True, index=True)
    release_id = Column(String(50), unique=True, index=True, nullable=False)
    version_id = Column(String(50), index=True, nullable=False)
    target_type = Column(String(20), default="agent", nullable=False)
    target_ref = Column(String(80), default="", index=True, nullable=False)
    status = Column(String(20), default="pending", index=True, nullable=False)
    started_at = Column(String(30), default="", nullable=False)
    finished_at = Column(String(30), default="", nullable=False)
    summary_json = Column(Text, default="{}", nullable=False)


class CollectorRegistrationToken(Base):
    """一次性 Agent 注册令牌，只保存哈希。"""

    __tablename__ = "collector_registration_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(String(50), unique=True, index=True, nullable=False)
    token_hash = Column(String(128), default="", nullable=False)
    description = Column(String(255), default="", nullable=False)
    expires_at = Column(String(30), default="", nullable=False)
    used_at = Column(String(30), default="", nullable=False)
    created_by = Column(String(100), default="", nullable=False)
    created_at = Column(String(30), default="", nullable=False)
