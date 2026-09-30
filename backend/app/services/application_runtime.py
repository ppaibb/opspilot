"""应用运行实例纯逻辑服务。"""
from __future__ import annotations

from typing import Any

import base64
import posixpath
import re
import shlex

import yaml
from fastapi import HTTPException

RUNTIME_CONTROL_ACTIONS = {"start", "stop", "restart", "status"}
RUNTIME_CONTROL_SUPPORTED_TYPES = {"systemd", "docker", "docker_compose", "supervisor"}
RUNTIME_STATUS_ONLY_TYPES = {"kubernetes", "helm"}
RUNTIME_EXECUTION_CHANNELS = {"auto", "ansible", "ssh_shell"}
MAX_RUNTIME_CONTROL_OUTPUT_LENGTH = 4000
MAX_COMPOSE_CONTENT_LENGTH = 20000
MAX_CONFIG_FILE_VALUE_LENGTH = 20000
MAX_DOCKER_RUN_ARGS_LENGTH = 2000
MAX_SYSTEMD_ARTIFACT_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_SUPERVISOR_COMMAND_LENGTH = 1000
MAX_SUPERVISOR_ENVIRONMENT_LENGTH = 4000
SUPERVISOR_PROGRAM_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SUPERVISOR_USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
SUPERVISOR_FORBIDDEN_COMMAND_CHARS = re.compile("[;&|`$<>\n\r]")
SUPERVISOR_RUNTIME_VALUES = {"system", "managed"}
COMPOSE_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
DOCKER_IMAGE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,499}$")
DOCKER_RUN_ARG_FLAGS_WITH_VALUE = {
    "-p",
    "--publish",
    "-e",
    "--env",
    "--env-file",
    "-v",
    "--volume",
    "--mount",
    "--network",
    "--restart",
    "--add-host",
    "--label",
    "--user",
    "-u",
    "--workdir",
    "-w",
    "--entrypoint",
}
DOCKER_RUN_ARG_FLAGS_NO_VALUE = {"--init", "--read-only", "--tty", "-t"}
DOCKER_RUN_FORBIDDEN_FLAGS = {
    "--name",
    "-d",
    "--detach",
    "--rm",
    "--privileged",
    "--pid",
    "--ipc",
    "--uts",
    "--network=host",
}


def shell_quote(value: str) -> str:
    """对单个 shell 参数做安全转义。"""
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="运行标识不能为空")
    return shlex.quote(text)


def validate_compose_project_name(value: str) -> str:
    """校验 Compose project 名称。"""
    project_name = str(value or "").strip()
    if not COMPOSE_PROJECT_NAME_PATTERN.fullmatch(project_name):
        raise HTTPException(status_code=400, detail="Compose 项目名只允许字母、数字、点、横线和下划线，且不能以符号开头")
    return project_name


def compose_cli_function() -> str:
    """生成兼容 Compose v2 插件与 v1 独立命令的受控 shell 函数。"""
    return (
        "compose_cmd() { "
        "if docker compose version >/dev/null 2>&1; then docker compose \"$@\"; "
        "elif command -v docker-compose >/dev/null 2>&1; then docker-compose \"$@\"; "
        "else echo 'Docker Compose CLI 不可用：请安装 Docker Compose 插件或 docker-compose' >&2; return 127; fi; "
        "}; "
    )


def build_compose_preflight_module_args(
    project_name: str,
    deploy_dir: str,
    compose_filename: str,
    compose_content: str,
    env_filename: str = ".env",
    env_content: str = "",
) -> tuple[str, str]:
    """生成 Compose 项目只读预检脚本，不写远程文件、不拉镜像、不启停容器。"""
    project = validate_compose_project_name(project_name)
    directory = validate_compose_deploy_path(deploy_dir)
    filename = validate_compose_filename(compose_filename)
    compose_path = f"{directory.rstrip('/')}/{filename}"
    compose_text = validate_compose_content(compose_content)
    env_text = validate_env_content(env_content)
    safe_env_filename = validate_env_filename(env_filename)
    compose_b64 = base64.b64encode(compose_text.encode("utf-8")).decode("ascii")
    env_b64 = base64.b64encode(env_text.encode("utf-8")).decode("ascii") if env_text else ""
    env_option = f"--env-file \"$tmp_dir/{safe_env_filename}\" " if env_text else ""
    script = "\n".join([
        "set -e",
        "command -v docker >/dev/null 2>&1 || { echo 'Docker CLI 未安装' >&2; exit 127; }",
        "docker info >/dev/null 2>&1 || { echo 'Docker daemon 不可用或当前账号无权限' >&2; exit 1; }",
        compose_cli_function(),
        "compose_cmd version >/dev/null",
        f"test ! -d {shell_quote(directory)} || test -w {shell_quote(directory)} || {{ echo '部署目录不可写' >&2; exit 1; }}",
        f"test ! -d {shell_quote(directory)} || test -z \"$(find {shell_quote(directory)} -mindepth 1 -maxdepth 1 -print -quit)\" || {{ echo '部署目录已存在且非空，为避免覆盖现有文件，本次部署已拒绝' >&2; exit 2; }}",
        "tmp_dir=$(mktemp -d)",
        "trap 'rm -rf \"$tmp_dir\"' EXIT",
        f"printf %s {shell_quote(compose_b64)} | base64 -d > \"$tmp_dir/{filename}\"",
        *([f"printf %s {shell_quote(env_b64)} | base64 -d > \"$tmp_dir/{safe_env_filename}\""] if env_b64 else []),
        f"compose_cmd -p {shell_quote(project)} {env_option}-f \"$tmp_dir/{filename}\" config --services",
    ])
    return script, compose_path


def build_compose_project_discovery_command(project_name: str) -> str:
    """按 Compose project 标准标签生成部署后服务发现命令。"""
    project = validate_compose_project_name(project_name)
    return (
        f"docker ps -a --filter label=com.docker.compose.project={shell_quote(project)} "
        "--filter label=com.docker.compose.service "
        "--format '{{.Label \"com.docker.compose.project\"}}\t"
        "{{.Label \"com.docker.compose.project.working_dir\"}}\t"
        "{{.Label \"com.docker.compose.project.config_files\"}}\t"
        "{{.Label \"com.docker.compose.service\"}}\t{{.Names}}\t{{.Status}}'"
    )


def validate_docker_image_ref(value: str) -> str:
    """校验 Docker 镜像引用。

    发布页只允许传镜像名、tag 或 digest，不允许空格、Shell 元字符或超长文本，
    避免 Docker 发布入口退化成命令注入通道。
    """
    image_ref = str(value or "").strip()
    if not image_ref:
        raise HTTPException(status_code=400, detail="Docker 发布必须填写镜像地址或 tag")
    if not DOCKER_IMAGE_REF_PATTERN.match(image_ref):
        raise HTTPException(status_code=400, detail="Docker 镜像地址包含非法字符")
    return image_ref


