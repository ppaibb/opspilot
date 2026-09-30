"""资源分组 API 逻辑测试(SQLite 内存库,直接调用路由函数)。

不经过 FastAPI 依赖注入,直接给路由函数传 db/payload,验证 CRUD、归组、删组置空、
按分组统计的核心逻辑。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
import app.db.models  # noqa: F401  注册全部模型
from app.models.asset import Asset
from app.api import groups as g


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _mk_asset(db, instance_id, ip, type_="healthy", cpu=4, mem=8, disk=100, group_id=None):
    a = Asset(
        instance_id=instance_id, name=instance_id, ip=ip, type=type_,
        cpu_cores=cpu, memory_gb=mem, system_disk_gb=disk, group_id=group_id,
    )
    db.add(a)
    db.commit()
    return a


def test_group_crud_and_unique(db):
    created = g.create_group(g.GroupCreate(name="电商业务", color="#ff0000"), db=db, current_user=None, _=None)
    assert created["id"] > 0
    assert created["name"] == "电商业务"

    # 重名冲突
    with pytest.raises(Exception):
        g.create_group(g.GroupCreate(name="电商业务"), db=db, current_user=None, _=None)

    # 更新
    updated = g.update_group(created["id"], g.GroupUpdate(description="核心"), db=db, current_user=None, _=None)
    assert updated["description"] == "核心"

    listed = g.list_groups(db=db)
    assert len(listed) == 1
    assert listed[0]["asset_count"] == 0


def test_assign_and_delete_frees_assets(db):
    grp = g.create_group(g.GroupCreate(name="测试组"), db=db, current_user=None, _=None)
    _mk_asset(db, "i-1", "10.0.0.1")
    _mk_asset(db, "i-2", "10.0.0.2")

    res = g.assign_assets(g.AssignRequest(asset_ids=["i-1", "i-2"], group_id=grp["id"]), db=db, current_user=None, _=None)
    assert res["updated"] == 2

    listed = g.list_groups(db=db)
    assert listed[0]["asset_count"] == 2

    # 删组应把资源 group_id 置空,不删资源
    delres = g.delete_group(grp["id"], db=db, current_user=None, _=None)
    assert delres["freed_assets"] == 2
    assert db.query(Asset).count() == 2
    assert db.query(Asset).filter(Asset.group_id.isnot(None)).count() == 0


def test_group_stats_cost_index(db):
    grp = g.create_group(g.GroupCreate(name="A组"), db=db, current_user=None, _=None)
    # cost_index = cpu + mem/2 + disk/100 = 4 + 4 + 1 = 9
    _mk_asset(db, "i-1", "10.0.0.1", type_="healthy", cpu=4, mem=8, disk=100, group_id=grp["id"])
    _mk_asset(db, "i-2", "10.0.0.2", type_="critical", cpu=2, mem=4, disk=200, group_id=grp["id"])  # 2+2+2=6
    _mk_asset(db, "i-3", "10.0.0.3", type_="healthy", cpu=8, mem=16, disk=50)  # 未分组 8+8+0.5=16.5

    stats = g.group_stats(db=db)["groups"]
    by_name = {row["name"]: row for row in stats}
    assert by_name["A组"]["host_count"] == 2
    assert by_name["A组"]["healthy"] == 1
    assert by_name["A组"]["critical"] == 1
    assert by_name["A组"]["cost_index"] == 15.0
    assert by_name["未分组"]["host_count"] == 1
    assert by_name["未分组"]["cost_index"] == 16.5


def test_assign_vm_generic_path(db):
    """方案 B 通用路径:非主机类型(vm)只写成员表,按类型隔离统计。"""
    grp = g.create_group(g.GroupCreate(name="虚机组"), db=db, current_user=None, _=None)
    # 给虚机归组(走成员表,业务表无需 group_id 列)
    res = g.assign_assets(
        g.AssignRequest(asset_ids=["vm-1", "vm-2", "vm-3"], group_id=grp["id"], resource_type="vm"),
        db=db, current_user=None, _=None,
    )
    assert res["updated"] == 3
    assert res["resource_type"] == "vm"

    # list_groups 的 counts 按类型细分
    listed = g.list_groups(db=db)
    grp_row = next(r for r in listed if r["id"] == grp["id"])
    assert grp_row["counts"].get("vm") == 3
    assert grp_row["member_count"] == 3
    assert grp_row["asset_count"] == 0  # 主机数为 0,vm 不计入兼容字段

    # 按 vm 统计
    vm_stats = g.group_stats(resource_type="vm", db=db)["groups"]
    by_name = {row["name"]: row for row in vm_stats}
    assert by_name["虚机组"]["host_count"] == 3

    # 移出分组
    g.assign_assets(
        g.AssignRequest(asset_ids=["vm-1"], group_id=None, resource_type="vm"),
        db=db, current_user=None, _=None,
    )
    listed2 = g.list_groups(db=db)
    grp_row2 = next(r for r in listed2 if r["id"] == grp["id"])
    assert grp_row2["counts"].get("vm") == 2


def test_invalid_resource_type_rejected(db):
    with pytest.raises(Exception):
        g.assign_assets(
            g.AssignRequest(asset_ids=["x-1"], group_id=None, resource_type="bogus"),
            db=db, current_user=None, _=None,
        )
