# OCI Telegram Bot (甲骨文自动化运维与抢机机器人)

基于 Python 与 Docker 构建的 Oracle Cloud Infrastructure (OCI) 自动化运维工具。支持多账号凭据管理、自动化子网初始化、安全列表全端口放行、批量抢占 ARM/AMD 实例，并通过 Telegram Bot 提供实时交互面板与日志查看。

---

## ✨ 功能特性

* **多账号隔离**：支持单容器管理多个 OCI 租户账号，目录结构清晰。
* **自动化网络构建**：新账号首次启动自动创建 VCN、Internet 网关、路由表及公网 Subnet。
* **全端口安全放行**：自动同步更新安全列表规则，开放全入站端口（0.0.0.0/0），省去控制台繁琐配置。
* **智能轮询抢机**：后台异步多线程轮询，自动捕获 `Out of capacity` 缺货状态并按设定间隔自动重试，成功后即刻停止并推送结果。
* **开机即用配置**：实例启动时自动注入 cloud-init，启用 root 密码登录并预设 SSH 密码。
* **Telegram 交互中控**：
  * 支持交互式内联键盘切换账号、查看配额与实例状态。
  * 实时抓取后台运行日志至 TG 对话框。
  * 随时通过指令或面板动态修改轮询间隔与默认密码。

---

## 🚀 快速开始

### 1. 环境准备与项目克隆

在全新 VPS 上执行以下命令安装 Docker 并克隆仓库：

```bash
# 安装 Docker 环境
curl -fsSL [https://get.docker.com](https://get.docker.com) | bash

# 克隆项目代码
mkdir -p /root/docker && cd /root/docker
git clone [https://github.com/game315422/oci-bot.git](https://github.com/game315422/oci-bot.git)
cd oci-bot
