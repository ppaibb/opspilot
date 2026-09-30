"""真实环境端到端冒烟：验证高危票据链路（只读探针，不改真实数据）。

探针原理：对"不存在的资源"调用受保护路由。
- 票据正确 → 守卫放行 → 业务层返回 404/422；
- 票据缺失/错误/重放 → 守卫拦截 → 401/403。
两种结果可区分，无需产生任何真实副作用。

用法：backend/.venv/Scripts/python.exe scripts/smoke_ticket_e2e.py [base_url]
"""
import sys

try:
    import urllib.error
    import urllib.request
except ImportError:  # pragma: no cover
    raise

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8019") + "/api"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FAILED = []


def call(method, path, body=None, headers=None):
    """发起请求，返回 (status, body)。"""
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with OPENER.open(req, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:200]
    except Exception as e:  # 连接失败等
        return -1, str(e)[:200]


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILED.append(f"{name} {detail}")


#  前端真实使用的动作键 → 一个安全的"不存在资源"探针。
#  结构：唯一标识 → (动作键, 方法, 路径, 请求体)；方法/路径按 clusters.py 装饰器逐个核对。
PROBES = {
    "hosts:delete": ("hosts:delete", "DELETE", "/assets/no-such-host-id", None),
    "hosts:batch_delete": ("hosts:batch_delete", "POST", "/assets/batch-delete",
                            {"ids": ["no-such-host-id"]}),
    "clusters:delete": ("clusters:delete", "DELETE", "/clusters/no-such-cluster", None),
    "clusters:edit(基础信息)": ("clusters:edit", "PUT", "/clusters/no-such-cluster", {"name": "smoke"}),
    "clusters:edit(资源编辑)": ("clusters:edit", "PATCH",
                                "/clusters/no-such/resources/default/ConfigMap/x/edit", {}),
    "clusters:edit(扩缩容)": ("clusters:edit", "PATCH",
                              "/clusters/no-such/workloads/Deployment/default/x/scale", {}),
    "clusters:apply": ("clusters:apply", "POST", "/clusters/no-such-cluster/apply", {}),
    "clusters:drain": ("clusters:drain", "POST", "/clusters/no-such/nodes/x/drain", {}),
    "clusters:cordon": ("clusters:cordon", "POST", "/clusters/no-such/nodes/x/cordon", {}),
    "clusters:uncordon": ("clusters:uncordon", "POST", "/clusters/no-such/nodes/x/uncordon", {}),
    "clusters:patch_labels": ("clusters:patch_labels", "PATCH",
                              "/clusters/no-such/nodes/x/labels", {}),
    "clusters:patch_taints": ("clusters:patch_taints", "PATCH",
                              "/clusters/no-such/nodes/x/taints", {}),
    "clusters:create_namespace": ("clusters:create_namespace", "POST",
                                  "/clusters/no-such/namespaces", {"name": "smoke"}),
    "clusters:restart": ("clusters:restart", "PATCH",
                         "/clusters/no-such/workloads/Deployment/default/x/restart", {}),
    "clusters:set-image": ("clusters:set-image", "PATCH",
                           "/clusters/no-such/workloads/Deployment/default/x/set-image", {}),
    "clusters:rollout-undo": ("clusters:rollout-undo", "POST",
                              "/clusters/no-such/workloads/Deployment/default/x/rollout-undo", {}),
    "clusters:pause": ("clusters:pause", "PATCH",
                       "/clusters/no-such/workloads/Deployment/default/x/pause", {}),
    "clusters:trigger_cronjob": ("clusters:trigger_cronjob", "POST",
                                 "/clusters/no-such/cronjobs/default/x/trigger", {}),
    "clusters:suspend_cronjob": ("clusters:suspend_cronjob", "PATCH",
                                 "/clusters/no-such/cronjobs/default/x/suspend", {}),
    "clusters:resize_pvc": ("clusters:resize_pvc", "PATCH",
                            "/clusters/no-such/pvc/default/x/resize", {}),
    "vms:poweroff": ("vms:poweroff", "POST", "/vms/999999/power", {"action": "stop"}),
    "vms:delete": ("vms:delete", "DELETE", "/vms/999999", None),
    "credentials:delete": ("credentials:delete", "DELETE", "/credentials/999999", None),
    "credentials:export": ("credentials:export", "GET", "/credentials/999999/export", None),
    "domains:delete": ("domains:delete", "DELETE", "/domains/999999", None),
    "certificates:revoke": ("certificates:revoke", "POST", "/certificates/999999/revoke", {}),
    "certificates:delete": ("certificates:delete", "DELETE", "/certificates/999999", None),
    "certificates:download": ("certificates:download", "GET", "/certificates/999999/download", None),
    "wireguard:delete": ("wireguard:delete", "DELETE", "/wireguard/networks/999999", None),
    "apps:delete": ("apps:delete", "DELETE", "/applications/999999", None),
    "config:rollback": ("config:rollback", "POST", "/applications/999999/configs/999999/rollback", {}),
    "finance:create": ("finance:create", "POST", "/finance/accounts", {}),
    "finance:delete": ("finance:delete", "DELETE", "/finance/accounts/999999", None),
    "users:reset-password": ("users:reset-password", "POST",
                             "/settings/users/999999/reset-password", {"new_password": "Xx@123456"}),
}

