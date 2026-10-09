# 49 - Grafana 免密跳转（SSO）全链路落地（2026-10-08）

> 需求原话：「接下来去吧grafana 这套跑通，就是安装好之后 可以通过页面跳转到 grafana
> 自带使用系统的 sso 功能 你懂吧?就是 grafana.ini 也都配置好了 你去看看」。
>
> 结论：**跑通了**。OpsPilot 监控页点「Grafana 更多数据」→ 同源新标签页打开 Grafana
> Explore，已按当前登录用户身份登录（管理员→Grafana Admin，其余→Viewer），
> 查询条件与指标数据一并带过去。检查-全量 **4/4 全绿**（后端 **745 passed** + 1 skip，
> **119 subtests passed**）、前端 vitest **52 passed**，GUI 端到端有浏览器实证。

---

## 一、现状盘点：为什么原来的链路走不通

部署拓扑（关键前提）：**OpsPilot 前后端在 Windows 开发机，PostgreSQL 与监控栈在
局域网另一台 Ubuntu（`192.168.3.99`）**。Grafana 以容器跑在那台机器上，
`opspilot-grafana`，端口映射 `0.0.0.0:3002:3000`，子路径 `/grafana`。

盘点结果：

| 项 | 实况 | 判断 |
|---|---|---|
| Grafana 版本 | 11.0.0 | OK |
| `grafana.ini` / env | auth.proxy 已启用，`root_url` 配错 | 需修 |
| 宿主机 nginx | **未运行**，`auth_request` 指向一个并非 OpsPilot 的服务 | 链路断了 |
| nginx 配置 | 不在仓库里，无法随仓库发布 | 不可维护 |
| OpsPilot 后端 | 只监听 `127.0.0.1:8001`（开发机回环） | 浏览器不能直连 |
| `vm_url` / `grafana_url` | 只存在后端 SystemConfig | 前端拿不到内网地址 |

原设计是「边缘 Nginx 做 `auth_request`，校验通过后把身份头喂给 Grafana」。
在本拓扑下它有三处硬伤：nginx 没起、auth 地址指向错误服务、配置不入库。
因此本轮改为**后端同源反代**，与仓库既有约定一致——`app/api/monitoring/services/proxy.py`
里写明「前端从不直接访问 VictoriaMetrics/VictoriaLogs/Grafana，所有查询走后端代理」。

---

## 二、架构：后端同源反代 + auth.proxy

```
浏览器                 OpsPilot 后端                Grafana (192.168.3.99:3002)
  │                            │                              │
  │ 1 点「Grafana 更多数据」    │                              │
  ├─GET /api/monitoring/grafana/sso-link──────────────────────▶│
  │◀────── { url: "/grafana/?opspilot_sso=<90s JWT>" }─────────┤
  │                            │                              │
  │ 2 window.open 同源新标签页  │                              │
  ├─GET /grafana/explore?...&opspilot_sso=<JWT>───────────────▶│
  │                            │ 3 校验 JWT → 查库取用户        │
  │                            │ 4 注入 X-WEBAUTH-USER/NAME/    │
  │                            │   EMAIL/ROLE，转发 ───────────▶│ auth.proxy 自动建号/登录
  │                            │◀──────── 200 HTML ────────────┤
  │◀─ HTML + Set-Cookie: opspilot_grafana_session (8h, HttpOnly)
  │                            │                              │
  │ 5 后续请求只带 Cookie       │                              │
  ├─GET /grafana/api/user ────▶│ 6 校验 Cookie → 重新注入身份头 │
  │                            │────────────────────────────▶│
  │◀───────────────────────────┴──────────────────────────────┤
```

身份映射（`app/api/grafana_sso.py`）：

- `管理员` → Grafana **Admin**；其余一律 **Viewer**。
  库里角色存中文展示名，`admin`/`administrator` 是部署环境常见英文别名，
  统一走 `normalize_role_name()` + `ROLE_ALIAS_MAP` 归一化后再比。
- Grafana 登录名统一加 `opspilot_` 前缀（`_build_grafana_login`）。
  **这不是美观问题**：若把 OpsPilot 用户名原样写进 `X-WEBAUTH-USER`，
  当用户名叫 `admin` 时会直接命中 Grafana 内置管理员账号并把它转成
  auth-proxy 外部用户（本轮盘点时宿主机上已经发生了这件事）。

