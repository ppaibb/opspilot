# OpsPilot 前端重构开发文档

> 最后更新：2026-07-23

---

## 一、项目信息

| 项 | 值 |
|---|---|
| 项目路径 | `c:\workspace\newtime\frontend` |
| 后端 API | `http://127.0.0.1:8001` |
| Swagger | `http://127.0.0.1:8001/openapi.json` |
| 页面清单 | `c:\workspace\newtime\frontend-pages.md` |

---

## 二、技术栈

| 层 | 选型 | 版本 |
|---|---|---|
| 框架 | React | 19 |
| 类型 | TypeScript | 6 |
| 构建 | Vite（Rolldown） | 8 |
| UI 组件 | Ant Design | 5.29 |
| 高级组件 | @ant-design/pro-components | 2.8 |
| 路由 | react-router-dom | 7 |
| 服务端状态 | @tanstack/react-query | 5 |
| HTTP 客户端 | axios | 1 |
| 全局状态 | zustand（持久化） | 5 |
| 图表 | echarts + echarts-for-react | 6 |
| 包管理 | pnpm | 10 |

### 关于 antd 版本

`@ant-design/pro-components` 目前只支持 antd ^4.24 或 ^5.11，不支持 antd 6.x。因此锁定 antd 5.29，等 Pro Components 支持 6.x 后升级。

### 未安装但已规划

| 库 | 用途 | 备注 |
|---|---|---|
| @wterm/react | WebSSH 终端 | 用户指定，https://github.com/vercel-labs/wterm |
| monaco-editor | YAML/配置编辑 | 集群 YAML apply 等功能需要 |
| @xyflow/react | 编排中心拖拽画布 | React Flow 更名后的包名 |

---

## 三、目录结构

```
frontend/src/
├── main.tsx                    # 入口
├── App.tsx                     # QueryClient + ConfigProvider + Router
├── index.css                   # 全局样式
├── vite-env.d.ts               # Vite 类型声明
├── types/
│   └── api.ts                  # 公共 TS 接口（Asset, Group, RBAC 等）
├── api/
│   ├── client.ts               # axios 实例（base: /api, Bearer, 401 跳转）
│   └── index.ts                # 按模块的 API 函数
│       ├── authApi             # login, getMe, getRiskLevels
│       ├── dashboardApi        # overview
│       ├── assetsApi           # CRUD + ping + sync
│       ├── groupsApi           # CRUD + assign
│       ├── rbacApi             # metadata
│       └── loginPageApi        # getConfig（免鉴权）
├── store/
│   └── auth.ts                 # Zustand 登录态（token/user 持久化到 localStorage）
├── hooks/
│   └── usePermission.ts        # usePermission + useRiskConfirm
├── components/
│   └── layout/
│       └── MainLayout.tsx      # 侧边栏导航 + 顶栏 + 用户下拉
├── router/
│   └── index.tsx               # 全部路由 + AuthGuard/GuestGuard + 占位页
└── pages/
    ├── login/index.tsx         # ★ 登录页（完整实现）
    ├── dashboard/index.tsx     # ★ 仪表盘（完整实现）
    └── assets/hosts/index.tsx  # ★ 主机管理（完整 CRUD 示范）
    ├── assets/vms/             # 占位
    ├── assets/clusters/        # 占位
    ├── assets/credentials/     # 占位
    ├── assets/domains/         # 占位
    ├── assets/certificates/    # 占位
    ├── monitoring/             # 占位
    ├── ai/                     # 占位
    ├── apps/                   # 占位
    ├── tools/                  # 占位
    └── settings/               # 占位
```

---

## 四、已实现的核心功能

### 4.1 登录页 (`pages/login/index.tsx`)

- 左右分屏布局：左侧 60% 深蓝紫渐变面板（logo + 原创火箭 SVG 插图 + 底部链接），右侧 40% 白色表单区
- 从后端 `/api/settings/login-page`（免鉴权）动态加载 `hero_title` / `hero_subtitle`
- 用户名密码表单，连接 `/api/auth/login`，登录成功 token 存入 Zustand 持久化
- 未登录自动跳转登录页，已登录访问登录页自动跳首页
- 密码输入框右侧"忘记密码？"链接，点击弹出居中 Modal 提示联系管理员（`App.useApp()` 实例，避免静态调用失效）
- 网页标题改为 `OpsPilot`，lang 改为 `zh-CN`
- Favicon 替换为原创火箭图标（深蓝圆底 + 紫色鼻锥 + 蓝色舷窗 + 橙色尾焰）

