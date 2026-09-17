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

LOG_HISTORY = deque(maxlen=50)

def add_bot_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    LOG_HISTORY.append(f"[{ts}] {msg}")
    logger.info(msg)

# ================= 0. Telegram 64 字节限制：OCID 短标识符映射系统 =================
ID_MAP: dict[str, str] = {}
REV_ID_MAP: dict[str, str] = {}
ID_COUNTER: int = 0

def get_short_id(ocid: str) -> str:
    global ID_COUNTER
    if ocid in ID_MAP:
        return ID_MAP[ocid]
    short_id = f"i{ID_COUNTER}"
    ID_COUNTER += 1
    ID_MAP[ocid] = short_id
    REV_ID_MAP[short_id] = ocid
    return short_id

def get_full_ocid(short_id: str) -> str:
    return REV_ID_MAP.get(short_id, "")

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


# ================= 2. 多账号动态解析与独立代理注入 =================
class OCIAccount:
    def __init__(self, name: str, folder_path: str):
        self.name = name
        self.folder_path = folder_path
        self.proxy_url = self._parse_proxy()
        self.config_dict = self._parse_credentials()

        client_kwargs = {}
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url

        self.compute_client = oci.core.ComputeClient(self.config_dict, **client_kwargs)
        self.blockstorage_client = oci.core.BlockstorageClient(self.config_dict, **client_kwargs)
        self.network_client = oci.core.VirtualNetworkClient(self.config_dict, **client_kwargs)
        self.monitoring_client = oci.monitoring.MonitoringClient(self.config_dict, **client_kwargs)
        self.identity_client = oci.identity.IdentityClient(self.config_dict, **client_kwargs)

        self.compartment_id = self.config_dict["tenancy"]
        self.availability_domain = None
        self.subnet_id = None
        self.vcn_id = None

        self._cache_instances = []
        self._cache_instances_ts = 0.0
        self._cache_boot_volumes = []
        self._cache_boot_volumes_ts = 0.0

    def _parse_proxy(self) -> str:
        proxy_file = os.path.join(self.folder_path, "proxy.txt")
        if os.path.isfile(proxy_file):
            try:
                with open(proxy_file, "r", encoding="utf-8") as f:
                    proxy = f.read().strip()
                    if proxy:
                        return proxy
            except Exception as e:
                logger.warning(f"读取 [{self.name}] proxy.txt 异常: {e}")
        return ""

    def test_proxy_connection(self) -> dict:
        """测试该账号实际走出的出口 IP（按需调用，不消耗 OCI API）"""
        target_api = "https://api.ipify.org"
        t0 = time.time()
        try:
            if self.proxy_url:
                proxy_handler = urllib.request.ProxyHandler({
                    "http": self.proxy_url,
                    "https": self.proxy_url,
                })
                opener = urllib.request.build_opener(proxy_handler)
            else:
                opener = urllib.request.build_opener()

            req = urllib.request.Request(target_api, headers={"User-Agent": "curl/7.68.0"})
            with opener.open(req, timeout=6.0) as resp:
                exit_ip = resp.read().decode("utf-8").strip()
                latency = int((time.time() - t0) * 1000)
                return {
                    "success": True,
                    "exit_ip": exit_ip,
                    "latency": latency,
                    "is_proxy": bool(self.proxy_url),
                }
        except Exception as e:
            return {"success": False, "error": str(e), "is_proxy": bool(self.proxy_url)}

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
            if os.path.isfile(f) and not f.endswith("proxy.txt"):
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

    def invalidate_cache(self):
        self._cache_instances_ts = 0.0
        self._cache_boot_volumes_ts = 0.0

    def ensure_network_ready(self):
        if self.availability_domain and self.subnet_id:
            return

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
        self.ensure_network_ready()
        vcns = self.network_client.list_vcns(self.compartment_id).data
        if not vcns:
            raise RuntimeError("未在当前区间检测到任何可用 VCN")

        modified_lists = 0
        modified_nsgs = 0

        for vcn in vcns:
            sec_lists = self.network_client.list_security_lists(self.compartment_id, vcn_id=vcn.id).data
            for sec_list in sec_lists:
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

        return f"已成功更新 `{len(vcns)}` 个 VCN：\n• 安全列表: 共放行 `{modified_lists}` 个\n• 网络安全组: 共放行 `{modified_nsgs}` 个\n全部入站协议与端口 (`0.0.0.0/0:all`) 现已全通！"


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
                proxy_tip = f" (代理: `{acc.proxy_url.split('@')[-1]}`)" if acc.proxy_url else " (直连)"
                add_bot_log(f"[+] 载入账号: [{item}] ({acc.config_dict['region']}){proxy_tip}")
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


IMAGE_CACHE = {}

def find_ubuntu_24_image(acc: OCIAccount, arch: str) -> str:
    cache_key = f"{acc.name}_{arch}"
    if cache_key in IMAGE_CACHE:
        return IMAGE_CACHE[cache_key]

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
            IMAGE_CACHE[cache_key] = images[0].id
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
                IMAGE_CACHE[cache_key] = img.id
                return img.id
            elif arch == "x86_64" and ("aarch64" not in name and "arm" not in name):
                IMAGE_CACHE[cache_key] = img.id
                return img.id
    raise RuntimeError(f"未在区域 {acc.config_dict['region']} 找到 Ubuntu 24.04 镜像")


