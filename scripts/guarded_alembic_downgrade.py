"""要求备份凭据后才允许执行 Alembic downgrade。"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision", help="目标修订，例如 -1")
    parser.add_argument("--backup-id", required=True, help="已验证数据库备份/快照编号")
    parser.add_argument("--confirm", action="store_true", help="确认接受数据丢失风险")
    args = parser.parse_args()
    if not args.confirm or len(args.backup_id.strip()) < 8:
        parser.error("降级前必须提供至少 8 字符的备份编号并显式 --confirm")
    if os.getenv("ALLOW_ALEMBIC_DOWNGRADE") != "I_HAVE_A_VERIFIED_BACKUP":
        parser.error("必须设置 ALLOW_ALEMBIC_DOWNGRADE=I_HAVE_A_VERIFIED_BACKUP")
    return subprocess.call([sys.executable, "-m", "alembic", "downgrade", args.revision])


if __name__ == "__main__":
    raise SystemExit(main())
