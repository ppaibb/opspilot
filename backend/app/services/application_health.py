"""应用健康聚合与快照巡检服务。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.json_fields import dump_json_list as _dump_json_list
from app.models.application import (
    Application,
    ApplicationComponent,
    ApplicationHealthSnapshot,
    ApplicationResource,
)
from app.models.asset import Asset, DomainRecord
from app.models.certificate import CertificateRecord
from app.services.application_time import now_dt

APPLICATION_HEALTH_SNAPSHOT_INTERVAL_SECONDS = 300
APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS = 168  # 保留 7 天


def now_str() -> datetime:
    """返回应用健康快照统一使用的 DateTime。"""
    return now_dt()


def dump_json_list(items: list[str] | None) -> str:
    """序列化字符串列表，避免 service 反向依赖路由模块。"""
    return _dump_json_list(items)


def is_expiring_soon(raw_value: str | None, days: int = 30) -> bool:
    """判断日期字符串是否在指定天数内到期。"""
    if not raw_value:
        return False
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(str(raw_value).strip()[:19], fmt)
            return datetime.now() <= parsed <= datetime.now() + timedelta(days=days)
        except Exception:
            continue
    return False


def host_asset_lookup(db: Session) -> dict[str, Asset]:
    """按主机资产 ID 构建只读索引。"""
    rows = db.query(Asset).all()
    return {str(row.instance_id or "").strip(): row for row in rows if str(row.instance_id or "").strip()}


def build_application_health(
    app_status: str,
    component_statuses: list[str],
    host_health_values: list[str],
    domain_expiring_count: int = 0,
    certificate_expiring_count: int = 0,
    active_alert_count: int = 0,
) -> dict[str, Any]:
    """聚合应用健康度。

    安全边界：这里只读取数据库里已经存在的状态，不 SSH、不发 HTTP 探测、
    不执行用户输入命令，避免应用列表页引入 SSRF 或远程命令风险。
    """
    normalized_app_status = str(app_status or "active").strip().lower()
    component_values = [str(item or "").strip().lower() for item in component_statuses if str(item or "").strip()]
    host_values = [str(item or "").strip().lower() for item in host_health_values if str(item or "").strip()]
    reasons: list[str] = []

    if normalized_app_status == "offline":
        return {
            "health_status": "offline",
            "health_label": "已下线",
            "health_reasons": ["应用已下线"],
        }
    if normalized_app_status == "maintenance":
        reasons.append("应用处于维护状态")

    if not component_values and not host_values and not reasons:
        return {
            "health_status": "unknown",
            "health_label": "未纳管",
            "health_reasons": ["未绑定运行实例或主机"],
        }

    degraded = False
    warning = bool(reasons)
    offline_components = len([item for item in component_values if item == "offline"])
    maintenance_components = len([item for item in component_values if item == "maintenance"])
    unhealthy_hosts = len([item for item in host_values if item not in {"healthy", "online", "active", "normal"}])

    if offline_components:
        degraded = True
        reasons.append(f"{offline_components} 个运行实例离线")
    if unhealthy_hosts:
        degraded = True
        reasons.append(f"{unhealthy_hosts} 台绑定主机不可用")
    if maintenance_components:
        warning = True
        reasons.append(f"{maintenance_components} 个运行实例维护中")
    if active_alert_count > 0:
        warning = True
        reasons.append(f"{active_alert_count} 条活动告警")
    if domain_expiring_count > 0:
        warning = True
        reasons.append(f"{domain_expiring_count} 个域名即将到期")
    if certificate_expiring_count > 0:
        warning = True
        reasons.append(f"{certificate_expiring_count} 张证书即将到期")

    if degraded:
        return {"health_status": "degraded", "health_label": "需处理", "health_reasons": reasons[:4]}
    if warning:
        return {"health_status": "warning", "health_label": "有预警", "health_reasons": reasons[:4]}
    return {"health_status": "healthy", "health_label": "健康", "health_reasons": ["运行状态正常"]}


def active_alert_count_for_hosts(
    db: Session,
    host_ips: list[str],
    host_names: list[str],
) -> int:
    """best-effort 查询当前应用绑定主机命中的活动告警数。

    失败时返回 0，绝不阻塞健康快照生成。
    """
    if not host_ips and not host_names:
        return 0
    try:
        # 延迟导入避免循环依赖。
        from app.api.monitoring import (  # noqa: WPS433
            _fetch_alerts,
            _filter_dashboard_alerts,
            read_monitoring_config,
        )

        cfg = read_monitoring_config(db)
        alerts = _filter_dashboard_alerts(_fetch_alerts(cfg), host_ips, host_names)
        return len(alerts)
    except Exception:
        return 0


def compute_application_health_snapshot(
    db: Session,
    app: Application,
    include_alerts: bool = True,
) -> dict[str, Any]:
    """聚合单个应用的健康快照数据。

    只读：不执行 SSH、不调用任意命令。
    """
    resources = db.query(ApplicationResource).filter(ApplicationResource.app_id == app.app_id).all()
    components = db.query(ApplicationComponent).filter(ApplicationComponent.app_id == app.app_id).all()
    host_lookup = host_asset_lookup(db)
    host_health_values: list[str] = []
    host_ips: list[str] = []
    host_names: list[str] = []
    for resource in resources:
        if resource.resource_type != "host":
            continue
        asset = host_lookup.get(str(resource.resource_id or "").strip())
        if asset is None:
            host_health_values.append("unknown")
            continue
        host_health_values.append(str((asset.type or asset.status or "unknown")))
        if asset.ip:
            host_ips.append(str(asset.ip))
        if asset.name:
            host_names.append(str(asset.name))

    domain_ids = [
        int(row.resource_id)
        for row in resources
        if row.resource_type == "domain" and str(row.resource_id).isdigit()
    ]
    domains = db.query(DomainRecord).filter(DomainRecord.id.in_(domain_ids)).all() if domain_ids else []
    cert_ids = [
        str(row.resource_id)
        for row in resources
        if row.resource_type == "certificate" and str(row.resource_id).strip()
    ]
    certs = db.query(CertificateRecord).filter(CertificateRecord.cert_id.in_(cert_ids)).all() if cert_ids else []
    domain_expiring = len([item for item in domains if is_expiring_soon(item.expires_at)])
    certificate_expiring = len([item for item in certs if is_expiring_soon(item.not_after)])

    active_alert_count = active_alert_count_for_hosts(db, host_ips, host_names) if include_alerts else 0
    health = build_application_health(
        app_status=app.status or "active",
        component_statuses=[row.status or "" for row in components],
        host_health_values=host_health_values,
        domain_expiring_count=domain_expiring,
        certificate_expiring_count=certificate_expiring,
        active_alert_count=active_alert_count,
    )

    component_active = len([row for row in components if str(row.status or "").strip().lower() == "active"])
    host_total = len([row for row in resources if row.resource_type == "host"])
    host_healthy = len(
        [value for value in host_health_values if value.lower() in {"healthy", "online", "active", "normal"}]
    )

    return {
        "app_id": app.app_id,
        "health_status": health["health_status"],
        "health_label": health["health_label"],
        "health_reasons": health["health_reasons"],
        "component_total": len(components),
        "component_active": component_active,
        "host_total": host_total,
        "host_healthy": host_healthy,
        "active_alert_count": active_alert_count,
        "domain_expiring_count": domain_expiring,
        "certificate_expiring_count": certificate_expiring,
    }


def run_application_health_snapshot_cycle() -> int:
    """对所有应用生成一次健康快照，并清理过期快照。

    返回本次写入的快照数量，方便测试断言。
    安全边界：
    - 调用方不接受用户输入；
    - 仅读取本地业务表 + 现有 Alertmanager 只读链路；
    - Alertmanager 查询失败会被 best-effort 吞到 0，不阻塞其它应用快照。
    """
    # 延迟导入 SessionLocal，避免导入期和测试中的循环依赖。
    from app.db.database import SessionLocal  # noqa: WPS433

    db = SessionLocal()
    written = 0
    try:
        apps = db.query(Application).all()
        now = now_str()
        for app in apps:
            try:
                snapshot = compute_application_health_snapshot(db, app, include_alerts=True)
            except Exception:
                continue
            db.add(
                ApplicationHealthSnapshot(
                    app_id=app.app_id,
                    snapshot_time=now,
                    health_status=snapshot["health_status"],
                    health_label=snapshot["health_label"],
                    health_reasons=dump_json_list(snapshot["health_reasons"]),
                    component_total=int(snapshot["component_total"]),
                    component_active=int(snapshot["component_active"]),
                    host_total=int(snapshot["host_total"]),
                    host_healthy=int(snapshot["host_healthy"]),
                    active_alert_count=int(snapshot["active_alert_count"]),
                    domain_expiring_count=int(snapshot["domain_expiring_count"]),
                    certificate_expiring_count=int(snapshot["certificate_expiring_count"]),
                )
            )
            written += 1

        cutoff = datetime.now() - timedelta(hours=APPLICATION_HEALTH_SNAPSHOT_RETENTION_HOURS)
        db.query(ApplicationHealthSnapshot).filter(
            ApplicationHealthSnapshot.snapshot_time < cutoff
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:  # pragma: no cover - 定时任务不应阻塞主进程
        db.rollback()
        print("[APPLICATION HEALTH SNAPSHOT FAILURE]", exc)
    finally:
        db.close()
    return written


async def start_application_health_snapshot_runner() -> None:
    """后台巡检协程：每 5 分钟写一次应用健康快照。"""
    import asyncio  # noqa: WPS433 - 只在运行态需要
    from app.core.scheduler_registry import register_task, track_run

    register_task(
        "application_health_snapshot",
        "应用健康快照",
        "每 5 分钟聚合一次应用组件/主机/告警健康度，写入时间带数据源",
        APPLICATION_HEALTH_SNAPSHOT_INTERVAL_SECONDS,
    )
    print("[OpsPilot] 启动应用健康快照巡检器（5分钟）...")
    while True:
        with track_run("application_health_snapshot"):
            await asyncio.to_thread(run_application_health_snapshot_cycle)
        await asyncio.sleep(APPLICATION_HEALTH_SNAPSHOT_INTERVAL_SECONDS)