IP_CACHE: dict[str, tuple[str, str, float]] = {}

def get_instance_ips(acc: OCIAccount, instance_id: str) -> tuple[str, str]:
    now = time.time()
    if instance_id in IP_CACHE:
        pub, priv, ts = IP_CACHE[instance_id]
        if now - ts < 60:
            return pub, priv

    try:
        vnics = acc.compute_client.list_vnic_attachments(acc.compartment_id, instance_id=instance_id).data
        for va in vnics:
            if va.lifecycle_state == "ATTACHED":
                vnic = acc.network_client.get_vnic(va.vnic_id).data
                pub_ip = vnic.public_ip or "无公网IP"
                priv_ip = vnic.private_ip or "无内网IP"
                IP_CACHE[instance_id] = (pub_ip, priv_ip, now)
                return pub_ip, priv_ip
    except Exception:
        pass
    return "未知", "未知"


def wait_for_instance_public_ip(acc: OCIAccount, instance_id: str, max_retries: int = 6) -> str:
    for _ in range(max_retries):
        if instance_id in IP_CACHE:
            del IP_CACHE[instance_id]
        pub_ip, _ = get_instance_ips(acc, instance_id)
        if pub_ip not in ["未知", "无公网IP", ""]:
            return pub_ip
        time.sleep(3)
    return "分配中 (稍后在列表查看)"


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
        inst_id = resp.data.id
        inst_name = resp.data.display_name
        acc.invalidate_cache()
        public_ip = wait_for_instance_public_ip(acc, inst_id)

        return {
            "success": True, 
            "instance_id": inst_id, 
            "name": inst_name,
            "public_ip": public_ip
        }
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
    acc.invalidate_cache()
    return res.data.lifecycle_state


def terminate_instance_and_boot_volume(acc: OCIAccount, instance_id: str) -> None:
    acc.compute_client.terminate_instance(
        instance_id=instance_id,
        preserve_boot_volume=False
    )
    acc.invalidate_cache()
    if instance_id in IP_CACHE:
        del IP_CACHE[instance_id]


def delete_single_boot_volume(acc: OCIAccount, boot_volume_id: str) -> None:
    acc.blockstorage_client.delete_boot_volume(boot_volume_id=boot_volume_id)
    acc.invalidate_cache()


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

    if instance_id in IP_CACHE:
        del IP_CACHE[instance_id]
    acc.invalidate_cache()
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

    job_data = getattr(job, "data", {}) or {}
    acc_name = job_data.get("account")
    spec = job_data.get("spec", {})

    acc = ACCOUNTS.get(acc_name)
    if not acc:
        job.schedule_removal()
        acc_state = context.chat_data.setdefault("accounts_state", {}).setdefault(acc_name, {})
        acc_state["is_sniping"] = False
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 账号 `{acc_name}` 无效或不存在，抢机终止。")
        return

    acc_state = context.chat_data.setdefault("accounts_state", {}).setdefault(acc_name, {})
    attempts = acc_state.get("attempts", 0) + 1
    acc_state["attempts"] = attempts

    created_count = acc_state.get("created_count", 0)
    target_count = spec.get("target_count", 1)

    result = await loop.run_in_executor(None, launch_vm, acc, spec, created_count + 1)

    if result.get("success"):
        created_count += 1
        acc_state["created_count"] = created_count

        desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if spec["arch"] == "ARM" else "1C 1G"
        add_bot_log(f"🎉 [{acc.name}] 开机成功 ({created_count}/{target_count}): {result['name']} IP: {result.get('public_ip')}")

        msg = (
            f"🎉 *恭喜！账号 [{acc.name}] 开机成功！ ({created_count}/{target_count})*\n\n"
            f"🌍 区域: `{acc.config_dict['region']}`\n"
            f"🖥 实例名: `{result['name']}`\n"
            f"🌐 公网 IP: `{result.get('public_ip')}`\n"
            f"⚙️ 规格: `{spec['arch']} ({desc})`\n"
            f"💾 引导卷: `{spec['boot_gbs']} GB` | 系统: `Ubuntu 24.04`\n"
            f"👤 用户名: `root`\n"
            f"🔑 密码: `{spec.get('root_password', DEFAULT_ROOT_PASSWORD)}`\n\n"
        )

        if created_count >= target_count:
            job.schedule_removal()
            acc_state["is_sniping"] = False
            msg += f"🏁 *账号 [{acc.name}] 已全部达成目标开机数量（共 {target_count} 台），任务完成！*"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        else:
            msg += f"⏳ *[{acc.name}] 继续后台抢下一台... 当前进度: ({created_count}/{target_count})*"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

    else:
        if result.get("fatal"):
            job.schedule_removal()
            acc_state["is_sniping"] = False
            add_bot_log(f"⚠️ [{acc.name}] 致命错误: {result.get('reason')}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ *账号 [{acc.name}] 遇到错误已终止*:\n`{result.get('reason')}`",
                parse_mode="Markdown",
            )
            return

        add_bot_log(f"[{acc.name}-{spec['arch']}] 第{attempts}次重试: {result.get('reason')}")

        if attempts % 300 == 0:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ *[{acc.name}] 持续抢机中 (第 {attempts} 次)*\n进度: `({created_count}/{target_count}台)` | 状态: `{result.get('reason')}`",
                parse_mode="Markdown",
            )


