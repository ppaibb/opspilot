"""华为云只读资源适配：IAM 项目、ECS 和 Flexus L。

本模块只返回导入所需的白名单字段。RMS 套餐属性可能包含敏感信息，
禁止直接向前端透传 SDK 响应，也不能将原始异常写入业务提示。
"""

from dataclasses import dataclass
import ipaddress
import json
import math

from huaweicloudsdkcore.auth.credentials import BasicCredentials, GlobalCredentials
from huaweicloudsdkcore.http.http_config import HttpConfig
from huaweicloudsdkecs.v2 import EcsClient, ListServersDetailsRequest, ShowServerRequest
from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion
from huaweicloudsdkiam.v3 import IamClient, KeystoneListAuthProjectsRequest
from huaweicloudsdkiam.v3.region.iam_region import IamRegion
from huaweicloudsdkrms.v1 import ListAllResourcesRequest, RmsClient
from huaweicloudsdkrms.v1.region.rms_region import RmsRegion

PAGE_SIZE = 100
MAX_PAGES = 100


class HuaweiCloudError(RuntimeError):
    """可以安全呈现给用户的华为云业务错误。"""


@dataclass(frozen=True)
class HuaweiProject:
    """由 IAM 授权结果确定的项目身份，不能使用前端自报的项目身份。"""

    project_id: str
    name: str
    region: str
    account_id: str


@dataclass
class HuaweiScan:
    """统一扫描结果；部分项目失败必须携带警告，不能伪装为完整成功。"""

    instances: list[dict]
    warnings: list[str]


def safe_error(stage: str, exc: Exception) -> str:
    """按状态码归类，不拼接可能带请求、签名或密钥的 SDK 异常正文。"""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        reason = "认证或权限不足，请检查 AK/SK 及 IAM、ECS、配置审计的只读授权"
    elif status == 429:
        reason = "云 API 请求受限，请稍后重试"
    else:
        reason = "云 API 调用失败，请检查网络、凭证及服务授权后重试"
    return f"{stage}：{reason}"


def _number(value) -> float:
    """云字段缺失或格式异常时返回未知容量 0，避免 NaN 写入数据库。"""
    try:
        result = float(value or 0)
        return result if math.isfinite(result) and result >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def normalize_server(server, project: HuaweiProject, flexus: dict | None = None) -> dict:
    """映射真实 SDK 的 snake_case 属性，内存单位 MB 固定换算成 GiB。"""
    if not server.id or server.tenant_id != project.project_id:
        raise HuaweiCloudError("云主机项目身份不一致，已停止导入")
    public_ips, private_ips = [], []
    for addresses in (server.addresses or {}).values():
        for address in addresses or []:
            try:
                parsed = ipaddress.ip_address(address.addr)
            except ValueError:
                continue
            target = public_ips if address.os_ext_ip_stype == "floating" else private_ips
            target.append(parsed)
    # 优先 IPv4，但纯 IPv6 主机仍可以保留实际地址。
    public_ips.sort(key=lambda address: address.version)
    private_ips.sort(key=lambda address: address.version)
    public_ip = str(public_ips[0]) if public_ips else ""
    private_ip = str(private_ips[0]) if private_ips else ""
    flavor = server.flavor
    metadata = server.metadata or {}
    resource_type = "FlexusL" if flexus else "ECS"
    return {
        "instance_id": server.id,
        "name": (flexus or {}).get("name") or server.name or server.id,
        "ip": public_ip or private_ip,
        "private_ip": private_ip,
        "provider": "huawei",
        "region": project.region,
        "cloud_resource_type": resource_type,
        "cloud_project_id": project.project_id,
        "cloud_account_id": project.account_id,
        "status": f"华为云 {resource_type},{server.status or 'UNKNOWN'}",
        "os": metadata.get("image_name") or metadata.get("os_type") or "",
        "cpu_cores": _number(getattr(flavor, "vcpus", None)),
        "memory_gb": round(_number(getattr(flavor, "ram", None)) / 1024, 2),
        # ECS flavor.disk 在官方接口中无效；不把规格值误当实际系统盘。
        # Flexus L 套餐的 root_volume 是实际配置，普通 ECS 交由已有 SSH 采集补齐。
        "system_disk_gb": _number((flexus or {}).get("system_disk_gb")),
    }


