import asyncio
import base64
from collections import deque
from datetime import datetime, timezone, timedelta
import glob
import logging
import os
import sys
import time
import urllib.request
import configparser
import oci
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# 内存日志队列（保留最近 50 条供 TG 面板查看）
LOG_HISTORY = deque(maxlen=50)

def add_bot_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    LOG_HISTORY.append(f"[{ts}] {msg}")
    logger.info(msg)

# ================= 服务端 VPS 公网 IP 自动获取与缓存 =================
SERVER_IP = "Unknown"

def get_server_ip() -> str:
    global SERVER_IP
    if SERVER_IP != "Unknown":
        return SERVER_IP
    apis = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]
    for api in apis:
        try:
            req = urllib.request.Request(api, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                if resp.status == 200:
                    ip = resp.read().decode("utf-8").strip()
                    if ip:
                        SERVER_IP = ip
                        return SERVER_IP
        except Exception:
            continue
    return SERVER_IP

# ================= 1. 读取环境变量配置 =================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_ADMIN_ID_RAW = os.getenv("TG_ADMIN_ID", "").strip()

if not TG_BOT_TOKEN or not TG_ADMIN_ID_RAW:
    logger.error("❌ 缺少必需环境变量：TG_BOT_TOKEN 或 TG_ADMIN_ID，请检查 docker-compose.yml！")
    sys.exit(1)

try:
    ADMIN_USER_ID = int(TG_ADMIN_ID_RAW)
except ValueError:
    logger.error("❌ TG_ADMIN_ID 必须为纯数字！")
    sys.exit(1)

DEFAULT_ROOT_PASSWORD = os.getenv("DEFAULT_ROOT_PASSWORD", "RootPass@2026")
MONTHLY_QUOTA_GB = int(os.getenv("MONTHLY_QUOTA_GB", "10000"))
DEFAULT_INTERVAL = int(os.getenv("DEFAULT_INTERVAL", "30"))
MIN_INTERVAL = int(os.getenv("MIN_INTERVAL", "10"))

ACCOUNTS_DIR = "/app/accounts"
DUMMY_SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGf6s1K8f9yZ5Y8w4d5A2b1C3e4F5g6H7i8J9k0L1m2N dummy@oci"


# ================= 2. 多账号动态解析 =================
class OCIAccount:
    def __init__(self, name: str, folder_path: str):
        self.name = name
        self.folder_path = folder_path
        self.config_dict = self._parse_credentials()
        self.compute_client = oci.core.ComputeClient(self.config_dict)
        self.network_client = oci.core.VirtualNetworkClient(self.config_dict)
        self.monitoring_client = oci.monitoring.MonitoringClient(self.config_dict)
        self.identity_client = oci.identity.IdentityClient(self.config_dict)

        self.compartment_id = self.config_dict["tenancy"]
        self.availability_domain = None
        self.subnet_id = None
        self.vcn_id = None

    def _parse_credentials(self) -> dict:
        pem_files = glob.glob(os.path.join(self.folder_path, "*.pem"))
        if not pem_files:
            raise RuntimeError("未找到 .pem 私钥文件")
        target_pem = pem_files[0]
        try:
            os.chmod(target_pem, 0o600)
        except Exception:
            pass

        candidate_files = glob.glob(os.path.join(self.folder_path, "*.txt")) + [
            os.path.join(self.folder_path, "config")
        ]
        valid_cfg = None
        for f in candidate_files:
            if os.path.isfile(f):
                with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                    cnt = fp.read()
                    if "user=" in cnt and "fingerprint=" in cnt:
                        valid_cfg = f
                        break
        if not valid_cfg:
            raise RuntimeError("未找到有效配置文本")

        parser = configparser.ConfigParser()
        parser.read(valid_cfg, encoding="utf-8")
        sec_name = parser.sections()[0] if parser.sections() else "DEFAULT"
        sec = parser[sec_name]

        cfg = {
            "user": sec.get("user", "").strip(),
            "fingerprint": sec.get("fingerprint", "").strip(),
            "key_file": target_pem,
            "tenancy": sec.get("tenancy", "").strip(),
            "region": sec.get("region", "").strip(),
        }
        oci.config.validate_config(cfg)
        return cfg

    def ensure_network_ready(self):
        if not self.availability_domain:
            ads = self.identity_client.list_availability_domains(self.compartment_id).data
            if ads:
                self.availability_domain = ads[0].name

        subnets = self.network_client.list_subnets(self.compartment_id).data
        if subnets:
            self.subnet_id = subnets[0].id
            self.vcn_id = subnets[0].vcn_id
            return

        add_bot_log(f"[{self.name}] 正在自动创建 VCN 与公共子网...")
        vcn_details = oci.core.models.CreateVcnDetails(
            cidr_block="10.0.0.0/16",
            compartment_id=self.compartment_id,
            display_name=f"auto-vcn-{self.name}",
            dns_label=f"autovcn{int(time.time())}"[-15:]
        )
        vcn = self.network_client.create_vcn(vcn_details).data
        self.vcn_id = vcn.id
        time.sleep(3)

        igw_details = oci.core.models.CreateInternetGatewayDetails(
            compartment_id=self.compartment_id,
            is_enabled=True,
            vcn_id=self.vcn_id,
            display_name="auto-igw"
        )
        igw = self.network_client.create_internet_gateway(igw_details).data

        default_route_table_id = vcn.default_route_table_id
        route_rule = oci.core.models.RouteRule(
            destination="0.0.0.0/0",
            destination_type="CIDR_BLOCK",
            network_entity_id=igw.id
        )
        self.network_client.update_route_table(
            default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(route_rules=[route_rule])
        )

        subnet_details = oci.core.models.CreateSubnetDetails(
            cidr_block="10.0.0.0/24",
            compartment_id=self.compartment_id,
            vcn_id=self.vcn_id,
            display_name="auto-public-subnet",
            route_table_id=default_route_table_id,
            security_list_ids=[vcn.default_security_list_id],
            prohibit_public_ip_on_vnic=False
        )
        subnet = self.network_client.create_subnet(subnet_details).data
        self.subnet_id = subnet.id
        time.sleep(3)
        add_bot_log(f"[{self.name}] 默认公网已就绪: {self.subnet_id}")

    def open_all_security_ports(self) -> str:
        """遍历账号下所有 VCN 的安全列表和安全组，彻底放开 0.0.0.0/0 所有入站端口"""
        self.ensure_network_ready()
        
        # 1. 查询该区间下所有 VCN
        vcns = self.network_client.list_vcns(self.compartment_id).data
        if not vcns:
            raise RuntimeError("未在当前区间检测到任何可用 VCN")

        modified_lists = 0
        modified_nsgs = 0

        for vcn in vcns:
            # 2. 处理该 VCN 下的所有安全列表 (Security Lists)
            sec_lists = self.network_client.list_security_lists(self.compartment_id, vcn_id=vcn.id).data
            for sec_list in sec_lists:
                # 检查是否已存在全放行规则
                has_all_open = any(
                    rule.protocol == "all" and rule.source == "0.0.0.0/0"
                    for rule in sec_list.ingress_security_rules
                )
                
                new_ingress = list(sec_list.ingress_security_rules)
                if not has_all_open:
                    rule_all = oci.core.models.IngressSecurityRule(
                        protocol="all",
                        source="0.0.0.0/0",
                        source_type="CIDR_BLOCK",
                        is_stateless=False,
                        description="Allow All Traffic Ingress (Bot Auto)"
                    )
                    new_ingress.append(rule_all)

                update_details = oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=new_ingress,
                    egress_security_rules=sec_list.egress_security_rules
                )
                self.network_client.update_security_list(sec_list.id, update_details)
                modified_lists += 1

            # 3. 处理该 VCN 下的所有网络安全组 (NSG)
            try:
                nsgs = self.network_client.list_network_security_groups(
                    compartment_id=self.compartment_id, 
                    vcn_id=vcn.id
                ).data
                for nsg in nsgs:
                    rules = self.network_client.list_network_security_group_security_rules(nsg.id).data
                    has_nsg_all = any(
                        r.direction == "INGRESS" and r.protocol == "all" and r.source == "0.0.0.0/0"
                        for r in rules
                    )
                    if not has_nsg_all:
                        add_rule = oci.core.models.AddSecurityRuleDetails(
                            direction="INGRESS",
                            protocol="all",
                            source="0.0.0.0/0",
                            source_type="CIDR_BLOCK",
                            is_stateless=False,
                            description="Allow All Traffic Ingress (Bot Auto)"
                        )
                        self.network_client.add_network_security_group_security_rules(
                            nsg.id,
                            oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(security_rules=[add_rule])
                        )
                        modified_nsgs += 1
            except Exception as nsg_err:
                logger.warning(f"检查网络安全组异常: {nsg_err}")

        return f"已成功更新 `{len(vcns)}` 个 VCN：\n• 安全列表 (Security Lists): 共放行 `{modified_lists}` 个\n• 网络安全组 (NSG): 共放行 `{modified_nsgs}` 个\n全部入站协议与端口 (`0.0.0.0/0:all`) 现已全通！"


