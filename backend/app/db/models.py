"""集中注册 SQLAlchemy ORM 模型。

Alembic、启动初始化脚本和测试都依赖 ``Base.metadata`` 能看到完整表结构。
把模型导入集中到这里，可以避免每个入口各自维护一份清单导致漏表。
"""
from __future__ import annotations

from app.models.application import (  # noqa: F401
    Application,
    ApplicationComponent,
    ApplicationConfig,
    ApplicationConfigRevision,
    ApplicationHealthSnapshot,
    ApplicationLink,
    ApplicationRelease,
    ApplicationResource,
)
from app.models.asset import Asset, DomainDNSRecord, DomainRecord  # noqa: F401
from app.models.audit import AuditLog  # noqa: F401
from app.models.certificate import CertificateRecord  # noqa: F401
from app.models.cluster import Cluster  # noqa: F401
from app.models.collector import (  # noqa: F401
    CollectorAgent,
    CollectorConfigVersion,
    CollectorGroup,
    CollectorPolicy,
    CollectorRegistrationToken,
    CollectorRelease,
)
from app.models.credential import Credential  # noqa: F401
from app.models.resource_group import ResourceGroup  # noqa: F401
from app.models.resource_group_member import ResourceGroupMember  # noqa: F401
from app.models.monitoring_webhook_event import MonitoringWebhookEvent  # noqa: F401
from app.models.monitoring_active_alert import MonitoringActiveAlert  # noqa: F401
from app.models.monitoring_history_alert import MonitoringHistoryAlert  # noqa: F401
from app.models.monitoring_notification_log import MonitoringNotificationLog  # noqa: F401
from app.models.system_config import SystemConfig  # noqa: F401

from app.models.user import User  # noqa: F401
from app.models.user_im_binding import UserImBinding  # noqa: F401
from app.models.vm import VirtualMachine  # noqa: F401
from app.models.dashboard_snapshot import DashboardSnapshot  # noqa: F401
from app.models.ai_chat import AIChatSession, AIChatMessage  # noqa: F401
from app.models.notification import UserNotification  # noqa: F401

# 下面几块是后补的。漏注册的后果不只是"建表漏一张"—— Alembic 的
# target_metadata 来自本文件，漏一个类，autogenerate 就会把那张表判成
# "库里多出来的表"并生成 DROP TABLE。数据库管理、费用、部署任务、
# WireGuard、影子告警、根因分析都曾因此不在 Alembic 视野里。
from app.models.database import DbInstance, DbQueryGrant, DbQueryLog, DbTicket  # noqa: F401
from app.models.finance import BillSyncRun, CloudAccount, CloudBill  # noqa: F401
from app.models.deployment_task import ApplicationDeploymentTask  # noqa: F401
from app.models.wireguard import WireGuardNetwork, WireGuardPeer  # noqa: F401
from app.models.monitoring_shadow_alert import MonitoringShadowAlert  # noqa: F401
from app.models.root_cause import AlertRootCause  # noqa: F401
from app.models.risk_ticket import RiskAckTicket  # noqa: F401
from app.models.asset_host_key import AssetHostKey  # noqa: F401
from app.models.change_ticket import ChangeTicket  # noqa: F401
from app.models.task_lease import TaskLease  # noqa: F401
