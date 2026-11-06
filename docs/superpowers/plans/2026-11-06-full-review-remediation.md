# OpsPilot 全量评审整改实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 分批执行。所有行为变更严格按红灯测试、最小实现、绿灯验证、独立提交推进。

**目标：** 完成 `2026-11-06-review-remediation-design.md` 的四批整改，封堵安全与迁移风险，修复前端可靠性缺陷，并建立可持续的测试与静态门禁。

**架构：** 后端以精确风险票据、版本化加密、统一出站策略和 Alembic 为权威边界；前端以显式票据、统一认证下载和集中查询键为唯一数据通路；部署与 CI 共同验证空库、存量库、Compose 和镜像。

**技术栈：** FastAPI、SQLAlchemy、Alembic、PyJWT、cryptography、PostgreSQL、React 19、TypeScript、TanStack Query、Vitest、Docker Compose、GitHub Actions。

---

## 文件职责

- `backend/app/core/security.py`、`backend/app/api/auth.py`、`backend/app/models/risk_ticket.py`：风险票据规范化、签发和消费。
- `backend/app/core/crypto.py`、`backend/app/services/credential_crypto.py`：版本化密文与惰性迁移。
- `backend/app/core/outbound_policy.py`：LLM、K8s、RDAP 共用出站校验。
- `backend/app/core/tokens.py`、`backend/app/models/refresh_token.py`：PyJWT 与 refresh token 生命周期。
- `backend/alembic/versions/`：票据、refresh token及缺失 schema 的真实迁移。
- `deploy/`、`docker-compose.yml`、`.github/workflows/ci.yml`：安装、更新、网络、容器和 CI。
- `frontend/src/api/`、`frontend/src/lib/fetch.ts`：认证、票据、下载和查询键。
- `backend/tests/`、`frontend/src/**/*.test.*`：每项行为的可执行契约。

---

## 第一批：安全与数据边界

### 任务 1：风险票据精确绑定

**文件：**
- 修改：`backend/app/models/risk_ticket.py`
- 修改：`backend/app/api/auth.py`
- 修改：`backend/app/core/security.py`
- 创建：`backend/alembic/versions/20261106_risk_ticket_v2.py`
- 修改：`backend/tests/test_risk_ticket_contract.py`

- [ ] 写失败测试：短键解析多于一组时 422；票据方法、路由、资源 ID 或请求摘要不匹配时 403 且不消费；完全匹配只成功一次。

```python
def test_ticket_cannot_cross_resource(self):
    ticket = self.issue(resource_id="user-a", method="DELETE")
    response = self.consume(ticket, resource_id="user-b", method="DELETE")
    assert response.status_code == 403
    assert self.ticket(ticket).consumed_at is None
```

- [ ] 运行 `pytest tests/test_risk_ticket_contract.py -q`，确认新增用例因字段未校验失败。
- [ ] 给模型增加 `http_method`、`route_template`、`request_digest`；迁移使用 nullable 兼容滚动升级并清理未消费旧票据。
- [ ] 签发端将短键解析为唯一全键；消费端按全键、动作、方法、路由、资源和摘要原子 UPDATE。
- [ ] 运行专项测试和 `scripts/smoke_ticket_e2e.py`，提交：`fix(security): bind risk tickets to exact requests`。

### 任务 2：所有高危调用改显式票据

**文件：**
- 修改：`frontend/src/hooks/usePermission.ts`
- 修改：`frontend/src/api/client.ts`
- 修改：高危 mutation 所在页面
- 修改：`backend/app/api/assets.py`、`backend/app/api/clusters/pods.py`、`backend/app/api/database.py`、`backend/app/api/monitoring/routes.py`、通知与应用路由
- 修改：`backend/app/core/rbac.py`
- 测试：`frontend/src/api/client.test.ts`、`frontend/src/hooks/usePermission.test.ts`、后端风险契约测试

- [ ] 写失败测试，证明 GET 轮询不会获得票据，只有目标 mutation 携带票据；SSH exec、Pod exec、DB 执行、采集器卸载、通知清空、组件启停/批删缺票均 403。
- [ ] 删除全局 `pendingRiskTicket` 槽位；`fetchRiskTicket` 接收规范化目标并返回仅供单请求使用的 header。
- [ ] 为上述后端路由注册风险等级并挂 `require_risk_ack`，前端通过 `useRiskConfirm` 显式发送。
- [ ] 验证前后端专项并提交：`fix(security): require explicit tickets for privileged actions`。

### 任务 3：版本化凭证加密

