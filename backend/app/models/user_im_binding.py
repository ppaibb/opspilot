"""用户 IM 渠道绑定模型（第一版：飞书）。

【设计说明】
 每个登录用户可绑定自己的飞书身份，平台事件（证书/工单/告警等）产生
 用户通知时，按其偏好在飞书群里 @ 本人（第一版不做私信，私信需要
 飞书自建应用的消息能力，见开发文档 29 期规划）。

关键约束：
1. 一个用户在同一个平台只有一条绑定（user_id + platform 唯一）；
2. open_id 是飞书侧用户标识（ou_ 前缀），由 app 凭证调通讯录接口按
   手机号/邮箱解析得到，不允许前端直填——防止冒用他人身份；
3. webhook_url 是"群自定义机器人"地址，决定 @ 消息发到哪个群；
   为空时回落到系统级默认群机器人（管理员在系统管理里配置）。
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint

from app.db.database import Base


class UserImBinding(Base):
    """用户 IM 渠道绑定（飞书）。"""

    __tablename__ = "user_im_bindings"
    __table_args__ = (UniqueConstraint("user_id", "platform", name="uq_user_im_binding"),)

    id = Column(Integer, primary_key=True, index=True, comment="自增ID")
    user_id = Column(Integer, nullable=False, index=True, comment="平台用户ID")
    platform = Column(String(20), nullable=False, default="feishu", comment="IM平台: feishu")
    identity = Column(String(100), nullable=False, default="", comment="解析用的手机号或邮箱")
    open_id = Column(String(64), nullable=False, default="", comment="飞书open_id(ou_开头)")
    notify_mode = Column(
        String(20),
        nullable=False,
        default="group_at",
        comment="通知方式: group_at(群内@本人) / off(关闭)",
    )
    webhook_url = Column(String(500), nullable=False, default="", comment="群自定义机器人webhook,空则用系统默认")
    bound_at = Column(String(30), nullable=False, default="", comment="绑定时间")
    updated_at = Column(String(30), nullable=False, default="", comment="最近更新时间")
