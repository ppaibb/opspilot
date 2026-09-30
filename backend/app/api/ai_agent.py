"""AI 平台二期接口。

架构说明：
1. 二期在一期底座上扩展多 Provider（Gemini/OpenAI 兼容/Anthropic）与多 Agent；
2. 所有配置均复用 `system_configs` 键值存储，避免当前阶段过早引入复杂新表；
3. Agent 只允许读取资产、监控、证书、域名、虚机与审计摘要，不开放写操作。

安全审计：
1. 模型密钥只在后端加密存储，不向前端回传明文；
2. 传给模型的上下文经过结构化裁剪，不包含凭证内容、SSH 私钥、Token Secret 等敏感字段；
3. 当前不允许 Agent 自动执行生产操作，编排能力仅开放执行前置预检；
4. 所有写入接口都受 RBAC 动作级权限保护，Ops Agent 查询接口也受模块级权限保护。
"""
from __future__ import annotations

import asyncio
import os
import platform
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import requests
from sqlalchemy.orm import Session

from app.api.monitoring import get_monitoring_dashboard
from app.api.monitoring.constants import DEFAULT_STEP_SECONDS
from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.security import get_current_user, require_permissions
from app.db.database import get_db
from app.models.ai_chat import AIChatMessage, AIChatSession
from app.models.asset import Asset, DomainRecord
from app.models.audit import AuditLog
from app.models.certificate import CertificateRecord
from app.models.credential import Credential
from app.models.system_config import SystemConfig
from app.models.user import User
from app.models.vm import VirtualMachine

router = APIRouter()

AI_LLM_CONFIG_KEY = "ai_llm_config"
AI_SKILL_CONFIG_KEY = "ai_skill_registry"
AI_ORCHESTRATION_POLICY_KEY = "ai_orchestration_policy"
SUPPORTED_LLM_PROVIDERS = {"gemini", "openai_compatible", "anthropic"}
DEFAULT_LLM_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_OPENAI_COMPAT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_COMPAT_MODEL = "gpt-5.4-mini"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
DEFAULT_SYSTEM_PROMPT = (
    "你是 OpsPilot 的只读运维分析 Agent。"
    "你只能基于平台提供的结构化上下文给出结论与建议，"
    "不得编造不存在的监控数据，不得输出敏感密钥，不得建议直接执行高危写操作。"
)
# 终端 AI 命令助手：把自然语言需求翻译成可执行命令，不注入任何资产上下文。
TERMINAL_COMMAND_SYSTEM_PROMPT = (
    "你是一名资深 Linux/Unix 运维命令行专家。"
    "用户会用自然语言描述运维需求，你的唯一任务是输出对应的 shell 命令。"
    "严格遵守以下规则：\n"
    "1. 只输出命令本身，最多 3 行；不要输出任何开场白、解释、总结或 Markdown 标题；\n"
    "2. 如需说明，仅允许在命令上方用以 # 开头的单行简短注释，且不超过一行；\n"
    "3. 不要用 ``` 代码块包裹，直接输出纯文本命令；\n"
    "4. 优先使用只读、非破坏性命令；若需求本身涉及高危写操作（删除、格式化、kill 等），"
    "在命令前用注释标注“# ⚠ 高危操作，请人工确认”；\n"
    "5. 无法理解需求时，只输出一行注释说明缺少什么信息。"
)
MAX_CONTEXT_ITEMS = 5
EXPIRING_SOON_DAYS = 30

DEFAULT_SKILL_CATALOG: list[dict[str, Any]] = [
    {
        "key": "skill.assets.overview",
        "title": "资产总览",
        "description": "汇总主机在线、待验证、异常分布，并列出重点异常主机。",
        "category": "资产",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["当前主机整体健康度如何？", "有哪些异常主机需要优先处理？"],
    },
    {
        "key": "skill.vms.overview",
        "title": "虚机总览",
        "description": "汇总虚机运行状态、节点分布与停机情况。",
        "category": "资产",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["PVE 虚机运行情况怎么样？", "有哪些虚机当前处于停止状态？"],
    },
    {
        "key": "skill.monitoring.overview",
        "title": "监控概览",
        "description": "读取采集目标、在线率、最近趋势指标等只读监控摘要。",
        "category": "监控",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["当前监控采集是否正常？", "CPU 和内存平均趋势如何？"],
    },
    {
        "key": "skill.alerts.active",
        "title": "活动告警",
        "description": "读取当前活动告警并提炼高风险告警摘要。",
        "category": "监控",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["现在有哪些活动告警？", "哪个告警最值得优先处理？"],
    },
    {
        "key": "skill.domains.overview",
        "title": "域名总览",
        "description": "读取域名数量、托管分布和到期提醒情况。",
        "category": "域名",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["域名资源情况如何？", "有没有快到期的域名？"],
    },
    {
        "key": "skill.certificates.overview",
        "title": "证书总览",
        "description": "读取证书签发状态、即将到期证书与失败记录。",
        "category": "证书",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["有没有快到期的证书？", "最近哪些证书签发失败了？"],
    },
    {
        "key": "skill.audit.recent",
        "title": "最近审计",
        "description": "读取最近平台关键变更记录，辅助定位最近操作。",
        "category": "审计",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["最近谁改了系统配置？", "最近平台发生了哪些关键操作？"],
    },
    {
        "key": "skill.security.attack_surface",
        "title": "攻击面概览",
        "description": "汇总活动告警、异常主机、过期证书与过期凭证等风险暴露。",
        "category": "安全",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["当前攻击面风险点有哪些？", "最需要优先处理的安全问题是什么？"],
    },
    {
        "key": "skill.security.compliance_baseline",
        "title": "基线合规扫描",
        "description": "检查 root 登录占比、过期凭证与关键审计事件，形成合规摘要。",
        "category": "安全",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["当前合规基线情况如何？", "哪些配置偏离了安全基线？"],
    },
    {
        "key": "skill.system.overview",
        "title": "系统信息总览",
        "description": "读取当前 OpsPilot 宿主机的 CPU、内存状态与操作系统信息。",
        "category": "系统",
        "risk_level": "low",
        "enabled": True,
        "sample_queries": ["当前系统负载怎么样？", "OpsPilot 服务器的 OS 版本是什么？"],
    },
]

SKILL_FOCUS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "skill.assets.overview": ("主机", "资产", "服务器", "ssh", "巡检", "在线", "离线"),
    "skill.vms.overview": ("虚机", "虚拟机", "pve", "模板", "宿主机", "节点"),
    "skill.monitoring.overview": ("监控", "cpu", "内存", "网络", "趋势", "采集目标"),
    "skill.alerts.active": ("告警", "报警", "firing", "resolved", "异常"),
    "skill.domains.overview": ("域名", "dns", "解析", "root domain"),
    "skill.certificates.overview": ("证书", "ssl", "tls", "acme", "续签", "到期"),
    "skill.audit.recent": ("审计", "谁", "改了", "变更", "操作记录"),
    "skill.security.attack_surface": ("安全", "风险", "攻击面", "漏洞", "暴露", "security"),
    "skill.security.compliance_baseline": ("合规", "基线", "审计", "root", "凭证过期", "compliance"),
    "skill.system.overview": ("系统", "负载", "cpu", "内存", "资源使用", "os"),
    "skill.k8s.pods": ("k8s", "pod", "node", "命名空间", "容器"),
    "skill.dba.slow_queries": ("数据库", "mysql", "慢查", "sql", "慢查询", "死锁"),
}

AGENT_SKILL_SCOPES: dict[str, dict[str, Any]] = {
    "ops": {
        "title": "运维分析 Agent",
        "skills": {
            "skill.assets.overview",
            "skill.vms.overview",
            "skill.monitoring.overview",
            "skill.alerts.active",
            "skill.domains.overview",
            "skill.certificates.overview",
            "skill.audit.recent",
            "skill.security.attack_surface",
            "skill.security.compliance_baseline",
            "skill.system.overview",
        },
    },
    "k8s": {
        "title": "K8s 诊断助手",
        "skills": {
            "skill.k8s.pods",
        },
    },
    "dba": {
        "title": "DBA 慢查助手",
        "skills": {
            "skill.dba.slow_queries",
        },
    },
}

DEFAULT_ORCHESTRATION_POLICY: dict[str, Any] = {
    "approval_enabled": True,
    "budget_guard_enabled": True,
    "max_budget_cost_index": 1200.0,
    "timeout_seconds": 90,
    "max_steps": 6,
    "required_approvers": 1,
    "ticket_required": True,
    "high_risk_actions": [
        "terminal.exec",
        "pve.power",
        "cert.revoke",
        "monitoring.rule.write",
    ],
}


class LLMConfigItemResponse(BaseModel):
    """单条 LLM 配置读取响应（不含明文密钥）。"""

    id: str
    name: str
    provider: str
    base_url: str
    model: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: int
    system_prompt: str
    has_api_key: bool
    using_env_fallback: bool
    is_active: bool
    created_at: str
    updated_at: str


class LLMConfigListResponse(BaseModel):
    """LLM 配置列表响应。"""

    items: list[LLMConfigItemResponse]
    active_id: str


class LLMConfigSaveRequest(BaseModel):
    """LLM 配置新增/编辑请求。"""

    name: str = Field(default="", max_length=60)
    provider: str = "gemini"
    base_url: str = ""
    model: str = DEFAULT_LLM_MODEL
    api_key: str = ""
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_output_tokens: int = Field(default=1200, ge=128, le=8192)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    activate: bool = False


class LLMConfigTestRequest(BaseModel):
    """LLM 连通性检测请求。"""

    provider: str = "gemini"
    base_url: str = ""
    model: str = DEFAULT_LLM_MODEL
    api_key: str = ""
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_output_tokens: int = Field(default=1200, ge=128, le=8192)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


class LLMConfigTestResponse(BaseModel):
    """LLM 连通性检测响应。"""

    ok: bool
    provider: str
    base_url: str
    model: str
    latency_ms: int
    detail: str
    checked_at: str


class OrchestrationPolicyResponse(BaseModel):
    """编排前置策略响应。"""

    approval_enabled: bool
    budget_guard_enabled: bool
    max_budget_cost_index: float
    timeout_seconds: int
    max_steps: int
    required_approvers: int
    ticket_required: bool
    high_risk_actions: list[str]
    updated_at: str


class OrchestrationPolicyUpdate(BaseModel):
    """编排前置策略保存请求。"""

    approval_enabled: bool = True
    budget_guard_enabled: bool = True
    max_budget_cost_index: float = Field(default=1200.0, ge=100.0, le=100000.0)
    timeout_seconds: int = Field(default=90, ge=15, le=3600)
    max_steps: int = Field(default=6, ge=1, le=64)
    required_approvers: int = Field(default=1, ge=1, le=5)
    ticket_required: bool = True


class OrchestrationPreviewRequest(BaseModel):
    """编排执行前置预检请求。"""

    operation_name: str = Field(..., min_length=1, max_length=120)
    estimated_cost_index: float = Field(default=0.0, ge=0.0, le=100000.0)
    expected_steps: int = Field(default=1, ge=1, le=128)
    expected_duration_seconds: int = Field(default=30, ge=1, le=7200)
    action_keys: list[str] = Field(default_factory=list)
    has_ticket: bool = False
    requester: str = ""


class OrchestrationPreviewResponse(BaseModel):
    """编排执行前置预检响应。"""

    allowed: bool
    requires_approval: bool
    reasons: list[str]
    matched_high_risk_actions: list[str]
    policy: OrchestrationPolicyResponse


class SkillRegistryItem(BaseModel):
    """Skill Registry 条目。"""

    key: str
    title: str
    description: str
    category: str
    risk_level: str
    enabled: bool
    sample_queries: list[str]


class SkillRegistryResponse(BaseModel):
    """Skill Registry 读取响应。"""

    skills: list[SkillRegistryItem]
    updated_at: str


class OpsAgentRuntimeResponse(BaseModel):
    """Ops Agent 运行态信息。"""

    llm_enabled: bool
    llm_ready: bool
    provider: str
    model: str
    using_env_fallback: bool
    notice: str
    enabled_skills: list[SkillRegistryItem]
    quick_questions: list[str]