ACCOUNTS: dict[str, OCIAccount] = {}


def reload_all_accounts():
    global ACCOUNTS
    ACCOUNTS.clear()
    if not os.path.exists(ACCOUNTS_DIR):
        logger.error(f"未找到目录: {ACCOUNTS_DIR}")
        return

    for item in os.listdir(ACCOUNTS_DIR):
        full_path = os.path.join(ACCOUNTS_DIR, item)
        if os.path.isdir(full_path):
            try:
                acc = OCIAccount(item, full_path)
                ACCOUNTS[item] = acc
                add_bot_log(f"[+] 成功载入账号: [{item}] ({acc.config_dict['region']})")
            except Exception as e:
                add_bot_log(f"[-] 载入账号 [{item}] 失败: {e}")


reload_all_accounts()


# ================= 3. 业务工具函数 =================
def generate_user_data(root_password: str) -> str:
    script = f"""#!/bin/bash
echo "root:{root_password}" | chpasswd
sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config
sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config
rm -rf /etc/ssh/sshd_config.d/* 2>/dev/null || true
systemctl restart sshd || systemctl restart ssh
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
iptables -F
iptables -X
netfilter-persistent save 2>/dev/null || true
"""
    return base64.b64encode(script.encode("utf-8")).decode("utf-8")


