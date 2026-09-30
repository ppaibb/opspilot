# K8s 集群实时动态（SSE Watch 前端落地）

> 创建日期：2026-09-19  
> 状态：完成（后端 49 测试全绿、tsc 零错误、vite build 通过）

## 📊 交付总结

补上集群管理最后一块遗留：**SSE Watch 前端消费**。详情页头部新增「实时动态」入口，
打开即订阅 Informer 推送的资源变更流——对标 `kubectl get -w` 的 UI 化，
也是 Rancher/KubeSphere 集群页的标配能力。

**顺带修复一个安全漏洞**：`POST /watch/session` 原本挂载在无鉴权的 router 上，
匿名用户可领取 token 观察集群资源变更。现补登录校验
（token 本身走 URL 传输是 SSE 限制，所以签发入口必须先过认证，泄露窗口 90 秒且一次性）。

## 一、协议（既有后端，本轮零接口改动）

```
POST /api/clusters/{cid}/watch/session   → {token}（一次性，90s TTL，本轮起需登录）
GET  /api/clusters/{cid}/watch?token&kinds&namespace  → SSE 流
  event: snapshot  Informer 缓存初始快照（前端只展示统计数，防初次刷屏）
  event: watch     {type: ADDED/MODIFIED/DELETED, kind, namespace, name, resourceVersion, object}
  event: ping      30s 心跳
```

订阅走 `InformerManager`（SharedInformer）：每 (cluster, kind) 一条 Watch 长连接、
多订阅者共享缓存、无订阅者 60 秒后自动停流——前端断开即释放服务端资源。

## 二、前端组件（`components/ClusterLiveFeed.tsx`）

- **连接策略**：fetch 流式读取（与日志 Live Tail 同模式）。关键点：token 一次性，
  `EventSource` 的自动重连必然 401，因此组件内**手动重连**——断流后换新 token
  重连、连续失败退避（1s→5s），5 次后停止并提示
- **三种事件帧**解析：snapshot → 统计数提示；watch → 事件列表；ping → 忽略
- 交互：
  - **kind 多选**（14 种，默认订阅 Pod/Deployment/StatefulSet/DaemonSet/Service/Ingress，
    Event 量大默认不选）+ **namespace 筛选**（复用页面级清单）——变更即重连
  - **暂停/继续**（暂停只停渲染不停接收，继续时补齐缓冲）、清屏
  - 关键字过滤（名称/类型）、新增/变更/删除**计数统计**、环形缓冲 500 条
  - 事件行：色 Tag（新增绿/变更蓝/删除红）+ Kind + ns/name + 时间，最新在上
- 生命周期：关闭抽屉即 abort 请求并清空状态；kinds/namespace 变更重开会话

## 三、接入点

详情页头部新增「实时动态」按钮（⚡ 图标），与「YAML 部署」并列。

## 四、测试隔离性修复（附带）

合并运行测试套件时发现：`test_finance_contract` / `test_finance_api_e2e` 的
`Base.metadata.create_all` 全量建表，在其他模块导入链注册了 JSONB（PG 专属）模型后
会在 SQLite 上编译失败。修复：两处改为**按需建表**（`tables=[...]` 只建自己需要的表），
合并运行 49/49 全绿。

## 五、验证

| 项 | 结果 |
|---|---|
| 后端合并回归（namespace/rollout/health/drain/exec/ws-router/finance） | 49/49 |
| 前端 tsc / vite build | 零错误 / 通过 |
| 图标导出校验 | 通过 |

## 六、遗留

- Watch 事件与具体 Tab 联动刷新（如收到 Deployment MODIFIED 自动失效工作负载 query）——
  当前实时动态为独立面板，避免过度刷新
- Secret 事件的脱敏展示（后端 object 摘要已剔除 data，前端无需处理）
