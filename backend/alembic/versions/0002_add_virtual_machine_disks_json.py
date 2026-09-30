"""为虚机资产增加磁盘明细字段。"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0002_vm_disks_json"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增虚机磁盘明细 JSON 字符串列。"""
    bind = op.get_bind()
    inspector = inspect(bind)
    if "virtual_machines" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("virtual_machines")}
    if "disks_json" in columns:
        return
    op.add_column(
        "virtual_machines",
        sa.Column("disks_json", sa.String(length=2000), nullable=True, server_default="[]"),
    )


def downgrade() -> None:
    """回滚虚机磁盘明细列。"""
    bind = op.get_bind()
    inspector = inspect(bind)
    if "virtual_machines" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("virtual_machines")}
    if "disks_json" not in columns:
        return
    with op.batch_alter_table("virtual_machines") as batch_op:
        batch_op.drop_column("disks_json")
