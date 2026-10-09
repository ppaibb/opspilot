# 数据库工单功能·分用户矩阵测试（真实接口、真实开发库）
# 覆盖：权限面 / 审核规则拦截 / 状态机 / 自审批分权 / 实例授权 / 查询授权工单 /
#       备份回滚 / 驳回流 / 定时执行 / 审计留痕。
# 会还原共享配置（allowed_roles）；产生的工单与 smoke 表数据按 smoke 约定保留。
from __future__ import annotations

import datetime as dt
import requests
import sqlalchemy as sa
import sys
import time

BASE = "http://127.0.0.1:8001"
RESULTS: list[tuple[str, bool, str]] = []


def login(u: str, p: str) -> dict:
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, f"{u} 登录失败"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else '✗ FAIL'} | {name}" + (f" | {detail[:90]}" if detail else ""))


admin = login("admin", "Admin@12345")
ops = login("ops01", "Ops@12345")
dba = login("dba01", "Dba@12345")
viewer = login("viewer01", "View@12345")

# ── A. 权限面 ──────────────────────────────────────────────
r = requests.get(f"{BASE}/api/db/tickets", headers=viewer)
check("viewer01 看工单列表被权限拦截", r.status_code in (401, 403), f"HTTP {r.status_code}")

r = requests.post(f"{BASE}/api/db/tickets", headers=viewer, json={
    "instance_id": 1, "ticket_type": "change",
    "sql_text": "UPDATE smoke_multiuser_t SET note='x' WHERE id=1;"})
check("viewer01 提交工单 403（无 action.db.submit）", r.status_code == 403, f"HTTP {r.status_code}")

# ── B. 审核规则拦截（ops01 提交） ────────────────────────────
r = requests.post(f"{BASE}/api/db/tickets", headers=ops, json={
    "instance_id": 1, "ticket_type": "change", "database": "opspilot",
    "sql_text": "DROP TABLE smoke_multiuser_t;"})
body = r.json()
check("DROP TABLE 被 422 拒收（D3）", r.status_code == 422, str(body.get("detail"))[:80])
check("拒收明细点明禁止 DROP 及不可恢复后果", "禁止 DROP TABLE" in str(body.get("detail")), "")

r = requests.post(f"{BASE}/api/db/tickets", headers=ops, json={
    "instance_id": 1, "ticket_type": "change", "database": "opspilot",
    "sql_text": "DELETE FROM smoke_multiuser_t;"})
check("无 WHERE DELETE 被 422 拒收（D4/R4）", r.status_code == 422, str(r.json().get("detail"))[:80])

r = requests.post(f"{BASE}/api/db/tickets", headers=ops, json={
    "instance_id": 1, "ticket_type": "change", "database": "opspilot",
    "title": "字符串不误伤-D5",
    "sql_text": "INSERT INTO smoke_multiuser_t VALUES (2, '包含 drop table users; 的普通字符串');"})
ok = r.status_code == 200
ticket_b = r.json().get("id") if ok else None
check("字符串含 DROP 语的 INSERT 正常过审（D5）", ok, f"ticket={ticket_b}")

# ── C. 变更工单全流程 + 状态机（ops01 提交 / dba01 审批执行） ────
SQL_A = "UPDATE smoke_multiuser_t SET note='flow-A-执行于分用户测试' WHERE id = 1;"
r = requests.post(f"{BASE}/api/db/tickets", headers=ops, json={
    "instance_id": 1, "ticket_type": "change", "database": "opspilot",
    "title": "分用户测试-A 全流程", "sql_text": SQL_A})
ticket_a = r.json().get("id")
check("ops01 提交 UPDATE 工单", r.status_code == 200 and ticket_a, f"ticket={ticket_a}")

r = requests.post(f"{BASE}/api/db/tickets/{ticket_a}/execute", headers=dba)
check("未审批直接执行被 422 拒绝（状态机）", r.status_code == 422, str(r.json().get("detail"))[:60])

r = requests.post(f"{BASE}/api/db/tickets/{ticket_a}/review", headers=ops, json={"decision": "approve"})
check("ops01 自审批被 403 拒绝（分权）", r.status_code == 403, str(r.json().get("detail"))[:40])

r = requests.post(f"{BASE}/api/db/tickets/{ticket_a}/review", headers=dba, json={"decision": "approve"})
check("dba01 审批通过", r.status_code == 200, "")

r = requests.post(f"{BASE}/api/db/tickets/{ticket_a}/review", headers=dba, json={"decision": "approve"})
check("重复审批被 422 拒绝（D7）", r.status_code == 422, str(r.json().get("detail"))[:40])

