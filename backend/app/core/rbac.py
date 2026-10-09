"""
RBAC 权限模型定义。

设计目标：
1. 为菜单、页面和后端接口提供统一的模块级 + 动作级权限键；
2. 使用内置角色模板作为默认策略，避免当前原型阶段引入过重的关系表设计；
3. 允许通过用户级 JSON 覆盖实现“在角色模板基础上的个性化授权”。
"""
from __future__ import annotations

import json
from typing import Any

from app.db.database import SessionLocal
from app.models.system_config import SystemConfig


MODULE_PERMISSION_CATALOG: list[dict[str, str]] = [
    {
        "key": "module.dashboard",
        "group": "工作台",
        "title": "概览大盘",
        "description": "查看首页概览与总览指标。",
        "path": "/dashboard",
    },
    {
        "key": "module.assets.hosts",
        "group": "资产管理",
        "title": "主机管理",
        "description": "查看并维护主机资产、SSH 校验、配置采集与监控纳管。",
        "path": "/assets/hosts",
    },
    {
        "key": "module.assets.vms",
        "group": "资产管理",
        "title": "虚机管理",
        "description": "查看并管理 PVE 虚拟机与模板创建。",
        "path": "/assets/vms",
    },
    {
        "key": "module.assets.clusters",
        "group": "资产管理",
        "title": "集群管理",
        "description": "查看并维护集群接入能力。",
        "path": "/assets/clusters",
    },
    {
        "key": "module.assets.domains",
        "group": "资产管理",
        "title": "域名管理",
        "description": "查看域名并管理 DNS 解析记录。",
        "path": "/assets/domains",
    },
    {
        "key": "module.assets.certificates",
        "group": "资产管理",
        "title": "证书管理",
        "description": "管理 ACME 证书、下载、续签与吊销。",
        "path": "/assets/certificates",
    },
    {
        "key": "module.assets.credentials",
        "group": "资产管理",
        "title": "凭证管理",
        "description": "管理 SSH Key、AK/SK、Token 等敏感凭证。",
        "path": "/assets/credentials",
    },
    {
        "key": "module.apps.list",
        "group": "应用中心",
        "title": "应用列表",
        "description": "查看应用中心列表。",
        "path": "/apps/list",
    },
    {
        "key": "module.apps.deployment",
        "group": "应用中心",
        "title": "发布记录",
        "description": "查看发布记录页面。",
        "path": "/apps/deployment",
    },
    {
        "key": "module.apps.config",
        "group": "应用中心",
        "title": "配置中心",
        "description": "查看应用配置中心页面。",
        "path": "/apps/config",
    },
    {
        "key": "module.monitoring.live",
        "group": "监控告警",
        "title": "实时大盘",
        "description": "查看监控指标与活动告警。",
        "path": "/monitoring/live",
    },
    {
        "key": "module.monitoring.rules",
        "group": "监控告警",
        "title": "告警规则",
        "description": "管理告警规则与告警路由。",
        "path": "/monitoring/rules",
    },
    {
        "key": "module.monitoring.channels",
        "group": "监控告警",
        "title": "渠道模板",
        "description": "管理通知渠道、进行 Webhook 连通性测试并维护告警消息模板。",
        "path": "/monitoring/channels",
    },
    {
        "key": "module.monitoring.logs",
        "group": "监控告警",
        "title": "日志检索",
        "description": "查询 VictoriaLogs 日志数据。",
        "path": "/monitoring/logs",
    },
    {
        "key": "module.monitoring.metrics",
        "group": "监控告警",
        "title": "指标查询",
        "description": "PromQL 指标查询与指标浏览器。",
        "path": "/monitoring/metrics",
    },
    {
        "key": "module.monitoring.alerts",
        "group": "监控告警",
        "title": "告警中心",
        "description": "查看与认领告警（活动/已恢复/静默）。",
        "path": "/monitoring/alerts",
    },
    {
        "key": "module.monitoring.collector",
        "group": "监控告警",
        "title": "采集 Fleet",
        "description": "查看 Alloy Collector Agent、采集策略和配置发布状态。",
        "path": "/monitoring/collectors",
    },
    {
        "key": "module.ai.ops",
        "group": "AI 平台",
        "title": "智能工作台",
        "description": "通过只读上下文分析资产、监控、告警、证书与安全风险等运维状态。",
        "path": "/ai/ops-agent",
    },
    {
        "key": "module.ai.skills",
        "group": "AI 平台",
        "title": "Skill 管理",
        "description": "管理 AI 平台可用的只读技能清单。",
        "path": "/ai/skills",
    },
    {
        "key": "module.ai.orchestration",
        "group": "AI 平台",
        "title": "编排中心",
        "description": "查看 AI 编排能力规划与后续执行入口。",
        "path": "/ai/orchestration",
    },
    {
        "key": "module.ai.llm",
        "group": "AI 平台",
        "title": "LLM 配置",
        "description": "配置模型提供商、模型参数与系统提示词。",
        "path": "/ai/llm-config",
    },
    {
        "key": "module.tools.terminal",
        "group": "运维工具",
        "title": "Web终端",
        "description": "通过浏览器建立 SSH 终端会话。",
        "path": "/tools/terminal",
    },
    {
        "key": "module.tools.scripts",
        "group": "运维工具",
        "title": "批量模板",
        "description": "维护 Shell、Ansible 模块与 Playbook 批量模板。",
        "path": "/tools/batch-templates",
    },
    {
        "key": "module.settings.system",
        "group": "系统设置",
        "title": "系统管理",
        "description": "管理 PVE、监控连接、功能开关和通知渠道。",
        "path": "/settings/system",
    },
    {
        "key": "module.settings.users",
        "group": "系统设置",
        "title": "用户管理",
        "description": "管理用户角色与模块授权。",
        "path": "/settings/users",
    },
    {
        "key": "module.settings.audit",
        "group": "系统设置",
        "title": "操作审计",
        "description": "查看系统审计记录。",
        "path": "/settings/audit",
    },
    {
        "key": "module.settings.permissions",
        "group": "系统设置",
        "title": "权限管理",
        "description": "查看与编辑角色模板权限（保存仍需用户管理写权限）。",
        "path": "/settings/permissions",
    },
    {
        "key": "module.settings.scheduled",
        "group": "系统设置",
        "title": "定时任务",
        "description": "查看与调整后台周期任务的启停与间隔。",
        "path": "/settings/scheduled-tasks",
    },
    {
        # 个人 IM 是自助能力（配自己的通知渠道），全员默认可用；
        # 不希望某角色自助配置时可在权限管理页显式关闭。
        "key": "module.settings.im",
        "group": "系统设置",
        "title": "个人 IM 通知",
        "description": "绑定个人飞书账号、接收群内 @ 通知（自助能力，默认全员可用）。",
        "path": "/settings/im",
    },
    {
        "key": "module.tools.changes",
        "group": "运维工具",
        "title": "变更工单",
        "description": "查看与处理变更工单。",
        "path": "/tools/changes",
    },
    {
        "key": "module.assets.databases",
        "group": "资产管理",
        "title": "数据库管理",
        "description": "数据库实例、SQL 工单与自助查询（类 Yearning）。",
        "path": "/assets/databases",
    },
    {
        "key": "module.wireguard.manage",
        "group": "资产管理",
        "title": "虚拟组网",
        "description": "管理 WireGuard 虚拟组网、成员对端与配置下发。",
        "path": "/assets/wireguard",
    },
    {
        "key": "module.finance.overview",
        "group": "费用管理",
        "title": "费用总览",
        "description": "查看云账号消费总览、趋势与产品占比。",
        "path": "/finance/overview",
    },
    {
        "key": "module.finance.bills",
        "group": "费用管理",
        "title": "账单明细",
        "description": "查询云账单明细与维度聚合。",
        "path": "/finance/bills",
    },
    {
        "key": "module.finance.accounts",
        "group": "费用管理",
        "title": "云账号",
        "description": "管理费用模块纳管的云账号与同步状态。",
        "path": "/finance/accounts",
    },
]

