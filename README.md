# OCI Telegram Bot 使用教程

基于 Python 与 Docker 构建的甲骨文云（Oracle Cloud Infrastructure）自动化运维与抢机机器人。

---

## 准备工作

* 一台境外 VPS（用于运行 Docker 服务）
* 一个 Telegram 账号及创建好的 Bot Token（通过 [@BotFather](https://t.me/BotFather) 获取）
* 你的 Telegram 纯数字 ID（通过 [@userinfobot](https://t.me/userinfobot) 获取）
* 甲骨文 API 凭据：`oci_api_key.pem` 和 `config.txt`

---

## 快速开始

### 1. 安装基础环境与拉取代码

在目标 VPS 终端执行以下命令：

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | bash

# 克隆仓库
mkdir -p /root/docker && cd /root/docker
git clone https://github.com/game315422/oci-bot.git
cd oci-bot

```

### 2. 配置账号凭据

在 `accounts` 目录下为每个甲骨文账号单独创建子目录（例如 `acc1`）：

```bash
mkdir -p accounts/acc1

```

将该账号的 `oci_api_key.pem` 与 `config.txt` 上传到 `accounts/acc1/` 目录下。

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

## Telegram 交互指令

* `/start`：调出主控制面板与账号列表
* **🚀 开始抢机**：选择实例配置（ARM/AMD）、核心与内存大小，提交抢机任务
* **⏹ 停止抢机**：终止当前账号正在运行的抢机任务
* **📋 查看日志**：查看后台最近的轮询重试记录
* `/set_pwd <新密码>`：动态修改开机后的初始 root 密码

---

## 核心注意事项

* **单实例原则**：Telegram Bot 采用长轮询模式，同一个 `TG_BOT_TOKEN` **严禁在两台 VPS 上同时运行**，否则会出现 `409 Conflict` 报错。切换机器前请先在老机器上执行 `docker compose down`。
* **限流防护**：轮询间隔建议保持在 **60s - 150s**。如果出现 `API 限流 (429)`，系统会自动等待冷却恢复，无需手动干预。
* **引导卷大小**：Ubuntu 24.04 等官方镜像要求硬盘空间不得低于 **50GB**。
