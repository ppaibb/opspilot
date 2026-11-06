"""把已知敏感字段从历史明文/v1 密文迁移到 v2。

用法（在 backend 目录）：
    python -m scripts.migrate_credential_ciphertext --dry-run
    python -m scripts.migrate_credential_ciphertext --batch-size 100

日志只输出表名和记录 ID，绝不输出明文、密文或密钥。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret, encrypt_secret
from app.db.database import SessionLocal
from app.models.asset import Asset
from app.models.credential import Credential
from app.models.database import DbInstance
from app.models.wireguard import WireGuardNetwork, WireGuardPeer

LOGGER = logging.getLogger("credential_ciphertext_migration")
V2_PREFIX = "enc::v2::"


@dataclass(frozen=True)
class SensitiveColumn:
    """一处需要迁移的模型敏感字段。"""

    model: type[Any]
    attribute: str


SENSITIVE_COLUMNS = (
    SensitiveColumn(Credential, "content"),
    SensitiveColumn(Asset, "ssh_password"),
    SensitiveColumn(DbInstance, "password"),
    SensitiveColumn(WireGuardNetwork, "server_private_key"),
    SensitiveColumn(WireGuardPeer, "private_key"),
)


def needs_migration(value: str | None) -> bool:
    """仅重写非空且尚未采用 v2 格式的值。"""
    return bool(value and value.strip() and not value.strip().startswith(V2_PREFIX))


def migrate_column(
    db: Session,
    spec: SensitiveColumn,
    *,
    batch_size: int,
    dry_run: bool,
) -> int:
    """逐批迁移单列，并只记录行标识。"""
    changed = 0
    last_id = 0
    while True:
        rows = (
            db.query(spec.model)
            .filter(spec.model.id > last_id)
            .order_by(spec.model.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            break
        for row in rows:
            last_id = int(row.id)
            value = getattr(row, spec.attribute)
            if not needs_migration(value):
                continue
            # 解密失败会终止整次迁移，避免把损坏数据覆盖成空值。
            plaintext = decrypt_secret(value)
            setattr(row, spec.attribute, encrypt_secret(plaintext))
            changed += 1
            LOGGER.info("待迁移 %s id=%s", spec.model.__tablename__, row.id)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    return changed


def run_migration(*, batch_size: int, dry_run: bool) -> int:
    """迁移所有已登记敏感列，返回重写记录总数。"""
    if batch_size <= 0:
        raise ValueError("batch-size 必须大于 0")
    total = 0
    db = SessionLocal()
    try:
        for spec in SENSITIVE_COLUMNS:
            total += migrate_column(
                db,
                spec,
                batch_size=batch_size,
                dry_run=dry_run,
            )
        if dry_run:
            db.rollback()
        return total
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="迁移 OpsPilot 凭证密文到 v2")
    parser.add_argument("--dry-run", action="store_true", help="仅检查，不提交数据库")
    parser.add_argument("--batch-size", type=int, default=100, help="每批记录数")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    count = run_migration(batch_size=args.batch_size, dry_run=args.dry_run)
    mode = "检查" if args.dry_run else "迁移"
    LOGGER.info("%s完成，共 %s 条记录", mode, count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
