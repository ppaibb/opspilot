"""告警规则归一化与持久化。

【自建告警引擎架构】
 告警评估由内置引擎(app/services/alert_engine/)常驻完成:每个周期直接从
 system_configs(RULES_CONFIG_KEY)读取规则、查询 VictoriaMetrics、做 firing/resolved
 状态机判定并经 dispatch_active_alerts 派发。规则数据改动后引擎下一周期(30s)即生效,
 无需再渲染规则文件或重载外部进程。

核心职责:
1. 规则归一化:前端可能漏传 id、severity,后端必须补全;
2. 规则持久化:写入 system_configs;
3. 单条规则更新时间戳:内容比对,仅变更/新增的规则刷新 updated_at。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.system_config import SystemConfig
from app.api.monitoring.constants import (
    ALERT_RULE_TEMPLATES,
    ALERT_SEVERITIES,
    AUTH_TYPES,
    CONFIG_KEY,
    DEFAULT_ALERT_RULES,
    DEFAULT_TIMEOUT_SECONDS,
    MONITORING_COMPOSE_FILE,
    MONITORING_DEPLOY_DIR,
    NOTIFICATIONS_CONFIG_KEY,
    RELOADABLE_MONITORING_SERVICES,
    RULES_CONFIG_KEY,
    VMAGENT_LEGACY_MARKER,
)
from app.api.monitoring.schemas import AlertRuleItem
from app.api.monitoring.services.config_store import (
    _normalize_config_item_id,
    _read_system_config_row,
    _safe_load_json_dict,
    _save_system_config_json,
)
from app.api.monitoring.services.notifications import (
    read_monitoring_notifications,
)


def _run_local_command(args: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
    """执行固定的监控套件本地命令，返回结构化结果供页面展示。"""
    try:
        completed = subprocess.run(
            args,
            cwd=str(MONITORING_DEPLOY_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "code": completed.returncode,
            "stdout": (completed.stdout or "")[-6000:],
            "stderr": (completed.stderr or "")[-6000:],
        }
    except FileNotFoundError as exc:
        return {"ok": False, "code": 127, "stdout": "", "stderr": f"命令不存在: {exc.filename}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "stdout": "", "stderr": f"命令执行超过 {timeout_seconds} 秒"}


def _compose_args(*parts: str) -> list[str]:
    """生成受控 docker compose 命令参数，compose 文件路径不接受外部输入。"""
    return ["docker", "compose", "-f", str(MONITORING_COMPOSE_FILE), *parts]


def _normalize_monitoring_rules(raw_rules: list[dict[str, Any]] | Any, channel_ids: set[str]) -> list[dict[str, Any]]:
    """
    规整告警规则配置。

    处理原则：
    - 只保留结构合法的规则；
    - channel_ids 必须引用当前存在的通知渠道；
    - 名称、表达式、摘要等字段都做长度限制。
    """
    if not isinstance(raw_rules, list):
        raw_rules = DEFAULT_ALERT_RULES

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            continue
        rule_id = _normalize_config_item_id(str(item.get("id") or ""), "rule", index)
        if rule_id in seen_ids:
            rule_id = _normalize_config_item_id(f"{rule_id}-{index + 1}", "rule", index)
        seen_ids.add(rule_id)

        severity = str(item.get("severity") or "warning").strip().lower()
        if severity not in ALERT_SEVERITIES:
            severity = "warning"
        # 契约：name/duration；同时兼容旧别名 alert/for（Pydantic 已映射，这里再兜一层裸 dict）
        duration = (str(item.get("duration") or item.get("for") or "5m").strip() or "5m")[:32]
        expr = str(item.get("expr") or "").strip()
        if not expr:
            continue
        bound_channel_ids = []
        for channel_id in item.get("channel_ids") or []:
            safe_channel_id = str(channel_id or "").strip()
            if safe_channel_id and safe_channel_id in channel_ids and safe_channel_id not in bound_channel_ids:
                bound_channel_ids.append(safe_channel_id)

        recovery_debounce_seconds = int(item.get("recovery_debounce_seconds") if item.get("recovery_debounce_seconds") is not None else 60)
        recovery_debounce_seconds = min(max(recovery_debounce_seconds, 0), 3600)

        reminder_interval_minutes = int(item.get("reminder_interval_minutes") if item.get("reminder_interval_minutes") is not None else 30)
        reminder_interval_minutes = min(max(reminder_interval_minutes, 1), 1440)

        max_reminders_cap = int(item.get("max_reminders_cap") if item.get("max_reminders_cap") is not None else 0)
        max_reminders_cap = min(max(max_reminders_cap, 0), 100)

        # 复杂逻辑中文注释：
        # 从规则配置中提取绑定的主机（资产）ID 列表，归一化并去重。
        # 这些 ID 对应 assets 表中的 instance_id，后续用于提取主机 IP 进行 PromQL 过滤。
        host_ids = item.get("host_ids") or []
        if not isinstance(host_ids, list):
            host_ids = []
        safe_host_ids = [str(hid).strip() for hid in host_ids if hid]

        # 复杂逻辑中文注释：
        # group 为分组标签字段（资源分组名称的字符串快照），仅用于规则归类与筛选，
        # 不与资源成员表做强关联，避免改名/删组级联影响规则。空串表示「未分组」。
        rule_group = str(item.get("group") or "").strip()[:60]

        # 复杂逻辑中文注释：
        # im_user_ids 为规则的飞书 @ 通知人列表（平台用户 ID）。只接受正整数、
        # 去重、限量（20）；用户是否存在、是否已绑定飞书，在派发时按绑定关系
        # 过滤——保存规则时不做用户表查询，避免规则模块耦合用户模块。
        raw_im_users = item.get("im_user_ids") or []
        if not isinstance(raw_im_users, list):
            raw_im_users = []
        im_user_ids: list[int] = []
        for raw_uid in raw_im_users:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid > 0 and uid not in im_user_ids:
                im_user_ids.append(uid)
            if len(im_user_ids) >= 20:
                break

        normalized.append(
            {
                "id": rule_id,
                "name": (str(item.get("name") or item.get("alert") or "").strip() or f"OpsPilotRule{index + 1}")[:80],
                "expr": expr[:600],
                "duration": duration,
                "severity": severity,
                "enabled": bool(item.get("enabled", True)),
                "channel_ids": bound_channel_ids,
                "summary": (str(item.get("summary") or "").strip() or "OpsPilot 告警")[:120],
                "description": (str(item.get("description") or "").strip() or "请登录 OpsPilot 查看告警详情")[:300],
                "recovery_debounce_seconds": recovery_debounce_seconds,
                "reminder_interval_minutes": reminder_interval_minutes,
                "max_reminders_cap": max_reminders_cap,
                "host_ids": safe_host_ids,
                "group": rule_group,
                "im_user_ids": im_user_ids,
                "updated_at": str(item.get("updated_at") or "").strip(),
            }
        )
    return normalized


def stamp_rule_updated_at(db: Session, normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为每条规则盖"更新时间"戳。

    复杂逻辑中文注释：
    - 单条规则没有独立的更新时间，这里通过与库中旧规则做"内容比对"来推断：
      只有新增的规则、或内容相对旧值发生变化的规则，才盖上当前时间戳；
      未改动的规则保留其原有 updated_at，避免每次保存把所有规则时间都刷新。
    - 比对时忽略 updated_at 字段本身，只比对实际配置内容。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old_by_id: dict[str, dict[str, Any]] = {}
    row = _read_system_config_row(db, RULES_CONFIG_KEY)
    if row:
        payload = _safe_load_json_dict(row.config_value)
        for r in payload.get("rules") or []:
            if isinstance(r, dict) and r.get("id"):
                old_by_id[str(r["id"])] = r

    def _content(rule: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in rule.items() if k != "updated_at"}

    for rule in normalized:
        old = old_by_id.get(str(rule.get("id") or ""))
        if old is not None and _content(old) == _content(rule):
            rule["updated_at"] = str(old.get("updated_at") or "") or now
        else:
            rule["updated_at"] = now
    return normalized


def read_monitoring_rules(db: Session) -> tuple[list[dict[str, Any]], str]:
    """读取告警规则配置。"""
    channels, _ = read_monitoring_notifications(db)
    channel_ids = {str(item.get("id") or "") for item in channels}
    row = _read_system_config_row(db, RULES_CONFIG_KEY)
    if not row:
        return _normalize_monitoring_rules(DEFAULT_ALERT_RULES, channel_ids), ""
    
    payload = _safe_load_json_dict(row.config_value)
    rules = payload.get("rules") or []
    
    # 复杂逻辑中文注释：
    # 自动纠正历史上由于测试或误配置导致的 OpsPilotTargetDown 规则中 up == 1 的异常表达式。
    # 如果表达式是 up == 1，只要主机存活（up=1）就会不断报警，这是不合理的，故强制还原为正常的 up == 0。
    need_save = False
    for r in rules:
        if isinstance(r, dict) and r.get("name") == "OpsPilotTargetDown" and r.get("expr") == "up == 1":
            r["expr"] = "up == 0"
            need_save = True
            
    if need_save:
        try:
            content = json.dumps({"rules": rules}, ensure_ascii=False)
            row.config_value = content
            row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
            # 规则改动后由内置告警引擎下一周期自动读取生效,无需重写文件/重载外部进程
        except Exception:
            db.rollback()
            
    return _normalize_monitoring_rules(rules, channel_ids), str(row.updated_at or "")



# 安全重载 Docker Compose 中的监控服务。
# 使用 docker compose ps 先判断容器是否处于运行状态，只对运行中的服务发 restart，
# 避免未启动时调用 restart 产生误导性错误日志。
def _restart_compose_service_if_running(service_name: str) -> dict[str, Any]:
    """仅在目标服务已运行时执行固定重启。"""
    if service_name not in RELOADABLE_MONITORING_SERVICES:
        return {"attempted": False, "ok": False, "message": "目标服务不在允许列表"}
    if not MONITORING_COMPOSE_FILE.exists():
        return {"attempted": False, "ok": False, "message": "监控套件 compose 文件不存在"}

    ps_result = _run_local_command(_compose_args("ps", "--status", "running", service_name), 15)
    if not ps_result["ok"]:
        return {
            "attempted": False,
            "ok": False,
            "message": str(ps_result["stderr"] or ps_result["stdout"] or "无法检测服务运行状态").strip(),
        }
    if service_name not in str(ps_result["stdout"] or ""):
        return {"attempted": False, "ok": False, "message": f"{service_name} 当前未运行，配置已写入，待套件启动后生效"}

    restart_result = _run_local_command(_compose_args("restart", service_name), 60)
    return {
        "attempted": True,
        "ok": bool(restart_result["ok"]),
        "message": f"{service_name} 已按最新配置重启" if restart_result["ok"] else str(restart_result["stderr"] or restart_result["stdout"] or f"{service_name} 重启失败").strip(),
        "stdout": str(restart_result["stdout"] or ""),
        "stderr": str(restart_result["stderr"] or ""),
    }


def _yaml_quote(value: Any) -> str:
    """把字符串安全写入 YAML/HCL 标量。

    通用辅助函数(供 vmagent Alloy 配置、Vector 日志目标等渲染使用):
    使用单引号包裹,YAML 单引号内唯一需要转义的是单引号本身('' 表示一个 ')。
    这样字符串里的双引号不会被破坏。
    """
    s = "" if value is None else str(value)
    escaped = s.replace("'", "''")
    return f"'{escaped}'"


def _persist_rules_and_sync_alerting(db: Session, rules: list[dict[str, Any]]) -> dict[str, Any]:
    """保存规则到 system_configs。

    【自建告警引擎架构】规则数据写入数据库后,内置告警引擎(app/services/alert_engine/)
    下一评估周期(默认 30s)会自动读取并生效——主机绑定的 instance 过滤由引擎的
    apply_host_filter 在评估时即时完成,无需再渲染规则文件或重载外部进程。
    """
    try:
        updated_at = _save_system_config_json(db, RULES_CONFIG_KEY, {"rules": rules})
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"保存告警规则配置失败: {str(exc)}")
    # 兼容旧返回结构:前端读取 updated_at;sync_info 字段保留但标注新架构无需文件同步
    return {
        "updated_at": updated_at,
        "engine": "builtin",
        "message": "规则已保存,内置告警引擎将在下一周期(约30秒)自动生效",
    }


def apply_rule_templates(
    db: Session,
    template_ids: list[str],
) -> dict[str, Any]:
    """
    从内置模板库批量创建告警规则。

    设计说明：
    1. 模板规则默认 enabled=False，避免批量创建后立刻触发告警风暴；
    2. 若目标 ID 已存在于当前规则中，则跳过（防止重复）；
    3. channel_ids 继承空列表，避免未配置通知渠道时产生无效告警噪音；
    4. 创建后立即同步 alerts.yml 与 alertmanager.yml。
    """
    if not template_ids:
        raise HTTPException(status_code=400, detail="请选择至少一条规则模板")

    existing_rules, _ = read_monitoring_rules(db)
    existing_ids = {str(item.get("id") or "") for item in existing_rules}
    template_map = {str(tpl.get("id") or ""): tpl for tpl in ALERT_RULE_TEMPLATES}

    added: list[dict[str, Any]] = []
    skipped: list[str] = []
    for tid in template_ids:
        tid = str(tid or "").strip()
        tpl = template_map.get(tid)
        if not tpl:
            continue
        if tid in existing_ids:
            skipped.append(tid)
            continue
        new_rule = {
            "id": tid,
            "name": str(tpl.get("name") or "").strip(),
            "expr": str(tpl.get("expr") or "").strip(),
            "duration": str(tpl.get("duration") or "5m").strip()[:32],
            "severity": str(tpl.get("severity") or "warning").strip().lower(),
            "enabled": False,
            "channel_ids": [],
            "summary": str(tpl.get("summary") or "").strip()[:120],
            "description": str(tpl.get("description") or "").strip()[:300],
        }
        existing_rules.append(new_rule)
        added.append(new_rule)

    if not added:
        raise HTTPException(status_code=400, detail="所选模板均已存在，未新增任何规则")

    sync_info = _persist_rules_and_sync_alerting(db, existing_rules)
    return {
        "added_count": len(added),
        "skipped_count": len(skipped),
        "added_rules": added,
        "skipped_ids": skipped,
        **sync_info,
    }
