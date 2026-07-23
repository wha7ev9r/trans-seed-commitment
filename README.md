# trans-commitment

> 在 VPS 上做种 Ubuntu 镜像（及任意 .torrent），回馈开源社区。
> 内嵌 Transmission + Flood for Transmission 现代 UI + 月度流量配额守护 + VPS 流量监控。

---

## 功能一览

- **内嵌 Transmission**（`linuxserver/transmission`），开箱即用
- **Flood for Transmission** 现代 Web UI，深色/浅色主题，移动端友好
- **月度流量配额**：按 Transmission session 增量统计上传量，接近安全线自动暂停；月初恢复配额暂停的种子
- **VPS 流量监控**（`vnstat`）：监控指定网卡整体流量，展示非 Transmission 流量；不代替厂商硬限额
- **统一控制台**：紧凑状态控制台 + 本地 Canvas 流量趋势 + 下方 iframe Flood UI
- **手动控制**：面板上可暂停/恢复全部种子、实时修改月度配额
- **强制加密**（`encryption=2`）：所有 peer 连接加密 + IP blocklist 防恶意 peer
- **一键 Ubuntu 做种**：`ubuntu-torrents/list.txt` + `fetch.sh`，wget 到 watch 目录自动加载
- **自定义做种**：Flood UI 手动添加、丢 `.torrent` 到 `watch/`、自己创建种子发布，三种方式全支持
- **两层凭证**：quota-guard Basic Auth + Transmission RPC 用户名密码
- **Docker Compose 部署**：Transmission、quota-guard、vnStat 采集和内网查询服务协作

---

## 快速开始

### 前置条件

- Docker 20.10+ + Docker Compose V2
- VPS 至少 20 GB 可用磁盘（单版 Ubuntu ISO ~6 GB）
- P2P 端口 51413 **必须在 VPS 防火墙/安全组放行（TCP + UDP）**（否则无法做种）

### 部署

```bash
# 1. 克隆项目
git clone https://github.com/YOUR_USER/trans-commitment.git
cd trans-commitment

# 2. 初始化（首次运行会创建 .env，然后退出）
bash setup.sh

# 3. 编辑 .env（改密码、配额、网卡名）
vim .env

# 4. 再次运行 setup（校验 Flood UI、目录权限并复制 settings.json）
bash setup.sh

# 5. 启动
docker compose up -d

# 6. 检查状态
docker compose ps
docker compose logs quota-guard | tail -20
```

### 导入 Ubuntu 种子

```bash
cd ubuntu-torrents && bash fetch.sh
```

Transmission 会通过 watch 目录自动检测到 .torrent 文件并开始下载 ➔ 做种。

> `list.txt` 默认包含 Ubuntu 24.04 / 22.04 / 25.10 / 20.04 的 desktop + server 各版本。  
> **想做种其他文件？**
>
> - 在 Flood UI 里手动添加任意 .torrent URL 或磁力链接
> - 复制 `list.txt` 加上你自己的 .torrent 链接，再 run `fetch.sh`
> - 把你自己的 .torrent 文件直接丢进 `watch/` 目录
> - 用 Transmission 创建自己的种子：Flood UI → 添加种子 → 选择本地文件 → 指定 tracker URL

### 访问面板

| 地址                     | 内容                                | 认证                                                                     |
| ------------------------ | ----------------------------------- | ------------------------------------------------------------------------ |
| `http://你的VPS-IP:9092` | 统一控制台（状态栏 + Flood iframe） | quota-guard Basic Auth（`.env` 中 `QUOTA_USER/QUOTA_PASS`）              |
| Flood iframe             | Transmission 内嵌                   | Transmission RPC 认证（`.env` 中 `TRANSMISSION_USER/TRANSMISSION_PASS`） |

> **重要**：`127.0.0.1:9091` 和 `127.0.0.1:9092` 只绑本地。  
> 公网访问请走反向代理 + HTTPS。下面的 OpenResty 配置是关键。
>
> `setup.sh` 会根据 `.env` 的 `PUID/PGID` 准备运行目录。不要用 UID 与 `PUID` 不一致的普通用户执行初始化。

---

## OpenResty 反向代理（推荐）

> Port 9092 is **not** exposed to the internet.
> Use reverse proxy + HTTPS for production access.

### OpenResty / nginx 配置示例

```nginx
server {
    listen 443 ssl;
    server_name seed.your-domain.com;

    ssl_certificate     /etc/ssl/your-domain.crt;
    ssl_certificate_key /etc/ssl/your-domain.key;

    # 统一入口 — quota-guard 控制台 + Flood iframe + RPC 反代
    location / {
        proxy_pass http://127.0.0.1:9092;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    # 如需单独直接访问 Transmission（绕过 quota-guard），可以加：
    # location /direct/ {
    #     proxy_pass http://127.0.0.1:9091/;
    #     proxy_buffering off;
    # }
}
```

