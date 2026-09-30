# 30 — 个人 IM 渠道与飞书群 @ 通知

> 日期：2026-09-21
> 范围：`backend/app/models/user_im_binding.py`、`backend/app/services/im_feishu.py`、`backend/app/api/im.py`、
> `create_notification()` fanout、告警规则 `im_user_ids`、DB 工单审批通知、前端个人 IM 页 / 系统管理 IM 接入 Tab / 告警规则选通知人
> 起因：用户要求"每个登录用户给自己配一个 IM 渠道，告警/DB 审核/证书申请完成后在群里 @ 他或私信他"

---

## 一、需求与分期

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1（本轮） | 个人绑定飞书 + **群内 @ 我**（自定义机器人 webhook + `<at user_id>`） | ✅ 完成 |
| P2 | 自建应用私信（im:message:send 单聊） | 待排期 |
| P3 | IM 回调操作系统（卡片按钮 → 平台 API） | 待排期（需公网可达地址） |

本轮按用户拍板只做 P1：**只接飞书、只做群 @**。私信和回调依赖自建应用 +
公网可达的 `console_url`，等用户加完公网再做。

## 二、架构：一个挂载点覆盖全部业务通知

平台里所有业务通知（证书、虚拟机、Agent、DB 工单……）最终都走
`app/api/notifications.py` 的 `create_notification()`——这是全站唯一入口。
本轮不改动任何业务模块，只在 `create_notification()` 写库 + SSE 发布之后追加一次
**best-effort 飞书 fanout**：绑定就发，没绑定/发送失败只记 debug 日志，
绝不影响站内通知和主流程。

告警是例外：告警走 `alert_dispatcher`，有自己的"渠道"并行通道，
所以单独给告警规则加了 `im_user_ids`（规则级 @ 通知人），与 webhook 渠道并列派发。

```
业务动作 ──► create_notification() ──► user_notifications + SSE
                    │
                    └─► fanout_user_notification() ──► 飞书群 @（个人 webhook > 系统默认群）

alert_engine（30s 循环）
   └─► dispatch_active_alerts()
          ├─► webhook 渠道（原有）
          └─► notify_users_alert(im_user_ids)（新增，触发/恢复/静默恢复三处）
```

## 三、后端实现

### 1. 绑定表 `user_im_bindings`（`app/models/user_im_binding.py` + Alembic `20260921_im_bindings`）

| 字段 | 说明 |
|---|---|
| `user_id` + `platform` | 唯一约束 `uq_user_im_binding`，一人一平台一条（当前 platform 恒为 feishu） |
| `identity` | 用户填写的邮箱或手机号（登录态自查，非管理员） |
| `open_id` | **服务端换来的**飞书 open_id，前端只拿到脱敏值（前 6 位 + `****`） |
| `notify_mode` | `group_at`（默认）/ `off` 开关 |
| `webhook_url` | 个人群机器人地址，留空则用系统默认群 |

安全红线（已落实到代码）：

- **open_id 只由服务端换**：PUT binding 时后端拿 `app_id/app_secret` 调
  `contact/v3/users/batch_get_id`，前端传什么都不覆盖这个值；换不到直接 422，
  绝不用空值兜底。
- **个人 webhook 防 SSRF**：`_validate_webhook` 只放行 `https://` 且 host 在
  `("open.feishu.cn", "www.feishu.cn")` 白名单内，内网地址/IP 直连一律 422。
- **app_secret 永不返回明文**：`read_feishu_config` 只回 `app_secret_set: bool`；
  保存时空字符串表示"不修改"，保留旧值并清 token 缓存。

### 2. 飞书服务 `app/services/im_feishu.py`

- `get_tenant_access_token`：模块级缓存 + `threading.Lock`，剩余不足 5 分钟提前续期；
  code != 0 或网络异常抛 `FeishuError`。
- `resolve_open_id`：含 `@` 走 `emails`，否则按 `mobiles` 提交换 open_id；兼容返回体的
  `user_id` / `open_id` 两种字段名。
- `send_group_at`：自定义机器人文本消息，正文
  `<at user_id="ou_xxx"></at>\n【标题】\n内容\n查看详情: <link>`；
  成功判定同时兼容新版 `code == 0` 与旧版 `StatusCode == 0`。
- `fanout_user_notification` / `notify_users_alert`：发送全部 best-effort，
  失败吞掉返回 `{"ok": False, ...}`，调用方只记日志。

