"""数据库初始化入口。

该脚本只做两件事：
1. 按当前 SQLAlchemy 模型创建缺失的数据表；
2. 复用运行时引导逻辑，根据环境变量可选创建首个管理员。

安全边界：
- 不创建默认弱口令账号；
- 不注入演示资产、域名、凭据或集群；
- 不覆盖已有用户密码。
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.db.bootstrap import ensure_bootstrap_admin
from app.db.database import Base, engine
from app.db import models as db_models  # noqa: F401


def init_db() -> None:
    """初始化数据库结构，并按环境变量引导管理员账号。"""
    print("[OpsPilot] 初始化数据库结构...")
    Base.metadata.create_all(bind=engine)
    ensure_bootstrap_admin()
    print("[OpsPilot] 数据库初始化完成。")


if __name__ == "__main__":
    init_db()
