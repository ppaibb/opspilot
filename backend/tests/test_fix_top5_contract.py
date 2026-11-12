"""Top 5 修复契约测试（凭据/域名/证书评审 2026-11-09 §五）。

覆盖：
- P0-6：自动续签轮询按 ACME_PROVIDER_PLUGINS implemented 放行（腾讯云触发、AWS/手动跳过）。
- P0-7：ACME 账户私钥新文件 0600、存量 0644 文件读到即收紧为 0600。
- P1-22/P1-26：云路部署历史记 recorded（未推送）、后置脚本失败记失败（见
  test_certificate_deploy_history.py 的云路/后置脚本用例）。
- P0-1：used_by 聚合统计补财务云账号 / 证书 / 应用配置三类引用；三类引用下删凭证被拒；
  三处写点（财务、证书签发、应用配置）同步 used_by。
- P1-1：解密失败全局兜底（main.py handler 映射、批量导出逐条 failed、运行时调用点转业务错误）。
"""
from __future__ import annotations

import io
import stat
import threading
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import certificates as cert_api
from app.api import credentials as creds_api
from app.api import finance as finance_api
from app.api.application_routes import configs as app_configs
from app.core.config import settings
from app.core.crypto import CredentialDecryptionError, encrypt_secret
from app.models.application import Application, ApplicationConfig, ApplicationConfigRevision
from app.models.asset import Asset, DomainRecord
from app.models.audit import AuditLog
from app.models.certificate import CertificateRecord
from app.models.credential import Credential
from app.models.finance import BillSyncRun, CloudAccount, CloudBill
from app.models.user import User
from app.schemas.application import ApplicationConfigPayload
from app.schemas.finance import CloudAccountCreate
from app.services.credential_usage import reference_counts, sync_credential_usage

ALL_TABLES = (
    Application,
    ApplicationConfig,
    ApplicationConfigRevision,
    Asset,
    AuditLog,
    BillSyncRun,
    CertificateRecord,
    CloudAccount,
    CloudBill,
    Credential,
    DomainRecord,
    User,
)