def validate_docker_run_args(value: str) -> list[str]:
    """解析并校验 Docker run 参数白名单。

    允许端口、环境变量、卷、网络、重启策略等常见运行参数；禁止 `--name`、
    `-d`、`--rm`、`--privileged` 和 host namespace，容器名和生命周期由平台脚本控制。
    """
    text = str(value or "").strip()
    if not text:
        return []
    if len(text) > MAX_DOCKER_RUN_ARGS_LENGTH:
        raise HTTPException(status_code=400, detail="Docker run 参数不能超过 2000 字符")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Docker run 参数解析失败：{exc}") from exc
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            raise HTTPException(status_code=400, detail="Docker run 参数只能填写选项，镜像由发布表单单独指定")
        if token in DOCKER_RUN_FORBIDDEN_FLAGS or any(token.startswith(f"{flag}=") for flag in DOCKER_RUN_FORBIDDEN_FLAGS):
            raise HTTPException(status_code=400, detail=f"Docker run 参数不允许使用 {token}")
        if token in DOCKER_RUN_ARG_FLAGS_NO_VALUE:
            result.append(token)
            index += 1
            continue
        matched_value_flag = None
        for flag in DOCKER_RUN_ARG_FLAGS_WITH_VALUE:
            if token == flag or token.startswith(f"{flag}="):
                matched_value_flag = flag
                break
        if not matched_value_flag:
            raise HTTPException(status_code=400, detail=f"Docker run 参数不在白名单内：{token}")
        result.append(token)
        if token == matched_value_flag:
            index += 1
            if index >= len(tokens) or tokens[index].startswith("-"):
                raise HTTPException(status_code=400, detail=f"Docker run 参数 {token} 缺少取值")
            result.append(tokens[index])
        index += 1
    return result


def validate_compose_deploy_path(value: str) -> str:
    """校验 Docker Compose 标准部署目录。"""
    raw_value = str(value or "").strip()
    if not raw_value:
        raise HTTPException(status_code=400, detail="Compose 部署目录不能为空")
    if "\x00" in raw_value:
        raise HTTPException(status_code=400, detail="Compose 部署目录包含非法字符")
    if not raw_value.startswith("/"):
        raise HTTPException(status_code=400, detail="Compose 部署目录必须是 Linux 绝对路径")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", raw_value):
        raise HTTPException(status_code=400, detail="Compose 部署目录只允许字母、数字、点、横线、下划线和斜杠")

    normalized = posixpath.normpath(raw_value)
    if normalized in {"/", "/opt", "/srv", "/data", "/home", "/var", "/var/www", "/root"}:
        raise HTTPException(status_code=400, detail="Compose 部署目录必须指向具体应用目录")
    if normalized.startswith("../") or "/../" in normalized:
        raise HTTPException(status_code=400, detail="Compose 部署目录不能包含上级目录跳转")

    allowed_prefixes = ("/opt/", "/srv/", "/data/", "/home/", "/var/www/", "/root/")
    if not normalized.startswith(allowed_prefixes):
        raise HTTPException(status_code=400, detail="Compose 部署目录仅允许位于 /opt、/srv、/data、/home、/var/www 或 /root 下")
    return normalized


def validate_compose_filename(value: str) -> str:
    """校验 Compose 文件名，禁止路径穿越和非 yaml 文件。"""
    filename = str(value or "docker-compose.yml").strip() or "docker-compose.yml"
    if "\x00" in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Compose 文件名不能包含路径")
    if len(filename) > 120:
        raise HTTPException(status_code=400, detail="Compose 文件名不能超过 120 个字符")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        raise HTTPException(status_code=400, detail="Compose 文件名只允许字母、数字、点、横线和下划线")
    if not filename.endswith((".yml", ".yaml")):
        raise HTTPException(status_code=400, detail="Compose 文件名必须以 .yml 或 .yaml 结尾")
    return filename


def validate_compose_service_name(value: str) -> str:
    """校验可选服务名，服务名会拼入 docker compose 子命令。"""
    service_name = str(value or "").strip()
    if not service_name:
        return ""
    if len(service_name) > 120:
        raise HTTPException(status_code=400, detail="Compose 服务名不能超过 120 个字符")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_name):
        raise HTTPException(status_code=400, detail="Compose 服务名只允许字母、数字、点、横线和下划线")
    return service_name


def validate_systemd_artifact_filename(value: str, fallback: str = "artifact.bin") -> str:
    """校验 systemd 发布制品文件名，禁止路径穿越。"""
    filename = posixpath.basename(str(value or "").strip()) or fallback
    if len(filename) > 180:
        raise HTTPException(status_code=400, detail="制品文件名不能超过 180 个字符")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", filename):
        raise HTTPException(status_code=400, detail="制品文件名只允许字母、数字、点、横线、下划线和加号")
    return filename


def validate_systemd_artifact_ref(value: str) -> str:
    """校验 systemd 发布制品引用，只允许 URL 或 Linux 绝对路径。"""
    artifact_ref = str(value or "").strip()
    if not artifact_ref:
        return ""
    if artifact_ref.startswith(("http://", "https://")):
        if not re.fullmatch(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,1000}", artifact_ref):
            raise HTTPException(status_code=400, detail="制品 URL 包含非法字符")
        return artifact_ref
    if not artifact_ref.startswith("/"):
        raise HTTPException(status_code=400, detail="制品路径必须是 Linux 绝对路径或 HTTP(S) URL")
    if not re.fullmatch(r"/[A-Za-z0-9._/+=:@-]+", artifact_ref):
        raise HTTPException(status_code=400, detail="制品路径包含非法字符")
    normalized = posixpath.normpath(artifact_ref)
    if normalized.startswith("../") or "/../" in normalized:
        raise HTTPException(status_code=400, detail="制品路径不能包含上级目录跳转")
    return normalized


def validate_systemd_artifact_content_b64(value: str) -> str:
    """校验上传制品的 base64 内容，限制大小并确保可解码。"""
    content = str(value or "").strip()
    if not content:
        return ""
    try:
        decoded = base64.b64decode(content, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="上传制品内容不是合法 base64") from exc
    if len(decoded) > MAX_SYSTEMD_ARTIFACT_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="上传制品不能超过 50MB")
    return content


def validate_supervisor_program_name(value: str) -> str:
    """校验 supervisor program 名称，避免写配置文件时路径穿越。"""
    program = str(value or "").strip()
    if not SUPERVISOR_PROGRAM_PATTERN.fullmatch(program):
        raise HTTPException(status_code=400, detail="Supervisor 程序名只允许字母、数字、点、横线和下划线")
    return program