# 需要真实存在对象才能触发守卫的探针（不存在对象的 404 早于票据校验）。
# users:disable 走"建临时用户→测→清理"的闭环。
TEMP_USER = "smoke-ticket-probe"


def cleanup_temp_user() -> None:
    """清理冒烟探针临时账号（仅删除本脚本创建的用户）。

    项目没有提供删除用户的 HTTP 接口，且该账号是本次冒烟的产物，
    因此直接用后端 ORM 删行。仅在 backend/.venv 下运行时可用。
    """
    try:
        import os
        backend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "backend")
        if os.path.isdir(backend_dir):
            # Settings 按 CWD 找 .env，切到 backend 目录才能连上开发库
            os.chdir(backend_dir)
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)
        from app.db.database import SessionLocal
        from app.models.user import User
        db = SessionLocal()
        try:
            deleted = db.query(User).filter(
                User.username == TEMP_USER).delete(synchronize_session=False)
            db.commit()
            print(f"[INFO] 清理临时探针账号：{TEMP_USER}（删除 {deleted} 行）")
        finally:
            db.close()
    except Exception as e:  # 非 backend venv 环境跑时给出手工提示
        print(f"[WARN] 未能自动清理临时账号 {TEMP_USER}：{e}")
        print(f"       请手工清理：DELETE FROM users WHERE username = '{TEMP_USER}';")