class SkillRegistryUpdateItem(BaseModel):
    """Skill Registry 更新条目。"""

    key: str
    enabled: bool


class SkillRegistryUpdate(BaseModel):
    """Skill Registry 更新请求。"""

    skills: list[SkillRegistryUpdateItem]


class OpsAgentQueryRequest(BaseModel):
    """Ops Agent 查询请求。"""

    message: str = Field(..., min_length=1, max_length=400)
    use_llm: bool = True
    # 前端可主动指定本次引用的 Skill key 列表；为空则维持关键词自动匹配。
    focus_skills: list[str] = Field(default_factory=list)
    # 前端可主动指定本次使用的 LLM 配置 id；为空则使用默认（激活）配置。向后兼容。
    config_id: str | None = None
    # 近几轮对话（role=user|assistant, content=纯文本），注入 prompt 形成多轮记忆。
    history: list[dict[str, str]] = Field(default_factory=list)


class OpsAgentContextBlock(BaseModel):
    """Ops Agent 返回的上下文摘要块。"""

    key: str
    title: str
    summary: str
    items: list[str]


class OpsAgentQueryResponse(BaseModel):
    """Ops Agent 查询响应。"""

    answer: str
    mode: str
    provider: str
    model: str
    llm_enabled: bool
    used_skills: list[str]
    notice: str = ""
    suggestions: list[str]
    context_blocks: list[OpsAgentContextBlock]


class ChatRequest(BaseModel):
    """兼容旧版悬浮助手的聊天请求。"""

    message: str


class ChatResponse(BaseModel):
    """兼容旧版悬浮助手的聊天响应。"""

    reply: str
    mode: str
    notice: str = ""


class TerminalCommandRequest(BaseModel):
    """终端 AI 命令助手请求：把自然语言运维需求翻译成 Linux 命令。"""

    requirement: str = Field(..., min_length=1, max_length=400)
    os_hint: str = Field(default="Linux", max_length=40)
    config_id: str | None = None


class TerminalCommandResponse(BaseModel):
    """终端 AI 命令助手响应。"""

    ok: bool
    command: str
    mode: str
    provider: str
    model: str
    notice: str = ""


class AIObservabilityStatsResponse(BaseModel):
    """AI 观测大盘：全局指标响应（全部来自真实表，无 mock）。"""
    total_sessions: int          # 累计会话数（ai_chat_sessions）
    total_messages: int          # 累计消息数（ai_chat_messages）
    today_messages: int          # 今日消息数
    active_users: int            # 有过会话的用户数
    user_messages: int           # 用户提问数
    assistant_messages: int      # 助手回复数
    chat_trend_7d: list[int]     # 近 7 天每日消息数


class AIAuditLogItem(BaseModel):
    """AI 观测大盘：审计日志条目"""
    id: str
    time: str
    user: str
    agent: str
    intent: str
    risk: str
    status: str


class AIObservabilityAuditResponse(BaseModel):
    """AI 观测大盘：审计日志列表响应"""
    logs: list[AIAuditLogItem]