ACTION_PERMISSION_CATALOG: list[dict[str, str]] = [
    {
        "key": "action.ai.skills.manage",
        "group": "AI 平台",
        "title": "Skill 管理写操作",
        "description": "启停可用 Skill 并更新只读能力清单。",
        "path": "/ai/skills",
    },
    {
        "key": "action.ai.llm.manage",
        "group": "AI 平台",
        "title": "LLM 配置写操作",
        "description": "保存模型配置、提示词与连接参数。",
        "path": "/ai/llm-config",
    },
    {
        "key": "action.ai.orchestration.manage",
        "group": "AI 平台",
        "title": "编排策略写操作",
        "description": "保存编排前置策略（审批、预算、超时与步骤限制）。",
        "path": "/ai/orchestration",
    },
    {
        "key": "action.apps.manage",
        "group": "应用中心",
        "title": "应用目录写操作",
        "description": "创建、编辑、删除应用目录，并维护入口链接与资源绑定。",
        "path": "/apps/list",
    },
    {
        "key": "action.apps.deploy",
        "group": "应用中心",
        "title": "应用发布记录操作",
        "description": "登记外部发布记录，或基于固定执行通道发起发布并写入记录。",
        "path": "/apps/deployment",
    },
    {
        "key": "action.apps.control",
        "group": "应用中心",
        "title": "应用运行控制",
        "description": "对应用运行实例执行状态检查、启动、停止和重启。",
        "path": "/apps/list",
    },
    {
        "key": "action.apps.config.manage",
        "group": "应用中心",
        "title": "应用配置写操作",
        "description": "新增、编辑、删除应用配置项与凭证引用。",
        "path": "/apps/config",
    },
    {
        "key": "action.assets.hosts.manage",
        "group": "资产管理 · 主机",
        "title": "主机写操作",
        "description": "新增、编辑、删除主机，刷新配置与云同步。",
        "path": "/assets/hosts",
    },
    {
        "key": "action.assets.hosts.connect",
        "group": "资产管理 · 主机",
        "title": "主机连接操作",
        "description": "执行 SSH 测试、创建 Web 终端会话与远程命令。",
        "path": "/assets/hosts",
    },
    {
        "key": "action.assets.hosts.monitoring",
        "group": "资产管理 · 主机",
        "title": "主机监控纳管",
        "description": "安装 Alloy Agent，并启停主机监控与日志纳管。",
        "path": "/assets/hosts",
    },
    {
        "key": "action.assets.vms.manage",
        "group": "资产管理 · 虚机",
        "title": "虚机写操作",
        "description": "同步、创建、编辑、删除虚机与模板相关资源。",
        "path": "/assets/vms",
    },
    {
        "key": "action.assets.vms.power",
        "group": "资产管理 · 虚机",
        "title": "虚机电源操作",
        "description": "执行开机、关机、重启等电源控制。",
        "path": "/assets/vms",
    },
    {
        "key": "action.assets.clusters.manage",
        "group": "资产管理 · 集群",
        "title": "集群资源写操作",
        "description": "执行 YAML Apply、资源表单编辑、伸缩、重启与删除等集群写操作。",
        "path": "/assets/clusters",
    },
    {
        "key": "action.assets.domains.manage",
        "group": "资产管理 · 域名",
        "title": "域名写操作",
        "description": "导入域名、维护解析记录、删除域名。",
        "path": "/assets/domains",
    },
    {
        "key": "action.assets.certificates.manage",
        "group": "资产管理 · 证书",
        "title": "证书写操作",
        "description": "签发、续签、修改、吊销、删除和部署证书。",
        "path": "/assets/certificates",
    },
    {
        "key": "action.assets.certificates.download",
        "group": "资产管理 · 证书",
        "title": "证书下载",
        "description": "下载证书与私钥压缩包。",
        "path": "/assets/certificates",
    },
    {
        "key": "action.assets.credentials.manage",
        "group": "资产管理 · 凭证",
        "title": "凭证写操作",
        "description": "新增、编辑、删除凭证。",
        "path": "/assets/credentials",
    },
    {
        "key": "action.assets.credentials.export",
        "group": "资产管理 · 凭证",
        "title": "凭证导出",
        "description": "导出凭证明文内容。",
        "path": "/assets/credentials",
    },
    {
        "key": "action.monitoring.rules.manage",
        "group": "监控告警",
        "title": "告警规则写操作",
        "description": "保存告警规则，由内置告警引擎自动生效。",
        "path": "/monitoring/rules",
    },
    {
        "key": "action.monitoring.channels.manage",
        "group": "监控告警",
        "title": "渠道模板写操作",
        "description": "创建、编辑和删除通知渠道，配置发送模板并执行连通性测试。",
        "path": "/monitoring/channels",
    },
    {
        "key": "action.monitoring.collector.manage",
        "group": "监控告警",
        "title": "采集 Fleet 写操作",
        "description": "生成 Agent 注册令牌、维护采集策略并发布配置。",
        "path": "/monitoring/collectors",
    },
    {
        "key": "action.tools.scripts.manage",
        "group": "运维工具",
        "title": "批量模板写操作",
        "description": "保存批量模板、Ansible 可执行路径、并发与超时配置。",
        "path": "/tools/batch-templates",
    },
    {
        "key": "action.tools.scripts.execute",
        "group": "运维工具",
        "title": "批量模板执行操作",
        "description": "执行 Shell、Ansible 模块、Playbook 模板或临时任务并查看输出。",
        "path": "/tools/batch-templates",
    },
    {
        "key": "action.settings.system.manage",
        "group": "系统设置",
        "title": "系统管理写操作",
        "description": "保存系统设置、执行自检、触发监控部署与通知渠道测试。",
        "path": "/settings/system",
    },
    {
        "key": "action.settings.users.manage",
        "group": "系统设置",
        "title": "用户管理写操作",
        "description": "新增用户、禁用账号、重置密码与保存权限授权。",
        "path": "/settings/users",
    },
    {
        "key": "action.db.manage",
        "group": "资产管理",
        "title": "数据库实例写操作",
        # 描述必须写明豁免副作用：勾选即等于"该角色免授权查任何库"，
        # 管理员在权限管理页据此决定是否授予（参见 database.py _user_has_manage）。
        "description": "新增/编辑/删除数据源与连通测试。注意：持有该权限的角色做自助查询时豁免查询授权校验（可直接查询已登记的任何库）。",
        "path": "/assets/databases",
    },
    {
        "key": "action.db.submit",
        "group": "资产管理",
        "title": "SQL 工单提交",
        "description": "提交变更工单、申请查询授权与自助查询（不含执行）。",
        "path": "/assets/databases",
    },
    {
        "key": "action.db.execute",
        "group": "资产管理",
        "title": "SQL 工单执行",
        "description": "执行已批准的变更工单与导出回滚语句。",
        "path": "/assets/databases",
    },
    {
        "key": "action.db.approve",
        "group": "资产管理",
        "title": "SQL 工单审批",
        "description": "批准或驳回 SQL 工单。",
        "path": "/assets/databases",
    },
    {
        "key": "action.change.submit",
        "group": "运维工具",
        "title": "变更工单提交",
        "description": "创建变更工单、取消本人待审工单。",
        "path": "/tools/changes",
    },
    {
        "key": "action.change.approve",
        "group": "运维工具",
        "title": "变更工单审批",
        "description": "批准或驳回变更工单。",
        "path": "/tools/changes",
    },
    {
        "key": "action.change.execute",
        "group": "运维工具",
        "title": "变更工单执行",
        "description": "执行已批准的变更工单（高危，需风险确认）。",
        "path": "/tools/changes",
    },
    {
        "key": "action.wireguard.manage",
        "group": "资产管理",
        "title": "虚拟组网写操作",
        "description": "创建/编辑/删除组网与成员、下载配置与二维码。",
        "path": "/assets/wireguard",
    },
    {
        "key": "action.finance.accounts.manage",
        "group": "费用管理",
        "title": "云账号写操作",
        "description": "新增、编辑、删除费用云账号并校验凭证。",
        "path": "/finance/accounts",
    },
    {
        "key": "action.finance.sync",
        "group": "费用管理",
        "title": "账单同步操作",
        "description": "手动触发云账号账单同步。",
        "path": "/finance/accounts",
    },
]

