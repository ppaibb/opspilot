"""create wireguard tables (wg_networks / wg_peers)

Revision ID: 20260919_wireguard
Revises: 20260917_finance
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20260919_wireguard'
down_revision = '20260917_finance'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'wg_networks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), nullable=False, comment='组网名称'),
        sa.Column('cidr', sa.String(50), nullable=False, comment='隧道网段'),
        sa.Column('server_asset_id', sa.String(50), server_default='', comment='出口节点资产 ID'),
        sa.Column('server_name', sa.String(100), server_default='', comment='出口节点名称快照'),
        sa.Column('endpoint', sa.String(200), nullable=False, comment='对端可达地址'),
        sa.Column('port', sa.Integer(), server_default='51820', comment='监听端口'),
        sa.Column('server_private_key', sa.Text(), comment='服务端私钥（加密）'),
        sa.Column('server_public_key', sa.String(100), server_default='', comment='服务端公钥'),
        sa.Column('forward_iface', sa.String(30), server_default='eth0', comment='出口网卡'),
        sa.Column('full_tunnel', sa.Integer(), server_default='0', comment='成员全隧道: 1/0'),
        sa.Column('dns', sa.String(200), server_default='', comment='成员 DNS'),
        sa.Column('remark', sa.String(500), server_default='', comment='备注'),
        sa.Column('created_at', sa.String(50), comment='创建时间'),
        comment='WireGuard 虚拟组网',
    )

    op.create_table(
        'wg_peers',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('network_id', sa.Integer(), nullable=False, comment='所属组网'),
        sa.Column('name', sa.String(100), nullable=False, comment='成员名称'),
        sa.Column('asset_id', sa.String(50), server_default='', comment='关联资产 ID'),
        sa.Column('allowed_ip', sa.String(50), nullable=False, comment='隧道内 IP'),
        sa.Column('private_key', sa.Text(), comment='成员私钥（加密）'),
        sa.Column('public_key', sa.String(100), server_default='', comment='成员公钥'),
        sa.Column('enabled', sa.Integer(), server_default='1', comment='启用: 1/0'),
        sa.Column('remark', sa.String(500), server_default='', comment='备注'),
        sa.Column('created_at', sa.String(50), comment='创建时间'),
        sa.UniqueConstraint('network_id', 'allowed_ip', name='uq_wg_peer_ip'),
        comment='WireGuard 成员对端',
    )
    op.create_index('idx_wg_peers_network', 'wg_peers', ['network_id'])


def downgrade():
    op.drop_table('wg_peers')
    op.drop_table('wg_networks')