r = requests.post(f"{BASE}/api/db/tickets/{ticket_a}/execute", headers=dba)
check("dba01 执行成功", r.status_code == 200, "")

r = requests.post(f"{BASE}/api/db/tickets/{ticket_a}/execute", headers=dba)
check("重复执行被 422 拒绝（状态机）", r.status_code == 422, str(r.json().get("detail"))[:40])

# D14 备份回滚：UPDATE 工单应有回滚语句可导出
r = requests.get(f"{BASE}/api/db/tickets/{ticket_a}/rollback", headers=admin)
rb = r.text or ""
check("UPDATE 工单可导出回滚 SQL（D14）", r.status_code == 200 and ("UPDATE" in rb or "INSERT" in rb), rb[:80])

# 数据真实变更验证
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
from app.core.config import settings as _cfg  # noqa: E402

eng = sa.create_engine(_cfg.DATABASE_URL)
with eng.connect() as conn:
    note_a = conn.execute(sa.text("SELECT note FROM smoke_multiuser_t WHERE id=1")).scalar()
check("工单 A 的 UPDATE 真实落库", note_a == "flow-A-执行于分用户测试", f"note={note_a}")

# ── D. 驳回流（D5 工单走驳回） ──────────────────────────────
r = requests.post(f"{BASE}/api/db/tickets/{ticket_b}/review", headers=dba, json={"decision": "reject"})
check("dba01 驳回工单 B", r.status_code == 200, "")
r = requests.get(f"{BASE}/api/db/tickets", headers=admin)
rows = r.json() if isinstance(r.json(), list) else r.json().get("items") or []
row_b = next((t for t in rows if t.get("id") == ticket_b), {})
check("工单 B 状态为 rejected", row_b.get("status") == "rejected", f"status={row_b.get('status')}")
r = requests.post(f"{BASE}/api/db/tickets/{ticket_b}/review", headers=dba, json={"decision": "approve"})
check("已驳回工单再审批被 422 拒绝", r.status_code == 422, str(r.json().get("detail"))[:40])

# ── E. 实例授权（D11）：临时把实例限给 DBA，验完还原 ───────────
r = requests.put(f"{BASE}/api/db/instances/1", headers=admin, json={
    "name": "本机pg数据库", "db_type": "postgresql", "host": "192.168.3.99",
    "port": 5432, "username": "opspilot", "database": "opspilot",
    "allowed_roles": ["DBA"]})
check("admin 将实例授权限给 DBA 角色", r.status_code == 200, r.text[:60])

r = requests.post(f"{BASE}/api/db/tickets", headers=ops, json={
    "instance_id": 1, "ticket_type": "change", "database": "opspilot",
    "sql_text": "UPDATE smoke_multiuser_t SET note='x' WHERE id=1;"})
check("未授权角色 ops01 提工单被 403（D11）", r.status_code == 403, str(r.json().get("detail"))[:50])

r = requests.post(f"{BASE}/api/db/query", headers=ops, json={
    "instance_id": 1, "database": "opspilot", "sql_text": "SELECT 1;"})
check("未授权角色自助查询被 403", r.status_code == 403, str(r.json().get("detail"))[:50])

requests.put(f"{BASE}/api/db/instances/1", headers=admin, json={
    "name": "本机pg数据库", "db_type": "postgresql", "host": "192.168.3.99",
    "port": 5432, "username": "opspilot", "database": "opspilot",
    "allowed_roles": []})
r = requests.get(f"{BASE}/api/db/instances/1", headers=admin)
back = "DBA" not in str(r.json().get("allowed_roles"))
check("实例授权已还原为全员", back, str(r.json().get("allowed_roles")))

# ── F. 查询授权工单流（申请→批准→授权生效→自助查询→撤销） ───────
# 受试者用 dba01：其角色无 action.db.manage（管理员豁免走不通），
# 能完整走"无授权被拒 → 申请 → 批准生效 → 可查 → 撤销回落"链路。
r = requests.post(f"{BASE}/api/db/query", headers=dba, json={
    "instance_id": 1, "database": "opspilot", "sql_text": "SELECT 1;"})
check("授权前 dba01 自助查询被 403 拒绝", r.status_code == 403, str(r.json().get("detail"))[:50])

r = requests.post(f"{BASE}/api/db/tickets", headers=dba, json={
    "instance_id": 1, "ticket_type": "query", "database": "opspilot",
    "grant_hours": 1, "grant_reason": "分用户测试-查询授权",
    "grant_tables": ["smoke_multiuser_t"]})
