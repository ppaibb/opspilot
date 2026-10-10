# OpsPilot 前端重构 — Agent 上下文

> 本文件供 AI 编程助手（GitHub Copilot、Cursor、Claude Code 等）读取，
> 用于建立项目上下文。请在每次新会话开始时参考本文件。

---

## 项目概述

**项目名称**：OpsPilot 前端重构  
**项目路径**：`c:\workspace\newtime\frontend`  
**性质**：企业级运维管理平台，约 30 个页面，6 大模块  
**后端 API**：`http://127.0.0.1:8001`（开发代理目标见 `frontend/vite.config.ts`）  
**Swagger**：`http://127.0.0.1:8001/openapi.json`  
**页面清单**：`开发文档记录/frontend-pages.md`  
**开发计划**：`开发文档记录/开发计划.md`  
**页面记录**：`开发文档记录/页面开发记录/`（每个页面一个文档）

---

## 技术栈（已锁定，不要改动）

| 层 | 选型 | 版本 |
|---|---|---|
| 框架 | React | 19 |
| 类型 | TypeScript | 6 |
| 构建 | Vite（Rolldown） | 8 |
| UI 组件 | **Ant Design 5**（锁定 5.x，不要升级到 6.x） | 5.29 |
| 高级组件 | @ant-design/pro-components | 2.8 |
| 路由 | react-router-dom | 7 |
| 服务端状态 | @tanstack/react-query | 5 |
| HTTP 客户端 | axios | 1 |
| 全局状态 | zustand（持久化） | 5 |
| 图表 | echarts + echarts-for-react | 6 |
| WebSSH 终端 | @wterm/react（vercel-labs/wterm） | 待安装 |
| 编排画布 | @xyflow/react（React Flow） | 待安装 |
| YAML 编辑 | monaco-editor | 待安装 |
| 包管理 | pnpm | 10 |
| 前端测试 | vitest + @testing-library/react | 3.x |

> ⚠️ antd **必须保持 5.x**，Pro Components 不支持 6.x。

---

## 目录结构

```
frontend/src/
├── main.tsx                    # 入口
├── App.tsx                     # QueryClient + ConfigProvider（antd 中文 + 主题）+ Router
├── index.css                   # 全局样式（仅基础重置）
├── types/api.ts                # 手写 TS 接口类型
├── api/
│   ├── client.ts               # axios 实例（baseURL:/api, Bearer, 401 白名单跳转）
│   └── index.ts                # 按模块的 API 函数
├── store/auth.ts               # Zustand（token/user 持久化，key:opspilot-auth）
├── hooks/usePermission.ts      # usePermission + useRiskConfirm
├── hooks/useCrudResource.ts    # 标准 CRUD save/delete 封装
├── components/layout/
│   └── MainLayout.tsx          # 侧边栏 + 顶栏 + 用户下拉
├── router/index.tsx            # 路由 + AuthGuard + GuestGuard
└── pages/
    ├── login/                  # ✅ 完成
    ├── dashboard/              # ✅ 骨架
    └── assets/hosts/           # ✅ 完整 CRUD（作为其他页面的模板）
    └── ...（全部 27 个页面已实现，无占位路由）
```

### 后端集群 API（2026-09-23 起按域拆分）

```
backend/app/api/clusters/       # 原 clusters.py（5920 行）
├── __init__.py                 # router/ws_router + re-export + mock.patch 兼容 shim
├── _router.py                  # 共享 APIRouter
├── _schemas.py                 # Pydantic 模型
├── _shared.py                  # 客户端缓存 / YAML / 诊断
├── ws_session.py               # K8s WS 一次性会话
├── cluster_mgmt.py / nodes.py / workloads.py
├── resources.py / namespaces.py / pods.py
```

---

## 核心架构约定

### 认证
- `POST /api/auth/login` → 返回 `{ access_token, token_type, user }`
- Token 存入 Zustand，axios 拦截器自动注入 `Authorization: Bearer <token>`
- 401 响应自动跳转 `/login`，但 `/auth/login` 本身的 401 排除在外（密码错误）

### 权限
- 后端 `GET /api/settings/rbac/metadata` 返回权限矩阵
- `usePermission().has('action.assets.hosts.delete')` 控制按钮显示/禁用
- `useRiskConfirm().confirm('assets:delete', callback)` 处理高危操作二次确认
- 高危等级来自 `GET /api/auth/risk-levels`

### 高危操作票据流（2026-09-22 起强制）
- 前端 `useRiskConfirm` 对 high 等级先弹确认框，确认后才调 `POST /api/auth/risk-challenge`
  取一次性票据（120s TTL，绑定用户+动作），票据放在 15s TTL 槽位里，由 axios 拦截器
  附加到**下一个**请求的 `X-Risk-Ticket` 头上，用完即弃；多请求场景用 `fetchRiskTicket`
  显式带 `X-Risk-Ticket` 头，不要依赖槽位
- 后端 `require_risk_ack` 强制校验并**原子消费**票据（并发复用同一张票只成功一次）；
  无条件 `X-Risk-Ack: true` 头已删除
- **新写高危路由必须挂 `require_risk_ack`**，不要自行发明确认头、不要直通放行；
  前端 actionKey 与后端 action 实参必须逐字符一致——权威键表和端到端冒烟脚本
  （`scripts/smoke_ticket_e2e.py`，34 键×4 步 144 项检查）见
  `开发文档记录/页面开发记录/37-安全与可靠性加固.md`
- 用户启用/禁用是同一路由但只有**禁用**索票（action `disable`），启用不索票——
  挂着固定 `require_risk_ack` 会让启用误索 disable 票