> **注意**：`proxy_buffering off` 可避免代理缓存控制台和 RPC 响应，较长的 `proxy_read_timeout` 可以容忍慢请求。
> **Flood UI 的反代路径**：quota-guard 内部把 `/torrents/*` 路径重写后传给 transmission:9091。你只需把 `/` 路由到 9092 即可。

### 网关安全

P2P 端口 `51413` 必须公网可达，但可以限速防滥用：

```bash
# ufw 限速，防止 SYN flood
ufw limit 51413/tcp
ufw limit 51413/udp
```

---

## 面板使用指南

### 顶部状态栏

打开 `https://seed.your-domain.com/`，顶部固定状态栏实时显示：

| 字段                    | 说明                                                   |
| ----------------------- | ------------------------------------------------------ |
| 本月已用                | Transmission 本月累计上传流量                          |
| 流量轨道                | 青色→琥珀→红色；安全线前默认预留 1 GiB                 |
| 配额                    | 当前月度配额 — **点击编辑按钮可实时修改**（无需重启）  |
| VPS 总                  | vnstat 监控的 VPS 网卡总流量（含非 Transmission 流量） |
| 活跃/种子               | 当前活跃上传数 / 做种总数                              |
| 流量趋势                | 最近 60 天每日上传量（点击展开本地趋势图）             |
| **暂停全部 / 恢复全部** | 一键暂停或恢复；达到安全线后需先提高配额               |
| 实时 ↑                  | 当前实时总上传速率                                     |

### 修改月度配额

1. 状态栏里点配额的 ✏️ 按钮
2. 输入新配额（单位：TiB，支持小数，如 `2.5` = 2.5 TiB）
3. 点 OK
4. 接近安全线自动暂停（默认检测周期 ≤ 10 秒）

> 为降低轮询停止期间超额的风险，默认在配额前预留 1 GiB，最多预留配额的 10%。这仍是应用层保护，不是网络层硬限额。

### 添加自定义种子

三种方式：

1. **Flood UI**（iframe 下方）：点 ➕ → 输入 .torrent URL 或 磁力链接 → Add
2. **Watch 目录**：把 `.torrent` 文件丢到 `watch/` 目录 → Transmission 秒级自动加载
3. **自己建种作种**：用 `transmission-create` 或 GUI 工具创建 .torrent → 丢到 watch/ → 自动做种

---

## 配置参考（.env）

| 变量                         | 默认值                   | 说明                                                                              |
| ---------------------------- | ------------------------ | --------------------------------------------------------------------------------- |
| `TZ`                         | `Asia/Shanghai`          | 时区                                                                              |
| `TRANSMISSION_USER`          | `admin`                  | Transmission RPC 用户名                                                           |
| `TRANSMISSION_PASS`          | _(必须修改)_             | Transmission RPC 密码（≥20位推荐）                                                |
| `TRANSMISSION_RPC_WHITELIST` | Docker 私网通配符        | RPC 来源白名单；Transmission 支持 `*`，不支持 CIDR                                |
| `QUOTA_USER`                 | `seed`                   | quota-guard 面板 Basic Auth 用户名                                                |
| `QUOTA_PASS`                 | _(必须修改)_             | quota-guard 面板 Basic Auth 密码                                                  |
| `QUOTA_GUARD_PORT`           | `9092`                   | quota-guard 绑定端口（本地）                                                      |
| P2P 端口                     | `51413`                  | 固定值；如需改端口，需同步改 `docker-compose.yml` 和 `transmission/settings.json` |
| `MONTHLY_QUOTA_BYTES`        | `1099511627776`（1 TiB） | 月度配额（字节）                                                                  |
| `CHECK_INTERVAL_SECONDS`     | `10`                     | 配额检查周期（秒）                                                                |
| `QUOTA_SAFETY_MARGIN_BYTES`  | `1073741824`（1 GiB）    | 配额前安全余量，最多按配额的 10% 计算                                             |
| `PUID` / `PGID`              | `1000`                   | 容器进程 UID/GID                                                                  |
| `VNSTAT_INTERFACE`           | `eth0`                   | 宿主机主网卡名（`ip link` 查看，面板按此接口筛选）                                |
| 上传限速                     | `10240`（10MB/s）        | 写在 `transmission/settings.json` 的 `speed-limit-up`                             |
| 下载限速                     | 不启用                   | 写在 `transmission/settings.json` 的 `speed-limit-down-enabled=false`             |

---

## 安全加固清单

部署到公网后，务必逐项确认：