ticket_c = r.json().get("id")
check("dba01 提交查询授权申请", r.status_code == 200 and ticket_c, f"ticket={ticket_c}")

r = requests.post(f"{BASE}/api/db/tickets/{ticket_c}/review", headers=admin, json={"decision": "approve"})
check("admin 批准查询授权（授权自动生效）", r.status_code == 200, "")

r = requests.post(f"{BASE}/api/db/query", headers=dba, json={
    "instance_id": 1, "database": "opspilot", "sql_text": "SELECT 1;"})
check("授权后 dba01 自助查询成功", r.status_code == 200 and r.json().get("ok"), "")

r = requests.get(f"{BASE}/api/db/query-grants", params={"scope": "all"}, headers=admin)
gj = r.json()
grants = gj if isinstance(gj, list) else (gj.get("items") or gj.get("grants") or list(gj.values())[0] if isinstance(gj, dict) else [])
mine = None
for g in grants if isinstance(grants, list) else []:
    if str(g.get("username")) == "dba01" and str(g.get("status")) == "active":
        mine = g
        break
check("授权台账出现 dba01 的 active 授权", mine is not None,
      f"grants_type={type(grants).__name__} n={len(grants) if isinstance(grants, list) else '?'}")

if mine:
    r = requests.post(f"{BASE}/api/db/query-grants/{mine['id']}/revoke", headers=admin)
    check("撤销授权", r.status_code == 200, "")
    r = requests.post(f"{BASE}/api/db/query", headers=dba, json={
        "instance_id": 1, "database": "opspilot", "sql_text": "SELECT 1;"})
    check("撤销后自助查询回到 403", r.status_code == 403, "")

# ── G. 定时执行（D13）：90 秒后到点，scheduler 自动执行 ────────
target = (dt.datetime.now() + dt.timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
r = requests.post(f"{BASE}/api/db/tickets", headers=ops, json={
    "instance_id": 1, "ticket_type": "change", "database": "opspilot",
    "title": "分用户测试-定时执行", "execute_after": target,
    "sql_text": "UPDATE smoke_multiuser_t SET note='scheduled-ok' WHERE id = 1;"})
ticket_d = r.json().get("id")
check("提交定时工单（90s 后）", r.status_code == 200 and ticket_d, f"ticket={ticket_d}")
if ticket_d:
    r = requests.post(f"{BASE}/api/db/tickets/{ticket_d}/review", headers=dba, json={"decision": "approve"})
    check("dba01 批准定时工单", r.status_code == 200, "")
    print(">> 等待 scheduler 到点自动执行（最长 150s）…")
    deadline = dt.datetime.now() + dt.timedelta(seconds=150)
    status = ""
    while dt.datetime.now() < deadline:
        time.sleep(10)
        rr = requests.get(f"{BASE}/api/db/tickets", headers=admin)
        rows = rr.json() if isinstance(rr.json(), list) else rr.json().get("items") or []
        row_d = next((t for t in rows if t.get("id") == ticket_d), {})
        status = row_d.get("status")
        if status in ("success", "failed"):
            break
    with eng.connect() as conn:
        note_d = conn.execute(sa.text("SELECT note FROM smoke_multiuser_t WHERE id=1")).scalar()
    check("定时工单到点自动执行（D13）", status == "success" and note_d == "scheduled-ok",
          f"status={status} note={note_d}")

# ── H. 审计留痕（D10）：有工单的实例禁止删除 ─────────────────
r = requests.delete(f"{BASE}/api/db/instances/1", headers=admin)
check("有工单记录的实例删除被 422 拒绝（D10）", r.status_code == 422, str(r.json().get("detail"))[:60])

# ── I. 通知验证：ops01 应收到 驳回/审批通过/执行结果 通知 ────────
r = requests.get(f"{BASE}/api/notifications?limit=10", headers=ops)
items = r.json() if isinstance(r.json(), list) else r.json().get("items") or []
titles = " | ".join(str(n.get("title") or "") for n in items[:8])
check("ops01 收到审批/执行/驳回通知", ("审批通过" in titles) and ("执行成功" in titles or "执行" in titles),
      titles[:90])

# ── 汇总 ──────────────────────────────────────────────────
failed = [x for x in RESULTS if not x[1]]
print(f"\n===== 结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 =====")
for name, ok, detail in failed:
    print(f"✗ {name} | {detail[:120]}")
sys.exit(1 if failed else 0)