def find_ubuntu_24_image(acc: OCIAccount, arch: str) -> str:
    shape_target = "VM.Standard.A1.Flex" if arch == "aarch64" else "VM.Standard.E2.1.Micro"
    try:
        images = acc.compute_client.list_images(
            compartment_id=acc.compartment_id,
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            shape=shape_target,
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        if images:
            return images[0].id
    except Exception as e:
        logger.warning(f"过滤镜像异常: {e}")

    all_images = acc.compute_client.list_images(
        compartment_id=acc.compartment_id, sort_by="TIMECREATED", sort_order="DESC"
    ).data
    for img in all_images:
        name = img.display_name.lower()
        if "24.04" in name:
            if arch == "aarch64" and ("aarch64" in name or "arm" in name):
                return img.id
            elif arch == "x86_64" and ("aarch64" not in name and "arm" not in name):
                return img.id
    raise RuntimeError(f"未在区域 {acc.config_dict['region']} 找到 Ubuntu 24.04 镜像")


def launch_vm(acc: OCIAccount, spec: dict, current_idx: int = 1) -> dict:
    arch = spec.get("arch", "ARM")
    boot_gbs = spec.get("boot_gbs", 50)
    root_pwd = spec.get("root_password", DEFAULT_ROOT_PASSWORD)

    try:
        acc.ensure_network_ready()
    except Exception as e:
        return {"success": False, "reason": f"网络初始化失败: {e}", "fatal": True}

    if not acc.subnet_id or not acc.availability_domain:
        return {"success": False, "reason": "未找到公共子网 (Subnet)", "fatal": True}

    metadata = {
        "ssh_authorized_keys": DUMMY_SSH_KEY,
        "user_data": generate_user_data(root_pwd),
    }

    ts_suffix = str(int(time.time()))[-4:]
    if arch == "ARM":
        img_id = find_ubuntu_24_image(acc, "aarch64")
        details = oci.core.models.LaunchInstanceDetails(
            compartment_id=acc.compartment_id,
            availability_domain=acc.availability_domain,
            display_name=f"Free-ARM-{int(spec['ocpus'])}C{int(spec['memory'])}G-#{current_idx}-{ts_suffix}",
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=spec.get("ocpus", 1.0),
                memory_in_gbs=spec.get("memory", 6.0),
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=img_id,
                boot_volume_size_in_gbs=boot_gbs,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=acc.subnet_id,
                assign_public_ip=True,
            ),
            metadata=metadata,
        )
    else:
        img_id = find_ubuntu_24_image(acc, "x86_64")
        details = oci.core.models.LaunchInstanceDetails(
            compartment_id=acc.compartment_id,
            availability_domain=acc.availability_domain,
            display_name=f"Free-AMD-1C1G-#{current_idx}-{ts_suffix}",
            shape="VM.Standard.E2.1.Micro",
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=img_id,
                boot_volume_size_in_gbs=boot_gbs,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=acc.subnet_id,
                assign_public_ip=True,
            ),
            metadata=metadata,
        )

    try:
        resp = acc.compute_client.launch_instance(details)
        return {"success": True, "instance_id": resp.data.id, "name": resp.data.display_name}
    except oci.exceptions.ServiceError as e:
        if "Out of host capacity" in str(e.message) or e.status == 500:
            return {"success": False, "reason": "无容量 (Out of capacity)"}
        elif e.status == 429:
            return {"success": False, "reason": "API 限流 (429)"}
        return {"success": False, "reason": f"[{e.code}] {e.message}", "fatal": True}
    except Exception as e:
        return {"success": False, "reason": str(e)}


def change_instance_power(acc: OCIAccount, instance_id: str, action: str) -> str:
    res = acc.compute_client.instance_action(instance_id=instance_id, action=action)
    return res.data.lifecycle_state


def change_public_ip(acc: OCIAccount, instance_id: str) -> str:
    vnics = acc.compute_client.list_vnic_attachments(acc.compartment_id, instance_id=instance_id).data
    if not vnics:
        raise RuntimeError("该实例未关联任何网卡")
    vnic_id = vnics[0].vnic_id

    private_ips = acc.network_client.list_private_ips(vnic_id=vnic_id).data
    primary_ip = next((ip for ip in private_ips if ip.is_primary), None)
    if not primary_ip:
        raise RuntimeError("未找到主 Private IP")

    public_ips = acc.network_client.list_public_ips(scope="REGION", compartment_id=acc.compartment_id).data
    current_pip = next((p for p in public_ips if p.private_ip_id == primary_ip.id), None)
    old_ip = current_pip.ip_address if current_pip else "无"

    if current_pip:
        acc.network_client.delete_public_ip(current_pip.id)
        time.sleep(3)

    new_pip = acc.network_client.create_public_ip(
        oci.core.models.CreatePublicIpDetails(
            compartment_id=acc.compartment_id,
            lifetime="EPHEMERAL",
            private_ip_id=primary_ip.id,
            display_name="new-ip",
        )
    ).data
    return f"原 IP: `{old_ip}`\n新 IP: `{new_pip.ip_address}`"


