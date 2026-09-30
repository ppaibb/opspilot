# 虚拟组网（WireGuard）—— 类 wg-easy 功能落地

> 创建日期：2026-09-19  
> 状态：P0 完成（后端 11 路由 + 12 契约测试全绿；前端组网管理页全功能）

## 📊 功能定位

在 CMDB 之上叠加 **WireGuard 虚拟组网**编排：选出口节点（主机资产）创建隧道网段，
成员（主机/移动端）自动分配隧道 IP 并生成配置——对标 wg-easy 的对端管理体验，
叠加 OpsPilot 特有的**主机资产关联**与**脚本通道下发**。

**与 wg-easy 的差异**：wg-easy 需部署在 WG 服务器本体上；OpsPilot 是控制面——
密钥/配置在平台侧生成，下发靠生成的安装脚本（可贴进「脚本执行」远程执行），
不要求平台部署在 WG 节点上。

## 一、核心概念

| 概念 | 说明 |
|---|---|
| 组网（Network） | 一个隧道网段（如 10.8.0.0/24）+ 一台出口节点 + 监听端口 |
| 出口节点 | 网段 .1 自动保留；从 CMDB 主机选择，端点自动带出 IP（可改域名） |
| 成员（Peer） | 隧道 IP 自动分配下一个空闲位（.2 起）；可关联 CMDB 主机或手工登记 |
| 全隧道 / 分流 | 成员 AllowedIPs=0.0.0.0/0（全流量走隧道）或仅路由网段 |
| 禁用成员 | 服务端配置重新生成时移除其 [Peer] 段（记录保留，可恢复） |

## 二、后端

### 数据模型（`app/models/wireguard.py`）

- `wg_networks`：名称/网段/出口资产快照/endpoint/port/服务端密钥（私钥加密）/出口网卡/
  全隧道/DNS；**网段与密钥创建后不可变**（变更=重建组网）
- `wg_peers`：名称/关联资产/隧道 IP（network+IP 唯一约束）/密钥（加密）/启停
- Alembic：`20260919_create_wireguard_tables`（接在 finance 之后）

### 服务层（`app/services/wireguard.py`，纯函数、12 个契约测试）

- **密钥**：X25519（cryptography 库，与 `wg genkey|wg pubkey` 等价）——零新依赖；
  私钥 `encrypt_secret` 加密落库，接口永不回传明文，明文仅经配置下载/二维码两个出口
- **IP 分配**：ipaddress 标准库；跳过 .1 与已占用；耗尽返回 None
- **配置渲染**：服务端（含 iptables 转发/NAT PostUp/Down、仅启用成员）/
  成员（Address/DNS/Endpoint/AllowedIPs 按全隧道或分流/PersistentKeepalive=25）
- **部署脚本**：服务端与成员各一份 bash（apt 装 wireguard → heredoc 写配置 → 
  systemctl enable --now），内嵌配置，可贴进「脚本执行」远程执行
- **二维码**：segno 纯 Python PNG data URI（⚠️ 新增依赖 `segno>=1.6.0`，纯 Python 无 PIL）

### API（`/api/wireguard`，11 路由，模块权限 `module.wireguard.manage`）

| 路由 | 说明 |
|---|---|
| GET/POST `/networks` | 列表（含成员数）/ 创建（同名拒绝 + **网段重叠检测**） |
| PUT/DELETE `/networks/{id}` | 编辑元信息 / 删除（连带成员记录，服务器侧接口提示人工卸载） |
| GET `/networks/{id}/peers` | 成员列表 |
| POST `/networks/{id}/peers` | 添加成员（IP 自动分配或手工指定，占用即 422） |
| PATCH/DELETE `/peers/{id}` | 启停/备注 / 删除 |
| GET `/networks/{id}/server-config` | 服务端 conf（download=1 走附件下载） |
| GET `/networks/{id}/server-install-script` | 出口节点安装脚本 |
| GET `/peers/{id}/config` | 成员 conf + 二维码 data URI + 安装脚本（一次拉齐） |

## 三、前端（`pages/assets/wireguard/index.tsx`）

- **组网卡片墙**：名称/网段/出口/成员数/模式；点击进成员管理抽屉；卡片可直接复制服务端公钥
- **成员管理抽屉**：
  - 服务端卡：下载 wg0.conf、生成安装脚本（Modal 展示+复制）、重载提示
  - 成员表：名称/隧道 IP/关联资产/启停/删除
  - 添加成员 Modal：名称 + 关联主机（可选）+ IP 留空自动分配
- **成员配置弹窗**：二维码（移动端扫码导入）+ 配置文本 + 安装脚本三视图，下载/复制
- 菜单：资产管理 → 虚拟组网；权限 `module.wireguard.manage`（运维角色已授权）

## 四、使用流程（首次接入）

1. **创建组网**：填名称/网段（如 10.8.0.0/24）→ 选出口节点（自动带出端点）→ 保存
2. **部署出口**：下载服务端安装脚本 → 贴进「脚本执行」对出口节点执行
   （或手工在服务器跑；需放行 UDP 端口）
3. **添加成员**：填名称（可关联主机）→ 点「配置」→ 成员主机用安装脚本下发，
   移动端扫码导入
4. **启停/删除成员**后重新下载服务端 conf 并在出口节点 `systemctl restart wg-quick@wg0`

## 五、验证

- 后端：`test_wireguard_contract` 12/12（密钥格式/加密往返/IP 分配三态/配置渲染/
  禁用排除/全隧道分流/heredoc 闭合）+ 全量回归 61 用例全绿
- 前端：tsc 零错误、vite build 通过
- 依赖：`segno>=1.6.0` 已加入 requirements.txt（**新增依赖声明**）

## 五点五、三项增强（2026-09-19 补记，已交付）

1. **状态回读**：`GET /networks/{id}/status` SSH 到出口节点解析 `wg show dump`
   （在线判定 180s 窗口），成员表新增在线列（握手时间/流量 Tooltip）；不可达优雅降级
2. **脚本执行深度集成**：成员配置弹窗「远程安装到主机」直接调 Ansible shell 通道下发
   （关联资产时可用，高危确认）
3. **主机联动**：主机列表「更多 → 虚拟组网」抽屉（`GET /by-asset/{asset_id}`），
   展示所属组网/隧道 IP/公钥/启停

## 六、P1 规划

- **状态回读**：SSH 到出口节点执行 `wg show` 解析握手/流量（复用资产 SSH 凭证）
- 脚本执行深度集成：添加成员直接弹出预填的 Ansible shell 任务
- 成员配置过期轮换（重新生成密钥对）
- 隧道拓扑图（出口-成员连线可视化）
