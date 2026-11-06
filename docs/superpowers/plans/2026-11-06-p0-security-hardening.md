# OpsPilot 六项 P0 安全加固实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 封堵用户删除越权、Grafana Auth Proxy 冒充、Kubeconfig 泄露/丢失、Compose 弱口令与自动建表、开发 Grafana 默认凭证、安装脚本泄密六项 P0。

**架构：** 高危操作采用“RBAC → 票据签发 → 业务端再次授权 → 原子消费”的双闸门。Grafana 仅信任 OpsPilot 反代来源，同机默认走内部网络，分体部署必须显式声明反代来源。Kubeconfig 与安装密钥统一进入持久化私有目录，Compose 不再提供可运行的公开默认秘密。

**技术栈：** FastAPI、SQLAlchemy、Alembic、Pytest、Docker Compose、Grafana Auth Proxy、Bash。

**实施状态（2026-11-06）：** 六项 P0 均已完成。最终方案在复核时补充了两处
可用性保护：Kubeconfig 默认目录精确对齐容器的 `/app/storage` 持久卷；同机
Grafana 通过 `opspilot-monitoring-bridge` 专用网络访问，并仅迁移旧安装器自动
生成的 `MONITORING_HOST:3000` 地址，绝不覆盖管理员手工配置。

---

## 0. 施工边界和架构图

```mermaid
flowchart LR
    User[登录用户] --> Permission[有效权限校验]
    Permission --> Challenge[签发一次性风险票据]
    Challenge --> Endpoint[业务接口权限依赖]
    Endpoint --> Consume[再次校验权限并原子消费票据]

    Browser[浏览器] --> Proxy[OpsPilot /grafana 同源反代]
    Proxy -->|固定内部网络或白名单 IP| Grafana[Grafana Auth Proxy]
    Direct[局域网直连/伪造请求头] -.拒绝.-> Grafana

    Cluster[K8s 纳管] --> PrivateDir[/app/storage/kubeconfigs 0700]
    PrivateDir --> File[*.yaml 0600]
    File --> Volume[backend-storage 持久卷]

    Installer[install.sh] --> SecretEnv[.env 0600]
    Compose[Compose] --> RequiredSecret[显式数据库/Grafana密码]
    Compose --> Alembic[alembic upgrade head]
```

### 文件职责

- `backend/app/api/auth.py`：风险票据签发前校验调用者有效权限。
- `backend/app/core/security.py`：票据消费前再次校验当前权限，覆盖权限被撤销和漏挂业务权限依赖。
- `backend/app/api/settings.py`：用户删除路由显式声明用户管理权限。
- `backend/app/api/clusters/_shared.py`：Kubeconfig 持久目录解析和旧目录迁移。
- `backend/.dockerignore`：阻止历史/新目录中的 Kubeconfig 进入镜像构建上下文。
- `scripts/run_checks.py`：秘密扫描同时识别历史和新 Kubeconfig 目录。
- `docker-compose.yml`：根应用栈强制秘密、关闭运行时自动建表、增加 Alembic 初始化。
- `deploy/monitoring/docker-compose.yml`：开发监控栈强密码、回环绑定、Auth Proxy 来源限制。
- `deploy/offline/monitoring/docker-compose.yaml`：离线/生产监控栈的来源限制和显式暴露策略。
- `deploy/offline/app/docker-compose.yaml`：同机部署时后端与监控的受控连接方式。
- `deploy/offline/install.sh`：私有文件权限、无明文密码日志、同机/分体 Grafana 参数生成。
- `deploy/offline/env.example`、`deploy/offline/README.md`、`deploy/README.md`：新变量和升级说明。
- `deploy/offline/package.sh`：离线镜像升级到已修补的 Grafana 版本。
- `backend/tests/test_risk_ticket_contract.py`：票据签发/消费授权回归。
- `backend/tests/test_e2e_api.py`：只读用户删除用户的端到端拒绝回归。
- `backend/tests/test_p0_deployment_security_contract.py`：Compose、Dockerignore、安装脚本和 Kubeconfig 静态安全契约。

### 施工纪律

- 从最新 `origin/main` 创建 `codex/p0-security-hardening`；不要在含未提交部署修改的 `C:/workspace/newtime` 直接施工。
- 每个任务独立提交，不把格式化、依赖升级或其他重构混入 P0。
- 不打印真实密码、Token、Kubeconfig 内容；失败测试只使用固定的测试字符串。
- 根 Compose 和离线 Compose 都必须通过 `docker compose config`。