def render_progress_bar(percentage: float, length: int = 10) -> str:
    filled_length = int(length * (percentage / 100))
    filled_length = max(0, min(length, filled_length))
    bar = "■" * filled_length + "□" * (length - filled_length)
    return f"`[{bar}]` *{percentage:.1f}%*"


def get_traffic_for_account(acc: OCIAccount, period: str = "month") -> str:
    now = datetime.now(timezone.utc)
    if period == "month":
        start_time = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        query = "VnicEgressBytes[1d].sum()"
        title = f"📅 *[{acc.name}] 本月账单概览 ({now.year}-{now.month:02d})*"
    elif period == "24h":
        start_time = now - timedelta(hours=24)
        query = "VnicEgressBytes[1h].sum()"
        title = f"🕒 *[{acc.name}] 近 24 小时出网*"
    elif period == "7d":
        start_time = now - timedelta(days=7)
        query = "VnicEgressBytes[1d].sum()"
        title = f"📈 *[{acc.name}] 近 7 天趋势*"
    else:
        start_time = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        query = "VnicEgressBytes[1d].sum()"
        title = f"📅 *[{acc.name}] 流量统计*"

    query_details = oci.monitoring.models.SummarizeMetricsDataDetails(
        namespace="oci_vcn",
        query=query,
        start_time=start_time,
        end_time=now,
    )
    metric_data = acc.monitoring_client.summarize_metrics_data(
        compartment_id=acc.compartment_id,
        summarize_metrics_data_details=query_details,
    ).data

    total_bytes = sum(dp.value for item in metric_data for dp in item.aggregated_datapoints)
    used_gb = total_bytes / (1024 ** 3)
    remaining_gb = max(0.0, MONTHLY_QUOTA_GB - used_gb)
    percentage = (used_gb / MONTHLY_QUOTA_GB) * 100 if MONTHLY_QUOTA_GB else 0.0

    lines = [
        title,
        f"🌍 *区域*: `{acc.config_dict['region']}`",
        "",
        f"📊 *消耗进度*: {render_progress_bar(percentage)}",
        f"📤 *已用出网*: `{used_gb:.2f} GB`",
        f"🎁 *免费额度*: `{MONTHLY_QUOTA_GB} GB` (10 TB)",
        f"🟢 *剩余可用*: `{remaining_gb:.2f} GB`",
    ]
    if period == "24h":
        mbps = (used_gb * 1024 * 8) / (24 * 3600)
        lines.append(f"⚡ *24H 平均速率*: `{mbps:.2f} Mbps`")

    lines.append(f"\n_刷新时间: {now.strftime('%H:%M:%S')} UTC_")
    return "\n".join(lines)


# ================= 4. 后台定时任务 =================
async def sniper_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    loop = asyncio.get_running_loop()

    acc_name = context.chat_data.get("current_account")
    acc = ACCOUNTS.get(acc_name)
    if not acc:
        job.schedule_removal()
        context.chat_data["is_sniping"] = False
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 账号 `{acc_name}` 已失效，抢机终止。")
        return

    spec = context.chat_data.get("current_spec", {"arch": "ARM", "ocpus": 1.0, "memory": 6.0, "boot_gbs": 50, "target_count": 1})
    attempts = context.chat_data.get("sniper_attempts", 0) + 1
    context.chat_data["sniper_attempts"] = attempts

    created_count = context.chat_data.get("created_count", 0)
    target_count = spec.get("target_count", 1)

    result = await loop.run_in_executor(None, launch_vm, acc, spec, created_count + 1)

    if result.get("success"):
        created_count += 1
        context.chat_data["created_count"] = created_count

        desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if spec["arch"] == "ARM" else "1C 1G"
        add_bot_log(f"🎉 [{acc.name}] 开机成功 ({created_count}/{target_count}): {result['name']}")

        msg = (
            f"🎉 *恭喜！账号 [{acc.name}] 开机成功！ ({created_count}/{target_count})*\n\n"
            f"🌍 区域: `{acc.config_dict['region']}`\n"
            f"🖥 实例名: `{result['name']}`\n"
            f"⚙️ 规格: `{spec['arch']} ({desc})`\n"
            f"💾 磁盘: `{spec['boot_gbs']} GB` | 系统: `Ubuntu 24.04`\n"
            f"👤 用户名: `root`\n"
            f"🔑 密码: `{spec.get('root_password', DEFAULT_ROOT_PASSWORD)}`\n"
            f"🆔 实例 OCID:\n`{result['instance_id']}`\n\n"
        )

        if created_count >= target_count:
            job.schedule_removal()
            context.chat_data["is_sniping"] = False
            msg += f"🏁 *已全部达成目标开机数量（共 {target_count} 台），任务顺利完成！*"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        else:
            msg += f"⏳ *继续后台抢下一台... 当前进度: ({created_count}/{target_count})*"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

    else:
        if result.get("fatal"):
            job.schedule_removal()
            context.chat_data["is_sniping"] = False
            add_bot_log(f"⚠️ [{acc.name}] 致命错误: {result.get('reason')}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ *账号 [{acc.name}] 遇到错误已终止*:\n`{result.get('reason')}`",
                parse_mode="Markdown",
            )
            return

        add_bot_log(f"[{acc.name}-{spec['arch']}] 第{attempts}次重试: {result.get('reason')}")

        if attempts % 500 == 0:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ *[{acc.name}] 持续抢机中 (第 {attempts} 次)*\n进度: `({created_count}/{target_count}台)` | 状态: `{result.get('reason')}`",
                parse_mode="Markdown",
            )


