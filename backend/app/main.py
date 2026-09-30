from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
# 环境变量配置已交由 app.core.config 接管
import re
from urllib.parse import unquote

from app.api.ai_agent import router as ai_router
from app.api.applications import router as applications_router
from app.api.auth import router as auth_router
from app.api.assets import router as assets_router, start_assets_health_checker
from app.api.cmdb import router as cmdb_router
from app.api.changes import router as changes_router
from app.api.groups import router as groups_router
from app.api.dashboard import router as dashboard_router
from app.api.domains import router as domains_router
from app.api.grafana_sso import router as grafana_sso_router
from app.api.credentials import router as creds_router
from app.api.clusters import router as clusters_router
from app.api.clusters import ws_router as clusters_ws_router
from app.api.k8s_watch import router as k8s_watch_router
from app.api.certificates import router as certificates_router
from app.api.collector import agent_router as collector_agent_router
from app.api.collector import router as collector_router
from app.api.monitoring import router as monitoring_router
from app.api.monitoring_webhooks import router as monitoring_webhooks_router
from app.api.alert_center import router as alert_center_router

from app.api.settings import router as settings_router, public_router as public_settings_router
from app.api.vms import router as vms_router
from app.api.finance import router as finance_router
from app.api.wireguard import router as wireguard_router
from app.api.database import router as database_router
from app.api.terminal_ws import router as terminal_ws_router
from app.api.tools import router as tools_router
from app.api.notifications import router as notifications_router
from app.api.im import router as im_router

from app.core.rbac import get_risk_level
from app.core.logging import setup_logging
from app.core.security import get_current_user, require_permissions

setup_logging()
from contextlib import asynccontextmanager
import asyncio
from jose import JWTError, jwt
from app.core.config import settings
from app.db.database import Base, engine, SessionLocal
from app.db import models as db_models  # noqa: F401
from app.db.bootstrap import ensure_bootstrap_admin, ensure_localhost_asset, ensure_monitoring_config
from app.models.user import User
from app.models.audit import AuditLog
from app.models.asset import Asset
from app.models.application import (
    Application,
    ApplicationComponent,
    ApplicationConfig,
    ApplicationConfigRevision,
    ApplicationHealthSnapshot,
    ApplicationLink,
    ApplicationRelease,
    ApplicationResource,
)
from app.models.credential import Credential
from app.models.finance import BillSyncRun, CloudAccount, CloudBill
from app.models.wireguard import WireGuardNetwork, WireGuardPeer
from app.models.database import DbInstance, DbQueryLog, DbTicket
from app.models.root_cause import AlertRootCause
from app.models.monitoring_webhook_event import MonitoringWebhookEvent
from app.models.monitoring_history_alert import MonitoringHistoryAlert
from app.models.vm import VirtualMachine
from app.models.resource_group import ResourceGroup
from app.models.system_config import SystemConfig
from app.models.certificate import CertificateRecord
from app.models.collector import (
    CollectorAgent,
    CollectorConfigVersion,
    CollectorGroup,
    CollectorPolicy,
    CollectorRegistrationToken,
    CollectorRelease,
)
from app.core.crypto import encrypt_secret


def migrate_asset_passwords():
    """把历史明文 SSH 密码转为加密存储。"""
    db = SessionLocal()
    try:
        assets = db.query(Asset).all()
        changed = False
        for asset in assets:
            raw = (asset.ssh_password or "").strip()
            if not raw:
                continue
            if raw.startswith("enc::"):
                continue
            asset.ssh_password = encrypt_secret(raw)
            changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