### 3. REST API `app/api/im.py`（前缀 `/api/im`，整表挂 `get_current_user`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/feishu/binding` | 我的绑定（open_id 脱敏） |
| PUT | `/feishu/binding` | 绑定/更新：校验身份格式 → 服务端换 open_id → upsert |
| DELETE | `/feishu/binding` | 解绑 |
| POST | `/feishu/test` | 发一条测试 @ 消息 |
| GET | `/users` | 全站启用用户 + `im_bound` 标记（供告警规则选通知人） |
| GET/PUT | `/feishu/config` | 管理员级：app_id / app_secret / 默认群 webhook（需 `module.settings.system`） |

### 4. 告警规则 `im_user_ids`（`monitoring/schemas.py` + `services/rules.py` + `alert_dispatcher.py`）

- 规则新增 `im_user_ids: list[int]`，归一化规则：非 list 忽略、非正整数丢弃、去重、上限 20。
- `dispatch_active_alerts` 的渠道门从"无渠道就跳过"改为"无渠道且无通知人才跳过"——
  只配了 @ 人、没配任何 webhook 的规则现在也能正常通知。
- 派发点共 4 处：触发（"触发"）、立即恢复（"已恢复"）、静默恢复宽限期结束
  （`handle_resolved_alert_grace` 传 `im_user_ids`，"已恢复"）。
- 消息带 instance/severity/summary，链接指向 `/monitoring/alerts`。

### 5. DB 工单审批通知补齐（`app/api/database.py`）

原来提交人只收到执行结果、审批人什么都不收。本轮补两条：

- `_notify_ticket_pending`：建单后按权限（`action.db.approve`）找活跃审批人，
  发 warning 级 `db` 站内通知"待审批 SQL 工单：{title}"。
- `_notify_ticket_reviewed`：审批后通知提交人"SQL 工单审批通过/被驳回"。

两条都走 `create_notification`，因此自动继承飞书群 @ 能力。

### 6. 顺手修的存量 bug（`app/api/ai_agent.py`）

`get_ai_observability_stats` 用 `datetime.utcnow()` 取"今天"，而消息写入方
`_now_str()` 用本地 `datetime.now()`——每天 0:00–08:00（UTC+8）今日消息数与 7 天趋势
末位被错归零。两处（`today_str` 与趋势循环）改为 `datetime.now()`，已补中文注释。

## 四、前端实现

| 文件 | 内容 |
|---|---|
| `pages/settings/im/index.tsx` | 个人 IM 页（路由 `/settings/im`，菜单"个人 IM 通知"）：绑定表单（身份必填、webhook 选填 https 校验、通知模式单选）、绑定后 Descriptions 展示脱敏 open_id + 群来源（个人群/系统默认群）、发送测试消息、解绑（Popconfirm）、排障指引 |
| `pages/settings/system/index.tsx` | 系统管理新增「IM 接入（飞书）」Tab：app_id/app_secret/默认群 webhook 表单 + 管理员 5 步接入指引（建应用→开权限→加机器人→换 webhook→回填） |
| `pages/monitoring/alert-rules/index.tsx` | 规则表单新增「飞书通知人」多选（拉 `/im/users`，未绑定用户带"（未绑定）"后缀提示）、表格新增飞书通知人列 |
| `types/api.ts` / `api/index.ts` | `ImBinding` / `FeishuImConfig` / `ImUser` 类型 + `imApi` |

## 五、验证结果

- 新增契约测试 `backend/test_im_feishu_contract.py`：**36 例全绿**
  （@ 文本拼装 / token 缓存与失败 / open_id 换取与找不到 / 群发成功判定新旧协议 /
  fanout 各跳过分支 / 批量通知计数去重 / 规则归一化含 20 上限 / webhook 白名单 /
  `create_notification` 触发 fanout / 未绑定用户不发送）
- 全量回归：`pytest -q` **348 passed**（较上轮 312 + 新 36）
- 前端：`tsc --noEmit` 零错误、`vite build` 通过
- Alembic 链：head `20260920_db_enhance` → `20260921_im_bindings`（未合并既有双 head，
  与上轮决定一致，继续搁置）

## 六、遗留

- P2 私信、P3 IM 回调需自建应用 + 公网可达 `console_url`，等用户确认公网方案后开工
- 告警"通知人"目前只覆盖飞书；后续接其他 IM 时在 `im_feishu.py` 旁加平行服务、
  `UserImBinding.platform` 已为此留好枚举位