---

## 任务 1：封堵用户删除越权和无权限票据签发

**文件：**
- 修改：`backend/app/api/auth.py:190-245`
- 修改：`backend/app/core/security.py:164-258`
- 修改：`backend/app/api/settings.py:955-961`
- 修改：`backend/tests/test_risk_ticket_contract.py`
- 修改：`backend/tests/test_e2e_api.py:150-210`

- [ ] **步骤 1：为签发端写失败测试**

在 `test_risk_ticket_contract.py` 增加只读用户，并验证全键和短键都不能换票：

```python
READ_ONLY = User(id=3, username="viewer", role="只读", status="active")

def test_read_only_user_cannot_issue_full_permission_ticket(self):
    with self.assertRaises(HTTPException) as caught:
        challenge(
            {"permission_key": "action.settings.users.manage", "action_id": "delete"},
            self.db,
            user=READ_ONLY,
        )
    self.assertEqual(caught.exception.status_code, 403)

def test_read_only_user_cannot_issue_short_permission_ticket(self):
    with self.assertRaises(HTTPException) as caught:
        challenge({"action_key": "users:delete"}, self.db, user=READ_ONLY)
    self.assertEqual(caught.exception.status_code, 403)
```

- [ ] **步骤 2：为消费端和删除路由写失败测试**

先由管理员签票，再把同一用户对象改成只读，验证消费时仍拒绝；另在 E2E 中使用只读账号请求删除其他用户，断言 403 且目标用户仍存在。

```python
def test_permission_revoked_after_issue_cannot_consume_ticket(self):
    ticket = self._fresh_ticket(
        {"permission_key": "action.settings.users.manage", "action_id": "delete"}
    )
    ADMIN.role = "只读"
    try:
        with self.assertRaises(HTTPException) as caught:
            call_guard(
                guard_for("action.settings.users.manage", "delete"),
                ticket,
                user=ADMIN,
            )
        self.assertEqual(caught.exception.status_code, 403)
    finally:
        ADMIN.role = "管理员"
```

- [ ] **步骤 3：运行测试确认漏洞可复现**

运行：

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_risk_ticket_contract.py tests/test_e2e_api.py -q
```

预期：新增的无权限签票、权限撤销后消费、只读删除用例失败。

- [ ] **步骤 4：实现签发端权限过滤**

在 `risk_challenge()` 解析出 `pairs` 后、创建票据前加入：

```python
permission_map = resolve_effective_permissions(current_user)
authorized_pairs = [
    (permission, action)
    for permission, action in pairs
    if permission_map.get(permission, False)
]
if not authorized_pairs:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="当前账号无权确认该高危操作",
    )
```

风险等级只从 `authorized_pairs` 计算。短键仍保存短键票据；消费端的精确权限校验负责阻止把同一个短键用于未授权路由。

- [ ] **步骤 5：实现消费端权限复核**

在 `consume_risk_ticket()` 查询并消费票据之前加入：

```python
permission_map = resolve_effective_permissions(user)
if not permission_map.get(permission_key, False):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="当前账号无权执行该高危操作",
    )
```

权限失败不得消费票据，避免管理员纠正角色后票据被无意义作废。

- [ ] **步骤 6：给删除用户路由增加显式权限依赖**

```python
_=Depends(
    require_permissions(
        "module.settings.users",
        "action.settings.users.manage",
    )
),
_risk=Depends(require_risk_ack("action.settings.users.manage", "delete")),
```

保留“不能删自己”和“不能删最后一个可用管理员”两条现有保护。

- [ ] **步骤 7：验证并提交**

预期：专项测试全部通过；只读账号在签票阶段和删除阶段均得到 403。

```powershell
git add backend/app/api/auth.py backend/app/core/security.py backend/app/api/settings.py backend/tests/test_risk_ticket_contract.py backend/tests/test_e2e_api.py
git commit -m "fix(security): enforce RBAC for risk tickets and user deletion"
```

---

## 任务 2：将 Kubeconfig 移入私有持久卷并阻止进入镜像

**文件：**
- 修改：`backend/app/api/clusters/_shared.py:1-25`
- 修改：`backend/.dockerignore`
- 修改：`.gitignore`
- 修改：`scripts/run_checks.py:45-55,312`
- 创建：`backend/tests/test_kubeconfig_storage_security.py`

- [ ] **步骤 1：写目录和忽略规则失败测试**

```python
from pathlib import Path

