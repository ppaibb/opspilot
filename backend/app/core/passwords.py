"""
密码处理工具。

说明：
1. 统一封装口令哈希与校验，避免不同模块各自维护一套 CryptContext；
2. 当前仅支持 bcrypt，满足现阶段 SQLite + FastAPI 原型环境；
3. 所有用户新增、登录校验、密码重置都必须复用本模块。
"""
from __future__ import annotations

from passlib.context import CryptContext


_PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """对明文密码进行哈希。"""
    return _PASSWORD_CONTEXT.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与哈希值是否匹配。"""
    return _PASSWORD_CONTEXT.verify(plain_password, hashed_password)
