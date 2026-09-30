# OpsPilot 离线交付包

一键安装：前后端应用栈（含 PG 初始化）+ 监控栈。目标机只需 **Docker + Compose v2**，**无需外网**。

## 目录

```text
deploy/offline/
├── install.sh              # 交互/参数化安装入口
├── package.sh              # 开发机打包（构建镜像 + docker save）
├── env.example             # 参数模板
├── app/
│   └── docker-compose.yaml # PG + init(alembic) + backend + frontend
├── monitoring/
│   ├── docker-compose.yaml # VictoriaMetrics + VictoriaLogs + Grafana + Alloy
│   └── config/alloy.alloy  # 本机 host 指标 + Docker 日志
└── images/                 # *.tar 离线镜像
```

## 目标机安装

```bash
# 1) 拷贝本目录到目标机（U盘/scp），chmod +x install.sh
cd deploy/offline
chmod +x install.sh

# 2) 一键安装（交互填参数）
sudo ./install.sh

# 或非交互：先改好配置
cp env.example my.conf
# 编辑 my.conf 填密码/IP/端口
sudo ./install.sh --config my.conf
```

安装完成后终端会打印：

- 前端 URL、管理员账号密码
- 监控端口（若启用）

登录后到 **系统设置 → 监控连接** 确认 VM/VLogs/Grafana 并点「测试连接」。

## 安装参数（env.example）

| 参数 | 说明 | 示例 |
|---|---|---|
| `INSTALL_DIR` | 文档/记录用安装目录 | `/opt/opspilot` |
| `PUBLIC_URL` | 浏览器访问前端地址 | `http://192.168.1.10:8080` |
| `FRONTEND_PORT` / `BACKEND_PORT` | 宿主机端口 | `8080` / `8001` |
| `POSTGRES_PASSWORD` | 数据库密码（必填） | 强随机 |
| `SECRET_KEY` / `CREDENTIAL_ENCRYPTION_KEY` | 应用密钥（**更换加密钥会导致已存凭证无法解密**） | 强随机 |
| `INIT_ADMIN_PASSWORD` | 初始管理员密码 | 强随机 |
| `INSTALL_MONITORING` | 是否装监控栈 | `true`/`false` |
| `MONITORING_HOST` | 后端查询与 Agent 写入用的监控机 IP | `192.168.1.10` |
| `MONITOR_BIND` | 监控端口绑定 | `0.0.0.0`（跨机）/ `127.0.0.1` |
| `GRAFANA_ADMIN_PASSWORD` | Grafana 管理员密码 | 强随机 |

## 镜像清单（images/*.tar）

| 镜像 | 用途 |
|---|---|
| `opspilot/backend:1.0.0` | 后端 + **init-db**（alembic upgrade head） |
| `opspilot/frontend:1.0.0` | nginx 静态 + `/api` 反代 |
| `postgres:16-alpine` | 数据库 |
| `victoriametrics/victoria-metrics:v1.140.0` | 指标 |
| `victoriametrics/victoria-logs:v1.50.0` | 日志 |
| `grafana/grafana:11.0.0` | 可视化 |
| `grafana/alloy:v1.16.3` | 本机内置采集（host 指标 + Docker 日志） |
| `nginx:1.27-alpine` / `node:22-alpine` / `python:3.12-slim` | 构建中间层（可选保留，减小包体可删） |

## 开发机重新打包

```bash
cd deploy/offline
./package.sh 1.0.0
```

会：构建前后端 → 拉基础/监控镜像 → `docker save` 到 `images/`。

## 架构（同机部署）

```text
浏览器 → :8080 frontend → :8001 backend
                         → db:5432
init 容器: alembic upgrade head（在 backend 之前成功退出）
监控: :8428 VM  :9428 VLogs  :3000 Grafana
```

分体：监控装在另一台，`MONITORING_HOST` 填监控机 IP；`MONITOR_BIND=0.0.0.0`。

## 验收

1. `curl http://<IP>:8080/` 前端可开  
2. `curl http://<IP>:8001/api/settings/login-page` 返回 JSON  
3. 登录成功；监控连接 self-check 全绿  
4. 采集页给业务机装 Alloy → Agent online → 日志查询有 `job:container`

## 常用运维

```bash
cd deploy/offline
docker compose -f app/docker-compose.yaml --env-file .env ps
docker compose -f app/docker-compose.yaml --env-file .env logs -f backend
docker compose -f monitoring/docker-compose.yaml --env-file .env ps
```
