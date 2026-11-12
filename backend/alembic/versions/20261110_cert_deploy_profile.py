"""证书部署目标档案列 certificates.deploy_profile_json。

Revision ID: 20261110_cert_deploy_profile
Revises: 20261109_cert_deployments

续期成功后自动重部署（P0-1）需要一张证书的部署目标持久化档案。
旧 deploy_targets 列不可信：255 字摘要、host 路从不写、编辑证书时清零，
因此新增 JSON 文本列，只由部署派发写入、编辑不碰。
开发库靠启动时 ensure_model_columns 补列，生产靠本迁移。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app import alembic_guards as g

revision = "20261110_cert_deploy_profile"
down_revision = "20261109_cert_deployments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.add_column_if_missing(
        op,
        "certificates",
        sa.Column("deploy_profile_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    g.drop_column_if_exists(op, "certificates", "deploy_profile_json")
