"""证书部署历史表 certificate_deployments。

Revision ID: 20261109_cert_deployments
Revises: 20261106_auth_sessions
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app import alembic_guards as g

revision = "20261109_cert_deployments"
down_revision = "20261106_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    g.create_table_if_missing(
        op,
        "certificate_deployments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deploy_id", sa.String(length=50), unique=True, nullable=True),
        sa.Column("cert_id", sa.String(length=50), nullable=True),
        sa.Column("target_type", sa.String(length=20), nullable=True),
        sa.Column("targets", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("results_json", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.String(length=30), nullable=True),
        sa.Column("finished_at", sa.String(length=30), nullable=True),
    )
    g.create_index_if_missing(op, "ix_certificate_deployments_cert_id", "certificate_deployments", ["cert_id"])
    g.create_index_if_missing(op, "ix_certificate_deployments_deploy_id", "certificate_deployments", ["deploy_id"])
    g.create_index_if_missing(op, "ix_certificate_deployments_id", "certificate_deployments", ["id"])


def downgrade() -> None:
    g.drop_index_if_exists(op, "ix_certificate_deployments_id", "certificate_deployments")
    g.drop_index_if_exists(op, "ix_certificate_deployments_deploy_id", "certificate_deployments")
    g.drop_index_if_exists(op, "ix_certificate_deployments_cert_id", "certificate_deployments")
    g.drop_table_if_exists(op, "certificate_deployments")