@pytest.fixture(autouse=True)
def _independent_keys(monkeypatch):
    """每例使用确定且相互独立的 JWT/凭证密钥。"""
    monkeypatch.setattr(settings, "SECRET_KEY", "jwt-key-for-top5-tests")
    monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "credential-key-for-top5-tests")


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in ALL_TABLES:
        model.__table__.create(engine)
    monkeypatch.setattr(cert_api, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(cert_api, "CERT_STORAGE_DIR", tmp_path / "certs")
    monkeypatch.setattr(cert_api, "ACME_ACCOUNT_DIR", tmp_path / "acme-accounts")
    session = Session(engine)
    session.add(User(
        username="admin", name="管理员", role="管理员", hashed_password="x",
        permission_overrides='{"action.assets.credentials.manage": true}',
    ))
    session.add(User(username="op", name="操作员", role="user", hashed_password="x"))
    yield session
    session.close()
    engine.dispose()


def _admin(db):
    return db.query(User).filter(User.username == "admin").first()


def _add_cred(db, cred_id, name="凭证"):
    cred = Credential(
        cred_id=cred_id, name=name, type="AccessKey", status="active",
        content=encrypt_secret('{"access_key": "ak-1234567890", "secret_key": "sk-1234567890"}'),
    )
    db.add(cred)
    db.commit()
    return cred


def _used_by(db, cred_id):
    row = db.query(Credential).filter(Credential.cred_id == cred_id).first()
    return row.used_by if row else None


# ── P0-1：聚合统计 ──────────────────────────────────────────────────────────

def test_reference_counts_covers_all_five_sources_with_dedup(db):
    _add_cred(db, "C1")
    _add_cred(db, "C2")
    _add_cred(db, "C3")
    _add_cred(db, "C4")
    _add_cred(db, "C5")
    # 资产：同一行两列指向同一凭证只算 1 次；两台主机指向同一凭证算 2 次
    db.add(Asset(instance_id="H1", credential_ref="C1", cloud_credential_ref="C1"))
    db.add(Asset(instance_id="H2", credential_ref="C1", cloud_credential_ref=""))
    db.add(Asset(instance_id="H3", credential_ref="", cloud_credential_ref="C2"))
    # 域名：同一行 account_id 与 snapshot 指向同一凭证只算 1 次
    db.add(DomainRecord(domain="a.com", account_id="C2", snapshot_credential_id="C2"))
    db.add(DomainRecord(domain="b.com", account_id="C2", snapshot_credential_id=""))
    # 三类新增引用
    db.add(CloudAccount(name="财务云账号", credential_ref="C3"))
    db.add(CertificateRecord(cert_id="CRT-1", primary_domain="x.com", account_id="C3"))
    db.add(ApplicationConfig(config_id="CFG-1", app_id="APP-1", key="k1", credential_ref="C4"))
    db.add(ApplicationConfig(config_id="CFG-2", app_id="APP-1", key="k2", credential_ref="C4"))
    db.commit()

    counts = reference_counts(db)
    assert counts == {"C1": 2, "C2": 3, "C3": 2, "C4": 2}
    assert "C5" not in counts

    sync_credential_usage(db)
    assert _used_by(db, "C1") == 2
    assert _used_by(db, "C2") == 3
    assert _used_by(db, "C3") == 2
    assert _used_by(db, "C4") == 2
    assert _used_by(db, "C5") == 0


def test_sync_credential_usage_incremental_only_touches_given_ids(db):
    _add_cred(db, "C1")
    _add_cred(db, "C2")
    db.add(CloudAccount(name="财务云账号", credential_ref="C1"))
    db.add(CertificateRecord(cert_id="CRT-1", primary_domain="x.com", account_id="C2"))
    db.commit()
    sync_credential_usage(db, {"C1"})
    assert _used_by(db, "C1") == 1
    assert _used_by(db, "C2") == 0  # 未指定的不碰（仍为初始值）
    sync_credential_usage(db, {"C2"})
    assert _used_by(db, "C2") == 1


# ── P0-1：三类引用下删凭证被拒 ─────────────────────────────────────────────

@pytest.mark.parametrize("setup", ["finance", "certificate", "app_config"])
def test_delete_credential_blocked_by_new_reference_kinds(db, setup):
    cred_id = "CRED-X"
    _add_cred(db, cred_id, name="被引用的凭证")
    if setup == "finance":
        db.add(CloudAccount(name="财务云账号", credential_ref=cred_id))
    elif setup == "certificate":
        db.add(CertificateRecord(cert_id="CRT-1", primary_domain="x.com", account_id=cred_id))
    else:
        db.add(ApplicationConfig(config_id="CFG-1", app_id="APP-1", key="k", credential_ref=cred_id))
    db.commit()

    # 单删：先同步再拦截
    with pytest.raises(HTTPException) as exc:
        creds_api.delete_credential(cred_id, db=db, _=None, _risk=None)
    assert exc.value.status_code == 400
    assert "仍被" in str(exc.value.detail)
    assert _used_by(db, cred_id) == 1

    # 批量删：同一凭证进 failed，不进 success
    result = creds_api.batch_delete_credentials(
        creds_api.CredentialBatchDeleteRequest(ids=[cred_id]), db=db, _=None, _risk=None
    )
    assert result["success_count"] == 0
    assert result["failed"][0]["id"] == cred_id
    assert db.query(Credential).filter(Credential.cred_id == cred_id).first() is not None


def test_delete_credential_allowed_after_reference_removed(db):
    cred_id = "CRED-X"
    _add_cred(db, cred_id)
    account = CloudAccount(name="财务云账号", credential_ref=cred_id)
    db.add(account)
    db.commit()
    db.delete(account)
    db.commit()
    sync_credential_usage(db, {cred_id})
    assert _used_by(db, cred_id) == 0
    result = creds_api.delete_credential(cred_id, db=db, _=None, _risk=None)
    assert result == {"message": "success"}
    assert db.query(Credential).filter(Credential.cred_id == cred_id).first() is None


# ── P0-1：财务写点同步 ─────────────────────────────────────────────────────

def test_finance_create_update_delete_sync_used_by(db, monkeypatch):
    cred_id = "CRED-FIN"
    other_id = "CRED-OTHER"
    _add_cred(db, cred_id)
    _add_cred(db, other_id)
    monkeypatch.setattr(finance_api, "verify_account_credentials", lambda ak, sk: (True, "", "owner-1"))
    monkeypatch.setattr(finance_api, "_verify_and_update", lambda db_, account: None)

    account = finance_api.create_cloud_account(
        CloudAccountCreate(name="云账号", provider="aliyun", credential_ref=cred_id, enabled=True),
        db=db, current_user=_admin(db), _risk=None,
    )
    assert _used_by(db, cred_id) == 1

    finance_api.update_cloud_account(
        account.id,
        CloudAccountCreate(name="云账号", provider="aliyun", credential_ref=other_id, enabled=True),
        db=db, current_user=_admin(db),
    )
    assert _used_by(db, cred_id) == 0
    assert _used_by(db, other_id) == 1

    finance_api.delete_cloud_account(account.id, db=db, current_user=_admin(db), _risk=None)
    assert _used_by(db, other_id) == 0


# ── P0-1：证书签发/删除写点同步 ────────────────────────────────────────────

def test_certificate_issue_and_delete_sync_used_by(db, tmp_path, monkeypatch):
    cred_id = "CRED-CERT"
    _add_cred(db, cred_id, name="阿里云-闫东")

    class _FakeBackgroundTasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, func, *args, **kwargs):
            self.tasks.append((func, args, kwargs))

    payload = cert_api.CertificateIssueRequest(
        primary_domain="example.com", account_id=cred_id, acme_server="letsencrypt",
    )
    row = cert_api.issue_certificate(
        payload, background_tasks=_FakeBackgroundTasks(), db=db, current_user=_admin(db), _=None
    )
    assert row["account_id"] == cred_id
    assert _used_by(db, cred_id) == 1
    cert_api.ISSUING_CERT_IDS.discard(row["cert_id"])

    cert_api.delete_certificate(row["cert_id"], db=db, _=None, _risk=None)
    assert _used_by(db, cred_id) == 0


