"""主机管理 P0/P1 回归：密码保留语义、删除清理分组成员、列表鉴权。

背景（2026-09-24 全链路审查）：
- 编辑/移动/SSH 测试传空密码时会误清空已存 SSH 密码；
- 删除主机不清理 resource_group_members，分组计数虚高；
- GET /assets 与 /assets/summary 缺少认证依赖。
"""
import inspect
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.api.assets as assets_api
from app.api.assets import (
    apply_password_update,
    delete_asset_record,
    get_assets,
    get_summary,
    resolve_update_password,
)
from app.db.database import Base
from app.models.asset import Asset
from app.models.resource_group_member import RESOURCE_TYPE_HOST, ResourceGroupMember


# ---------------------------- 密码保留语义 ----------------------------


def test_password_update_keeps_existing_when_blank():
    asset = SimpleNamespace(ssh_password="EXISTING")

    apply_password_update(asset, "")
    assert asset.ssh_password == "EXISTING"

    apply_password_update(asset, "   ")
    assert asset.ssh_password == "EXISTING"


def test_password_update_replaces_when_provided(monkeypatch):
    asset = SimpleNamespace(ssh_password="OLD")
    monkeypatch.setattr(assets_api, "encrypt_secret", lambda text: f"enc:{text}")

    apply_password_update(asset, "NEW")

    assert asset.ssh_password == "enc:NEW"


def test_password_update_can_clear_explicitly(monkeypatch):
    asset = SimpleNamespace(ssh_password="OLD")
    monkeypatch.setattr(assets_api, "encrypt_secret", lambda text: f"enc:{text}")

    apply_password_update(asset, "", allow_clear=True)

    assert asset.ssh_password == ""


def test_update_password_resolution_prefers_typed_then_stored():
    assert resolve_update_password("typed", "stored") == "typed"
    assert resolve_update_password("", "stored") == "stored"
    assert resolve_update_password("   ", "stored") == "stored"
    assert resolve_update_password("", "") == ""


# ---------------------------- 删除清理分组成员 ----------------------------


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_host(db, instance_id: str, group_id: int | None = 1) -> Asset:
    asset = Asset(instance_id=instance_id, name="web", ip="10.0.0.1")
    db.add(asset)
    db.flush()
    if group_id is not None:
        db.add(ResourceGroupMember(
            group_id=group_id,
            resource_type=RESOURCE_TYPE_HOST,
            resource_id=instance_id,
        ))
    db.commit()
    return asset


def test_delete_removes_group_membership():
    db = _make_db()
    asset = _seed_host(db, "ins-del-1")
    assert db.query(ResourceGroupMember).filter_by(resource_id="ins-del-1").count() == 1

    delete_asset_record(db, asset)
    db.commit()

    assert db.query(Asset).filter_by(instance_id="ins-del-1").count() == 0
    assert db.query(ResourceGroupMember).filter_by(resource_id="ins-del-1").count() == 0


def test_delete_without_group_membership_is_safe():
    db = _make_db()
    asset = _seed_host(db, "ins-del-2", group_id=None)

    delete_asset_record(db, asset)
    db.commit()

    assert db.query(Asset).filter_by(instance_id="ins-del-2").count() == 0


# ---------------------------- 列表接口鉴权 ----------------------------


def test_list_and_summary_require_authentication():
    for fn in (get_assets, get_summary):
        params = inspect.signature(fn).parameters
        assert "current_user" in params, f"{fn.__name__} 必须要求登录"
