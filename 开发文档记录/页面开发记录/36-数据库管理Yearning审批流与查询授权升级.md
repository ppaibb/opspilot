# 36-数据库管理 Yearning 审批流与查询授权升级

> 日期：2026-09-21
> 范围：数据库管理（/assets/databases）
> 目标：复用现有 RBAC、站内通知、SSE 与飞书群 @ 能力，把数据库管理从单级 SQL 工单提升为 Yearning 风格的提交-审批-执行与查询授权闭环。

## 一、功能交付

### 1. 三权分离

新增 `action.db.submit`，与已有 `action.db.approve`、`action.db.execute` 分开：

- `submit`：提交变更工单、提交查询授权申请、自助只读查询；
- `approve`：审批工单；
- `execute`：执行已批准变更工单、导出回滚语句；
- `manage`：管理数据源、规则、审批流、查询授权。

旧角色兼容：历史角色拥有 `action.db.execute` 时，RBAC 解析会自动补齐 `action.db.submit`，升级后不会意外失去提交能力。

### 2. 自审批防护

后端审批状态机禁止提交人审批自己的工单（同时匹配提交人的用户名和展示名）；管理员角色保留运维兜底豁免。前端只向当前审批级且具备审批权限的用户展示审批按钮，后端校验作为最终安全边界。

待审批通知也排除提交人本人，避免产生“通知自己审批自己”的误导。通知仍复用 `create_notification()`：站内信、SSE 和飞书个人渠道自动复用，无需另起 IM 通道。

### 3. 多级审批流

新增审批流配置接口与页面 Tab：

- `GET/PUT /api/db/approval-flows`；
- 变更工单、查询授权工单分别配置角色链；
- 角色链按顺序逐级审批，当前级批准后通知下一角色；
- 任一级驳回立即终止；
- 空审批链保持历史单级模式；
- 工单提交时快照审批链，后续配置变化不影响在途工单；
- 工单详情抽屉展示 Steps、当前级、逐级审批人、意见和时间。

### 4. 查询授权闭环

查询工单不再按变更工单执行，而是：

1. 提交人填写目标实例、目标库、可选表范围、授权小时数、申请理由；
2. 通过 R8 只读边界和多级审批；
3. 终审通过自动创建 `db_query_grants`；
4. 授权到期由 scheduler 自动改为 `expired`；
5. 自助查询必须拥有本人名下、目标实例+目标库匹配、尚未过期的授权；
6. 管理员可手动授权、提前吊销；吊销会走站内信 + SSE + 飞书通知。

授权接口：

- `GET /api/db/query-grants?scope=mine|all`；
- `POST /api/db/query-grants`；
- `POST /api/db/query-grants/{id}/revoke`。

查询授权表记录来源工单、被授权人、目标库、表范围、理由、过期时间、授权人、状态，满足审计要求。

### 5. 数据字典

库表浏览器新增表结构按钮和抽屉：

- 字段名、类型、可空、默认值、主键标记、注释、额外属性；
- 索引名、字段、唯一性 / 索引定义；
- MySQL 显示 `SHOW CREATE TABLE` DDL；
- PostgreSQL 显示字段注释与索引定义。

接口：`GET /api/db/instances/{id}/tables/{table}/schema?database=...`。标识符做反引号转义，库名使用参数绑定，避免把数据字典接口变成注入入口。

## 二、数据模型

`DbTicket` 增加：

- `approval_steps`：提交时的角色链快照；
- `current_step`：当前审批级；
- `approval_log`：逐级审批记录；
- `grant_detail`：查询授权申请信息。

新增 `DbQueryGrant`，由 `app/db/models.py` 注册，开发库启动时由 `ensure_model_columns()` / `create_all` 负责补列或建表；生产环境仍应使用 Alembic。

## 三、页面体验

数据库管理页从四个 Tab 升级为六个 Tab：

1. SQL 工单；
2. 自助查询；
3. 查询授权；
4. 数据源；
5. 审批流；
6. 审核规则。

布局保持现有 Ant Design 5 规范：顶部概览卡片、Tab 待办角标、Drawer 表单、Table 列表、App.useApp() 消息与确认、响应式 Row/Col。查询页会显示当前库的授权状态和过期时间，未授权时明确引导提交授权工单，不让用户在执行后才看到模糊 403。

## 四、IM 复用说明

没有新增 IM 依赖，也没有复制发送逻辑：所有数据库业务通知统一调用 `create_notification()`，由现有通知管道自动完成：

- `user_notifications` 持久化；
- SSE 实时推送；
- 已绑定用户的飞书群机器人 @ 通知；
- 站内通知链接统一指向 `/assets/databases`。

## 五、验证

- 后端新增 `tests/test_database_rbac_contract.py`：28 项专项契约测试；
- 全量后端：`411 passed`；
- 前端：`pnpm build` 通过；Rolldown 仅保留既有 chunk size / dynamic import warning；
- HTTP 冒烟：`GET /api/db/approval-flows` 返回变更/查询空审批链与角色列表；`GET /api/db/query-grants?scope=mine` 返回授权列表。

## 六、后续人工验收清单

- [ ] 用无 `action.db.submit` 用户确认提交和自助查询按钮禁用；
- [ ] 用不同角色配置两级审批，验证每级只看到自己的审批按钮；
- [ ] 验证提交人不能自审，管理员角色豁免；
- [ ] 提交查询授权工单，终审后确认授权列表、查询页授权提示和过期回收；
- [ ] 配置飞书个人渠道后验证审批、授权、吊销通知能到站内信和飞书；
- [ ] 在库表浏览器点击表结构，确认字段、索引、DDL 抽屉排版和复制按钮；
- [ ] 在 PostgreSQL 与 MySQL 各抽查一次数据字典响应。