# ── P0-1：应用配置写点同步 ────────────────────────────────────────────────

def _add_app(db):
    app = Application(app_id="APP-1", name="应用一")
    db.add(app)
    db.commit()
    return app


def _config_payload(key="API_KEY", cred_id=""):
    return ApplicationConfigPayload(
        env="prod", key=key, value_type="credential_ref", value="", credential_ref=cred_id,
    )


def test_application_config_create_update_delete_sync_used_by(db, monkeypatch):
    cred_id = "CRED-APP"
    other_id = "CRED-APP2"
    _add_cred(db, cred_id)
    _add_cred(db, other_id)
    # 管理员绕过应用权限组范围；凭证可见性走管理权限
    monkeypatch.setattr(
        "app.services.credential_access.resolve_effective_permissions",
        lambda user: {"action.assets.credentials.manage": True},
    )
    _add_app(db)
    admin = _admin(db)

    created = app_configs.create_application_config(
        "APP-1", _config_payload(cred_id=cred_id), db=db, current_user=admin
    )
    assert _used_by(db, cred_id) == 1

    app_configs.update_application_config(
        "APP-1", created["config_id"], _config_payload(cred_id=other_id), db=db, current_user=admin
    )
    assert _used_by(db, cred_id) == 0
    assert _used_by(db, other_id) == 1

    app_configs.delete_application_config("APP-1", created["config_id"], db=db, current_user=admin)
    assert _used_by(db, other_id) == 0


# ── P0-6：自动续签按插件 implemented 放行 ──────────────────────────────────

