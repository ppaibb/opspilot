# 集群管理 CRUD 完备性审计与补全

> 创建日期：2026-09-19  
> 状态：完成（CRUD 矩阵无缺口；tsc 零错误、vite build 通过、后端 29 测试全绿）

## 📊 PM 视角 CRUD 审计结论

对集群管理逐资源盘点增删改查，发现 **3 个真实缺口**，全部补齐：

| 资源 | 增 | 删 | 改 | 查 | 本轮动作 |
|---|---|---|---|---|---|
| 集群 | ✅ 纳管 | ✅ 解除 | ✅ PUT | ✅ 列表/详情 | 无缺口 |
| 节点 | N/A（集群自管） | N/A | ✅ labels/taints/cordon | ✅ +metrics | 无缺口 |
| 工作负载 | ✅ YAML 途径 | ✅ | ⚠️ 回滚不能选版本 | ✅ | ✅ 逐版回滚 |
| Pod | N/A（控制器重建） | ✅ | N/A | ✅ 日志/终端/诊断 | 无缺口 |
| **命名空间** | ✅ | ❌ **无删除接口** | ❌ **无标签编辑** | ✅ | ✅ 后端+前端补齐 |
| **通用资源 40 种** | ✅ YAML | ✅ | ❌ **YAML 只读** | ✅ | ✅ 在线编辑 |
| CronJob | ✅ YAML | ✅ | ✅ 触发/挂起 | ✅ Jobs | 无缺口 |

**关键判断**：后端 `apply` 是标准 **server-side apply（create-or-update）语义**，
因此通用资源的"改"不需要新协议——编辑现有资源 YAML 后重新 apply 即可，
与 `kubectl apply -f` 完全同构。

## 一、后端新增（`app/api/clusters.py`）

1. **`DELETE /{cluster_id}/namespaces/{ns}`**
   - 保护名单 `PROTECTED_NAMESPACES`（kube-system / kube-public / kube-node-lease /
     default）直接 403——这些命名空间承载核心组件，误删会瘫痪集群
   - K8s 删除是异步级联（Terminating），响应明确告知
   - 权限 `action.assets.clusters.manage` + 风险确认网关（delete 已是 high）
2. **`PATCH /{cluster_id}/namespaces/{ns}/labels`**
   - 增量合并语义（value=null 删除 key），与节点标签一致
   - `kubernetes.io/`、`k8s.io/` 前缀自动跳过（调度/网络插件依赖保护）
3. **纯函数 `_check_namespace_deletion_allowed`**：保护判断独立出来便于单测
   （删除路由本身需要真实集群连接，与项目既有测试策略一致）

## 二、前端改动（`detail.tsx`）

1. **命名空间 Tab**：操作列 +「编辑标签」抽屉（每行 k=v、-key 删除，同节点语义）
   +「删除」按钮（Popconfirm 强警告"级联删除不可恢复"+ 风险确认）
2. **YAML 在线编辑**：资源浏览 YAML 抽屉加「编辑」模式——
   编辑态 TextArea →「保存并应用」（风险确认）→ server-side apply；
   失败时透传 dry-run 校验错误；编辑中标题显示"编辑中"Tag
3. **逐版回滚**：发布历史抽屉每个修订卡加「回滚到此版本」
   （后端 `rollout-undo` 本就支持 `{revision}`，此前 UI 只能盲回上一版）

## 三、编码规范自查（Google 风格 + 中文注释）

- 后端新路由：中文 docstring 说明**安全边界与语义**（如"级联删除是异步的"）、
  保护逻辑抽纯函数并注释为什么（"便于单测覆盖"）
- 前端新组件：文件头模块注释讲设计动机；`NamespaceLabelsEditor` 与节点标签
  共用语义在注释中显式声明；风险确认/缓存失效等关键行内注释解释 why
- 命名：`_check_namespace_deletion_allowed` / `PROTECTED_NAMESPACES` /
  `NamespaceLabelsEditor` 等自解释命名，无缩写

## 四、验证

| 项 | 结果 |
|---|---|
| 后端新增契约测试（保护名单 4 例 + 标签合并语义） | 29/29 全绿 |
| 前端 tsc / vite build | 零错误 / 通过 |

## 五、遗留（明确不做或后置）

- 工作负载**表单式创建**（Rancher 式 Deploy 表单）：YAML 工作台已覆盖 C 语义，
  表单属体验增强而非 CRUD 缺口，后置
- SSE Watch、HPA 独立页：见 22 号文档遗留节