**文件：**
- 修改：`backend/app/core/config.py`、`backend/app/core/crypto.py`
- 创建：`backend/app/services/credential_crypto.py`
- 创建：`backend/scripts/migrate_credential_ciphertext.py`
- 创建：`backend/tests/test_crypto_contract.py`
- 修改：凭证读取与保存服务、环境样例和安装器

- [ ] 写失败测试：缺密钥、密钥等于 JWT 密钥、损坏密文均显式失败；v1 可读；v2 每次 salt 不同但均可解密。

```python
def test_corrupted_ciphertext_never_becomes_empty_secret():
    with pytest.raises(CredentialDecryptionError):
        decrypt_secret("enc::v2::broken")
```

- [ ] 实现 `enc::v2::<salt>::<token>`，HKDF info 固定为 `opspilot/credential/v2`；保留 v1 读取但禁止 SECRET_KEY 回退。
- [ ] 在凭证写路径统一写 v2；批量迁移脚本支持 dry-run、逐批提交和仅记录凭证 ID。
- [ ] 更新部署样例和启动校验，运行凭证/云资源/费用测试，提交：`fix(security): version credential encryption`。

### 任务 4：PyJWT、refresh token 与登录防护

**文件：**
- 修改：`backend/requirements.txt`、`backend/app/core/security.py`、JWT 调用点
- 创建：`backend/app/core/tokens.py`、`backend/app/models/refresh_token.py`
- 创建：`backend/alembic/versions/20261106_refresh_tokens.py`
- 修改：`backend/app/api/auth.py`、`backend/app/core/login_rate_limit.py`
- 修改：`frontend/src/store/auth.ts`、`frontend/src/api/client.ts`
- 测试：新增 token/login 契约，扩展前端 client 测试

- [ ] 写失败测试：算法混淆拒绝、60 分钟 access、refresh 单次轮换、重放撤销 token family、禁用/改密使 refresh 失效、不存在用户仍执行假哈希。
- [ ] 用 PyJWT 封装 encode/decode，所有调用只经 `tokens.py`；删除 python-jose。
- [ ] 新增 `/auth/refresh` 和 `/auth/logout`，数据库只存 refresh 哈希；前端遇 401 只自动刷新一次，失败再登出。
- [ ] 登录源 IP 仅在直接来源属于 `TRUSTED_PROXY_CIDRS` 时解析 XFF；限流状态持久化数据库。
- [ ] 验证认证、SSE、Grafana SSO 和前端测试，提交：`fix(auth): rotate tokens and trust proxy sources`。

### 任务 5：统一 SSRF 出站策略

**文件：**
- 创建：`backend/app/core/outbound_policy.py`
- 修改：`backend/app/api/ai_agent.py`、`backend/app/api/clusters/cluster_mgmt.py`、`backend/app/api/domains.py`
- 修改：`backend/app/core/config.py`、环境样例
- 创建：`backend/tests/test_outbound_policy.py`

- [ ] 写参数化失败测试覆盖 loopback、link-local、云元数据、DNS rebinding、HTTP、用户信息 URL、跨域重定向和超大响应；显式 CIDR/host 允许项通过。
- [ ] 实现 URL、DNS、最终连接 IP、逐跳重定向、大小和超时校验。
- [ ] LLM、K8s scan、RDAP 统一调用策略；外部错误映射为稳定中文分类，底层细节只入日志。
- [ ] 运行 AI、集群、域名测试并提交：`fix(security): enforce outbound request policy`。

### 任务 6：Bootstrap、审计和小型安全缺口

**文件：**
- 修改：`backend/app/db/bootstrap.py`、`backend/app/main.py`
- 修改：`backend/app/api/monitoring_webhooks.py`、`backend/app/services/db_ops.py`
- 创建/修改：对应契约测试

- [ ] 写失败测试：已有管理员状态/角色不改变；弱初始化密码拒绝；审计写失败不覆盖响应；risk reason 清洗限长；log_error 未登录拒绝；Webhook 常量时间比较；PG bool 为 TRUE/FALSE。
- [ ] 实现最小修复并给高敏 GET 补审计规则。
- [ ] 运行 bootstrap、audit、webhook、db_ops、E2E 测试并提交：`fix(security): harden bootstrap and audit boundaries`。

### 任务 7：认证 Blob 下载

**文件：**
- 修改：`frontend/src/api/index.ts`、`frontend/src/utils/download.ts`
- 修改：`TerminalModal.tsx`、数据库工单、WireGuard、账单页面
- 测试：`frontend/src/api/downloads.test.ts`

