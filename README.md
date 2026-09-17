# OCI Telegram Bot 使用教程

基于 Python 与 Docker 构建的甲骨文云（Oracle Cloud Infrastructure）多账号并发运维与抢机机器人。

---

## 准备工作

* 一台境外 VPS（用于运行 Docker 服务）
* Telegram 账号及创建好的 Bot Token（通过 [@BotFather](https://t.me/BotFather) 获取）
* Telegram 纯数字 ID（通过 [@userinfobot](https://t.me/userinfobot) 获取）
* 甲骨文 API 凭据：`.pem` 私钥和 `config.txt`
* （可选）各账号专属代理（HTTP / SOCKS5）

---

## 快速开始

### 1. 基础环境与拉取代码

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | bash

# 克隆仓库
mkdir -p /root/docker && cd /root/docker
git clone https://github.com/game315422/oci-bot.git
cd oci-bot

```

### 2. 配置账号凭据与专属代理

在 `accounts` 目录下为每个甲骨文账号单独创建子目录（例如 `acc1`）：

```bash
mkdir -p accounts/acc1

```

将该账号的 `oci_api_key.pem` 与 `config.txt` 上传到对应子目录下。

**`config.txt` 规范格式：**

```ini
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaaxxxxx
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
key_file=/app/accounts/acc1/oci_api_key.pem
tenancy=ocid1.tenancy.oc1..aaaaaaaaxxxxx
region=ap-singapore-2

```

> **注意**：`key_file` 路径必须使用容器内的映射路径 `/app/accounts/子目录名/私钥文件名.pem`。

**（可选）配置专属代理防关联合并：**

为避免多个账号共用宿主机同一个出口 IP 导致风控连带封号，可在账号目录内放入 `proxy.txt`（仅需写入一行链接，不放则默认 VPS 直连）：

```text
accounts/
├── acc1/
│   ├── config.txt
│   ├── oci_api_key.pem
│   └── proxy.txt          <--- 该账号走专属代理
└── acc2/
    ├── config.txt
    └── oci_api_key.pem    <--- 直连模式

```

* `proxy.txt` 内容格式示例：
* **HTTP**：`[http://123.45.67.89:8080](http://123.45.67.89:8080)` 或 `[http://user:pass@123.45.67.89:8080](http://user:pass@123.45.67.89:8080)`
* **SOCKS5**：`socks5://123.45.67.89:1080` 或 `socks5://user:pass@123.45.67.89:1080`



### 3. 配置环境变量

检查或编辑 `docker-compose.yml` 中的环境变量：

```yaml
services:
  oci-bot:
    build: .
    container_name: oci-tg-bot
    restart: always
    volumes:
      - ./accounts:/app/accounts
    environment:
      - TG_BOT_TOKEN=你的BotToken
      - TG_ADMIN_ID=你的Telegram纯数字ID
      - DEFAULT_ROOT_PASSWORD=你的预设root密码
      - MONTHLY_QUOTA_GB=10000
      - DEFAULT_INTERVAL=150
      - MIN_INTERVAL=30
      - TZ=Asia/Shanghai

```

### 4. 启动与管理

```bash
# 构建并启动服务
docker compose up -d --build

# 实时查看运行日志
docker compose logs -f

# 停止服务
docker compose down

```

---

## Telegram 控制台功能

* `/start`：唤起主控制面板与账号列表。
* `/set_pwd <新密码>`：动态修改指定账号开机后的初始 root 密码。
* **🎯 开始抢机 / 🛑 停止抢机**：支持二次确认防误触，各账号独立任务并发运行。
* **⚙️ 规格与台数设置**：自由切换 ARM（1C6G 至 4C24G）与 AMD 微型机，调整目标开机数量。
* **⚡ 实例电源**：直接查看当前机器 IP、开关机、软/硬重启。
* **🔄 更换公网 IP**：主界面一键直达，释放并重新申请临时公网 IP。
* **💾 引导卷管理**：查看所有引导卷大小与绑定机器，支持单独删除闲置卷释放免费配额。
* **🌐 测试出口 IP**：一键测试当前账号实际走出的外网 IP 与延迟，即时验证代理是否生效。
* **🔓 端口全开**：一键更新 OCI 后台安全列表（Security Lists）与网络安全组（NSG）入站规则（`0.0.0.0/0:all`）。

---

## 核心注意事项

* **单实例原则**：Telegram Bot 采用长轮询模式，同一个 `TG_BOT_TOKEN` **严禁在两台 VPS 上同时运行**，否则会出现 `409 Conflict` 报错。切换机器前必须在原服务器执行 `docker compose down`。
* **限流与封号防护**：轮询间隔建议保持在 **60s - 150s**。已配置代理的账号在代理断开时会强行阻断请求，绝不偷跑宿主 IP；未在面板操作时 API 调用严格为 0。
* **引导卷大小**：Ubuntu 24.04 等官方系统镜像要求引导卷空间不得低于 **50GB**。彻底删除实例时支持联动清空引导卷，防止扣除免费磁盘额度。