def validate_supervisor_command(value: str) -> str:
    """校验 supervisor command。

    Supervisor 本身不是 shell，但这里仍拒绝常见 shell 控制符，避免用户把发布
    入口变成脚本执行器；复杂启动逻辑应封装到受控启动脚本后再引用脚本路径。
    """
    command = str(value or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="Supervisor 启动命令不能为空")
    if len(command) > MAX_SUPERVISOR_COMMAND_LENGTH:
        raise HTTPException(status_code=400, detail="Supervisor 启动命令不能超过 1000 字符")
    if SUPERVISOR_FORBIDDEN_COMMAND_CHARS.search(command):
        raise HTTPException(status_code=400, detail="Supervisor 启动命令不能包含 shell 控制符")
    return command


def validate_supervisor_user(value: str) -> str:
    """校验可选运行用户。"""
    user = str(value or "").strip()
    if not user:
        return ""
    if not SUPERVISOR_USER_PATTERN.fullmatch(user):
        raise HTTPException(status_code=400, detail="Supervisor 运行用户格式不合法")
    return user


def validate_supervisor_environment(value: str) -> str:
    """校验 Supervisor environment 多行 KEY=VALUE，并转成 ini 逗号格式。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_SUPERVISOR_ENVIRONMENT_LENGTH:
        raise HTTPException(status_code=400, detail="Supervisor 环境变量不能超过 4000 字符")
    pairs: list[str] = []
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        if "=" not in item:
            raise HTTPException(status_code=400, detail="Supervisor 环境变量必须使用 KEY=VALUE")
        key, value_part = item.split("=", 1)
        key = key.strip()
        value_part = value_part.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", key):
            raise HTTPException(status_code=400, detail=f"环境变量名不合法：{key}")
        if any(ch in value_part for ch in ['\x00', '\n', '\r', '"', ","]):
            raise HTTPException(status_code=400, detail=f"环境变量值包含非法字符：{key}")
        pairs.append(f'{key}="{value_part}"')
    return ",".join(pairs)


def validate_supervisor_runtime(value: str) -> str:
    """校验 Supervisor 运行时模式。

    system 表示复用目标机已有 Supervisor；managed 表示在目标机初始化
    OpsPilot 独立托管 runtime，避免依赖系统包预装。
    """
    runtime = str(value or "system").strip().lower()
    if runtime not in SUPERVISOR_RUNTIME_VALUES:
        raise HTTPException(status_code=400, detail="Supervisor 运行时仅支持 system/managed")
    return runtime


def validate_compose_content(value: str) -> str:
    """校验 Compose 文件内容。"""
    content = str(value or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Compose 文件内容不能为空")
    if "\x00" in content:
        raise HTTPException(status_code=400, detail="Compose 文件内容包含非法字符")
    if len(content) > MAX_COMPOSE_CONTENT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Compose 文件内容不能超过 {MAX_COMPOSE_CONTENT_LENGTH} 字符")
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Compose 文件 YAML 格式不正确：{exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Compose 文件顶层必须是对象")
    services = parsed.get("services")
    if not isinstance(services, dict) or not services:
        raise HTTPException(status_code=400, detail="Compose 文件 services 必须是非空对象")
    invalid_services = [name for name, spec in services.items() if not isinstance(name, str) or not isinstance(spec, dict)]
    if invalid_services:
        raise HTTPException(status_code=400, detail="Compose 文件每个 service 都必须是对象")
    return content


def validate_compose_file_path(value: str) -> str:
    """校验 Compose 文件绝对路径。"""
    text = str(value or "").strip().replace("\\", "/")
    if not text or "\x00" in text:
        raise HTTPException(status_code=400, detail="Compose 文件路径不能为空")
    normalized = posixpath.normpath(text)
    if not normalized.startswith("/"):
        raise HTTPException(status_code=400, detail="Compose 文件路径必须是 Linux 绝对路径")
    deploy_dir = posixpath.dirname(normalized)
    filename = posixpath.basename(normalized)
    validate_compose_deploy_path(deploy_dir)
    validate_compose_filename(filename)
    return f"{deploy_dir.rstrip('/')}/{filename}"


def validate_env_filename(value: str) -> str:
    """校验 `.env` 文件名。"""
    filename = str(value or ".env").strip() or ".env"
    if "\x00" in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="环境变量文件名不能包含路径")
    if len(filename) > 120:
        raise HTTPException(status_code=400, detail="环境变量文件名不能超过 120 个字符")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        raise HTTPException(status_code=400, detail="环境变量文件名只允许字母、数字、点、横线和下划线")
    return filename


def validate_env_file_path(value: str, compose_file: str) -> str:
    """校验 `.env` 文件路径；未传时默认与 compose 文件同目录。"""
    text = str(value or "").strip()
    compose_dir = posixpath.dirname(validate_compose_file_path(compose_file))
    if not text:
        return f"{compose_dir}/.env"
    normalized = posixpath.normpath(text.replace("\\", "/"))
    if not normalized.startswith("/"):
        normalized = f"{compose_dir.rstrip('/')}/{normalized.lstrip('/')}"
    deploy_dir = posixpath.dirname(normalized)
    filename = posixpath.basename(normalized)
    validate_compose_deploy_path(deploy_dir)
    validate_env_filename(filename)
    return f"{deploy_dir.rstrip('/')}/{filename}"


def validate_env_content(value: str) -> str:
    """校验环境变量文件内容。"""
    content = str(value or "").strip()
    if "\x00" in content:
        raise HTTPException(status_code=400, detail="环境变量文件内容包含非法字符")
    if len(content) > MAX_CONFIG_FILE_VALUE_LENGTH:
        raise HTTPException(status_code=400, detail=f"环境变量文件内容不能超过 {MAX_CONFIG_FILE_VALUE_LENGTH} 字符")
    return content


def build_compose_control_ref(compose_file: str, service_name: str = "", env_file: str = "") -> str:
    """生成结构化 Compose 运行标识。"""
    parts = [f"compose={validate_compose_file_path(compose_file)}"]
    env_path = str(env_file or "").strip()
    if env_path:
        parts.append(f"env={validate_env_file_path(env_path, compose_file)}")
    service = validate_compose_service_name(service_name)
    if service:
        parts.append(f"service={service}")
    return ";".join(parts)


def parse_compose_control_ref(control_ref: str) -> tuple[str, str, str]:
    """解析 Compose 运行标识。"""
    text = str(control_ref or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Compose 运行标识不能为空")
    if "=" in text:
        parts: dict[str, str] = {}
        for chunk in text.split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            parts[key.strip()] = value.strip()
        compose_file = parts.get("compose") or parts.get("file") or ""
        service = parts.get("service") or ""
        env_file = parts.get("env") or parts.get("env_file") or ""
    elif ":" in text:
        compose_file, service = text.rsplit(":", 1)
        compose_file = compose_file.strip()
        service = service.strip()
        env_file = ""
    else:
        compose_file = text
        service = ""
        env_file = ""
    if not compose_file:
        raise HTTPException(status_code=400, detail="Compose 运行标识缺少 compose 文件")
    compose_path = validate_compose_file_path(compose_file)
    env_path = validate_env_file_path(env_file, compose_path) if env_file else ""
    return compose_path, validate_compose_service_name(service), env_path


def parse_kubernetes_control_ref(control_ref: str) -> tuple[str, str, str]:
    """解析 Kubernetes 运行标识。"""
    parts = [item.strip() for item in str(control_ref or "").strip().split("/") if item.strip()]
    if len(parts) == 2:
        namespace, name = parts
        kind = "deployment"
    elif len(parts) == 3:
        namespace, kind, name = parts
    else:
        raise HTTPException(status_code=400, detail="Kubernetes 运行标识格式应为 namespace/deployment/name")
    return namespace, kind, name


def parse_helm_control_ref(control_ref: str) -> tuple[str, str]:
    """解析 Helm 运行标识，格式为 `namespace/release`。"""
    parts = [item.strip() for item in str(control_ref or "").strip().split("/") if item.strip()]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Helm 运行标识格式应为 namespace/release")
    return parts[0], parts[1]


def resolve_supervisor_runtime_metadata(value: str) -> str:
    """从运行实例元数据中解析 Supervisor 运行时。

    运行实例表暂时没有独立的 supervisor_runtime 字段，第一版复用历史
    `runtime` 字段保存 `supervisor:managed` / `supervisor:system`。这里统一解析，
    避免生命周期控制直接信任前端传入的任意字符串。
    """
    text = str(value or "").strip().lower()
    if text.startswith("supervisor:"):
        return validate_supervisor_runtime(text.split(":", 1)[1])
    if text in SUPERVISOR_RUNTIME_VALUES:
        return validate_supervisor_runtime(text)
    return "system"


def build_runtime_control_module_args(
    deployment_type: str,
    control_ref: str,
    action: str,
    runtime_metadata: str = "",
) -> str:
    """根据运行类型和动作生成固定白名单命令。

    安全边界：该函数只接受运行类型、运行标识和动作，不接受任意命令。
    Supervisor 托管模式的配置路径固定为 /opt/opspilot/supervisor，不能由用户配置，
    防止把生命周期控制扩展成任意文件路径执行器。
    """
    runtime_type = str(deployment_type or "").strip()
    action_name = str(action or "").strip().lower()
    if action_name == "update":
        raise HTTPException(status_code=400, detail="运行实例只支持生命周期控制，更新发布请在发布部署页执行")
    if action_name not in RUNTIME_CONTROL_ACTIONS:
        raise HTTPException(status_code=400, detail="运行实例动作不支持")
    if runtime_type in RUNTIME_STATUS_ONLY_TYPES:
        if action_name != "status":
            raise HTTPException(status_code=400, detail="该运行方式一期仅支持只读状态检查")
        if runtime_type == "kubernetes":
            namespace, kind, name = parse_kubernetes_control_ref(control_ref)
            return f"kubectl -n {shell_quote(namespace)} get {shell_quote(kind)} {shell_quote(name)} -o json"
        namespace, release = parse_helm_control_ref(control_ref)
        return f"helm -n {shell_quote(namespace)} status {shell_quote(release)} -o json"
    if runtime_type not in RUNTIME_CONTROL_SUPPORTED_TYPES:
        raise HTTPException(status_code=400, detail="该运行方式暂不支持平台启停")
    if runtime_type in {"systemd", "binary"}:
        service = shell_quote(control_ref)
        if action_name == "status":
            return f"systemctl status --no-pager {service}"
        return f"systemctl {action_name} {service}"
    if runtime_type == "docker":
        container = shell_quote(control_ref)
        if action_name == "status":
            return f"docker inspect --format '{{{{.State.Status}}}}' {container}"
        return f"docker {action_name} {container}"
    if runtime_type == "supervisor":
        program = shell_quote(validate_supervisor_program_name(control_ref))
        supervisor_runtime = resolve_supervisor_runtime_metadata(runtime_metadata)
        supervisorctl = "supervisorctl"
        if supervisor_runtime == "managed":
            supervisorctl = "supervisorctl -c /opt/opspilot/supervisor/supervisord.conf"
        if action_name == "status":
            return f"{supervisorctl} status {program}"
        return f"{supervisorctl} {action_name} {program}"
    compose_file, service_name, env_file = parse_compose_control_ref(control_ref)
    env_arg = f" --env-file {shell_quote(env_file)}" if env_file else ""
    compose_args = f"-f {shell_quote(compose_file)}{env_arg}"
    service = f" {shell_quote(service_name)}" if service_name else ""
    compose_cli = (
        "compose_cmd() { "
        "if docker compose version >/dev/null 2>&1; then docker compose \"$@\"; "
        "elif command -v docker-compose >/dev/null 2>&1; then docker-compose \"$@\"; "
        "else echo 'Docker Compose CLI 不可用：请安装 Docker Compose 插件或 docker-compose' >&2; return 127; fi; "
        "}; "
    )
    compose_command = f"compose_cmd {compose_args}"
    if action_name == "status":
        return f"{compose_cli}{compose_command} ps{service}"
    return f"{compose_cli}{compose_command} {action_name}{service}"


def build_runtime_release_module_args(deployment_type: str, control_ref: str, artifact_ref: str, action: str = "publish") -> str:
    """生成 K8s/Helm 发布或回滚的固定白名单命令。"""
    runtime_type = str(deployment_type or "").strip()
    action_name = str(action or "publish").strip().lower()
    ref = str(artifact_ref or "").strip()
    if runtime_type == "kubernetes":
        namespace, kind, name = parse_kubernetes_control_ref(control_ref)
        if action_name == "publish":
            if not ref:
                raise HTTPException(status_code=400, detail="Kubernetes 发布需要填写 manifest 路径或 URL")
            return f"kubectl -n {shell_quote(namespace)} apply -f {shell_quote(ref)}"
        if action_name == "rollback":
            if not ref.isdigit():
                raise HTTPException(status_code=400, detail="Kubernetes 回滚版本必须是数字 revision")
            return f"kubectl -n {shell_quote(namespace)} rollout undo {shell_quote(kind)}/{shell_quote(name)} --to-revision={shell_quote(ref)}"
    if runtime_type == "helm":
        namespace, release = parse_helm_control_ref(control_ref)
        if action_name == "publish":
            if not ref:
                raise HTTPException(status_code=400, detail="Helm 发布需要填写 chart 引用")
            return f"helm upgrade --install {shell_quote(release)} {shell_quote(ref)} -n {shell_quote(namespace)} --wait"
        if action_name == "rollback":
            if not ref.isdigit():
                raise HTTPException(status_code=400, detail="Helm 回滚版本必须是数字 revision")
            return f"helm -n {shell_quote(namespace)} rollback {shell_quote(release)} {shell_quote(ref)} --wait"
    raise HTTPException(status_code=400, detail="该运行方式暂不支持发布或回滚")


def resolve_runtime_execution_channel(channel: str, target_count: int) -> str:
    """解析运行实例实际执行通道。"""
    execution_channel = str(channel or "auto").strip()
    if execution_channel not in RUNTIME_EXECUTION_CHANNELS:
        raise HTTPException(status_code=400, detail="执行通道仅支持 auto/ansible/ssh_shell")
    if target_count < 1:
        raise HTTPException(status_code=400, detail="运行实例未绑定主机资源，无法执行启停控制")
    if execution_channel == "auto":
        return "ssh_shell" if target_count == 1 else "ansible"
    if execution_channel == "ssh_shell" and target_count != 1:
        raise HTTPException(status_code=400, detail="SSH 直连通道仅支持单台主机")
    return execution_channel


def derive_runtime_status(action: str, deployment_type: str, ok: bool, stdout: str, stderr: str) -> str | None:
    """根据运行控制结果推导实例状态。"""
    action_name = str(action or "").strip().lower()
    runtime_type = str(deployment_type or "").strip()
    output = f"{stdout or ''}\n{stderr or ''}".lower()
    if action_name != "status":
        if not ok:
            return None
        if action_name in {"start", "restart"}:
            return "active"
        if action_name == "stop":
            return "offline"
        return None
    if runtime_type == "systemd":
        if "active: active (running)" in output or "active: active" in output:
            return "active"
        if any(token in output for token in ["inactive", "failed", "dead", "not-found", "not found"]):
            return "offline"
    if runtime_type == "docker":
        if "running" in output:
            return "active"
        if any(token in output for token in ["exited", "dead", "created", "restarting", "paused", "not found"]):
            return "offline"
    if runtime_type == "docker_compose":
        if "running" in output or " up " in output or "\tup" in output:
            return "active"
        if any(token in output for token in ["exited", "created", "not found", "no such service"]):
            return "offline"
    if runtime_type == "supervisor":
        if "running" in output:
            return "active"
        if any(token in output for token in ["stopped", "fatal", "exited", "backoff", "no such process"]):
            return "offline"
    if runtime_type == "kubernetes":
        if any(token in output for token in ['"readyreplicas":1', '"availablereplicas":1', '"readyreplicas": 1', '"availablereplicas": 1']):
            return "active"
        if any(token in output for token in ['"replicas":0', '"replicas": 0', "notfound", "not found"]):
            return "offline"
    if runtime_type == "helm":
        if '"status":"deployed"' in output or '"status": "deployed"' in output or "status:\tdeployed" in output:
            return "active"
        if any(token in output for token in ["failed", "pending", "uninstalled", "not found"]):
            return "offline"
    if ok:
        return "active"
    return "unknown" if action_name == "status" else "offline"


def truncate_runtime_control_output(stdout: str, stderr: str) -> str:
    """合并并截断运行控制输出。"""
    text = "\n".join([part for part in [stdout.strip(), stderr.strip()] if part])
    return text[-MAX_RUNTIME_CONTROL_OUTPUT_LENGTH:]


def build_compose_update_module_args(
    compose_file: str,
    service_name: str,
    compose_content: str,
    env_file: str = "",
    env_content: str = "",
    pull_image: bool = True,
    project_name: str = "",
) -> str:
    """生成 Compose 项目首次部署脚本，拒绝复用非空目录并在失败时清理本次内容。"""
    compose_path = validate_compose_file_path(compose_file)
    deploy_dir = posixpath.dirname(compose_path)
    service = validate_compose_service_name(service_name)
    compose_text = validate_compose_content(compose_content)
    env_text = validate_env_content(env_content)
    env_path = validate_env_file_path(env_file, compose_path) if env_text or env_file else ""
    inferred_project = re.sub(r"[^A-Za-z0-9_.-]", "-", posixpath.basename(deploy_dir)).strip(".-_") or "opspilot-app"
    project = validate_compose_project_name(project_name or inferred_project)
    service_arg = f" {shell_quote(service)}" if service else ""
    env_arg = f" --env-file {shell_quote(env_path)}" if env_path else ""
    project_arg = f" -p {shell_quote(project)}"
    compose_cmd = f"compose_cmd{project_arg} -f {shell_quote(compose_path)}{env_arg}"
    compose_b64 = base64.b64encode(compose_text.encode("utf-8")).decode("ascii")
    env_b64 = base64.b64encode(env_text.encode("utf-8")).decode("ascii") if env_text else ""
    lines = [
        "set -e",
        compose_cli_function(),
        f"project={shell_quote(project)}",
        f"compose_file={shell_quote(compose_path)}",
        f"deploy_dir={shell_quote(deploy_dir)}",
        "existing_project=$(docker ps -a --filter \"label=com.docker.compose.project=$project\" --format '{{.ID}}' | head -n 1)",
        "if [ -n \"$existing_project\" ]; then echo '目标 Compose 项目已存在，为避免影响现有业务，本入口拒绝覆盖部署' >&2; exit 2; fi",
        "created_deploy_dir=\"false\"",
        "rollback_deploy() {",
        "  exit_code=$?",
        "  trap - EXIT",
        "  compose_cmd -p \"$project\" -f \"$compose_file\" down --remove-orphans >/dev/null 2>&1 || true",
        "  rm -f \"$compose_file\"",
        *( [f"  rm -f {shell_quote(env_path)}"] if env_path else [] ),
        "  if [ \"$created_deploy_dir\" = \"true\" ]; then rmdir \"$deploy_dir\" 2>/dev/null || true; fi",
        "  echo \"部署失败，已清理本次创建的 Compose 项目和文件（退出码 $exit_code）\" >&2",
        "  exit \"$exit_code\"",
        "}",
        "trap rollback_deploy EXIT",
        "if [ -d \"$deploy_dir\" ]; then",
        "  if [ -n \"$(find \"$deploy_dir\" -mindepth 1 -maxdepth 1 -print -quit)\" ]; then echo '部署目录已存在且非空，为避免覆盖现有文件，本次部署已拒绝' >&2; exit 2; fi",
        "else",
        "  mkdir -p \"$deploy_dir\"",
        "  created_deploy_dir=\"true\"",
        "fi",
        f"printf %s {shell_quote(compose_b64)} | base64 -d > \"$compose_file\"",
    ]
    if env_b64:
        lines.extend([
            f"printf %s {shell_quote(env_b64)} | base64 -d > {shell_quote(env_path)}",
        ])
    if pull_image:
        lines.append(f"{compose_cmd} pull{service_arg}")
    lines.extend([
        f"{compose_cmd} up -d{service_arg}",
        f"{compose_cmd} ps{service_arg}",
        "expected_services=$(compose_cmd -p \"$project\" -f \"$compose_file\" config --services | wc -l | tr -d ' ')",
        "running_services=$(docker ps --filter \"label=com.docker.compose.project=$project\" --filter label=com.docker.compose.service --format '{{.Label \"com.docker.compose.service\"}}' | sort -u | wc -l | tr -d ' ')",
        "if [ \"$expected_services\" -lt 1 ] || [ \"$running_services\" -ne \"$expected_services\" ]; then echo \"Compose 服务未全部运行：期望 $expected_services，实际 $running_services\" >&2; exit 3; fi",
        "trap - EXIT",
        "echo \"Compose 项目部署成功：$running_services/$expected_services 个服务运行中\"",
    ])
    return "\n".join(lines)


def build_docker_container_release_module_args(container_name: str, image_ref: str, docker_run_args: str = "") -> str:
    """生成 Docker 单容器镜像发布脚本。

    业务取舍：Docker 单容器无法像 Compose 一样从声明式文件恢复完整运行参数，
    因此发布页显式填写新镜像和受限 `docker run` 参数。脚本先备份旧容器名，
    新容器启动成功才删除备份；失败时尽力恢复旧容器，避免发布失败后无容器可用。
    """
    container = shell_quote(container_name)
    image = shell_quote(validate_docker_image_ref(image_ref))
    args = " ".join(shell_quote(item) for item in validate_docker_run_args(docker_run_args))
    backup_name = f"{container_name}.opspilot.bak"
    backup = shell_quote(backup_name)
    run_args = f" {args}" if args else ""
    return "\n".join([
        "set -e",
        f"container={container}",
        f"backup={backup}",
        f"image={image}",
        "docker pull \"$image\"",
        "if docker ps -a --format '{{.Names}}' | grep -Fx \"$backup\" >/dev/null; then",
        "  docker rm -f \"$backup\" >/dev/null",
        "fi",
        "docker stop \"$container\" >/dev/null 2>&1 || true",
        "docker rename \"$container\" \"$backup\"",
        f"if ! docker run -d --name \"$container\"{run_args} \"$image\"; then",
        "  docker rm -f \"$container\" >/dev/null 2>&1 || true",
        "  docker rename \"$backup\" \"$container\"",
        "  docker start \"$container\" >/dev/null",
        "  echo 'new container failed, rolled back to previous container' >&2",
        "  exit 1",
        "fi",
        "docker rm -f \"$backup\" >/dev/null",
        "docker inspect --format '{{.State.Status}}' \"$container\"",
    ])


def build_systemd_artifact_release_module_args(
    service_name: str,
    deploy_dir: str,
    artifact_ref: str = "",
    artifact_filename: str = "",
    artifact_content_b64: str = "",
) -> str:
    """生成 systemd 制品发布脚本。

    约定 systemd unit 的 ExecStart 指向部署目录下的 `current` 链接或其内部文件。
    发布时将制品放入 `releases/`，切换 `current` 链接后重启；重启失败则把链接
    切回上一个目标并尽力恢复服务。
    """
    service = shell_quote(service_name)
    deploy_path = validate_compose_deploy_path(deploy_dir)
    upload_b64 = validate_systemd_artifact_content_b64(artifact_content_b64)
    source_ref = validate_systemd_artifact_ref(artifact_ref)
    if not upload_b64 and not source_ref:
        raise HTTPException(status_code=400, detail="systemd 发布必须提供制品路径、URL 或上传文件")
    if upload_b64:
        filename = validate_systemd_artifact_filename(artifact_filename)
    else:
        source_name = posixpath.basename(source_ref.split("?", 1)[0].rstrip("/")) or "artifact.bin"
        filename = validate_systemd_artifact_filename(artifact_filename, source_name)
    release_dir = f"{deploy_path.rstrip('/')}/releases"
    target_path = f"{release_dir}/{filename}"
    current_link = f"{deploy_path.rstrip('/')}/current"
    lines = [
        "set -e",
        f"service={service}",
        f"release_dir={shell_quote(release_dir)}",
        f"target={shell_quote(target_path)}",
        f"current={shell_quote(current_link)}",
        "mkdir -p \"$release_dir\"",
        "previous=\"\"",
        "if [ -L \"$current\" ]; then previous=$(readlink \"$current\"); fi",
    ]
    if upload_b64:
        lines.append(f"printf %s {shell_quote(upload_b64)} | base64 -d > \"$target.tmp\"")
    elif source_ref.startswith(("http://", "https://")):
        lines.append(f"curl -fL {shell_quote(source_ref)} -o \"$target.tmp\"")
    else:
        lines.append(f"cp {shell_quote(source_ref)} \"$target.tmp\"")
    lines.extend([
        "mv \"$target.tmp\" \"$target\"",
        "chmod 0755 \"$target\"",
        "ln -sfn \"$target\" \"$current\"",
        "systemctl daemon-reload",
        "if ! systemctl restart \"$service\"; then",
        "  if [ -n \"$previous\" ]; then",
        "    ln -sfn \"$previous\" \"$current\"",
        "    systemctl restart \"$service\" >/dev/null 2>&1 || true",
        "  fi",
        "  echo 'systemd release failed, rolled back current link when possible' >&2",
        "  exit 1",
        "fi",
        "systemctl status --no-pager \"$service\"",
    ])
    return "\n".join(lines)


def build_supervisor_artifact_release_module_args(
    program_name: str,
    deploy_dir: str,
    command: str,
    artifact_ref: str = "",
    artifact_filename: str = "",
    artifact_content_b64: str = "",
    run_user: str = "",
    environment: str = "",
    runtime_mode: str = "system",
) -> str:
    """生成 Supervisor 进程托管发布脚本。

    Supervisor 更适合应用进程托管。脚本固定完成制品落盘、current 链接切换、
    program 配置写入和 `supervisorctl reread/update/restart/status`，不接收任意 shell。
    """
    program = validate_supervisor_program_name(program_name)
    deploy_path = validate_compose_deploy_path(deploy_dir)
    process_command = validate_supervisor_command(command)
    supervisor_user = validate_supervisor_user(run_user)
    supervisor_env = validate_supervisor_environment(environment)
    supervisor_runtime = validate_supervisor_runtime(runtime_mode)
    upload_b64 = validate_systemd_artifact_content_b64(artifact_content_b64)
    source_ref = validate_systemd_artifact_ref(artifact_ref)
    if not upload_b64 and not source_ref:
        raise HTTPException(status_code=400, detail="Supervisor 发布必须提供制品路径、URL 或上传文件")
    if upload_b64:
        filename = validate_systemd_artifact_filename(artifact_filename)
    else:
        source_name = posixpath.basename(source_ref.split("?", 1)[0].rstrip("/")) or "artifact.bin"
        filename = validate_systemd_artifact_filename(artifact_filename, source_name)
    release_dir = f"{deploy_path.rstrip('/')}/releases"
    target_path = f"{release_dir}/{filename}"
    current_link = f"{deploy_path.rstrip('/')}/current"
    stdout_log = f"/var/log/opspilot/{program}.out.log"
    stderr_log = f"/var/log/opspilot/{program}.err.log"
    managed_root = "/opt/opspilot/supervisor"
    managed_conf_dir = f"{managed_root}/conf.d"
    managed_log_dir = "/var/log/opspilot/supervisor"
    managed_socket = f"{managed_root}/run/supervisor.sock"
    supervisorctl = "supervisorctl"
    if supervisor_runtime == "managed":
        config_path = f"{managed_conf_dir}/{program}.conf"
        supervisorctl = f"supervisorctl -c {managed_root}/supervisord.conf"
        stdout_log = f"{managed_log_dir}/{program}.out.log"
        stderr_log = f"{managed_log_dir}/{program}.err.log"
    else:
        config_path = f"/etc/supervisor/conf.d/{program}.conf"
    config_lines = [
        f"[program:{program}]",
        f"command={process_command}",
        f"directory={deploy_path}",
        "autostart=true",
        "autorestart=true",
        "startsecs=3",
        "stopsignal=TERM",
        f"stdout_logfile={stdout_log}",
        f"stderr_logfile={stderr_log}",
    ]
    if supervisor_user:
        config_lines.append(f"user={supervisor_user}")
    if supervisor_env:
        config_lines.append(f"environment={supervisor_env}")
    config_b64 = base64.b64encode(("\n".join(config_lines) + "\n").encode("utf-8")).decode("ascii")
    lines = [
        "set -e",
        f"program={shell_quote(program)}",
        f"release_dir={shell_quote(release_dir)}",
        f"target={shell_quote(target_path)}",
        f"current={shell_quote(current_link)}",
        f"config_path={shell_quote(config_path)}",
        "mkdir -p \"$release_dir\" /var/log/opspilot",
        "previous=\"\"",
        "if [ -L \"$current\" ]; then previous=$(readlink \"$current\"); fi",
    ]
    if supervisor_runtime == "system":
        lines.extend([
            "command -v supervisorctl >/dev/null 2>&1 || { echo 'supervisorctl not found; install Supervisor or choose OpsPilot managed runtime' >&2; exit 127; }",
            "mkdir -p /etc/supervisor/conf.d",
        ])
    else:
        managed_conf = "\n".join([
            "[unix_http_server]",
            f"file={managed_socket}",
            "chmod=0700",
            "",
            "[supervisord]",
            f"logfile={managed_log_dir}/supervisord.log",
            f"pidfile={managed_root}/run/supervisord.pid",
            "childlogdir=/var/log/opspilot/supervisor",
            "",
            "[supervisorctl]",
            f"serverurl=unix://{managed_socket}",
            "",
            "[rpcinterface:supervisor]",
            "supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface",
            "",
            "[include]",
            f"files={managed_conf_dir}/*.conf",
            "",
        ])
        managed_conf_b64 = base64.b64encode(managed_conf.encode("utf-8")).decode("ascii")
        service_content = "\n".join([
            "[Unit]",
            "Description=OpsPilot managed supervisord",
            "After=network.target",
            "",
            "[Service]",
            "Type=forking",
            f"ExecStart=/usr/bin/env supervisord -c {managed_root}/supervisord.conf",
            f"ExecReload=/usr/bin/env supervisorctl -c {managed_root}/supervisord.conf reload",
            f"ExecStop=/usr/bin/env supervisorctl -c {managed_root}/supervisord.conf shutdown",
            "Restart=on-failure",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ])
        service_b64 = base64.b64encode(service_content.encode("utf-8")).decode("ascii")
        lines.extend([
            "command -v supervisord >/dev/null 2>&1 || { echo 'supervisord not found; install python3-supervisor/supervisor first' >&2; exit 127; }",
            "command -v supervisorctl >/dev/null 2>&1 || { echo 'supervisorctl not found; install python3-supervisor/supervisor first' >&2; exit 127; }",
            f"mkdir -p {shell_quote(managed_conf_dir)} {shell_quote(managed_root + '/run')} {shell_quote(managed_log_dir)}",
            f"printf %s {shell_quote(managed_conf_b64)} | base64 -d > {shell_quote(managed_root + '/supervisord.conf')}",
            f"printf %s {shell_quote(service_b64)} | base64 -d > /etc/systemd/system/opspilot-supervisord.service",
            "systemctl daemon-reload",
            "systemctl enable --now opspilot-supervisord.service",
        ])
    if upload_b64:
        lines.append(f"printf %s {shell_quote(upload_b64)} | base64 -d > \"$target.tmp\"")
    elif source_ref.startswith(("http://", "https://")):
        lines.append(f"curl -fL {shell_quote(source_ref)} -o \"$target.tmp\"")
    else:
        lines.append(f"cp {shell_quote(source_ref)} \"$target.tmp\"")
    lines.extend([
        "mv \"$target.tmp\" \"$target\"",
        "chmod 0755 \"$target\"",
        "ln -sfn \"$target\" \"$current\"",
        f"printf %s {shell_quote(config_b64)} | base64 -d > \"$config_path\"",
        f"{supervisorctl} reread",
        f"{supervisorctl} update",
        f"if ! {supervisorctl} restart \"$program\"; then",
        "  if [ -n \"$previous\" ]; then",
        "    ln -sfn \"$previous\" \"$current\"",
        f"    {supervisorctl} restart \"$program\" >/dev/null 2>&1 || true",
        "  fi",
        "  echo 'supervisor release failed, rolled back current link when possible' >&2",
        "  exit 1",
        "fi",
        f"{supervisorctl} status \"$program\"",
    ])
    return "\n".join(lines)


def build_runtime_update_module_args(row: Any, compose_content: str = "", env_content: str = "") -> str:
    """根据运行实例配置生成更新命令。"""
    runtime_type = str(row.deployment_type or "").strip()
    if runtime_type == "docker_compose":
        compose_file, service_name, env_file = parse_compose_control_ref(row.control_ref or "")
        if not compose_content:
            raise HTTPException(status_code=400, detail="运行实例缺少 Compose 文件内容，请先编辑实例并保存 compose 文件")
        return build_compose_update_module_args(
            compose_file=compose_file,
            service_name=service_name,
            compose_content=compose_content,
            env_file=env_file,
            env_content=env_content,
            pull_image=True,
        )
    if runtime_type == "systemd":
        raise HTTPException(status_code=400, detail="systemd 发布必须在发布部署页填写制品信息")
    if runtime_type == "docker":
        raise HTTPException(status_code=400, detail="Docker 发布必须在发布部署页填写镜像地址")
    if runtime_type in {"kubernetes", "kubernetes_yaml"}:
        namespace, kind, name = parse_kubernetes_control_ref(row.control_ref or "")
        return f"kubectl -n {shell_quote(namespace)} rollout restart {shell_quote(kind)}/{shell_quote(name)}"
    if runtime_type == "helm":
        raise HTTPException(status_code=400, detail="Helm 运行实例请在“发布记录”里选择 chart 再执行 helm upgrade")
    raise HTTPException(status_code=400, detail="该运行方式暂不支持平台更新")


def build_runtime_discovery_command(discovery_type: str) -> str:
    """生成运行实例扫描命令。"""
    runtime_type = str(discovery_type or "").strip()
    if runtime_type == "systemd":
        return "systemctl list-units --type=service --all --no-pager --plain"
    if runtime_type == "docker":
        return "docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'"
    if runtime_type == "docker_compose":
        return (
            "docker ps -a --filter label=com.docker.compose.service "
            "--format '{{.Label \"com.docker.compose.project\"}}\t"
            "{{.Label \"com.docker.compose.project.working_dir\"}}\t"
            "{{.Label \"com.docker.compose.project.config_files\"}}\t"
            "{{.Label \"com.docker.compose.service\"}}\t{{.Names}}\t{{.Status}}'"
        )
    if runtime_type == "supervisor":
        return "supervisorctl status"
    raise HTTPException(status_code=400, detail="扫描类型仅支持 systemd/docker/docker_compose/supervisor")


def runtime_status_from_text(text: str) -> str:
    """从运行时状态文本中归一化平台状态。"""
    value = str(text or "").lower()
    if any(token in value for token in ["inactive", "failed", "dead", "exited", "created", "paused", "stopped", "fatal", "backoff"]):
        return "offline"
    if any(token in value for token in ["active", "running", "up "]):
        return "active"
    return "offline"


def service_name_from_unit(unit_name: str) -> str:
    """从 systemd unit 名称生成候选展示名。"""
    text = str(unit_name or "").strip()
    if text.endswith(".service"):
        text = text[:-8]
    return text or unit_name


SYSTEM_RUNTIME_SERVICE_NAMES = {
    "auditd.service", "chronyd.service", "cron.service", "crond.service", "dbus.service",
    "network.service", "networkmanager.service", "polkit.service", "rsyslog.service",
    "ssh.service", "sshd.service", "systemd-journald.service", "systemd-logind.service",
    "systemd-networkd.service", "systemd-resolved.service", "systemd-timesyncd.service",
    "systemd-udevd.service",
}
SYSTEM_RUNTIME_SERVICE_PREFIXES = (
    "getty@", "serial-getty@", "systemd-", "user@", "session-", "dbus-", "networkd-",
)


def is_system_runtime_service(unit_name: str) -> bool:
    """识别默认不向业务应用推荐纳管的基础 Systemd 服务。"""
    value = str(unit_name or "").strip().lower()
    return value in SYSTEM_RUNTIME_SERVICE_NAMES or value.startswith(SYSTEM_RUNTIME_SERVICE_PREFIXES)


def parse_systemd_discovery_output(stdout: str) -> list[dict[str, str]]:
    """解析 systemd 服务扫描输出。"""
    candidates: list[dict[str, str]] = []
    for line in str(stdout or "").splitlines():
        parts = line.strip().split(maxsplit=4)
        if len(parts) < 4:
            continue
        unit_name = parts[0]
        if not unit_name.endswith(".service") or unit_name == "UNIT":
            continue
        description = parts[4] if len(parts) >= 5 else ""
        candidates.append(
            {
                "deployment_type": "systemd",
                "name": service_name_from_unit(unit_name),
                "control_ref": unit_name,
                "status": runtime_status_from_text(f"{parts[2]} {parts[3]}"),
                "description": description,
            }
        )
    return candidates


def parse_docker_discovery_output(stdout: str) -> list[dict[str, str]]:
    """解析 Docker 容器扫描输出。"""
    candidates: list[dict[str, str]] = []
    for line in str(stdout or "").splitlines():
        parts = line.rstrip().split("\t")
        if len(parts) < 3:
            continue
        name, image, status_text = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not name:
            continue
        candidates.append(
            {
                "deployment_type": "docker",
                "name": name,
                "control_ref": name,
                "status": runtime_status_from_text(status_text),
                "description": image,
            }
        )
    return candidates


def compose_file_path(working_dir: str, config_file: str) -> str:
    """根据 Docker Compose label 生成 compose 文件路径。"""
    config = str(config_file or "").split(",")[0].strip()
    workdir = str(working_dir or "").strip()
    if not config:
        return ""
    if config.startswith("/"):
        return config
    return f"{workdir.rstrip('/')}/{config.lstrip('/')}" if workdir else config


def parse_compose_discovery_output(stdout: str) -> list[dict[str, str]]:
    """解析 Docker Compose 容器 label 扫描输出。"""
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in str(stdout or "").splitlines():
        parts = line.rstrip().split("\t")
        if len(parts) < 6:
            continue
        project_name, working_dir, config_file, service, container_name, status_text = [item.strip() for item in parts[:6]]
        compose_file = compose_file_path(working_dir, config_file)
        if not compose_file or not service:
            continue
        control_ref = f"{compose_file}:{service}"
        identity = f"{project_name}:{control_ref}"
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(
            {
                "deployment_type": "docker_compose",
                "name": f"{project_name} / {service}" if project_name else service,
                "control_ref": control_ref,
                "status": runtime_status_from_text(status_text),
                "description": f"Compose 项目：{project_name}；容器：{container_name}" if project_name else container_name,
                "project_name": project_name,
            }
        )
    return candidates


def parse_supervisor_discovery_output(stdout: str) -> list[dict[str, str]]:
    """解析 supervisorctl status 输出。"""
    candidates: list[dict[str, str]] = []
    for line in str(stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split(None, 2)
        if len(parts) < 2:
            continue
        program, state = parts[0], parts[1].upper()
        if not SUPERVISOR_PROGRAM_PATTERN.fullmatch(program):
            continue
        candidates.append({
            "deployment_type": "supervisor",
            "name": program,
            "control_ref": program,
            "status": "active" if state == "RUNNING" else "offline",
            "description": text,
        })
    return candidates


def parse_runtime_discovery_output(discovery_type: str, stdout: str) -> list[dict[str, str]]:
    """按扫描类型解析运行实例候选。"""
    runtime_type = str(discovery_type or "").strip()
    if runtime_type == "systemd":
        return parse_systemd_discovery_output(stdout)
    if runtime_type == "docker":
        return parse_docker_discovery_output(stdout)
    if runtime_type == "docker_compose":
        return parse_compose_discovery_output(stdout)
    if runtime_type == "supervisor":
        return parse_supervisor_discovery_output(stdout)
    raise HTTPException(status_code=400, detail="扫描类型仅支持 systemd/docker/docker_compose/supervisor")