### 通知 / SSE 传输安全
- SSE 连接一律走**一次性短票据**（60s）：先用 JWT 调接口换短票，再拿短票连 SSE；
  **禁止把长期 JWT 拼进 URL**（会进浏览器历史/代理/访问日志）。参考 `/api/notifications` 换票流

### Grafana 免密跳转（SSO）
- 入口是**后端同源反代** `app/api/grafana_sso.py` 的 `/grafana/*`（catch-all 全方法），
  Grafana 侧开 `auth.proxy`，后端注入 `X-WEBAUTH-USER/NAME/EMAIL/ROLE` 头。
  **不要让浏览器直连 Grafana 地址**——`grafana_url` 只存后端 SystemConfig，
  且监控节点常与 OpsPilot 分离部署，浏览器未必可达
- 链路：`GET /api/monitoring/grafana/sso-link` 签 **90 秒** JWT（只放地址栏）→
  反代校验后注入身份头 + 种 **8 小时 HttpOnly Cookie**（`opspilot_grafana_session`）→
  后续请求**每次回库重取身份并重新注入**，不依赖 Grafana 自己的会话 Cookie
- 会话 Cookie 载荷只放 `uid + token_version`，校验时比对 `token_version`——
  复用「改密/禁用作废全部会话」机制，**不要另造 token 黑名单**
- Grafana 登录名统一加 `opspilot_` 前缀（`_build_grafana_login`）：
  原样写用户名会让叫 `admin` 的 OpsPilot 用户撞上 Grafana 内置管理员并把它转成外部用户
- 静态资源白名单 `public/`、`avatar/`、`favicon.ico`、`robots.txt` 必须匿名放行——
  Grafana index.html 用相对地址，这些请求不可能带票据，拦了就是首屏白屏
- 无票无 Cookie 的非静态请求一律 401，**不要原样转发**（否则反代变开放代理）
- 角色映射：`管理员`→Grafana Admin、其余→Viewer，走 `normalize_role_name()` 归一化
- Grafana provisioning **不做 `${VAR}` 环境变量插值**，写错会被静默忽略、库里留旧值，
  数据源文件里只能写静态 URL
- `GF_SERVER_ROOT_URL` 必须用**相对**子路径（`/grafana`）：写绝对地址会把
  容器内/开发机地址泄漏进 HTML 的 `appUrl`，远程浏览器被带去它自己的 localhost
- 保留 `auth-validate` 接口给边缘 Nginx `auth_request` 方案，两个入口共用同一套 Token 与身份映射

### 新环境部署（Docker/离线包）
- **CN 环境新机必配 Docker registry 镜像加速**：直连 `registry-1.docker.io` 超时，
  `/etc/docker/daemon.json` 配 `registry-mirrors`（daocloud/1ms/dockerproxy）；
  apt 也先换阿里云源再装 `docker.io` + `docker-compose-v2`
- **`deploy/offline/images/` 里的镜像是快照**，不含最新代码——别拿旧镜像部署还以为
  带新功能；有网新机直接源码 `docker build`（Dockerfile 自带清华 pip 源）
- **非交互安装**：`bash install.sh --config env.conf`，字段模板见
  `页面开发记录/50-新环境部署演练235.md`；init 容器做 `create_all + alembic stamp head`
- **alloy 配置坑**：`loki.source.docker` 的 `targets` 是**必填属性**（v1.x 强校验），
  必须接 `discovery.docker` 的输出，缺失启动即崩无限重启；`stage.template` 的属性名是
  `template` 不是 `value`；bind-mount 的配置文件更新后 `docker compose up -d` 不会重建
  容器，必须 `docker restart`。改完跑 `docker logs opspilot-alloy` 确认没有 loader 报错。
  采集配置共三份，改一处必须三处同步：offline 部署包 / monitoring 部署包 /
  后端 `render_alloy_config()` 渲染器（agent 远程模块模式由后端下发，改渲染器即生效）
- **容器日志标签约定**（日志页 主机/服务/任务/级别 四个下拉的数据来源）：
  `host`/`instance` 来自 compose 注入的 `HOST_NAME`/`HOST_INSTANCE`
  （install.sh 已持久化进 .env，手工重建容器不会丢）；`service` ← 容器名
  （relabel `__meta_docker_container_name`）；`level` ← 解析日志行（默认 INFO，
  大写级别词覆盖）；`job` 固定 `container`。少了哪个哪个下拉就是空的
- **VLogs v1.50 的 `field_names`/`field_values` 都强制要求 `query` 参数**（缺失 400，
  后端翻译成 502，日志页标签下拉整体挂）；枚举字段时带 `query: "*"`
- **Grafana VictoriaLogs 数据源插件**（日志钻取用，uid `opspilot-vlogs`）不在
  grafana.com 目录，`GF_INSTALL_PLUGINS` 会 404，只能手动装：下载
  `VictoriaMetrics/victorialogs-datasource` releases 的 zip → docker cp 进
  `/var/lib/grafana/plugins/`（grafanadata 卷）→ 重启，步骤见 `deploy/README.md` 第六节；
  GitHub releases 大文件直连易断，用 gh-proxy 前缀
- **本机资产 IP 优先取 `MONITORING_HOST`**：后端在容器里时 UDP 探测只能拿到网桥 IP，
  与 Alloy `HOST_INSTANCE` 对不上，host-metrics/日志 host 过滤就失联（bootstrap 已修）