- [ ] 写失败测试，断言四类下载使用 axios Bearer、Blob 和 content-disposition，不调用 `window.open` 或裸 href。
- [ ] 增加具名下载 API 并替换调用；失败展示后端 detail。
- [ ] 运行 Vitest/lint/build，提交：`fix(frontend): authenticate protected downloads`。

---

## 第二批：数据库与部署

### 任务 8：建立 schema 差异基线并补真实迁移

**文件：**
- 创建：`backend/tests/schema/legacy_schema.sql`
- 创建：`backend/tests/test_alembic_upgrade_contract.py`
- 修改：`scripts/verify_alembic_full_upgrade.py`
- 创建：一个或多个 `backend/alembic/versions/20261106_*.py`

- [ ] 在临时 PostgreSQL 分别创建空库和 legacy fixture，运行 upgrade head 后逐表、列、索引、外键与 ORM 比较；先确认 legacy 用例失败并列出差异。
- [ ] 按依赖顺序新增幂等迁移，先表和可空列、再回填、最后索引/约束；不得使用 `Base.metadata.create_all`。
- [ ] 验证空库、legacy、重复 upgrade 全绿并提交：`fix(db): complete production schema migrations`。

### 任务 9：统一安装和更新迁移链路

**文件：**
- 修改：`deploy/offline/app/docker-compose.yaml`、`deploy/update/deploy_update.sh`
- 修改：`backend/alembic/env.py`、部署文档
- 测试：扩展部署安全契约

- [ ] 写静态和容器测试，禁止 `create_all`、`stamp head`，要求 upgrade 在 backend 前成功，更新失败不切新镜像。
- [ ] 删除离线内联建表和重复版本表 workaround；更新脚本执行迁移、滚动启动、健康检查和镜像回退。
- [ ] 四份 Compose config 与临时 PostgreSQL 演练通过，提交：`fix(deploy): make alembic the only schema path`。

### 任务 10：安装器 fail-closed 与版本 manifest

**文件：**
- 修改：`deploy/offline/install.sh`、`package.sh`、`env.example`、README
- 创建：`deploy/offline/manifest.example.json`
- 测试：`backend/tests/test_installer_contract.py`

- [ ] 写失败测试覆盖健康失败退出非零、配置备份 0600、必填校验、24 字符密码、INSTALL_DIR 生效、镜像 tag/digest 从 manifest 读取。
- [ ] 实现预检、备份、manifest 和最终服务矩阵；任何必需项失败不打印完成。
- [ ] shellcheck/bash -n 和临时目录集成测试通过，提交：`fix(installer): fail closed and verify artifacts`。

### 任务 11：监控网络、版本和自监控

**文件：**
- 修改：两份 monitoring Compose、三份 Alloy 配置、Grafana provisioning、告警规则
- 测试：新增监控栈一致性契约

- [ ] 写失败测试要求 VM/VLogs/Alloy 默认不绑定 0.0.0.0、版本一致、日志轮转/资源限制/healthcheck/磁盘上限存在，三份 Alloy 关键段一致。
- [ ] 统一版本变量和配置，加入自身抓取、失联及磁盘告警。
- [ ] Compose config 与 Alloy config 校验通过，提交：`fix(monitoring): isolate and observe the monitoring stack`。

### 任务 12：容器、CI、配置和破坏性操作保护

**文件：**
- 修改：前后端 Dockerfile、根 Compose、CI、环境样例、验证脚本、部署文档
- 创建：downgrade 保护脚本与测试

- [ ] 写契约测试：镜像非 root/固定版本/healthcheck/多阶段；CI 包含 Linux PG、迁移、Compose、build、audit；生产 DB 拒绝验证脚本；downgrade 无备份确认拒绝。
- [ ] 根 Compose 补 restart 和健康依赖；Dockerfile 移除全量 lifecycle 放行。
- [ ] 配置样例覆盖全部必需键且脱敏摘要可检测拼写错误；资产密码迁移写完成标记。
- [ ] 本地统一检查和 CI YAML 解析通过，提交：`build(security): harden containers and delivery gates`。

---

## 第三批：前端可靠性

### 任务 13：统一认证、查询键和请求生命周期

**文件：**
- 修改：`frontend/src/lib/fetch.ts`、`api/client.ts`、`api/queryKeys.ts`
- 修改：AI、日志、资产和应用页面
- 测试：client/fetch/query key 测试

