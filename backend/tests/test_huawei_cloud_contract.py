"""华为云导入行为测试：只模拟云端边界，资产读写使用真实 SQLite。"""

from types import SimpleNamespace
from unittest.mock import Mock
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import assets
from app.core.crypto import encrypt_secret
from app.models.asset import Asset
from app.models.credential import Credential
from app.services import huawei_cloud as cloud
from app.services import huawei_asset_sync as sync


@pytest.mark.parametrize("already_present", [False, True])
def test_identity_migration_handles_new_and_existing_columns(already_present, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/20261105_huawei_asset_identity.py"
    spec = importlib.util.spec_from_file_location("huawei_identity_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        extra = ", cloud_account_id VARCHAR(64), cloud_project_id VARCHAR(64)" if already_present else ""
        connection.execute(text("CREATE TABLE assets (id INTEGER PRIMARY KEY" + extra + ")"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert {"cloud_account_id", "cloud_project_id"} <= {c["name"] for c in inspect(connection).get_columns("assets")}
        migration.downgrade()
        migration.downgrade()
        assert [c["name"] for c in inspect(connection).get_columns("assets")] == ["id"]
    engine.dispose()


def project(name="cn-north-4", project_id="project-1", account_id="account-1"):
    return cloud.HuaweiProject(project_id, name, "cn-north-4", account_id)


def server(instance_id="vm-1", tenant_id="project-1"):
    from huaweicloudsdkecs.v2 import ServerAddress, ServerDetail, ServerFlavor

    return ServerDetail(
        id=instance_id, tenant_id=tenant_id, name="生产服务器", status="ACTIVE",
        flavor=ServerFlavor(vcpus="2", ram="4096", disk="0"),
        metadata={"image_name": "Ubuntu 24.04", "os_type": "Linux"},
        addresses={"vpc": [
            ServerAddress(addr="10.0.0.8", version="4", os_ext_ip_stype="fixed"),
            ServerAddress(addr="203.0.113.8", version="4", os_ext_ip_stype="floating"),
        ]},
    )


def client(monkeypatch, projects=None):
    result = cloud.HuaweiCloud("test-access-key", "test-secret-key")
    monkeypatch.setattr(result, "projects", lambda: projects or [project()])
    monkeypatch.setattr(result, "flexus_servers", lambda region: {})
    return result


def instance(instance_id="vm-1", resource_type="ECS", **overrides):
    result = dict(
        instance_id=instance_id, name="生产服务器", ip="203.0.113.8", private_ip="10.0.0.8",
        provider="huawei", region="cn-north-4", cloud_resource_type=resource_type,
        cloud_project_id="project-1", cloud_account_id="account-1", os="Ubuntu 24.04",
        cpu_cores=2.0, memory_gb=4.0, system_disk_gb=0.0, status="华为云 ECS,ACTIVE",
    )
    return result | overrides


@pytest.fixture
def db():
    # ASGI 同步路由运行在线程池，测试共享同一内存库，避免不同连接产生空库。
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Asset.__table__.create(engine)
    Credential.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_sdk_mapping_uses_mb_and_floating_ip():
    result = cloud.normalize_server(server(), project())
    assert result["cpu_cores"] == 2
    assert result["memory_gb"] == 4
    assert result["ip"] == "203.0.113.8"
    assert result["private_ip"] == "10.0.0.8"
    assert result["system_disk_gb"] == 0  # flavor.disk 不是实际系统盘容量。


def test_sdk_has_effective_timeout_and_tls_verification():
    config = cloud.HuaweiCloud._http_config()
    assert config.timeout == (5, 15)
    assert config.ignore_ssl_verification is False
    assert config.allow_redirects is False


def test_private_only_host_keeps_private_ip():
    host = server()
    host.addresses["vpc"] = host.addresses["vpc"][:1]
    result = cloud.normalize_server(host, project())
    assert result["ip"] == result["private_ip"] == "10.0.0.8"


def test_project_tenant_mismatch_is_rejected():
    with pytest.raises(cloud.HuaweiCloudError, match="项目"):
        cloud.normalize_server(server(tenant_id="other-project"), project())


def test_authorized_projects_include_subprojects_and_skip_disabled(monkeypatch):
    api = Mock()
    api.keystone_list_auth_projects.return_value = SimpleNamespace(projects=[
        SimpleNamespace(id="p1", name="cn-north-4", domain_id="d1", enabled=True),
        SimpleNamespace(id="p2", name="cn-north-4_production", domain_id="d1", enabled=True),
        SimpleNamespace(id="p3", name="cn-north-4_off", domain_id="d1", enabled=False),
        SimpleNamespace(id="p4", name="not-an-endpoint", domain_id="d1", enabled=True),
    ], links=None)
    result = cloud.HuaweiCloud("test-access-key", "test-secret-key")
    monkeypatch.setattr(result, "iam_client", lambda: api)
    assert [p.project_id for p in result.projects()] == ["p1", "p2"]


def test_ecs_scans_all_pages_and_projects(monkeypatch):
    result = client(monkeypatch, [project(), project("cn-north-4_prod", "project-2")])
    monkeypatch.setattr(cloud, "PAGE_SIZE", 1)
    api = Mock()
    api.list_servers_details.side_effect = [
        SimpleNamespace(servers=[server("vm-1")]), SimpleNamespace(servers=[]),
        SimpleNamespace(servers=[server("vm-2", "project-2")]), SimpleNamespace(servers=[]),
    ]
    monkeypatch.setattr(result, "ecs_client", lambda _: api)
    scan = result.scan("cn-north-4")
    assert [row["instance_id"] for row in scan.instances] == ["vm-1", "vm-2"]
    assert [c.args[0].offset for c in api.list_servers_details.call_args_list] == [1, 2, 1, 2]
    assert all(c.args[0].enterprise_project_id == "all_granted_eps" for c in api.list_servers_details.call_args_list)


def test_flexus_rms_marker_pagination_uses_physical_server_id(monkeypatch):
    result = client(monkeypatch)
    api = Mock()
    resource = SimpleNamespace(
        project_id="project-1", region_id="cn-north-4", name="轻量套餐",
        properties={"resources": [
            {"logical_resource_type": "huaweicloudinternal_ecs_instance", "physical_resource_id": "vm-light",
             "resource_attributes": [{"key": "root_volume", "value": '[{"size":40}]'}]},
            {"logical_resource_type": "huaweicloudinternal_cbc_freeresource", "physical_resource_id": "traffic"},
        ]},
    )
    api.list_all_resources.side_effect = [
        SimpleNamespace(resources=[resource], page_info=SimpleNamespace(next_marker="next-page")),
        SimpleNamespace(resources=[], page_info=SimpleNamespace(next_marker=None)),
    ]
    monkeypatch.setattr(result, "rms_client", lambda: api)
    # 覆盖 helper 在 client() 内设置的 stub，执行真实分页函数。
    result.flexus_servers = cloud.HuaweiCloud.flexus_servers.__get__(result)
    found = result.flexus_servers("cn-north-4")
    assert set(found) == {"vm-light"}
    assert found["vm-light"]["system_disk_gb"] == 40
    assert api.list_all_resources.call_args_list[1].args[0].marker == "next-page"
    assert api.list_all_resources.call_args_list[0].args[0].type == "hcss.l-instance"


def test_flexus_is_classified_once_and_missing_detail_is_fetched(monkeypatch):
    result = client(monkeypatch)
    api = Mock()
    api.list_servers_details.return_value = SimpleNamespace(servers=[server("vm-light")])
    api.show_server.return_value = SimpleNamespace(server=server("vm-missing"))
    monkeypatch.setattr(result, "ecs_client", lambda _: api)
    monkeypatch.setattr(result, "flexus_servers", lambda _: {
        "vm-light": {"project_id": "project-1", "name": "轻量一", "system_disk_gb": 40},
        "vm-missing": {"project_id": "project-1", "name": "轻量二", "system_disk_gb": 80},
    })
    scan = result.scan("cn-north-4")
    assert len(scan.instances) == 2
    assert {row["cloud_resource_type"] for row in scan.instances} == {"FlexusL"}
    assert scan.instances[0]["system_disk_gb"] == 40
    assert api.show_server.call_args.args[0].server_id == "vm-missing"


def test_region_input_cannot_be_used_as_endpoint(monkeypatch):
    result = client(monkeypatch)
    with pytest.raises(cloud.HuaweiCloudError, match="区域"):
        result.scan("https://127.0.0.1")


def test_raw_sdk_error_never_reaches_client(monkeypatch):
    result = client(monkeypatch)
    api = Mock()
    api.list_servers_details.side_effect = RuntimeError("secret-key=do-not-leak")
    monkeypatch.setattr(result, "ecs_client", lambda _: api)
    with pytest.raises(cloud.HuaweiCloudError) as caught:
        result.scan("cn-north-4")
    assert "do-not-leak" not in str(caught.value)


def test_partial_project_failure_is_explicit(monkeypatch):
    result = client(monkeypatch, [project(), project("cn-north-4_prod", "project-2")])
    api = Mock()
    api.list_servers_details.side_effect = [SimpleNamespace(servers=[server()]), RuntimeError("private details")]
    monkeypatch.setattr(result, "ecs_client", lambda _: api)
    scan = result.scan("cn-north-4")
    assert len(scan.instances) == 1
    assert scan.warnings and "cn-north-4_prod" in scan.warnings[0]
    assert "private details" not in scan.warnings[0]


def request(ids=None):
    return assets.SyncRequest(provider="huawei", region="cn-north-4", instance_ids=ids, credential_id="cred-1")


def use_scan(monkeypatch, rows=None, warnings=None):
    api = Mock()
    api.scan.return_value = cloud.HuaweiScan(rows or [instance()], warnings or [])
    monkeypatch.setattr(sync, "HuaweiCloud", lambda *args: api)
    return api


def test_preview_does_not_write_assets(db, monkeypatch):
    use_scan(monkeypatch)
    result = sync.sync_huawei_assets(db, request(), "ak", "sk")
    assert result["success"] and result["preview"]
    assert not result["instances"][0]["is_imported"]
    assert db.query(Asset).count() == 0


def test_import_tracks_cloud_credential_usage(db, monkeypatch):
    db.add(Credential(cred_id="cred-1", type="AccessKey", status="active", used_by=0))
    db.commit()
    use_scan(monkeypatch)
    assert sync.sync_huawei_assets(db, request(["vm-1"]), "ak", "sk")["success"]
    assert db.query(Credential).one().used_by == 1


def test_import_is_idempotent_and_preserves_ssh_configuration(db, monkeypatch):
    use_scan(monkeypatch)
    assert sync.sync_huawei_assets(db, request(["vm-1"]), "ak", "sk")["success"]
    host = db.query(Asset).one()
    host.credential_ref = "ssh-credential"
    host.ssh_user = "ubuntu"
    host.remark = "人工备注"
    db.commit()
    use_scan(monkeypatch, [instance(ip="203.0.113.9", memory_gb=8)])
    result = sync.sync_huawei_assets(db, request(["vm-1"]), "ak", "sk")
    assert result["success"] and result["imported_count"] == 0
    assert db.query(Asset).count() == 1
    assert host.ip == "203.0.113.9" and host.memory_gb == 8
    assert (host.credential_ref, host.ssh_user, host.remark) == ("ssh-credential", "ubuntu", "人工备注")


@pytest.mark.parametrize("field,value", [
    ("provider", "aws"), ("cloud_project_id", "other-project"),
    ("cloud_account_id", "other-account"), ("region", "cn-east-3"),
])
def test_identity_conflict_never_overwrites(db, monkeypatch, field, value):
    db.add(Asset(**(instance() | {field: value})))
    db.commit()
    use_scan(monkeypatch)
    result = sync.sync_huawei_assets(db, request(["vm-1"]), "ak", "sk")
    assert not result["success"] and "身份" in result["message"]
    assert getattr(db.query(Asset).one(), field) == value


def test_missing_selected_instance_rolls_back_entire_import(db, monkeypatch):
    use_scan(monkeypatch)
    result = sync.sync_huawei_assets(db, request(["vm-1", "missing"]), "ak", "sk")
    assert not result["success"]
    assert db.query(Asset).count() == 0


def test_rotating_credentials_with_same_account_updates_reference(db, monkeypatch):
    db.add(Asset(**instance(), cloud_credential_ref="old-credential"))
    db.commit()
    use_scan(monkeypatch)
    assert sync.sync_huawei_assets(db, request(["vm-1"]), "ak", "sk")["success"]
    assert db.query(Asset).one().cloud_credential_ref == "cred-1"


def test_route_accepts_huawei_and_resolves_saved_keys(db, monkeypatch):
    db.add(Credential(cred_id="cred-1", type="AccessKey", status="active", content=encrypt_secret(
        '{"access_key":"test-access-key","secret_key":"test-secret-key"}')))
    db.commit()
    api = use_scan(monkeypatch)
    result = assets.sync_assets(request(), db, SimpleNamespace(role="管理员", permission_overrides=""))
    assert result["success"] and result["preview"]
    api.scan.assert_called_once_with("cn-north-4")


def test_explicitly_expired_credential_is_not_used(db, monkeypatch):
    db.add(Credential(cred_id="cred-1", type="AccessKey", status="expired", content="secret"))
    db.commit()
    api = use_scan(monkeypatch)
    result = assets.sync_assets(request(), db, SimpleNamespace(role="管理员", permission_overrides=""))
    assert not result["success"] and "过期" in result["message"]
    api.scan.assert_not_called()


def test_unauthorized_credential_is_not_used(db, monkeypatch):
    db.add(Credential(cred_id="cred-1", type="AccessKey", status="active", allowed_roles='["管理员"]', content="secret"))
    db.commit()
    api = use_scan(monkeypatch)
    result = assets.sync_assets(request(), db, SimpleNamespace(role="只读", permission_overrides=""))
    assert not result["success"] and "无权" in result["message"]
    api.scan.assert_not_called()


def http_client(db, role="管理员"):
    """真实 ASGI 路由与权限依赖，替换的仅是用户身份、测试数据库和云 API。"""
    from fastapi import FastAPI
    from app.core.security import get_current_user
    from app.db.database import get_db
    from tests.asgi_client import ASGITestClient

    app = FastAPI()
    app.include_router(assets.router, prefix="/api/assets")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, username="test", role=role, permission_overrides="",
    )
    return ASGITestClient(app)


def test_http_import_checks_manage_permission_before_calling_cloud(db, monkeypatch):
    api = use_scan(monkeypatch)
    response = http_client(db, "只读").post("/api/assets/sync", json={
        "provider": "huawei", "region": "cn-north-4", "credential_id": "cred-1", "instance_ids": ["vm-1"],
    })
    assert response.status_code == 403
    api.scan.assert_not_called()
    assert db.query(Asset).count() == 0


def test_http_preview_and_import_use_saved_encrypted_credential(db, monkeypatch):
    db.add(Credential(cred_id="cred-1", type="AccessKey", status="active", content=encrypt_secret(
        '{"access_key":"test-access-key","secret_key":"test-secret-key"}')))
    db.commit()
    use_scan(monkeypatch, [instance(), instance("vm-light", "FlexusL")])
    client = http_client(db)
    payload = {"provider": "huawei", "region": "cn-north-4", "credential_id": "cred-1"}
    preview = client.post("/api/assets/sync", json=payload)
    assert preview.status_code == 200 and preview.json()["preview"]
    assert len(preview.json()["instances"]) == 2
    assert "test-secret-key" not in preview.text
    assert db.query(Asset).count() == 0
    imported = client.post("/api/assets/sync", json=payload | {"instance_ids": ["vm-1", "vm-light"]})
    assert imported.status_code == 200 and imported.json()["imported_count"] == 2
    assert {row.cloud_resource_type for row in db.query(Asset)} == {"ECS", "FlexusL"}