- [ ] `.env` 中 `TRANSMISSION_PASS` 和 `QUOTA_PASS` 已改为强随机密码（`openssl rand -base64 24`）
- [ ] P2P 端口 51413 已在 VPS 安全组放行 **TCP + UDP**
- [ ] `encryption: 2`（强制加密）已在 `transmission/settings.json` 中（模板默认启用）
- [ ] Blocklist 启用（`blocklist-enabled: true`），阻止恶意 peer IP
- [ ] `.env` 中 `TRANSMISSION_RPC_WHITELIST` 仅允许本机和实际 Docker 私网来源
- [ ] UFW 对 51413 限速：`ufw limit 51413/tcp && ufw limit 51413/udp`
- [ ] OpenResty 配置了 HTTPS；quota-guard Basic Auth 已启用
- [ ] 宿主机没有对公网开放 vnStat 端口 8685（当前 Compose 默认不映射）
- [ ] Transmission 不设 `LPD`（公网环境关掉本地 peer 发现）
- [ ] VPS 开启了 rate limiting / conntrack 调优（如 1Gbps 以上带宽）
- [ ] 定期检查并更新 Compose 中固定的 Transmission、vnStat 和 Python 依赖版本

### 调优建议

编辑 `./transmission/config/settings.json`（容器停止后修改）：

```json
{
  "encryption": 2,
  "cache-size-mb": 128,
  "peer-limit-global": 500,
  "peer-limit-per-torrent": 100,
  "seed-queue-size": 50,
  "upload-slots-per-torrent": 20,
  "utp-enabled": true,
  "peer-socket-tos": "throughput"
}
```

> 上述值是比模板更激进的可选调优，不是当前默认值。修改现有键并保持文件为合法 JSON；编辑后执行 `docker compose restart transmission`。

---

## 自定义做种（自选种子）

本项目的 Ubuntu 清单只是起点。你可以完全自定义做种内容：

### 方法一：Flood UI 手动添加

1. 打开面板 → 在 iframe 内 Flood UI 点击 ➕
2. 粘贴 `.torrent` 文件 URL 或磁力链接
3. 点击 Add — 立即开始下载/做种

### 方法二：Watch 目录自动

```bash
# 任何 .torrent 文件丢进 watch/ 目录，秒级自动加载
cp your-file.torrent ./watch/
```

### 方法三：自定义清单批量

```bash
# 复制清单文件自己改
cp ubuntu-torrents/list.txt ubuntu-torrents/my-list.txt
# 加上你的 .torrent URL（每行一个）
echo "https://example.com/myfile.iso.torrent" >> ubuntu-torrents/my-list.txt
# 拉取
bash ubuntu-torrents/fetch.sh  # 用默认 list.txt
# 或用自定义清单：修改 fetch.sh 里的 LIST_FILE 变量
```

### 方法四：自己建种发布

Transmission 支持创建新种子：

```bash
# 在 Transmission 容器内或用 transmission-remote
transmission-create -p -t udp://tracker.opentrackr.org:1337/announce \
  -o my-release.torrent /path/to/your/files
```

然后把 `my-release.torrent` 放 `watch/` 目录或通过 Flood UI 添加。

---

## 项目结构

```
trans-commitment/
├── docker-compose.yml        # Transmission、quota-guard、vnStat 采集/查询编排
├── setup.sh                  # 校验下载 Flood UI / 创建目录 / 对齐权限 / 复制配置
├── .env.example              # 环境变量模板
├── .gitignore
├── README.md
│
├── transmission/
│   └── settings.json         # 加密/限速/白名单/blocklist/watch-dir 模板
│
├── quota-guard/              # 月度配额守护 + 统一控制台
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── guard.py              # Flask + APScheduler + RPC + 反代 + HTML 控制台
│   └── tests/
│       └── test_guard.py     # 配额状态机与 vnStat 解析回归测试
│
├── ubuntu-torrents/
│   ├── list.txt              # 官方 .torrent 直链清单
│   └── fetch.sh              # 一键下载到 watch/
│
├── flood-ui/                 # Flood for Transmission UI（setup.sh 下载，.gitignored）
├── watch/                    # 监控目录（.gitignored）
├── downloads/                # 做种文件存放（.gitignored）
├── transmission/config/       # 运行时配置（.gitignored）
└── quota-guard/state/        # 配额持久化 JSON（.gitignored）
```

vnStat 数据使用 Compose 命名卷 `vnstat-data`，采集容器不开 HTTP，查询容器只在 Compose 内网监听 8685。

---

## 维护与更新

```bash
# 更新镜像（Transmission / vnStat 等远程镜像）
docker compose pull
# 重建本地 quota-guard 镜像（代码变更后必须执行）
docker compose build quota-guard
docker compose up -d

# 查看日志
docker compose logs -f --tail=50 quota-guard
docker compose logs -f --tail=50 transmission

# 重启
docker compose restart

# 运行 quota-guard 回归测试
python -m unittest discover -s quota-guard/tests -v

# 重建容器并删除 vnstat-data 命名卷
# downloads、Transmission 配置和 quota-guard 状态是 bind mount，不会被删除
docker compose down -v
bash setup.sh
docker compose up -d
```

---

## License

MIT
