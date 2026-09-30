"""首页系统总览聚合接口。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.ai_agent import (
    _read_llm_runtime_config,
    _read_orchestration_policy,
    _read_skill_registry,
)
from app.db.database import get_db
from app.models.application import Application
from app.models.asset import Asset, DomainRecord
from app.models.audit import AuditLog
from app.models.user import User
from app.models.vm import VirtualMachine
from app.models.dashboard_snapshot import DashboardSnapshot
from app.models.cluster import Cluster
from app.models.certificate import CertificateRecord

router = APIRouter()


def _format_time_label(value: datetime | str | None) -> str:
    """格式化时间标签，统一展示为月-日 时:分。"""
    if isinstance(value, datetime):
        return value.strftime("%m-%d %H:%M")
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16]


def parse_audit_action_to_message(user: str, action: str, resource: str) -> str:
    """将底层的 API 操作路径或原始 Action 字符串转化为高品质、人文化的大盘通顺操作日志。"""
    user_str = str(user or "未知用户")
    action_str = str(action or "").strip()
    resource_str = str(resource or "").strip()
    
    # 1. 计划任务/自动任务的完整句子日志，直接保留
    if user_str == "系统自动任务" or action_str.startswith("系统自动"):
        return action_str
        
    # 2. WebSSH 操作审计翻译
    if action_str.startswith("WebSSH文件"):
        op = action_str.replace("WebSSH文件", "")
        return f"{user_str} 通过 WebSSH {op}了文件"
        
    # 3. 翻译通用 API 端点写操作
    verb = action_str.split(" ", 1)[0] if " " in action_str else "操作"
    if verb not in ("创建", "更新", "删除", "POST", "PUT", "DELETE"):
        verb = "操作"
        
    # 映射 HTTP method 关键字为易读中文
    method_map = {"POST": "创建", "PUT": "更新", "DELETE": "删除"}
    verb = method_map.get(verb, verb)
        
    # 主机/资产路由
    if resource_str.startswith("/api/assets"):
        if "ping" in resource_str:
            return f"{user_str} 探测了主机 SSH 的连通性"
        if "batch-refresh-config" in resource_str:
            return f"{user_str} 批量更新了主机的硬件配置"
        if "refresh-config" in resource_str:
            return f"{user_str} 更新了主机的硬件配置"
        if verb == "创建":
            return f"{user_str} 纳管了新的主机资产"
        if verb == "删除":
            return f"{user_str} 移除了主机资产"
        return f"{user_str} {verb}了主机资产"
        
    # 虚机资产
    if resource_str.startswith("/api/vms"):
        if "sync" in resource_str:
            return f"{user_str} 同步了 PVE 虚拟主机"
        if verb == "创建":
            return f"{user_str} 创建了新的虚拟机"
        if verb == "删除":
            return f"{user_str} 销毁了虚拟机"
        return f"{user_str} {verb}了虚拟机配置"

    # 域名与解析记录
    if resource_str.startswith("/api/domains"):
        if "records" in resource_str:
            if verb == "创建":
                return f"{user_str} 添加了域名解析记录"
            if verb == "删除":
                return f"{user_str} 删除了域名解析记录"
            return f"{user_str} 修改了域名解析记录"
        if verb == "创建":
            return f"{user_str} 托管了新域名"
        if verb == "删除":
            return f"{user_str} 取消了域名托管"
        return f"{user_str} {verb}了域名配置"

    # 证书配置
    if resource_str.startswith("/api/certificates"):
        if "issue" in resource_str:
            return f"{user_str} 提交了证书 ACME 自动签发任务"
        if "upload" in resource_str:
            return f"{user_str} 手动上传并导入了自定义证书"
        if "deploy" in resource_str:
            return f"{user_str} 执行了证书的远程分发部署"
        if "auto-renew" in resource_str:
            return f"{user_str} 变更了证书的自动续签策略"
        if verb == "删除":
            return f"{user_str} 删除了证书记录"
        return f"{user_str} {verb}了证书配置"

    # 应用生命周期
    if resource_str.startswith("/api/applications"):
        if "deploy" in resource_str or "release" in resource_str:
            return f"{user_str} 发布部署了应用中心的项目"
        if verb == "创建":
            return f"{user_str} 创建了新应用项目"
        if verb == "删除":
            return f"{user_str} 移除了应用项目"
        return f"{user_str} {verb}了应用配置"

    # 凭证/密码箱
    if resource_str.startswith("/api/credentials"):
        if "export" in resource_str:
            return f"{user_str} 导出了敏感凭证内容"
        if verb == "创建":
            return f"{user_str} 录入了新安全凭证"
        if verb == "删除":
            return f"{user_str} 删除了安全凭证"
        return f"{user_str} {verb}了安全凭证配置"

    # 运维工具
    if resource_str.startswith("/api/tools"):
        if "execute" in resource_str or "run" in resource_str:
            return f"{user_str} 触发了批量运维脚本的远程执行"
        if verb == "创建":
            return f"{user_str} 新增了运维脚本模板"
        return f"{user_str} {verb}了运维工具配置"
        
    # AI 助手 / Skill
    if resource_str.startswith("/api/ai"):
        return f"{user_str} 调整了 AI 平台的 Skill 或模型配置"

    # 系统设置与权限
    if resource_str.startswith("/api/settings"):
        return f"{user_str} 修改了系统全局安全或 RBAC 策略"

    # 降级兜底：去掉冗长的 API 路径，只保留动作
    return f"{user_str} 执行了系统{verb}操作"


def _build_audit_activities(audits: list[AuditLog]) -> list[dict[str, Any]]:
    """将审计日志转换为首页最近动态。

    这里只展示操作主题和结果，不回传请求体、凭据、密钥或其他敏感内容。
    """
    rows: list[dict[str, Any]] = []
    for item in audits:
        resource = str(item.resource or "")
        # 日志查询等高频只读动作对首页价值较低，直接过滤。
        if resource.startswith("/api/monitoring/logs/query"):
            continue

        message = parse_audit_action_to_message(item.user, item.action, resource)
        rows.append(
            {
                "id": f"audit-{item.id}",
                "type": "config",
                "message": message,
                "time": _format_time_label(item.time),
                "status": "success" if str(item.result or "success") == "success" else "error",
                "sort_ts": item.time.timestamp() if isinstance(item.time, datetime) else 0.0,
            }
        )
    return rows





@router.get("/overview")
def get_dashboard_overview(db: Session = Depends(get_db)):
    """返回首页系统总览所需的聚合数据。

    设计约束：
    1. 首页站在 OpsPilot 系统视角，只展示平台对象数量和 AI 能力状态；
    2. 不再依赖监控时序数据，避免 VictoriaMetrics/Alloy 状态影响首页可用性；
    3. AI 配置只返回 provider/model/ready 等状态，不返回 API Key 或加密密文。
    """
    # 使用 group_by 和 func.count 优化数据库查询，避免拉取整表
    app_total = db.query(func.count(Application.id)).scalar() or 0
    app_status_raw = db.query(Application.status, func.count(Application.id)).group_by(Application.status).all()
    app_status = {str(status or "unknown").strip().lower() or "unknown": count for status, count in app_status_raw}

    user_total = db.query(func.count(User.id)).scalar() or 0
    user_status_raw = db.query(User.status, func.count(User.id)).group_by(User.status).all()
    user_status = {str(status or "unknown").strip().lower() or "unknown": count for status, count in user_status_raw}

    asset_total = db.query(func.count(Asset.id)).scalar() or 0
    asset_status_raw = db.query(Asset.type, func.count(Asset.id)).group_by(Asset.type).all()
    host_status = {str(status or "unknown").strip().lower() or "unknown": count for status, count in asset_status_raw}

    vm_total = db.query(func.count(VirtualMachine.id)).scalar() or 0
    vm_status_raw = db.query(VirtualMachine.status, func.count(VirtualMachine.id)).group_by(VirtualMachine.status).all()
    vm_status = {str(status or "unknown").strip().lower() or "unknown": count for status, count in vm_status_raw}

    # 统计集群、域名、证书
    cluster_total = db.query(func.count(Cluster.id)).scalar() or 0
    domain_total = db.query(func.count(DomainRecord.id)).scalar() or 0
    certificate_total = db.query(func.count(CertificateRecord.id)).scalar() or 0

    # 多拉取一些审计日志，以防被只读操作过滤掉太多
    audits = db.query(AuditLog).order_by(AuditLog.time.desc()).limit(50).all()

    # 惰性更新今日总量快照
    import datetime
    today = datetime.date.today()
    snapshot = db.query(DashboardSnapshot).filter(DashboardSnapshot.date == today).first()
    now_str = str(datetime.datetime.now())
    if snapshot:
        snapshot.hosts_total = asset_total
        snapshot.vms_total = vm_total
        snapshot.applications_total = app_total
        snapshot.users_total = user_total
        snapshot.updated_at = now_str
    else:
        snapshot = DashboardSnapshot(
            date=today,
            hosts_total=asset_total,
            vms_total=vm_total,
            applications_total=app_total,
            users_total=user_total,
            updated_at=now_str
        )
        db.add(snapshot)
    try:
        db.commit()
    except Exception:
        db.rollback()

    # 获取近 7 天历史趋势 (按日期正序返回给前端)
    historical_snapshots = db.query(DashboardSnapshot).order_by(DashboardSnapshot.date.desc()).limit(7).all()
    historical_snapshots.reverse()
    
    trends = {
        "hosts": [s.hosts_total for s in historical_snapshots],
        "vms": [s.vms_total for s in historical_snapshots],
        "apps": [s.applications_total for s in historical_snapshots],
        "users": [s.users_total for s in historical_snapshots],
        "dates": [s.date.strftime("%m-%d") for s in historical_snapshots],
    }

    llm_config, llm_updated_at = _read_llm_runtime_config(db)
    skills, skills_updated_at = _read_skill_registry(db)
    orchestration_policy, orchestration_updated_at = _read_orchestration_policy(db)
    enabled_skills = [item for item in skills if item.get("enabled")]
    ai_ready = bool(llm_config.get("enabled") and llm_config.get("has_api_key") and enabled_skills)

    activities = [
        {
            "id": row["id"],
            "type": row["type"],
            "message": row["message"],
            "time": row["time"],
            "status": row["status"],
        }
        for row in sorted(_build_audit_activities(audits), key=lambda item: item.get("sort_ts", 0), reverse=True)[:15]
    ]

    # 真实告警统计 (由内置告警引擎写入 monitoring_active_alert 表,_fetch_alerts 直接读库)
    alerts = {
        "critical": 0,
        "warning": 0,
        "info": 0,
        "list": [],
    }
    try:
        from app.api.monitoring.services.config_store import read_monitoring_config
        from app.api.monitoring.services.dashboard import _fetch_alerts

        cfg = read_monitoring_config(db)
        # _fetch_alerts 直接读取内置告警引擎维护的活动告警表
        raw_alerts = _fetch_alerts(cfg, db)
        
        firing_alerts = []
        for item in raw_alerts:
            # 只统计并展示正处于触发状态 (firing) 的告警，过滤 pending (未达到触发阈值时长)
            state = str(item.get("status") or "firing").strip().lower()
            if state != "firing":
                continue
                
            severity = str(item.get("severity") or "warning").strip().lower()
            if severity in alerts:
                alerts[severity] += 1
            else:
                alerts["warning"] += 1
            firing_alerts.append(item)

        # 按照告警触发时间 (time 字段) 降序排序，最新的排在最前
        def get_alert_time(x):
            t = x.get("time") or ""
            return t if t != "-" else ""
            
        firing_alerts.sort(key=get_alert_time, reverse=True)
        # 大盘只展示最新的 5 条活跃告警
        alerts["list"] = firing_alerts[:5]
    except Exception:
        # 兜底防御，确保监控组件故障不影响整个系统概览首页的渲染
        pass

    return {
        "summary": {
            "applications_total": app_total,
            "applications_active": int(app_status.get("active", 0)),
            "applications_maintenance": int(app_status.get("maintenance", 0)),
            "users_total": user_total,
            "users_active": int(user_status.get("active", 0)),
            "users_disabled": int(user_status.get("disabled", 0)),
            "hosts_total": asset_total,
            "hosts_healthy": int(host_status.get("healthy", 0)),
            "hosts_pending": int(host_status.get("pending", 0)),
            "hosts_abnormal": max(0, asset_total - int(host_status.get("healthy", 0)) - int(host_status.get("pending", 0))),
            "vms_total": vm_total,
            "vms_running": int(vm_status.get("running", 0)),
            "vms_stopped": max(0, vm_total - int(vm_status.get("running", 0))),
            "clusters_total": cluster_total,
            "domains_total": domain_total,
            "certificates_total": certificate_total,
        },
        "trends": trends,
        "ai": {
            "ready": ai_ready,
            "llm_enabled": bool(llm_config.get("enabled")),
            "has_api_key": bool(llm_config.get("has_api_key")),
            "provider": str(llm_config.get("provider") or "gemini"),
            "model": str(llm_config.get("model") or ""),
            "using_env_fallback": bool(llm_config.get("using_env_fallback")),
            "llm_updated_at": llm_updated_at,
            "skills_total": len(skills),
            "skills_enabled": len(enabled_skills),
            "skills_updated_at": skills_updated_at,
            "orchestration_approval_enabled": bool(orchestration_policy.get("approval_enabled")),
            "orchestration_budget_guard_enabled": bool(orchestration_policy.get("budget_guard_enabled")),
            "orchestration_updated_at": orchestration_updated_at,
        },
        "alerts": alerts,
        "activities": activities,
    }
