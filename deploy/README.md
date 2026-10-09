# OpsPilot 部署手册

> 两种场景：**增量更新**（已有环境升级镜像）和 **离线全新安装**（空机从 0 装）。

---

## 一、增量更新（日常发版）—— 一键

### Windows 一键

```bat
部署-更新.bat root@10.10.0.9
```

### 命令行（Git Bash / Linux / macOS）

```bash
# 前后端都发（自动在目标机当前 tag 上 +1，如 1.0.12 → 1.0.13）
bash deploy/update/deploy_update.sh --host root@10.10.0.9

# 只发前端 / 只发后端
bash deploy/update/deploy_update.sh --host root@10.10.0.9 --frontend-only
bash deploy/update/deploy_update.sh --host root@10.10.0.9 --backend-only

# 指定版本号
bash deploy/update/deploy_update.sh --host root@10.10.0.9 --backend-tag 1.0.7 --frontend-tag 1.0.13

# 只做健康检查（不构建不重启）
bash deploy/update/deploy_update.sh --host root@10.10.0.9 --check-only
```

### 脚本会自动完成

1. SSH 探测目标机 `STAGE_DIR`（默认 `/opt/opspilot-src/opspilot-offline-stage`）
2. 读目标机当前镜像 tag，递增 patch 位
3. 本机 `docker build` 前后端
4. 传输：后端（~1GB）走 `docker save | ssh docker load` 流式；前端 scp
5. 改 `.env` 的 `OPSPILOT_*_IMAGE`
6. `docker compose up -d backend frontend`
7. 健康检查：`/`、`/api/settings/login-page`、容器状态、后端 ERROR 日志

### 前提

| 项 | 要求 |
|---|---|
| 本机 | Docker、ssh/scp，能免密登录目标机 |
| 目标机 | 已完成一次离线安装（有 compose 与 `.env`） |
| 网络 | 本机能 ssh 到目标机即可（镜像走 SSH 流，无需额外端口） |

### 特殊情况

| 场景 | 用法 |
|---|---|
| 目标机安装目录不同 | `--stage-dir /your/path` |
| 后端 scp 代替流式 | `--backend-scp` |
| 改完没起容器 | 目标机：`cd $STAGE_DIR && docker compose -f app/docker-compose.yaml --env-file .env up -d` |

---

## 二、离线全新安装（空机）

见 [`offline/README.md`](offline/README.md)。

```bash
# 1) 开发机打包（构建 + docker save 全部镜像）
cd deploy/offline && ./package.sh 1.0.0

# 2) 整目录拷到目标机（U盘/scp）
# 3) 目标机一键装
cd deploy/offline && sudo ./install.sh
# 或非交互
sudo ./install.sh --config env.example
```

安装产物：

- 应用栈：`app/docker-compose.yaml` → PG + init-db + backend + frontend
- 监控栈：`monitoring/docker-compose.yaml` → VM + VLogs + Grafana + Alloy（可选）

---

## 三、目录说明

```text
deploy/
├── README.md               # 本文件
├── update/                 # ★ 一键增量更新
│   └── deploy_update.sh
└── offline/                # ★ 离线全新安装
    ├── install.sh
    ├── package.sh
    ├── env.example
    ├── app/docker-compose.yaml
    ├── monitoring/
    ├── images/             # package.sh 产出（大文件，不入 Git）
    └── scripts/            # 目标机一次性运维脚本（1009 等，不入 Git）
```

仓库根另有：

| 文件 | 用途 |
|---|---|
| `部署-更新.bat` | Windows 一键增量更新 |
| `检查-全量.bat` | 测试/lint/build 统一检查 |
| `启动-后端/前端/全部.bat` | 本机开发启动 |
| `docker-compose.yml` | 本机 compose（开发/演示用，非目标机安装用） |

---

## 四、目标机运维速查

```bash
# 登录
ssh root@10.10.0.9

# 状态
cd /opt/opspilot-src/opspilot-offline-stage
docker ps | grep opspilot
grep OPSPILOT_ .env | grep IMAGE

# 重启应用栈
set -a && source .env && set +a
docker compose -f app/docker-compose.yaml --env-file .env up -d backend frontend

# 重启监控栈
docker compose -f monitoring/docker-compose.yaml --env-file .env up -d

# 健康检查
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/settings/login-page
```

**访问**：`http://<目标机>:8080`，管理员密码在 `.env` 的 `INIT_ADMIN_PASSWORD`。

---

## 五、发版建议流程

```text
改代码 → 检查-全量.bat → 部署-更新.bat root@10.10.0.9 → 浏览器强刷验证
```

---

## 六、Grafana VictoriaLogs 数据源插件（日志钻取）

日志查询页的「Grafana」按钮会带 LogsQL 跳到 Grafana Explore 的 **VictoriaLogs**
数据源（uid `opspilot-vlogs`，provisioning 文件随 `monitoring/config/grafana/` 下发）。
该插件 **不在 grafana.com 目录**（`GF_INSTALL_PLUGINS` 会 404），需要手动装一次：

```bash
# 在监控机上执行（需外网；GitHub 直连慢可用 gh-proxy 前缀）
VER=v0.32.0
REPO=https://github.com/VictoriaMetrics/victorialogs-datasource/releases/download
curl -L -o /tmp/logs-ds.zip "$REPO/$VER/victoriametrics-logs-datasource-$VER.zip"
mkdir -p /tmp/logs-ds && unzip -o /tmp/logs-ds.zip -d /tmp/logs-ds
docker cp /tmp/logs-ds/victoriametrics-logs-datasource opspilot-grafana:/var/lib/grafana/plugins/
docker restart opspilot-grafana
```

- `/var/lib/grafana/plugins` 在 `grafanadata` 卷里，装一次跨重建有效。
- compose 已带 `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=victoriametrics-logs-datasource`。
- 验证：Grafana → Connections → Data sources 应出现「OpsPilot VictoriaLogs」。