---

## 三、问题清单与修复

### 3.1 Grafana 侧（宿主机，已修）

| 问题 | 现象 | 修复 |
|---|---|---|
| `root_url` 指向已死的 nginx 端口 `http://localhost:18080/` | 页面 HTML 里 `appUrl` 泄漏绝对地址，远程浏览器被带去自己的 localhost | `GF_SERVER_ROOT_URL=/grafana`（**相对地址**） |
| 缺角色映射 env | 所有人都是 Grafana Admin | `GF_AUTH_PROXY_ENABLE_ROLE_MAPPING=true` + `GF_AUTH_PROXY_HEADERS` 补 `Name/Email/Role` 三项 |
| 端口只绑容器内 | 后端从 `192.168.3.99` 能连，但排障不便 | 绑 `0.0.0.0:3002:3000` |
| 数据源网络错 | Grafana 被重建到 `opspilot-monitoring_default`，而 VM/VLogs 在 `opspilot-net`，DNS 解析失败 | 宿主机 provisioning 数据源 URL 改用 `http://192.168.3.99:8428`（容器内实测可达） |
| 卷属主错 | 崩循环 `Error: ✗ attempt to write a readonly database` | 递归 `chown -R 472:0` 整个 grafana data 卷 |

宿主机文件已同步落盘（`/opt/OpsPilot-monitoring/docker-compose.yml` + `.env` +
provisioning），重建容器不会丢配置；原文件备份 `.bak-1791471674`。

### 3.2 仓库侧（已修，保证全新安装一致）

- `deploy/monitoring/docker-compose.yml`：Grafana 镜像 `10.4.2` → `11.0.0`；
  `GF_SERVER_ROOT_URL: /grafana`；补齐 auth-proxy 全套 env；挂 provisioning 卷。
- `deploy/offline/monitoring/docker-compose.yaml`：同上，`GF_SERVER_ROOT_URL`
  默认值从 `http://localhost:3000/` 改为 `/grafana`。
- **新增** `deploy/monitoring/config/grafana/provisioning/datasources/opspilot-victoriametrics.yml`
  （offline 包同步一份）：默认数据源 `OpsPilot VictoriaMetrics`（uid `opspilot-vm`）。
- `deploy/offline/install.sh`：`write_backend_env()` 补写 `VM_URL`/`VLOGS_URL`/`GRAFANA_URL`，
  安装摘要里 Grafana 那行注明「也可从 OpsPilot 监控页免密进入」。

> ⚠️ **Grafana provisioning 不做 `${VAR}` 环境变量插值**。曾经把
> `url: ${OPSPILOT_VM_URL:-http://192.168.3.99:8428}` 写进数据源文件，
> 结果文件被静默忽略、库里仍是旧值。provisioning 文件里只能写**静态 URL**，
> 这一点在两个 provisioning 文件的注释里都钉死了。

### 3.3 后端（`app/api/grafana_sso.py`，重写）

新增 `grafana_proxy_router`（`/grafana` 前缀 + catch-all 全方法路由），
`app/main.py` 挂载。核心是**一次性票据换会话 Cookie**：

1. `GET /api/monitoring/grafana/sso-link` 用当前登录用户签一枚 **90 秒** JWT
   （`aud=grafana-sso`、`type=grafana-sso`），返回**同源相对地址**——
   浏览器从哪个 origin 打开本系统就从哪个 origin 进 Grafana。
2. 反代校验票据 → 回库取用户 → 注入 `X-WEBAUTH-*` 头 → 同时给浏览器种一枚
   **8 小时 HttpOnly Cookie**（`opspilot_grafana_session`，`Path=/grafana`、
   `SameSite=Lax`、HTTPS 下加 `Secure`）。
3. 后续请求只带 Cookie：**身份每次请求都回库重取并重新注入**。
   Grafana 的 auth.proxy 本来就逐请求解析 `X-WEBAUTH-*`，不需要依赖它自己的会话。
   副作用是改角色/改邮箱在 Grafana 侧即时生效。