def _now_str() -> str:
    """返回统一的本地时间字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_load_json_dict(raw_value: str | None) -> dict[str, Any]:
    """安全解析 JSON，保证返回字典。"""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _get_system_config_row(db: Session, config_key: str) -> SystemConfig | None:
    """读取系统配置行。"""
    return (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == config_key)
        .first()
    )


def _save_system_config(db: Session, config_key: str, payload: dict[str, Any]) -> SystemConfig:
    """保存系统配置。

    说明：
    1. 所有 AI 平台配置都复用 system_configs，避免新表迁移；
    2. 持久化时统一带 updated_at，便于前端展示最近更新时间。
    """
    row = _get_system_config_row(db, config_key)
    if not row:
        row = SystemConfig(config_key=config_key)
    row.config_value = json.dumps(payload, ensure_ascii=False)
    row.updated_at = _now_str()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _parse_date(raw_value: str | None) -> date | None:
    """解析常见日期字符串。

    支持：
    1. YYYY-MM-DD
    2. YYYY-MM-DD HH:MM:SS
    3. ISO 格式字符串
    """
    text = str(raw_value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text):
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
        except ValueError:
            continue
    return None


def _safe_int(value: Any, default: int = 0) -> int:
    """安全转换整数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转换浮点数。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truncate_text(raw_value: str, limit: int = 80) -> str:
    """截断过长文本，避免上下文噪声过大。"""
    text = str(raw_value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _normalize_openai_base_url(raw_value: str) -> str:
    """归一化 OpenAI 兼容网关地址。"""
    text = str(raw_value or "").strip()
    if not text:
        return DEFAULT_OPENAI_COMPAT_BASE_URL
    return text.rstrip("/")


def _normalize_anthropic_base_url(raw_value: str) -> str:
    """归一化 Anthropic 接口地址，统一收敛到 `.../v1`。

    兼容以下三种用户输入（最终都拼出 `.../v1/messages`）：
    1. 裸域名 `https://aizzz.org`            → `https://aizzz.org/v1`
    2. 已带版本 `https://aizzz.org/v1`        → 原样
    3. 完整 endpoint `https://aizzz.org/v1/messages` → 去掉 `/messages`
    """
    text = str(raw_value or "").strip().rstrip("/")
    if not text:
        return DEFAULT_ANTHROPIC_BASE_URL
    # 用户若把完整 endpoint 粘进来，去掉末尾 /messages
    if text.endswith("/messages"):
        text = text[: -len("/messages")].rstrip("/")
    # 缺版本段时自动补 /v1（标准 Anthropic Messages 协议路径）
    if not text.endswith("/v1") and "/v1/" not in f"{text}/":
        text = f"{text}/v1"
    return text



def _resolve_base_url_for_provider(provider: str, raw_value: str) -> str:
    """按 Provider 解析 Base URL。"""
    if provider == "openai_compatible":
        return _normalize_openai_base_url(raw_value)
    if provider == "anthropic":
        return _normalize_anthropic_base_url(raw_value)
    return ""


def _resolve_env_api_key_for_provider(provider: str) -> str:
    """按 Provider 返回环境变量回退密钥。"""
    if provider == "openai_compatible":
        return settings.OPENAI_COMPAT_API_KEY.strip()
    if provider == "anthropic":
        return settings.ANTHROPIC_API_KEY.strip()
    return settings.GEMINI_API_KEY.strip()


def _default_model_for_provider(provider: str) -> str:
    """按提供商返回默认模型。"""
    if provider == "openai_compatible":
        return DEFAULT_OPENAI_COMPAT_MODEL
    if provider == "anthropic":
        return DEFAULT_ANTHROPIC_MODEL
    return DEFAULT_LLM_MODEL


def _read_orchestration_policy(db: Session) -> tuple[dict[str, Any], str]:
    """读取编排前置策略。"""
    row = _get_system_config_row(db, AI_ORCHESTRATION_POLICY_KEY)
    payload = _safe_load_json_dict(row.config_value if row else "")
    merged = {**DEFAULT_ORCHESTRATION_POLICY}
    for key, value in payload.items():
        if key not in merged:
            continue
        merged[key] = value

    # 类型兜底，防止历史脏数据导致前端显示异常。
    normalized = {
        "approval_enabled": bool(merged.get("approval_enabled")),
        "budget_guard_enabled": bool(merged.get("budget_guard_enabled")),
        "max_budget_cost_index": max(_safe_float(merged.get("max_budget_cost_index"), 1200.0), 0.0),
        "timeout_seconds": max(_safe_int(merged.get("timeout_seconds"), 90), 1),
        "max_steps": max(_safe_int(merged.get("max_steps"), 6), 1),
        "required_approvers": max(_safe_int(merged.get("required_approvers"), 1), 1),
        "ticket_required": bool(merged.get("ticket_required")),
        "high_risk_actions": [
            str(item).strip()
            for item in list(merged.get("high_risk_actions") or [])
            if str(item).strip()
        ] or list(DEFAULT_ORCHESTRATION_POLICY["high_risk_actions"]),
    }
    return normalized, str(row.updated_at if row else "")


def _serialize_orchestration_policy(
    policy: dict[str, Any],
    updated_at: str,
) -> OrchestrationPolicyResponse:
    """序列化编排前置策略响应。"""
    return OrchestrationPolicyResponse(
        approval_enabled=bool(policy.get("approval_enabled")),
        budget_guard_enabled=bool(policy.get("budget_guard_enabled")),
        max_budget_cost_index=_safe_float(policy.get("max_budget_cost_index"), 1200.0),
        timeout_seconds=_safe_int(policy.get("timeout_seconds"), 90),
        max_steps=_safe_int(policy.get("max_steps"), 6),
        required_approvers=_safe_int(policy.get("required_approvers"), 1),
        ticket_required=bool(policy.get("ticket_required")),
        high_risk_actions=list(policy.get("high_risk_actions") or []),
        updated_at=updated_at,
    )


def _normalize_stored_llm_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """归一化一条已存储的 LLM 配置（保留密文 api_key）。"""
    if not isinstance(entry, dict):
        return None
    provider = str(entry.get("provider") or "gemini").strip().lower() or "gemini"
    if provider not in SUPPORTED_LLM_PROVIDERS:
        provider = "gemini"
    entry_id = str(entry.get("id") or "").strip() or uuid.uuid4().hex
    model = str(entry.get("model") or _default_model_for_provider(provider)).strip() or _default_model_for_provider(provider)
    return {
        "id": entry_id,
        "name": str(entry.get("name") or "").strip()[:60] or model,
        "provider": provider,
        "base_url": _resolve_base_url_for_provider(provider, str(entry.get("base_url") or "")),
        "model": model,
        "api_key": str(entry.get("api_key") or ""),
        "temperature": min(max(_safe_float(entry.get("temperature"), 0.2), 0.0), 1.0),
        "max_output_tokens": min(max(_safe_int(entry.get("max_output_tokens"), 1200), 128), 8192),
        "timeout_seconds": min(max(_safe_int(entry.get("timeout_seconds"), 30), 5), 120),
        "system_prompt": str(entry.get("system_prompt") or DEFAULT_SYSTEM_PROMPT).strip() or DEFAULT_SYSTEM_PROMPT,
        "created_at": str(entry.get("created_at") or ""),
        "updated_at": str(entry.get("updated_at") or ""),
    }


def _load_llm_config_store(db: Session) -> tuple[list[dict[str, Any]], str]:
    """读取多条 LLM 配置存储，自动迁移旧版单条结构。

    存储结构（system_configs[ai_llm_config]）：
        {"configs": [ {id,name,provider,...,api_key(密文)} ], "active_id": "<id>"}
    旧版结构为单条 dict（含 provider/api_key 等），首次读取时自动转为列表第一条并设为启用。
    """
    row = _get_system_config_row(db, AI_LLM_CONFIG_KEY)
    payload = _safe_load_json_dict(row.config_value if row else "")

    raw_configs = payload.get("configs")
    if isinstance(raw_configs, list):
        items = [e for e in (_normalize_stored_llm_entry(c) for c in raw_configs) if e]
        active_id = str(payload.get("active_id") or "")
        if items and active_id not in {e["id"] for e in items}:
            active_id = items[0]["id"]
        return items, active_id

    if payload.get("provider") or payload.get("api_key"):
        # 旧版单条结构 → 迁移为列表并立即落库，固定 id（否则每次读取生成新 id 会导致编辑/删除 404）
        migrated = _normalize_stored_llm_entry(payload)
        items = [migrated] if migrated else []
        active_id = items[0]["id"] if items else ""
        if items:
            _save_llm_config_store(db, items, active_id)
        return items, active_id

    return [], ""



def _save_llm_config_store(db: Session, items: list[dict[str, Any]], active_id: str) -> SystemConfig:
    """保存多条 LLM 配置存储。"""
    return _save_system_config(
        db,
        AI_LLM_CONFIG_KEY,
        {"configs": items, "active_id": active_id},
    )


def _resolve_runtime_from_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
    """把一条存储配置解析为运行态配置（解密 + 环境变量回退）。"""
    if not entry:
        return {
            "enabled": False,
            "provider": "gemini",
            "base_url": _resolve_base_url_for_provider("gemini", ""),
            "model": _default_model_for_provider("gemini"),
            "temperature": 0.2,
            "max_output_tokens": 1200,
            "timeout_seconds": 30,
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "api_key": "",
            "has_api_key": False,
            "using_env_fallback": False,
        }
    provider = entry["provider"]
    stored_api_key = decrypt_secret(str(entry.get("api_key") or ""))
    env_api_key = _resolve_env_api_key_for_provider(provider)
    resolved_api_key = stored_api_key or env_api_key
    return {
        # active 且有可用密钥即视为启用（无独立开关）
        "enabled": bool(resolved_api_key),
        "provider": provider,
        "base_url": entry["base_url"],
        "model": entry["model"],
        "temperature": entry["temperature"],
        "max_output_tokens": entry["max_output_tokens"],
        "timeout_seconds": entry["timeout_seconds"],
        "system_prompt": entry["system_prompt"],
        "api_key": resolved_api_key,
        "has_api_key": bool(resolved_api_key),
        "using_env_fallback": bool(not stored_api_key and env_api_key),
    }


def _read_llm_runtime_config(db: Session, config_id: str | None = None) -> tuple[dict[str, Any], str]:
    """读取 LLM 运行态配置。

    说明：
    1. 默认取 active_id 指向的那条；若传入 config_id 且命中，则改用该条（工作台临时切换模型用，不改全局默认）；
    2. 解密其密钥，必要时回退到对应环境变量；
    3. 无任何配置时返回 enabled=False 的占位配置（工作台退回本地摘要）。
    """
    items, active_id = _load_llm_config_store(db)
    target_id = config_id or active_id
    active_entry = next((e for e in items if e["id"] == target_id), None)
    # 指定的 config_id 不存在时回退到默认配置，保证向后兼容。
    if active_entry is None and config_id:
        active_entry = next((e for e in items if e["id"] == active_id), None)
    config = _resolve_runtime_from_entry(active_entry)
    updated_at = str(active_entry.get("updated_at") or "") if active_entry else ""
    return config, updated_at


def _serialize_llm_config_item(entry: dict[str, Any], active_id: str) -> LLMConfigItemResponse:
    """把一条存储配置转换为前端列表项（不回传明文密钥）。"""
    provider = entry["provider"]
    stored_api_key = decrypt_secret(str(entry.get("api_key") or ""))
    env_api_key = _resolve_env_api_key_for_provider(provider)
    has_api_key = bool(stored_api_key or env_api_key)
    return LLMConfigItemResponse(
        id=entry["id"],
        name=entry["name"],
        provider=provider,
        base_url=entry["base_url"],
        model=entry["model"],
        temperature=_safe_float(entry.get("temperature"), 0.2),
        max_output_tokens=_safe_int(entry.get("max_output_tokens"), 1200),
        timeout_seconds=_safe_int(entry.get("timeout_seconds"), 30),
        system_prompt=str(entry.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
        has_api_key=has_api_key,
        using_env_fallback=bool(not stored_api_key and env_api_key),
        is_active=entry["id"] == active_id,
        created_at=str(entry.get("created_at") or ""),
        updated_at=str(entry.get("updated_at") or ""),
    )


def _read_skill_registry(db: Session) -> tuple[list[dict[str, Any]], str]:
    """读取 Skill Registry。

    Skill Registry 一期只允许开关内置只读技能，不开放任意代码执行。
    """
    row = _get_system_config_row(db, AI_SKILL_CONFIG_KEY)
    payload = _safe_load_json_dict(row.config_value if row else "")
    overrides = payload.get("skills") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    result: list[dict[str, Any]] = []
    for item in DEFAULT_SKILL_CATALOG:
        override_meta = overrides.get(item["key"]) or {}
        enabled = item["enabled"]
        if isinstance(override_meta, dict) and isinstance(override_meta.get("enabled"), bool):
            enabled = override_meta["enabled"]
        result.append({**item, "enabled": enabled})
    return result, str(row.updated_at if row else "")


def _build_llm_runtime_config_from_payload(
    payload: LLMConfigTestRequest,
    fallback_api_key: str,
) -> dict[str, Any]:
    """把检测请求转换为运行态配置。

    说明：
    1. 检测时优先使用表单里临时输入的 API Key；
    2. 若表单留空，则复用已保存配置或环境变量回退，便于验证当前生效配置；
    3. 检测接口不会把临时密钥写回数据库。
    """
    provider = _ensure_supported_provider(payload.provider)
    resolved_api_key = payload.api_key.strip() or fallback_api_key.strip()
    return {
        "enabled": True,
        "provider": provider,
        "base_url": _resolve_base_url_for_provider(provider, payload.base_url),
        "model": payload.model.strip() or _default_model_for_provider(provider),
        "temperature": payload.temperature,
        "max_output_tokens": payload.max_output_tokens,
        "timeout_seconds": payload.timeout_seconds,
        "system_prompt": payload.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT,
        "api_key": resolved_api_key,
        "has_api_key": bool(resolved_api_key),
        "using_env_fallback": False,
    }


def _build_agent_quick_questions(enabled_skills: list[dict[str, Any]]) -> list[str]:
    """生成 Agent 首页示例问题。"""
    quick_questions: list[str] = []
    for skill in enabled_skills:
        for sample_query in list(skill.get("sample_queries") or []):
            if sample_query and sample_query not in quick_questions:
                quick_questions.append(sample_query)
            if len(quick_questions) >= 6:
                return quick_questions
    return quick_questions


def _select_focus_skill_keys(
    message: str,
    enabled_skills: list[dict[str, Any]],
    requested_keys: list[str] | None = None,
) -> list[str]:
    """根据用户问题选择更相关的 Skill。

    若前端显式传入 ``requested_keys``（方案 A），则在已启用 Skill 范围内按其取交集、
    保持请求顺序；交集为空时回退到关键词自动匹配。
    """
    enabled_key_set = {item["key"] for item in enabled_skills}
    if requested_keys:
        explicit = [key for key in requested_keys if key in enabled_key_set]
        if explicit:
            # 去重保序
            seen: set[str] = set()
            deduped = [k for k in explicit if not (k in seen or seen.add(k))]
            return deduped
    lowered = message.lower()
    matched_keys: list[str] = []
    for skill_key, keywords in SKILL_FOCUS_KEYWORDS.items():
        if skill_key not in enabled_key_set:
            continue
        if any(keyword in lowered for keyword in keywords):
            matched_keys.append(skill_key)
    return matched_keys or [item["key"] for item in enabled_skills]


def _build_asset_context(db: Session) -> dict[str, Any]:
    """构建主机资产上下文。"""
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    total = len(assets)
    healthy = sum(1 for item in assets if str(item.type or "").lower() == "healthy")
    pending = sum(1 for item in assets if str(item.type or "").lower() == "pending")
    abnormal_assets = [
        item for item in assets if str(item.type or "").lower() not in {"healthy"}
    ]
    summary = f"主机 {total} 台，在线 {healthy} 台，待验证 {pending} 台，异常 {max(total - healthy - pending, 0)} 台。"
    items = [
        f"{item.name}({item.ip}) · {item.provider or 'local'} · {item.type or 'unknown'}"
        for item in abnormal_assets[:MAX_CONTEXT_ITEMS]
    ]
    if not items:
        items = ["当前未发现异常主机。"]
    return {
        "key": "skill.assets.overview",
        "title": "资产总览",
        "summary": summary,
        "items": items,
    }


def _build_vm_context(db: Session) -> dict[str, Any]:
    """构建虚机上下文。"""
    vms = db.query(VirtualMachine).order_by(VirtualMachine.id.desc()).all()
    total = len(vms)
    running = sum(1 for item in vms if str(item.status or "").lower() == "running")
    stopped = sum(1 for item in vms if str(item.status or "").lower() != "running")
    items = [
        f"{item.name} · 节点 {item.node or '-'} · 状态 {item.status or '-'}"
        for item in vms[:MAX_CONTEXT_ITEMS]
    ] or ["当前未同步到虚机数据。"]
    return {
        "key": "skill.vms.overview",
        "title": "虚机总览",
        "summary": f"虚机 {total} 台，运行中 {running} 台，非运行态 {stopped} 台。",
        "items": items,
    }


def _build_monitoring_context(db: Session) -> dict[str, Any]:
    """构建监控概览上下文。"""
    try:
        payload = get_monitoring_dashboard(
            app_id="", app_name="", host_ips="", host_names="",
            range_minutes=60, step_seconds=DEFAULT_STEP_SECONDS, db=db,
        )
    except Exception as exc:  # pragma: no cover - 防御式兜底
        return {
            "key": "skill.monitoring.overview",
            "title": "监控概览",
            "summary": "监控数据读取失败。",
            "items": [f"读取监控概览失败：{exc}"],
        }

    summary_data = payload.get("summary") or {}
    metrics = payload.get("metrics") or []
    latest_metric = metrics[-1] if metrics else {}
    configured = bool(payload.get("configured"))
    items = [
        f"采集目标 {summary_data.get('targets', 0)} 个，在线 {summary_data.get('up', 0)} 个，离线 {summary_data.get('down', 0)} 个。",
        f"平均 CPU {_safe_float(latest_metric.get('cpu')):.2f}% ，平均内存 {_safe_float(latest_metric.get('memory')):.2f}%。",
        f"平均入网 {_safe_float(latest_metric.get('networkIn')):.2f} MB/s ，平均出网 {_safe_float(latest_metric.get('networkOut')):.2f} MB/s。",
    ]
    if not configured:
        items = [str(payload.get("message") or "监控连接尚未配置。")]
    return {
        "key": "skill.monitoring.overview",
        "title": "监控概览",
        "summary": "监控组件已接入，支持从 VictoriaMetrics 读取概览摘要。" if configured else "监控组件尚未完成可用接入。",
        "items": items,
    }


def _build_alert_context(db: Session) -> dict[str, Any]:
    """构建活动告警上下文。"""
    try:
        payload = get_monitoring_dashboard(
            app_id="", app_name="", host_ips="", host_names="",
            range_minutes=60, step_seconds=DEFAULT_STEP_SECONDS, db=db,
        )
    except Exception as exc:  # pragma: no cover - 防御式兜底
        return {
            "key": "skill.alerts.active",
            "title": "活动告警",
            "summary": "活动告警读取失败。",
            "items": [f"读取活动告警失败：{exc}"],
        }

    alerts = list(payload.get("alerts") or [])
    items = [
        (
            f"{item.get('host') or '-'} · {item.get('rule') or '未命名告警'} · "
            f"{item.get('severity') or 'warning'} · {item.get('status') or 'firing'}"
        )
        for item in alerts[:MAX_CONTEXT_ITEMS]
    ]
    if not items:
        items = ["当前没有活动告警。"]
    return {
        "key": "skill.alerts.active",
        "title": "活动告警",
        "summary": f"当前活动告警 {len(alerts)} 条。",
        "items": items,
    }


def _build_domain_context(db: Session) -> dict[str, Any]:
    """构建域名上下文。"""
    domains = db.query(DomainRecord).order_by(DomainRecord.id.desc()).all()
    today = date.today()
    expiring = []
    for item in domains:
        expires_at = _parse_date(item.expires_at)
        if expires_at and expires_at <= today + timedelta(days=EXPIRING_SOON_DAYS):
            expiring.append(item)
    items = [
        f"{item.domain} · 到期 {item.expires_at or '-'} · 云厂商 {item.provider or '-'}"
        for item in expiring[:MAX_CONTEXT_ITEMS]
    ]
    if not items:
        items = ["当前没有 30 天内到期的域名。"]
    return {
        "key": "skill.domains.overview",
        "title": "域名总览",
        "summary": f"域名 {len(domains)} 个，30 天内到期 {len(expiring)} 个。",
        "items": items,
    }


def _build_certificate_context(db: Session) -> dict[str, Any]:
    """构建证书上下文。"""
    certificates = db.query(CertificateRecord).order_by(CertificateRecord.id.desc()).all()
    today = date.today()
    expiring = []
    failed = []
    for item in certificates:
        expires_at = _parse_date(item.not_after)
        if expires_at and expires_at <= today + timedelta(days=EXPIRING_SOON_DAYS):
            expiring.append(item)
        if str(item.status or "").lower() in {"failed", "expired", "expiring"}:
            failed.append(item)
    items = [
        f"{item.primary_domain} · 状态 {item.status or '-'} · 到期 {item.not_after or '-'}"
        for item in (expiring + failed)[:MAX_CONTEXT_ITEMS]
    ]
    if not items:
        items = ["当前没有即将到期或失败的证书。"]
    return {
        "key": "skill.certificates.overview",
        "title": "证书总览",
        "summary": (
            f"证书 {len(certificates)} 张，30 天内到期 {len(expiring)} 张，"
            f"异常状态 {len(failed)} 张。"
        ),
        "items": items,
    }


def _build_audit_context(db: Session) -> dict[str, Any]:
    """构建最近审计上下文。"""
    audits = db.query(AuditLog).order_by(AuditLog.time.desc()).limit(MAX_CONTEXT_ITEMS).all()
    items = [
        f"{item.user} · {item.action} · {item.time.strftime('%m-%d %H:%M') if item.time else '-'}"
        for item in audits
    ]
    if not items:
        items = ["当前没有可展示的审计记录。"]
    return {
        "key": "skill.audit.recent",
        "title": "最近审计",
        "summary": f"最近 {len(audits)} 条关键变更已纳入审计。",
        "items": items,
    }


def _build_security_attack_surface_context(db: Session) -> dict[str, Any]:
    """构建 Security 攻击面概览上下文。"""
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    certificates = db.query(CertificateRecord).order_by(CertificateRecord.id.desc()).all()
    credentials = db.query(Credential).order_by(Credential.id.desc()).all()

    critical_assets = [
        item for item in assets if str(item.type or "").lower() == "critical"
    ]
    abnormal_certs = [
        item for item in certificates
        if str(item.status or "").lower() in {"expired", "failed", "expiring", "revoked"}
    ]
    expired_creds = [
        item for item in credentials
        if str(item.status or "").lower() == "expired"
    ]

    try:
        monitoring_payload = get_monitoring_dashboard(
            app_id="", app_name="", host_ips="", host_names="",
            range_minutes=60, step_seconds=DEFAULT_STEP_SECONDS, db=db,
        )
        active_alerts = list(monitoring_payload.get("alerts") or [])
    except Exception:
        active_alerts = []

    items: list[str] = []
    if active_alerts:
        for item in active_alerts[:2]:
            items.append(
                f"活动告警 {item.get('rule') or '未命名'} · {item.get('severity') or 'warning'} · {item.get('host') or '-'}"
            )
    if critical_assets:
        items.append(f"异常主机 {len(critical_assets)} 台，优先检查网络可达性与 SSH 凭证。")
    if abnormal_certs:
        items.append(f"异常证书 {len(abnormal_certs)} 张，建议优先处理过期/吊销状态。")
    if expired_creds:
        items.append(f"已过期凭证 {len(expired_creds)} 条，建议立即轮换并清理引用。")
    if not items:
        items = ["当前未发现明显攻击面暴露信号。"]

    return {
        "key": "skill.security.attack_surface",
        "title": "攻击面概览",
        "summary": (
            f"活动告警 {len(active_alerts)} 条，异常主机 {len(critical_assets)} 台，"
            f"异常证书 {len(abnormal_certs)} 张，过期凭证 {len(expired_creds)} 条。"
        ),
        "items": items,
    }


def _build_security_compliance_context(db: Session) -> dict[str, Any]:
    """构建 Security 基线合规上下文。"""
    assets = db.query(Asset).order_by(Asset.id.desc()).all()
    domains = db.query(DomainRecord).order_by(DomainRecord.id.desc()).all()
    audits = db.query(AuditLog).order_by(AuditLog.time.desc()).limit(MAX_CONTEXT_ITEMS).all()
    credentials = db.query(Credential).order_by(Credential.id.desc()).all()

    root_login_assets = [
        item for item in assets if str(item.ssh_user or "").strip().lower() == "root"
    ]
    soon_expiring_domains = []
    today = date.today()
    for item in domains:
        expires_at = _parse_date(item.expires_at)
        if expires_at and expires_at <= today + timedelta(days=EXPIRING_SOON_DAYS):
            soon_expiring_domains.append(item)
    expiring_or_expired_creds = [
        item for item in credentials if str(item.status or "").lower() in {"expiring", "expired"}
    ]

    items: list[str] = []
    if root_login_assets:
        items.append(f"使用 root 登录主机 {len(root_login_assets)} 台，建议逐步收敛到最小权限账号。")
    if expiring_or_expired_creds:
        items.append(f"临近过期/已过期凭证 {len(expiring_or_expired_creds)} 条，建议执行凭证轮换。")
    if soon_expiring_domains:
        items.append(f"30 天内到期域名 {len(soon_expiring_domains)} 个，建议验证续费与自动提醒。")
    if audits:
        latest_audit = audits[0]
        items.append(
            f"最近关键操作：{latest_audit.user} · {latest_audit.action} · {latest_audit.time.strftime('%m-%d %H:%M') if latest_audit.time else '-'}"
        )
    if not items:
        items = ["当前未发现明显基线偏离信号。"]

    return {
        "key": "skill.security.compliance_baseline",
        "title": "基线合规扫描",
        "summary": (
            f"root 登录主机 {len(root_login_assets)} 台，"
            f"异常凭证 {len(expiring_or_expired_creds)} 条，"
            f"近到期域名 {len(soon_expiring_domains)} 个。"
        ),
        "items": items,
    }


def _build_system_context(db: Session) -> dict[str, Any]:
    """生成系统资源上下文块"""
    os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    
    items = [
        f"- 操作系统版本: {os_info}",
        f"- Python 版本: {platform.python_version()}",
        f"- 系统平均负载 (1/5/15分钟): {load_avg[0]}, {load_avg[1]}, {load_avg[2]}"
    ]
    
    return {
        "key": "skill.system.overview",
        "title": "系统运行信息",
        "summary": f"成功读取系统状态，当前 1 分钟负载为 {load_avg[0]}",
        "items": items,
    }


SKILL_CONTEXT_BUILDERS = {
    "skill.assets.overview": _build_asset_context,
    "skill.vms.overview": _build_vm_context,
    "skill.monitoring.overview": _build_monitoring_context,
    "skill.alerts.active": _build_alert_context,
    "skill.domains.overview": _build_domain_context,
    "skill.certificates.overview": _build_certificate_context,
    "skill.audit.recent": _build_audit_context,
    "skill.security.attack_surface": _build_security_attack_surface_context,
    "skill.security.compliance_baseline": _build_security_compliance_context,
    "skill.system.overview": _build_system_context,
}


def _build_context_blocks(
    db: Session,
    selected_skill_keys: list[str],
) -> list[dict[str, Any]]:
    """根据选定 Skill 生成上下文块。"""
    blocks: list[dict[str, Any]] = []
    for skill_key in selected_skill_keys:
        builder = SKILL_CONTEXT_BUILDERS.get(skill_key)
        if not builder:
            continue
        blocks.append(builder(db))
    return blocks


def _build_follow_up_suggestions(context_blocks: list[dict[str, Any]]) -> list[str]:
    """根据上下文块生成下一步建议。"""
    suggestions: list[str] = []
    block_keys = {item["key"] for item in context_blocks}
    if "skill.alerts.active" in block_keys:
        suggestions.append("继续问：哪些告警最需要优先处理，以及建议排查路径是什么？")
    if "skill.assets.overview" in block_keys:
        suggestions.append("继续问：异常主机是否集中在同一云厂商或地域？")
    if "skill.certificates.overview" in block_keys:
        suggestions.append("继续问：哪些证书需要优先续签，以及对应业务域名是什么？")
    if "skill.audit.recent" in block_keys:
        suggestions.append("继续问：最近的配置变更是否可能与当前异常相关？")
    if not suggestions:
        suggestions.append("继续问：请基于当前平台状态给出下一步排查建议。")
    return suggestions[:3]


def _build_local_answer(message: str, context_blocks: list[dict[str, Any]]) -> str:
    """在未配置模型或模型调用失败时，返回本地结构化摘要。"""
    lines = [f"**问题**：{message}", "", "**只读上下文摘要**"]
    for block in context_blocks:
        lines.append(f"- **{block['title']}**：{block['summary']}")
        for item in block.get("items", [])[:3]:
            lines.append(f"  - {item}")
    lines.extend(
        [
            "",
            "**边界说明**",
            "- 当前回答基于平台实时只读摘要生成，没有执行任何生产写操作。",
            "- 若需要进一步执行变更，请先在对应业务模块中人工确认。",
        ]
    )
    return "\n".join(lines)


def _build_llm_prompt(
    question: str,
    context_blocks: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> str:
    """构造发给模型的提示词。history 为近几轮 user/assistant 消息，形成多轮记忆。"""
    context_payload = {
        "question": question,
        "context_blocks": [
            {
                "title": item["title"],
                "summary": item["summary"],
                "items": item.get("items", []),
            }
            for item in context_blocks
        ],
    }
    history_section = ""
    if history:
        lines = []
        for h in history[-8:]:
            role = "用户" if str(h.get("role") or "user") == "user" else "助手"
            content = str(h.get("content") or "").strip()
            if content:
                lines.append(f"{role}：{content}")
        if lines:
            history_section = "对话历史（仅作理解上下文，结论仍须基于下方实时数据）：\n" + "\n".join(lines) + "\n\n"
    return (
        history_section
        + "用户问题如下，请仅基于给定上下文回答，不得编造额外事实。\n\n"
        f"问题：{question}\n\n"
        "上下文(JSON)：\n"
        f"{json.dumps(context_payload, ensure_ascii=False, indent=2)}\n\n"
        "请输出：\n"
        "1. 结论摘要；\n"
        "2. 关键风险点；\n"
        "3. 下一步建议；\n"
        "4. 如果上下文不足，明确说明缺少什么数据。"
    )


def _generate_gemini_content(
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
) -> str:
    """同步执行 Gemini 推理。"""
    from google import genai

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
    )
    return str(getattr(response, "text", "") or "").strip()


def _generate_openai_compatible_content(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout_seconds: int,
) -> str:
    """同步执行 OpenAI 兼容接口推理。

    兼容说明：
    1. 使用标准 `/v1/chat/completions` 协议；
    2. 支持 OpenAI 官方网关以及兼容该协议的自建/第三方网关；
    3. 若返回结构中没有标准 choices/message，会返回空字符串并触发上层回退逻辑。
    """
    endpoint = f"{_normalize_openai_base_url(base_url)}/chat/completions"
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        },
        timeout=timeout_seconds,
    )
    _raise_llm_http_error(response)
    payload = response.json() if response.content else {}
    choices = list(payload.get("choices") or [])
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # 兼容部分网关返回内容片段列表的场景。
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
            elif isinstance(item, str) and item.strip():
                chunks.append(item.strip())
        return "\n".join(chunks).strip()
    return ""


