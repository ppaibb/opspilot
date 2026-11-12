"""证书部署异步化 + 部署历史。

覆盖：POST /{id}/deploy 校验通过后立即返回 deploy_id 并建 running 记录、
后台 run_deploy_job 逐台/逐目标执行并写历史终态（host 全成功 / 部分失败、
k8s 成功与失败、cloud 只记录不调接口）、GET 列表与详情（倒序、404）。
SSH / k8s 全部 mock，不触真实网络；日志写盘隔离到 tmp_path。
"""
import io

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import certificates
from app.api.certificates import (
    CertificateDeployRequest,
    deploy_certificate,
    get_certificate_deployment,
    list_certificate_deployments,
    run_deploy_job,
    serialize_deployment,
)
from app.core.crypto import encrypt_secret
from app.models.asset import Asset
from app.models.certificate import CertificateRecord
from app.models.certificate_deployment import CertificateDeployment
from app.models.credential import Credential
from app.models.user import User


class _FakeBackgroundTasks:
    """只记录 add_task 调用，不执行。"""

    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class _FakeChannel:
    def recv_exit_status(self):
        return _FakeSSHClient.post_script_exit_status


class _FakeStdout(io.BytesIO):
    def __init__(self, content=b""):
        super().__init__(content)
        self.channel = _FakeChannel()


