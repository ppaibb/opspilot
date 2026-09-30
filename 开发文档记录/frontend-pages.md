# OpsPilot 前端页面功能清单

> 初始生成于 2026-07-23，最后更新于 2026-08-13。

---

## 登录页（`/login`）
- 功能：系统登录
- 表单：用户名 + 密码输入，提交登录
- API 接口：`GET /api/settings/login-page`（加载登录页配置），`POST /api/auth/login`（登录认证）
- 关键交互：表单提交，登录失败提示

---

## 首页仪表盘（`/`）
- 功能：全局概览仪表盘
- API 接口：`GET /api/dashboard/overview`
- 关键交互：
  - **统计卡片**：资产总数、主机数、虚机数、集群数、域名数、证书数等概览指标
  - **资产汇总**：按类型分组的资产统计
  - **快捷操作**：跳转到常用功能入口（新增主机、新增虚机、Agent 工作台等）
  - **近期告警 / 事件时间线**

---

## 资产管理

### 主机管理（`/assets/hosts`）
- 功能：主机资产的增删改查
- API 接口：
  - `GET /api/assets`（列表）
  - `POST /api/assets`（新增）
  - `PUT /api/assets/{id}`（更新）
  - `DELETE /api/assets/{id}`（删除）
  - `POST /api/assets/{id}/ssh-test`（SSH 连通测试）
  - `POST /api/assets/{id}/sync`（同步主机信息）
  - `GET /api/assets/{id}/detail`（详情）
  - `GET /api/groups`（分组列表）
  - `POST /api/groups`（新增分组）
  - `PUT /api/groups/{id}`（更新分组）
  - `DELETE /api/groups/{id}`（删除分组）
  - `POST /api/groups/{gid}/assign`（分配资产到分组）
- 关键交互：
  - **表格**：主机列表，支持搜索、筛选（分组/角色）、排序
  - **抽屉**：新增/编辑主机表单（名称、IP、端口、凭证、角色、操作系统等）
  - **分组管理侧栏**：树形分组，拖拽分配
  - **操作按钮**：SSH 测试、同步、编辑、删除
  - **导入/导出**

### 虚拟机管理（`/assets/vms`）
- 功能：虚拟机资产的增删改查、电源控制（开机/关机）、虚拟机克隆、云端同步
- API 接口：
  - `GET /api/vms`（列表）
  - `POST /api/vms`（新增）
  - `PUT /api/vms/{id}`（更新）
  - `DELETE /api/vms/{id}`（删除）
  - `POST /api/vms/{id}/power`（电源控制：start/stop）
  - `POST /api/vms/{id}/clone`（虚拟机克隆）
  - `POST /api/vms/sync`（云端同步）
- 关键交互：
  - **概览统计卡片**：总数、运行中、已停止、CPU 总算力卡片
  - **表格**：虚拟机列表，支持名称/IP搜索与规格展示
  - **抽屉与 Modal**：新增/编辑抽屉、快捷克隆 Modal
  - **操作按钮**：开机、关机、克隆、编辑、删除

### 集群管理（`/assets/clusters`）
- 功能：集群资产的增删改查
- API 接口：
  - `GET /api/assets`（列表）
  - `POST /api/assets`（新增）
  - `PUT /api/assets/{id}`（更新）
  - `DELETE /api/assets/{id}`（删除）
  - `GET /api/groups`（分组列表）
  - `POST /api/groups`（新增分组）
  - `PUT /api/groups/{id}`（更新分组）
  - `DELETE /api/groups/{id}`（删除分组）
  - `POST /api/groups/{gid}/assign`（分配资产到分组）
- 关键交互：
  - **表格**：集群列表，支持搜索、筛选（分组/集群类型）、排序
  - **抽屉**：新增/编辑集群表单
  - **分组管理侧栏**
  - **操作按钮**：编辑、删除

### 凭证管理（`/assets/credentials`）
- 功能：SSH 密码/密钥、API Token 等凭证的管理
- API 接口：
  - `GET /api/credentials`（列表）
  - `POST /api/credentials`（新增）
  - `PUT /api/credentials/{id}`（更新）
  - `DELETE /api/credentials/{id}`（删除）