### SSH 连接
- 一律走 `app/services/ssh_client.py` 的 `pin_and_connect`：主机指纹 TOFU 钉扎
  （首连自动记录到 `asset_host_keys`，指纹变化即拒绝）；**禁止 `AutoAddPolicy`**，
  禁止绕过统一入口直连 Paramiko

### 后台周期任务
- 后台任务（scheduler 周期任务 / 账单同步 / 应用发布等）必须先接 `app/services/leases.py`
  的数据库租约（`task_leases` 表）再执行，防多 worker/多实例重复跑；
  调度器对租约故障 fail-open（租约表不可用时放行执行并记日志——宁可极小概率重复，不可停摆）

### 用户会话（token_version）
- `users.token_version` 是会话版本号：签发 JWT 时写入、校验时比对；
  **重置密码 / 禁用账号必须递增**，使该用户全部旧 token 立即 401；
  新的"作废某用户所有会话"类需求复用这一机制，不要另造 token 黑名单

### 数据请求
- 所有列表/详情用 `useQuery`，变更用 `useMutation`
- 操作成功后调用 `queryClient.invalidateQueries({ queryKey: ['xxx'] })` 刷新缓存
- 不手写 useEffect + fetch

### 特殊协议
- **SSE 流**：AI 对话流式输出、日志 Live Tail、Agent 安装进度、K8s Watch
- **WebSocket**：终端 Session（先 POST 换一次性 token，再连 WS）
- **WebSSH**：使用 @wterm/react，后端流程：`POST /api/assets/terminal/session` → WS

---

## 页面开发规范（新建页面必须遵守）

1. **参考模板**：`frontend/src/pages/assets/hosts/index.tsx`
2. **表格**：antd `Table`，复杂场景用 `ProTable`
3. **新增/编辑**：antd `Drawer` + `Form`（不用 Modal，除非字段极少）
4. **删除**：`Popconfirm` + `useRiskConfirm()` 二次确认
5. **状态**：`useQuery` + `useMutation`，不写本地 loading state
6. **视觉**：不自作主张添加颜色、背景、装饰——等用户指示
7. **提示**：用 `App.useApp()` 的 `message`/`modal` 实例，不用静态调用

---

## 后端接口约定

- 响应格式：**直接返回数据**（无 `{code, data, message}` 包裹）
- 错误格式：HTTP 状态码 + `{ detail: string }`（422 含字段详情）
- 分页：部分接口用 `limit/offset`，部分返回纯数组（无统一规范）
- 免鉴权接口：`/api/settings/login-page`、`/api/monitoring/collector/agent/*`

### 凭证接口的特殊约定（易踩，改动前必读）

- `POST/PUT /api/credentials` **只接受单个加密 `content` 字段**，没有 `username`/`password`/`private_key`/`access_key` 等独立字段；`expires_at` 必填（`YYYY-MM-DD`）
- `content` 的格式按类型约定：`AccessKey` → JSON `{"access_key":..,"secret_key":..}`；`SSH Key` → PEM 原文；`Password`/`Token` → 明文
- `type` 取值必须用 `AccessKey` / `SSH Key` / `Token` / `Password`——后端 `_build_export_payload`、`is_long_lived_key_type`、finance 的 AK/SK 下拉都按这些字符串判断，写成 `access_key` 这类下划线形式会静默走错分支
- 凭证**永不回传密文**，编辑时敏感输入框留空 = 不改动（需 `replace_content: true` 才替换）
- 凭证表**没有用户名列**，SSH 登录用户取主机的 `ssh_user`

### 阿里云账单同步的特殊约定（易踩，改动前必读）

- **BSS SDK 的模型属性是 snake_case**（`product_name`、`pretax_amount`、`instance_id`），而 `to_map()` 序列化出来的 JSON 键才是 PascalCase（`ProductName`、`PretaxAmount`）。`getattr(item, "ProductName")` 静默返回 `None`——曾导致整表金额=0、产品名/实例 ID 全空，而同步流程一路"成功"。**取值一律用 snake_case。**
- `DescribeInstanceBill` 的字段与直觉不同的几处：计费项中文名在 `item_name`（没有 `BillingItemName`）；实例备注名在 `nick_name`（没有 `ResourceName`）；计费项编码常在 `pip_code`（`billing_item` 常为空串）；标签只有 `tag` 且是 `key:x value:y;...` 字符串（没有 `Tags` 对象数组）
- 该接口**不返回** `PaymentAmount` / `CashAmount`，`cloud_bills.payment_amount` 恒为 0 是正常的；实付金额需另调 `QueryBillOverview`。费用总览/账单/聚合全部以 `pretax_amount`（税前应付）为准
- 分页外层字段同样是 snake_case：`account_id` / `account_name` / `next_token`

### 开发库表结构的特殊约定（易踩，改动前必读）

- **开发库的表由 lifespan 的 `create_all` 建，而 `create_all` 只建缺失的表、不会给已存在的表加列**；Alembic 迁移只用来验证迁移文件本身，不往开发库上跑。所以**给模型加新列后，老库不会自动有这一列**，直到某个查询把它 SELECT 出来才炸 `UndefinedColumn`
- 启动时的 `ensure_model_columns()`（`app/main.py`）已把这层兜底泛化：反射比对模型元数据与库结构，缺列自动 ALTER 补上，逐列 try/except，**没有服务端默认值的列不加 NOT NULL**。正常情况下不用手动补列；若某列类型编译不出来导致补列失败，启动日志会打印 `补列失败 <表>.<列>`
- 新表则走 `create_all`，无需额外操作；新 ORM 模型除了 import 进 main 链路，还必须加进 `backend/app/db/models.py` 注册表
- 仍以 Alembic 为准的场景：生产环境设 `DB_AUTO_CREATE_TABLES=0`，此时不会建表也不会补列，必须 `alembic upgrade head`