# ================= 5. TG 交互键盘构建 =================
def get_account_state(chat_data: dict, acc_name: str) -> dict:
    return chat_data.setdefault("accounts_state", {}).setdefault(acc_name, {
        "is_sniping": False,
        "attempts": 0,
        "created_count": 0,
        "interval": DEFAULT_INTERVAL,
        "spec": {
            "arch": "ARM",
            "ocpus": 1.0,
            "memory": 6.0,
            "boot_gbs": 50,
            "root_password": DEFAULT_ROOT_PASSWORD,
            "target_count": 1
        }
    })

def build_main_keyboard(is_sniping: bool, current_acc_name: str, spec: dict, interval: int):
    arch = spec["arch"]
    desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if arch == "ARM" else "1C 1G"
    target_count = spec.get("target_count", 1)

    snip_btn = (
        InlineKeyboardButton("🛑 停止抢机", callback_data="ask_stop_sniper")
        if is_sniping
        else InlineKeyboardButton(f"🎯 开始抢机 (目标:{target_count}台)", callback_data="ask_start_sniper")
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
            InlineKeyboardButton("🔄 更换公网 IP", callback_data="menu_change_ip_direct"),
        ],
        [
            InlineKeyboardButton("💾 引导卷管理", callback_data="menu_boot_vols"),
            InlineKeyboardButton("🌐 测试出口 IP", callback_data="action_test_proxy"),
        ],
        [
            InlineKeyboardButton("🔓 端口全开 (甲骨文规则)", callback_data="action_open_ports"),
            InlineKeyboardButton("📜 实时日志", callback_data="view_logs"),
        ],
        [
            InlineKeyboardButton("📶 流量交互中心", callback_data="traffic_view_month")
        ]
    ])


