"""个人 IM 渠道（飞书群 @）契约测试。

覆盖：消息文本构造 / token 缓存 / 身份解析 / 群发送结果判定 /
绑定分发（fanout）与告警通知人分发 / 规则 im_user_ids 归一化 /
webhook 域名白名单 / create_notification 的 IM 挂钩。
"""
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.system_config import SystemConfig
from app.models.user_im_binding import UserImBinding  # noqa: F401  注册进 Base
from app.models.notification import UserNotification  # noqa: F401  注册进 Base
from app.services import im_feishu
from app.services.im_feishu import (
    FeishuError,
    fanout_user_notification,
    get_tenant_access_token,
    invalidate_token_cache,
    notify_users_alert,
    resolve_open_id,
    send_group_at,
    _build_at_text,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        bind=engine,
        tables=[UserImBinding.__table__, SystemConfig.__table__, UserNotification.__table__],
    )
    return sessionmaker(bind=engine)()


def _save_config(db, *, app_id="cli_app", app_secret="secret", default_webhook=""):
    from datetime import datetime

    import json

    row = SystemConfig(
        config_key=im_feishu.IM_FEISHU_CONFIG_KEY,
        config_value=json.dumps(
            {"app_id": app_id, "app_secret": app_secret, "default_webhook_url": default_webhook},
            ensure_ascii=False,
        ),
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.add(row)
    db.commit()
    return row


def _bind(db, user_id=1, open_id="ou_user1", notify_mode="group_at", webhook_url=""):
    from datetime import datetime

    row = UserImBinding(
        user_id=user_id,
        platform="feishu",
        identity="13800000001",
        open_id=open_id,
        notify_mode=notify_mode,
        webhook_url=webhook_url,
        bound_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        updated_at="",
    )
    db.add(row)
    db.commit()
    return row


class AtTextContract(unittest.TestCase):
    """飞书 @ 文本消息构造。"""

    def test_contains_at_and_fields(self):
        text = _build_at_text("ou_x", "证书续期完成", "example.com 已续期", "/assets/certificates")
        self.assertIn('<at user_id="ou_x"></at>', text)
        self.assertIn("【证书续期完成】", text)
        self.assertIn("example.com 已续期", text)
        self.assertIn("查看详情: /assets/certificates", text)

    def test_minimal(self):
        text = _build_at_text("ou_x", "", "", "")
        self.assertEqual(text, '<at user_id="ou_x"></at>')


class TokenContract(unittest.TestCase):
    """tenant_access_token 获取与缓存。"""

    def setUp(self):
        invalidate_token_cache()

    def tearDown(self):
        invalidate_token_cache()

    @patch("app.services.im_feishu.requests.post")
    def test_token_cached(self, mock_post):
        mock_post.return_value.json.return_value = {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        first = get_tenant_access_token("app", "secret")
        second = get_tenant_access_token("app", "secret")
        self.assertEqual(first, "t-abc")
        self.assertEqual(second, "t-abc")
        self.assertEqual(mock_post.call_count, 1)  # 第二次走缓存

    @patch("app.services.im_feishu.requests.post")
    def test_token_missing_credentials(self, mock_post):
        with self.assertRaises(FeishuError):
            get_tenant_access_token("", "")
        mock_post.assert_not_called()

    @patch("app.services.im_feishu.requests.post")
    def test_token_error_code(self, mock_post):
        mock_post.return_value.json.return_value = {"code": 99991, "msg": "bad app"}
        with self.assertRaises(FeishuError):
            get_tenant_access_token("app", "secret")

    @patch("app.services.im_feishu.requests.post")
    def test_token_network_error(self, mock_post):
        mock_post.side_effect = OSError("boom")
        with self.assertRaises(FeishuError):
            get_tenant_access_token("app", "secret")


class ResolveContract(unittest.TestCase):
    """手机号/邮箱 → open_id 解析。"""

    def setUp(self):
        invalidate_token_cache()

    def tearDown(self):
        invalidate_token_cache()

    @patch("app.services.im_feishu.requests.post")
    def test_resolve_by_email_uses_emails_field(self, mock_post):
        mock_post.side_effect = [
            _Resp({"code": 0, "tenant_access_token": "t", "expire": 7200}),
            _Resp({"code": 0, "data": {"user_list": [{"user_id": "ou_by_email"}]}}),
        ]
        open_id = resolve_open_id("app", "secret", "zhangsan@corp.com")
        self.assertEqual(open_id, "ou_by_email")
        body = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(body, {"emails": ["zhangsan@corp.com"]})

    @patch("app.services.im_feishu.requests.post")
    def test_resolve_by_mobile_uses_mobiles_field(self, mock_post):
        mock_post.side_effect = [
            _Resp({"code": 0, "tenant_access_token": "t", "expire": 7200}),
            _Resp({"code": 0, "data": {"user_list": [{"open_id": "ou_by_mobile"}]}}),
        ]
        open_id = resolve_open_id("app", "secret", "13800000001")
        self.assertEqual(open_id, "ou_by_mobile")
        body = mock_post.call_args_list[1].kwargs["json"]
        self.assertEqual(body, {"mobiles": ["13800000001"]})

    @patch("app.services.im_feishu.requests.post")
    def test_resolve_not_found_raises(self, mock_post):
        mock_post.side_effect = [
            _Resp({"code": 0, "tenant_access_token": "t", "expire": 7200}),
            _Resp({"code": 0, "data": {"user_list": []}}),
        ]
        with self.assertRaises(FeishuError):
            resolve_open_id("app", "secret", "13900000000")

    def test_resolve_empty_identity(self):
        with self.assertRaises(FeishuError):
            resolve_open_id("app", "secret", "  ")


class SendGroupAtContract(unittest.TestCase):
    """群自定义机器人发送结果判定。"""

    def test_success_new_code(self):
        with patch("app.services.im_feishu.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"code": 0, "msg": "success"}
            result = send_group_at("https://open.feishu.cn/open-apis/bot/v2/hook/x", "ou_x", "t", "c")
        self.assertTrue(result["ok"])

    def test_success_legacy_status_code(self):
        with patch("app.services.im_feishu.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"StatusCode": 0}
            result = send_group_at("https://open.feishu.cn/open-apis/bot/v2/hook/x", "ou_x", "t", "c")
        self.assertTrue(result["ok"])

    def test_rejected_raises(self):
        with patch("app.services.im_feishu.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"code": 19001, "msg": "bot not in chat"}
            with self.assertRaises(FeishuError):
                send_group_at("https://open.feishu.cn/open-apis/bot/v2/hook/x", "ou_x", "t", "c")

    def test_guards(self):
        with self.assertRaises(FeishuError):
            send_group_at("", "ou_x", "t", "c")  # 空 webhook
        with self.assertRaises(FeishuError):
            send_group_at("ftp://x", "ou_x", "t", "c")  # 非 http(s)
        with self.assertRaises(FeishuError):
            send_group_at("https://open.feishu.cn/hook/x", "", "t", "c")  # 空 open_id


class FanoutContract(unittest.TestCase):
    """用户通知 → 飞书群 @ 分发。"""

    def test_no_binding_skipped(self):
        db = _make_db()
        result = fanout_user_notification(db, user_id=1, title="t", content="c")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skipped"], "未绑定")

    def test_mode_off_skipped(self):
        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db, notify_mode="off")
        result = fanout_user_notification(db, user_id=1, title="t", content="c")
        self.assertEqual(result["skipped"], "已关闭")

    def test_no_webhook_anywhere_skipped(self):
        db = _make_db()
        _bind(db, webhook_url="")
        result = fanout_user_notification(db, user_id=1, title="t", content="c")
        self.assertEqual(result["skipped"], "未配置群机器人")

    @patch("app.services.im_feishu.send_group_at")
    def test_personal_webhook_preferred(self, mock_send):
        mock_send.return_value = {"ok": True}
        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db, webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/personal")
        result = fanout_user_notification(db, user_id=1, title="标题", content="内容", link="/x")
        self.assertTrue(result["ok"])
        args = mock_send.call_args.args
        self.assertEqual(args[0], "https://open.feishu.cn/open-apis/bot/v2/hook/personal")
        self.assertEqual(args[1], "ou_user1")
        self.assertEqual(args[2], "标题")

    @patch("app.services.im_feishu.send_group_at")
    def test_fallback_to_default_webhook(self, mock_send):
        mock_send.return_value = {"ok": True}
        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db, webhook_url="")
        fanout_user_notification(db, user_id=1, title="t", content="c")
        self.assertEqual(mock_send.call_args.args[0], "https://open.feishu.cn/open-apis/bot/v2/hook/def")

    @patch("app.services.im_feishu.send_group_at")
    def test_send_failure_is_swallowed(self, mock_send):
        mock_send.side_effect = FeishuError("bot 拒收")
        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db)
        result = fanout_user_notification(db, user_id=1, title="t", content="c")
        self.assertFalse(result["ok"])
        self.assertIn("拒收", result["error"])


class NotifyUsersAlertContract(unittest.TestCase):
    """告警规则通知人批量 @。"""

    @patch("app.services.im_feishu.send_group_at")
    def test_mixed_users(self, mock_send):
        mock_send.return_value = {"ok": True}
        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db, user_id=1, open_id="ou_1")
        _bind(db, user_id=2, open_id="ou_2", notify_mode="off")
        _bind(db, user_id=3, open_id="ou_3", webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/p3")
        result = notify_users_alert(db, [1, 2, 3, 99], title="告警触发：CPUHigh", content="c")
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["skipped"], 2)  # user2 关闭 + user99 未绑定
        webhooks = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("https://open.feishu.cn/open-apis/bot/v2/hook/p3", webhooks)

    @patch("app.services.im_feishu.send_group_at")
    def test_dedupe_user_ids(self, mock_send):
        mock_send.return_value = {"ok": True}
        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db, user_id=1)
        notify_users_alert(db, [1, 1, 1], title="t", content="c")
        self.assertEqual(mock_send.call_count, 1)