from app.api.clusters import _shared


def test_default_kubeconfig_dir_is_inside_persistent_storage():
    normalized = Path(_shared.KUBECONFIG_DIR).as_posix()
    expected = Path(__file__).resolve().parents[1] / "storage" / "kubeconfigs"
    assert Path(_shared.KUBECONFIG_DIR).resolve() == expected.resolve()


def test_backend_dockerignore_excludes_all_kubeconfig_locations():
    text = (Path(__file__).parents[1] / ".dockerignore").read_text(encoding="utf-8")
    assert "app/kubeconfigs" in text
    assert "app/storage/kubeconfigs" in text
```

再用临时目录测试：旧目录有 `c-old.yaml`、新目录为空时，迁移后新文件存在、模式为 `0600`，旧文件不存在；目标已存在时不得覆盖。

- [ ] **步骤 2：运行测试确认失败**

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_kubeconfig_storage_security.py -q
```

- [ ] **步骤 3：实现可配置的持久目录**

在 `_shared.py` 中把默认路径改为后端根目录下的 `storage/kubeconfigs`，并允许生产显式覆盖：

```python
_APP_DIR = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _APP_DIR.parent
_LEGACY_KUBECONFIG_DIR = _APP_DIR / "kubeconfigs"
KUBECONFIG_DIR = str(
    Path(
        os.environ.get(
            "OPSPILOT_KUBECONFIG_DIR",
            str(_BACKEND_DIR / "storage" / "kubeconfigs"),
        )
    )
)
```

新增 `migrate_legacy_kubeconfigs(legacy_dir, target_dir)`：

- 只处理文件名匹配 `c-*.yaml` 的普通文件。
- 目标目录先用 `ensure_private_directory()` 设置 `0700`。
- 目标文件不存在时用 `write_sensitive_text_file()` 写入并设置 `0600`，成功后删除旧文件。
- 目标已存在时保留目标，旧文件不删除并记录 warning，避免覆盖当前凭据。
- 迁移日志只能出现文件名，不能出现内容。

- [ ] **步骤 4：补齐构建忽略和秘密扫描**

`backend/.dockerignore` 加入：

```text
app/kubeconfigs
app/storage/kubeconfigs
**/kubeconfigs
```

根 `.gitignore` 增加 `backend/storage/kubeconfigs/`。`scripts/run_checks.py` 的已知秘密目录同时包含历史目录和新目录。

- [ ] **步骤 5：验证镜像上下文和持久卷路径**

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_kubeconfig_storage_security.py tests/test_cluster_client_cache.py tests/test_cluster_health_summary_contract.py -q
cd ..
docker compose config
```

确认根 Compose 和离线 Compose 都把 `/app/storage` 挂到 `backend-storage`，无需新增第二个存储卷。

- [ ] **步骤 6：提交**

```powershell
git add backend/app/api/clusters/_shared.py backend/.dockerignore .gitignore scripts/run_checks.py backend/tests/test_kubeconfig_storage_security.py
git commit -m "fix(security): persist and exclude kubeconfig files"
```

---

## 任务 3：根 Compose 移除弱口令并改用 Alembic 初始化

**文件：**
- 修改：`docker-compose.yml`
- 创建：`.env.example`
- 修改：`README.md`
- 创建：`backend/tests/test_p0_deployment_security_contract.py`

- [ ] **步骤 1：写根 Compose 安全契约失败测试**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_root_compose_has_no_default_database_password():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "change-this-db-password" not in text
    assert "${POSTGRES_PASSWORD:?" in text


def test_root_compose_disables_runtime_schema_creation():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "DB_AUTO_CREATE_TABLES: '1'" not in text
    assert "DB_AUTO_CREATE_TABLES: '0'" in text
    assert "alembic" in text and "upgrade" in text and "head" in text
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_p0_deployment_security_contract.py -q
```

- [ ] **步骤 3：修改根 Compose**

