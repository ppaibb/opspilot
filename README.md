# OpsPilot

企业级一站式运维管理平台。覆盖资产管理、K8s 集群运维、监控告警、费用管理、数据库 SQL 工单、AI 智能运维与虚拟组网，面向中小规模运维团队的日常操作与审计闭环。

> 状态：功能主体全部落地，处于「可用 + 持续打磨」阶段（更新于 2026-10-09）。
> 后端 pytest **752** 例 + 前端 vitest **52** 例，`检查-全量.bat` 4/4 全绿。

---

## 页面速览

以下截图为真实环境（235 部署实例）实拍。

### 登录与工作台

<img src="docs/screenshots/01-login.png" alt="登录页" width="48%"> <img src="docs/screenshots/02-dashboard.png" alt="仪表盘" width="48%">

### 资产管理

<img src="docs/screenshots/03-hosts.png" alt="主机管理" width="48%"> <img src="docs/screenshots/04-vms.png" alt="虚拟机管理" width="48%">
<img src="docs/screenshots/05-clusters.png" alt="集群管理" width="48%"> <img src="docs/screenshots/06-cluster-detail.png" alt="集群详情七 Tab" width="48%">
<img src="docs/screenshots/07-credentials.png" alt="凭证管理" width="48%"> <img src="docs/screenshots/08-domains.png" alt="域名管理" width="48%">
<img src="docs/screenshots/09-certificates.png" alt="证书管理" width="48%"> <img src="docs/screenshots/10-cmdb.png" alt="CMDB 关联图谱" width="48%">
<img src="docs/screenshots/11-wireguard.png" alt="WireGuard 虚拟组网" width="48%"> <img src="docs/screenshots/12-databases.png" alt="数据库管理工作台" width="48%">

### 应用与 AI

<img src="docs/screenshots/13-apps.png" alt="应用列表" width="48%"> <img src="docs/screenshots/14-ai-workbench.png" alt="AI Agent 工作台" width="48%">

### 监控中心

<img src="docs/screenshots/15-monitoring-live.png" alt="实时看板" width="48%"> <img src="docs/screenshots/16-metrics.png" alt="PromQL 指标查询" width="48%">
<img src="docs/screenshots/17-logs.png" alt="LogsQL 日志查询" width="48%"> <img src="docs/screenshots/18-alerts.png" alt="告警中心" width="48%">
<img src="docs/screenshots/19-alert-rules.png" alt="告警规则" width="48%">

### 费用 / 运维工具 / 系统设置

<img src="docs/screenshots/20-finance.png" alt="费用总览" width="48%"> <img src="docs/screenshots/21-script.png" alt="Ansible 脚本执行" width="48%">
<img src="docs/screenshots/22-users.png" alt="用户管理" width="48%"> <img src="docs/screenshots/23-permissions.png" alt="权限管理" width="48%">
<img src="docs/screenshots/24-audit.png" alt="审计日志" width="48%"> <img src="docs/screenshots/25-notifications.png" alt="通知中心" width="48%">

---

## 系统架构

```mermaid
flowchart LR
    Browser["浏览器"] --> FE["前端<br/>nginx 反代 + React SPA"]
    FE -->|"/api/*"| BE["FastAPI 后端<br/>JWT · RBAC · 高危票据 · 审计"]
    FE -->|"/grafana/* 同源反代"| BE
    BE --> DB[("PostgreSQL 16")]
    BE -->|注入 X-WEBAUTH 身份头| GF["Grafana<br/>auth.proxy 免密免登"]
    BE -->|PromQL / LogsQL 代理| VM["VictoriaMetrics"]
    BE -->|LogsQL 代理| VL["VictoriaLogs"]
    GF -->|数据源| VM
    AG["Alloy 采集器<br/>host 指标 + 容器日志"] --> VM
    AG --> VL
    BE -->|账单同步| Ali["阿里云 BSS"]
    BE -->|通知 fanout| IM["飞书 / 站内信"]
    Browser -->|"WebSSH / K8s WebSocket"| BE
```

- **前端从不直连** VictoriaMetrics / VictoriaLogs / Grafana，所有查询走后端代理；
- Grafana 以**同源反代**方式接入（`/grafana/*`），浏览器无需可达监控节点。

### Grafana 免密跳转（SSO）