### 数据库执行引擎的特殊约定（易踩，改动前必读）

- **`cursor.execute()` 的返回值不能当影响行数**：pymysql 返回 int，**psycopg3 返回 cursor 自身**，`int(cursor.execute(...))` 在 PostgreSQL 上必然抛 `TypeError`。影响行数一律取 `cursor.rowcount`（两个驱动都支持；SELECT 时是结果集行数；为 -1 表示无法确定，要兜底成 0）
- `execute_statements` 每次执行都新建连接、用完即关（`_build_connection`），所以**TEMP 表不能跨调用复用**；MySQL 走 pymysql，PostgreSQL 走 psycopg3（venv 里没有 psycopg2）
- 自助查询复用审核引擎的 R8 规则，任何写语句在接口层就被 422 拒掉，走不到执行引擎

---

## 已完成功能清单

- ✅ 工程脚手架（Vite + React 18 + TS + antd 5）
- ✅ Vite proxy（`/api/*` → `http://127.0.0.1:8001`）
- ✅ axios 实例（Bearer 注入 + 401 白名单跳转 + 全局错误提示）
- ✅ Zustand 登录态（token 持久化）
- ✅ 路由骨架（AuthGuard + GuestGuard + 全部占位路由）
- ✅ 侧边栏导航布局（6 大模块，约 27 个入口）
- ✅ 登录页（左右分屏 + 火箭 SVG + 忘记密码 + 错误 Alert）
- ✅ 仪表盘（统计卡片骨架）
- ✅ 主机管理（完整 CRUD + GroupSidebar + 凭证下拉 + 刷新配置 + 批量删除）
- ✅ 资产管理 P3：虚拟机 / 凭证 / 域名 / 证书（域名证书用专用接口，非 /api/assets）
- ✅ 凭证管理契约修复：前端 `CredentialCreate` 与后端（单加密 `content` + 必填 `expires_at`）脱节导致所有新增/编辑 422，已按类型组装 content（AK/SK 走 JSON、SSH Key 走 PEM）并对齐类型取值；顺带修应用配置中心凭证下拉的失效路由
- ✅ 集群管理（Kubeconfig 纳管 + 解除 + 健康摘要）
- ✅ GroupSidebar 公共组件（新增/编辑/删除分组 + 分配资产）
- ✅ 权限体系打通（登录后 bootstrap 拉 me/risk-levels/access）
- ✅ 顶栏：面包屑 + 全局搜索（Ctrl+K）+ 通知铃铛
- ✅ 站内通知（会话级 + SSE 实时推送 + 收件箱页 /notifications）
- ✅ 用户管理（列表 + 新增 + 修改角色 + 重置密码 + 禁用/启用）
- ✅ 权限管理（角色模板列表 + 权限勾选 + 新增自定义角色 + 删除）
- ✅ 审计日志（列表 + 多维筛选 + 高危行高亮）
- ✅ 全局 Query 错误提示（502/503/网络错误 → 友好 message）
- ✅ 资产管理打磨重构与后端落地（虚拟机按规范移除分组侧栏 + 独立克隆 API POST /vms/{id}/clone + 证书主机 SSH 与云服务 CDN/ALB 双模式部署 API POST /certificates/{id}/deploy + 4 大资产页统一概览统计卡片 + 防御性渲染）
- ✅ AI Agent 智能工作台（`/ai/workbench`，Ant Design Pro 极简 UI + Markdown 富文本引擎 + @Mention 技能召唤系统 + SSE 流式渲染 + WebSSH 极客代码注入）
- ✅ 应用管理（CRUD + 资源绑定 + 运行实例扫描/纳管 + 分步进度 + 类型折叠筛选 + 分页与批量移除）
- ✅ 费用管理（阿里云第一版：云账号 AK/SK 绑定 + BSS 账单按日同步 + 费用总览 + 账单明细 + 6 小时定时同步）
- ✅ 账单同步字段映射修复（BSS SDK 属性名 snake_case 而非 PascalCase，`getattr` 全落空导致同步"成功"但金额全 0/产品名全空；补 `_parse_bill_tags` 标签解析、`_dedup_key` 去重基修正、三处字段回落 + 8 例回归测试钉死映射）
- ✅ AI 平台补全（模型配置 CRUD+激活+连通测试 / 技能管理 / 全景看板 / 编排中心 V1 策略+预检 / 工作台接入路由+解包修复）
- ✅ 监控查询真实现（指标查询 PromQL 双模式+指标浏览器 / 日志查询 LogsQL+直方图+上下文+Live Tail SSE）
- ✅ 脚本执行（Ansible 四合一：ad-hoc/Playbook/模板执行 + 高危确认 + 历史 + 配置）
- ✅ 首页驾驶舱（告警/费用/AI 态势 + 趋势 + 快捷入口 + 资产健康度，按权限动态显隐）
- ✅ K8s 集群详情页（概览健康诊断/节点 drain/工作负载全操作/Pod 诊断/资源浏览 YAML+事件；修 PUT 405；+refresh 快照同步）
- ✅ K8s 集群管理 UX 重构（Pod 实时日志查看器 WS 流 + 节点用量条 + 概览可下钻 + ns/状态筛选 + YAML + Tab 深链；对标 Rancher/KubeSphere）
- ✅ K8s 集群管理全部能力落地（Pod Exec 终端 + YAML 工作台 dry-run/apply + 命名空间/审计 Tab + 跨资源搜索 + 节点标签污点编辑 + 改镜像/暂停/CronJob 操作 + PVC 扩容/Ingress TLS/关联资源）
- ✅ K8s 集群 CRUD 完备性补全（命名空间删除+标签编辑含保护名单、资源 YAML 在线编辑（server-side apply）、发布历史逐版回滚）
- ✅ K8s 集群实时动态（SSE Watch 前端消费：Informer 推送流 + 手动重连 + 过滤统计；修 watch/session 匿名领 token 漏洞）
- ✅ 虚拟组网（WireGuard，类 wg-easy：组网/成员管理 + X25519 密钥生成 + 配置/二维码/安装脚本出口 + 主机资产关联；新增依赖 segno）
- ✅ WG 三项增强（SSH wg show 状态回读/在线判定 + 成员 Ansible 一键远程安装 + 主机页组网联动抽屉）
- ✅ 数据库管理深化（实例级角色授权 + 审核规则可配置 R1-R9 开关/级别 + 工单定时执行 scheduler + 执行结果站内通知 + UPDATE/DELETE 自动备份与回滚 SQL 导出）
- ✅ 数据库管理（类 Yearning：数据源 CRUD/连通测试 + SQL 规则引擎 R1-R9 工单流审批执行分权 + 只读自助查询审计；MySQL pymysql/PG psycopg；新增依赖 pymysql）
- ✅ IM 智能运维闭环（塔斯汀模式：真 LLM 根因分析注入历史经验 + 根因三元组沉淀 + AI 准确率统计 + IM 卡片 {{console_url}} 处理入口）
- ✅ 费用联动与预算告警（主机页实例费用抽屉 + GET/PUT /finance/budget + 预算进度卡 + budget_alert runner 复用告警管道）+ 实时看板升级（全局态势 + 多主机对比）
- ✅ 个人 IM 渠道与飞书群 @ 通知（P1：user_im_bindings 绑定表 + im_feishu 服务 [token 缓存/服务端换 open_id/自定义机器人群 @] + /api/im REST + create_notification fanout 全业务通知一键覆盖 + 告警规则 im_user_ids 通知人 + DB 工单审批通知补齐 + 前端个人 IM 页/系统管理 IM 接入 Tab/规则选通知人；open_id 只服务端换、个人 webhook 限 https+飞书域名白名单、app_secret 不回明文；契约测试 36 例，全量 348 绿）
- ✅ 数据库管理与站内信体验优化（DB 页概览卡片点击联动筛选 + 工单 Tab 角标 + 关键词即时搜索 + 批量批准（SQL 摘要确认）+ 提交工单 Modal→920px Drawer + Ctrl/Cmd+Enter 快捷提交/执行 + 高危操作改 App.useApp().modal（修静态 Modal.confirm 与 useRiskConfirm 直通零确认）+ 修审核规则 Tab 渲染期 setState 反模式；通知中心分类/搜索/时间分组/未读竖条 + 铃铛分类 Tag 与单条已读 + store local- 通知跳过空请求；后端 list 增 category/q 筛选含 LIKE 转义；契约测试 352 绿）
- ✅ 账单同步字段映射修复（阿里云 BSS SDK 属性是 snake_case、`to_map()` 才是 PascalCase，`getattr(item, "ProductName")` 静默返回 None 导致同步"成功"但每行金额/产品全空；顺带修 Tag 字符串按空格切段把值截断的问题；契约测试 8 例）
- ✅ 开发库缺列修复 + 未定义名专项清理（`create_all` 只建表不加列 → 泛化成启动时反射比对的 `ensure_model_columns()`，根治模型加列老库缺列这一类静默故障；修 `db_ops` 在 PostgreSQL 上 `int(cursor.execute())` 的 TypeError；pyflakes 扫出并修掉 9 处 `NameError` 级未定义名——集中在"函数拆文件后忘 import"和"新代码忘 import"，含证书自动续签写审计、监控 Alloy 同步、数据库/表列表等真实链路）
- ✅ 自助查询切错库治理 + 结果展示友好化（查询日志新增"实际连接的库"列，切错库导致的失败在审计里能直接看出；库下拉过滤 PG/MySQL 维护库但实例默认库永不过滤，目标库默认跟随实例并标注"（默认）"；驱动报错翻成可操作中文提示——PG 按 SQLSTATE、MySQL 按 errno、连接类按消息特征匹配，`42P01` 正则抠出表名点名并说明当前连的是哪个库；`_build_connection` 移进 try 修掉 finally 里的 NameError 并让连接失败也产出带提示的结果；结果区成功显示库/耗时/行数、失败 hint + 原始报错折叠、0 行空态；顺带把 `app/db/models.py` 漏注册的 11 个模型类补上——Alembic 此前完全看不见数据库管理/费用/部署/WireGuard/影子告警/根因分析，autogenerate 会为它们生成 DROP TABLE）
- ✅ 工程化加固与架构拆分（清理调试残留；裸 fetch 全部改走 axios/authenticatedFetch 并接通死代码 `lib/fetch.ts`；移除未用 recharts；vitest 冒烟 10 例；useCrudResource 铺到 clusters/finance/wireguard；`clusters.py` 5920 行拆为 `app/api/clusters/` 包，57 REST + 2 WS 不变、mock 兼容 shim；requirements 钉死版本；pre-commit + GitHub Actions；Docker/compose/部署说明）
- ✅ CMDB 关联图谱与运维入口补全（`/api/cmdb/graph|impact` 应用↔资产拓扑与影响面 + `/assets/cmdb` echarts 图谱页；SSH 指纹列表/清除入口上线；费用账单 CSV 导出）
- ✅ 集群管理功能巡检与优化（七个 Tab + YAML 工作台逐点击巡检产出 10 项问题全修：集群运维十动作批量注册风险等级——未注册即静默 low，前端一确认框都不弹；YAML apply 补 field_manager 修"试运行能过、应用必失败"；核心组件表外置措辞/占位 Pod 名/RKE 下 etcd 双行矛盾；操作审计时间本地化 + action/resource 拆分 + PATCH 补进审计映射 + 20 条集群子路径风险规则；命名空间用量结构化 + 核心命名空间前端前置禁删；诊断图标换医药箱、头部 vv 双前缀、YAML 工作台误关守护、集群列表 ⋯ Tooltip；契约测试 8 例，检查-全量 4/4 全绿后端 702 passed，详见 `47-集群管理功能巡检与优化.md`）
- ✅ 集群管理第二轮优化（代码级复盘 14 项全修：PUT 编辑集群 / PATCH 资源 edit / DELETE 通用资源删除 / Pod exec session 四条路由补进 `AUDIT_RISK_ROUTE_RULES`，patch_labels/scale/create_namespace/exec_session 补风险登记（此前前端弹了确认、票据也消费了，审计却记 low）；审计 Tab 补六项筛选 + 服务端分页 + 30s 自动刷新 + CSV 导出 + ip/risk_reason 回传；工作负载操作列 12 个按钮收敛为 5 高频外显 + ⋯ 菜单 7 项，按后端 kind 矩阵 `SCALE_KINDS`/`ROLLOUT_KINDS`/`DELETE_KINDS` 动态禁用消掉死按钮；全局搜索点结果带 ns/kind/name 跳转并 `.cluster-row-focused` 高亮定位 + kinds 多选；实时动态渲染 Informer 状态摘要 + 5 次退避失败后手动「重新连接」；三个 Tab 筛选全部入 URL（新增 `detail/useTabParams.ts`，URL 即单一数据源，刷新/分享/前进后退不丢）+ namespaces 双 queryKey 合一 + 删除改 invalidate + 资源列表接 `continue_token` 每页 100 翻页；概览 Top 诊断问题与核心组件异常行可点击下钻、零依赖 `YamlPre` 复制按钮替换四处裸 `<pre>`；检查-全量 4/4 全绿后端 707 passed + 119 subtests，GUI 14 项全过，详见 `48-集群管理第二轮优化.md`）
- ✅ Grafana 免密跳转（SSO）全链路（监控页点「Grafana 更多数据」→ 同源新标签页打开 Grafana Explore，已按当前用户身份登录、查询条件与指标数据一并带过去；架构为后端同源反代 `/grafana/*` + Grafana `auth.proxy`，原边缘 Nginx `auth_request` 方案在本拓扑下三处硬伤已废；`sso-link` 签 90 秒 JWT 只放地址栏，反代校验后注入 `X-WEBAUTH-*` 并种 8 小时 HttpOnly 会话 Cookie，后续请求身份每次回库重取，会话吊销复用 `token_version`；`管理员`→Grafana Admin、其余→Viewer，登录名统一 `opspilot_` 前缀防撞 Grafana 内置 admin；宿主机 `GF_SERVER_ROOT_URL` 改相对 `/grafana`（绝对地址会把容器内/开发机 IP 泄漏进 HTML `appUrl`）、补角色映射 env、修数据源网络与 data 卷属主；仓库两个 compose 升级 Grafana 11.0.0 + 对齐 auth-proxy env + 新增 VictoriaMetrics 数据源 provisioning；GUI 首轮白屏暴露 3 个真实缺陷——Grafana 静态资源被 SSO 拦成 401（`/public/**` 走相对地址、请求不可能带票据，加白名单）、无票无 Cookie 时匿名透传给 Grafana（反代变开放代理，改一律 401）、`int(x or -1)` 让 `token_version=0` 永远失配；检查-全量 4/4 全绿后端 745 passed + 119 subtests、vitest 52 passed，契约测试 38 例，详见 `49-Grafana免密跳转SSO.md`）