class HuaweiCloud:
    """每次请求独立创建客户端；不跨用户缓存明文 AK/SK。"""

    def __init__(self, access_key: str, secret_key: str):
        self._access_key = access_key
        self._secret_key = secret_key
        self._account_id = ""

    @staticmethod
    def _http_config() -> HttpConfig:
        config = HttpConfig.get_default_config()
        config.ignore_ssl_verification = False
        # SDK 使用 requests 的 (连接超时, 读取超时) 元组，不识别独立超时属性。
        config.timeout = (5, 15)
        config.allow_redirects = False
        return config

    def _global_credentials(self) -> GlobalCredentials:
        return GlobalCredentials(self._access_key, self._secret_key, self._account_id or None).with_iam_endpoint(
            "https://iam.cn-north-4.myhuaweicloud.com"
        )

    def iam_client(self):
        """固定官方端点，避免区域输入或 SDK 环境配置变成任意请求目标。"""
        return (IamClient.new_builder().with_credentials(self._global_credentials())
                .with_region(IamRegion.CN_NORTH_4).with_http_config(self._http_config()).build())

    def rms_client(self):
        return (RmsClient.new_builder().with_credentials(self._global_credentials())
                .with_region(RmsRegion.CN_NORTH_4).with_http_config(self._http_config()).build())

    def ecs_client(self, project: HuaweiProject):
        credentials = BasicCredentials(self._access_key, self._secret_key, project.project_id)
        return (EcsClient.new_builder().with_credentials(credentials)
                .with_region(EcsRegion.static_fields[project.region])
                .with_http_config(self._http_config()).build())

    def projects(self) -> list[HuaweiProject]:
        """查询当前身份授权项目，包括同区域的子项目，排除停用项目。"""
        try:
            response = self.iam_client().keystone_list_auth_projects(KeystoneListAuthProjectsRequest())
        except Exception as exc:
            raise HuaweiCloudError(safe_error("获取华为云授权项目失败", exc)) from None
        projects = []
        # 子项目名称是 region_custom_name，按完整区域边界识别，不能随意截短。
        regions = sorted(EcsRegion.static_fields, key=len, reverse=True)
        for item in response.projects or []:
            if item.enabled is False or not item.id or not item.domain_id:
                continue
            region = next((r for r in regions if item.name == r or (item.name or "").startswith(r + "_")), None)
            if region:
                if self._account_id and self._account_id != item.domain_id:
                    raise HuaweiCloudError("授权项目所属云账号不一致，已停止扫描")
                self._account_id = item.domain_id
                projects.append(HuaweiProject(item.id, item.name, region, item.domain_id))
        if not projects:
            raise HuaweiCloudError("没有可用的华为云授权项目，请检查 IAM 项目权限")
        return projects

    def regions(self) -> list[str]:
        """仅扫描有权访问的区域，不盲扫全部地域。"""
        return sorted({project.region for project in self.projects()})

    def flexus_servers(self, region: str) -> dict[str, dict]:
        """按官方 RMS 套餐资源识别轻量主机，不按名字或规格字符串猜测。"""
        found, markers = {}, set()
        marker = None
        try:
            api = self.rms_client()
            for _ in range(MAX_PAGES):
                response = api.list_all_resources(ListAllResourcesRequest(
                    type="hcss.l-instance", region_id=None if region == "auto" else region,
                    limit=PAGE_SIZE, marker=marker,
                ))
                for bundle in response.resources or []:
                    for resource in (bundle.properties or {}).get("resources", []) or []:
                        if resource.get("logical_resource_type") != "huaweicloudinternal_ecs_instance":
                            continue
                        server_id = resource.get("physical_resource_id")
                        if not server_id:
                            continue
                        root_size = 0
                        for attribute in resource.get("resource_attributes", []) or []:
                            if attribute.get("key") == "root_volume":
                                try:
                                    volumes = json.loads(attribute.get("value") or "[]")
                                    root_size = sum(_number(volume.get("size")) for volume in volumes)
                                except (TypeError, ValueError, AttributeError):
                                    root_size = 0
                        found[server_id] = {
                            "project_id": bundle.project_id, "region": bundle.region_id,
                            "name": bundle.name, "system_disk_gb": root_size,
                        }
                marker = getattr(response.page_info, "next_marker", None)
                if not marker:
                    return found
                if marker in markers:
                    raise HuaweiCloudError("Flexus L 分页标记重复，已停止扫描，请重试")
                markers.add(marker)
        except HuaweiCloudError:
            raise
        except Exception as exc:
            # 无法识别轻量服务器时拒绝把它们错标 ECS；授权补齐后重新扫描。
            raise HuaweiCloudError(safe_error("查询 Flexus L 失败", exc)) from None
        raise HuaweiCloudError("Flexus L 超过单次扫描上限，请缩小账号资源范围")

    def scan(self, region: str) -> HuaweiScan:
        """查询全部指定区域授权项目；单个项目失败保留其他项目并明确提示。"""
        projects = self.projects()
        if region != "auto" and region not in {p.region for p in projects}:
            raise HuaweiCloudError("所选区域没有可访问的华为云项目")
        projects = [p for p in projects if region == "auto" or p.region == region]
        flexus = self.flexus_servers(region)
        instances, warnings = {}, []
        successful_projects = 0
        for project in projects:
            try:
                api = self.ecs_client(project)
                project_hosts = {}
                for page in range(1, MAX_PAGES + 1):
                    # 企业项目授权与 IAM 区域项目是两层权限；查询当前身份有权的全部企业项目。
                    response = api.list_servers_details(ListServersDetailsRequest(
                        limit=PAGE_SIZE, offset=page, enterprise_project_id="all_granted_eps",
                    ))
                    servers = response.servers or []
                    for server in servers:
                        light = flexus.get(server.id)
                        if light and light["project_id"] != project.project_id:
                            raise HuaweiCloudError("Flexus L 云主机项目身份不一致")
                        if server.id in project_hosts:
                            raise HuaweiCloudError("ECS 分页返回重复实例，已停止扫描")
                        project_hosts[server.id] = normalize_server(server, project, light)
                    if len(servers) < PAGE_SIZE:
                        break
                else:
                    raise HuaweiCloudError("ECS 超过单次分页扫描上限")
                # 轻量主机可能不在普通 ECS 列表中，按 RMS 中的物理 ID 补查详情。
                for server_id, light in flexus.items():
                    if light["project_id"] != project.project_id or server_id in project_hosts:
                        continue
                    try:
                        response = api.show_server(ShowServerRequest(server_id=server_id))
                        project_hosts[server_id] = normalize_server(response.server, project, light)
                    except Exception as exc:
                        warnings.append(safe_error(f"{project.name} / Flexus L {server_id} 查询失败", exc))
                instances.update(project_hosts)
                successful_projects += 1
            except HuaweiCloudError as exc:
                warnings.append(f"{project.name}：{exc}")
            except Exception as exc:
                warnings.append(safe_error(f"{project.name} 查询失败", exc))
        if successful_projects == 0:
            raise HuaweiCloudError("；".join(warnings) or "没有可扫描的华为云项目")
        return HuaweiScan(list(instances.values()), warnings)