- 关键交互：
  - **表格**：凭证列表，支持搜索、筛选（类型）、排序
  - **抽屉**：新增/编辑凭证表单（类型切换：密码/密钥/API Token）
  - **操作按钮**：编辑、删除

### 域名管理（`/assets/domains`）
- 功能：域名资产的增删改查，DNS 解析记录展示
- API 接口：
  - `GET /api/assets`（列表）
  - `POST /api/assets`（新增）
  - `PUT /api/assets/{id}`（更新）
  - `DELETE /api/assets/{id}`（删除）
  - `GET /api/groups`（分组列表）
  - `POST /api/groups`（新增分组）
  - `PUT /api/groups/{id}`（更新分组）
  - `DELETE /api/groups/{id}`（删除分组）
  - `POST /api/groups/{gid}/assign`（分配资产到分组）
- 关键交互：
  - **表格**：域名列表，支持搜索、筛选（分组）、排序
  - **抽屉**：新增/编辑域名表单
  - **分组管理侧栏**
  - **操作按钮**：编辑、删除

### 证书管理（`/assets/certificates`）
- 功能：SSL 证书托管、自动续期配置、部署跟踪
- API 接口：
  - `GET /api/certificates`（列表）
  - `POST /api/certificates`（新增）
  - `PUT /api/certificates/{id}`（更新）
  - `DELETE /api/certificates/{id}`（删除）
  - `POST /api/certificates/{id}/renew`（手动续期）
  - `POST /api/certificates/{id}/revoke`（吊销）
  - `GET /api/groups`（分组列表）
  - `POST /api/groups`（新增分组）
  - `PUT /api/groups/{id}`（更新分组）
  - `DELETE /api/groups/{id}`（删除分组）
  - `POST /api/groups/{gid}/assign`（分配资产到分组）
- 关键交互：
  - **表格**：证书列表，支持搜索、筛选（类型/域名）、排序，证书到期预警高亮
  - **抽屉**：新增/编辑证书表单
  - **分组管理侧栏**
  - **操作按钮**：续期、吊销、编辑、删除

### WebSSH 终端与 SFTP 管理（`/assets/terminal` & `TerminalModal`）
- 功能：企业级 SSH 伪终端长连接会话与全功能 SFTP 远程文件管理器
- API 接口：
  - `POST /api/assets/terminal/session`（换取 session_token 与 ws_path）
  - `GET /api/assets/terminal/files/{asset_id}/list`（SFTP 目录文件列表）
  - `POST /api/assets/terminal/files/{asset_id}/mkdir`（SFTP 新建远程目录）
  - `POST /api/assets/terminal/files/{asset_id}/upload`（SFTP 文件上传）
  - `DELETE /api/assets/terminal/files/{asset_id}`（SFTP 远程文件/目录删除）
  - `GET /api/assets/terminal/files/{asset_id}/download`（SFTP 远程文件安全下载）
- 关键交互：
  - **形态一（工作区页面 `/assets/terminal`）**：多会话 Tab 并列切换，可折叠主机侧边栏。
  - **形态二（macOS 悬浮 / 全屏 Modal `TerminalModal`）**：居中 1020px * 640px 弹窗，支持全屏与右下角最小化挂起。
  - **🤖 AI 运维命令助手**：自然语言意图输入，智能拼装 Linux 命令一键注入终端并执行。
  - **⚡ 常用快捷指令**：预设 `uname`, `free -h`, `df -h`, `docker ps` 等指令一键执行。
  - **🎨 4款主题皮肤**：夜空深蓝 (Slate)、极客矩阵 (Matrix)、赛博朋克 (Cyberpunk)、日光浅色 (Solarized) 随时秒切。
  - **📁 SFTP 文件侧栏**：包含路径导航、`+新建` 目录 Modal、`↑上传`（含 100MB 上限限制）、`↓下载`、`🗑️删除` 二次确认 Popconfirm。

---

## AI 能力

### Agent 工作台（`/ai/workbench`）
- 功能：AI 对话工作台，管理会话和 AI 配置
- API 接口：
  - `GET /api/ai/sessions`（会话列表）
  - `GET /api/ai/configs`（AI 配置列表）
- 关键交互：
  - **会话列表**：新建/切换/删除会话
  - **对话区**：消息输入、流式输出
  - **配置切换**：选择不同 AI 模型/配置