def _raise_llm_http_error(response: "requests.Response") -> None:
    """把 LLM 服务端 4xx/5xx 转成可操作提示（403 多半是 key/模型/协议问题）。"""
    if response.ok:
        return
    status = response.status_code
    detail = (response.text or "")[:200]
    if status in (401, 403):
        raise RuntimeError(
            f"模型服务拒绝访问（HTTP {status}）：请检查 API Key 是否有效、模型名是否在权限内、"
            f"以及服务商协议（Anthropic /v1/messages 或 OpenAI 兼容 /v1/chat/completions）是否选对。"
            f" 响应片段：{detail}"
        )
    if status == 404:
        raise RuntimeError(
            f"模型服务地址或模型不存在（HTTP 404）：检查 base_url 与 model 名称。响应片段：{detail}"
        )
    response.raise_for_status()


def _generate_anthropic_content(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout_seconds: int,
) -> str:
    """同步执行 Anthropic Messages 推理。

    协议说明：
    1. 使用 Anthropic 官方 `messages` 协议；
    2. 默认地址为 `https://api.anthropic.com/v1/messages`；
    3. 兼容返回内容片段数组，提取其中 `type=text` 的文本段。
    """
    endpoint = f"{_normalize_anthropic_base_url(base_url)}/messages"
    response = requests.post(
        endpoint,
        headers={
            # 兼容官方与中转站：两种鉴权头都带上
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout_seconds,
    )
    _raise_llm_http_error(response)
    payload = response.json() if response.content else {}
    content_chunks = list(payload.get("content") or [])
    if not content_chunks:
        return ""

    texts: list[str] = []
    for item in content_chunks:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n".join(texts).strip()


def _stream_openai_compatible_content(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout_seconds: int,
):
    """流式执行 OpenAI 兼容接口推理，逐块产出文本增量。

    协议说明：
    1. 请求带 `stream=true`，响应为 SSE，每行以 `data: ` 开头；
    2. 解析 `choices[0].delta.content` 文本增量；
    3. 读到 `data: [DONE]` 结束。
    """
    endpoint = f"{_normalize_openai_base_url(base_url)}/chat/completions"
    with requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "stream": True,
        },
        timeout=timeout_seconds,
        stream=True,
    ) as response:
        _raise_llm_http_error(response)
        response.encoding = "utf-8"  # SSE 无 charset 时 requests 默认 latin-1，会让中文乱码
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                yield piece