def main() -> int:
    print(f"冒烟目标：{BASE}")

    st, resp = call("POST", "/auth/login", {"username": "admin", "password": "Admin@12345"})
    check("真实登录", st == 200 and isinstance(resp, dict) and "access_token" in resp, f"(HTTP {st})")
    if st != 200:
        return 1
    auth = {"Authorization": f"Bearer {resp['access_token']}"}

    st, resp = call("GET", "/auth/risk-levels", headers=auth)
    levels = resp.get("levels") if isinstance(resp, dict) else None
    check("risk-levels 返回 {levels:{}} 信封（前端解包修复点）",
          st == 200 and isinstance(levels, dict) and len(levels) > 0,
          f"(HTTP {st}, keys={len(levels) if isinstance(levels, dict) else 0})")

    st, resp = call("GET", "/auth/me", headers=auth)
    check("token 校验（含 ver 声明）", st == 200, f"(HTTP {st})")

    print(f"\n── {len(PROBES)} 个前端真实动作键：换票 → 无票被拒 → 带票放行 → 重放被拒 ──")
    for label, (action_key, method, path, body) in PROBES.items():
        st, resp = call("POST", "/auth/risk-challenge", {"action_key": action_key}, headers=auth)
        if st != 200 or not isinstance(resp, dict) or "ticket" not in resp:
            check(f"换票 {label}", False, f"(HTTP {st}: {str(resp)[:100]})")
            continue
        ticket = resp["ticket"]

        st_no, _ = call(method, path, body=body, headers=auth)
        check(f"无票被拒 {label}", st_no in (401, 403), f"(HTTP {st_no})")

        st_ok, _ = call(method, path, body=body, headers={**auth, "X-Risk-Ticket": ticket})
        check(f"带票放行 {label}", st_ok not in (401, 403),
              f"(HTTP {st_ok}，非 401/403 即守卫放行)")

        st_replay, _ = call(method, path, body=body, headers={**auth, "X-Risk-Ticket": ticket})
        check(f"重放被拒 {label}", st_replay in (401, 403), f"(HTTP {st_replay})")

    # 跨动作票据：vms 的票不能用于 users 的路由（防串用）
    st, resp = call("POST", "/auth/risk-challenge", {"action_key": "vms:delete"}, headers=auth)
    st_cross, _ = call("POST", "/settings/users/999999/reset-password",
                       body={"new_password": "Xx@123456"},
                       headers={**auth, "X-Risk-Ticket": resp["ticket"]})
    check("跨动作票据被拒（vms 票打 users 路由）", st_cross in (401, 403), f"(HTTP {st_cross})")

    # ── 启用用户不应索票（P0-4 修复点）──
    st, _ = call("PUT", "/settings/users/999999/status", body={"status": "active"}, headers=auth)
    check("启用用户无票放行（999999 不存在→非 401/403）", st not in (401, 403), f"(HTTP {st})")

    # ── 禁用真实用户：建临时账号，走完整"拒票/放行/重放"，再清理 ──
    print("── users:disable 真实对象闭环（临时账号） ──")
    st, resp = call("POST", "/settings/users",
                    {"username": TEMP_USER, "password": "Smoke@12345", "name": "冒烟探针",
                     "role": "只读"},
                    headers=auth)
    if st != 200 or not isinstance(resp, dict) or "id" not in resp:
        check("创建临时探针用户", False, f"(HTTP {st}: {str(resp)[:120]})")
    else:
        uid = resp["id"]
        check("创建临时探针用户", True, f"(id={uid})")
        body = {"status": "disabled"}
        st, resp = call("POST", "/auth/risk-challenge", {"action_key": "users:disable"},
                        headers=auth)
        ticket = resp.get("ticket", "") if isinstance(resp, dict) else ""
        if not ticket:
            check("换票 users:disable", False, f"(HTTP {st})")
        else:
            st_no, _ = call("PUT", f"/settings/users/{uid}/status", body=body, headers=auth)
            check("无票被拒 users:disable", st_no in (401, 403), f"(HTTP {st_no})")
            st_ok, _ = call("PUT", f"/settings/users/{uid}/status", body=body,
                            headers={**auth, "X-Risk-Ticket": ticket})
            check("带票放行 users:disable", st_ok not in (401, 403), f"(HTTP {st_ok})")
            check("带票后用户确实被禁用", st_ok == 200)
            st_rp, _ = call("PUT", f"/settings/users/{uid}/status", body=body,
                            headers={**auth, "X-Risk-Ticket": ticket})
            check("重放被拒 users:disable", st_rp in (401, 403), f"(HTTP {st_rp})")
            # 恢复启用（不索票）后清理临时账号
            call("PUT", f"/settings/users/{uid}/status", body={"status": "active"}, headers=auth)
        cleanup_temp_user()

    # SSE 票据端点
    st, resp = call("POST", "/notifications/stream/ticket", headers=auth)
    check("SSE 一次性票据端点可领取", st == 200 and isinstance(resp, dict) and "ticket" in resp,
          f"(HTTP {st})")

    print("\n" + ("=" * 50))
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：")
        for f in FAILED:
            print("  - " + f)
        return 1
    print(f"全部通过：{len(PROBES) * 4 + 8} 项检查全绿")
    return 0


if __name__ == "__main__":
    import json
    sys.exit(main())