### AI 全景看板（`/ai/dashboard`）
- 功能：AI 使用统计与可观测性看板
- API 接口：
  - `GET /api/ai/observability/stats`（使用统计）
  - `GET /api/ai/observability/audit`（审计日志）
- 关键交互：
  - **统计卡片**：调用次数、Token 消耗等
  - **审计日志表格**

### 技能管理（`/ai/skills`）
- 功能：AI 技能（Skill）的增删改查
- API 接口：
  - `GET /api/ai/skills`（列表）
  - `POST /api/ai/skills`（新增）
  - `PUT /api/ai/skills/{id}`（更新）
  - `DELETE /api/ai/skills/{id}`（删除）
- 关键交互：
  - **表格**：技能列表
  - **抽屉/弹窗**：新增/编辑技能表单（名称、描述、提示词等）
  - **操作按钮**：编辑、删除

### 编排中心（`/ai/orchestration`）
- 功能：AI Agent 工作流编排
- API 接口：`GET /api/ai/orchestration/*`
- 关键交互：
  - **画布**：拖拽式工作流编排
  - **节点配置**：LLM 调用、工具调用、条件分支等节点

### 模型配置（`/ai/llm-config`）
- 功能：大语言模型提供商和模型的配置管理
- API 接口：
  - `GET /api/ai/llm/providers`（提供商列表）
  - `POST /api/ai/llm/providers`（新增提供商）
  - `PUT /api/ai/llm/providers/{id}`（更新提供商）
  - `DELETE /api/ai/llm/providers/{id}`（删除提供商）
  - `GET /api/ai/llm/models`（模型列表）
  - `POST /api/ai/llm/models`（新增模型）
  - `PUT /api/ai/llm/models/{id}`（更新模型）
  - `DELETE /api/ai/llm/models/{id}`（删除模型）
- 关键交互：
  - **表格**：提供商列表、模型列表
  - **抽屉**：新增/编辑提供商/模型表单
  - **操作按钮**：编辑、删除、启用/禁用

---

## 应用管理

### 应用列表（`/apps/list`）
- 功能：应用的增删改查、资源绑定、健康检查、时间线、链接管理及运行实例管理
- API 接口：
  - `GET /api/applications`（列表）
  - `GET /api/applications/role-options`（角色选项）
  - `GET /api/applications/resource-options`（资源选项）
  - `DELETE /api/applications/{id}`（删除应用）
  - `GET /api/applications/{id}`（应用详情）
  - `GET /api/applications/{id}/overview`（应用概览）
  - `GET /api/applications/{id}/timeline?limit=30`（变更时间线）
  - `GET /api/applications/{id}/health-timeline?hours=24`（健康时间线）
  - `POST /api/applications/{id}/resources`（绑定/解绑资源）
  - `POST /api/applications/{id}/components`（登记或部署运行实例）
  - `DELETE /api/applications/{id}/components/{componentId}`（移除单个运行实例登记）
  - `POST /api/applications/{id}/components/batch-delete`（批量移除运行实例登记，最多 2000 条）
  - `POST /api/applications/{id}/components/{componentId}/actions`（启动、停止、重启或查看状态）
  - `POST /api/applications/{id}/runtime-discovery`（运行时发现）
  - `POST /api/applications/{id}/links`（管理链接）
- 关键交互：
  - **表格**：应用列表，支持搜索、筛选
  - **抽屉**：新增/编辑应用表单
  - **详情页面**：概览、资源绑定、运行实例和快捷链接
  - **运行时发现**：按绑定主机逐台扫描，展示进度；缺少 Docker / Supervisor 时正常跳过
  - **扫描预览**：候选结果按部署类型筛选、折叠和分页，确认后纳管
  - **实例列表**：部署类型筛选、分页、跨页选择、单条或批量移除
  - **删除边界**：移除操作只删除 OpsPilot 登记记录，不停止主机上的实际进程

### 部署管理（`/apps/deployment`）
- 功能：应用部署流水线管理
- API 接口：
  - `GET /api/applications`（应用列表）
  - `GET /api/deployments`（部署列表）
  - `POST /api/deployments`（触发部署）
- 关键交互：
  - **表格**：部署记录列表
  - **弹窗**：触发新部署表单
  - **操作按钮**：部署、查看详情