- ✅ 新环境部署演练（192.168.3.235 全新 Ubuntu 裸机从零到全功能可用：装 Docker + registry 镜像加速 → 源码上机构建 → install.sh --config 非交互一键部署 → 8 容器全绿；抓出并修复 3 个只有真全新环境才暴露的 bug——offline alloy 配置缺必填 targets 属性启动即崩、VLogs v1.50 field_names 强制 query 参数日志标签下拉 502（99 同样存在）、容器内注册本机资产拿到网桥 IP 与 Alloy HOST_INSTANCE 失联；API 验收 21/21 + GUI 实证含生产反代下 Grafana SSO 跳转；契约测试 +3，检查-全量 4/4 全绿，详见 `50-新环境部署演练235.md`）
- ✅ 日志标签修复与日志钻取落日志库（删 2.9GB 旧离线镜像；日志页 主机/服务/任务/级别 四下拉从「只有 container、三个空」修到全有值——HOST_NAME 持久化进 .env、relabel 容器名为 service、loki.process 解析 level，三份采集配置同步（offline/monitoring 包 + 后端渲染器）；日志页 Grafana 钻取从查指标库改为带 LogsQL 落 VictoriaLogs 数据源——插件不在 grafana.com 目录需手动 docker cp 安装（README 第六节）、provisioning opspilot-vlogs、GrafanaLink 支持 datasource 参数；235 全部实证；契约测试 +1，检查-全量 4/4 全绿，详见 `51-日志标签修复与日志钻取落日志库.md`）
- ✅ 集群纳管解卡与文案精简（`/scan` 返回 `version` 而前端读 `server_version`——探测成功版本也永远落空再被必填拦死；ClusterCreate 六字段全默认值 + 前端去 required + 探测失败也进第二步手动填；网页实测纳管 Rancher 托管集群 231-Rancher/v1.32.6 端到端通；文案精简两批共 16 处——四个监控页教学「说明」卡片、查询前空卡、「点上下文/命中预览」引导句、证书分发机制介绍等界面教学句全删，SSH 指纹告警/未配置引导/审核规则/业务规则等状态类提示保留；检查-全量 4/4 全绿，详见 `52-集群纳管解卡与文案精简.md`）
- ✅ 数据库工作台概览卡下沉与去重（四张概览卡原压在 Tab 栏上方、切 Tab 栏位跳动，是全站唯一内容盖过导航的页面；第一轮 `Row` 下沉让七 Tab 结构统一，第二轮分析确认与「今日流水线」重复（待执行口径还打架）后整行删除、清死代码，连接异常降级为仅异常时出现的条件告警条；断言 .ant-statistic=0、三态 tabsTop 恒 112px；检查-全量 4/4 全绿，详见 `53-数据库工作台概览卡下沉.md`）
- ✅ 数据库页 RBAC 显隐收敛（后端四把钥匙分权本就严格，缺口在前端「只禁用不隐藏」+ 管理 Tab 无门：查询授权/数据源/审批流/审核规则四 Tab 收进 canManage、InstancesPanel 管理按钮改条件渲染、工单/工作台提交入口按 submit 显隐；用「只读+页面+submit」测试账号实测只见三 Tab、后端 POST instances 403、admin 视野无损；检查-全量 4/4 全绿，详见 `54-数据库页RBAC显隐收敛.md`）
- ✅ 权限管理抽屉加权限键搜索（「找不到 action.db.manage」排查=键一直都在：角色行铅笔图标 → 权限配置 → 资产管理组 13 项里，标题是功能名非按钮文案；修法=抽屉加搜索框四路过滤+命中组自动展开；顺带收拾权限组列表——名称断行、图标按钮改带文字链接、权限数带总量；检查-全量 4/4 全绿，详见 `55-权限管理抽屉加权限键搜索.md`）
- ✅ 内置数据库管理员角色与权限组排序（新增预设：页面+db 四件套+dashboard/ai.ops/im 基线，自动成系统组；排序从标题 Unicode 码点改为系统预设按定义顺序在前/自定义按名称在后——rbac.py 一处修两处 UI 生效；权限组表删冗余「角色标识」列——title 与 role 恒等纯重复，表格收敛四列；契约测试 +2；检查-全量 4/4 全绿，详见 `56-内置数据库管理员角色与权限组排序.md`）
- ✅ README 整理与截图入库（重写 README：数据刷新 752+52、3 张 mermaid 图——架构总览/Grafana SSO 时序/高危票据时序、25 张页面实拍截图分组展示于 `docs/screenshots/`；截图纪律=等路由标记文字出现再拍防白屏废片；集群详情截图需临时纳管、拍完票据删除恢复原状；检查-全量 4/4 全绿，详见 `57-README整理与截图入库.md`）
- ✅ 235 全量全新部署与本机接入（全新 Ubuntu 22.04 → Docker → 源码上机构建 → `install.sh --config` 装到 `/data/opspilot`；监控链路 self-check 全绿 + 指标/日志/Grafana SSO 跨机验证；部署机自注册 `testserver-305 (本机)` 并由内置 Alloy 采集；修复 3 个真问题——新增 `.gitattributes` 钉 `*.sh`/`*.alloy` 为 LF（CRLF 在 Linux 执行即炸）、`bootstrap.py` 自注册主机名优先取 `HOST_NAME`（原容器 ID 做名字）+ `install.sh` 注入 app/.env、`prom_instance_regex` 的 `re.escape` 产出 `\-` 触发 VM 422 致主机监控图表抽屉静默全空（日志正常）改字符类零反斜杠转义；详见 `58-235全量全新部署与本机接入.md`）

