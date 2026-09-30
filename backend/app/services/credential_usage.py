"""
凭证引用计数联动服务
"""
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.asset import Asset
from app.models.credential import Credential


def sync_credential_usage(db: Session, cred_ids: set[str] | None = None) -> None:
    """
    根据资产表中的 credential_ref 重新计算并回写 Credential.used_by。
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
            .filter(Asset.credential_ref == cred.cred_id)
            .scalar()
        ) or 0
        cred.used_by = int(count)