PERMISSION_CATALOG: list[dict[str, str]] = [
    *MODULE_PERMISSION_CATALOG,
    *ACTION_PERMISSION_CATALOG,
]

PERMISSION_DEFAULTS: dict[str, bool] = {
    item["key"]: False for item in PERMISSION_CATALOG
}

PERMISSION_LABEL_MAP: dict[str, str] = {
    item["key"]: item["title"] for item in PERMISSION_CATALOG
}


def _grant_all() -> dict[str, bool]:
    """生成全权限模板。"""
    return {key: True for key in PERMISSION_DEFAULTS}


ROLE_TEMPLATE_PRESETS: dict[str, dict[str, Any]] = {
    "管理员": {
        "title": "管理员",
        "description": "拥有全部页面与后台管理能力。",
        "permissions": _grant_all(),
    },
    "运维": {
        "title": "运维",
        "description": "聚焦主机、虚机、监控、终端与系统维护。",
        "permissions": {
            **PERMISSION_DEFAULTS,
            "module.dashboard": True,
            "module.ai.ops": True,
            "module.ai.skills": True,
            "module.ai.orchestration": True,
            "module.assets.hosts": True,
            "module.assets.vms": True,
            "module.assets.clusters": True,
            "module.assets.domains": True,
            "module.assets.certificates": True,
            "module.assets.credentials": True,
            "module.apps.list": True,
            "module.apps.deployment": True,
            "module.apps.config": True,
            "module.monitoring.live": True,
            "module.monitoring.rules": True,
            "module.monitoring.channels": True,
            "module.monitoring.logs": True,
            "module.monitoring.metrics": True,
            "module.monitoring.alerts": True,
            "module.monitoring.collector": True,
            "module.tools.terminal": True,
            "module.tools.scripts": True,
            "module.tools.changes": True,
            "module.settings.system": True,
            "module.settings.audit": True,
            "module.settings.permissions": True,
            "module.settings.scheduled": True,
            "module.settings.im": True,
            "module.finance.overview": True,
            "module.finance.bills": True,
            "module.finance.accounts": True,
            "module.wireguard.manage": True,
            "module.assets.databases": True,
            "action.ai.orchestration.manage": True,
            "action.apps.control": True,
            "action.apps.deploy": True,
            "action.assets.hosts.manage": True,
            "action.assets.hosts.connect": True,
            "action.assets.hosts.monitoring": True,
            "action.assets.vms.manage": True,
            "action.assets.vms.power": True,
            "action.assets.clusters.manage": True,
            "action.assets.domains.manage": True,
            "action.assets.certificates.manage": True,
            "action.assets.certificates.download": True,
            "action.assets.credentials.manage": True,
            "action.assets.credentials.export": True,
            "action.monitoring.rules.manage": True,
            "action.monitoring.channels.manage": True,
            "action.monitoring.collector.manage": True,
            "action.tools.scripts.manage": True,
            "action.tools.scripts.execute": True,
            "action.settings.system.manage": True,
            "action.finance.accounts.manage": True,
            "action.finance.sync": True,
            "action.wireguard.manage": True,
            "action.db.manage": True,
            "action.db.submit": True,
    "action.change.submit": True,
    "action.change.approve": True,
    "action.change.execute": True,
            "action.db.execute": True,
            "action.db.approve": True,
        },
    },
    "数据库管理员": {
        "title": "数据库管理员",
        "description": "数据库实例与 SQL 工单全生命周期管理（提单/审批/执行/数据源维护）。",
        "permissions": {
            **PERMISSION_DEFAULTS,
            "module.dashboard": True,
            "module.ai.ops": True,
            "module.assets.databases": True,
            "module.settings.im": True,
            "action.db.manage": True,
            "action.db.submit": True,
            "action.db.execute": True,
            "action.db.approve": True,
        },
    },
    "开发": {
        "title": "开发",
        "description": "聚焦应用中心、日志与基础概览。",
        "permissions": {
            **PERMISSION_DEFAULTS,
            "module.dashboard": True,
            "module.ai.ops": True,
            "module.ai.skills": True,
            "module.ai.orchestration": True,
            "module.apps.list": True,
            "module.apps.deployment": True,
            "module.apps.config": True,
            "module.monitoring.live": True,
            "module.monitoring.logs": True,
            "module.monitoring.metrics": True,
            "module.monitoring.alerts": True,
            "module.monitoring.collector": True,
            "module.settings.im": True,
            "action.apps.manage": True,
            "action.apps.control": True,
            "action.apps.deploy": True,
            "action.apps.config.manage": True,
        },
    },
    "域名管理员": {
        "title": "域名管理员",
        "description": "聚焦域名解析与证书生命周期管理。",
        "permissions": {
            **PERMISSION_DEFAULTS,
            "module.dashboard": True,
            "module.ai.ops": True,
            "module.assets.domains": True,
            "module.assets.certificates": True,
            "module.settings.im": True,
            "action.assets.domains.manage": True,
            "action.assets.certificates.manage": True,
            "action.assets.certificates.download": True,
        },
    },
    "只读": {
        "title": "只读",
        "description": "仅查看概览、实时监控与日志检索。",
        "permissions": {
            **PERMISSION_DEFAULTS,
            "module.dashboard": True,
            "module.ai.ops": True,
            "module.monitoring.live": True,
            "module.monitoring.logs": True,
            "module.settings.im": True,
        },
    },
}