```mermaid
sequenceDiagram
    participant U as 浏览器
    participant P as OpsPilot 前端
    participant B as 后端 /grafana 反代
    participant G as Grafana
    U->>P: 点击「Grafana 更多数据」
    P->>B: GET /api/monitoring/grafana/sso-link
    B-->>P: 90 秒一次性票据（只放地址栏）
    U->>B: GET /grafana/explore?opspilot_sso=...
    B->>G: 剥离票据，注入 X-WEBAUTH-USER/NAME/EMAIL/ROLE
    G-->>B: 200 登录成功（管理员→Admin 其余→Viewer）
    B-->>U: HTML + 8 小时 HttpOnly 会话 Cookie
    U->>B: 后续请求仅带 Cookie
    B->>G: 每次回库重取身份并重新注入
```

### 高危操作一次性票据

```mermaid
sequenceDiagram
    participant U as 用户
    participant F as 前端 useRiskConfirm
    participant B as 后端 require_risk_ack
    U->>F: 点击高危操作（如删除集群）
    F->>F: 二次确认弹窗
    F->>B: POST /api/auth/risk-challenge
    B-->>F: 一次性票据（120s TTL，绑定用户+动作）
    F->>B: 业务请求 + X-Risk-Ticket 头
    B->>B: 原子消费票据（并发复用同一张只成功一次）
    B-->>U: 执行成功，审计按登记风险等级落库
```

---

## 功能矩阵

| 模块 | 完成度 | 说明 |
|---|---|---|
| 登录 / 权限 / 审计 | ✅ 完整 | JWT + RBAC 权限矩阵 + 高危票据流 + 审计留痕 + 内置角色（含数据库管理员） |
| 资产管理 | ✅ 完整 | 主机 / 虚机 / 凭证 / 域名 / 证书 / 集群 / CMDB 图谱 / WireGuard / 数据库 |
| K8s 集群 | ✅ 完整 | 七 Tab 详情 + YAML 工作台 + Pod 终端/日志 + SSE Watch + 发布回滚 |
| 监控中心 | ✅ 完整 | PromQL 指标查询 / LogsQL 日志 + Live Tail / 告警规则与认领闭环 / Grafana SSO |
| 应用管理 | ✅ 完整 | 应用 CRUD + 资源绑定 + 部署异步化 + 配置中心回滚 |
| 费用管理 | ✅ 完整 | 阿里云 BSS 账单同步 + 费用总览 + 预算告警 + 主机费用联动 |
| 数据库管理 | ✅ 完整 | Yearning 风格 SQL 工单（三权分立/多级审批/定时执行/备份回滚）+ 自助查询 |
| AI 能力 | ✅ 完整 | Agent 工作台（SSE 流式）+ 模型配置 + 技能管理 + 全景看板 + 根因沉淀 |
| 运维工具 | ✅ 完整 | Ansible 脚本执行 + 变更工单 + 定时任务 + 证书自动续签 |
| IM 通知 | ✅ 完整 | 站内通知 + SSE 推送 + 飞书群 @ + 个人 IM 渠道绑定 |

**已知遗留（低优先级）**

- 仪表盘「资产总览」对无资产权限用户仍可见（overview 接口语义待定）

---

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 19 · TypeScript · Vite 8 · Ant Design 5 · Pro Components · React Query 5 · Zustand · ECharts |
| 后端 | Python · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL 16 · Paramiko（SSH）· kubernetes |
| 终端 | @wterm/react（WebSSH + SFTP） |
| 集成 | 阿里云 ECS/BSS · 腾讯云 · VMware · ACME 证书 · WireGuard · 飞书 IM |
| 可观测 | VictoriaMetrics · VictoriaLogs · Grafana · Alloy |
| 部署 | Docker Compose · 离线安装包 · 一键增量发版 |

> ⚠️ Ant Design 锁定 **5.x**，不要升级到 6.x（Pro Components 不兼容）。

---

## 目录结构