def ensure_model_columns():
    """给已存在的表补齐模型新增的列（只增不删，逐列幂等）。

    create_all 只建缺失的表，**不会给已存在的表加列**；而开发库的表结构由
    create_all 维护、Alembic 迁移并不往开发库上跑，于是模型每加一列，老库就缺
    一列——直到某个查询把该列 SELECT 出来才炸。db_instances.allowed_roles 就曾
    让数据库管理页整页 500（UndefinedColumn），而缺列本身毫无征兆。

    启动时按元数据反射比对，缺什么补什么；单列失败只告警，不影响启动。
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in present:
                continue
            try:
                type_sql = column.type.compile(dialect=engine.dialect)
                ddl = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {type_sql}"
                # 没有服务端默认值时不加 NOT NULL，否则老数据行会违反约束
                if not column.nullable and column.server_default is not None:
                    ddl += " NOT NULL"
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table_name}.{column.name}")
            except Exception as exc:  # noqa: BLE001
                print(f"[OpsPilot] 补列失败 {table_name}.{column.name}: {exc}")
    if added:
        print(f"[OpsPilot] 已补齐缺失列 {len(added)} 个：{', '.join(added)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    logging.getLogger(__name__).info("OpsPilot backend starting (log file: logs/app.log)")
    if settings.DB_AUTO_CREATE_TABLES:
        # 本地开发/演示保留自动建表体验；生产环境建议关闭并使用 Alembic。
        Base.metadata.create_all(bind=engine)
        # create_all 不加列，模型后续新增的列在这里按元数据补齐（幂等）。
        ensure_model_columns()
    migrate_asset_passwords()
    # BackgroundTasks 运行在 Web 进程内，进程重启会中断执行中的发布任务。
    # 启动时先把超时 running 记录收敛为 failed，避免发布页永久卡在执行中。
    from app.api.applications import recover_stale_running_releases  # noqa: WPS433
    recover_stale_running_releases()
    # Alloy 二进制 gzip 预热：后台线程压缩 ~400MB（约 16s），避免首个 agent
    # 安装请求同步压缩把事件循环整个阻塞（Windows 后端实测踩过）。
    import threading  # noqa: WPS433

    def _preheat_alloy_binary_cache() -> None:
        try:
            from app.services.collector_bootstrap import (
                build_alloy_binary_gzip,
                local_alloy_binary_path,
            )

            path = local_alloy_binary_path()
            if path:
                build_alloy_binary_gzip(path)
                logging.getLogger(__name__).info("Alloy 二进制 gzip 缓存预热完成")
        except Exception as exc:  # 预热失败不阻断启动，安装时回退 apt
            logging.getLogger(__name__).warning("Alloy 二进制缓存预热失败（回退 apt 安装）: %s", exc)

    threading.Thread(target=_preheat_alloy_binary_cache, daemon=True, name="alloy-bin-preheat").start()
    # 同理：SQL 工单执行也跑在 Web 进程内，重启会留下永远 executing 的单。
    # 按认领心跳把超时的标记 failed 并注明"结果未知"，等人工核对目标库。
    from app.api.database import recover_stale_executing_tickets  # noqa: WPS433
    db = SessionLocal()
    try:
        recover_stale_executing_tickets(db)
    finally:
        db.close()
    # 仅做管理员账号引导，不注入任何演示数据。
    ensure_bootstrap_admin()
    # 注册本机为默认主机资产（内置 Docker Alloy 已覆盖采集，无需 SSH 安装）。
    ensure_localhost_asset()
    # 首次启动写入监控连接（MONITORING_HOST / VM_URL…），已配置则不覆盖。
    ensure_monitoring_config()
    # 自建告警分发器需要主事件循环，用于恢复防抖协程。
    from app.api.monitoring.services.alert_dispatcher import set_main_event_loop  # noqa: WPS433
    set_main_event_loop(asyncio.get_running_loop())

    # 后台任务统一由 SchedulerManager 管理：配置持久化、启停、动态周期和幂等句柄。
    from app.core.scheduler_registry import scheduler_manager  # noqa: WPS433
    scheduler_db = SessionLocal()
    try:
        await scheduler_manager.start_all(scheduler_db)
    finally:
        scheduler_db.close()

    # SharedInformer 管理器初始化
    from app.services.k8s_informer import InformerManager  # noqa: WPS433
    from app.api.clusters import _get_api_client  # noqa: WPS433
    informer_manager = InformerManager.get_instance()
    informer_manager.initialize(loop=asyncio.get_running_loop(), api_client_factory=_get_api_client)

    yield
    await scheduler_manager.shutdown()
    await informer_manager.shutdown()


app = FastAPI(
    title="OpsPilot API",
    description="Enterprise OpsPilot Backend Services",
    version="1.0.0",
    lifespan=lifespan
)

# 配置 CORS，允许前端直接向后端发送跨域请求 (兜底非代理访问)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由
app.include_router(ai_router, prefix="/api/ai", tags=["AI Agent"], dependencies=[Depends(get_current_user)])
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(
    dashboard_router,
    prefix="/api/dashboard",
    tags=["Dashboard"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.dashboard"))],
)
app.include_router(
    assets_router,
    prefix="/api/assets",
    tags=["Assets"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.hosts"))],
)
app.include_router(
    groups_router,
    prefix="/api/groups",
    tags=["Groups"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.hosts"))],
)
app.include_router(
    domains_router,
    prefix="/api/domains",
    tags=["Domains"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.domains"))],
)
app.include_router(
    certificates_router,
    prefix="/api/certificates",
    tags=["Certificates"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.certificates"))],
)
app.include_router(
    creds_router,
    prefix="/api/credentials",
    tags=["Credentials"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.credentials"))],
)
app.include_router(
    applications_router,
    prefix="/api/applications",
    tags=["Applications"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.apps.list"))],
)
app.include_router(
    clusters_router,
    prefix="/api/clusters",
    tags=["Clusters"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.clusters"))],
)
# K8s Pod 日志/Exec WebSocket 使用短期一次性 session token。
# 这里不能继承主集群 router 的 HTTP Bearer 依赖，否则 WebSocket 握手会触发
# OAuth2PasswordBearer 的 Request 解析错误。
app.include_router(clusters_ws_router, prefix="/api/clusters", tags=["Clusters WebSocket"])
# K8s Watch SSE（session token 认证，不走 Bearer）
app.include_router(k8s_watch_router, prefix="/api/clusters", tags=["K8s Watch SSE"])
app.include_router(monitoring_router, prefix="/api/monitoring", tags=["Monitoring"], dependencies=[Depends(get_current_user)])
app.include_router(collector_router, prefix="/api/monitoring/collector", tags=["Collector Fleet"])
app.include_router(collector_agent_router, prefix="/api/monitoring/collector/agent", tags=["Collector Agent"])
app.include_router(grafana_sso_router, tags=["Grafana SSO"])
app.include_router(monitoring_webhooks_router, prefix="/api/monitoring/webhooks", tags=["Monitoring Webhooks"])
app.include_router(alert_center_router, prefix="/api/monitoring/alerts", tags=["Alert Center"])
# 登录页配置接口需要在未登录时访问，单独挂载公开路由（无 get_current_user 依赖）。
app.include_router(public_settings_router, prefix="/api/settings", tags=["Settings"])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"], dependencies=[Depends(get_current_user)])
app.include_router(
    vms_router,
    prefix="/api/vms",
    tags=["Virtual Machines"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.vms"))],
)
# WebSocket 终端通道不走 OAuth header（浏览器 WS 无法稳定携带），改为一次性会话 token 校验。
app.include_router(terminal_ws_router, prefix="/api/terminal", tags=["Terminal"])
app.include_router(
    tools_router,
    prefix="/api/tools",
    tags=["Tools"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.tools.scripts"))],
)
app.include_router(
    wireguard_router,
    prefix="/api/wireguard",
    tags=["WireGuard"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.wireguard.manage"))],
)
app.include_router(
    database_router,
    prefix="/api/db",
    tags=["Database"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.assets.databases"))],
)
app.include_router(
    finance_router,
    prefix="/api/finance",
    tags=["Finance"],
    dependencies=[Depends(get_current_user), Depends(require_permissions("module.finance.overview"))],
)
app.include_router(notifications_router, prefix="/api/notifications", tags=["Notifications"])
# 个人 IM 渠道（飞书群 @ 通知）：所有登录用户可绑定，管理员配置项在路由内单独鉴权
app.include_router(im_router, prefix="/api/im", tags=["IM"], dependencies=[Depends(get_current_user)])
app.include_router(cmdb_router, prefix="/api/cmdb", tags=["CMDB"], dependencies=[Depends(get_current_user)])
app.include_router(changes_router, prefix="/api/changes", tags=["Change Tickets"], dependencies=[Depends(get_current_user)])

AUDIT_METHOD_ACTION_MAP = {
    "POST": "创建",
    "PUT": "更新",
    "DELETE": "删除",
}

AUDIT_RISK_ROUTE_RULES = (
    ("GET", re.compile(r"^/api/credentials/[^/]+/export$"), "action.assets.credentials.export", "export"),
    ("POST", re.compile(r"^/api/credentials/batch-export$"), "action.assets.credentials.export", "export"),
    ("GET", re.compile(r"^/api/certificates/[^/]+/download$"), "action.assets.certificates.download", "download"),
    ("POST", re.compile(r"^/api/certificates/[^/]+/deploy$"), "action.assets.certificates.manage", "deploy"),
    ("POST", re.compile(r"^/api/certificates/batch-delete$"), "action.assets.certificates.manage", "delete"),
    ("DELETE", re.compile(r"^/api/certificates/[^/]+$"), "action.assets.certificates.manage", "delete"),
    ("DELETE", re.compile(r"^/api/clusters/[^/]+$"), "action.assets.clusters.manage", "delete"),
)


def resolve_request_risk_level(method: str, path: str) -> str:
    """按后端风险注册表解析请求风险等级。"""
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    for rule_method, pattern, permission_key, action_id in AUDIT_RISK_ROUTE_RULES:
        if normalized_method == rule_method and pattern.match(normalized_path):
            return get_risk_level(permission_key, action_id)
    return "low"


@app.middleware("http")
async def audit_write_operations(request, call_next):
    method = request.method.upper()
    path = request.url.path
    risk_level = resolve_request_risk_level(method, path)
    should_audit = (
        path.startswith("/api/")
        and (method in AUDIT_METHOD_ACTION_MAP or risk_level != "low")
    )
    if not should_audit:
        return await call_next(request)

    response = await call_next(request)

    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()

    username = "未知用户"
    display_name = "未知用户"
    if token:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            username = payload.get("sub") or username
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.username == username).first()
                if user and user.name:
                    display_name = user.name
                else:
                    display_name = username
            finally:
                db.close()
        except JWTError:
            pass

    db = SessionLocal()
    try:
        db.add(
            AuditLog(
                user=display_name,
                action=f"{AUDIT_METHOD_ACTION_MAP.get(method, '读取')} {path}",
                resource=path,
                ip=request.client.host if request.client else "-",
                result="success" if response.status_code < 400 else "failed",
                risk_level=risk_level,
                risk_reason=unquote(request.headers.get("X-Risk-Reason", "")),
            )
        )
        db.commit()
    finally:
        db.close()

    return response

@app.get("/")
def root():
    return {"status": "ok", "message": "OpsPilot FastAPI Backend is Running"}

if __name__ == "__main__":
    import uvicorn
    # 本地开发模式启动服务器
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)


from pydantic import BaseModel
class LogError(BaseModel):
    msg: str
    stack: str

@app.post("/api/log_error")
async def log_error(error: LogError):
    print("================ FRONTEND ERROR ==================")
    print("MSG:", error.msg)
    print("STACK:", error.stack)
    print("==================================================")
    return {"status": "ok"}