def test_auto_renew_cycle_fires_for_implemented_plugins_only(db, tmp_path, monkeypatch):
    monkeypatch.setattr(cert_api, "CERT_STORAGE_DIR", tmp_path / "certs")
    soon = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    cases = [
        ("CRT-ALI", "aliyun", "CRED-ALI", True),
        ("CRT-TEC", "tencent", "CRED-TEC", True),
        ("CRT-AWS", "aws", "CRED-AWS", False),
        ("CRT-CUSTOM", "custom", "", False),
    ]
    for cert_id, provider, account_id, _ in cases:
        db.add(CertificateRecord(
            cert_id=cert_id, primary_domain=f"{provider}.example.com", provider=provider,
            account_id=account_id, status="issued", auto_renew="yes",
            renew_before_days=30, not_after=soon, last_renew_attempt_at="",
        ))
    db.commit()

    queued: list[str] = []
    monkeypatch.setattr(cert_api, "reserve_issue", lambda cert_id: bool(queued.append(cert_id) or True))

    started: list[tuple] = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kwargs):
            self._target = target
            self._args = args

        def start(self):
            started.append((self._target, self._args))

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    cert_api.run_certificates_auto_renew_cycle()

    assert set(queued) == {"CRT-ALI", "CRT-TEC"}
    assert {args[0] for _, args in started} == {"CRT-ALI", "CRT-TEC"}
    db.expire_all()
    assert db.query(CertificateRecord).filter(CertificateRecord.cert_id == "CRT-ALI").first().status == "renewing"
    assert db.query(CertificateRecord).filter(CertificateRecord.cert_id == "CRT-TEC").first().status == "renewing"
    assert db.query(CertificateRecord).filter(CertificateRecord.cert_id == "CRT-AWS").first().status == "issued"
    assert db.query(CertificateRecord).filter(CertificateRecord.cert_id == "CRT-CUSTOM").first().status == "issued"
    cert_api.ISSUING_CERT_IDS.discard("CRT-ALI")
    cert_api.ISSUING_CERT_IDS.discard("CRT-TEC")


# ── P0-7：ACME 账户私钥 0600 ──────────────────────────────────────────────

def test_account_jwk_new_file_is_0600(db, tmp_path, monkeypatch):
    monkeypatch.setattr(cert_api, "ACME_ACCOUNT_DIR", tmp_path / "acme-accounts")
    _jwk, key = cert_api.load_or_create_account_jwk("CRED-ALI", "letsencrypt")
    key_path = cert_api.resolve_account_key_path("CRED-ALI", "letsencrypt")
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.read_bytes().startswith(b"-----BEGIN RSA PRIVATE KEY-----")


def test_account_jwk_existing_0644_file_is_tightened_to_0600(db, tmp_path, monkeypatch):
    monkeypatch.setattr(cert_api, "ACME_ACCOUNT_DIR", tmp_path / "acme-accounts")
    _jwk, key = cert_api.load_or_create_account_jwk("CRED-ALI", "letsencrypt")
    key_path = cert_api.resolve_account_key_path("CRED-ALI", "letsencrypt")
    key_path.chmod(0o644)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o644
    _jwk2, key2 = cert_api.load_or_create_account_jwk("CRED-ALI", "letsencrypt")
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key2.private_numbers().public_numbers.n == key.private_numbers().public_numbers.n


# ── P1-1：main.py 全局 handler 映射 ────────────────────────────────────────

def test_main_exception_handlers_map_decryption_to_422_and_config_to_503():
    from starlette.requests import Request

    from app.main import app
    from app.services.credential_crypto import (
        CredentialDecryptionError,
        CredentialEncryptionConfigError,
    )

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    decryption_handler = app.exception_handlers[CredentialDecryptionError]
    resp = decryption_handler(request, CredentialDecryptionError("bad"))
    assert resp.status_code == 422
    body = resp.body.decode("utf-8")
    assert "凭证密文已损坏，需重新录入" in body
    assert "enc::" not in body

    config_handler = app.exception_handlers[CredentialEncryptionConfigError]
    resp = config_handler(request, CredentialEncryptionConfigError("no key"))
    assert resp.status_code == 503
    assert "CREDENTIAL_ENCRYPTION_KEY" not in resp.body.decode("utf-8")


# ── P1-1：批量导出逐条 failed、单条导出不 500（走全局 handler） ───────────

def _export_user(db):
    return _admin(db)


