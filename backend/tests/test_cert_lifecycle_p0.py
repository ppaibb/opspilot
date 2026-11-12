"""证书全链路 P0 修复（2026-11-10）回归测试。

P0-1 续期成功后自动重部署：部署写档案、续期成功按档案重推（host 成功 /
云路 recorded 诚实状态 / 无档案静默跳过）、重部署失败进通知。
P0-2 到期提醒默认规则 + 全体管理员站内信兜底。
P0-3 已吊销证书不可续签：手动 / 批量 / 自动循环 / run_issue_job 纵深防御，
续签失败不清空吊销原因。

SSH / ACME 全部 mock，不触真实网络；日志与证书文件写盘隔离到 tmp_path。
"""
import io
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services.alert_engine.cert_expiry_alert as cea
from app.api import certificates
from app.api.certificates import (
    CertificateBatchRenewRequest,
    CertificateDeployRequest,
    batch_renew_certificates,
    deploy_certificate,
    renew_certificate,
    run_certificates_auto_renew_cycle,
    run_deploy_job,
    run_issue_job,
    trigger_auto_redeploy,
)
from app.api.monitoring.constants import CERT_EXPIRY_RULE_ID, DEFAULT_ALERT_RULES
from app.api.monitoring.services.rules import _normalize_monitoring_rules, read_monitoring_rules
from app.core.crypto import encrypt_secret
from app.models.asset import Asset
from app.models.audit import AuditLog
from app.models.certificate import CertificateRecord
from app.models.certificate_deployment import CertificateDeployment
from app.models.credential import Credential
from app.models.notification import UserNotification
from app.models.system_config import SystemConfig
from app.models.user import User


REVOKED_REASON = "已吊销证书不可续签，如需重新签发请先删除该记录"


# ---------------------------------------------------------------------------
# 假 SSH（与 test_certificate_deploy_history.py 同模式）
# ---------------------------------------------------------------------------
class _FakeChannel:
    def recv_exit_status(self):
        return 0


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
    last_instance = None

    def __init__(self):
        self.store = {}
        type(self).last_instance = self

    def connect(self, *args, **kwargs):
        pass

    def open_sftp(self):
        return _FakeSftp(self.store)

    def exec_command(self, *args, **kwargs):
        return None, _FakeStdout(b"ok"), _FakeStdout(b"")

    def close(self):
        pass