ROLE_TEMPLATE_CONFIG_KEY = "rbac_role_templates"

# ---------------------------------------------------------------------------
# 操作风险等级注册表
# ---------------------------------------------------------------------------
# 设计说明：
# 1. 以 (权限键, 动作标识) 复合键为粒度，同一权限键下不同操作可有不同等级；
# 2. 未注册的操作默认为 low，无需额外确认；
# 3. 前端通过 /api/rbac/risk-levels 读取并缓存，后端硬编码不允许前端篡改。

ACTION_RISK_LEVELS: dict[tuple[str, str], str] = {
    ("action.assets.credentials.export", "export"): "high",
    ("action.assets.certificates.manage", "revoke"): "high",
    ("action.assets.certificates.download", "download"): "high",
    ("action.assets.certificates.manage", "deploy"): "low",
    ("action.assets.certificates.manage", "delete"): "medium",
    ("action.assets.hosts.manage", "batch_delete"): "medium",
    ("action.assets.vms.power", "poweroff"): "medium",
    ("action.assets.vms.power", "restart"): "medium",
    ("action.assets.vms.manage", "delete"): "medium",
    ("action.assets.clusters.manage", "apply"): "medium",
    ("action.assets.clusters.manage", "edit"): "medium",
    ("action.assets.clusters.manage", "restart"): "medium",
    ("action.assets.clusters.manage", "pause"): "medium",
    ("action.assets.clusters.manage", "delete"): "high",
    # 集群运维动作（2026-10-08 巡检补注册）：此前整族动作未登记 → 回落 low →
    # 前端不弹确认、后端不索票，一次误点就把生产节点标记为不可调度/驱逐全部 Pod。
    # 等级按「对运行中业务的影响」分级：
    # - high：改变集群对工作负载的承载能力（cordon 停止调度、drain 驱逐 Pod、
    #   set-image 触发滚动更新、rollout-undo 回退版本）
    # - medium：可预期但影响面较小的状态变更（恢复调度、重启、暂停/挂起、
    #   手动触发定时任务、污点、PVC 扩容不可逆）
    ("action.assets.clusters.manage", "cordon"): "high",
    ("action.assets.clusters.manage", "uncordon"): "medium",
    ("action.assets.clusters.manage", "drain"): "high",
    ("action.assets.clusters.manage", "set-image"): "high",
    ("action.assets.clusters.manage", "rollout-undo"): "high",
    ("action.assets.clusters.manage", "suspend_cronjob"): "medium",
    ("action.assets.clusters.manage", "trigger_cronjob"): "medium",
    ("action.assets.clusters.manage", "patch_taints"): "medium",
    ("action.assets.clusters.manage", "resize_pvc"): "medium",
    # 2026-10-08 二轮补登记：上一轮把 cordon/drain 家族拉起来了，但复盘发现
    # 还有四个动作挂着 require_risk_ack / 前端已发确认却未登记 → 回落 low，
    # 审计页风险列不可信。均为 medium：不破坏承载能力但改动共享状态。
    # - patch_labels：节点 / 命名空间标签（影响调度与选择器命中）
    # - scale：扩缩容（改了业务容量，但不删数据）
    # - create_namespace：新建命名空间（含配额预设）
    # - exec_session：Pod 终端入口，等同远程登录进容器
    ("action.assets.clusters.manage", "patch_labels"): "medium",
    ("action.assets.clusters.manage", "scale"): "medium",
    ("action.assets.clusters.manage", "create_namespace"): "medium",
    ("action.assets.clusters.manage", "exec_session"): "medium",
    ("action.assets.domains.manage", "delete"): "medium",
    ("action.tools.scripts.execute", "execute"): "medium",
    ("action.change.execute", "execute"): "high",
    ("action.settings.users.manage", "disable"): "medium",
    # 删除用户（2026-10-05 新增端点）：不可恢复且影响登录主体，高危
    ("action.settings.users.manage", "delete"): "high",
    ("action.settings.system.manage", "delete"): "medium",
    ("action.monitoring.channels.manage", "delete"): "medium",
    ("action.finance.accounts.manage", "delete"): "high",
    ("action.finance.accounts.manage", "create"): "medium",
}