def test_batch_export_corrupt_credential_goes_to_failed_and_rest_export(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.credential_access.resolve_effective_permissions",
        lambda user: {"action.assets.credentials.manage": True},
    )
    good = _add_cred(db, "CRED-GOOD", name="好的凭证")
    bad = Credential(
        cred_id="CRED-BAD", name="坏的凭证", type="Token", status="active",
        content="enc::v2::broken::token",
    )
    db.add(bad)
    db.commit()

    resp = creds_api.batch_export_credentials(
        creds_api.CredentialBatchExportRequest(ids=["CRED-GOOD", "CRED-BAD"]),
        db=db, current_user=_export_user(db), _=None, _risk=None,
    )
    assert resp.status_code == 200
    assert resp.headers["X-OpsPilot-Credential-Exported-Count"] == "1"
    assert resp.headers["X-OpsPilot-Credential-Failed-Count"] == "1"
    with zipfile.ZipFile(io.BytesIO(resp.body)) as archive:
        manifest = __import__("json").loads(archive.read("manifest.json"))
        names = archive.namelist()
    assert manifest["exported_count"] == 1
    assert manifest["failed_count"] == 1
    assert manifest["failed"][0]["cred_id"] == "CRED-BAD"
    assert "损坏" in manifest["failed"][0]["reason"]
    assert manifest["exported"][0]["cred_id"] == "CRED-GOOD"
    # 文件名经 _safe_export_filename 清洗（中文名会被清掉，回落到 cred_id 再拼一次）
    assert "CRED-GOOD-CRED-GOOD.json" in names


def test_single_export_corrupt_credential_raises_decryption_error(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.credential_access.resolve_effective_permissions",
        lambda user: {"action.assets.credentials.manage": True},
    )
    db.add(Credential(
        cred_id="CRED-BAD", name="坏的凭证", type="Token", status="active",
        content="enc::v2::broken::token",
    ))
    db.commit()
    with pytest.raises(CredentialDecryptionError):
        creds_api.export_credential("CRED-BAD", db=db, current_user=_export_user(db), _=None, _risk=None)


# ── P1-1：运行时调用点转业务错误 ──────────────────────────────────────────

def test_asset_auth_corrupt_credential_returns_business_notice_not_500(db):
    from app.services.asset_auth import resolve_credential_ssh_auth

    cred = Credential(
        cred_id="CRED-BAD", name="坏的凭证", type="SSH Key", status="active",
        content="enc::v2::broken::token",
    )
    auth = resolve_credential_ssh_auth(cred, fallback_password="fallback-pw")
    assert auth.password == "fallback-pw"
    assert auth.pkey_content == ""
    assert "已损坏" in auth.notice


def test_asset_auth_corrupt_asset_password_falls_back_to_credential(db):
    from app.models.asset import Asset
    from app.services.asset_auth import resolve_asset_ssh_auth

    _add_cred(db, "CRED-SSH", name="SSH 凭证")
    cred = db.query(Credential).filter(Credential.cred_id == "CRED-SSH").first()
    cred.type = "Password"
    cred.content = encrypt_secret("cred-password")
    db.add(Asset(
        instance_id="H1", ip="10.0.0.1", ssh_user="root",
        ssh_password="enc::v2::broken::token", credential_ref="CRED-SSH",
    ))
    db.commit()
    asset = db.query(Asset).filter(Asset.instance_id == "H1").first()
    auth = resolve_asset_ssh_auth(db, asset)
    assert auth.password == "cred-password"


def test_bill_sync_corrupt_credential_returns_business_error(db):
    from app.services.bill_sync import load_account_keys

    _add_cred(db, "CRED-BAD", name="坏的凭证")
    cred = db.query(Credential).filter(Credential.cred_id == "CRED-BAD").first()
    cred.content = "enc::v2::broken::token"
    db.commit()
    account = CloudAccount(name="云账号", credential_ref="CRED-BAD")
    ak, sk, err = load_account_keys(db, account)
    assert (ak, sk) == ("", "")
    assert "已损坏" in err


def test_run_issue_job_corrupt_credential_marks_failed_not_500(db, tmp_path, monkeypatch):
    monkeypatch.setattr(cert_api, "CERT_STORAGE_DIR", tmp_path / "certs")
    _add_cred(db, "CRED-BAD", name="阿里云-闫东")
    cred = db.query(Credential).filter(Credential.cred_id == "CRED-BAD").first()
    cred.content = "enc::v2::broken::token"
    db.add(CertificateRecord(
        cert_id="CRT-BAD", primary_domain="bad.example.com", provider="aliyun",
        account_id="CRED-BAD", status="pending",
    ))
    db.commit()

    cert_api.run_issue_job("CRT-BAD", renew=False, reserved=True, user_id=None)
    row = db.query(CertificateRecord).filter(CertificateRecord.cert_id == "CRT-BAD").first()
    assert row.status == "failed"
    assert "已损坏" in (row.last_error or "")
    cert_api.ISSUING_CERT_IDS.discard("CRT-BAD")
