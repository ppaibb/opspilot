# 数据库管理（类 Yearning 的 SQL 工单平台）

> 创建日期：2026-09-19  
> 状态：P0 完成（后端 11 路由 + 审核引擎 16 契约测试全绿；前端三 Tab 页面）

## 📊 功能定位

对标 [Yearning](https://github.com/cookieY/Yearning) 的 SQL 审核平台，叠加 OpsPilot 的
主机资产与脚本通道：**数据源管理 + SQL 工单（提交即自动审核 → 审批 → 执行）+ 只读自助查询**。

与 Yearning 的 P0 差异：审批流为固定两级（自动审核 + 人工批准），不做自定义流程/备份回滚
（P1）；无语句回滚（失败即停，已执行的自动提交，工单内注明）。

## 一、核心概念

| 概念 | 说明 |
|---|---|
| 数据源（实例） | MySQL（pymysql）/ PostgreSQL（psycopg 复用），密码加密落库不回传 |
| SQL 工单 | 提交 → **规则引擎自动审核（block 直接拒收）** → 人工审批 → 执行 → 逐条结果 |
| 自助查询 | 只读语句直接执行（≤500 行 / 60 秒），不建工单，全量落查询日志 |
| 分权 | 提交/执行（action.db.execute）与审批（action.db.approve）分离 |

## 二、SQL 审核规则引擎（`services/db_ops.py`，纯函数 16 测试）

**语句切分**：状态机跳过 `'...'`/`"..."`/`` `...` `` 字面量与 `--`/`#`/`/* */` 注释内的分号；
尾段纯注释自动丢弃。

**规则表**（block 拒收 / warn 放行提示）：

| 规则 | 级别 | 内容 |
|---|---|---|
| R0 | block | SQL 为空 |
| R1 | block | DROP DATABASE / SCHEMA |
| R2 | block | DROP TABLE |
| R3 | block | TRUNCATE |
| R4 | block | UPDATE/DELETE 无 WHERE |
| R5 | warn | SELECT * 建议明列 |
| R6 | warn | ALTER DROP COLUMN/INDEX（不可逆提示） |
| R7 | warn | 单语句 > 100KB 超大事务 |
| R8 | block | 查询工单/自助查询含非只读语句 |
| R9 | warn | 无法识别的语句类型 |

**关键防御**：规则匹配前先做**字符串字面量脱敏**（`_desensitize`）——
`INSERT INTO t VALUES ('drop table users;')` 里的关键词是数据不是语句，不误伤（有测试覆盖）。

## 三、执行引擎

- 变更：逐条执行自动提交，失败即停（记录已执行条目与错误）；执行超时 60 秒（驱动层）
- 查询：只执行首条（多语句拒绝），结果集截断 500 行，bytes/Decimal/datetime 转 JSON 安全类型
- 连接：每次执行新建用完即关（工单低频，不做连接池）

## 四、API（`/api/db`，11 路由，权限 `module.assets.databases`）

| 路由 | 说明 |
|---|---|
| GET/POST `/instances`、PUT/DELETE `/{id}`、POST `/{id}/test` | 数据源 CRUD + 连通测试（SELECT VERSION()） |
| GET/POST `/tickets` | 工单列表 / 提交（block 级 422 拒收并返回明细） |
| POST `/tickets/{id}/review` | 审批（approve→待执行 / reject→驳回，状态机校验） |
| POST `/tickets/{id}/execute` | 执行已批准工单（逐条结果落库） |
| POST `/query`、GET `/query-logs` | 自助查询（复用 R8 只读校验）+ 审计日志 |

RBAC：`module.assets.databases` + `action.db.manage/execute/approve`（运维角色已授权）。

## 五、前端（`pages/assets/databases/index.tsx`，三 Tab）

1. **SQL 工单**：状态筛选 + 列表（类型/实例/状态机 Tag/三方角色）→ 提交 Modal
   （实例 + 类型 + SQL 编辑区，提交即审核）→ 操作（批准/驳回/执行/详情抽屉：
   审核明细 + SQL + 逐条执行结果表）
2. **自助查询**：实例选择 + SQL 编辑 + 结果表（动态列）+ 耗时/行数提示 + 查询审计表
3. **数据源**：CRUD + 连通测试（状态徽标带原因 Tooltip）

菜单：资产管理 → 数据库管理。

## 六、验证与依赖

- `test_db_ops_contract` 16/16（切分/分类/只读判定/R1-R9/JSON 序列化）
- tsc 零错误、vite build 通过
- ⚠️ **新增依赖 `pymysql>=1.1.0`**（MySQL 驱动；PG 复用 psycopg）
- Alembic：`20260919_create_database_tables`（db_instances / db_tickets / db_query_logs）

## 七点六、体验补齐（2026-09-20 第二轮）

1. **库表浏览**（回应"选数据源要能带出库表"）：`GET /instances/{id}/databases`、
   `GET /instances/{id}/tables?database=`（含行数估计）；
   提交工单 Modal 左侧库表面板（点表名插入 `库`.`表`）、查询 Tab 同款浏览器 +
   目标库下拉（连接级切库）；工单/查询均支持 database 覆盖（db_tickets 新列）。
2. K8s 可视化编辑见 27 号补记。

## 七点五、深化增强（2026-09-20，回应「简陋」批评）

1. **实例级使用授权**：`allowed_roles`（JSON 角色数组，空=全员）——未授权角色不能把该库作为工单/自助查询目标（403）；前端授权列 + 表单多选。审计说明：全局审计中间件已自动记录全部 POST/PUT/DELETE（含数据源增删改、审批、执行），系统设置→审计日志可查。
2. **审核规则可配置**（Yearning 审核设置）：R1-R9 开关 + block/warn 级别调整，存 system_config，保存即生效；前端「审核规则」Tab。
3. **定时执行**：工单可选 execute_after（低峰变更），批准后由后台 scheduler（30 秒扫描）到点自动执行（含备份/通知，与手动执行同路径）。
4. **执行结果站内通知**：执行成功/失败推送给提交人（复用通知中心，SSE 实时）。
5. **变更安全网**：UPDATE/DELETE 执行前自动 SELECT 受影响行快照（上限 1 万行）+ 反向 INSERT 回滚语句；工单详情/列表一键下载 rollback SQL；JOIN 等复杂语句不备份不阻塞（人工把控）。

新增契约测试 7 例（合计 23 全绿）；迁移 `20260920_db_enhance`。

## 七、P1 规划

- 语句级回滚（事务包裹选项）、定时执行、备份快照
- Monaco SQL 编辑器（高亮/自动补全）、Explain 影响行数预估
- 自定义审批流（多级/会签）、自动审批白名单（低危 DML）