def _stream_anthropic_content(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    system_prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout_seconds: int,
):
    """流式执行 Anthropic Messages 推理，逐块产出文本增量。

    协议说明：
    1. 请求带 `stream=true`，响应为 SSE；
    2. 关注 `content_block_delta` 事件中的 `delta.text`；
    3. `message_stop` 事件结束。
    """
    endpoint = f"{_normalize_anthropic_base_url(base_url)}/messages"
    with requests.post(
        endpoint,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        },
        timeout=timeout_seconds,
        stream=True,
    ) as response:
        _raise_llm_http_error(response)
        response.encoding = "utf-8"  # SSE 无 charset 时 requests 默认 latin-1，会让中文乱码
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            etype = str(event.get("type") or "")
            if etype == "content_block_delta":
                delta = event.get("delta") or {}
                piece = delta.get("text")
                if isinstance(piece, str) and piece:
                    yield piece
            elif etype == "message_stop":
                break


def _stream_llm_analysis(
    llm_config: dict[str, Any],
    question: str,
    context_blocks: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
):
    """按 Provider 选择流式推理实现，逐块产出文本增量。

    Gemini 暂不在此实现流式（其 SDK 流式接入差异较大），调用方应回退到
    一次性结果再做本地分块；OpenAI 兼容与 Anthropic 走原生 SSE 流式。
    """
    provider = str(llm_config.get("provider") or "")
    api_key = str(llm_config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("未配置可用模型 API Key")

    prompt = _build_llm_prompt(question, context_blocks, history)
    timeout_seconds = _safe_int(llm_config.get("timeout_seconds"), 30)
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    system_prompt = str(llm_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
    temperature = _safe_float(llm_config.get("temperature"), 0.2)
    max_output_tokens = _safe_int(llm_config.get("max_output_tokens"), 1200)

    if provider == "openai_compatible":
        yield from _stream_openai_compatible_content(
            api_key,
            str(llm_config.get("base_url") or DEFAULT_OPENAI_COMPAT_BASE_URL),
            model,
            prompt,
            system_prompt,
            temperature,
            max_output_tokens,
            timeout_seconds,
        )
        return
    if provider == "anthropic":
        yield from _stream_anthropic_content(
            api_key,
            str(llm_config.get("base_url") or DEFAULT_ANTHROPIC_BASE_URL),
            model,
            prompt,
            system_prompt,
            temperature,
            max_output_tokens,
            timeout_seconds,
        )
        return
    raise RuntimeError(f"暂不支持流式的模型提供商：{provider}")


async def _run_llm_analysis(
    llm_config: dict[str, Any],
    question: str,
    context_blocks: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> str:
    """执行 LLM 分析。"""
    provider = str(llm_config.get("provider") or "")
    api_key = str(llm_config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("未配置可用模型 API Key")

    prompt = _build_llm_prompt(question, context_blocks, history)
    timeout_seconds = _safe_int(llm_config.get("timeout_seconds"), 30)
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    system_prompt = str(llm_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
    temperature = _safe_float(llm_config.get("temperature"), 0.2)
    max_output_tokens = _safe_int(llm_config.get("max_output_tokens"), 1200)

    if provider == "gemini":
        return await asyncio.wait_for(
            asyncio.to_thread(
                _generate_gemini_content,
                api_key,
                model,
                prompt,
                system_prompt,
                temperature,
                max_output_tokens,
            ),
            timeout=timeout_seconds,
        )

    if provider == "openai_compatible":
        return await asyncio.wait_for(
            asyncio.to_thread(
                _generate_openai_compatible_content,
                api_key,
                str(llm_config.get("base_url") or DEFAULT_OPENAI_COMPAT_BASE_URL),
                model,
                prompt,
                system_prompt,
                temperature,
                max_output_tokens,
                timeout_seconds,
            ),
            timeout=timeout_seconds,
        )

    if provider == "anthropic":
        return await asyncio.wait_for(
            asyncio.to_thread(
                _generate_anthropic_content,
                api_key,
                str(llm_config.get("base_url") or DEFAULT_ANTHROPIC_BASE_URL),
                model,
                prompt,
                system_prompt,
                temperature,
                max_output_tokens,
                timeout_seconds,
            ),
            timeout=timeout_seconds,
        )

    raise RuntimeError(f"暂不支持的模型提供商：{provider}")


async def _run_llm_health_check(llm_config: dict[str, Any]) -> LLMConfigTestResponse:
    """执行最小化模型连通性检测。"""
    provider = str(llm_config.get("provider") or "gemini")
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    if not llm_config.get("has_api_key"):
        return LLMConfigTestResponse(
            ok=False,
            provider=provider,
            base_url=str(llm_config.get("base_url") or ""),
            model=model,
            latency_ms=0,
            detail="未配置可用模型 API Key。",
            checked_at=_now_str(),
        )

    started_at = time.perf_counter()
    timeout_seconds = _safe_int(llm_config.get("timeout_seconds"), 30)
    try:
        if provider == "gemini":
            response_text = await asyncio.wait_for(
                asyncio.to_thread(
                    _generate_gemini_content,
                    str(llm_config.get("api_key") or ""),
                    model,
                    "这是 OpsPilot 的模型连通性自检请求。请只返回字符串 OK。",
                    str(llm_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
                    _safe_float(llm_config.get("temperature"), 0.2),
                    min(_safe_int(llm_config.get("max_output_tokens"), 1200), 256),
                ),
                timeout=timeout_seconds,
            )
        elif provider == "openai_compatible":
            response_text = await asyncio.wait_for(
                asyncio.to_thread(
                    _generate_openai_compatible_content,
                    str(llm_config.get("api_key") or ""),
                    str(llm_config.get("base_url") or DEFAULT_OPENAI_COMPAT_BASE_URL),
                    model,
                    "这是 OpsPilot 的模型连通性自检请求。请只返回字符串 OK。",
                    str(llm_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
                    _safe_float(llm_config.get("temperature"), 0.2),
                    min(_safe_int(llm_config.get("max_output_tokens"), 1200), 256),
                    timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        elif provider == "anthropic":
            response_text = await asyncio.wait_for(
                asyncio.to_thread(
                    _generate_anthropic_content,
                    str(llm_config.get("api_key") or ""),
                    str(llm_config.get("base_url") or DEFAULT_ANTHROPIC_BASE_URL),
                    model,
                    "这是 OpsPilot 的模型连通性自检请求。请只返回字符串 OK。",
                    str(llm_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
                    _safe_float(llm_config.get("temperature"), 0.2),
                    min(_safe_int(llm_config.get("max_output_tokens"), 1200), 256),
                    timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        else:
            raise RuntimeError(f"暂不支持的模型提供商：{provider}")

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        if not response_text.strip():
            return LLMConfigTestResponse(
                ok=False,
                provider=str(llm_config.get("provider") or "gemini"),
                base_url=str(llm_config.get("base_url") or ""),
                model=model,
                latency_ms=latency_ms,
                detail="模型返回为空，请检查模型可用性或配额。",
                checked_at=_now_str(),
            )
        return LLMConfigTestResponse(
            ok=True,
            provider=str(llm_config.get("provider") or "gemini"),
            base_url=str(llm_config.get("base_url") or ""),
            model=model,
            latency_ms=latency_ms,
            detail=f"模型连通性正常，响应摘要：{_truncate_text(response_text, 80)}",
            checked_at=_now_str(),
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        return LLMConfigTestResponse(
            ok=False,
            provider=str(llm_config.get("provider") or "gemini"),
            base_url=str(llm_config.get("base_url") or ""),
            model=model,
            latency_ms=latency_ms,
            detail=f"模型检测失败：{_truncate_text(str(exc), 120)}",
            checked_at=_now_str(),
        )


def _ensure_supported_provider(provider: str) -> str:
    """校验并归一化 LLM 提供商。"""
    normalized = provider.strip().lower() or "gemini"
    if normalized not in SUPPORTED_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail="当前仅支持 Gemini、OpenAI 兼容网关和 Anthropic")
    return normalized


def _resolve_agent_scope(agent_key: str) -> dict[str, Any]:
    """读取并校验 Agent 的技能作用域配置。"""
    scope = AGENT_SKILL_SCOPES.get(agent_key)
    if not scope:
        raise HTTPException(status_code=400, detail=f"未知 Agent：{agent_key}")
    return scope


def _filter_enabled_skills_for_agent(
    agent_key: str,
    skills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 Agent 作用域过滤可用 Skill。

    安全说明：
    1. Skill Registry 是全局开关，Agent 只读取其中 enabled=true 的项目；
    2. 再叠加 agent scope 白名单，避免跨 Agent 读取不应暴露的上下文；
    3. 返回值用于 runtime 展示与 query 执行，确保前后端结果一致。
    """
    scope = _resolve_agent_scope(agent_key)
    allowed_skill_keys = set(scope.get("skills") or set())
    return [
        item for item in skills
        if item.get("enabled") and item.get("key") in allowed_skill_keys
    ]


def _build_agent_runtime(db: Session, agent_key: str) -> OpsAgentRuntimeResponse:
    """构建指定 Agent 的安全运行态信息。"""
    llm_config, _ = _read_llm_runtime_config(db)
    skills, _ = _read_skill_registry(db)
    enabled_skills = _filter_enabled_skills_for_agent(agent_key, skills)
    scope = _resolve_agent_scope(agent_key)
    provider = str(llm_config.get("provider") or "gemini")
    model = str(llm_config.get("model") or _default_model_for_provider(provider))

    notice = ""
    if not enabled_skills:
        notice = f"当前 {scope.get('title') or 'Agent'} 没有启用任何 Skill，暂时无法生成分析上下文。"
    elif not llm_config["has_api_key"]:
        notice = "当前未配置可用模型密钥，请先在 LLM 配置中完成接入。"
    elif not llm_config["enabled"]:
        notice = "模型接入信息已存在，但模型分析开关处于关闭状态。"

    return OpsAgentRuntimeResponse(
        llm_enabled=bool(llm_config["enabled"]),
        llm_ready=bool(llm_config["enabled"] and llm_config["has_api_key"]),
        provider=provider,
        model=model,
        using_env_fallback=bool(llm_config.get("using_env_fallback")),
        notice=notice,
        enabled_skills=[SkillRegistryItem(**item) for item in enabled_skills],
        quick_questions=_build_agent_quick_questions(enabled_skills),
    )


async def _execute_agent_query(
    db: Session,
    message: str,
    use_llm: bool,
    agent_key: str,
    requested_skill_keys: list[str] | None = None,
    config_id: str | None = None,
) -> OpsAgentQueryResponse:
    """执行指定 Agent 的查询。

    说明：
    1. 先按 Agent 白名单 + Skill Registry 开关裁剪上下文，避免无关噪声；
    2. 如果已配置模型，则优先调用 LLM；传入 config_id 时临时使用该模型配置，不改全局默认；
    3. 若模型不可用，则自动回退到本地结构化摘要，保证功能可用性。
    """
    llm_config, _ = _read_llm_runtime_config(db, config_id)
    skills, _ = _read_skill_registry(db)
    enabled_skills = _filter_enabled_skills_for_agent(agent_key, skills)
    scope = _resolve_agent_scope(agent_key)
    agent_title = str(scope.get("title") or "Agent")
    provider = str(llm_config.get("provider") or "gemini")
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    if not enabled_skills:
        return OpsAgentQueryResponse(
            answer=f"当前没有启用任何 Skill，{agent_title} 暂时无法获取分析上下文。",
            mode="fallback",
            provider=provider,
            model=model,
            llm_enabled=bool(llm_config["enabled"] and llm_config["has_api_key"]),
            used_skills=[],
            notice=f"请先在 Skill 管理中启用 {agent_title} 作用域内的至少一个只读 Skill。",
            suggestions=["前往 Skill 管理，启用相关 Skill 后再重试。"],
            context_blocks=[],
        )
    selected_skill_keys = _select_focus_skill_keys(message, enabled_skills, requested_skill_keys)
    context_blocks = _build_context_blocks(db, selected_skill_keys)
    suggestions = _build_follow_up_suggestions(context_blocks)

    mode = "fallback"
    notice = ""
    answer = _build_local_answer(message, context_blocks)
    if use_llm and llm_config["enabled"] and llm_config["has_api_key"]:
        try:
            llm_answer = await _run_llm_analysis(llm_config, message, context_blocks)
            if llm_answer:
                answer = llm_answer
                mode = "llm"
            else:
                notice = "模型返回为空，已自动回退到本地结构化摘要。"
        except Exception as exc:
            notice = f"模型调用失败，已自动回退：{_truncate_text(str(exc), 120)}"
    elif use_llm and not llm_config["has_api_key"]:
        notice = "当前未配置可用模型密钥，已使用本地结构化摘要。"

    return OpsAgentQueryResponse(
        answer=answer,
        mode=mode,
        provider=provider,
        model=model,
        llm_enabled=bool(llm_config["enabled"] and llm_config["has_api_key"]),
        used_skills=selected_skill_keys,
        notice=notice,
        suggestions=suggestions,
        context_blocks=[
            OpsAgentContextBlock(
                key=item["key"],
                title=item["title"],
                summary=item["summary"],
                items=list(item.get("items", [])),
            )
            for item in context_blocks
        ],
    )


def _evaluate_orchestration_preview(
    policy: dict[str, Any],
    payload: OrchestrationPreviewRequest,
) -> tuple[bool, bool, list[str], list[str]]:
    """执行编排前置预检评估。

    返回：
    1. 是否允许执行；
    2. 是否需要审批；
    3. 拒绝/提示原因；
    4. 命中的高风险动作列表。
    """
    reasons: list[str] = []
    policy_high_risk = set(list(policy.get("high_risk_actions") or []))
    requested_actions = {str(item).strip() for item in payload.action_keys if str(item).strip()}
    matched_high_risk_actions = sorted(list(policy_high_risk.intersection(requested_actions)))

    over_budget = bool(
        policy.get("budget_guard_enabled")
        and payload.estimated_cost_index > _safe_float(policy.get("max_budget_cost_index"), 0.0)
    )
    if over_budget:
        reasons.append(
            "估算成本超过预算阈值："
            f"{payload.estimated_cost_index:.2f} > {_safe_float(policy.get('max_budget_cost_index'), 0.0):.2f}"
        )

    over_steps = payload.expected_steps > _safe_int(policy.get("max_steps"), 1)
    if over_steps:
        reasons.append(
            f"预计步骤超限：{payload.expected_steps} > {_safe_int(policy.get('max_steps'), 1)}"
        )

    overtime = payload.expected_duration_seconds > _safe_int(policy.get("timeout_seconds"), 1)
    if overtime:
        reasons.append(
            "预计执行时长超过超时阈值："
            f"{payload.expected_duration_seconds}s > {_safe_int(policy.get('timeout_seconds'), 1)}s"
        )

    ticket_missing = bool(policy.get("ticket_required")) and not payload.has_ticket
    if ticket_missing:
        reasons.append("缺少工单号，当前策略要求高风险或变更类编排必须绑定工单。")

    requires_approval = bool(policy.get("approval_enabled")) and bool(matched_high_risk_actions)
    if requires_approval:
        reasons.append(
            "命中高风险动作，需要审批后执行："
            + "，".join(matched_high_risk_actions)
        )

    # 二期骨架阶段：只要触发预算/步骤/超时硬阈值就阻断；审批场景允许继续（但标记为 requires_approval）。
    blocked_reasons = [item for item in reasons if "超过" in item or "超限" in item]
    allowed = len(blocked_reasons) == 0
    return allowed, requires_approval, reasons, matched_high_risk_actions


def _build_ops_agent_runtime(db: Session, agent_id: str = "ops") -> OpsAgentRuntimeResponse:
    """构建 Agent 首页所需的安全运行态信息。"""
    return _build_agent_runtime(db, agent_id)


async def _execute_ops_agent_query(
    db: Session,
    message: str,
    use_llm: bool,
    requested_skill_keys: list[str] | None = None,
    config_id: str | None = None,
    agent_id: str = "ops"
) -> OpsAgentQueryResponse:
    """执行 Agent 只读查询。"""
    return await _execute_agent_query(db, message, use_llm, agent_id, requested_skill_keys, config_id)


@router.get(
    "/configs",
    response_model=LLMConfigListResponse,
    dependencies=[Depends(require_permissions("module.ai.llm"))],
)
def list_llm_configs(db: Session = Depends(get_db)) -> LLMConfigListResponse:
    """读取全部 LLM 配置列表。"""
    items, active_id = _load_llm_config_store(db)
    return LLMConfigListResponse(
        items=[_serialize_llm_config_item(e, active_id) for e in items],
        active_id=active_id,
    )


@router.post(
    "/configs",
    response_model=LLMConfigListResponse,
    dependencies=[Depends(require_permissions("action.ai.llm.manage"))],
)
def create_llm_config(
    payload: LLMConfigSaveRequest,
    db: Session = Depends(get_db),
) -> LLMConfigListResponse:
    """新增一条 LLM 配置。首条配置或显式 activate 时设为启用。"""
    provider = _ensure_supported_provider(payload.provider)
    base_url = _resolve_base_url_for_provider(provider, payload.base_url)
    model = payload.model.strip() or _default_model_for_provider(provider)
    now = datetime.utcnow().isoformat()
    items, active_id = _load_llm_config_store(db)
    new_id = uuid.uuid4().hex
    items.append(
        {
            "id": new_id,
            "name": payload.name.strip()[:60] or model,
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": encrypt_secret(payload.api_key) if payload.api_key.strip() else "",
            "temperature": payload.temperature,
            "max_output_tokens": payload.max_output_tokens,
            "timeout_seconds": payload.timeout_seconds,
            "system_prompt": payload.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT,
            "created_at": now,
            "updated_at": now,
        }
    )
    if payload.activate or not active_id:
        active_id = new_id
    _save_llm_config_store(db, items, active_id)
    return LLMConfigListResponse(
        items=[_serialize_llm_config_item(e, active_id) for e in items],
        active_id=active_id,
    )


@router.put(
    "/configs/{config_id}",
    response_model=LLMConfigListResponse,
    dependencies=[Depends(require_permissions("action.ai.llm.manage"))],
)
def update_llm_config_item(
    config_id: str,
    payload: LLMConfigSaveRequest,
    db: Session = Depends(get_db),
) -> LLMConfigListResponse:
    """编辑一条 LLM 配置。api_key 留空表示沿用原密钥。"""
    provider = _ensure_supported_provider(payload.provider)
    base_url = _resolve_base_url_for_provider(provider, payload.base_url)
    model = payload.model.strip() or _default_model_for_provider(provider)
    items, active_id = _load_llm_config_store(db)
    target = next((e for e in items if e["id"] == config_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该 LLM 配置")
    target["name"] = payload.name.strip()[:60] or model
    target["provider"] = provider
    target["base_url"] = base_url
    target["model"] = model
    if payload.api_key.strip():
        target["api_key"] = encrypt_secret(payload.api_key)
    target["temperature"] = payload.temperature
    target["max_output_tokens"] = payload.max_output_tokens
    target["timeout_seconds"] = payload.timeout_seconds
    target["system_prompt"] = payload.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    target["updated_at"] = datetime.utcnow().isoformat()
    if payload.activate:
        active_id = config_id
    _save_llm_config_store(db, items, active_id)
    return LLMConfigListResponse(
        items=[_serialize_llm_config_item(e, active_id) for e in items],
        active_id=active_id,
    )


@router.post(
    "/configs/{config_id}/activate",
    response_model=LLMConfigListResponse,
    dependencies=[Depends(require_permissions("action.ai.llm.manage"))],
)
def activate_llm_config(
    config_id: str,
    db: Session = Depends(get_db),
) -> LLMConfigListResponse:
    """把指定配置设为启用（单选）。"""
    items, _ = _load_llm_config_store(db)
    if not any(e["id"] == config_id for e in items):
        raise HTTPException(status_code=404, detail="未找到该 LLM 配置")
    _save_llm_config_store(db, items, config_id)
    return LLMConfigListResponse(
        items=[_serialize_llm_config_item(e, config_id) for e in items],
        active_id=config_id,
    )


@router.delete(
    "/configs/{config_id}",
    response_model=LLMConfigListResponse,
    dependencies=[Depends(require_permissions("action.ai.llm.manage"))],
)
def delete_llm_config(
    config_id: str,
    db: Session = Depends(get_db),
) -> LLMConfigListResponse:
    """删除一条 LLM 配置。若删除的是启用项，则自动启用列表首条。"""
    items, active_id = _load_llm_config_store(db)
    if not any(e["id"] == config_id for e in items):
        raise HTTPException(status_code=404, detail="未找到该 LLM 配置")
    items = [e for e in items if e["id"] != config_id]
    if active_id == config_id:
        active_id = items[0]["id"] if items else ""
    _save_llm_config_store(db, items, active_id)
    return LLMConfigListResponse(
        items=[_serialize_llm_config_item(e, active_id) for e in items],
        active_id=active_id,
    )


@router.post(
    "/config/test",
    response_model=LLMConfigTestResponse,
    dependencies=[Depends(require_permissions("action.ai.llm.manage"))],
)
async def test_llm_config(
    payload: LLMConfigTestRequest,
    db: Session = Depends(get_db),
) -> LLMConfigTestResponse:
    """执行 LLM 连通性检测。

    安全边界：
    1. 本接口不会持久化临时 API Key；
    2. 仅执行最小化只读探测，不会调用任何业务写操作；
    3. 返回值不包含敏感密钥，只返回状态、耗时和脱敏后的错误信息。
    """
    provider = _ensure_supported_provider(payload.provider)
    current_config, _ = _read_llm_runtime_config(db)
    current_provider = str(current_config.get("provider") or "gemini")
    fallback_api_key = (
        str(current_config.get("api_key") or "")
        if provider == current_provider
        else _resolve_env_api_key_for_provider(provider)
    )
    runtime_config = _build_llm_runtime_config_from_payload(
        payload,
        fallback_api_key,
    )
    return await _run_llm_health_check(runtime_config)


@router.get(
    "/skills",
    response_model=SkillRegistryResponse,
    dependencies=[Depends(require_permissions("module.ai.skills"))],
)
def get_skill_registry(db: Session = Depends(get_db)) -> SkillRegistryResponse:
    """读取 Skill Registry。"""
    skills, updated_at = _read_skill_registry(db)
    return SkillRegistryResponse(
        skills=[SkillRegistryItem(**item) for item in skills],
        updated_at=updated_at,
    )


@router.put(
    "/skills",
    response_model=SkillRegistryResponse,
    dependencies=[Depends(require_permissions("action.ai.skills.manage"))],
)
def update_skill_registry(
    payload: SkillRegistryUpdate,
    db: Session = Depends(get_db),
) -> SkillRegistryResponse:
    """保存 Skill Registry 开关。"""
    enabled_map = {item.key: item.enabled for item in payload.skills}
    unknown_keys = [key for key in enabled_map if key not in {item["key"] for item in DEFAULT_SKILL_CATALOG}]
    if unknown_keys:
        raise HTTPException(status_code=400, detail=f"未知 Skill：{', '.join(unknown_keys)}")

    saved_row = _save_system_config(
        db,
        AI_SKILL_CONFIG_KEY,
        {
            "skills": {
                item["key"]: {
                    "enabled": bool(enabled_map.get(item["key"], item["enabled"])),
                }
                for item in DEFAULT_SKILL_CATALOG
            }
        },
    )
    skills, _ = _read_skill_registry(db)
    return SkillRegistryResponse(
        skills=[SkillRegistryItem(**item) for item in skills],
        updated_at=saved_row.updated_at,
    )


@router.get(
    "/orchestration/policy",
    response_model=OrchestrationPolicyResponse,
    dependencies=[Depends(require_permissions("module.ai.orchestration"))],
)
def get_orchestration_policy(
    db: Session = Depends(get_db),
) -> OrchestrationPolicyResponse:
    """读取编排前置策略。"""
    policy, updated_at = _read_orchestration_policy(db)
    return _serialize_orchestration_policy(policy, updated_at)


@router.put(
    "/orchestration/policy",
    response_model=OrchestrationPolicyResponse,
    dependencies=[Depends(require_permissions("action.ai.orchestration.manage"))],
)
def update_orchestration_policy(
    payload: OrchestrationPolicyUpdate,
    db: Session = Depends(get_db),
) -> OrchestrationPolicyResponse:
    """保存编排前置策略。"""
    saved_row = _save_system_config(
        db,
        AI_ORCHESTRATION_POLICY_KEY,
        {
            "approval_enabled": payload.approval_enabled,
            "budget_guard_enabled": payload.budget_guard_enabled,
            "max_budget_cost_index": payload.max_budget_cost_index,
            "timeout_seconds": payload.timeout_seconds,
            "max_steps": payload.max_steps,
            "required_approvers": payload.required_approvers,
            "ticket_required": payload.ticket_required,
            # 高风险动作清单由后端控制，避免前端任意放宽策略。
            "high_risk_actions": list(DEFAULT_ORCHESTRATION_POLICY["high_risk_actions"]),
        },
    )
    policy, _ = _read_orchestration_policy(db)
    return _serialize_orchestration_policy(policy, saved_row.updated_at)


@router.post(
    "/orchestration/preview",
    response_model=OrchestrationPreviewResponse,
    dependencies=[Depends(require_permissions("module.ai.orchestration"))],
)
def preview_orchestration_execution(
    payload: OrchestrationPreviewRequest,
    db: Session = Depends(get_db),
) -> OrchestrationPreviewResponse:
    """执行编排前置预检（不触发真实执行）。"""
    policy, updated_at = _read_orchestration_policy(db)
    allowed, requires_approval, reasons, matched_actions = _evaluate_orchestration_preview(
        policy,
        payload,
    )
    serialized_policy = _serialize_orchestration_policy(policy, updated_at)
    return OrchestrationPreviewResponse(
        allowed=allowed,
        requires_approval=requires_approval,
        reasons=reasons or ["符合当前策略，可进入执行队列。"],
        matched_high_risk_actions=matched_actions,
        policy=serialized_policy,
    )


@router.get(
    "/{agent_id}/runtime",
    response_model=OpsAgentRuntimeResponse,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def get_ops_agent_runtime(
    agent_id: str,
    db: Session = Depends(get_db),
) -> OpsAgentRuntimeResponse:
    """读取 Agent 的安全运行态摘要。"""
    return _build_ops_agent_runtime(db, agent_id)


@router.post(
    "/{agent_id}/query",
    response_model=OpsAgentQueryResponse,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
async def query_ops_agent(
    agent_id: str,
    payload: OpsAgentQueryRequest,
    db: Session = Depends(get_db),
) -> OpsAgentQueryResponse:
    """执行 Agent 只读分析。"""
    return await _execute_ops_agent_query(
        db, payload.message.strip(), payload.use_llm, list(payload.focus_skills or []), payload.config_id, agent_id
    )


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """组装一条 SSE 事件帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_text_for_typewriter(text: str, size: int = 24):
    """把整段文本切成小块，模拟打字机逐块输出（用于回退场景）。"""
    for i in range(0, len(text), size):
        yield text[i : i + size]


@router.post(
    "/{agent_id}/query/stream",
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
async def query_ops_agent_stream(
    agent_id: str,
    payload: OpsAgentQueryRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """执行 Ops Agent 只读分析（SSE 流式）。

    事件协议：
    1. `meta`  —— 首帧，携带 provider/model/used_skills/context_blocks/notice；
    2. `delta` —— 多帧，`{"text": "增量文本"}`，前端追加渲染；
    3. `done`  —— 末帧，`{"mode": "llm|fallback", "answer": "完整答案", "notice": ""}`；
    4. `error` —— 异常帧，`{"message": "错误信息"}`（仍会补一个 done 收尾）。
    """
    message = payload.message.strip()
    use_llm = payload.use_llm
    requested_skill_keys = list(payload.focus_skills or [])

    # 1) 全部 DB 相关准备工作在流式开始前完成，避免会话生命周期问题。
    llm_config, _ = _read_llm_runtime_config(db, payload.config_id)
    skills, _ = _read_skill_registry(db)
    enabled_skills = _filter_enabled_skills_for_agent(agent_id, skills)
    scope = _resolve_agent_scope(agent_id)
    agent_title = str(scope.get("title") or "Agent")
    provider = str(llm_config.get("provider") or "gemini")
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    llm_ready = bool(llm_config["enabled"] and llm_config["has_api_key"])

    if not enabled_skills:
        selected_skill_keys: list[str] = []
        context_blocks: list[dict[str, Any]] = []
        local_answer = f"当前没有启用任何 Skill，{agent_title} 暂时无法获取分析上下文。"
        prep_notice = f"请先在 Skill 管理中启用 {agent_title} 作用域内的至少一个只读 Skill。"
    else:
        selected_skill_keys = _select_focus_skill_keys(message, enabled_skills, requested_skill_keys)
        context_blocks = _build_context_blocks(db, selected_skill_keys)
        local_answer = _build_local_answer(message, context_blocks)
        prep_notice = ""

    suggestions = _build_follow_up_suggestions(context_blocks) if context_blocks else []
    meta_payload = {
        "provider": provider,
        "model": model,
        "llm_enabled": llm_ready,
        "used_skills": selected_skill_keys,
        "suggestions": suggestions,
        "context_blocks": [
            {
                "key": item["key"],
                "title": item["title"],
                "summary": item["summary"],
                "items": list(item.get("items", [])),
            }
            for item in context_blocks
        ],
    }

    can_stream_llm = (
        use_llm
        and llm_ready
        and bool(enabled_skills)
        and provider in {"openai_compatible", "anthropic"}
    )

    def _generate():
        yield _sse_event("meta", meta_payload)

        if not enabled_skills:
            for piece in _chunk_text_for_typewriter(local_answer):
                yield _sse_event("delta", {"text": piece})
            yield _sse_event(
                "done",
                {"mode": "fallback", "answer": local_answer, "notice": prep_notice},
            )
            return

        if can_stream_llm:
            collected: list[str] = []
            try:
                for piece in _stream_llm_analysis(llm_config, message, context_blocks, getattr(payload, "history", None)):
                    collected.append(piece)
                    yield _sse_event("delta", {"text": piece})
            except Exception as exc:  # noqa: BLE001 - 失败回退到本地摘要
                notice = f"模型调用失败，已自动回退：{_truncate_text(str(exc), 120)}"
                yield _sse_event("error", {"message": notice})
                for piece in _chunk_text_for_typewriter(local_answer):
                    yield _sse_event("delta", {"text": piece})
                yield _sse_event(
                    "done",
                    {"mode": "fallback", "answer": local_answer, "notice": notice},
                )
                return

            full = "".join(collected).strip()
            if full:
                yield _sse_event("done", {"mode": "llm", "answer": full, "notice": ""})
            else:
                for piece in _chunk_text_for_typewriter(local_answer):
                    yield _sse_event("delta", {"text": piece})
                yield _sse_event(
                    "done",
                    {
                        "mode": "fallback",
                        "answer": local_answer,
                        "notice": "模型返回为空，已自动回退到本地结构化摘要。",
                    },
                )
            return

        # 不支持原生流式的 Provider（如 Gemini）：一次性推理后分块输出。
        # 先立刻吐一帧占位，避免长调用把 SSE 堵住、前端一直「思考中」。
        if use_llm and llm_ready and enabled_skills and provider not in {"openai_compatible", "anthropic"}:
            yield _sse_event("delta", {"text": "正在分析平台上下文…\n\n"})
            try:
                prompt = _build_llm_prompt(message, context_blocks, getattr(payload, "history", None))
                api_key = str(llm_config.get("api_key") or "").strip()
                model = str(llm_config.get("model") or _default_model_for_provider(provider))
                system_prompt = str(llm_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT)
                temperature = _safe_float(llm_config.get("temperature"), 0.2)
                max_output_tokens = _safe_int(llm_config.get("max_output_tokens"), 1200)
                # 同步推理包超时，防止 genai SDK 挂死把整条流拖没
                import concurrent.futures as _fut

                with _fut.ThreadPoolExecutor(max_workers=1) as _pool:
                    full = _pool.submit(
                        _generate_gemini_content,
                        api_key,
                        model,
                        prompt,
                        system_prompt,
                        temperature,
                        max_output_tokens,
                    ).result(timeout=_safe_int(llm_config.get("timeout_seconds"), 30) or 30)
                full = (full or "").strip()
                if full:
                    # 清掉占位行，换成真正的模型回答
                    yield _sse_event("delta", {"text": full})
                    yield _sse_event("done", {"mode": "llm", "answer": full, "notice": ""})
                    return
                notice = "模型返回为空，已回退到本地结构化摘要。"
            except Exception as exc:  # noqa: BLE001
                notice = f"模型调用失败，已回退：{_truncate_text(str(exc), 120)}"
                yield _sse_event("error", {"message": notice})
            for piece in _chunk_text_for_typewriter(local_answer):
                yield _sse_event("delta", {"text": piece})
            yield _sse_event(
                "done",
                {"mode": "fallback", "answer": local_answer, "notice": notice},
            )
            return

        # 不满足流式条件：本地结构化摘要分块输出。
        notice = ""
        if use_llm and not llm_config["has_api_key"]:
            notice = "当前未配置可用模型密钥，已使用本地结构化摘要。"
        elif use_llm and llm_ready and provider not in {"openai_compatible", "anthropic"}:
            notice = "当前模型提供商暂不支持流式，已使用本地结构化摘要。"
        for piece in _chunk_text_for_typewriter(local_answer):
            yield _sse_event("delta", {"text": piece})
        yield _sse_event(
            "done",
            {"mode": "fallback", "answer": local_answer, "notice": notice},
        )

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _extract_terminal_command(text: str) -> str:
    """从模型输出中提炼纯净命令：剥离代码块围栏与解释性废话行。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    block = re.search(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", raw, re.IGNORECASE)
    if block and block.group(1).strip():
        raw = block.group(1).strip()
    kept: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            kept.append(stripped)
            continue
        if re.match(r"^(?:好的|以下|总结|说明|解释|注意|请注意|你可以|步骤|例如|如需|提示|功能|首先|其次|最后)", stripped):
            continue
        kept.append(stripped)
        if len(kept) >= 3:
            break
    return "\n".join(kept[:3]).strip()


async def _run_llm_raw_prompt(
    llm_config: dict[str, Any],
    system_prompt: str,
    prompt: str,
) -> str:
    """直接以给定 system_prompt + 裸 prompt 调用 LLM，不注入任何平台上下文。"""
    provider = str(llm_config.get("provider") or "")
    api_key = str(llm_config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("未配置可用模型 API Key")

    timeout_seconds = _safe_int(llm_config.get("timeout_seconds"), 30)
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    temperature = _safe_float(llm_config.get("temperature"), 0.2)
    max_output_tokens = min(_safe_int(llm_config.get("max_output_tokens"), 1200), 512)

    if provider == "gemini":
        return await asyncio.wait_for(
            asyncio.to_thread(
                _generate_gemini_content,
                api_key, model, prompt, system_prompt, temperature, max_output_tokens,
            ),
            timeout=timeout_seconds,
        )
    if provider == "openai_compatible":
        return await asyncio.wait_for(
            asyncio.to_thread(
                _generate_openai_compatible_content,
                api_key, str(llm_config.get("base_url") or DEFAULT_OPENAI_COMPAT_BASE_URL),
                model, prompt, system_prompt, temperature, max_output_tokens, timeout_seconds,
            ),
            timeout=timeout_seconds,
        )
    if provider == "anthropic":
        return await asyncio.wait_for(
            asyncio.to_thread(
                _generate_anthropic_content,
                api_key, str(llm_config.get("base_url") or DEFAULT_ANTHROPIC_BASE_URL),
                model, prompt, system_prompt, temperature, max_output_tokens, timeout_seconds,
            ),
            timeout=timeout_seconds,
        )
    raise RuntimeError(f"暂不支持的模型提供商：{provider}")


@router.post(
    "/command/generate",
    response_model=TerminalCommandResponse,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
async def generate_terminal_command(
    payload: TerminalCommandRequest,
    db: Session = Depends(get_db),
) -> TerminalCommandResponse:
    """终端 AI 命令助手：将自然语言运维需求翻译成 Linux 命令。

    与只读分析 Agent 完全隔离——不注入任何资产/监控上下文，使用专用命令生成
    系统提示词。未配置模型密钥时返回明确提示（不做资产摘要兜底）。
    生成的命令仅供前端填入终端供人工 review，后端不执行。
    """
    llm_config, _ = _read_llm_runtime_config(db, payload.config_id)
    provider = str(llm_config.get("provider") or "gemini")
    model = str(llm_config.get("model") or _default_model_for_provider(provider))
    llm_ready = bool(llm_config["enabled"] and llm_config["has_api_key"])

    if not llm_ready:
        return TerminalCommandResponse(
            ok=False,
            command="",
            mode="unavailable",
            provider=provider,
            model=model,
            notice="当前未配置可用模型密钥，AI 命令助手暂不可用。请先在 AI 配置中启用模型。",
        )

    os_hint = (payload.os_hint or "Linux").strip() or "Linux"
    prompt = f"目标系统：{os_hint}\n运维需求：{payload.requirement.strip()}"
    try:
        raw = await _run_llm_raw_prompt(llm_config, TERMINAL_COMMAND_SYSTEM_PROMPT, prompt)
    except Exception as exc:  # noqa: BLE001 - 失败明确回报，不做资产兜底
        return TerminalCommandResponse(
            ok=False,
            command="",
            mode="error",
            provider=provider,
            model=model,
            notice=f"模型调用失败：{_truncate_text(str(exc), 120)}",
        )

    command = _extract_terminal_command(raw)
    if not command:
        return TerminalCommandResponse(
            ok=False,
            command="",
            mode="empty",
            provider=provider,
            model=model,
            notice="模型未能生成有效命令，请换一种描述再试。",
        )
    return TerminalCommandResponse(
        ok=True,
        command=command,
        mode="llm",
        provider=provider,
        model=model,
        notice="",
    )


@router.get(
    "/observability/stats",
    response_model=AIObservabilityStatsResponse,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def get_ai_observability_stats(db: Session = Depends(get_db)) -> AIObservabilityStatsResponse:
    """读取 AI 观测大盘核心指标——全部来自真实表（ai_chat_sessions / ai_chat_messages），无 mock。

    说明：created_at 存为 "%Y-%m-%d %H:%M:%S" 字符串，按日期前缀聚合即可，无需类型转换。
    """
    total_sessions = db.query(AIChatSession).count()
    total_messages = db.query(AIChatMessage).count()
    user_messages = db.query(AIChatMessage).filter(AIChatMessage.role == "user").count()
    assistant_messages = db.query(AIChatMessage).filter(AIChatMessage.role == "assistant").count()
    active_users = db.query(AIChatSession.user_id).distinct().count()

    # 复杂逻辑中文注释：
    # 消息 created_at 由 _now_str() 以本地时间写入，这里必须用同一时区的
    # "今天"做前缀匹配——早期用 utcnow() 导致每天 0:00-08:00（UTC+8）期间
    # 今日消息数与趋势末位被错归零。
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_messages = (
        db.query(AIChatMessage)
        .filter(AIChatMessage.created_at.like(f"{today_str}%"))
        .count()
    )

    # 近 7 天每日消息数（含今天，按日期前缀计数；与写入方同为本地时区）。
    chat_trend_7d: list[int] = []
    for offset in range(6, -1, -1):
        day_str = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        day_count = (
            db.query(AIChatMessage)
            .filter(AIChatMessage.created_at.like(f"{day_str}%"))
            .count()
        )
        chat_trend_7d.append(day_count)

    return AIObservabilityStatsResponse(
        total_sessions=total_sessions,
        total_messages=total_messages,
        today_messages=today_messages,
        active_users=active_users,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        chat_trend_7d=chat_trend_7d,
    )


@router.get(
    "/observability/audit",
    response_model=AIObservabilityAuditResponse,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def get_ai_observability_audit(db: Session = Depends(get_db)) -> AIObservabilityAuditResponse:
    """读取 AI 观测大盘：调用与拦截审计日志 (Phase 3 MVP)"""
    # 尝试从真实数据库读取最近的审计操作，识别其风险等级并组装。
    audits = db.query(AuditLog).order_by(AuditLog.time.desc()).limit(15).all()
    logs: list[AIAuditLogItem] = []
    
    for item in audits:
        action_lower = str(item.action).lower()
        # 简单规则：包含危险关键词即视为高危操作
        is_high_risk = any(kw in action_lower for kw in ["delete", "remove", "exec", "power", "restart", "revoke"])
        
        logs.append(AIAuditLogItem(
            id=str(item.id),
            time=item.time.strftime('%m-%d %H:%M') if item.time else '-',
            user=str(item.user or "system"),
            agent="Ops Agent",  # 当前审计表暂未记录来源 Agent，先默认回退
            intent=str(item.action),
            risk="high" if is_high_risk else "low",
            status="approved" if is_high_risk else "success"
        ))
        
    if not logs:
        # 无真实审计数据时返回空列表，不再编造演示数据。
        return AIObservabilityAuditResponse(logs=[])

    return AIObservabilityAuditResponse(logs=logs)


# ---------------------------------------------------------------------------
# AI 会话持久化 (任务2: 会话保存记录 / 新增 / 删除)
# ---------------------------------------------------------------------------
# 设计要点:
# 1. 会话与消息存于独立表(ai_chat_sessions / ai_chat_messages),与 system_configs 配置分离;
# 2. 会话归属当前登录用户,跨用户不可见/不可删,避免越权;
# 3. 仅做存取,不在此处调用模型;前端发消息仍走 /ops-agent/query,再调用本接口落库。


class ChatMessageItem(BaseModel):
    id: int
    role: str
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ChatSessionItem(BaseModel):
    session_id: str
    title: str
    agent_id: str = "ops"
    message_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class ChatSessionDetail(ChatSessionItem):
    messages: list[ChatMessageItem] = Field(default_factory=list)


class ChatSessionListResponse(BaseModel):
    sessions: list[ChatSessionItem] = Field(default_factory=list)


class ChatSessionCreateRequest(BaseModel):
    title: str = Field(default="新会话", max_length=200)
    agent_id: str = Field(default="ops", max_length=50)


class ChatSessionUpdateRequest(BaseModel):
    title: str = Field(..., max_length=200)


class ChatMessageAppendRequest(BaseModel):
    role: str = Field(..., max_length=20)
    content: str = Field(..., max_length=20000)
    meta: dict[str, Any] = Field(default_factory=dict)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _serialize_chat_session(row: AIChatSession) -> ChatSessionItem:
    return ChatSessionItem(
        session_id=row.session_id,
        title=row.title or "新会话",
        agent_id=row.agent_id or "ops",
        message_count=int(row.message_count or 0),
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
    )


def _serialize_chat_message(row: AIChatMessage) -> ChatMessageItem:
    return ChatMessageItem(
        id=int(row.id),
        role=row.role or "user",
        content=row.content or "",
        meta=_safe_load_json_dict(row.meta_json or ""),
        created_at=row.created_at or "",
    )


def _get_owned_session(db: Session, session_id: str, user: User) -> AIChatSession:
    row = (
        db.query(AIChatSession)
        .filter(AIChatSession.session_id == session_id, AIChatSession.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    return row


@router.get(
    "/sessions",
    response_model=ChatSessionListResponse,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def list_chat_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSessionListResponse:
    """列出当前用户的会话,按最后更新时间倒序。"""
    rows = (
        db.query(AIChatSession)
        .filter(AIChatSession.user_id == current_user.id)
        .order_by(AIChatSession.updated_at.desc(), AIChatSession.id.desc())
        .all()
    )
    return ChatSessionListResponse(sessions=[_serialize_chat_session(row) for row in rows])


@router.post(
    "/sessions",
    response_model=ChatSessionItem,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def create_chat_session(
    payload: ChatSessionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSessionItem:
    """新增一个会话。"""
    now = _now_str()
    row = AIChatSession(
        session_id=uuid.uuid4().hex,
        user_id=current_user.id,
        title=(payload.title or "新会话").strip()[:200] or "新会话",
        agent_id=(payload.agent_id or "ops").strip()[:50] or "ops",
        message_count=0,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_chat_session(row)


@router.get(
    "/sessions/{session_id}",
    response_model=ChatSessionDetail,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def get_chat_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSessionDetail:
    """读取单个会话及其消息。"""
    row = _get_owned_session(db, session_id, current_user)
    messages = (
        db.query(AIChatMessage)
        .filter(AIChatMessage.session_id == session_id)
        .order_by(AIChatMessage.id.asc())
        .all()
    )
    base = _serialize_chat_session(row)
    return ChatSessionDetail(
        **base.model_dump(),
        messages=[_serialize_chat_message(m) for m in messages],
    )


@router.put(
    "/sessions/{session_id}",
    response_model=ChatSessionItem,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def rename_chat_session(
    session_id: str,
    payload: ChatSessionUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatSessionItem:
    """重命名会话。"""
    row = _get_owned_session(db, session_id, current_user)
    row.title = (payload.title or "").strip()[:200] or "新会话"
    row.updated_at = _now_str()
    db.commit()
    db.refresh(row)
    return _serialize_chat_session(row)


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def delete_chat_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """删除会话及其全部消息。"""
    row = _get_owned_session(db, session_id, current_user)
    db.query(AIChatMessage).filter(AIChatMessage.session_id == session_id).delete(
        synchronize_session=False
    )
    db.delete(row)
    db.commit()
    return {"ok": True, "session_id": session_id}


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatMessageItem,
    dependencies=[Depends(require_permissions("module.ai.ops"))],
)
def append_chat_message(
    session_id: str,
    payload: ChatMessageAppendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatMessageItem:
    """向会话追加一条消息(user 或 assistant),并维护计数与标题。"""
    session = _get_owned_session(db, session_id, current_user)
    role = (payload.role or "user").strip()[:20]
    if role not in {"user", "assistant"}:
        raise HTTPException(status_code=400, detail="role 仅支持 user/assistant")
    now = _now_str()
    try:
        meta_json = json.dumps(payload.meta or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        meta_json = "{}"
    message = AIChatMessage(
        session_id=session_id,
        role=role,
        content=payload.content or "",
        meta_json=meta_json,
        created_at=now,
    )
    db.add(message)
    session.message_count = int(session.message_count or 0) + 1
    session.updated_at = now
    # 首条用户消息自动作为会话标题(若仍是默认名)
    if role == "user" and (not session.title or session.title == "新会话"):
        session.title = (payload.content or "").strip().splitlines()[0][:40] or "新会话"
    db.commit()
    db.refresh(message)
    return _serialize_chat_message(message)