4. 会话 Cookie 载荷只放 `uid + token_version`。校验时比对 `token_version`——
   复用仓库既有的「改密/禁用账号递增 token_version 作废全部会话」机制，
   **不另造 token 黑名单**。

保留 `GET /api/monitoring/grafana/auth-validate`：若部署环境更希望由边缘 Nginx
做 `auth_request`（见 `deploy/monitoring/nginx-monitoring.conf`），仍可用原链路，
两个入口共用同一套 Token 与身份映射逻辑。

### 3.4 前端（`components/monitoring/GrafanaLink.tsx`）

- 票据**点击时**才取（90 秒 TTL，预取的链接大概率已过期），取到后拼上
  `/grafana` 反代前缀 + Explore 目标路径，`window.open` 同源新标签页。
- 未配置 `grafana_url` 时按钮灰显 + Tooltip 说明去哪配。
- 入口按 AGENTS.md 约定：JSON 走 `api/client.ts`，无裸 `fetch`。

### 3.5 反代/构建配置

- `frontend/vite.config.ts`：`/grafana` → `http://127.0.0.1:8001`（`ws: true`）。
- `frontend/nginx.conf`：`location /grafana/` → `http://backend:8001`，
  带 Host/X-Real-IP/X-Forwarded-* 与 upgrade 头，`proxy_buffering off`。

---

## 四、GUI 端到端验证暴露的三个真实缺陷

首轮 GUI 验证时 Grafana **白屏**，报
「If you're seeing this Grafana has failed to load its application files」。
`performance.getEntriesByType("resource")` 显示 8 个请求全 **401**：
`/grafana/public/build/app.*.js`、`runtime.*.js`、`grafana.dark.*.css` 等。
由此挖出三个缺陷，全部修复并补了契约测试：

**缺陷 1：Grafana 自己的静态资源被 SSO 拦在门外。**
 Grafana 的 index.html 用的是**相对**资源地址（`public/build/app.xxx.js`），
 浏览器会解析成 `/grafana/public/build/...` 自动发起请求——这些请求不可能带上
 地址栏里那枚票据，于是全部 401，首屏白屏。
 实测 Grafana 对 `/public/**` 本就允许匿名访问（不带任何头也是 200），
 所以新增 `GRAFANA_PUBLIC_PATH_PREFIXES = ("public/", "avatar/")` +
 `GRAFANA_PUBLIC_PATHS = {"favicon.ico", "robots.txt"}` 白名单单独放行，
 且不注入身份头。这些路径不含业务数据，放行不会比 Grafana 本身暴露得更多。

**缺陷 2：没有票据也没有 Cookie 时，请求被原样匿名转发给 Grafana。**
 原代码只判「Cookie 不在就 401」，两条都没有时 `payload` 为 None 也照样转发，
 反代实际成了开放代理（Grafana 的匿名可达面被透了出来）。
 改为 `_resolve_proxy_identity()` 解析不出来直接 401，
 文案点明「请从 OpsPilot 监控页面重新进入」。

**缺陷 3：`token_version == 0` 的用户永远登不进 Grafana。**
 `int(payload.get("tv") or -1)` —— Python 里 `0 or -1` 求值为 `-1`，
 于是默认 `token_version=0` 的用户每次都比对失败。改成显式 try/except 转换。

这三个都是「不看真实浏览器就发现不了」的问题：契约测试第一版全绿，
但用户在浏览器里看到的是白屏。

---

## 五、回归与验证

| 项 | 结果 |
|---|---|
| 后端 pytest | **745 passed** + 1 skipped，**119 subtests passed** |
| 后端 pyflakes（未定义名） | 通过 |
| 前端 vitest | **52 passed**（10 文件） |
| 前端 lint / build | 0 error；build 通过 |
| `检查-全量.bat` | **4/4 全绿** |

新增契约测试 `backend/tests/test_grafana_sso_contract.py` **38 例**，覆盖：

- 纯函数：角色映射（含 `admin`/`Administrator` 别名）、上游基地址规范化、
  同源 Location 改写、`opspilot_` 前缀与非法字符归一、Header 非 latin-1 回退、
  静态资源白名单判定；