def get_risk_level(permission_key: str, action_id: str) -> str:
    """返回操作的风险等级，未注册则为 low。"""
    return ACTION_RISK_LEVELS.get((permission_key, action_id), "low")

ROLE_ALIAS_MAP: dict[str, str] = {
    "admin": "管理员",
    "administrator": "管理员",
}


def normalize_role_name(role: str | None) -> str:
    """归一化角色名，兼容部署环境常见英文别名。"""
    role_name = str(role or "").strip()
    return ROLE_ALIAS_MAP.get(role_name.lower(), role_name)


def _safe_load_json_dict(raw_value: str | None) -> dict[str, Any]:
    """安全解析 JSON 字典。"""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_custom_role_templates() -> dict[str, dict[str, Any]]:
    """从系统配置读取权限组覆盖项。

    设计说明：
    1. SQLite 中统一存储“自定义权限组 + 系统预设组覆盖项”；
    2. 当前项目使用 SQLite，本阶段直接通过 SessionLocal 读取即可；
    3. 读取失败时返回空字典，避免影响登录链路与权限判断。
    """
    db = SessionLocal()
    try:
        row = (
            db.query(SystemConfig)
            .filter(SystemConfig.config_key == ROLE_TEMPLATE_CONFIG_KEY)
            .first()
        )
        payload = _safe_load_json_dict(row.config_value if row else "")
        result: dict[str, dict[str, Any]] = {}
        for role_key, meta in payload.items():
            if not isinstance(role_key, str):
                continue
            role_name = role_key.strip()
            if not role_name:
                continue
            if not isinstance(meta, dict):
                continue
            raw_permissions = meta.get("permissions") or {}
            if not isinstance(raw_permissions, dict):
                raw_permissions = {}
            # 系统预设权限组允许被覆盖，但为了兼容历史数据（旧版本缺少新权限键），
            # 这里先以代码内置预设为基线，再叠加数据库覆盖值。
            base_permissions = {}
            if role_name in ROLE_TEMPLATE_PRESETS:
                base_permissions = dict(
                    ROLE_TEMPLATE_PRESETS.get(role_name, {}).get("permissions") or {}
                )
            result[role_name] = {
                "title": str(meta.get("title") or role_name),
                "description": str(meta.get("description") or ""),
                "permissions": normalize_permission_map(
                    {**base_permissions, **raw_permissions}
                ),
            }
        return result
    except Exception:
        return {}
    finally:
        db.close()


