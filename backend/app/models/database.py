"""
数据库管理表（类 Yearning 的 SQL 工单平台，P0 范围）。

安全约定：
1. 实例密码 encrypt_secret 加密落库，接口不回传明文（has_password 标记）；
2. 工单 SQL 全文落库便于审计；执行结果（含错误）逐条记录；
3. 工单状态机：pending(待审批，多级审批链逐步推进) → approved(已批准/待执行) →
   executing → success / failed / rejected；查询工单终审通过即生成时限授权
   （db_query_grants），不走执行环节；
4. 自助查询需本人名下生效中的查询授权（管理员豁免），全部落查询日志。
"""
from sqlalchemy import Column, Integer, String, Text, JSON

from app.db.database import Base


class DbInstance(Base):
    """数据库实例（数据源）：MySQL / PostgreSQL。"""

    __tablename__ = "db_instances"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, comment="实例名称，如 生产订单库")
    db_type = Column(String(20), default="mysql", comment="mysql / postgresql")
    host = Column(String(200), nullable=False, comment="主机地址")
    port = Column(Integer, default=3306, comment="端口（MySQL 3306 / PG 5432）")
    database = Column(String(100), default="", comment="默认库名（PG 必填）")
    username = Column(String(100), nullable=False, comment="连接用户")
    password = Column(Text, default="", comment="连接密码（加密）")
    remark = Column(String(500), default="", comment="备注")
    status = Column(String(20), default="unknown", comment="unknown/ok/error")
    status_message = Column(String(500), default="", comment="最近连通测试信息")
    # 使用授权（JSON 角色数组；空=所有具备 action.db.execute 的角色可用）——
    # 与凭证 allowed_roles 同语义：控制"谁能把这个库作为工单/查询目标"。
    allowed_roles = Column(String(2000), default="", comment="授权权限组（JSON 数组，空=全部）")
    created_at = Column(String(50), comment="创建时间")


class DbTicket(Base):
    """SQL 工单：提交 → 自动审核 → 人工审批（可多级）→ 执行 → 结果。"""

    __tablename__ = "db_tickets"

    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(Integer, nullable=False, index=True, comment="目标实例")
    title = Column(String(200), default="", comment="工单标题")
    sql_text = Column(Text, nullable=False, comment="SQL 全文（; 分隔多语句）；查询授权工单存申请说明")
    database = Column(String(100), default="", comment="目标库（空=实例默认库）；查询工单=授权目标库")
    ticket_type = Column(String(20), default="change", comment="change(变更) / query(查询授权)")
    status = Column(String(20), default="pending", comment="pending/approved/executing/success/failed/rejected")
    # 自动审核结果：[{rule, level(pass/warn/block), message}] + 总体 pass/blocked。
    audit_result = Column(JSON, default=list, comment="规则引擎审核明细")
    audit_passed = Column(Integer, default=1, comment="审核是否通过（无 block 项）: 1/0")
    # 执行结果：[{sql, ok, rows_affected, error, duration_ms}]。
    exec_result = Column(JSON, default=list, comment="逐条执行结果")
    submitter = Column(String(100), default="", comment="提交人（展示名）")
    reviewer = Column(String(100), default="", comment="最终审批人（多级时为最后一级）")
    executor = Column(String(100), default="", comment="执行人")
    review_note = Column(String(500), default="", comment="最终审批意见")
    # 多级审批链：提交时按审批流配置快照（JSON 角色名数组；空=单级模式，任何审批人可审）。
    approval_steps = Column(JSON, default=list, comment="审批链快照：[角色名, ...]")
    current_step = Column(Integer, default=1, comment="当前审批级（1 起）；链空时恒为 1")
    approval_log = Column(JSON, default=list, comment="逐级审批记录：[{step, role, reviewer, decision, note, at}]")
    # 查询授权申请明细：{tables: [...], hours: n, reason: "..."}（仅 query 工单）。
    grant_detail = Column(JSON, default=dict, comment="查询授权申请明细")
    # 定时执行：批准后不立即执行，由后台 runner 在该时间点自动执行（空=立即）。
    execute_after = Column(String(50), default="", comment="定时执行时间（空=立即）")
    # 变更安全网：UPDATE/DELETE 执行前把受影响行快照 + 反向 INSERT 语句存档，
    # 事后可导出回滚（Yearning 的 rollback 能力的务实版）。
    backup_snapshot = Column(Text, comment="受影响行备份（JSON：{sql, rows, columns}）")
    rollback_statements = Column(Text, comment="回滚语句（多条 INSERT，分号分隔）")
    created_at = Column(String(50), comment="提交时间")
    executed_at = Column(String(50), default="", comment="执行完成时间")
    # 执行心跳：认领进入 executing 时写入，执行完成随结果落 executed_at。
    # executing 且心跳长时间未刷新 → 执行器中断（进程重启/崩溃），由
    # recover_stale_executing_tickets 标记 failed，避免工单永久卡在执行中。
    heartbeat_at = Column(String(50), default="", comment="执行认领心跳时间（executing 态）")


class DbQueryGrant(Base):
    """查询授权：查询工单终审通过后生成，时限内允许自助查询目标库。

    生命周期：active（生效中）→ expired（到期，scheduler 翻转）/ revoked（被吊销）。
    与 Yearning 的查询授权对齐：先审批、后授权、到期回收，全程可审计。
    """

    __tablename__ = "db_query_grants"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, index=True, default=0, comment="来源查询工单（手动授权时为 0）")
    instance_id = Column(Integer, nullable=False, index=True, comment="授权实例")
    username = Column(String(100), nullable=False, index=True, comment="被授权人账号（唯一标识）")
    display_name = Column(String(100), default="", comment="被授权人展示名")
    database = Column(String(100), nullable=False, comment="授权目标库")
    # 表范围仅作为审批参考与审计信息，不在执行层强制拦截（SQL 表名解析不可靠）。
    tables = Column(Text, default="", comment="表范围（JSON 数组文本，空=全部表，审批参考用）")
    reason = Column(String(500), default="", comment="申请理由")
    expires_at = Column(String(50), nullable=False, comment="失效时间 YYYY-MM-DD HH:MM:SS")
    granted_by = Column(String(100), default="", comment="授权人（终审通过时为最后一级审批人）")
    status = Column(String(20), default="active", index=True, comment="active/expired/revoked")
    created_at = Column(String(50), comment="创建时间")


class DbQueryLog(Base):
    """自助查询日志（只读 SELECT 的审计记录）。"""

    __tablename__ = "db_query_logs"

    id = Column(Integer, primary_key=True, index=True)
    instance_id = Column(Integer, nullable=False, index=True)
    username = Column(String(100), default="", comment="查询人")
    sql_text = Column(Text, comment="查询语句")
    # 实际执行时连的库。实例本身有默认库，但查询面板可以临时切库，
    # 缺了这一列就分不清"表不存在"是 SQL 写错了还是连错了库。
    # server_default 与迁移保持一致：create_all/补列/Alembic 三条路建出来的
    # 列约束相同，不会出现开发库可空而生产库 NOT NULL 的分裂。
    database = Column(String(100), nullable=False, server_default="", default="",
                      comment="实际连接的目标库")
    row_count = Column(Integer, default=0, comment="返回行数")
    duration_ms = Column(Integer, default=0, comment="耗时")
    ok = Column(Integer, default=1, comment="是否成功: 1/0")
    error = Column(String(500), default="", comment="失败原因")
    created_at = Column(String(50), comment="查询时间")
