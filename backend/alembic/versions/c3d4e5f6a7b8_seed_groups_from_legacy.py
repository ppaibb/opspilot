"""seed resource_groups from legacy asset status group names

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-23 10:25:00.000000

把旧的"业务分组标签"(存在 asset.status 第2段的字符串组名)导入结构化
resource_groups 表，并回填 asset.group_id。默认组名("默认分组"/"默认应用组")
视为未分组(group_id 保持 NULL)。

可回滚：downgrade 会清空本次导入的分组行并把所有 asset.group_id 置 NULL。
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


# 与 app/api/assets.py parse_asset_group 保持一致
_CLOUD_KEYWORDS = {
    'running', 'stopped', 'terminated', 'starting', 'stopping', 'unknown',
    '真·阿里云', '未知状态', 'ecs', 'cvm', 'ec2', 'swas', 'lighthouse', 'lightsail',
    '云主机', '阿里云轻量', '腾讯云主机', '腾讯云轻量', 'aws ec2', 'aws lightsail',
}
_DEFAULT_GROUP_NAMES = {'默认分组', '默认应用组', ''}

# 预设调色板，循环分配
_PALETTE = [
    '#2563eb', '#16a34a', '#d97706', '#dc2626', '#7c3aed',
    '#0891b2', '#db2777', '#65a30d', '#ea580c', '#4f46e5',
]


def _parse_group(status: str) -> str:
    if not status:
        return ''
    parts = [item.strip() for item in str(status).split(',')]
    if len(parts) > 1:
        for val in parts[1:]:
            if val and val.lower() not in _CLOUD_KEYWORDS:
                return val
    return ''


def upgrade() -> None:
    bind = op.get_bind()

    # 读取现有主机的 status，解析旧组名
    rows = bind.execute(
        sa.text("SELECT id, status FROM assets")
    ).fetchall()

    # 收集去重的有效组名（保留首次出现顺序）
    seen = []
    asset_group_name = {}
    for asset_id, status in rows:
        name = _parse_group(status)
        if name in _DEFAULT_GROUP_NAMES:
            asset_group_name[asset_id] = None
            continue
        asset_group_name[asset_id] = name
        if name not in seen:
            seen.append(name)

    # 插入分组并拿到 id（兼容已存在的同名分组：先查后插）
    now = datetime.utcnow()
    name_to_id = {}
    for idx, name in enumerate(seen):
        existing = bind.execute(
            sa.text("SELECT id FROM resource_groups WHERE name = :n"),
            {"n": name},
        ).fetchone()
        if existing:
            name_to_id[name] = existing[0]
            continue
        result = bind.execute(
            sa.text(
                "INSERT INTO resource_groups (name, color, description, sort_order, created_at, updated_at) "
                "VALUES (:n, :c, :d, :s, :ca, :ua) RETURNING id"
            ),
            {
                "n": name,
                "c": _PALETTE[idx % len(_PALETTE)],
                "d": "",
                "s": idx,
                "ca": now,
                "ua": now,
            },
        )
        name_to_id[name] = result.fetchone()[0]

    # 回填 asset.group_id
    for asset_id, name in asset_group_name.items():
        if name is None:
            continue
        gid = name_to_id.get(name)
        if gid is None:
            continue
        bind.execute(
            sa.text("UPDATE assets SET group_id = :g WHERE id = :a"),
            {"g": gid, "a": asset_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    # 解除所有主机的分组关联，再清空分组行（本迁移导入的数据）
    bind.execute(sa.text("UPDATE assets SET group_id = NULL"))
    bind.execute(sa.text("DELETE FROM resource_groups"))
