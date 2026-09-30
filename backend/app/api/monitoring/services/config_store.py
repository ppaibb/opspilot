"""监控配置持久化读写（SystemConfig 表封装）。

【为什么集中管理】
 monitoring 模块涉及 5 套配置（VM 连接、targets、log_targets、rules、notifications），
 全部以 JSON 形式落在 system_configs 表的 config_value 字段中。集中封装的好处：
1. 统一脏数据防护：所有读取路径都经过 _safe_load_json_dict，避免前端传入脏 JSON 导致后端崩溃；
2. 统一更新时间戳：写入时自动补 now_ts，保证前端"最后更新时间"始终准确；
3. 降低 SQLAlchemy 样板代码：后续新增第 6 套配置时，只需调用 _save_system_config_json。
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.system_config import SystemConfig
from app.api.monitoring.constants import (
    CONFIG_KEY,
    LOG_TARGETS_CONFIG_KEY,
    TARGETS_CONFIG_KEY,
)
from app.api.assets import now_ts

# 安全解析 JSON 配置，脏数据统一回退为空字典。
# 为什么用 blanket except 而不是 json.JSONDecodeError？
# 因为生产环境曾遇到过非字符串类型（如 None、int）直接落入 config_value，
# 若只捕获 JSONDecodeError 会导致 500。空字典回退是最安全的兜底策略。
def _safe_load_json_dict(raw_value: str | None) -> dict[str, Any]:
    """安全解析 JSON 配置，脏数据统一回退为空字典。"""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_monitoring_config(db: Session) -> dict[str, Any]:
    """读取 VictoriaMetrics 连接配置。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == CONFIG_KEY).first()
    if not row:
        return {}
    return _safe_load_json_dict(row.config_value)


def read_monitoring_targets(db: Session) -> dict[str, Any]:
    """读取 OpsPilot 维护的 vmagent target 清单。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == TARGETS_CONFIG_KEY).first()
    if not row:
        return {"targets": []}
    payload = _safe_load_json_dict(row.config_value)
    targets = payload.get("targets")
    return {"targets": targets if isinstance(targets, list) else []}


def save_monitoring_targets(db: Session, targets: list[dict[str, Any]]) -> None:
    """保存 target 清单。"""
    content = json.dumps({"targets": targets}, ensure_ascii=False)
    row = db.query(SystemConfig).filter(SystemConfig.config_key == TARGETS_CONFIG_KEY).first()
    updated_at = now_ts()
    if not row:
        row = SystemConfig(config_key=TARGETS_CONFIG_KEY, config_value=content, updated_at=updated_at)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = updated_at


def read_log_targets(db: Session) -> dict[str, Any]:
    """读取 OpsPilot 维护的日志采集主机清单。"""
    row = db.query(SystemConfig).filter(SystemConfig.config_key == LOG_TARGETS_CONFIG_KEY).first()
    if not row:
        return {"targets": []}
    payload = _safe_load_json_dict(row.config_value)
    targets = payload.get("targets")
    return {"targets": targets if isinstance(targets, list) else []}


def save_log_targets(db: Session, targets: list[dict[str, Any]]) -> None:
    """保存日志采集主机清单。"""
    content = json.dumps({"targets": targets}, ensure_ascii=False)
    row = db.query(SystemConfig).filter(SystemConfig.config_key == LOG_TARGETS_CONFIG_KEY).first()
    updated_at = now_ts()
    if not row:
        row = SystemConfig(config_key=LOG_TARGETS_CONFIG_KEY, config_value=content, updated_at=updated_at)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = updated_at


def _read_system_config_row(db: Session, config_key: str) -> SystemConfig | None:
    """按键读取系统配置行。"""
    return db.query(SystemConfig).filter(SystemConfig.config_key == config_key).first()


def _save_system_config_json(db: Session, config_key: str, payload: dict[str, Any]) -> str:
    """保存 JSON 结构配置并返回更新时间。"""
    content = json.dumps(payload, ensure_ascii=False)
    row = _read_system_config_row(db, config_key)
    updated_at = now_ts()
    if not row:
        row = SystemConfig(config_key=config_key, config_value=content, updated_at=updated_at)
        db.add(row)
    else:
        row.config_value = content
        row.updated_at = updated_at
    return updated_at


# 将前端传入的 ID 规整为安全短标识。
# 规则：只允许小写字母、数字、短横线；非法字符替换为短横线；空结果回退为 fallback_prefix-index。
# 为什么限制 64 字符？Alertmanager receiver 名称、Alloy 组件标签、文件名都对长度敏感，
# 过长会导致解析异常。
def _normalize_config_item_id(raw_value: str, fallback_prefix: str, index: int) -> str:
    """将前端传入的 ID 规整为安全短标识。"""
    normalized = re.sub(r"[^a-z0-9-]+", "-", (raw_value or "").strip().lower()).strip("-")
    if not normalized:
        normalized = f"{fallback_prefix}-{index + 1}"
    return normalized[:64]