class _FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class _SyncThread:
    """threading.Thread 的同步版：start() 即原地执行目标函数。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


# ---------------------------------------------------------------------------
# 假 ACME：自签证书 + 假 ClientV2/ClientNetwork，让 run_issue_job 走完成功分支
# ---------------------------------------------------------------------------
def _make_self_signed_pem(domain: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=89))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


class _FakeOrder:
    def __init__(self, authzr):
        self.authorizations = [authzr]


class _FakeClientV2:
    def __init__(self, directory, net, fullchain_pem: str):
        self._fullchain_pem = fullchain_pem

    def new_account(self, registration):
        return SimpleNamespace(uri="https://example.invalid/acct/1")

    def new_order(self, csr_pem):
        from acme import challenges

        dns01 = challenges.DNS01(token=os.urandom(16))
        authzr = SimpleNamespace(
            body=SimpleNamespace(
                identifier=SimpleNamespace(value="example.com"),
                challenges=[SimpleNamespace(chall=dns01)],
            )
        )
        return _FakeOrder(authzr)

    def answer_challenge(self, chall, response):
        return None

    def poll_and_finalize(self, order, deadline):
        return SimpleNamespace(fullchain_pem=self._fullchain_pem)


class _FakeNetResponse:
    def json(self):
        return {
            "newNonce": "https://example.invalid/acme/new-nonce",
            "newAccount": "https://example.invalid/acme/new-acct",
            "newOrder": "https://example.invalid/acme/new-order",
            "revokeCert": "https://example.invalid/acme/revoke-cert",
            "keyChange": "https://example.invalid/acme/key-change",
        }


class _FakeNet:
    def __init__(self, *args, **kwargs):
        self.session = SimpleNamespace(close=lambda: None)

    def get(self, url):
        return _FakeNetResponse()


def _install_fake_acme(monkeypatch, fullchain_pem: str):
    monkeypatch.setattr(
        certificates, "acme_client",
        SimpleNamespace(
            ClientNetwork=_FakeNet,
            ClientV2=lambda directory, net: _FakeClientV2(directory, net, fullchain_pem),
        ),
    )
    monkeypatch.setattr(certificates, "fetch_dns_zones", lambda *a, **k: ["example.com"])
    monkeypatch.setattr(certificates, "create_dns_txt_record", lambda *a, **k: ("rec-1", "example.com"))
    monkeypatch.setattr(certificates, "delete_dns_txt_record", lambda *a, **k: None)
    monkeypatch.setattr(certificates, "wait_dns_propagation", lambda: None)


def _install_fake_ssh(monkeypatch):
    import paramiko

    from app.api import assets as assets_api
    from app.services import ssh_client as ssh_client_mod

    monkeypatch.setattr(paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setattr(ssh_client_mod, "pin_and_connect", lambda client, *a, **k: None)
    monkeypatch.setattr(assets_api, "decode_asset_password", lambda asset: "pw")
    monkeypatch.setattr(assets_api, "ensure_asset_credential_access", lambda *a, **k: None)
    monkeypatch.setattr(assets_api, "get_credential_content", lambda *a, **k: "")


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (
        CertificateRecord, CertificateDeployment, Credential, Asset,
        User, UserNotification, SystemConfig, AuditLog,
    ):
        model.__table__.create(engine)
    certificates.ISSUING_CERT_IDS.clear()
    monkeypatch.setattr(certificates, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(cea, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(certificates, "CERT_STORAGE_DIR", tmp_path / "certs")
    monkeypatch.setattr(certificates, "ACME_ACCOUNT_DIR", tmp_path / "acme-accounts")
    session = Session(engine)
    admin = User(username="admin", name="管理员", role="admin", hashed_password="<redacted>")
    session.add(admin)
    session.add(User(username="op", name="操作员", role="user", hashed_password="<redacted>"))
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
        provider="aliyun", account_id="CRED-ALI", account_name="阿里云-闫东",
        status="issued", auto_renew="yes", renew_before_days=30,
        not_after=(datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
        cert_path=str(cert_path), key_path=str(key_path), fullchain_path=str(cert_path),
    ))
    session.add(Asset(instance_id="HOST-1", name="主机一", ip="10.0.0.1", ssh_port=22, ssh_user="root"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _admin(db):
    return db.query(User).filter(User.username == "admin").first()


def _cert(db, cert_id="CERT-1"):
    return db.query(CertificateRecord).filter(CertificateRecord.cert_id == cert_id).first()


def _deployments(db, cert_id="CERT-1"):
    return (
        db.query(CertificateDeployment)
        .filter(CertificateDeployment.cert_id == cert_id)
        .order_by(CertificateDeployment.id)
        .all()
    )


def _notifications(db, user_id=None, title=None):
    query = db.query(UserNotification)
    if user_id is not None:
        query = query.filter(UserNotification.user_id == user_id)
    if title is not None:
        query = query.filter(UserNotification.title == title)
    return query.all()


def _submit_host_deploy(db, monkeypatch, asset_ids=("HOST-1",)):
    tasks = _FakeBackgroundTasks()
    payload = CertificateDeployRequest(
        target_type="host", asset_ids=list(asset_ids),
        cert_path="/etc/nginx/ssl/cert.pem", key_path="/etc/nginx/ssl/privkey.pem",
        reload_cmd="systemctl reload nginx",
    )
    result = deploy_certificate(
        "CERT-1", payload, background_tasks=tasks, db=db, current_user=_admin(db), _=None
    )
    assert result["success"] is True
    return result, tasks


# ---------------------------------------------------------------------------
# P0-1：部署目标档案 + 续期成功自动重部署
# ---------------------------------------------------------------------------
def test_deploy_writes_profile_and_renew_redeploys_host(db, tmp_path, monkeypatch):
    _install_fake_ssh(monkeypatch)
    result, tasks = _submit_host_deploy(db, monkeypatch)
    # 档案随派发写下来（host 路此前从不写 deploy_targets，现在摘要也写了）
    row = _cert(db)
    profile = json.loads(row.deploy_profile_json)
    assert profile["target_type"] == "host"
    assert profile["asset_ids"] == ["HOST-1"]
    assert profile["cert_dest_path"] == "/etc/nginx/ssl/cert.pem"
    assert profile["key_dest_path"] == "/etc/nginx/ssl/privkey.pem"
    assert profile["post_script"] == "systemctl reload nginx"
    assert row.deploy_targets == "HOST-1"
    # 先跑完这次手动部署，历史记 success
    func, args, _kwargs = tasks.tasks[0]
    func(*args)
    assert _deployments(db)[0].status == "success"

    # 续期成功（ACME 全 mock）：成功分支应按档案自动触发一次重部署
    _install_fake_acme(monkeypatch, _make_self_signed_pem("example.com"))
    monkeypatch.setattr(certificates.threading, "Thread", _SyncThread)
    run_issue_job("CERT-1", renew=True, reserved=True, user_id=_admin(db).id)

    db.expire_all()
    row = _cert(db)
    assert row.status == "issued"
    assert row.last_error == ""
    records = _deployments(db)
    assert len(records) == 2
    auto = records[1]
    assert auto.triggered_by == "auto-renew"
    assert auto.target_type == "host"
    assert auto.status == "success"
    # 重部署推的是续期后的新证书文件，而不是旧文件内容
    renewed_cert = (tmp_path / "certs" / "CERT-1" / "fullchain.pem").read_text(encoding="utf-8")
    assert _FakeSSHClient.last_instance.store["/etc/nginx/ssl/cert.pem"].getvalue() == renewed_cert
    assert renewed_cert != "CERT-CONTENT"


def test_trigger_auto_redeploy_cloud_is_recorded_not_success(db):
    row = _cert(db)
    row.deploy_profile_json = json.dumps({
        "target_type": "cloud", "cloud_provider": "aliyun",
        "cloud_account_id": "CRED-ALI", "cloud_service": "cdn",
        "domain_instance": "example.com",
    })
    db.commit()

    result = trigger_auto_redeploy("CERT-1", _admin(db).id, run_async=False)

    assert result is not None and result["target_type"] == "cloud"
    records = _deployments(db)
    assert len(records) == 1
    # 云路只记录、未调用厂商接口：历史必须是 recorded，不能记 success
    assert records[0].status == "recorded"
    assert records[0].triggered_by == "auto-renew"
    data = json.loads(records[0].results_json)
    assert data[0].get("recorded") is True
    assert data[0].get("success") is False
    # recorded 是预期状态，不发失败通知
    assert _notifications(db, title="证书续期后自动重部署失败") == []


def test_trigger_auto_redeploy_without_profile_is_noop(db):
    assert trigger_auto_redeploy("CERT-1", _admin(db).id, run_async=False) is None
    assert _deployments(db) == []
    assert _notifications(db, title="证书续期后自动重部署失败") == []


def test_trigger_auto_redeploy_invalid_profile_notifies(db):
    row = _cert(db)
    row.deploy_profile_json = "{not-json"
    db.commit()

    assert trigger_auto_redeploy("CERT-1", _admin(db).id, run_async=False) is None
    assert _deployments(db) == []
    notes = _notifications(db, title="证书续期后自动重部署失败")
    assert len(notes) == 1
    assert notes[0].user_id == _admin(db).id


def test_auto_redeploy_failure_notifies_admin(db):
    row = _cert(db)
    row.deploy_profile_json = json.dumps({
        "target_type": "host", "asset_ids": ["HOST-9"],
        "cert_dest_path": "/etc/nginx/ssl/cert.pem",
        "key_dest_path": "/etc/nginx/ssl/privkey.pem", "post_script": "",
    })
    db.commit()

    result = trigger_auto_redeploy("CERT-1", None, run_async=False)

    assert result is not None
    assert _deployments(db)[0].status == "failed"
    notes = _notifications(db, title="证书续期后自动重部署失败")
    assert len(notes) == 1
    assert notes[0].user_id == _admin(db).id
    assert "CERT-1" in (notes[0].resource_id or "")


def test_trigger_auto_redeploy_k8s_profile_validates(db):
    row = _cert(db)
    row.deploy_profile_json = json.dumps({
        "target_type": "k8s", "cluster_id": "", "namespace": "default", "secret_name": "",
    })
    db.commit()
    # 缺 cluster_id/secret_name 的 k8s 档案无效：不建部署记录，但要通知
    assert trigger_auto_redeploy("CERT-1", _admin(db).id, run_async=False) is None
    assert _deployments(db) == []
    assert len(_notifications(db, title="证书续期后自动重部署失败")) == 1


# ---------------------------------------------------------------------------
# P0-2：默认到期规则 + 管理员站内信兜底
# ---------------------------------------------------------------------------
def test_default_rules_include_cert_expiry():
    by_id = {str(rule.get("id") or ""): rule for rule in DEFAULT_ALERT_RULES}
    assert CERT_EXPIRY_RULE_ID == "builtin-cert-expiry"
    assert CERT_EXPIRY_RULE_ID in by_id
    rule = by_id[CERT_EXPIRY_RULE_ID]
    assert rule["name"] == "证书即将到期"
    assert rule["enabled"] is True
    # 归一化不丢这条规则（空 expr 会被丢弃，见 _normalize_monitoring_rules）
    normalized = _normalize_monitoring_rules(DEFAULT_ALERT_RULES, set())
    assert CERT_EXPIRY_RULE_ID in {str(item.get("id") or "") for item in normalized}


def test_read_monitoring_rules_backfills_builtin_for_existing_envs(db):
    old_rules = [dict(rule) for rule in DEFAULT_ALERT_RULES if rule["id"] != CERT_EXPIRY_RULE_ID]
    assert len(old_rules) == 5
    db.add(SystemConfig(
        config_key="monitoring_rules",
        config_value=json.dumps({"rules": old_rules}, ensure_ascii=False),
        updated_at="2026-11-10 00:00:00",
    ))
    db.commit()

    rules, _ = read_monitoring_rules(db)

    by_id = {str(item.get("id") or "") for item in rules}
    assert CERT_EXPIRY_RULE_ID in by_id
    assert len(rules) == 6
    # 补回后已持久化：再读一次不再重复追加
    rules2, _ = read_monitoring_rules(db)
    assert len(rules2) == 6


def _firing_cert_event(domain="expiring.example.com", days_from_now=5):
    cert = SimpleNamespace(
        cert_id=f"cert-{domain}",
        name=domain,
        primary_domain=domain,
        status="issued",
        renew_before_days=30,
        not_after=(datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d %H:%M:%S"),
    )

    class _FakeQuery:
        def all(self):
            return [cert]

    class _FakeDB:
        def query(self, _model):
            return _FakeQuery()

    return cea._build_cert_events(_FakeDB())[0]


def test_cert_expiry_fallback_notifies_all_admins_without_channel(db):
    event = _firing_cert_event()
    assert event["status"] == "firing"

    sent = cea._notify_cert_expiry_fallback(
        db, [event], rules=[], alert_settings={"default_channel_ids": []},
    )

    assert sent == 1
    notes = _notifications(db, title="证书即将到期")
    assert len(notes) == 1
    assert notes[0].user_id == _admin(db).id
    assert notes[0].category == "certificate"
    assert notes[0].resource_id == "cert-expiring.example.com"
    # 24 小时内重复扫描不再重复发
    sent_again = cea._notify_cert_expiry_fallback(
        db, [event], rules=[], alert_settings={"default_channel_ids": []},
    )
    assert sent_again == 0
    assert len(_notifications(db, title="证书即将到期")) == 1


def test_cert_expiry_fallback_skipped_when_rule_has_channel(db):
    event = _firing_cert_event()
    rules = [{
        "id": CERT_EXPIRY_RULE_ID, "name": "证书即将到期",
        "channel_ids": ["channel-1"], "im_user_ids": [],
    }]

    sent = cea._notify_cert_expiry_fallback(
        db, [event], rules=rules, alert_settings={"default_channel_ids": []},
    )

    assert sent == 0
    assert _notifications(db, title="证书即将到期") == []


def test_cert_expiry_fallback_skipped_when_global_default_channel(db):
    event = _firing_cert_event()

    sent = cea._notify_cert_expiry_fallback(
        db, [event], rules=[], alert_settings={"default_channel_ids": ["channel-1"]},
    )

    assert sent == 0
    assert _notifications(db, title="证书即将到期") == []


def test_cert_expiry_fallback_ignores_resolved_events(db):
    event = _firing_cert_event(domain="healthy.example.com", days_from_now=200)
    assert event["status"] == "resolved"

    sent = cea._notify_cert_expiry_fallback(
        db, [event], rules=[], alert_settings={"default_channel_ids": []},
    )

    assert sent == 0
    assert _notifications(db) == []


# ---------------------------------------------------------------------------
# P0-3：已吊销证书不可续签
# ---------------------------------------------------------------------------
def _add_revoked_cert(db, cert_id="CERT-REV"):
    db.add(CertificateRecord(
        cert_id=cert_id, name="已吊销证书", primary_domain="revoked.example.com",
        provider="aliyun", account_id="CRED-ALI", account_name="阿里云-闫东",
        status="revoked", auto_renew="no", renew_before_days=30,
        not_after=(datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
        last_error="证书已吊销：私钥疑似泄露",
    ))
    db.commit()


def test_renew_revoked_rejected_409_and_reason_kept(db):
    _add_revoked_cert(db)

    with pytest.raises(HTTPException) as exc:
        renew_certificate(
            "CERT-REV", background_tasks=_FakeBackgroundTasks(),
            db=db, current_user=_admin(db), _=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == REVOKED_REASON
    # 拒绝续签不能动吊销原因
    assert _cert(db, "CERT-REV").last_error == "证书已吊销：私钥疑似泄露"
    assert _cert(db, "CERT-REV").status == "revoked"


def test_batch_renew_revoked_reported_failed_others_still_queued(db):
    _add_revoked_cert(db)
    tasks = _FakeBackgroundTasks()

    result = batch_renew_certificates(
        CertificateBatchRenewRequest(ids=["CERT-REV", "CERT-1"]),
        background_tasks=tasks, db=db, current_user=_admin(db), _=None,
    )

    assert result["success_count"] == 1
    assert {"id": "CERT-REV", "reason": REVOKED_REASON} in result["failed"]
    assert len(tasks.tasks) == 1
    assert _cert(db, "CERT-REV").last_error == "证书已吊销：私钥疑似泄露"


def test_auto_renew_cycle_skips_revoked(db, monkeypatch):
    _add_revoked_cert(db)
    # 吊销动作会关 auto_renew；这里模拟 auto_renew 被手动改回 yes 的脏数据，
    # 循环仍必须跳过已吊销证书。
    revoked = _cert(db, "CERT-REV")
    revoked.auto_renew = "yes"
    db.commit()

    started = []

    class _RecordingThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            started.append(args[0] if args else None)

        def start(self):
            pass

    monkeypatch.setattr(certificates.threading, "Thread", _RecordingThread)
    run_certificates_auto_renew_cycle()

    assert started == ["CERT-1"]
    db.expire_all()
    assert _cert(db, "CERT-REV").status == "revoked"
    assert _cert(db, "CERT-REV").last_error == "证书已吊销：私钥疑似泄露"


def test_run_issue_job_revoked_guard_preserves_reason(db):
    _add_revoked_cert(db)

    run_issue_job("CERT-REV", renew=True, reserved=True, user_id=_admin(db).id)

    db.expire_all()
    row = _cert(db, "CERT-REV")
    assert row.status == "revoked"
    assert row.last_error == "证书已吊销：私钥疑似泄露"
