# 用户站内通知 API 文档

> 版本：v1　　日期：2026-07-24　　状态：已上线，可供前端联调

---

## 概述

面向登录用户的站内信系统，用于异步操作结果通知（证书续期/签发、虚机同步、Agent 安装等长耗时任务完成或失败）。

**Base URL**：`/api/notifications`
**认证**：所有接口均需登录（Bearer Token），未登录返回 `401`。

---

## 数据结构

### Notification

```json
{
  "id": "string (uuid)",
  "type": "string",          // success | error | warning | info
  "category": "string",      // certificate | vm | agent | system
  "title": "string",         // 标题，如「证书续期完成」
  "content": "string",       // 详情，如「example.com 证书已续期至 2026-10-24」
  "link": "string | null",   // 前端路由，如 /assets/certificates
  "resource_id": "string | null",  // 关联资源 ID
  "is_read": false,
  "created_at": "2026-07-24 15:30:00"
}
```

---

## 接口列表

### 1. 获取通知列表

```
GET /api/notifications
```

**Query 参数**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| is_read | bool | 否 | 筛选未读(false)/已读(true)，不传返回全部 |
| limit | int | 否 | 每页数量，默认 20，最大 100 |
| offset | int | 否 | 偏移量，默认 0 |

**响应 200**

```json
{
  "items": [ Notification, ... ],
  "total": 42,
  "unread_count": 5
}
```

- `items` 按 `created_at` 降序排列
- `unread_count` 是该用户当前未读总数（不受 is_read 筛选影响）

---

### 2. 获取未读数量（轮询用）

```
GET /api/notifications/unread-count
```

**响应 200**

```json
{
  "unread_count": 5
}
```

前端建议每 **30~60 秒** 轮询此接口刷新铃铛角标，比拉全量列表省流量。

---

### 3. 标记单条已读

```
POST /api/notifications/{id}/read
```

**路径参数**

| 参数 | 说明 |
|------|------|
| id | 通知 ID |

**响应 200**

```json
{ "ok": true }
```

**错误**

- `404` — 通知不存在或不属于当前用户

---

### 4. 全部标记已读

```
POST /api/notifications/read-all
```

**响应 200**

```json
{ "ok": true, "updated": 5 }
```

---

### 5. 删除单条通知

```
DELETE /api/notifications/{id}
```

**响应 200**

```json
{ "ok": true }
```

**错误**

- `404` — 通知不存在或不属于当前用户

---

### 6. 清空全部通知

```
POST /api/notifications/clear
```

**响应 200**

```json
{ "ok": true, "deleted": 12 }
```

---

## 实时推送 SSE（已上线）

### 端点

```
GET /api/notifications/stream?token=<JWT>
Accept: text/event-stream
```

**鉴权**：原生浏览器 `EventSource` 不支持自定义请求头，JWT 通过 query param `token` 传递，与 `Authorization: Bearer` 等价。

### 消息格式

每产生一条新通知，推送一帧 `data`，内容为单条通知的完整 JSON（字段与列表接口 `items[]` 元素一致）：

```
data: {"id":"xxx","type":"success","category":"certificate","title":"证书续期完成","content":"example.com 已续期","link":"/assets/certificates","resource_id":"cert_123","is_read":false,"created_at":"2026-07-24 15:30:00"}
```

- 前端 `JSON.parse(event.data)` 后直接插入列表顶部并刷新未读角标
- 不需要自定义 `event:` 名，用默认 `message` 事件（前端监听 `onmessage`）

### 心跳

每 20 秒发一帧注释行心跳保活（`: keep-alive`），前端对无法 `JSON.parse` 的帧静默忽略。

### 连接与降级

- 前端挂载后立即尝试连接；`onopen` 成功即停止 `unread-count` 轮询
- `onerror`（断线）时前端标记未连接，轮询兜底接管；`EventSource` 自动重连
- 无此端点时前端自动走 45 秒轮询，功能不受影响

### Nginx 配置

生产环境若走 Nginx 反代，需关闭该路径的缓冲：

```nginx
location /api/notifications/stream {
    proxy_pass http://backend;
    proxy_buffering off;
    proxy_read_timeout 86400s;
}
```

---

## 前端对接流程

1. **铃铛角标**：轮询 `GET /api/notifications/unread-count`，返回 `unread_count` 显示红点/数字
2. **点开铃铛**：请求 `GET /api/notifications?limit=20`，渲染列表
3. **点击某条**：先调 `POST /api/notifications/{id}/read`，再跳转 `link`
4. **全部已读**：按钮调 `POST /api/notifications/read-all`

---

## 通知类型与图标建议

| type | 含义 | 建议颜色 |
|------|------|----------|
| success | 操作成功 | 绿色 |
| error | 操作失败 | 红色 |
| warning | 需注意 | 黄色 |
| info | 一般信息 | 蓝色/灰色 |

---

## 业务分类（category）

| category | 说明 |
|----------|------|
| certificate | 证书续期/签发 |
| domain | 域名同步 |
| vm | 虚机同步/操作 |
| host | 主机批量操作 |
| agent | Agent 安装/卸载 |
| system | 系统级通知（数据导入导出、备份巡检等） |

---

## 备注

- 所有接口仅操作当前登录用户的通知，不存在跨用户访问
- 删除/清空操作不可恢复
- SSE 已上线，前端优先用 SSE 推送，断线自动回退轮询