def get_role_template_map() -> dict[str, dict[str, Any]]:
    """返回“系统预设 + 自定义”合并后的权限组映射。"""
    return {
        **ROLE_TEMPLATE_PRESETS,
        **_read_custom_role_templates(),
    }


def is_system_role_template(role: str) -> bool:
    """判断权限组是否属于系统预设模板。"""
    return normalize_role_name(role) in ROLE_TEMPLATE_PRESETS


def list_role_templates() -> list[dict[str, Any]]:
    """返回角色模板列表。"""
    result: list[dict[str, Any]] = []
    for role, meta in get_role_template_map().items():
        is_system = is_system_role_template(role)
        result.append(
            {
                "role": role,
                "title": str(meta.get("title") or role),
                "description": str(meta.get("description") or ""),
                "permissions": dict(meta.get("permissions") or {}),
                "is_system": is_system,
                # 系统预设组允许编辑权限内容与说明，但不允许删除和改组名。
                "editable": True,
                "deletable": not is_system,
            }
        )
    # 排序：系统预设按定义顺序（管理员→运维→数据库管理员→开发→域名管理员→只读），
    # 自定义组排在其后按名称。此前按标题 Unicode 码点排序，展示顺序看似随机。
    preset_order = {role: idx for idx, role in enumerate(ROLE_TEMPLATE_PRESETS)}
    result.sort(
        key=lambda item: (
            (0, preset_order.get(str(item["role"]), len(preset_order)), "")
            if item["is_system"]
            else (1, 0, str(item["title"]))
        )
    )
    return result


