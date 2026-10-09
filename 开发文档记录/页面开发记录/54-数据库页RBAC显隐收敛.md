# 54 - 数据库页 RBAC 显隐收敛：管理入口对普通用户隐藏（2026-10-09）

> 需求原话：「按照我们的 rbac 设计，非 dba 和系统管理员能看到添加数据源这个操作吗？
> 普通用户按理说只能看到提交工单和自助查询才合理 对不对」→「同意你这个修法 我想
> 隐藏起来比较好」。
>
> 结论：设计意图与后端强制本就一致（四把钥匙分权、路由全硬挂）；缺口在前端
> **「只禁用不隐藏」+ 管理类 Tab 无显隐门**。本轮纯前端收敛，后端零改动。
> 用「页面权限 + submit、无 manage」测试账号实测：只见 工作台/SQL 工单/自助查询，
> 后端 403 抽查通过；admin 视野不变。检查-全量 **4/4 全绿**。

---

## 一、现状三层核查（改动前）

| 层 | 状态 |
|---|---|
| 后端路由 | **无缺口**：instances 增删改/测试连接全挂 `action.db.manage`；提单/自助查询挂 `submit`；审批/执行各挂各的；授权/审批流/审核规则挂 `manage`（database.py 282-1435） |
| 角色模板 | 默认只有 管理员（全量）/运维（db 四件套全有）能进页面；只读/开发/域名管理员连 `module.assets.databases` 都没有——**默认模板下不出现该场景** |
| 前端 | 三个缺口：① 添加/编辑/删除/测试连接 `disabled={!canManage}`（可见的灰按钮）；② 七个 Tab 无显隐门，普通用户能逛审批流/审核规则/数据源；③ 工作台开工卡两个按钮没挂 `submit` 门 |

触发条件：管理员给自定义角色发 `module.assets.databases + action.db.submit`
（不发 manage）——这正是「能提单的普通用户」的合理发法。

## 二、修法（纯前端）

| 位置 | 改动 |
|---|---|
| `index.tsx` | 查询授权/数据源/审批流/审核规则四个管理 Tab 收进 `canManage` 条件；普通用户只剩 工作台/SQL 工单/自助查询 |
| `InstancesPanel` | 测试/编辑/删除/新增实例四个按钮从 `disabled` 改为 `canManage &&` 条件渲染（不渲染） |
| `TicketsPanel` | 提交工单按钮从禁用改隐藏；空数据源提示分角色——管理态「请先到数据源 Tab 添加」/普通态「请联系管理员添加」 |
| `WorkbenchPanel` | 开工卡按 `action.db.submit` 整卡显隐（无 submit 的角色连卡都看不到） |

连带处理：页底「连接异常」告警的「去查看」按钮对无 manage 用户不渲染（目标 Tab 已隐藏）；
`GET /instances` 只要登录，藏 Tab 不影响提单表单的实例下拉（数据在页面层照常加载）。

## 三、验证（235，真实测试账号）

1. admin 建测试账号 `normal01`：角色「只读」+ `effective_permissions`
   `{module.assets.databases, action.db.submit}`（标准"能提单的普通用户"配置）；
2. **normal01 浏览器实测**：Tab 栏只有 工作台/SQL 工单/自助查询；开工卡在、
   「提交 SQL 工单」按钮在；SQL 工单 Tab「提交工单」在（灰=无数据源的数据态，
   非权限态）；空数据源提示显示「请联系管理员添加」；0 失败请求；
3. **后端强制抽查**：normal01 `POST /api/db/instances` → **403**；
   `GET /instances`、`GET /tickets` → 200（读正常）；
4. **admin 回归**：七个 Tab 全在、数据源 Tab「新增实例」在——管理员视野无损；
5. 清理：normal01 经 `users:delete` 风险票据流程删除（登录回 401）；
6. lint 0 error、vitest 52 passed、检查-全量 **4/4 全绿**。

## 四、涉及文件

- `frontend/src/pages/assets/databases/index.tsx`（管理 Tab 显隐）
- `frontend/src/pages/assets/databases/InstancesPanel.tsx`（四个管理按钮隐藏）
- `frontend/src/pages/assets/databases/TicketsPanel.tsx`（提交按钮隐藏 + 分角色提示文案）
- `frontend/src/pages/assets/databases/WorkbenchPanel.tsx`（开工卡按 submit 显隐）
