import hashlib
import json
import logging
import re
from datetime import datetime
import requests
from sqlalchemy.orm import Session
from app.api.monitoring.services.config_store import read_monitoring_config
from app.api.monitoring.services.rules import read_monitoring_rules
from app.core.crypto import decrypt_secret
from app.models.asset import Asset
from app.models.monitoring_shadow_alert import MonitoringShadowAlert

logger = logging.getLogger(__name__)

# 匹配 Prometheus/vmalert 注解模板里的 {{ $labels.xxx }} 与 {{ $value }}
_LABEL_TPL_RE = re.compile(r"\{\{\s*\$labels\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_VALUE_TPL_RE = re.compile(r"\{\{\s*\$value\s*\}\}")


def render_annotation(template: str, labels: dict, value: str) -> str:
    """渲染 vmalert 风格的注解模板。

    替代旧 vmalert 的注解渲染:把 {{ $labels.xxx }} 换成对应 label 值,
    {{ $value }} 换成当前样本值。无法识别的模板片段原样保留(不报错)。
    """
    if not template:
        return ""
    text = _LABEL_TPL_RE.sub(lambda m: str(labels.get(m.group(1), m.group(0))), template)
    text = _VALUE_TPL_RE.sub(lambda _: str(value), text)
    return text


def parse_duration(s: str) -> int:
    """
    解析 duration 字符串为秒数。非法值回退 300。
    """
    if not s:
        return 300
    s = str(s).strip().lower()
    if not s:
        return 300
    try:
        if s.endswith("s"):
            return int(s[:-1])
        elif s.endswith("m"):
            return int(s[:-1]) * 60
        elif s.endswith("h"):
            return int(s[:-1]) * 3600
        elif s.endswith("d"):
            return int(s[:-1]) * 86400
        else:
            return int(s)
    except ValueError:
        return 300

def series_fingerprint(rule_id: str, labels: dict) -> str:
    """
    生成稳定哈希（sha256 取前 64 hex），labels 先按 key 排序。
    """
    sorted_labels = sorted(labels.items(), key=lambda x: x[0])
    label_str = json.dumps(sorted_labels, separators=(",", ":"))
    seed = f"{rule_id}:{label_str}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()

def apply_host_filter(db: Session, host_ids: list[str], expr: str) -> str:
    """
    若 host_ids 非空则把 instance 匹配器拼到表达式上。
    使用 PromQL 的 AND 和 on(instance) 安全追加条件，避免词法分析。
    """
    if not host_ids:
        return expr
    assets = db.query(Asset).filter(Asset.instance_id.in_(host_ids)).all()
    ips = [asset.ip for asset in assets if asset.ip]
    if not ips:
        return expr
    # 正则 matcher 里点号无需转义（. 本就匹配点号），且本环境的 VictoriaMetrics
    # 对 instance=~"a\.b" 这种转义写法会返回 422，故直接用原始 IP，与旧 vmalert 注入路径一致。
    # 后缀用 (:.*)? 而不是 :.* —— Alloy 策略把 instance 打成纯 IP（无端口），
    # 旧写法 ip:.* 反而匹配不到；同时避免 10.0.0.1 前缀误命中 10.0.0.10。
    filter_val = "|".join(f"{ip}(:.*)?" for ip in ips)
    # 用 PromQL 的逻辑 AND 与 on(instance) 拼接
    return f"({expr}) and on(instance) up{{instance=~\"{filter_val}\"}}"

def _normalize_api_base(vm_url: str) -> str:
    """把 vm_url 规范成以 /api/v1 结尾的 base"""
    base = (vm_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"

def _request_kwargs(cfg: dict) -> dict:
    """生成 requests 的 timeout/verify/headers/auth"""
    headers = {}
    kwargs = {
        "timeout": max(3, int(cfg.get("timeout_seconds") or 10)),
        "verify": bool(cfg.get("verify_tls", True)),
        "headers": headers,
    }
    auth_type = str(cfg.get("auth_type") or "none").lower()
    secret = decrypt_secret(str(cfg.get("secret") or ""))
    if auth_type == "basic":
        username = str(cfg.get("username") or "")
        kwargs["auth"] = (username, secret)
    elif auth_type == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    return kwargs

def vm_query(cfg: dict, expr: str) -> list[dict]:
    """发 GET 并返回 JSON，失败抛出普通异常。不要抛 HTTPException"""
    api_base = _normalize_api_base(str(cfg.get("vm_url") or ""))
    if not api_base:
        raise ValueError("VM 查询地址未配置")
    
    url = f"{api_base}/query"
    params = {"query": expr}
    
    resp = requests.get(url, params=params, **_request_kwargs(cfg))
    resp.raise_for_status()
    payload = resp.json()
    
    if payload.get("status") == "error":
        raise ValueError(payload.get("error") or "VM 查询返回错误状态")
        
    return payload.get("data", {}).get("result", [])

def _build_alert_event(rule_id, name, severity, labels, value, summary_tpl, desc_tpl, status, active_at, group=""):
    """构造 dispatch_active_alerts 所需的 Alertmanager 风格告警字典。

    labels 注入 alertname/severity/opspilot_rule_id/group,使 dispatch 能按规则匹配通道并透传分组;
    summary/description 用 vmalert 风格模板渲染。
    """
    out_labels = dict(labels)
    out_labels["alertname"] = name
    out_labels["severity"] = severity
    out_labels["opspilot_rule_id"] = rule_id
    if group:
        out_labels["group"] = group
    if "instance" not in out_labels:
        out_labels["instance"] = labels.get("instance", "")
    starts_at = ""
    if active_at:
        starts_at = active_at.replace(microsecond=0).isoformat() + "Z"
    return {
        "labels": out_labels,
        "annotations": {
            "summary": render_annotation(summary_tpl, labels, value) or name,
            "description": render_annotation(desc_tpl, labels, value),
        },
        "status": status,
        "startsAt": starts_at,
    }


def evaluate_once(db: Session) -> dict:
    """
    执行一轮完整评估,返回统计 dict(含 events 列表供切流派发使用)。

    events 是本轮产生的 Alertmanager 风格告警:
    - 当前处于 firing 的每个序列 → 一条 status=firing(每轮都发,dispatch 自己做去重/重复提醒);
    - 本轮刚转入 resolved 的序列 → 一条 status=resolved(仅转换当轮发一次)。
    shadow_runner 忽略 events(保持只读);live_runner 会把 events 交给 dispatch_active_alerts。
    """
    stats = {"rules": 0, "breaching_series": 0, "pending": 0, "firing": 0, "resolved": 0, "events": []}
    events = stats["events"]
    try:
        cfg = read_monitoring_config(db)
    except Exception as exc:
        logger.error(f"告警引擎:读取监控配置失败: {exc}")
        return stats

    try:
        rules, _ = read_monitoring_rules(db)
    except Exception as exc:
        logger.error(f"告警引擎:读取监控规则失败: {exc}")
        return stats

    # 用 UTC 与项目现有告警模型(monitoring_active_alert 用 datetime.utcnow)及 vmalert 对齐,
    # 避免跨系统对账出现 8 小时时差。全链路(评估/派发/恢复防抖)统一 UTC。
    now = datetime.utcnow()

    for rule in rules:
        if not rule.get("enabled"):
            continue

        stats["rules"] += 1
        rule_id = str(rule.get("id"))
        expr = rule.get("expr")
        duration_sec = parse_duration(rule.get("duration"))
        severity = rule.get("severity") or "warning"
        name = rule.get("name") or "Unnamed"
        summary_tpl = rule.get("summary") or ""
        desc_tpl = rule.get("description") or ""
        rule_group = str(rule.get("group") or "").strip()

        filtered_expr = apply_host_filter(db, rule.get("host_ids") or [], expr)

        try:
            results = vm_query(cfg, filtered_expr)
        except Exception as exc:
            logger.error(f"告警引擎:查询规则 {rule_id} 失败: {exc}")
            continue

        breaching_series = {}
        for r in results:
            labels = r.get("metric", {})
            value_tuple = r.get("value", [])
            value = str(value_tuple[1]) if len(value_tuple) > 1 else ""
            fp = series_fingerprint(rule_id, labels)
            instance = labels.get("instance", "")
            breaching_series[fp] = {"labels": labels, "value": value, "instance": instance}
            stats["breaching_series"] += 1

        # 获取当前 rule_id 的所有历史行
        old_rows = db.query(MonitoringShadowAlert).filter(MonitoringShadowAlert.rule_id == rule_id).all()
        old_rows_by_fp = {row.fingerprint: row for row in old_rows}

        # 处理当前越线的 series
        for fp, data in breaching_series.items():
            labels_json = json.dumps(data["labels"], ensure_ascii=False)
            if fp not in old_rows_by_fp:
                # 当前越线 & 无旧行 → 新建 pending
                new_row = MonitoringShadowAlert(
                    fingerprint=fp,
                    rule_id=rule_id,
                    name=name,
                    instance=data["instance"],
                    severity=severity,
                    state="pending",
                    value=data["value"],
                    labels=labels_json,
                    first_breach_at=now,
                    first_seen_at=now,
                    last_seen_at=now
                )
                db.add(new_row)
                stats["pending"] += 1
            else:
                row = old_rows_by_fp[fp]
                row.last_seen_at = now
                row.value = data["value"]

                if row.state == "pending":
                    # 当前越线 & 旧行 pending → 检查 duration
                    if row.first_breach_at and (now - row.first_breach_at).total_seconds() >= duration_sec:
                        row.state = "firing"
                        row.active_at = now
                        stats["firing"] += 1
                    else:
                        stats["pending"] += 1
                elif row.state == "firing":
                    # 当前越线 & 旧行 firing → 保持
                    stats["firing"] += 1
                elif row.state == "resolved":
                    # 旧行已恢复,但现在再次越线
                    row.state = "pending"
                    row.first_breach_at = now
                    row.resolved_at = None
                    stats["pending"] += 1

            # 收集 firing 事件:当前序列在本轮结束后处于 firing 则发(每轮都发,dispatch 去重)
            cur_row = old_rows_by_fp.get(fp)
            if cur_row is not None and cur_row.state == "firing":
                events.append(_build_alert_event(
                    rule_id, name, severity, data["labels"], data["value"],
                    summary_tpl, desc_tpl, "firing", cur_row.active_at, rule_group,
                ))

        # 处理不再越线的 series
        for row in old_rows:
            if row.fingerprint not in breaching_series:
                if row.state != "resolved":
                    was_firing = row.state == "firing"
                    # 旧行存在但当前不再越线 → resolved
                    row.state = "resolved"
                    row.resolved_at = now
                    stats["resolved"] += 1
                    # 仅对此前已 firing 的序列发恢复事件(pending→消失 无需恢复通知)
                    if was_firing:
                        try:
                            row_labels = json.loads(row.labels) if row.labels else {}
                        except Exception:
                            row_labels = {}
                        events.append(_build_alert_event(
                            rule_id, name, severity, row_labels, row.value or "",
                            summary_tpl, desc_tpl, "resolved", row.active_at, rule_group,
                        ))

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"告警引擎:DB 提交失败: {exc}")

    return stats