---

## 可复用模块（新页面优先复用）

- `hooks/useCrudResource.ts` — 封装列表页 save/delete mutation + 缓存失效 + toast，标准 CRUD 页直接用
  （已用于 hosts/vms/credentials/domains/certificates/clusters/finance accounts/wireguard 组网）
- `hooks/usePermission.ts` — `usePermission().has()` 与 `useRiskConfirm().confirm()`
- `lib/fetch.ts` — `authenticatedFetch`：流式/SSE 场景专用（自动 Bearer）；JSON 接口一律走 `api/client.ts`
- `store/notifications.ts` — 通知 store，`push()` 推会话级通知，未来 `setAll()` 接后端
- `components/layout/menuConfig.tsx` — 菜单树 + 面包屑/搜索共享的扁平路由
- `router/routes.ts` — **路由单一数据源**：38 条懒加载路由 + 每条路由的权限键都在这里，
  菜单/面包屑/全局搜索/权限过滤均由它派生；新增页面只改这一处，不要再另维护路由副本
- `services/im_feishu.py` — 飞书服务（token 缓存/换 open_id/群 @ 发送）；新业务要发 IM 通知只需接 `create_notification()`，fanout 自动完成
- `services/leases.py` — 后台任务数据库租约（获取/心跳/过期释放）；新增后台任务先拿租约再干活
- `services/ssh_client.py` — SSH 统一入口 `pin_and_connect`（主机指纹 TOFU 钉扎），所有 SSH 连接从这里走
- 数据库管理 Yearning 对齐：数据库页现已具备 `action.db.submit` / `action.db.approve` / `action.db.execute` 三权分离、多级审批流、查询授权（`db_query_grants`）和数据字典抽屉；新增 DB 通知必须继续复用 `create_notification()`，不要直接调用飞书发送函数
- 页面模板参考 `pages/assets/hosts` 或 `vms`

