"""
凭证引用计数联动服务
"""
from sqlalchemy import func, inspect, select, union_all
from sqlalchemy.orm import Session

from app.models.application import ApplicationConfig
from app.models.asset import Asset, DomainRecord
from app.models.certificate import CertificateRecord
from app.models.credential import Credential
from app.models.finance import CloudAccount


def _ref_select(model, column):
    """取某表某引用列的 (cred_id, 行 id)：空串/NULL 不算引用。"""
    return select(column.label("cid"), model.id.label("rid")).where(
        column.isnot(None), column != ""
    )


def _dedup_ref_select(model, col_a, col_b):
    """取某表两列引用的并集：同一行两列指向同一凭证时只算一次。

    与旧逻辑的 OR 计数口径一致（同一资产 credential_ref 与
    cloud_credential_ref 指向同一凭证只计数一次；域名 account_id 与
    snapshot_credential_id 同理）。内层 UNION 必须先包成子查询，
    否则 SQLAlchemy 在外层 UNION ALL 里渲染不出合法 SQL。
    """
    sub = _ref_select(model, col_a).union(_ref_select(model, col_b)).subquery()
    return select(sub.c.cid, sub.c.rid)


def reference_counts(db: Session) -> dict[str, int]:
    """一条聚合查询算出全部凭证的引用数。

    覆盖：资产（SSH/云凭证两列）、域名（账户/快照两列）、财务云账号、
    证书（签发账户）、应用配置。证书的部署请求 cloud_account_id 是
    单次部署的临时入参，不落库（部署历史只存目标摘要），不算持久引用，
    故只计 CertificateRecord.account_id。

    兼容性：只对库里实际存在的表做聚合。部分调用方（如领域测试的
    内存库）只建了资产/域名两张表，缺表时按 0 引用计，不抛错；
    生产库五张表都在，不受影响。表清单必须用会话自身的连接来查
    （inspect(db.connection())）：若另开连接，StaticPool 内存库下会
    与会话共用同一 DBAPI 连接，Inspector 关闭连接时会回滚会话
    未提交的事务（曾导致刚 flush 的域名行在 commit 后凭空消失）。
    """
    try:
        existing_tables = set(inspect(db.connection()).get_table_names())
    except Exception:
        existing_tables = None  # 拿不到表清单时按全量查，让错误原样抛出
    parts = []
    if existing_tables is None or Asset.__tablename__ in existing_tables:
        parts.append(_dedup_ref_select(Asset, Asset.credential_ref, Asset.cloud_credential_ref))
    if existing_tables is None or DomainRecord.__tablename__ in existing_tables:
        parts.append(_dedup_ref_select(DomainRecord, DomainRecord.account_id, DomainRecord.snapshot_credential_id))
    if existing_tables is None or CloudAccount.__tablename__ in existing_tables:
        parts.append(_ref_select(CloudAccount, CloudAccount.credential_ref))
    if existing_tables is None or CertificateRecord.__tablename__ in existing_tables:
        parts.append(_ref_select(CertificateRecord, CertificateRecord.account_id))
    if existing_tables is None or ApplicationConfig.__tablename__ in existing_tables:
        parts.append(_ref_select(ApplicationConfig, ApplicationConfig.credential_ref))
    if not parts:
        return {}
    combined = union_all(*parts).subquery()
    rows = db.execute(
        select(combined.c.cid, func.count()).group_by(combined.c.cid)
    ).all()
    return {str(cid): int(count) for cid, count in rows}


def sync_credential_usage(db: Session, cred_ids: set[str] | None = None) -> None:
    """
    根据各表中的凭证引用重新计算 Credential.used_by。
    cred_ids 为空时全量同步；非空时只同步指定凭证。
    """
    # 当前项目 Session 配置了 autoflush=False，需要手动 flush 才能读到最新变更。
    db.flush()

    if cred_ids is None:
        credentials = db.query(Credential).all()
    else:
        ids = [cid for cid in cred_ids if cid]
        if not ids:
            return
        credentials = db.query(Credential).filter(Credential.cred_id.in_(ids)).all()

    counts = reference_counts(db)
    for cred in credentials:
        cred.used_by = int(counts.get(cred.cred_id, 0))
