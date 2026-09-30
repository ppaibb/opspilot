# 39-CMDB关联图谱与运维入口补全

> 日期：2026-09-23  
> 范围：CMDB 关联图谱（应用↔资产）、SSH 指纹管理入口、费用 CSV 导出

## 背景

业务缺口分析选定「CMDB 关联图谱」为主线，并清掉两件挂账小事（SSH 指纹入口未上线、费用 CSV 未导出）。变更工单/故障应急第一版不做。

## 变更清单

### 1. CMDB 关联图谱

**后端** `backend/app/api/cmdb.py`（挂载 `/api/cmdb`，权限 `module.apps.list`）
- `GET /graph`：应用节点（含 owner/environment/category/tags）+ 资产节点（host/vm/domain/certificate）+ 绑定边  
  支持按环境 / 负责人过滤；已删除但仍绑定的资源会画成 `missing` 悬空节点，避免影响面漏报
- `GET /impact`：节点影响面 —— 关联应用、责任人、涉及环境、资源明细  
  应用节点正查绑定资源；资产节点反查关联应用

**前端** `frontend/src/pages/assets/cmdb/index.tsx`
- 路由 `/assets/cmdb`（侧边栏「资产管理 → 关联图谱」）
- echarts force 图：应用蓝 / 主机绿 / 虚机紫 / 域名橙 / 证书粉，点击节点看影响面
- 环境下拉 + 负责人搜索 + 节点/关系计数

数据来源是已有的 `applications` + `application_resources`，**不新建关系表**；应用详情里绑定的主机/域名/证书会直接出现在图上。

### 2. SSH 指纹管理入口

- 后端：`GET /api/assets/host-keys`（admin，可按 host 过滤）列出 `asset_host_keys` 钉扎记录  
  （`POST /api/assets/{id}/reset-host-key` 原本就有）
- 前端主机页工具栏新增 **「SSH 指纹」**：抽屉展示 host/port/类型/指纹/首连/最近校验，可清除钉扎（匹配到主机资产才可点）

### 3. 费用 CSV 导出

- 后端：`GET /api/finance/bills/export` —— 与 `/bills` 同筛选（月/账号/产品/关键词/计费方式），不分页，金额降序；UTF-8 BOM，Excel 直开不乱码
- 前端账单页 **「导出 CSV」** 按钮，按当前筛选下载

## 验证

- `test_cmdb_graph_contract.py` 6 例全绿（图谱拼装/环境过滤/影响面/指纹模型/导出路由）
- `scripts/run_checks.py` **4/4 通过**：后端 **597 passed + 96 subtests**，pyflakes 0 undefined name，前端 lint 0 error，build 通过

## 第一版边界（刻意不做）

- 变更工单 / 故障应急
- 新建 CMDB 关系表、重要等级字段、成本/告警叠加到节点（影响面接口已预留扩展位）
- 资产类型扩到集群/数据库（`RESOURCE_TYPES` 仍是 host/vm/domain/certificate）

## 后续建议

1. 应用绑定资源类型扩到 cluster/database，图谱自动纳入
2. 节点卡片叠加本月费用与活动告警数（`/finance/instance/{id}/cost` 已有）
3. 从图谱一键发起变更/建工单（等变更主线）