### 配置中心（`/apps/config-center`）
- 功能：应用配置项的集中管理，支持 .env 导入、版本历史、凭证引用
- API 接口：
  - `GET /api/applications`（应用列表）
  - `GET /api/applications/config-credential-options`（凭证选项）
  - `GET /api/applications/{appId}/configs`（配置列表）
  - `POST /api/applications/{appId}/configs`（新增配置）
  - `PUT /api/applications/{appId}/configs/{configId}`（更新配置）
  - `DELETE /api/applications/{appId}/configs/{configId}`（删除配置）
  - `GET /api/applications/{appId}/configs/{configId}/revisions`（版本历史）
  - `POST /api/applications/{appId}/configs/import`（.env 导入）
- 关键交互：
  - **应用选择器**：下拉选择应用
  - **表格**：配置项列表（Key/Value/环境/类型）
  - **弹窗**：新增/编辑配置表单（支持明文值和凭证引用两种模式）
  - **导入弹窗**：粘贴或上传 .env 文件，支持覆盖/跳过/混合模式
  - **历史抽屉**：配置版本变更记录，新旧值对比
  - **操作按钮**：新增、编辑、删除、导入、刷新、查看历史

---

## 监控中心

### 实时看板（`/monitoring/live`）
- 功能：主机监控实时看板，Grafana SSO 跳转
- API 接口：
  - `GET /api/monitoring/grafana/sso-link`（Grafana SSO 链接）
  - `GET /api/monitoring/host-metrics`（主机指标）
  - `GET /api/monitoring/host-summary`（主机汇总）
  - `GET /api/monitoring/dashboard?range_minutes=15`（仪表盘数据）
  - `GET /api/monitoring/dashboard`（仪表盘数据，自定义时间范围）
- 关键交互：
  - **主机选择器**：选择查看目标主机
  - **指标卡片**：CPU、内存、磁盘、网络等实时指标
  - **图表**：时序指标折线图
  - **Grafana 跳转**：一键打开 Grafana 详细看板

### 指标查询（`/monitoring/metrics`）
- 功能：PromQL 指标查询与可视化
- API 接口：
  - `GET /api/monitoring/metrics/label-values?label=__name__`（指标名称列表）
  - `GET /api/monitoring/metrics/query`（执行 PromQL 查询）
  - `GET /api/monitoring/metrics/query-range`（范围查询）
- 关键交互：
  - **查询输入框**：PromQL 表达式输入
  - **自动补全**：指标名称建议
  - **时间范围选择器**
  - **图表**：查询结果时序图
  - **表格模式**：查询结果表格展示

### 告警规则（`/monitoring/alert-rules`）
- 功能：告警规则的管理、模板应用、规则测试
- API 接口：
  - `GET /api/monitoring/rules`（规则列表）
  - `GET /api/monitoring/notifications`（通知渠道列表）
  - `GET /api/assets`（资产列表，用于关联）
  - `POST /api/monitoring/rules`（创建规则）
  - `PUT /api/monitoring/rules/{id}`（更新规则）
  - `DELETE /api/monitoring/rules/{id}`（删除规则）
  - `GET /api/monitoring/rules/templates`（规则模板列表）
  - `POST /api/monitoring/rules/apply-templates`（应用模板）
  - `POST /api/monitoring/rules/test`（测试规则）
- 关键交互：
  - **表格**：告警规则列表（名称、表达式、严重级别、状态、通知渠道）
  - **弹窗**：新增/编辑规则表单（PromQL 表达式、持续时间、严重级别、标签、通知渠道）
  - **模板选择器**：从预置模板快速创建规则
  - **测试按钮**：验证规则 PromQL 表达式
  - **操作按钮**：编辑、删除、应用模板、测试