def normalize_permission_map(raw: Any) -> dict[str, bool]:
    """归一化权限字典，仅保留白名单键。"""
    normalized = {**PERMISSION_DEFAULTS}
    if not isinstance(raw, dict):
        return normalized
    for key, value in raw.items():
        if key not in normalized:
            continue
        if isinstance(value, bool):
            normalized[key] = value
    return normalized


def get_role_permissions(role: str) -> dict[str, bool]:
    """读取角色模板权限。未知角色按只读模板处理。"""
    role_name = normalize_role_name(role)
    template_map = get_role_template_map()
    meta = template_map.get(role_name) or ROLE_TEMPLATE_PRESETS["只读"]
    return _apply_legacy_permission_compatibility(normalize_permission_map(meta.get("permissions") or {}))


def _apply_legacy_permission_compatibility(permissions: dict[str, bool]) -> dict[str, bool]:
    """补齐历史权限组缺失的新权限键。

    旧数据库里的自定义权限组不会自动拥有后来新增的权限键。这里按照同一业务域
    的既有访问权做保守补齐，避免升级后菜单入口消失。
    """
    if (
        permissions.get("module.assets.hosts")
        and permissions.get("module.assets.vms")
        and "module.assets.clusters" in permissions
    ):
        permissions["module.assets.clusters"] = True
    if (
        permissions.get("module.monitoring.live")
        and permissions.get("module.monitoring.rules")
        and permissions.get("module.monitoring.logs")
        and "module.monitoring.collector" in permissions
    ):
        permissions["module.monitoring.collector"] = True
    if (
        permissions.get("module.monitoring.live")
        and permissions.get("module.monitoring.rules")
        and "module.monitoring.channels" in permissions
    ):
        permissions["module.monitoring.channels"] = True
    # 数据库管理分权升级（拆出 action.db.submit）：存量角色原本的 execute 同时管
    # 提交与执行，升级后不能让人凭空失去提交能力——有 execute 就自动补上 submit。
    if permissions.get("action.db.execute"):
        permissions["action.db.submit"] = True
    return permissions