- [ ] 写失败测试覆盖 fetch 401 登出、abort、超时；跨页 mutation 失效 `*-all`；AI 卸载中止；应用移组错误和失效。
- [ ] 实现统一认证处理、静态 API 调用和查询键工厂；长操作显式 timeout。
- [ ] 运行前端测试/lint/build并提交：`fix(frontend): unify request and cache lifecycles`。

### 任务 14：修复重复回调、筛选和统计

**文件：**
- 修改：DeploymentProgressModal、域名/证书/主机/日志页面和后端列表参数
- 创建组件/纯函数测试

- [ ] 写失败测试：同 task 成功仅回调一次；分组筛选生效；非法日期不计到期；主机页不重复全量请求；日志不渲染 1000 行无分页。
- [ ] 实现 ref 守卫、服务端 group_id、聚合统计和分页/虚拟化。
- [ ] 验证提交：`fix(frontend): stabilize polling filters and summaries`。

### 任务 15：统一权限和交互规范

**文件：**
- 修改：报告点名的 AI、K8s、资产、WireGuard、通知和凭证页面
- 删除：死页面与死 store
- 测试：权限显隐、通知 store、凭证导出组件测试

- [ ] 写失败测试覆盖无权限入口隐藏、单次确认、Drawer 表单、403 去重、通知回滚/TTL、按勾选凭证导出。
- [ ] 使用 `App.useApp()`，移除教学句、死代码和下拉说明；补完整 ACME 回填。
- [ ] 前端全量绿后提交：`refactor(frontend): align permissions and interaction contracts`。

---

## 第四批：测试与工程治理

### 任务 16：隔离 E2E 和关键恢复链路

**文件：**
- 创建/修改：`backend/tests/conftest.py`、E2E、ACME、DB、Finance、变更工单测试
- 修改：ACME/DB 服务的依赖注入边界

- [ ] 建立临时 PostgreSQL fixture、随机管理员和真实 ASGI client，删除开发库与硬编码密码。
- [ ] 先写并观察 ACME 四场景、DB 备份执行回滚、Finance 鉴权、真实 Session 状态机的失败。
- [ ] 实现最小依赖注入并让测试通过，提交：`test: isolate end to end and recovery workflows`。

### 任务 17：静态门禁、时间和异常治理

**文件：**
- 修改：星号导入、同步/计费异常点、UTC 时间点、ruff/CI 配置
- 测试：静态契约

- [ ] 增加 F403/F405、裸吞异常和 naive UTC 扫描测试并确认失败。
- [ ] 改显式导入、具体异常和 timezone-aware API；保留有注释的解析回退白名单。
- [ ] 清零现有 hooks warning，提交：`refactor: enforce explicit imports and observable failures`。

### 任务 18：按域拆分超大模块

**文件：**
- 拆分：`ai_agent.py`、`assets.py`、`monitoring/routes.py`
- 重构：`sync_assets`、集群健康摘要、告警派发、DNS 旧实现
- 测试：既有路由/契约测试

- [ ] 每次只迁移一个纯服务或路由域，先用路由清单和响应快照测试锁定行为。
- [ ] 新文件使用显式依赖，聚合入口保持 import 兼容；DNS 新增只走 adapter registry。
- [ ] 每个域单独测试和提交，最终提交：`refactor: split oversized service domains`。

### 任务 19：完成占位实现、状态持久化和依赖清理

**文件：**
- 修改：部署任务通知/执行器、scheduler、DNS 节流、依赖清单、package.json、文档

- [ ] 写失败测试证明部署任务真实通知和执行、租约 fail-open 告警/恢复、DNS 多 worker 节流由数据库保证。
- [ ] 接通现有通知和 runtime 执行服务；把节流迁入 lease/task 表；魔法数字具名。
- [ ] 删除未用依赖、固定前端直接依赖版本、更新 lockfile。
- [ ] 全量验证并提交：`chore: close deployment gaps and dependency debt`。

---

## 最终验收

- [ ] 后端全量 pytest、119 subtests、pyflakes/ruff、秘密扫描全绿。
- [ ] 前端 Vitest、ESLint 0 warning、TypeScript build 全绿。
- [ ] 空库和 legacy PostgreSQL upgrade 后 schema 与 ORM 一致。
- [ ] 四份 Compose config、前后端镜像构建、Alloy/Grafana 配置校验通过。
- [ ] 逐条复测评审报告问题并更新 `开发文档记录/页面开发记录/64-全量评审整改.md`。
- [ ] `git diff --check`、工作树状态和提交列表核验后，再决定合并或 PR。
