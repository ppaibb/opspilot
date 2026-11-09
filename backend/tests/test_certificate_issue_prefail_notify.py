"""签发前置校验失败的站内信通知。

run_issue_job 里 4 处前置校验失败（账户不存在 / 厂商不支持 /
凭证无法解析 / 域名为空）此前只调 set_issue_failed 改状态+记审计，
用户点签发后静默失败。本测试钉死：4 处均按 except 异常分支同口径
发站内信（category=certificate，link=/assets/certificates，
resource_id=cert_id；有 user_id 通知本人，无则通知全体 admin）。

run_issue_job 用模块级 SessionLocal() 开库会话、append_cert_log 写盘，
测试用内存 SQLite + tmp_path 隔离，不触真实 ACME/网络：4 处失败均
发生在任何 ACME 调用之前。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import certificates
from app.core.crypto import encrypt_secret
from app.models.audit import AuditLog
from app.models.certificate import CertificateRecord
from app.models.credential import Credential
from app.models.notification import UserNotification
from app.models.user import User

AKSK = '{"access_key": "ak", "secret_key": "sk"}'


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (CertificateRecord, Credential, User, AuditLog, UserNotification):
        model.__table__.create(engine)
    # run_issue_job 用模块级 SessionLocal()；指向内存库
    monkeypatch.setattr(certificates, "SessionLocal", lambda: Session(engine))
    # append_cert_log / cert_storage_paths 写盘；隔离到 tmp_path
    monkeypatch.setattr(certificates, "CERT_STORAGE_DIR", tmp_path / "certs")
    session = Session(engine)
    session.add(User(username="admin", name="管理员", role="admin", hashed_password="x"))
    session.add(User(username="op", name="操作员", role="user", hashed_password="x"))
    session.add(Credential(
        cred_id="CRED-ALI", name="阿里云-闫东", type="AccessKey",
        status="active", content=encrypt_secret(AKSK),
    ))
    session.add(Credential(
        cred_id="CRED-AWS", name="AWS-测试", type="AccessKey",
        status="active", content=encrypt_secret(AKSK),
    ))
    session.add(Credential(
        cred_id="CRED-BAD", name="阿里云-损坏", type="AccessKey",
        status="active", content=encrypt_secret("not-json"),
    ))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _add_cert(db, cert_id, domain, account_id):
    db.add(CertificateRecord(
        cert_id=cert_id, primary_domain=domain, san_domains="",
        provider="custom", account_id=account_id,
        acme_server="letsencrypt", key_type="ec-256",
        status="pending", auto_renew="yes", renew_before_days=30,
    ))
    db.commit()


def _notifications(db):
    return db.query(UserNotification).order_by(UserNotification.id).all()


def _run(db, cert_id, renew=False, user_id=2):
    certificates.run_issue_job(cert_id, renew, True, user_id)
    db.expire_all()
    cert = db.query(CertificateRecord).filter(
        CertificateRecord.cert_id == cert_id).first()
    assert cert.status == "failed"
    notes = _notifications(db)
    assert len(notes) == 1
    note = notes[0]
    assert note.category == "certificate"
    assert note.type == "error"
    assert note.link == "/assets/certificates"
    assert note.resource_id == cert_id
    return note


def test_prefail_missing_account_notifies_requester(db):
    _add_cert(db, "cert-missing", "missing.example.com", "CRED-NONE")
    note = _run(db, "cert-missing")
    assert note.user_id == 2
    assert note.title == "证书签发失败"
    assert "missing.example.com" in note.content
    assert "签发账户不存在" in note.content


def test_prefail_unsupported_provider_notifies_requester(db):
    _add_cert(db, "cert-aws", "aws.example.com", "CRED-AWS")
    note = _run(db, "cert-aws")
    assert note.user_id == 2
    assert note.title == "证书签发失败"
    assert "aws.example.com" in note.content
    assert "当前不支持该 DNS 厂商自动签发" in note.content


def test_prefail_unparsable_credential_notifies_requester(db):
    _add_cert(db, "cert-bad", "bad.example.com", "CRED-BAD")
    note = _run(db, "cert-bad")
    assert note.user_id == 2
    assert note.title == "证书签发失败"
    assert "bad.example.com" in note.content
    assert "凭证内容无法解析 AccessKey/SecretKey" in note.content


def test_prefail_empty_domains_notifies_requester(db):
    _add_cert(db, "cert-nodomain", "", "CRED-ALI")
    note = _run(db, "cert-nodomain")
    assert note.user_id == 2
    assert note.title == "证书签发失败"
    assert "签发域名为空" in note.content


def test_prefail_renew_title_differs_from_issue(db):
    _add_cert(db, "cert-renew", "renew.example.com", "CRED-NONE")
    note = _run(db, "cert-renew", renew=True)
    assert note.user_id == 2
    assert note.title == "证书续期失败"
    assert "renew.example.com" in note.content
    assert "续期失败" in note.content


def test_prefail_auto_renew_notifies_all_admins(db):
    _add_cert(db, "cert-auto", "auto.example.com", "CRED-NONE")
    certificates.run_issue_job("cert-auto", True, True, None)
    db.expire_all()
    notes = _notifications(db)
    assert [n.user_id for n in notes] == [1]
    assert notes[0].title == "证书自动续期失败"
    assert "auto.example.com" in notes[0].content
    assert "签发账户不存在" in notes[0].content