```
newtime/
├── backend/                # FastAPI 后端
│   ├── app/
│   │   ├── api/            # 路由（clusters/ 已按域拆包）
│   │   ├── core/           # 配置、安全、RBAC（角色预设/风险等级）
│   │   ├── db/             # 模型 + 会话 + 启动补列
│   │   ├── services/       # SSH / 飞书 / 租约 / 调度 / 采集配置渲染
│   │   └── main.py         # 入口（含 ensure_model_columns 启动补列）
│   ├── alembic/            # 数据库迁移
│   └── tests/              # pytest 752 例（契约测试为主）
├── frontend/               # React 前端
│   └── src/
│       ├── api/            # axios 封装 + 按模块 API
│       ├── pages/          # 页面目录（assets/monitoring/ai/apps/...）
│       ├── router/routes.ts# 路由单一数据源（40 条，含权限键）
│       ├── hooks/          # useCrudResource / usePermission / useRiskConfirm
│       └── store/          # Zustand（登录态 / 通知）
├── deploy/                 # 部署手册 + 离线安装 + 增量发版
├── docs/screenshots/       # README 页面截图（25 张实拍）
├── scripts/                # run_checks / 冒烟 / 迁移校验
├── 开发文档记录/            # 开发计划 + 页面开发记录 00–56 + 测试清单
├── 检查-全量.bat            # 统一验证入口（pytest + pyflakes + lint + build）
├── 启动-后端.bat / 启动-前端.bat / 启动-全部.bat
└── 部署-更新.bat            # 一键增量发版
```

---

## 快速开始

### 环境要求

- Python 3.11+（backend 虚拟环境）
- Node.js 20+ / pnpm 10
- PostgreSQL 16（或直接用 Docker Compose 起库）

### 开发启动

```bat
:: 1) 后端（首次先建 venv）
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd ..
启动-后端.bat          :: http://127.0.0.1:8001

:: 2) 前端（首次自动 pnpm install）
启动-前端.bat          :: Vite 开发服务器，/api 与 /grafana 代理到 8001
```

也可以 `启动-全部.bat` 一键双开。Swagger 文档：`http://127.0.0.1:8001/docs`。

### Docker Compose

```bash
docker compose up -d
# 前端 http://localhost:8080  后端 http://localhost:8001
```

### 校验

改完代码务必跑统一验证入口：

```bat
检查-全量.bat                 # pytest + pyflakes + eslint + build
检查-全量.bat --secret-scan   # 附加秘密扫描
```

---

## 核心约定（开发须知）

- **认证**：JWT Bearer，Zustand 持久化；401 自动跳登录（`/auth/login` 本身除外）
- **权限**：`usePermission().has('action.xxx')` 控制显隐（无权限入口直接隐藏而非置灰）；路由权限键集中在 `router/routes.ts`
- **高危操作**：`useRiskConfirm` → `POST /auth/risk-challenge` 取一次性票据 → `X-Risk-Ticket` 原子消费；新高危路由必须挂 `require_risk_ack`
- **数据请求**：列表/详情 `useQuery`，变更 `useMutation`；JSON 一律走 `api/client.ts`，SSE 走 `lib/fetch.ts`，禁止裸 `fetch`
- **展示文案**：不写「点右上角 X」式界面教学句；保留系统状态、失败原因、安全警告、业务规则
- **页面模板**：`frontend/src/pages/assets/hosts`（Table + Drawer + Popconfirm + useCrudResource）
- **时间**：外部存储参数一律 UTC RFC3339；数据库存本地时间字符串
- **SSH**：统一走 `services/ssh_client.py` 的 `pin_and_connect`（TOFU 指纹钉扎）
- **后台任务**：先拿 `services/leases.py` 数据库租约再执行

完整上下文见 [AGENTS.md](AGENTS.md)。

---

## 部署

两种场景，详见 [deploy/README.md](deploy/README.md)：

```bat
:: 增量更新（已有环境）
部署-更新.bat root@10.10.0.9

:: 离线全新安装（空机）
cd deploy/offline && ./package.sh 1.0.0     # 开发机打包
sudo ./install.sh                            # 目标机安装（--config 支持非交互）
```

安装产物：应用栈（PG + backend + frontend）+ 可选监控栈（VM + VLogs + Grafana + Alloy）。
新环境零配置项（`grafana_url` 等由环境变量自动种库）；Grafana 的 VictoriaLogs
数据源插件需手动安装一次，见 [deploy/README.md](deploy/README.md) 第六节。

---

## 文档

| 文档 | 内容 |
|---|---|
| [开发文档记录/开发计划.md](开发文档记录/开发计划.md) | 全量开发进度与技术决策 |
| [开发文档记录/frontend-pages.md](开发文档记录/frontend-pages.md) | 页面功能清单 + API 接口 |
| [开发文档记录/页面开发记录/](开发文档记录/页面开发记录/) | 00–56 每轮迭代的详细记录 |
| [开发文档记录/全量功能测试清单.md](开发文档记录/全量功能测试清单.md) | 端到端测试清单 |
| [开发文档记录/部署说明.md](开发文档记录/部署说明.md) | 部署细节 |
| [AGENTS.md](AGENTS.md) | AI 助手项目上下文与约定 |