# ================= 5. TG 交互键盘构建 =================
def build_main_keyboard(is_sniping: bool, current_acc_name: str, spec: dict, interval: int):
    arch = spec["arch"]
    desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if arch == "ARM" else "1C 1G"
    target_count = spec.get("target_count", 1)

    snip_btn = (
        InlineKeyboardButton("🛑 停止抢机", callback_data="stop_sniper")
        if is_sniping
        else InlineKeyboardButton(f"🎯 开始抢机 (目标:{target_count}台)", callback_data="start_sniper")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👤 当前账号: 【{current_acc_name}】", callback_data="menu_accounts")],
        [snip_btn],
        [
            InlineKeyboardButton(f"⚙️ 规格 [{arch} {desc} x{target_count}]", callback_data="menu_spec"),
            InlineKeyboardButton(f"⏱ 间隔 ({interval}s)", callback_data="menu_interval"),
        ],
        [
            InlineKeyboardButton("⚡ 实例电源", callback_data="menu_instances"),
            InlineKeyboardButton("🔓 端口全开 (甲骨文后台规则)", callback_data="action_open_ports"),
        ],
        [
            InlineKeyboardButton("📜 查看实时日志", callback_data="view_logs"),
            InlineKeyboardButton("📶 流量交互中心", callback_data="traffic_view_month")
        ]
    ])


