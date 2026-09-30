"""后台任务跨进程租约表。

多 worker / 多进程部署时，进程内调度器（scheduler_registry）、账单同步
daemon、发布 BackgroundTasks 都会在每个进程各跑一份。本表用 lease_key 的
唯一约束 + 过期时间提供一把跨进程的互斥锁，防止同一任务被重复执行。

时间字段（acquired_at / heartbeat_at / expires_at）统一存
`YYYY-MM-DD HH:MM:SS` 字符串（与 format_app_datetime 一致），该格式的
字符串比较与时间先后完全等价，因此 SQL 里可直接做 `expires_at < now` 的
字典序比较来判断租约是否过期，无需引入额外类型转换。
"""
from sqlalchemy import Column, Integer, String

from app.db.database import Base


class TaskLease(Base):
    """任务租约：同一 lease_key 全局最多存在一行，即同一时刻最多一个持有者。"""

    __tablename__ = "task_leases"

    id = Column(Integer, primary_key=True, index=True)
    lease_key = Column(
        String(120),
        nullable=False,
        unique=True,
        index=True,
        comment="租约键，如 scheduler:bill_sync / bill-sync:3 / release:REL-XXXX",
    )
    owner = Column(
        String(120),
        nullable=False,
        comment="持有者实例标识（主机名:进程号:随机串，见 leases.lease_owner）",
    )
    acquired_at = Column(String(30), default="", comment="获取时间")
    heartbeat_at = Column(String(30), default="", comment="最近一次续约/保活时间")
    expires_at = Column(String(30), default="", comment="过期时间；字符串比较即时间比较")