class _FakeSftpFile(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSftp:
    def __init__(self, store):
        self.store = store

    def file(self, path, mode):
        self.store[path] = _FakeSftpFile()
        return self.store[path]

    def close(self):
        pass


class _FakeSSHClient:
    """可配置成 connect 失败的假 SSH 客户端。"""

    fail_connect = False
    post_script_exit_status = 0
    last_instance = None

    def __init__(self):
        self.store = {}
        type(self).last_instance = self

    def connect(self, *args, **kwargs):
        if type(self).fail_connect:
            raise RuntimeError("connection refused")

    def open_sftp(self):
        return _FakeSftp(self.store)

    def exec_command(self, *args, **kwargs):
        return None, _FakeStdout(b"ok"), _FakeStdout(b"")

    def close(self):
        pass


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (CertificateRecord, CertificateDeployment, Credential, Asset, User):
        model.__table__.create(engine)
    # run_deploy_job 用模块级 SessionLocal()；指向内存库
    monkeypatch.setattr(certificates, "SessionLocal", lambda: Session(engine))
    # append_cert_log / cert_storage_paths 写盘；隔离到 tmp_path
    monkeypatch.setattr(certificates, "CERT_STORAGE_DIR", tmp_path / "certs")
    session = Session(engine)
    user = User(username="op", name="操作员", role="user", hashed_password="<redacted>")
    session.add(user)
    session.add(Credential(
        cred_id="CRED-ALI", name="阿里云-闫东", type="AccessKey",
        status="active", content=encrypt_secret('{"access_key":"ak","secret_key":"sk"}'),
    ))
    cert_dir = tmp_path / "certfiles"
    cert_dir.mkdir()
    cert_path = cert_dir / "fullchain.pem"
    key_path = cert_dir / "privkey.pem"
    cert_path.write_text("CERT-CONTENT", encoding="utf-8")
    key_path.write_text("KEY-CONTENT", encoding="utf-8")
    session.add(CertificateRecord(
        cert_id="CERT-1", name="测试证书", primary_domain="example.com",
        status="issued", cert_path=str(cert_path), key_path=str(key_path),
        fullchain_path=str(cert_path),
    ))
    session.add(Asset(instance_id="HOST-1", name="主机一", ip="10.0.0.1", ssh_port=22, ssh_user="root"))
    session.add(Asset(instance_id="HOST-2", name="主机二", ip="10.0.0.2", ssh_port=22, ssh_user="root"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _user(db):
    return db.query(User).filter(User.username == "op").first()


def _deploy_record(db, deploy_id):
    return db.query(CertificateDeployment).filter(CertificateDeployment.deploy_id == deploy_id).first()


def _submit_host(db, monkeypatch, asset_ids=("HOST-1", "HOST-2")):
    """走真实 endpoint 提交一次 host 部署，返回 (deploy_id, 后台任务记录)。"""
    tasks = _FakeBackgroundTasks()
    payload = CertificateDeployRequest(
        target_type="host", asset_ids=list(asset_ids),
        cert_path="/etc/nginx/ssl/cert.pem", key_path="/etc/nginx/ssl/privkey.pem",
        reload_cmd="systemctl reload nginx",
    )
    result = deploy_certificate(
        "CERT-1", payload, background_tasks=tasks, db=db, current_user=_user(db), _=None
    )
    return result, tasks


def _install_fake_ssh(monkeypatch, fail_connect=False):
    import paramiko
    from app.api import assets as assets_api
    from app.services import ssh_client as ssh_client_mod

    _FakeSSHClient.fail_connect = fail_connect
    monkeypatch.setattr(paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setattr(ssh_client_mod, "pin_and_connect", lambda client, *a, **k: None)
    monkeypatch.setattr(assets_api, "decode_asset_password", lambda asset: "pw")
    monkeypatch.setattr(assets_api, "ensure_asset_credential_access", lambda *a, **k: None)
    monkeypatch.setattr(assets_api, "get_credential_content", lambda *a, **k: "")


def _log_text(db):
    from app.api.certificates import read_cert_logs

    return "\n".join(read_cert_logs("CERT-1"))


def test_deploy_endpoint_returns_immediately_with_running_record(db):
    result, tasks = _submit_host(db, None)
    assert result["success"] is True
    assert result["deploy_id"]
    assert "后台执行中" in result["message"]
    assert len(tasks.tasks) == 1
    func, args, _kwargs = tasks.tasks[0]
    assert func is run_deploy_job
    assert args[0] == "CERT-1"
    assert args[1] == result["deploy_id"]
    record = _deploy_record(db, result["deploy_id"])
    assert record is not None
    assert record.status == "running"
    assert record.target_type == "host"
    assert record.targets == "HOST-1, HOST-2"
    assert record.triggered_by == "op"
    assert record.started_at
    assert record.finished_at == ""


def test_deploy_endpoint_keeps_validations(db):
    user = _user(db)
    tasks = _FakeBackgroundTasks()
    # 未选主机
    with pytest.raises(HTTPException) as exc:
        deploy_certificate("CERT-1", CertificateDeployRequest(target_type="host", asset_ids=[]),
                           background_tasks=tasks, db=db, current_user=user, _=None)
    assert exc.value.status_code == 400
    # 超过 20 台
    with pytest.raises(HTTPException) as exc:
        deploy_certificate("CERT-1", CertificateDeployRequest(target_type="host", asset_ids=[f"H{i}" for i in range(21)]),
                           background_tasks=tasks, db=db, current_user=user, _=None)
    assert exc.value.status_code == 400
    # 相对路径
    with pytest.raises(HTTPException) as exc:
        deploy_certificate("CERT-1", CertificateDeployRequest(target_type="host", asset_ids=["HOST-1"], cert_path="rel/cert.pem"),
                           background_tasks=tasks, db=db, current_user=user, _=None)
    assert exc.value.status_code == 400
    # 证书不存在
    with pytest.raises(HTTPException) as exc:
        deploy_certificate("CERT-NOPE", CertificateDeployRequest(target_type="host", asset_ids=["HOST-1"]),
                           background_tasks=tasks, db=db, current_user=user, _=None)
    assert exc.value.status_code == 404
    assert tasks.tasks == []
    assert db.query(CertificateDeployment).count() == 0


def test_run_deploy_job_host_all_success(db, monkeypatch):
    _install_fake_ssh(monkeypatch)
    result, tasks = _submit_host(db, monkeypatch)
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    record = _deploy_record(db, result["deploy_id"])
    assert record.status == "success"
    assert record.finished_at
    data = serialize_deployment(record)
    assert data["success_count"] == 2
    assert data["total_count"] == 2
    assert {item["asset_id"] for item in data["results"]} == {"HOST-1", "HOST-2"}
    assert all(item["success"] for item in data["results"])
    # 假 SSH 确实收到了证书与私钥
    assert _FakeSSHClient.last_instance.store["/etc/nginx/ssl/cert.pem"].getvalue() == "CERT-CONTENT"
    assert _FakeSSHClient.last_instance.store["/etc/nginx/ssl/privkey.pem"].getvalue() == "KEY-CONTENT"
    # 日志：开始、每台一行、结束汇总
    log = _log_text(db)
    assert "开始执行证书部署 [host]" in log
    assert "部署主机 HOST-1：成功" in log
    assert "部署主机 HOST-2：成功" in log
    assert "证书部署结束：目标 2 个，成功 2 个" in log


def test_run_deploy_job_host_partial_failure(db, monkeypatch):
    _install_fake_ssh(monkeypatch, fail_connect=True)
    # HOST-9 不存在 + HOST-1 SSH 连不上 → 全部失败；再补一台成功的场景用下例
    result, tasks = _submit_host(db, monkeypatch, asset_ids=("HOST-1", "HOST-9"))
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    record = _deploy_record(db, result["deploy_id"])
    assert record.status == "failed"
    data = serialize_deployment(record)
    assert data["success_count"] == 0
    by_id = {item.get("asset_id"): item for item in data["results"]}
    assert by_id["HOST-1"]["success"] is False
    assert "connection refused" in by_id["HOST-1"]["message"]
    assert by_id["HOST-9"]["success"] is False
    assert by_id["HOST-9"]["message"] == "主机未找到"
    log = _log_text(db)
    assert "部署主机 HOST-1：失败" in log
    assert "部署主机 HOST-9：失败" in log


def test_run_deploy_job_host_partial_one_ok_one_missing(db, monkeypatch):
    _install_fake_ssh(monkeypatch)
    result, tasks = _submit_host(db, monkeypatch, asset_ids=("HOST-1", "HOST-9"))
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    record = _deploy_record(db, result["deploy_id"])
    assert record.status == "partial"
    data = serialize_deployment(record)
    assert data["success_count"] == 1
    assert data["total_count"] == 2


def test_run_deploy_job_host_post_script_failure_is_failure(db, monkeypatch):
    _install_fake_ssh(monkeypatch)
    _FakeSSHClient.post_script_exit_status = 1
    try:
        result, tasks = _submit_host(db, monkeypatch, asset_ids=("HOST-1",))
        func, args, _kwargs = tasks.tasks[0]
        func(*args)
        record = _deploy_record(db, result["deploy_id"])
        # 后置脚本（如 reload nginx）非零退出：证书未生效，该台记失败、历史记 failed
        assert record.status == "failed"
        data = serialize_deployment(record)
        assert data["success_count"] == 0
        assert data["results"][0]["success"] is False
        assert "后置脚本执行失败(Code 1)" in data["results"][0]["message"]
        log = _log_text(db)
        assert "部署主机 HOST-1：失败" in log
    finally:
        _FakeSSHClient.post_script_exit_status = 0


def test_run_deploy_job_k8s_success_and_failure(db, monkeypatch):
    # 成功
    monkeypatch.setattr(
        certificates, "_deploy_cert_to_k8s",
        lambda row, cluster_id, namespace, secret_name: {
            "success": True, "message": "ok",
            "results": [{"target": f"{cluster_id}/{namespace}/{secret_name}", "success": True, "message": "TLS Secret 已创建"}],
        },
    )
    tasks = _FakeBackgroundTasks()
    payload = CertificateDeployRequest(target_type="k8s", cluster_id="CL-1", namespace="prod", secret_name="tls-x")
    result = deploy_certificate("CERT-1", payload, background_tasks=tasks, db=db, current_user=_user(db), _=None)
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    record = _deploy_record(db, result["deploy_id"])
    assert record.status == "success"
    assert record.targets == "k8s:CL-1/prod/tls-x"
    assert serialize_deployment(record)["results"][0]["target"] == "CL-1/prod/tls-x"

    # 失败：k8s helper 抛 HTTPException
    def _boom(row, cluster_id, namespace, secret_name):
        raise HTTPException(status_code=500, detail="K8s TLS Secret 推送失败: boom")

    monkeypatch.setattr(certificates, "_deploy_cert_to_k8s", _boom)
    tasks = _FakeBackgroundTasks()
    result = deploy_certificate("CERT-1", payload, background_tasks=tasks, db=db, current_user=_user(db), _=None)
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    record = _deploy_record(db, result["deploy_id"])
    assert record.status == "failed"
    assert "boom" in serialize_deployment(record)["results"][0]["message"]


def test_run_deploy_job_cloud_records_without_calling_provider(db):
    tasks = _FakeBackgroundTasks()
    payload = CertificateDeployRequest(target_type="cloud", cloud_provider="aliyun", cloud_account_id="CRED-ALI")
    result = deploy_certificate("CERT-1", payload, background_tasks=tasks, db=db, current_user=_user(db), _=None)
    assert result["deploy_id"]
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    record = _deploy_record(db, result["deploy_id"])
    # 云路只记录目标、未调用云厂商接口：历史必须记 recorded（未推送），不是 success
    assert record.status == "recorded"
    assert record.targets == "ALIYUN CDN/ALB (阿里云-闫东)"
    data = serialize_deployment(record)
    # 如实记录：没调云厂商接口，不许写"推送成功"；逐目标结果也不许算成功
    assert "未调用云厂商接口" in data["results"][0]["message"]
    assert "未推送" in data["results"][0]["message"]
    assert "推送成功" not in data["results"][0]["message"]
    assert data["results"][0]["success"] is False
    assert data["results"][0]["recorded"] is True
    assert data["success_count"] == 0
    assert data["total_count"] == 1
    # deploy_targets 更新行为不变：云路仍写证书行
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == "CERT-1").first()
    assert row.deploy_targets == "ALIYUN CDN/ALB (阿里云-闫东)"


def test_deployments_list_and_detail(db, monkeypatch):
    _install_fake_ssh(monkeypatch)
    result1, tasks1 = _submit_host(db, monkeypatch, asset_ids=("HOST-1",))
    func, args, _kwargs = tasks1.tasks[0]
    func(*args)
    result2, _tasks2 = _submit_host(db, monkeypatch, asset_ids=("HOST-2",))
    rows = list_certificate_deployments("CERT-1", db=db)
    assert [item["deploy_id"] for item in rows] == [result2["deploy_id"], result1["deploy_id"]]
    assert rows[0]["status"] == "running"  # 第二条还没跑后台任务
    assert rows[1]["status"] == "success"
    detail = get_certificate_deployment("CERT-1", result1["deploy_id"], db=db)
    assert detail["deploy_id"] == result1["deploy_id"]
    assert detail["results"][0]["asset_id"] == "HOST-1"
    with pytest.raises(HTTPException) as exc:
        list_certificate_deployments("CERT-NOPE", db=db)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        get_certificate_deployment("CERT-1", "no-such-deploy", db=db)
    assert exc.value.status_code == 404


def test_serialize_deployment_tolerates_bad_json(db):
    record = CertificateDeployment(
        deploy_id="D-BAD", cert_id="CERT-1", target_type="host", targets="x",
        status="running", results_json="{not-json", triggered_by="op", started_at="2026-11-09 00:00:00",
    )
    data = serialize_deployment(record)
    assert data["results"] == []
    assert data["success_count"] == 0
    assert data["total_count"] == 0
