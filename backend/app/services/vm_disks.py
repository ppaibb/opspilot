"""虚拟机磁盘明细解析与序列化服务。"""
from __future__ import annotations

from typing import Any

import json
import re

PVE_DISK_KEY_PATTERN = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")
PVE_DISK_SIZE_PATTERN = re.compile(r"(?:^|,)size=([0-9.]+)([KMGTP]?)", re.IGNORECASE)


def format_vm_disk_label(position: int) -> str:
    """按磁盘顺序生成展示标签。"""
    if position <= 0:
        return "系统盘"
    return f"数据盘 {position}"


def parse_pve_disk_size_gb(value: Any) -> float:
    """从 PVE 磁盘配置字符串中解析容量，统一转为 GB。"""
    match = PVE_DISK_SIZE_PATTERN.search(str(value or ""))
    if not match:
        return 0.0
    try:
        number = float(match.group(1))
    except Exception:
        return 0.0
    unit = (match.group(2) or "G").upper()
    multiplier = {
        "K": 1 / 1024 / 1024,
        "M": 1 / 1024,
        "G": 1,
        "T": 1024,
        "P": 1024 * 1024,
    }.get(unit, 1)
    return round(number * multiplier, 1)


def parse_pve_config_disks(config: dict[str, Any]) -> list[dict[str, Any]]:
    """从 PVE VM config 中提取磁盘明细。

    只返回前端展示需要的控制器键名和容量，不暴露存储卷路径、volume id
    或完整 PVE 配置，避免把底层存储结构带到资产列表。
    """
    candidates: list[tuple[str, int, float]] = []
    for key, value in (config or {}).items():
        key_text = str(key or "").strip()
        match = PVE_DISK_KEY_PATTERN.match(key_text)
        if not match:
            continue
        value_text = str(value or "").lower()
        if "media=cdrom" in value_text:
            continue
        size_gb = parse_pve_disk_size_gb(value)
        if size_gb <= 0:
            continue
        index = int(match.group(2))
        candidates.append((key_text, index, size_gb))

    candidates.sort(key=lambda item: (item[1], item[0]))
    return [
        {"key": key, "label": format_vm_disk_label(position), "size_gb": size_gb}
        for position, (key, _, size_gb) in enumerate(candidates)
    ]


def fallback_vm_disks(system_disk_gb: float) -> list[dict[str, Any]]:
    """旧数据或同步失败时，基于系统盘字段提供兼容磁盘列表。"""
    try:
        size_gb = float(system_disk_gb or 0.0)
    except Exception:
        size_gb = 0.0
    if size_gb <= 0:
        return []
    return [{"key": "system", "label": "系统盘", "size_gb": round(size_gb, 1)}]


def normalize_vm_disks(disks: Any, system_disk_gb: float = 0.0) -> list[dict[str, Any]]:
    """清洗磁盘明细，保证接口只返回稳定、非敏感字段。"""
    result: list[dict[str, Any]] = []
    if isinstance(disks, list):
        for position, item in enumerate(disks):
            if not isinstance(item, dict):
                continue
            try:
                size_gb = float(item.get("size_gb") or item.get("sizeGb") or 0.0)
            except Exception:
                size_gb = 0.0
            if size_gb <= 0:
                continue
            key = str(item.get("key") or f"disk{position}").strip()[:32]
            label = str(item.get("label") or format_vm_disk_label(position)).strip()[:40]
            result.append({"key": key, "label": label, "size_gb": round(size_gb, 1)})
    return result or fallback_vm_disks(system_disk_gb)


def serialize_vm_disks(disks: Any, system_disk_gb: float = 0.0) -> str:
    """序列化磁盘明细，便于跨 SQLite/PostgreSQL 保存。"""
    return json.dumps(normalize_vm_disks(disks, system_disk_gb), ensure_ascii=False)


def parse_vm_disks_json(raw: str | None, system_disk_gb: float = 0.0) -> list[dict[str, Any]]:
    """从数据库 JSON 字符串中解析磁盘明细。"""
    try:
        payload = json.loads(raw or "[]")
    except Exception:
        payload = []
    return normalize_vm_disks(payload, system_disk_gb)