class DispatcherHookContract(unittest.TestCase):
    """告警派发器的 IM 钩子。"""

    @patch("app.services.im_feishu.notify_users_alert")
    def test_notify_im_users_builds_message(self, mock_notify):
        from app.api.monitoring.services.alert_dispatcher import _notify_im_users

        db = _make_db()
        _notify_im_users(
            db,
            [1, 2],
            {"name": "CPUHigh", "instance": "10.0.0.1", "severity": "critical", "summary": "CPU 95%"},
            "触发",
        )
        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "告警触发：CPUHigh")
        self.assertIn("CPU 95%", kwargs["content"])
        self.assertIn("10.0.0.1", kwargs["content"])
        self.assertIn("critical", kwargs["content"])
        self.assertTrue(kwargs["link"].endswith("/monitoring/alerts"))

    @patch("app.services.im_feishu.notify_users_alert")
    def test_notify_im_users_empty_list_noop(self, mock_notify):
        from app.api.monitoring.services.alert_dispatcher import _notify_im_users

        db = _make_db()
        _notify_im_users(db, [], {"name": "X"}, "触发")
        mock_notify.assert_not_called()

    @patch("app.services.im_feishu.notify_users_alert")
    def test_notify_im_users_failure_swallowed(self, mock_notify):
        mock_notify.side_effect = RuntimeError("boom")
        from app.api.monitoring.services.alert_dispatcher import _notify_im_users

        db = _make_db()
        # 不抛异常即通过
        _notify_im_users(db, [1], {"name": "X"}, "触发")