def parse_permission_overrides(raw_value: str | None) -> dict[str, bool]:
    """从数据库字符串解析用户级权限覆盖。"""
    text = (raw_value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    overrides: dict[str, bool] = {}
    if not isinstance(payload, dict):
        return overrides
    for key, value in payload.items():
        if key not in PERMISSION_DEFAULTS:
            continue
        if isinstance(value, bool):
            overrides[key] = value
    return overrides


def build_permission_overrides(
    role: str,
    effective_permissions: dict[str, bool],
) -> dict[str, bool]:
    """根据最终权限与角色模板差异，生成需要持久化的覆盖项。"""
    role_permissions = get_role_permissions(role)
    target_permissions = normalize_permission_map(effective_permissions)
    overrides: dict[str, bool] = {}
    for key, value in target_permissions.items():
        if role_permissions.get(key) != value:
            overrides[key] = value
    return overrides


def resolve_effective_permissions_from_values(
    role: str,
    permission_overrides_raw: str | None,
) -> dict[str, bool]:
    """基于角色模板和覆盖项计算用户有效权限。"""
    merged = get_role_permissions(role)
    overrides = parse_permission_overrides(permission_overrides_raw)
    for key, value in overrides.items():
        merged[key] = value
    return merged


def resolve_effective_permissions(user: Any) -> dict[str, bool]:
    """根据用户对象解析有效权限。

    约定：
    - 用户对象需至少具备 `role` 与 `permission_overrides` 属性；
    - 该函数不依赖数据库，便于在认证链路与权限依赖中复用。
    """
    role = normalize_role_name(str(getattr(user, "role", "") or ""))
    overrides_raw = getattr(user, "permission_overrides", "") or ""
    return resolve_effective_permissions_from_values(role, overrides_raw)


def summarize_permission_labels(
    permissions: dict[str, bool],
    limit: int = 4,
) -> list[str]:
    """生成用户权限摘要标签，用于用户列表展示。"""
    granted = [
        PERMISSION_LABEL_MAP[key]
        for key, enabled in permissions.items()
        if enabled and key in PERMISSION_LABEL_MAP and key != "module.dashboard"
    ]
    if not granted and permissions.get("module.dashboard"):
        granted = [PERMISSION_LABEL_MAP["module.dashboard"]]
    if limit > 0 and len(granted) > limit:
        return granted[:limit]
    return granted
