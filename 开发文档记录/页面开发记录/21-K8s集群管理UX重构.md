# K8s 集群管理 UX 重构（对标 Rancher / KubeSphere）

> 创建日期：2026-09-18  
> 状态：完成（tsc 零错误、vite build 通过、后端 34 测试全绿）

## 📊 本轮自批评（PM/UX 视角，改动前先挑毛病）

对照 Rancher / KubeSphere，上一版详情页（见 20 号文档）有 8 个问题，全部在本轮修复：

| # | 自批评 | 竞品参照 | 本轮修复 |
|---|---|---|---|
| 1 | **Pod 日志完全没做**——集群管理头号功能，后端 WS 通道早就写好却没接 | 两家都放 C 位，实时流+容器切换 | ✅ PodLogViewer（WS 实时流） |
| 2 | 节点没有 CPU/内存用量条（`/nodes/metrics` 明明有） | Rancher 节点行内嵌 usage 条 | ✅ UsageBar 进度条 |
| 3 | 概览数字堆砌，卡片不可点、无容量可视化 | KubeSphere 四象限可下钻 | ✅ 三象限卡 + 容量 Progress |
| 4 | 工作负载 Tab 没有 namespace 筛选（Pods 有），不一致 | 全局 namespace 筛选 | ✅ 动态 ns 下拉（三 Tab 共享） |
| 5 | 工作负载不能看 YAML | Rancher 一切资源皆 YAML | ✅ YAML 抽屉（复用资源端点） |
| 6 | Pod 无状态筛选 | KubeSphere 按 Running/Pending 分组 | ✅ 状态分组筛选 |
| 7 | **Tab 不同步 URL**——刷新回概览、无法分享 | Rancher 每资源皆深链 | ✅ `?tab=` 同步 |
| 8 | 没有节点详情（后端有 Pod 分布/taints） | Rancher 节点页很厚 | ✅ 节点名可点开详情抽屉 |

## 一、新增功能

### Pod 实时日志查看器（`components/PodLogViewer.tsx`）★ 本轮核心

对接后端一次性 token 会话（`POST logs/session` → `ws_path?session_id&token`），
帧格式 `{type: log|system|error}`。功能对标 Rancher/KubeSphere：

- **容器切换**（多容器 Pod，容器清单来自 Pod 详情）
- 起始回看行数（tail 50-1000）、**previous** 开关（看崩溃重启前的日志）
- **暂停/继续**：暂停只停渲染不停传输，恢复时一次性补齐缓冲（上限 5000 行防 OOM）
- **自动滚动**：用户上翻自动关闭、贴底自动恢复（`onScroll` 判定）
- **关键字过滤**（前端 contains）+ 清屏 + **下载当前缓冲为 .log 文件**
- 连接状态徽标（连接中/实时接收中/流已结束/错误）；会话过期（4401）友好提示

配套修复：`vite.config.ts` 的 `/api` 代理补 `ws: true`——此前 K8s Pod 日志/Exec 的
WebSocket 升级会被开发代理挡掉（这也是这些 WS 路由一直没有前端使用方的原因之一）。

### 节点实时用量条 + 节点详情

- 列表新增 **CPU 用量 / 内存用量** 两列 Progress 条：capacity 解析自节点 status
  （`parseCpuToMillicores` / `parseMemToBytes`），usage 来自 `/nodes/metrics`
  （metrics-server）；未安装时整列优雅降级并在表头提示
- 节点名可点击 → 详情抽屉（Pod 分布 / taints / 资源分配率）

### 概览 Tab 重构（KubeSphere 风格）

- 节点 / 工作负载 / Pods **三张可点击统计卡**（大数字 + 副标题，异常值橙色），
  点击直接跳转对应 Tab（概览不再只是"看"）
- **资源容量卡**：Pod 水位进度条（>80% 红 / >60% 橙）+ CPU/内存容量说明
- 健康总览、Top 诊断问题、核心组件健康保留

### 一致性与深链

- **Tab 同步 URL**（`?tab=nodes`）：刷新不丢位置、可直接分享某个 Tab 的链接；
  非法参数回落概览
- **namespace 下拉三 Tab 共享**：页顶层一次拉取 `GET /namespaces`（5 分钟 staleTime），
  工作负载（新增）/Pods/资源浏览（新增动态项）统一
- 工作负载操作列补 **YAML 查看**；扩缩容 Modal 加 **± 步进器**

## 二、改动清单

| 文件 | 改动 |
|---|---|
| `components/PodLogViewer.tsx` | 新建（约 280 行，WS 日志流） |
| `pages/assets/clusters/detail.tsx` | 整体重写 v2（约 1100 行） |
| `api/index.ts` | +5 方法：nodeMetrics / nodeDetail / namespaces / podDetail / createLogsSession |
| `vite.config.ts` | `/api` 代理补 `ws: true` |

后端零改动——全部能力此前已就绪，本轮纯粹是把 UI/UX 补齐。

## 三、验证

- `tsc --noEmit` 零错误（修复 `ScrollerOutlined` 非法图标 → `VerticalAlignBottomOutlined`；
  `client` 改从 `@/api/client` 直连导入）
- `vite build` 通过
- 后端回归：cluster + finance 契约测试 34/34 通过

## 四、遗留（下轮候选）

- **Pod Exec 终端**：后端 exec/session + WS 全就绪，可复用日志查看器的会话模式
- 日志高亮（时间戳/级别着色）与多容器分栏对比
- 工作负载 → Pod 下钻（需后端按 ownerReferences 过滤）
- 集群列表快照定时刷新 runner + status 心跳