---

## 开发文档目录

```
开发文档记录/
├── frontend-pages.md              # 全部页面功能清单 + API 接口
├── 开发计划.md                    # 整体进度 + 近期队列 + 技术决策记录
├── 后端接口需求-站内通知.md    # 交付后端的接口规格
└── 页面开发记录/
    ├── 00-主布局与侧边栏.md
    ├── 01-登录页.md
    ├── 02-主机管理.md
    ├── 03-06-资产管理页面.md
    ├── 07-顶栏增强与站内通知.md
    ├── 12-WebSSH终端与SFTP文件管理.md
    ├── 13-资产管理重构与优化打磨.md
    ├── 17-费用管理-阿里云账单.md
    ├── 18-全项目补全-AI平台-监控查询-脚本执行-驾驶舱.md
    ├── 19-费用联动-预算告警-实时看板升级.md
    ├── 20-K8s集群详情页.md
    ├── 21-K8s集群管理UX重构.md
    ├── 22-K8s集群管理功能补全.md
    ├── 23-集群管理CRUD完备性补全.md
    ├── 24-集群实时动态SSE.md
    ├── 25-WireGuard虚拟组网.md
    ├── 26-数据库管理SQL工单.md
    ├── 27-IM智能运维闭环.md
    ├── 28-K8s集群管理缺陷修复.md
    ├── 29-后端测试套件清理与全绿.md
    ├── 30-IM个人渠道与群at通知.md
    ├── 31-数据库管理与站内信体验优化.md
    ├── 32-凭证管理契约修复.md
    ├── 33-账单同步字段映射修复.md
    ├── 34-开发库缺列修复与未定义名清理.md
    ├── 35-自助查询切错库与结果展示友好化.md
    ├── 36-数据库管理Yearning审批流与查询授权升级.md
    ├── 37-安全与可靠性加固.md
    ├── 38-工程化加固与架构拆分.md
    ├── 47-集群管理功能巡检与优化.md
    ├── 48-集群管理第二轮优化.md
    ├── 49-Grafana免密跳转SSO.md
    ├── 50-新环境部署演练235.md
    ├── 51-日志标签修复与日志钻取落日志库.md
    ├── 52-集群纳管解卡与文案精简.md
    ├── 53-数据库工作台概览卡下沉.md
    ├── 54-数据库页RBAC显隐收敛.md
    ├── 55-权限管理抽屉加权限键搜索.md
    ├── 56-内置数据库管理员角色与权限组排序.md
    ├── 57-README整理与截图入库.md
    ├── 58-235全量全新部署与本机接入.md
    └── ../全量功能测试清单.md
```