def build_accounts_keyboard(current_acc_name: str):
    buttons = []
    for acc_name, acc_obj in ACCOUNTS.items():
        is_curr = "✅ " if acc_name == current_acc_name else ""
        buttons.append([
            InlineKeyboardButton(
                f"{is_curr}{acc_name} ({acc_obj.config_dict['region']})",
                callback_data=f"switch_acc_{acc_name}",
            )
        ])
    buttons.append([InlineKeyboardButton("🔄 重新扫描账号文件夹", callback_data="reload_accounts")])
    buttons.append([InlineKeyboardButton("🔙 返回控制台", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def build_instances_keyboard(acc: OCIAccount):
    instances = acc.compute_client.list_instances(acc.compartment_id).data
    buttons = []
    for inst in instances:
        if inst.lifecycle_state != "TERMINATED":
            status_icon = "🟢" if inst.lifecycle_state == "RUNNING" else "🔴"
            buttons.append([
                InlineKeyboardButton(
                    f"{status_icon} {inst.display_name} ({inst.shape})",
                    callback_data=f"manage_inst_{inst.id}",
                )
            ])
    if not buttons:
        buttons.append([InlineKeyboardButton("（暂无运行中实例）", callback_data="none")])
    buttons.append([InlineKeyboardButton("🔙 返回控制台", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def build_instance_actions_keyboard(instance_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 开机", callback_data=f"act_START_{instance_id}"), InlineKeyboardButton("🔴 关机", callback_data=f"act_STOP_{instance_id}")],
        [InlineKeyboardButton("🔄 软重启", callback_data=f"act_SOFTRESET_{instance_id}"), InlineKeyboardButton("⚠️ 强制重启", callback_data=f"act_RESET_{instance_id}")],
        [InlineKeyboardButton("🔄 更换公网 IP", callback_data=f"act_CHANGEIP_{instance_id}")],
        [InlineKeyboardButton("🔙 返回实例列表", callback_data="menu_instances")],
    ])


def build_traffic_keyboard(current_period: str = "month"):
    btn_m = InlineKeyboardButton(f"{'🔘 ' if current_period == 'month' else ''}本月账单", callback_data="traffic_view_month")
    btn_24 = InlineKeyboardButton(f"{'🔘 ' if current_period == '24h' else ''}近 24 小时", callback_data="traffic_view_24h")
    btn_7d = InlineKeyboardButton(f"{'🔘 ' if current_period == '7d' else ''}近 7 天趋势", callback_data="traffic_view_7d")
    btn_refresh = InlineKeyboardButton("🔄 刷新数据", callback_data=f"traffic_view_{current_period}")
    btn_back = InlineKeyboardButton("🔙 返回控制台", callback_data="menu_main")
    return InlineKeyboardMarkup([[btn_m, btn_24, btn_7d], [btn_refresh], [btn_back]])


def build_spec_keyboard(spec: dict):
    target_count = spec.get("target_count", 1)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔘 ARM 1C 6G (免费)", callback_data="set_arm_1_6"), InlineKeyboardButton("🔘 ARM 2C 12G", callback_data="set_arm_2_12")],
        [InlineKeyboardButton("🔘 ARM 4C 24G (顶配)", callback_data="set_arm_4_24"), InlineKeyboardButton("🔘 AMD 1C 1G (微型)", callback_data="set_amd_1_1")],
        [InlineKeyboardButton("💾 硬盘: 50 GB", callback_data="set_disk_50"), InlineKeyboardButton("💾 硬盘: 100 GB", callback_data="set_disk_100")],
        [
            InlineKeyboardButton("🔢 目标开机数量:", callback_data="none"),
            InlineKeyboardButton(f"{'✅ ' if target_count==1 else ''}1台", callback_data="set_count_1"),
            InlineKeyboardButton(f"{'✅ ' if target_count==2 else ''}2台", callback_data="set_count_2"),
            InlineKeyboardButton(f"{'✅ ' if target_count==3 else ''}3台", callback_data="set_count_3"),
            InlineKeyboardButton(f"{'✅ ' if target_count==4 else ''}4台", callback_data="set_count_4"),
        ],
        [InlineKeyboardButton("🔙 返回控制台", callback_data="menu_main")],
    ])


def build_logs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新最新日志", callback_data="view_logs")],
        [InlineKeyboardButton("🔙 返回控制台", callback_data="menu_main")]
    ])


# ================= 6. 控制器与回调路由 =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ 无访问权限")
        return

    if not ACCOUNTS:
        reload_all_accounts()
        if not ACCOUNTS:
            await update.message.reply_text("❌ 未检测到可用账号凭据，请确认 accounts 目录挂载。")
            return

    current_acc = context.chat_data.get("current_account")
    if not current_acc or current_acc not in ACCOUNTS:
        current_acc = list(ACCOUNTS.keys())[0]
        context.chat_data["current_account"] = current_acc

    spec = context.chat_data.setdefault("current_spec", {"arch": "ARM", "ocpus": 1.0, "memory": 6.0, "boot_gbs": 50, "root_password": DEFAULT_ROOT_PASSWORD, "target_count": 1})
    interval = context.chat_data.setdefault("interval", DEFAULT_INTERVAL)
    is_sniping = context.chat_data.get("is_sniping", False)

    acc_obj = ACCOUNTS[current_acc]
    target_count = spec.get("target_count", 1)
    server_ip = get_server_ip()

    await update.message.reply_text(
        f"🖥 *运行节点 IP*: `{server_ip}`\n"
        f"🎮 *甲骨文多账号运维控制台*\n\n"
        f"• 当前选中账号: *{current_acc}*\n"
        f"• 所属区域: `{acc_obj.config_dict['region']}`\n"
        f"• 目标机型: `{spec['arch']} ({int(spec['ocpus'])}C {int(spec['memory'])}G | {spec['boot_gbs']}G)`\n"
        f"• 🎯 目标开机台数: `{target_count} 台`\n"
        f"• 抢机状态: `{'正在运行' if is_sniping else '待机'}`\n\n"
        "💡 点击「📜 查看实时日志」可在 TG 实时监控抢机状态与报错。",
        parse_mode="Markdown",
        reply_markup=build_main_keyboard(is_sniping, current_acc, spec, interval),
    )


async def set_pwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if not context.args:
        spec = context.chat_data.setdefault("current_spec", {})
        curr_pwd = spec.get("root_password", DEFAULT_ROOT_PASSWORD)
        await update.message.reply_text(f"💡 当前开机密码为: `{curr_pwd}`\n修改请使用: `/set_pwd 新密码`", parse_mode="Markdown")
        return

    new_pwd = context.args[0]
    spec = context.chat_data.setdefault("current_spec", {})
    spec["root_password"] = new_pwd
    await update.message.reply_text(f"✅ 开机 root 密码已更新为: `{new_pwd}`", parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_USER_ID:
        return

    chat_id = update.effective_chat.id
    data = query.data
    current_acc_name = context.chat_data.get("current_account", list(ACCOUNTS.keys())[0] if ACCOUNTS else "")
    acc = ACCOUNTS.get(current_acc_name)
    spec = context.chat_data.setdefault("current_spec", {"arch": "ARM", "ocpus": 1.0, "memory": 6.0, "boot_gbs": 50, "root_password": DEFAULT_ROOT_PASSWORD, "target_count": 1})
    interval = context.chat_data.setdefault("interval", DEFAULT_INTERVAL)
    is_sniping = context.chat_data.get("is_sniping", False)
    loop = asyncio.get_running_loop()

    if data == "menu_main":
        server_ip = get_server_ip()
        await query.edit_message_text(
            f"🖥 *运行节点 IP*: `{server_ip}`\n"
            "🎮 *甲骨文多账号运维控制台*",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval),
        )

    elif data == "view_logs":
        recent_logs = list(LOG_HISTORY)[-15:]
        log_text = "\n".join(recent_logs) if recent_logs else "（暂无日志记录）"
        now_str = datetime.now().strftime("%H:%M:%S")
        text_lines = [
            f"📜 *系统实时日志 (最近 15 条 - {now_str})*",
            "",
            "```text",
            log_text,
            "```",
        ]
        await query.edit_message_text(
            text="\n".join(text_lines),
            parse_mode="Markdown",
            reply_markup=build_logs_keyboard(),
        )

    elif data == "action_open_ports":
        if not acc:
            await query.edit_message_text("❌ 当前账号凭据失效")
            return
        await query.edit_message_text("⏳ 正在为当前账号后台所有安全列表 (Security Lists) 与安全组 (NSG) 放开入站规则 (0.0.0.0/0)...")
        try:
            result_str = await loop.run_in_executor(None, acc.open_all_security_ports)
            add_bot_log(f"[{acc.name}] 甲骨文后台规则端口全开成功")
            await query.edit_message_text(
                f"✅ *甲骨文后台防火墙放行成功！*\n\n{result_str}\n\n💡 *说明*：OCI 云后台安全列表及安全组均已放行全部流量，同时系统内防火墙在开机时也会清空放通。",
                parse_mode="Markdown",
                reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ 放行端口失败: `{e}`", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))

    elif data == "menu_accounts":
        await query.edit_message_text(
            f"👤 *选择要操作的甲骨文账号*\n\n当前: `{current_acc_name}`",
            parse_mode="Markdown",
            reply_markup=build_accounts_keyboard(current_acc_name),
        )

    elif data.startswith("switch_acc_"):
        new_acc = data.replace("switch_acc_", "")
        context.chat_data["current_account"] = new_acc
        new_obj = ACCOUNTS[new_acc]
        await query.edit_message_text(
            f"✅ 已切换激活账号: *{new_acc}* (`{new_obj.config_dict['region']}`)",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(is_sniping, new_acc, spec, interval),
        )

    elif data == "reload_accounts":
        reload_all_accounts()
        await query.edit_message_text(
            f"🔄 刷新完毕！共发现 `{len(ACCOUNTS)}` 个账号。",
            reply_markup=build_accounts_keyboard(current_acc_name),
        )

    elif data == "menu_instances":
        if not acc:
            await query.edit_message_text("❌ 当前账号凭据失效")
            return
        await query.edit_message_text(f"🖥 *[{acc.name}] 实例列表*", parse_mode="Markdown", reply_markup=build_instances_keyboard(acc))

    elif data.startswith("manage_inst_"):
        inst_id = data.replace("manage_inst_", "")
        await query.edit_message_text(f"⚙️ 目标机器:\n`{inst_id}`", parse_mode="Markdown", reply_markup=build_instance_actions_keyboard(inst_id))

    elif data.startswith("act_"):
        parts = data.split("_")
        action = parts[1]
        inst_id = parts[2]

        if action == "CHANGEIP":
            await query.edit_message_text("⏳ 正在更换 IP...")
            try:
                res_str = await loop.run_in_executor(None, change_public_ip, acc, inst_id)
                add_bot_log(f"[{acc.name}] 换 IP 成功: {res_str}")
                await query.edit_message_text(f"✅ *更换 IP 成功！*\n\n{res_str}", parse_mode="Markdown", reply_markup=build_instance_actions_keyboard(inst_id))
            except Exception as e:
                await query.edit_message_text(f"❌ 更换失败: `{e}`", reply_markup=build_instance_actions_keyboard(inst_id))
        else:
            await query.edit_message_text(f"⏳ 正在下发电源动作 `{action}` ...")
            try:
                state = await loop.run_in_executor(None, change_instance_power, acc, inst_id, action)
                add_bot_log(f"[{acc.name}] 电源动作 {action}: 状态变为 {state}")
                await query.edit_message_text(f"✅ 操作完成！当前状态: `{state}`", reply_markup=build_instance_actions_keyboard(inst_id))
            except Exception as e:
                await query.edit_message_text(f"❌ 失败: `{e}`", reply_markup=build_instance_actions_keyboard(inst_id))

    elif data.startswith("traffic_view_"):
        if not acc:
            return
        period = data.replace("traffic_view_", "")
        await query.edit_message_text(f"⏳ 正在统计 [{acc.name}] 的 `{period}` 流量数据...")
        try:
            report = await loop.run_in_executor(None, get_traffic_for_account, acc, period)
            await query.edit_message_text(report, parse_mode="Markdown", reply_markup=build_traffic_keyboard(period))
        except Exception as e:
            await query.edit_message_text(f"❌ 流量查询失败: `{e}`", reply_markup=build_traffic_keyboard(period))

    elif data == "menu_spec":
        arch_desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if spec["arch"] == "ARM" else "1C 1G"
        target_count = spec.get("target_count", 1)
        await query.edit_message_text(
            f"⚙️ *规格与开机数量调整*\n\n"
            f"• 当前选定: *{spec['arch']}* (`{arch_desc}` | `{spec['boot_gbs']} GB`)\n"
            f"• 🎯 目标开机台数: *{target_count} 台*\n"
            f"• 系统默认: `Ubuntu 24.04 LTS`",
            parse_mode="Markdown",
            reply_markup=build_spec_keyboard(spec),
        )

    elif data == "set_arm_1_6":
        spec.update({"arch": "ARM", "ocpus": 1.0, "memory": 6.0})
        await query.edit_message_text("✅ 已切换: *ARM 1C 6G*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data == "set_arm_2_12":
        spec.update({"arch": "ARM", "ocpus": 2.0, "memory": 12.0})
        await query.edit_message_text("✅ 已切换: *ARM 2C 12G*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data == "set_arm_4_24":
        spec.update({"arch": "ARM", "ocpus": 4.0, "memory": 24.0})
        await query.edit_message_text("✅ 已切换: *ARM 4C 24G*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data == "set_amd_1_1":
        spec.update({"arch": "AMD", "ocpus": 1.0, "memory": 1.0})
        await query.edit_message_text("✅ 已切换: *AMD 1C 1G*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data == "set_disk_50":
        spec["boot_gbs"] = 50
        await query.edit_message_text("✅ 引导卷已设为: *50 GB*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data == "set_disk_100":
        spec["boot_gbs"] = 100
        await query.edit_message_text("✅ 引导卷已设为: *100 GB*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data.startswith("set_count_"):
        new_cnt = int(data.split("_")[2])
        spec["target_count"] = new_cnt
        await query.edit_message_text(f"✅ 目标开机数量已设为: *{new_cnt} 台*", parse_mode="Markdown", reply_markup=build_spec_keyboard(spec))

    elif data == "start_sniper":
        if is_sniping:
            return
        context.chat_data["is_sniping"] = True
        context.chat_data["sniper_attempts"] = 0
        context.chat_data["created_count"] = 0
        target_count = spec.get("target_count", 1)

        context.job_queue.run_repeating(
            sniper_job, interval=interval, first=1, chat_id=chat_id, name=f"sniper_{chat_id}"
        )
        desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if spec["arch"] == "ARM" else "1C 1G"
        add_bot_log(f"[*] 账号 [{current_acc_name}] 启动抢机 (目标: {target_count} 台)")

        server_ip = get_server_ip()
        await query.edit_message_text(
            f"🖥 *运行节点 IP*: `{server_ip}`\n"
            f"🚀 *账号 [{current_acc_name}] 抢机任务已启动！*\n"
            f"• 规格: `{spec['arch']} ({desc})`\n"
            f"• 🎯 目标开机数量: `{target_count} 台`\n"
            f"• 轮询间隔: `{interval}s`\n"
            f"• 开机密码: `{spec.get('root_password', DEFAULT_ROOT_PASSWORD)}`",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(True, current_acc_name, spec, interval),
        )

    elif data == "stop_sniper":
        jobs = context.job_queue.get_jobs_by_name(f"sniper_{chat_id}")
        for j in jobs:
            j.schedule_removal()
        context.chat_data["is_sniping"] = False
        attempts = context.chat_data.get("sniper_attempts", 0)
        created_count = context.chat_data.get("created_count", 0)
        add_bot_log(f"[*] 手动停止抢机，尝试 {attempts} 次，已开出 {created_count} 台")

        await query.edit_message_text(
            f"⏹ *抢机任务已停止*\n累计尝试 `{attempts}` 次，已成功开出 `{created_count}` 台。",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(False, current_acc_name, spec, interval),
        )

    elif data == "menu_interval":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("15s", callback_data="int_15"), InlineKeyboardButton("30s", callback_data="int_30"), InlineKeyboardButton("60s", callback_data="int_60")],
            [InlineKeyboardButton("🔙 返回", callback_data="menu_main")],
        ])
        await query.edit_message_text("⏱ 选择抢机轮询间隔：", reply_markup=keyboard)

    elif data.startswith("int_"):
        new_int = int(data.split("_")[1])
        context.chat_data["interval"] = new_int
        if is_sniping:
            jobs = context.job_queue.get_jobs_by_name(f"sniper_{chat_id}")
            for j in jobs:
                j.schedule_removal()
            context.job_queue.run_repeating(sniper_job, interval=new_int, first=new_int, chat_id=chat_id, name=f"sniper_{chat_id}")
        await query.edit_message_text(f"✅ 轮询间隔已设为 `{new_int}` 秒！", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, new_int))


def main():
    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_pwd", set_pwd_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    add_bot_log(f"[*] OCI 多账号运维 Bot 已启动，已载入 {len(ACCOUNTS)} 个账号。")
    app.run_polling()


if __name__ == "__main__":
    main()