class RuleNormalizationContract(unittest.TestCase):
    """告警规则 im_user_ids 归一化。"""

    def _normalize(self, raw):
        from app.api.monitoring.services.rules import _normalize_monitoring_rules

        return _normalize_monitoring_rules(raw, set())

    def test_valid_ids_deduped(self):
        rules = self._normalize([
            {"id": "r1", "name": "CPU", "expr": "up == 0", "im_user_ids": [3, 1, 3, "2"]},
        ])
        self.assertEqual(rules[0]["im_user_ids"], [3, 1, 2])

    def test_invalid_ids_dropped(self):
        rules = self._normalize([
            {"id": "r1", "name": "CPU", "expr": "up == 0", "im_user_ids": [0, -5, "abc", None, 7]},
        ])
        self.assertEqual(rules[0]["im_user_ids"], [7])

    def test_capped_at_20(self):
        rules = self._normalize([
            {"id": "r1", "name": "CPU", "expr": "up == 0", "im_user_ids": list(range(1, 40))},
        ])
        self.assertEqual(len(rules[0]["im_user_ids"]), 20)

    def test_missing_field_defaults_empty(self):
        rules = self._normalize([{"id": "r1", "name": "CPU", "expr": "up == 0"}])
        self.assertEqual(rules[0]["im_user_ids"], [])

    def test_non_list_ignored(self):
        rules = self._normalize([{"id": "r1", "name": "CPU", "expr": "up == 0", "im_user_ids": "1,2"}])
        self.assertEqual(rules[0]["im_user_ids"], [])


class WebhookWhitelistContract(unittest.TestCase):
    """个人群机器人 webhook 域名白名单（防 SSRF）。"""

    def _validate(self, url):
        from app.api.im import _validate_webhook

        return _validate_webhook(url)

    def test_feishu_https_allowed(self):
        url = self._validate("https://open.feishu.cn/open-apis/bot/v2/hook/abc")
        self.assertEqual(url, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")

    def test_empty_allowed(self):
        self.assertEqual(self._validate("  "), "")

    def test_non_https_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            self._validate("http://open.feishu.cn/open-apis/bot/v2/hook/abc")

    def test_other_domain_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            self._validate("https://evil.example.com/hook")
        with self.assertRaises(HTTPException):
            self._validate("https://192.168.3.99:8001/api/x")


class CreateNotificationFanoutContract(unittest.TestCase):
    """create_notification 写库后触发 IM 分发。"""

    @patch("app.services.im_feishu.send_group_at")
    def test_notification_triggers_fanout(self, mock_send):
        mock_send.return_value = {"ok": True}
        from app.api.notifications import NotificationCreate, create_notification

        db = _make_db()
        _save_config(db, default_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/def")
        _bind(db, user_id=42)
        create_notification(
            db,
            data=NotificationCreate(
                user_id=42,
                type="success",
                category="certificate",
                title="证书续期完成",
                content="example.com 已续期",
                link="/assets/certificates",
            ),
        )
        mock_send.assert_called_once()
        args = mock_send.call_args.args
        self.assertEqual(args[1], "ou_user1")
        self.assertEqual(args[2], "证书续期完成")
        self.assertEqual(args[3], "example.com 已续期")
        self.assertEqual(args[4], "/assets/certificates")

    @patch("app.services.im_feishu.send_group_at")
    def test_unbound_user_no_send(self, mock_send):
        from app.api.notifications import NotificationCreate, create_notification

        db = _make_db()
        create_notification(
            db,
            data=NotificationCreate(user_id=7, type="info", category="system", title="t", content="c"),
        )
        mock_send.assert_not_called()


class _Resp:
    """requests.post 的替身：只实现 .json()。"""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


if __name__ == "__main__":
    unittest.main()
