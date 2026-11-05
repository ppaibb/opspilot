"""
凭证引用计数联动服务
"""
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.models.asset import Asset, DomainRecord
from app.models.credential import Credential


def sync_credential_usage(db: Session, cred_ids: set[str] | None = None) -> None:
    """
    根据资产表中的 SSH 和云 API 凭证引用重新计算 Credential.used_by。
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

    for cred in credentials:
        count = (
            db.query(func.count(Asset.id))
            # 同一资产若两个字段引用同一凭证，OR 查询也只计数一次。
            .filter(or_(Asset.credential_ref == cred.cred_id, Asset.cloud_credential_ref == cred.cred_id))
            .scalar()
        ) or 0
        # 只读快照仍依赖原凭证的授权规则，也计引用，防止凭证被误删。
        domain_count = db.query(func.count(DomainRecord.id)).filter(or_(
            DomainRecord.account_id == cred.cred_id,
            DomainRecord.snapshot_credential_id == cred.cred_id,
        )).scalar() or 0
        cred.used_by = int(count + domain_count)
