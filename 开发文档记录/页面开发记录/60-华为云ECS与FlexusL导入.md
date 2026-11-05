# 华为云 ECS 与 Flexus L 导入

日期：2026-11-05。分支：`codex/huawei-cloud-import`。

## 功能与入口

1. 凭证管理 → 新增凭证 → 类型选「云厂商 AK/SK」，填写华为云 AK/SK。
2. 主机管理 → 云同步 → 华为云 → 选择已保存凭证（或临时输入 AK/SK）→ 获取主机。
3. 在「云服务器」和「轻量服务器」Tab 查看 ECS / Flexus L，跨 Tab 勾选后统一导入。

凭证继续使用 `AccessKey` 类型及 JSON content，不新增不兼容的凭证类型。新增/修改仍由后端加密，编辑页不回显。云 API 凭证与 SSH 登录凭证独立。

## 架构与安全审计

实现前的架构图、风险和计划见 [实现计划](../../docs/superpowers/plans/2026-11-05-huawei-cloud-import.md)。

- IAM `KeystoneListAuthProjects` 获取当前身份授权的区域项目及子项目。
- ECS `ListServersDetails` 按 offset **页码**分页，并传 `enterprise_project_id=all_granted_eps`，兼容只拥有企业项目权限的用户。
- RMS `ListAllResources(type=hcss.l-instance)` 按 marker 分页，读取套餐内 `huaweicloudinternal_ecs_instance` 的 `physical_resource_id`，不是套餐 ID 或流量包 ID。
- 通过物理云主机 ID 与 ECS 列表合并；列表缺少的轻量主机使用 `ShowServer` 查询详情。同一物理主机只出现一次。
- Flexus L 的系统盘容量取 `resource_attributes.root_volume`；普通 ECS 的 flavor.disk 官方标为无效，不误报容量，后续可通过现有 SSH 配置采集补齐。
- 新增资产字段 `cloud_account_id` / `cloud_project_id`。重复导入更新云端元数据，保留 SSH 凭证、登录用户、分组和备注；不同厂商、账号、项目或区域的同 ID 资产禁止覆盖。
- 预览不写资产库；导入重新查询云端，整批验证所选实例存在和身份一致后提交。并发重复导入由数据库唯一约束兜底。
- 复用主机管理权限与凭证授权组，明确标记 expired 的凭证拒绝使用；保留现有 AK/SK 日期仅用于预警的约定。
- 固定 SDK 内置官方端点、开启 TLS 校验、关闭重定向，使用有效的 `(5, 15)` 连接/读取超时与分页上限。
- 云端仅执行查询，不执行开关机/删除等变更。SDK 异常经过脱敏；RMS 原始属性不透传前端。
- 凭证引用数同时统计 SSH 与云 API 凭证，防止正在被云主机引用的凭证被误认为未使用。

## 前端行为

补齐华为云来源标签、筛选与 Flexus L 轻量分类；跨 Tab 勾选不会覆盖另一类主机的选择。华为云已导入实例可再次勾选更新。

扫描失败区域自动重试一次，部分项目失败的警告常驻预览，最终统计使用本轮任务快照，避免旧闭包误报成功。扫描/导入中禁止修改凭证和关闭弹窗，退出或成功后清理临时 AK/SK。

## 数据库与依赖

SDK 固定版本 `3.1.220`：`huaweicloudsdkcore`、`huaweicloudsdkecs`、`huaweicloudsdkiam`、`huaweicloudsdkrms`。

开发库已按启动补列规则仅补齐本次两列，没有运行 Alembic，也没有修改业务资产或凭证。生产环境先安装更新后的 requirements，再执行 `alembic upgrade head` 并重启后端；前端重新构建。迁移使用项目已有幂等守卫，兼容旧库及当前模型已建列的新库。

IAM 用户需要访问自身授权项目及 ECS 查询权限；Flexus L 识别还需要配置审计资源查询权限（`rms:resources:list`）。优先使用最小权限 IAM 用户密钥。

## 验证

- 新增华为云后端行为/ASGI/迁移测试：27 passed。
- 后端全量：780 passed，119 subtests passed，1 skipped；保留现有依赖弃用警告。
- 全量未定义名称检查、前端 lint / build：通过；lint 只有现有 7 条 warning。
- 前端 Vitest：56 passed（含新增跨 Tab 选择及 Flexus L 分类测试）。
- `pip check`：无依赖冲突；Alembic 单 head `20261105_huawei_identity`。
- Playwright 桌面浏览器：3 个用例通过，覆盖保存 AK/SK → 扫描 → ECS/Flexus L 跨 Tab 勾选 → 导入后刷新，以及部分项目失败、配置审计无权限两条分支。

浏览器测试使用明确的模拟云 API，不使用真实云账号；ASGI 用例使用真实 SQLite 写入与权限依赖，仅替换外部云服务及测试用户身份。未发现已保存的华为云凭证，**尚未完成真实华为云账号联调**，不能将以上结果等同于真实云端验收。

## 官方参考

- [Flexus L 列举套餐与物理资源](https://support.huaweicloud.com/api-flexusl/renewal_instance_0001.html)
- [Flexus L 云主机详情](https://support.huaweicloud.com/api-flexusl/query_server_0001.html)
- [ECS 详情列表与企业项目权限](https://support.huaweicloud.com/api-ecs/zh-cn_topic_0094148850.html)
- [华为云 Python SDK](https://github.com/huaweicloud/huaweicloud-sdk-python-v3/blob/master/README_CN.md)