def build_accounts_keyboard(current_acc_name: str, accounts_state: dict):
    buttons = []
    for acc_name, acc_obj in ACCOUNTS.items():
        is_curr = "👉 " if acc_name == current_acc_name else ""
        is_running = " [🚀抢机中]" if accounts_state.get(acc_name, {}).get("is_sniping") else ""
        proxy_flag = " 🌐" if acc_obj.proxy_url else ""
        buttons.append([
            InlineKeyboardButton(
                f"{is_curr}{acc_name} ({acc_obj.config_dict['region']}){proxy_flag}{is_running}",
                callback_data=f"switch_acc_{acc_name}",
            )
        ])
    buttons.append([InlineKeyboardButton("🔄 重新扫描账号文件夹", callback_data="reload_accounts")])
    buttons.append([InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def build_instances_keyboard(acc: OCIAccount):
    now = time.time()
    if acc._cache_instances and (now - acc._cache_instances_ts < 60):
        instances = acc._cache_instances
    else:
        instances = acc.compute_client.list_instances(acc.compartment_id).data
        acc._cache_instances = instances
        acc._cache_instances_ts = now

    buttons = []
    for inst in instances:
        if inst.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
            status_icon = "🟢" if inst.lifecycle_state == "RUNNING" else "🔴"
            short_id = get_short_id(inst.id)
            pub_ip, _ = get_instance_ips(acc, inst.id)
            ip_str = f"[{pub_ip}]" if pub_ip not in ["未知", "无公网IP"] else "[无公网IP]"
            buttons.append([
                InlineKeyboardButton(
                    f"{status_icon} {inst.display_name} | {ip_str}",
                    callback_data=f"mg_{short_id}",
                )
            ])
    if not buttons:
        buttons.append([InlineKeyboardButton("（暂无运行中实例）", callback_data="none")])
    buttons.append([InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def build_change_ip_keyboard(acc: OCIAccount):
    now = time.time()
    if acc._cache_instances and (now - acc._cache_instances_ts < 60):
        instances = acc._cache_instances
    else:
        instances = acc.compute_client.list_instances(acc.compartment_id).data
        acc._cache_instances = instances
        acc._cache_instances_ts = now

    buttons = []
    for inst in instances:
        if inst.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
            short_id = get_short_id(inst.id)
            pub_ip, _ = get_instance_ips(acc, inst.id)
            buttons.append([
                InlineKeyboardButton(
                    f"🔄 更换: {inst.display_name} ({pub_ip})",
                    callback_data=f"ask_changeip_{short_id}",
                )
            ])
    if not buttons:
        buttons.append([InlineKeyboardButton("（暂无可用实例）", callback_data="none")])
    buttons.append([InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def build_boot_volumes_keyboard(acc: OCIAccount):
    now = time.time()
    if acc._cache_boot_volumes and (now - acc._cache_boot_volumes_ts < 60):
        boot_vols = acc._cache_boot_volumes
    else:
        ads = acc.identity_client.list_availability_domains(acc.compartment_id).data
        boot_vols = []
        for ad in ads:
            vols = acc.blockstorage_client.list_boot_volumes(
                availability_domain=ad.name,
                compartment_id=acc.compartment_id
            ).data
            boot_vols.extend(vols)
        acc._cache_boot_volumes = boot_vols
        acc._cache_boot_volumes_ts = now

    buttons = []
    for bv in boot_vols:
        if bv.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
            short_id = get_short_id(bv.id)
            size_gb = bv.size_in_gbs
            buttons.append([
                InlineKeyboardButton(
                    f"💾 {bv.display_name} ({size_gb}G) [{bv.lifecycle_state}]",
                    callback_data=f"bv_{short_id}",
                )
            ])
    if not buttons:
        buttons.append([InlineKeyboardButton("（暂无引导卷）", callback_data="none")])
    buttons.append([InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")])
    return InlineKeyboardMarkup(buttons)


def build_instance_actions_keyboard(short_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 开机", callback_data=f"act_START_{short_id}"), InlineKeyboardButton("🔴 关机", callback_data=f"act_STOP_{short_id}")],
        [InlineKeyboardButton("🔄 软重启", callback_data=f"act_SOFTRESET_{short_id}"), InlineKeyboardButton("⚠️ 强制重启", callback_data=f"act_RESET_{short_id}")],
        [InlineKeyboardButton("🔄 更换公网 IP", callback_data=f"ask_changeip_{short_id}")],
        [InlineKeyboardButton("💣 终止实例 (含引导卷)", callback_data=f"ask_term_{short_id}")],
        [
            InlineKeyboardButton("🔙 返回实例列表", callback_data="menu_instances"),
            InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")
        ],
    ])


def build_traffic_keyboard(current_period: str = "month"):
    btn_m = InlineKeyboardButton(f"{'🔘 ' if current_period == 'month' else ''}本月账单", callback_data="traffic_view_month")
    btn_24 = InlineKeyboardButton(f"{'🔘 ' if current_period == '24h' else ''}近 24 小时", callback_data="traffic_view_24h")
    btn_7d = InlineKeyboardButton(f"{'🔘 ' if current_period == '7d' else ''}近 7 天趋势", callback_data="traffic_view_7d")
    btn_refresh = InlineKeyboardButton("🔄 刷新数据", callback_data=f"traffic_view_{current_period}")
    btn_back = InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")
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
        [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")],
    ])


def build_logs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新最新日志", callback_data="view_logs")],
        [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
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

    acc_state = get_account_state(context.chat_data, current_acc)
    spec = acc_state["spec"]
    interval = acc_state["interval"]
    is_sniping = acc_state["is_sniping"]

    acc_obj = ACCOUNTS[current_acc]
    target_count = spec.get("target_count", 1)
    server_ip = get_server_ip()
    proxy_desc = f"`{acc_obj.proxy_url.split('@')[-1]}`" if acc_obj.proxy_url else "`直连 (无代理)`"

    await update.message.reply_text(
        f"🖥 *运行节点 IP*: `{server_ip}`\n"
        f"🎮 *甲骨文多账号运维控制台*\n\n"
        f"• 当前选中账号: *{current_acc}*\n"
        f"• 所属区域: `{acc_obj.config_dict['region']}`\n"
        f"• 网络出口: {proxy_desc}\n"
        f"• 目标机型: `{spec['arch']} ({int(spec['ocpus'])}C {int(spec['memory'])}G | {spec['boot_gbs']}G)`\n"
        f"• 🎯 目标开机台数: `{target_count} 台`\n"
        f"• 抢机状态: `{'🚀 正在运行' if is_sniping else '💤 待机'}`\n\n"
        "💡 点击「🌐 测试出口 IP」可实时校验当前账号使用的代理节点。",
        parse_mode="Markdown",
        reply_markup=build_main_keyboard(is_sniping, current_acc, spec, interval),
    )


async def set_pwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    current_acc = context.chat_data.get("current_account", list(ACCOUNTS.keys())[0] if ACCOUNTS else "")
    acc_state = get_account_state(context.chat_data, current_acc)
    spec = acc_state["spec"]

    if not context.args:
        curr_pwd = spec.get("root_password", DEFAULT_ROOT_PASSWORD)
        await update.message.reply_text(f"💡 [{current_acc}] 当前开机密码为: `{curr_pwd}`\n修改请使用: `/set_pwd 新密码`", parse_mode="Markdown")
        return

    new_pwd = context.args[0]
    spec["root_password"] = new_pwd
    await update.message.reply_text(f"✅ 账号 [{current_acc}] 开机密码已更新为: `{new_pwd}`", parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_USER_ID:
        return

    chat_id = update.effective_chat.id
    data = query.data

    current_acc_name = context.chat_data.get("current_account")
    if not current_acc_name or current_acc_name not in ACCOUNTS:
        current_acc_name = list(ACCOUNTS.keys())[0] if ACCOUNTS else ""
        context.chat_data["current_account"] = current_acc_name

    acc = ACCOUNTS.get(current_acc_name)
    acc_state = get_account_state(context.chat_data, current_acc_name)
    spec = acc_state["spec"]
    interval = acc_state["interval"]
    is_sniping = acc_state["is_sniping"]
    loop = asyncio.get_running_loop()

    if data == "menu_main":
        server_ip = get_server_ip()
        proxy_desc = f"`{acc.proxy_url.split('@')[-1]}`" if (acc and acc.proxy_url) else "`直连`"
        await query.edit_message_text(
            f"🖥 *运行节点 IP*: `{server_ip}`\n"
            f"🎮 *甲骨文多账号运维控制台*\n\n"
            f"• 当前选中账号: *{current_acc_name}*\n"
            f"• 出口模式: {proxy_desc}\n"
            f"• 当前状态: `{'🚀 正在抢机' if is_sniping else '💤 待机'}`",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval),
        )

    # ================= 原生测试账号代理连通性 =================
    elif data == "action_test_proxy":
        if not acc:
            await query.edit_message_text("❌ 当前账号凭据失效")
            return

        server_ip = get_server_ip()
        await query.edit_message_text(f"⏳ 正在通过账号 [{current_acc_name}] 设定的网络通道测试外网出口 IP...")

        test_res = await loop.run_in_executor(None, acc.test_proxy_connection)

        if test_res.get("success"):
            exit_ip = test_res.get("exit_ip")
            latency = test_res.get("latency")
            is_proxy = test_res.get("is_proxy")

            if is_proxy:
                # 拿到了非本机 IP 并且代理开启
                status_icon = "🟢" if exit_ip != server_ip else "🟡"
                proxy_clean = acc.proxy_url.split("@")[-1]
                msg_body = (
                    f"{status_icon} *代理通道测试成功！*\n\n"
                    f"• 操作账号: *{current_acc_name}*\n"
                    f"• 设定代理: `{proxy_clean}`\n"
                    f"• 实际出口 IP: `{exit_ip}`\n"
                    f"• 响应耗时: `{latency} ms`\n\n"
                    + ("🎉 **确认生效**：出口 IP 与宿主 VPS IP 完全不同，OCI API 请求已被成功代理接管！" if exit_ip != server_ip else "⚠️ **提示**：出口 IP 仍为本机 IP，请检查代理服务端路由规则。")
                )
            else:
                msg_body = (
                    f"ℹ️ *该账号未配置 proxy.txt*\n\n"
                    f"• 操作账号: *{current_acc_name}*\n"
                    f"• 网络出口: `直连模式 (宿主 VPS 出口)`\n"
                    f"• 真实公网 IP: `{exit_ip}`\n"
                    f"• 响应耗时: `{latency} ms`"
                )
        else:
            msg_body = (
                f"❌ *代理连通性测试失败！*\n\n"
                f"• 操作账号: *{current_acc_name}*\n"
                f"• 设定代理: `{acc.proxy_url}`\n"
                f"• 报错详情: `{test_res.get('error')}`\n\n"
                "💡 请确认 proxy.txt 内的协议、地址、端口与账密是否有效。"
            )

        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 重新测试", callback_data="action_test_proxy")],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
        ])
        await query.edit_message_text(msg_body, parse_mode="Markdown", reply_markup=confirm_kb)

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
        await query.edit_message_text("⏳ 正在为当前账号后台所有安全列表与安全组放开 0.0.0.0/0 ...")
        try:
            result_str = await loop.run_in_executor(None, acc.open_all_security_ports)
            add_bot_log(f"[{acc.name}] 甲骨文后台规则端口全开成功")
            await query.edit_message_text(
                f"✅ *甲骨文后台防火墙放行成功！*\n\n{result_str}",
                parse_mode="Markdown",
                reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ 放行端口失败: `{e}`", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))

    elif data == "menu_accounts":
        accounts_state = context.chat_data.setdefault("accounts_state", {})
        await query.edit_message_text(
            f"👤 *选择要操作的甲骨文账号*\n\n当前操作面板为: `{current_acc_name}`",
            parse_mode="Markdown",
            reply_markup=build_accounts_keyboard(current_acc_name, accounts_state),
        )

    elif data.startswith("switch_acc_"):
        new_acc = data.replace("switch_acc_", "")
        context.chat_data["current_account"] = new_acc
        new_state = get_account_state(context.chat_data, new_acc)
        new_obj = ACCOUNTS[new_acc]
        proxy_desc = f" (代理: `{new_obj.proxy_url.split('@')[-1]}`)" if new_obj.proxy_url else " (直连)"
        
        await query.edit_message_text(
            f"✅ 已切换激活账号: *{new_acc}* (`{new_obj.config_dict['region']}`){proxy_desc}\n"
            f"状态: `{'🚀 正在抢机中' if new_state['is_sniping'] else '💤 待机'}`",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(new_state["is_sniping"], new_acc, new_state["spec"], new_state["interval"]),
        )

    elif data == "reload_accounts":
        reload_all_accounts()
        accounts_state = context.chat_data.setdefault("accounts_state", {})
        await query.edit_message_text(
            f"🔄 刷新完毕！共发现 `{len(ACCOUNTS)}` 个账号。",
            reply_markup=build_accounts_keyboard(current_acc_name, accounts_state),
        )

    elif data == "menu_instances":
        if not acc:
            await query.edit_message_text("❌ 当前账号凭据失效")
            return
        await query.edit_message_text("⏳ 正在拉取实例与 IP 信息...")
        try:
            kb = await loop.run_in_executor(None, build_instances_keyboard, acc)
            await query.edit_message_text(f"🖥 *[{acc.name}] 实例列表*\n点击实例可管理电源、更换 IP 或删除：", parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            await query.edit_message_text(f"❌ 获取实例失败: `{e}`", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))

    elif data == "menu_change_ip_direct":
        if not acc:
            await query.edit_message_text("❌ 当前账号凭据失效")
            return
        await query.edit_message_text("⏳ 正在查询该账号下所有实例公网 IP...")
        try:
            kb = await loop.run_in_executor(None, build_change_ip_keyboard, acc)
            await query.edit_message_text(f"🔄 *[{acc.name}] 选择要更换公网 IP 的机器*：", parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            await query.edit_message_text(f"❌ 获取实例失败: `{e}`", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))

    elif data == "menu_boot_vols":
        if not acc:
            await query.edit_message_text("❌ 当前账号凭据失效")
            return
        await query.edit_message_text("⏳ 正在检索账号下的全部引导卷...")
        try:
            kb = await loop.run_in_executor(None, build_boot_volumes_keyboard, acc)
            await query.edit_message_text(f"💾 *[{acc.name}] 引导卷列表*\n点击可查看关联机器与独立删除：", parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            await query.edit_message_text(f"❌ 获取引导卷失败: `{e}`", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))

    elif data.startswith("bv_"):
        short_id = data.replace("bv_", "")
        bv_id = get_full_ocid(short_id)
        if not bv_id:
            await query.edit_message_text("❌ 引导卷信息已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        await query.edit_message_text("⏳ 正在检索该引导卷详细信息及关联实例...")
        try:
            bv_info = await loop.run_in_executor(None, acc.blockstorage_client.get_boot_volume, bv_id)
            bv_obj = bv_info.data
            bv_name = bv_obj.display_name
            bv_size = bv_obj.size_in_gbs
            bv_state = bv_obj.lifecycle_state

            attached_vm_desc = "⚠️ 未挂载 (闲置卷，可安全删除)"
            try:
                ads = acc.identity_client.list_availability_domains(acc.compartment_id).data
                found_vm = False
                for ad in ads:
                    attachments = acc.compute_client.list_boot_volume_attachments(
                        availability_domain=ad.name,
                        compartment_id=acc.compartment_id,
                        boot_volume_id=bv_id
                    ).data
                    for att in attachments:
                        if att.lifecycle_state == "ATTACHED":
                            inst_data = acc.compute_client.get_instance(att.instance_id).data
                            pub_ip, _ = get_instance_ips(acc, inst_data.id)
                            attached_vm_desc = f"🖥 `{inst_data.display_name}` (IP: `{pub_ip}`)"
                            found_vm = True
                            break
                    if found_vm:
                        break
            except Exception as att_err:
                attached_vm_desc = f"查询挂载关系异常: {att_err}"

            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💣 确认删除该引导卷", callback_data=f"ask_delbv_{short_id}")],
                [
                    InlineKeyboardButton("🔙 返回引导卷列表", callback_data="menu_boot_vols"),
                    InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")
                ]
            ])

            await query.edit_message_text(
                f"💾 *引导卷详情*\n\n"
                f"• 名称: `{bv_name}`\n"
                f"• 容量: `{bv_size} GB`\n"
                f"• 状态: `{bv_state}`\n"
                f"• 关联实例: {attached_vm_desc}\n\n"
                "💡 *提示*：若关联实例显示【未挂载】，说明是遗留卷，可放心删除以释放免费磁盘额度。",
                parse_mode="Markdown",
                reply_markup=confirm_kb
            )
        except Exception as e:
            await query.edit_message_text(f"❌ 读取引导卷详情失败: `{e}`", reply_markup=build_boot_volumes_keyboard(acc))

    elif data.startswith("ask_delbv_"):
        short_id = data.replace("ask_delbv_", "")
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚠️ 确认永久删除", callback_data=f"cf_delbv_{short_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"bv_{short_id}"),
            ],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
        ])
        await query.edit_message_text(
            f"⚠️ *确认永久删除该引导卷？*\n\n"
            f"• 账号: *{current_acc_name}*\n"
            f"• 引导卷短ID: `{short_id}`\n\n"
            "删除后数据将被彻底抹除！若卷正在挂载运行中，OCI 将自动拒绝删除以保护系统。",
            parse_mode="Markdown",
            reply_markup=confirm_kb
        )

    elif data.startswith("cf_delbv_"):
        short_id = data.replace("cf_delbv_", "")
        bv_id = get_full_ocid(short_id)
        if not bv_id:
            await query.edit_message_text("❌ 引导卷信息已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        await query.edit_message_text("⏳ 正在请求 OCI 后台删除引导卷...")
        try:
            await loop.run_in_executor(None, delete_single_boot_volume, acc, bv_id)
            add_bot_log(f"[{acc.name}] 成功删除引导卷: {short_id}")
            await query.edit_message_text(
                f"✅ *引导卷已成功删除！*\n已释放对应的硬盘配额空间。",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]])
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ 引导卷删除失败: `{e}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 返回引导卷列表", callback_data="menu_boot_vols")],
                    [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
                ])
            )

    elif data.startswith("mg_"):
        short_id = data.replace("mg_", "")
        inst_id = get_full_ocid(short_id)
        if not inst_id:
            await query.edit_message_text("❌ 实例已过期或不存在，请重新打开列表。", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return
        
        try:
            inst_info = await loop.run_in_executor(None, acc.compute_client.get_instance, inst_id)
            disp_name = inst_info.data.display_name
            state_desc = inst_info.data.lifecycle_state
            pub_ip, priv_ip = get_instance_ips(acc, inst_id)
        except Exception:
            disp_name = "未知实例"
            state_desc = "未知"
            pub_ip, priv_ip = "未知", "未知"

        await query.edit_message_text(
            f"⚙️ *实例管理*: `{disp_name}`\n"
            f"• 公网 IP: `{pub_ip}`\n"
            f"• 内网 IP: `{priv_ip}`\n"
            f"• 状态: `{state_desc}`\n\n请选择操作：",
            parse_mode="Markdown",
            reply_markup=build_instance_actions_keyboard(short_id)
        )

    elif data.startswith("ask_changeip_"):
        short_id = data.replace("ask_changeip_", "")
        inst_id = get_full_ocid(short_id)
        if not inst_id:
            await query.edit_message_text("❌ 实例映射已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        pub_ip, _ = get_instance_ips(acc, inst_id)
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 确认更换 IP", callback_data=f"cf_changeip_{short_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"mg_{short_id}"),
            ],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
        ])
        await query.edit_message_text(
            f"🔄 *确认更换该机器的公网 IP？*\n\n"
            f"• 当前公网 IP: `{pub_ip}`\n\n"
            "确认后将解绑并释放原 IP，并立即向甲骨文申请全新临时公网 IP。",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )

    elif data.startswith("cf_changeip_"):
        short_id = data.replace("cf_changeip_", "")
        inst_id = get_full_ocid(short_id)
        if not inst_id:
            await query.edit_message_text("❌ 实例映射已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        await query.edit_message_text("⏳ 正在更换 IP，请稍候...")
        try:
            res_str = await loop.run_in_executor(None, change_public_ip, acc, inst_id)
            add_bot_log(f"[{acc.name}] 换 IP 成功: {res_str}")
            await query.edit_message_text(
                f"✅ *更换 IP 成功！*\n\n{res_str}", 
                parse_mode="Markdown", 
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ 查看该实例", callback_data=f"mg_{short_id}")],
                    [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ 更换失败: `{e}`", 
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 返回实例", callback_data=f"mg_{short_id}")],
                    [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
                ])
            )

    elif data.startswith("act_"):
        parts = data.split("_")
        action = parts[1]
        short_id = parts[2]
        inst_id = get_full_ocid(short_id)

        if not inst_id:
            await query.edit_message_text("❌ 实例映射已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        await query.edit_message_text(f"⏳ 正在下发电源动作 `{action}` ...")
        try:
            state = await loop.run_in_executor(None, change_instance_power, acc, inst_id, action)
            add_bot_log(f"[{acc.name}] 电源动作 {action}: 状态变为 {state}")
            await query.edit_message_text(f"✅ 操作完成！当前状态: `{state}`", reply_markup=build_instance_actions_keyboard(short_id))
        except Exception as e:
            await query.edit_message_text(f"❌ 失败: `{e}`", reply_markup=build_instance_actions_keyboard(short_id))

    elif data.startswith("ask_term_"):
        short_id = data.replace("ask_term_", "")
        inst_id = get_full_ocid(short_id)
        if not inst_id:
            await query.edit_message_text("❌ 实例已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        pub_ip, _ = get_instance_ips(acc, inst_id)
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚠️ 确认彻底删除", callback_data=f"cf_term_{short_id}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"mg_{short_id}"),
            ],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
        ])
        await query.edit_message_text(
            f"💣 *危险操作：终止实例*\n\n"
            f"• 账号: *{current_acc_name}*\n"
            f"• 公网 IP: `{pub_ip}`\n\n"
            f"⚠️ **注意**：确认后将**彻底删除该实例及其绑定的引导卷（系统硬盘）**，数据将完全丢失且不可恢复！",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )

    elif data.startswith("cf_term_"):
        short_id = data.replace("cf_term_", "")
        inst_id = get_full_ocid(short_id)
        if not inst_id:
            await query.edit_message_text("❌ 实例已过期", reply_markup=build_main_keyboard(is_sniping, current_acc_name, spec, interval))
            return

        await query.edit_message_text("⏳ 正在彻底删除实例及引导卷...")
        try:
            await loop.run_in_executor(None, terminate_instance_and_boot_volume, acc, inst_id)
            add_bot_log(f"[{acc.name}] 已彻底终止实例及引导卷: {short_id}")
            await query.edit_message_text(
                f"✅ *实例及引导卷已成功提交终止！*\nOCI 后台正在回收计算资源与硬盘容量。",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]]),
            )
        except Exception as e:
            await query.edit_message_text(f"❌ 删除失败: `{e}`", reply_markup=build_instance_actions_keyboard(short_id))

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
            f"⚙️ *[{current_acc_name}] 规格与开机数量调整*\n\n"
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

    elif data == "ask_start_sniper":
        arch_desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if spec["arch"] == "ARM" else "1C 1G"
        target_count = spec.get("target_count", 1)
        boot_gbs = spec.get("boot_gbs", 50)
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认开始", callback_data="confirm_start_sniper"),
                InlineKeyboardButton("❌ 取消", callback_data="menu_main"),
            ],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
        ])
        await query.edit_message_text(
            f"⚠️ *确认启动当前账号抢机任务？*\n\n"
            f"• 账号: *{current_acc_name}*\n"
            f"• 区域: `{acc.config_dict['region']}`\n"
            f"• 出口: `{acc.proxy_url.split('@')[-1] if acc.proxy_url else '直连'}`\n"
            f"• 规格: `{spec['arch']} ({arch_desc})`\n"
            f"• 💾 引导卷: `{boot_gbs} GB`\n"
            f"• 目标台数: `{target_count} 台`\n"
            f"• 轮询间隔: `{interval} 秒`\n\n"
            "确认后该账号将在后台独立启动抢机任务，与其他账号互不影响。",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )

    elif data == "confirm_start_sniper":
        if acc_state["is_sniping"]:
            await query.edit_message_text(f"⚠️ 账号 [{current_acc_name}] 任务已在运行！", reply_markup=build_main_keyboard(True, current_acc_name, spec, interval))
            return

        acc_state["is_sniping"] = True
        acc_state["attempts"] = 0
        acc_state["created_count"] = 0
        target_count = spec.get("target_count", 1)

        job_data = {
            "account": current_acc_name,
            "spec": dict(spec),
        }

        job_name = f"sniper_{chat_id}_{current_acc_name}"
        context.job_queue.run_repeating(
            sniper_job,
            interval=interval,
            first=1,
            chat_id=chat_id,
            name=job_name,
            data=job_data,
        )
        desc = f"{int(spec['ocpus'])}C {int(spec['memory'])}G" if spec["arch"] == "ARM" else "1C 1G"
        add_bot_log(f"[*] 账号 [{current_acc_name}] 独立启动抢机 (目标: {target_count} 台)")

        server_ip = get_server_ip()
        await query.edit_message_text(
            f"🖥 *运行节点 IP*: `{server_ip}`\n"
            f"🚀 *账号 [{current_acc_name}] 抢机任务已启动！*\n"
            f"• 区域: `{acc.config_dict['region']}`\n"
            f"• 规格: `{spec['arch']} ({desc})`\n"
            f"• 💾 引导卷: `{spec.get('boot_gbs', 50)} GB`\n"
            f"• 🎯 目标开机数量: `{target_count} 台`\n"
            f"• 轮询间隔: `{interval}s`\n"
            f"• 开机密码: `{spec.get('root_password', DEFAULT_ROOT_PASSWORD)}`",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(True, current_acc_name, spec, interval),
        )

    elif data == "ask_stop_sniper":
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🛑 确认停止", callback_data="confirm_stop_sniper"),
                InlineKeyboardButton("↩️ 取消并继续", callback_data="menu_main"),
            ],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")]
        ])
        await query.edit_message_text(
            f"⚠️ *确认停止账号 [{current_acc_name}] 抢机？*\n\n"
            f"• 账号: *{current_acc_name}*\n"
            f"• 已尝试: `{acc_state.get('attempts', 0)}` 次\n"
            f"• 已成功开出: `{acc_state.get('created_count', 0)}` 台\n\n"
            "确认仅移除本账号的抢机任务，其他正在运行的账号不受影响。",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )

    elif data == "confirm_stop_sniper":
        job_name = f"sniper_{chat_id}_{current_acc_name}"
        jobs = context.job_queue.get_jobs_by_name(job_name)
        for j in jobs:
            j.schedule_removal()
        acc_state["is_sniping"] = False
        attempts = acc_state.get("attempts", 0)
        created_count = acc_state.get("created_count", 0)
        add_bot_log(f"[*] 账号 [{current_acc_name}] 手动停止抢机")

        await query.edit_message_text(
            f"⏹ *账号 [{current_acc_name}] 抢机任务已停止*\n累计尝试 `{attempts}` 次，已成功开出 `{created_count}` 台。",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard(False, current_acc_name, spec, interval),
        )

    elif data == "menu_interval":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("15s", callback_data="int_15"), InlineKeyboardButton("30s", callback_data="int_30"), InlineKeyboardButton("60s", callback_data="int_60"), InlineKeyboardButton("120s", callback_data="int_120")],
            [InlineKeyboardButton("🏠 返回主控制台", callback_data="menu_main")],
        ])
        await query.edit_message_text("⏱ 选择抢机轮询间隔（防封号推荐 60s 或以上）：", reply_markup=keyboard)

    elif data.startswith("int_"):
        new_int = int(data.split("_")[1])
        acc_state["interval"] = new_int
        if acc_state["is_sniping"]:
            job_name = f"sniper_{chat_id}_{current_acc_name}"
            jobs = context.job_queue.get_jobs_by_name(job_name)
            for j in jobs:
                j.schedule_removal()
            job_data = {"account": current_acc_name, "spec": dict(spec)}
            context.job_queue.run_repeating(
                sniper_job,
                interval=new_int,
                first=new_int,
                chat_id=chat_id,
                name=job_name,
                data=job_data,
            )
        await query.edit_message_text(f"✅ [{current_acc_name}] 轮询间隔已设为 `{new_int}` 秒！", reply_markup=build_main_keyboard(acc_state["is_sniping"], current_acc_name, spec, new_int))


def main():
    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_pwd", set_pwd_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    add_bot_log(f"[*] OCI 多账号并发运维 Bot 已启动，已载入 {len(ACCOUNTS)} 个账号。")
    app.run_polling()


if __name__ == "__main__":
    main()