### 4.2 仪表盘 (`pages/dashboard/index.tsx`)

- TanStack Query 调用 `/api/dashboard/overview`
- 显示资产总数、主机数、虚机数、集群数、域名数、证书数等统计卡片

### 4.3 主机管理示范页 (`pages/assets/hosts/index.tsx`)

- **表格**：主机列表，支持搜索（名称/IP）、分组筛选、分页
- **分组侧栏**：从 `/api/groups` 加载，点击筛选
- **新增/编辑抽屉**：antd Drawer + Form，包含名称、IP、提供商、地域、SSH 用户/端口、凭证、分组、CPU/内存/磁盘、备注
- **SSH 连通测试**：调用 `/api/ping` 验证连接
- **高危确认**：删除操作弹窗二次确认
- **数据刷新**：增删改后自动 invalidate TanStack Query 缓存

### 4.4 请求层 (`api/client.ts`)

- axios 实例 baseURL `/api`
- 请求拦截器：自动注入 `Bearer <token>`
- 响应拦截器：401 自动清空 token 并跳转 `/login`
- Vite proxy：`/api/*` → `http://127.0.0.1:8001`

### 4.5 权限与安全 (`hooks/usePermission.ts` + `store/auth.ts` + `router/index.tsx`)

- `bootstrapped` 标志：登录后权限是否已加载，setAuth 时重置为 false
- AuthGuard 检测 `token && !bootstrapped` 时并行调用三个接口：
  - `GET /api/auth/me` → 刷新用户信息
  - `GET /api/auth/risk-levels` → 写入高危等级到 store
  - `GET /api/settings/users/{id}/access` → `effective_permissions` 写入 store
- bootstrap 期间显示全局 Spin，任意单个接口失败不阻塞整体（Promise.allSettled）
- `usePermission().has(key)` 现在真正读取 store 里的 effective_permissions
- `useRiskConfirm().confirm(actionKey, callback)` 读取 store 里的 riskLevels，高危/中危操作弹窗确认

### 4.6 侧边栏导航 (`components/layout/MainLayout.tsx`)

- 6 个一级菜单，展开后约 27 个页面入口
- 双状态折叠：`isCollapsed`（锁定）+ `isHovered`（临时），`visualCollapsed = isCollapsed && !isHovered`
- 底部「收起/展开侧栏」按钮，点击锁定折叠态
- 折叠后 hover 侧边栏临时展开，离开自动收回
- 折叠/离开时强制重置 `openKeys` 为当前活跃分组，避免 hover 唤醒时展开一堆子菜单
- 文字/标签用 `opacity + translateX` 隐藏（非 `display:none`），保留占位，动画更平滑
- 过渡动画：`all 0.3s cubic-bezier(0.645, 0.045, 0.355, 1)`（antd motionEaseInOut）
- `index.css` 全局压掉 `.ant-menu-submenu-popup`，折叠态不再弹出浮动子菜单
- 顶栏用户下拉（退出登录）

---

## 五、后续待做

### 优先级：高

- [ ] 继续实现其他 CRUD 页面（虚拟机组机/域名/证书/应用/告警规则等），沿用主机管理的模板
- [ ] 用 openapi-typescript 从 `/openapi.json` 生成完整 TS 类型，替换 `types/api.ts` 中的手写类型
- [ ] 登录后连接 `/api/auth/me` + `/api/settings/rbac/metadata` 获取真实用户权限
- [ ] WebSSH 终端页（安装 @wterm/react）

### 优先级：中

- [ ] 监控：实时看板、指标查询（PromQL 自动补全）、日志查询（Live Tail SSE）
- [ ] 告警中心：活跃告警、历史告警、静默、AI 分析
- [ ] 凭证管理（含导出高风险操作）
- [ ] 应用管理（应用详情、资源绑定、时间线、健康检查）
- [ ] K8s 集群管理（节点/Pod/工作负载/YAML apply）

### 优先级：低

- [ ] 编排中心（React Flow 拖拽画布）
- [ ] AI 工作台（会话管理 + SSE 流式输出）
- [ ] 审计日志
- [ ] 系统设置（通知渠道、用户、RBAC、功能开关）

---

## 六、环境与启动

```bash
cd c:\workspace\newtime\frontend
pnpm install    # 安装依赖
pnpm dev        # 启动开发服务器（http://localhost:5173）
pnpm build      # 生产构建
```

后端需在 `127.0.0.1:8001` 运行（或按 vite proxy 目标）。前端 `/api/*` 请求通过 Vite proxy 转发。