### 告警中心（`/monitoring/alerts`）
- 功能：告警事件的实时查看、历史查询、静默管理、统计分析、AI 分析
- API 接口：
  - `GET /api/monitoring/alerts/active`（活跃告警）
  - `GET /api/monitoring/alerts/history`（历史告警）
  - `GET /api/monitoring/alerts/stats?window_hours=24`（告警统计）
  - `GET /api/monitoring/alerts/notifications/logs`（通知日志）
  - `GET /api/monitoring/alerts/settings`（告警设置）
  - `PUT /api/monitoring/alerts/settings`（更新告警设置）
  - `POST /api/monitoring/alerts/silence`（创建静默规则）
  - `GET /api/monitoring/alerts/silence`（静默规则列表）
  - `PUT /api/monitoring/alerts/silence/{id}`（更新静默规则）
  - `DELETE /api/monitoring/alerts/silence/{id}`（删除静默规则）
  - `POST /api/monitoring/alerts/silence`（批量创建静默）
  - `POST /api/monitoring/alerts/analyze`（AI 告警分析）
  - `POST /api/monitoring/alerts/history/batch`（批量操作历史告警）
  - `GET /api/monitoring/rules`（关联规则列表）
  - `GET /api/monitoring/notifications`（通知渠道列表）
- 关键交互：
  - **多 Tab 页面**：活跃告警、历史告警、静默规则、告警统计、通知日志、告警设置
  - **表格**：告警列表（级别、规则名、标签、触发时间、状态）
  - **静默规则管理**：创建/编辑/删除静默规则
  - **AI 分析按钮**：对告警进行 AI 根因分析
  - **批量操作**：批量确认/关闭历史告警
  - **告警统计图表**
  - **操作按钮**：确认、关闭、静默、AI 分析

### 日志查询（`/monitoring/logs`）
- 功能：日志实时搜索与分析
- API 接口：
  - `GET /api/monitoring/logs/search`（日志搜索）
  - `GET /api/monitoring/logs/fields`（字段列表）
- 关键交互：
  - **搜索输入框**：日志查询语法输入
  - **时间范围选择器**
  - **日志列表**：实时日志流展示
  - **字段过滤**：按字段筛选日志

### 采集车队（`/monitoring/collectors`）
- 功能：Alloy 采集器（Collector Agent）的注册、管理、策略下发、版本回滚
- API 接口：
  - `GET /api/monitoring/collector/agents`（采集器列表）
  - `GET /api/monitoring/collector/policies`（采集策略列表）
  - `POST /api/monitoring/collector/agents/register-token`（生成注册 Token）
  - `DELETE /api/monitoring/collector/agents/{agentId}`（删除采集器）
  - `POST /api/monitoring/targets/{assetId}/uninstall-alloy`（卸载 Alloy）
  - `DELETE /api/monitoring/collector/policies/{policyId}`（删除策略）
  - `POST /api/monitoring/collector/policies/{policyId}/publish`（发布策略）
  - `GET /api/monitoring/collector/policies/{policyId}/versions`（策略版本历史）
  - `GET /api/monitoring/collector/releases`（发布记录）
  - `POST /api/monitoring/collector/releases/{releaseId}/rollback`（版本回滚）
- 关键交互：
  - **表格**：采集器列表（主机、状态、版本、IP）、采集策略列表
  - **注册流程**：生成注册 Token → Agent 注册
  - **策略编辑器**：采集策略的创建/编辑
  - **发布/回滚**：策略发布到采集器、版本回滚
  - **操作按钮**：删除采集器、卸载 Alloy、发布策略、回滚

### 通知模板（`/monitoring/channel-templates`）
- 功能：告警通知消息模板的管理与预览
- API 接口：
  - `GET /api/monitoring/notifications/templates`（模板列表）
  - `POST /api/monitoring/notifications/templates`（创建模板）
  - `PUT /api/monitoring/notifications/templates`（更新模板）
  - `DELETE /api/monitoring/notifications/templates`（删除模板）
  - `POST /api/monitoring/notifications/templates/preview`（模板预览）
- 关键交互：
  - **表格**：模板列表（名称、渠道类型、内容摘要）
  - **弹窗**：新增/编辑模板表单（模板内容支持变量插值）
  - **预览功能**：渲染模板变量后的实际效果
  - **操作按钮**：编辑、删除、预览

---

## 运维工具