- `sso-link`：只返同源相对地址、未配置 Grafana 时 400；
- 反代：票据换身份头 + 种会话 Cookie、票据从转发 URL 剥离、
  `left=%7B%7D` 这类 JSON 查询串重新编码后不变形、静态资源匿名放行且不注入身份、
  无票无 Cookie 401、票据类型混用（拿会话 Cookie 当票据）401、
  会话 Cookie 按 uid 精确取人（库里多人时不取第一个）、
  `token_version` 不匹配 401、账号被禁用 401、上游连接失败 502、
  逐跳响应头与 `set-cookie` 不透传、跨源 Location 改写、登出清 Cookie 并回跳；
- `auth-validate`：管理员映射 Admin、只读映射 Viewer、无 Token 401；
- 路由注册：catch-all 已挂载、支持写方法、**不挂 `get_current_user`**。

GUI 端到端证据（Vite dev + 真 Grafana + 真 VictoriaMetrics，admin 账号）：

1. `/monitoring/metrics` 执行查询 → 结果卡出现「Grafana 更多数据」→ 点击；
2. 新标签页打开 `http://localhost:5173/grafana/explore?...&opspilot_sso=<JWT>`；
3. Grafana Explore 完整渲染：查询框里是 `up`，数据源 `OpsPilot VictoriaMetrics`，
   图例列出真实序列（`up{host="k8s-04",...}`、`up{host="ubuntu",...}`），
   **0 个失败请求**；
4. 点 Grafana 顶栏 Home → URL 变为 `/grafana/?orgId=1`（票据已不在地址栏），
   页面正常渲染、**0 个失败请求**——证明会话真的靠 Cookie 续着，不是靠地址栏；
5. `document.cookie` 读不到会话 Cookie（HttpOnly 生效），但接口全部 200；
6. 角色映射实测：admin → org role `Admin`；只读用户 → 建为 Grafana 外部 Viewer 用户。

宿主机探测产生的临时 Grafana 账号已全部删除，现仅剩 `admin` 与 `opspilot_admin`。

---

## 六、已知限制

1. **WebSocket 不透传**。反代按逐跳头规则剥掉 `upgrade`/`connection`，
   Grafana 的 `/api/live/ws`（实时推送）连不上。仪表盘、Explore、查询都正常，
   只有「实时」类能力降级。OpsPilot 自己的日志 Live Tail / K8s Watch 走自家 SSE，不受影响。
2. **Grafana 的「退出登录」被接管**。`/grafana/logout` 不透传给 Grafana，
   而是清掉我们的会话 Cookie 后 302 回 `/monitoring/metrics`——
   SSO 的入口本来就在 OpsPilot，回那里比停在 Grafana 登录页更合理。
3. **宿主机 Grafana 内置 `admin` 已被转成 auth-proxy 外部用户**
   （盘点早期有请求带过裸 `X-WEBAUTH-USER: admin`）。基本认证仍可用、角色仍是 Admin，
   功能无影响；现已由 `opspilot_` 前缀杜绝此类撞名。

---

## 七、涉及文件

**后端**
- `app/api/grafana_sso.py`（重写：票据/会话/身份头/反代/登出）
- `app/main.py`（挂载 `grafana_sso_router` + `grafana_proxy_router`）
- `tests/test_grafana_sso_contract.py`（新增 38 例）

**前端**
- `src/components/monitoring/GrafanaLink.tsx`（点击取票 + 同源跳转）
- `vite.config.ts`、`nginx.conf`（`/grafana` 反代）

**部署**
- `deploy/monitoring/docker-compose.yml`
- `deploy/offline/monitoring/docker-compose.yaml`
- `deploy/offline/install.sh`
- `deploy/monitoring/config/grafana/provisioning/datasources/opspilot-victoriametrics.yml`（新增）
- `deploy/offline/monitoring/config/grafana/provisioning/datasources/opspilot-victoriametrics.yml`（新增）

**宿主机（不入库）**
- `/opt/OpsPilot-monitoring/docker-compose.yml`（备份 `.bak-1791471674`）
- `/opt/OpsPilot-monitoring/.env`
- `/opt/OpsPilot-monitoring/grafana/provisioning/datasources/opspilot-victoriametrics.yml`