---

## 注意事项

0. **时间字段统一约定**：凡传给 VictoriaMetrics/VLogs 等外部存储的时间参数，一律 UTC RFC3339（`Z` 后缀，避免裸 `+` 的 URL 编码歧义）；数据库内存本地时间字符串。解析外部传入的时间用 `fromisoformat` + 显式时区——无时区串会被 VLogs 按 UTC 解释，UTC+8 下时间窗整体偏移 8 小时（日志标签下拉空/直方图全平踩过）
1. **不要自作主张修改视觉风格**（颜色、背景、装饰等），等用户明确要求
2. **antd 不要升级到 6.x**，会破坏 Pro Components
3. **新增依赖前询问**，避免引入冗余包
4. **每完成一个页面**，在 `开发文档记录/页面开发记录/` 新增对应文档，并更新 `开发计划.md` 的进度
5. WebSSH 使用 `@wterm/react`（vercel-labs/wterm），不要用 xterm.js
6. **改完代码跑一遍仓库根 `检查-全量.bat`**（统一验证入口：后端 pytest + pyflakes 未定义名 +
   前端 lint + 前端 build；带 `--secret-scan` 追加秘密扫描，只报告不修改）。
   等价命令行：`backend\.venv\Scripts\python.exe -m pytest -q`（backend 目录）、
   `.venv\Scripts\python.exe -m pyflakes backend/app`（仓库根）、
   `pnpm --dir frontend run lint / run test / run build`
7. **JSON 接口一律走 `api/client.ts`**（自动 Bearer + 401 + 风险票据）；
   SSE/流式走 `lib/fetch.ts` 的 `authenticatedFetch`。**禁止裸 `fetch()` 打 `/api`**。
8. **拆大文件时注意 mock 兼容**：契约测试可能 `patch.object(<package>, <symbol>)`，
   像 `app/api/clusters/` 那样用 `__init__` re-export + setattr 同步到实现模块，否则 mock 静默失效。
9. **下拉/菜单项保持简洁**：只写名称（可带图标与选中勾），**不要加副文案/说明行**（如「始终浅色」「与系统外观一致」）。
10. **展示文案别写界面教学**（用户明确要求，2026-10-09 两批共删 16 处）：「点右上角 X」「保存前点 Y」「点上方 Z」这类教用户怎么点的句子一律不写；**保留**系统状态、失败原因、安全警告、业务规则（如审核拒绝规则、审批豁免）。空态给一句最短操作提示即可，不要多句教程。