### 脚本执行（`/tools/script`）
- 功能：Ansible Ad-hoc 命令执行、Playbook 管理、模板管理
- API 接口：
  - `GET /api/tools/ansible/config`（Ansible 配置）
  - `PUT /api/tools/ansible/config`（更新 Ansible 配置）
  - `GET /api/tools/ansible/targets`（可执行目标列表）
  - `GET /api/tools/ansible/history`（执行历史）
  - `GET /api/tools/ansible/templates`（模板列表）
  - `GET /api/tools/ansible/role-options`（角色选项）
  - `POST /api/tools/ansible/templates`（创建模板）
  - `PUT /api/tools/ansible/templates/{id}`（更新模板）
  - `DELETE /api/tools/ansible/templates/{id}`（删除模板）
  - `POST /api/tools/ansible/run-template`（执行模板）
  - `POST /api/tools/ansible/run`（执行 Ad-hoc 命令）
  - `POST /api/tools/ansible/run-playbook`（执行 Playbook）
- 关键交互：
  - **多 Tab 页面**：Ad-hoc 执行、Playbook 管理、模板管理、执行历史、配置
  - **执行表单**：选择目标主机/角色 → 输入模块和参数 → 执行
  - **模板管理**：创建/编辑/删除/执行 Ansible 模板
  - **Playbook 编辑器**：编写/上传/执行 Playbook
  - **执行结果**：实时输出执行结果（stdout/stderr）
  - **历史记录**：查看过往执行记录

---

## 系统设置

### 系统管理（`/settings/system`）
- 功能：系统全局设置（通知渠道配置、系统参数等）
- API 接口：
  - `GET /api/settings/notifications`（通知渠道列表）
  - `POST /api/settings/notifications`（新增通知渠道）
  - `PUT /api/settings/notifications/{id}`（更新通知渠道）
  - `DELETE /api/settings/notifications/{id}`（删除通知渠道）
  - `GET /api/settings/notification-templates`（通知模板）
- 关键交互：
  - **表格**：通知渠道列表（类型：飞书/企微/钉钉/邮件/Webhook）
  - **弹窗**：新增/编辑通知渠道表单（不同渠道类型展示不同字段）
  - **操作按钮**：编辑、删除、测试发送

### 用户管理（`/settings/users`）
- 功能：系统用户的增删改查
- API 接口：
  - `GET /api/settings/users`（用户列表）
  - `POST /api/settings/users`（新增用户）
  - `PUT /api/settings/users/{id}`（更新用户）
  - `DELETE /api/settings/users/{id}`（删除用户）
- 关键交互：
  - **表格**：用户列表（用户名、角色、状态、创建时间）
  - **弹窗**：新增/编辑用户表单（用户名、密码、角色）
  - **操作按钮**：编辑、删除、启用/禁用

### 权限管理（`/settings/permissions`）
- 功能：RBAC 角色权限配置
- API 接口：
  - `GET /api/settings/rbac/metadata`（RBAC 元数据：角色、权限矩阵）
  - `GET /api/settings/users`（用户列表，用于角色分配）
  - `PUT /api/settings/rbac/roles/{role}`（更新角色权限）
- 关键交互：
  - **权限矩阵表格**：角色 × 权限维度的勾选矩阵
  - **角色列表**：系统预置角色和自定义角色
  - **操作按钮**：保存权限配置

### 审计日志（`/settings/audit`）
- 功能：系统操作审计日志查询
- API 接口：`GET /api/settings/audits`（审计日志列表）
- 关键交互：
  - **表格**：审计日志列表（操作人、操作类型、目标资源、时间、IP）
  - **筛选**：按时间范围、操作类型、操作人筛选

---

## 附录：全局 API 路径汇总

| 模块 | API 前缀 | 页面 |
|------|----------|------|
| 认证 | `/api/auth/*` | 登录 |
| 仪表盘 | `/api/dashboard/*` | 首页 |
| 资产 | `/api/assets/*` | 主机/虚机/集群/域名/证书管理 |
| 凭证 | `/api/credentials/*` | 凭证管理 |
| 分组 | `/api/groups/*` | 各资产页面 |
| AI | `/api/ai/*` | Agent 工作台、AI 看板、技能管理、编排、模型配置 |
| 应用 | `/api/applications/*` | 应用列表、部署、配置中心 |
| 部署 | `/api/deployments/*` | 部署管理 |
| 监控 | `/api/monitoring/*` | 实时看板、指标查询、告警规则/中心、日志、采集器、通知模板 |
| 工具 | `/api/tools/*` | 脚本执行 |
| 设置 | `/api/settings/*` | 系统管理、用户、权限、审计 |