数据库和后端连接串统一改用必填变量：

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
DATABASE_URL: postgresql+psycopg://opspilot:${POSTGRES_PASSWORD}@db:5432/opspilot
DB_AUTO_CREATE_TABLES: '0'
```

增加一次性 `init-db` 服务：使用与 backend 相同的 build、等待数据库健康、执行 `alembic upgrade head`、`restart: "no"`。backend 必须等待 `init-db` 的 `service_completed_successfully`，不允许应用进程自行 `create_all`。

- [ ] **步骤 4：增加根环境样例和启动文档**

`.env.example` 只放空值或明显不可运行的说明值：

```dotenv
POSTGRES_PASSWORD=
GRAFANA_ADMIN_PASSWORD=
GRAFANA_AUTH_PROXY_WHITELIST=172.30.250.0/24
```

README 启动命令改为先复制 `.env.example` 到 `.env`，再用 `openssl rand -hex 32` 生成密码。

- [ ] **步骤 5：验证全新数据库迁移**

使用隔离项目名，避免触碰开发数据库：

```powershell
$env:POSTGRES_PASSWORD="p0-test-only-7f57d2a93b8e4c4d"
docker compose -p opspilot-p0-check config
docker compose -p opspilot-p0-check up -d db init-db
docker compose -p opspilot-p0-check logs --no-color init-db
docker compose -p opspilot-p0-check down -v
```

预期：配置渲染成功；init-db 退出码为 0；日志包含 Alembic 升级到 head；不包含任何密码。

- [ ] **步骤 6：提交**

```powershell
git add docker-compose.yml .env.example README.md backend/tests/test_p0_deployment_security_contract.py
git commit -m "fix(deploy): require database secret and run migrations"
```

---

## 任务 4：封闭 Grafana Auth Proxy 旁路和开发默认凭证

**文件：**
- 修改：`deploy/monitoring/docker-compose.yml`
- 创建：`deploy/monitoring/.env.example`
- 修改：`deploy/offline/monitoring/docker-compose.yaml`
- 修改：`deploy/offline/app/docker-compose.yaml`
- 修改：`deploy/offline/env.example`
- 修改：`deploy/offline/install.sh`
- 修改：`deploy/offline/package.sh`
- 修改：`deploy/offline/README.md`
- 修改：`deploy/README.md`
- 修改：`backend/tests/test_p0_deployment_security_contract.py`

- [ ] **步骤 1：写 Grafana 配置失败测试**

测试必须覆盖开发和离线两份 Compose：

```python
def test_all_grafana_auth_proxy_stacks_have_source_whitelist():
    for relative in (
        "deploy/monitoring/docker-compose.yml",
        "deploy/offline/monitoring/docker-compose.yaml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "GF_AUTH_PROXY_WHITELIST" in text
        assert "GRAFANA_ADMIN_PASSWORD" in text
        assert "3000:3000" not in text


def test_grafana_uses_security_patched_image():
    for relative in (
        "deploy/monitoring/docker-compose.yml",
        "deploy/offline/monitoring/docker-compose.yaml",
        "deploy/offline/package.sh",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "11.0.0" not in text
        assert "11.6.14-security-04" in text
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_p0_deployment_security_contract.py -q
```

- [ ] **步骤 3：升级 Grafana 修补版本**

将所有 `grafana/grafana:11.0.0` 统一改为 `grafana/grafana:11.6.14-security-04`，包括开发 Compose、离线 Compose、安装脚本默认值、离线打包脚本和文档。该版本包含 Auth Proxy 白名单相关安全修复；Docker 标签用短横线表示上游版本号中的 `+security-04`。

- [ ] **步骤 4：加 Auth Proxy 来源白名单**

两份 Grafana 环境均增加：

```yaml
GF_AUTH_PROXY_WHITELIST: ${GRAFANA_AUTH_PROXY_WHITELIST:?set trusted OpsPilot proxy IP/CIDR}
GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD}
```

不得用 `0.0.0.0/0`、局域网整段或空字符串作为白名单。

- [ ] **步骤 5：定义同机与分体两种网络模式**

同机默认模式：

- Grafana 端口只绑定 `127.0.0.1`，或完全取消宿主机端口。
- OpsPilot backend 和 Grafana 加入专用 Docker 网络 `opspilot-monitoring`。
- `GRAFANA_URL=http://grafana:3000`。
- 白名单只允许该专用网络 CIDR，例如 `172.30.250.0/24`。

分体模式：

- 使用独立变量 `GRAFANA_BIND=<监控机私网IP>`，不能复用控制 VM/VLogs 的 `MONITOR_BIND`。
- `GRAFANA_AUTH_PROXY_WHITELIST=<OpsPilot后端私网IP>/32`。
- 文档要求主机防火墙只允许 OpsPilot 后端 IP 访问 3000。
- 浏览器仍只访问 OpsPilot `/grafana/*`，不展示或跳转裸端口。

开发 Compose 的默认端口必须是：

```yaml
ports:
  - "${GRAFANA_BIND:-127.0.0.1}:${GRAFANA_PORT:-3000}:3000"
```

- [ ] **步骤 6：验证伪造请求头被拒绝**

启动测试栈后执行两条请求：

```bash
curl -i -H 'X-WEBAUTH-USER: forged-admin' http://127.0.0.1:3000/api/user
curl -i http://127.0.0.1:8001/grafana/api/user -H "Cookie: opspilot_grafana_session=<测试会话>"
```

验收：第一条不返回伪造用户，更不能返回管理员权限；第二条经合法 OpsPilot 反代成功。测试记录只保存状态码和用户名，不保存 Cookie。

- [ ] **步骤 7：验证 Compose 并提交**

```powershell
$env:GRAFANA_ADMIN_PASSWORD="p0-test-grafana-4e0cbb204102"
$env:GRAFANA_AUTH_PROXY_WHITELIST="127.0.0.1/32"
docker compose -f deploy/monitoring/docker-compose.yml config
docker compose -f deploy/offline/monitoring/docker-compose.yaml --env-file deploy/offline/env.example config
```

```powershell
git add deploy/monitoring deploy/offline/monitoring deploy/offline/app/docker-compose.yaml deploy/offline/env.example deploy/offline/install.sh deploy/offline/package.sh deploy/offline/README.md deploy/README.md backend/tests/test_p0_deployment_security_contract.py
git commit -m "fix(security): isolate Grafana auth proxy"
```

---

## 任务 5：安装脚本私有化配置并移除密码输出

**文件：**
- 修改：`deploy/offline/install.sh`
- 修改：`deploy/offline/README.md`
- 修改：`backend/tests/test_p0_deployment_security_contract.py`

- [ ] **步骤 1：写安装脚本安全契约失败测试**

```python
def test_installer_never_prints_admin_password_and_protects_env():
    text = (ROOT / "deploy/offline/install.sh").read_text(encoding="utf-8")
    assert "umask 077" in text
    assert 'chmod 600 "$ENV_FILE"' in text
    assert 'chmod 600 "$dest"' in text
    assert "admin / $admin_pass" not in text
    assert "${INIT_ADMIN_USERNAME} / ${INIT_ADMIN_PASSWORD}" not in text
```

- [ ] **步骤 2：运行测试确认失败**

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_p0_deployment_security_contract.py -q
```

- [ ] **步骤 3：设置进程级私有 umask**

在 `set -euo pipefail` 后立即加：

```bash
umask 077
```

这保证脚本创建的 `.env`、临时 `.lf` 文件和 backend `.env` 从创建时就是私有权限，避免先 0644 后 chmod 的竞态窗口。

- [ ] **步骤 4：每次替换后显式恢复 0600**

以下路径都必须在写入或 `mv` 后执行 `chmod 600`：

- `$ENV_FILE`
- `$ENV_FILE.lf` 移动后的目标
- `write_backend_env()` 的 `$dest`
- `--config` 复制生成的 `$ENV_FILE`
- `HOST_NAME` 追加之后的 `$ENV_FILE`

`chmod` 失败必须让安装脚本退出，不允许 best-effort。

- [ ] **步骤 5：删除两处明文密码日志**

替换为：

```bash
log "管理员账号: ${INIT_ADMIN_USERNAME:-admin}"
log "初始管理员密码已写入受限配置文件（0600）: $ENV_FILE"
```

日志中不得包含数据库密码、管理员密码、Grafana 密码、`SECRET_KEY` 或加密密钥。

- [ ] **步骤 6：执行 Shell 和权限验收**

```bash
bash -n deploy/offline/install.sh
tmp_dir="$(mktemp -d)"
cp deploy/offline/install.sh "$tmp_dir/install.sh"
grep -nE 'INIT_ADMIN_PASSWORD.*[/ ]|admin /|POSTGRES_PASSWORD.*log' deploy/offline/install.sh
```

验收：`bash -n` 退出 0；grep 不得命中日志语句。完整安装演练后：

```bash
stat -c '%a %n' deploy/offline/.env deploy/offline/app/.env
```

两个文件都必须显示 `600`。

- [ ] **步骤 7：提交**

```powershell
git add deploy/offline/install.sh deploy/offline/README.md backend/tests/test_p0_deployment_security_contract.py
git commit -m "fix(installer): protect generated secrets and redact output"
```

---

## 任务 6：全量验证、攻击路径复测和交付

**文件：**
- 修改：`开发文档记录/页面开发记录/63-P0安全加固.md`

- [ ] **步骤 1：运行后端专项测试**

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_risk_ticket_contract.py tests/test_e2e_api.py tests/test_kubeconfig_storage_security.py tests/test_p0_deployment_security_contract.py tests/test_grafana_sso_contract.py -q
```

预期：全部通过。

- [ ] **步骤 2：运行统一全量检查**

```powershell
cd ..
.venv/Scripts/python.exe scripts/run_checks.py --secret-scan
pnpm --dir frontend test
```

预期：pytest、pyflakes、ESLint、前端 build、秘密扫描、Vitest 全部通过；秘密扫描不得在镜像上下文发现 Kubeconfig。

- [ ] **步骤 3：验证四份 Compose 场景**

验证根应用、开发监控、离线应用、离线监控：

```powershell
$env:POSTGRES_PASSWORD="p0-compose-test-fae28fb9"
$env:GRAFANA_ADMIN_PASSWORD="p0-grafana-test-4ef9348b"
$env:GRAFANA_AUTH_PROXY_WHITELIST="127.0.0.1/32"
docker compose config
docker compose -f deploy/monitoring/docker-compose.yml config
docker compose -f deploy/offline/app/docker-compose.yaml --env-file deploy/offline/env.example config
docker compose -f deploy/offline/monitoring/docker-compose.yaml --env-file deploy/offline/env.example config
```

再清除 `POSTGRES_PASSWORD` 和 `GRAFANA_ADMIN_PASSWORD`，确认对应 Compose 明确失败，不能静默回退默认密码。

- [ ] **步骤 4：逐条复测六条攻击路径**

1. 只读用户请求 `users:delete` 风险票据：403。
2. 使用历史已签票据、撤销权限后删除用户：403，目标用户保留。
3. 从白名单外 IP 伪造 `X-WEBAUTH-USER`：拒绝。
4. 重建 backend 容器：已纳管集群仍存在并可刷新。
5. 在存在真实 Kubeconfig 的开发树构建 backend 镜像：镜像内 `/app/app/kubeconfigs` 无历史文件，运行时文件只存在于 `/app/storage/kubeconfigs` 持久卷。
6. 安装日志无明文秘密，生成的两份 `.env` 均为 0600。

- [ ] **步骤 5：记录结果**

`63-P0安全加固.md` 必须包含：漏洞根因、修复边界、架构图、迁移说明、测试命令和实际结果。不得记录真实 IP、密码、Cookie、Token、Kubeconfig 内容。

- [ ] **步骤 6：最终提交**

```powershell
git add 开发文档记录/页面开发记录/63-P0安全加固.md
git commit -m "docs(security): record P0 hardening verification"
```

---

## 审批门槛

改完后提交以下材料给审批人：

1. 分支名和 6 个独立提交 SHA。
2. `git diff --stat origin/main...HEAD`。
3. 五组专项测试和全量检查的原始退出码及摘要。
4. 四份 `docker compose config` 的成功结果，以及缺失秘密时的失败结果。
5. 六条攻击路径的复测记录，敏感值必须脱敏。
6. `git diff --check` 输出。

审批人重点检查：

- 所有高危票据在签发和消费时都验证实时权限。
- 短键票据不能跨到未授权的同别名路由。
- Grafana 白名单不能是空值、`0.0.0.0/0` 或整个办公网段。
- 分体部署没有重新引入浏览器直连 Grafana 的链接。
- 旧 Kubeconfig 迁移不覆盖新文件，失败时保留旧文件。
- Compose 和安装脚本没有任何可运行的默认密码。
- 用户原有未提交部署改动没有被覆盖或混入安全提交。
