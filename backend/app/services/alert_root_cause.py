"""告警根因智能分析服务（塔斯汀闭环模式的核心引擎）。

职责：
1. 构建分析提示词：告警上下文 + **同类型告警的历史人工根因**（经验沉淀反哺 AI）；
2. 调用平台 LLM（复用 ai_agent 的运行时配置与三家 provider 生成函数，懒导入避免环）；
3. LLM 不可用时返回结构化降级诊断（明确标注 mode=fallback，绝不返回编造内容）；
4. 反馈与统计：人工根因落库、按 alert_name 的 AI 准确率。

对比旧 /analyze（Mock）：本服务输出要求 LLM 给 JSON（root_cause/impact/suggestions），
并把历史根因作为 few-shot 注入——这正是"AI 越用越贴业务"的机制。
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.root_cause import AlertRootCause
from app.services.application_time import format_app_datetime, now_dt

ANALYSIS_SYSTEM_PROMPT = (
    "你是资深 SRE，负责对监控告警做根因分析。基于给定上下文给出判断："
    "如果上下文不足，明确说'上下文不足'而不是编造。"
    "严格输出 JSON（不要 markdown 代码块）："
    '{"root_cause": "...", "impact": "...", "suggestions": [{"title": "...", "content": "..."}]}'
)


def find_history_root_causes(db: Session, alert_name: str, limit: int = 5) -> list[dict[str, Any]]:
    """检索同类告警的人工根因（只取有人工反馈的记录，作为 few-shot 经验）。"""
    rows = (
        db.query(AlertRootCause)
        .filter(
            AlertRootCause.alert_name == alert_name,
            AlertRootCause.human_root_cause != "",
        )
        .order_by(AlertRootCause.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "time": r.created_at or "",
            "instance": r.instance or "",
            "human_root_cause": r.human_root_cause,
            "human_resolution": r.human_resolution or "",
        }
        for r in rows
    ]


def build_analysis_prompt(alert: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """分析提示词：告警上下文 + 历史人工根因（经验注入）。"""
    return (
        "分析以下监控告警的根因：\n"
        f"上下文(JSON)：\n{json.dumps(alert, ensure_ascii=False, indent=2)}\n\n"
        + (
            f"该类告警的历史人工根因（参考经验，共 {len(history)} 条）：\n"
            f"{json.dumps(history, ensure_ascii=False, indent=2)}\n\n"
            "如历史根因与本次相似请优先参考并说明。\n"
            if history else "该类告警暂无历史人工根因。\n"
        )
        + "输出 root_cause（根因判断）/ impact（影响面）/ suggestions（3 条内可执行步骤）。"
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块包裹）。"""
    if not text:
        return None
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict) and "root_cause" in parsed:
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def run_ai_analysis(db: Session, alert: dict[str, Any]) -> dict[str, Any]:
    """执行根因分析：LLM 优先，历史根因注入；失败时结构化降级（不 Mock）。

    返回 {root_cause, impact, suggestions, mode, model, history_count}。
    mode: llm=模型诊断 / fallback=降级提示。
    """
    history = find_history_root_causes(db, str(alert.get("alert_name") or ""))
    prompt = build_analysis_prompt(alert, history)

    try:
        # 懒导入 ai_agent：其模块体积大且与告警模块存在相互引用风险。
        from app.api.ai_agent import (
            _generate_gemini_content,
            _generate_openai_compatible_content,
            _generate_anthropic_content,
            _read_llm_runtime_config,
        )

        config, _ = _read_llm_runtime_config(db)
        if not (config.get("enabled") and config.get("has_api_key")):
            raise RuntimeError("未配置可用的 LLM 模型（模型配置页）")
        provider = str(config.get("provider") or "gemini")
        model = str(config.get("model") or "")
        api_key = str(config.get("api_key") or "")

        common = dict(temperature=0.2, max_output_tokens=1500)
        if provider == "gemini":
            text = _generate_gemini_content(api_key, model, prompt, ANALYSIS_SYSTEM_PROMPT, **common)
        elif provider == "openai_compatible":
            text = _generate_openai_compatible_content(
                api_key, str(config.get("base_url") or ""), model, prompt,
                ANALYSIS_SYSTEM_PROMPT, timeout_seconds=int(config.get("timeout_seconds") or 30), **common,
            )
        else:
            text = _generate_anthropic_content(api_key, model, prompt, ANALYSIS_SYSTEM_PROMPT, **common)

        parsed = _extract_json(text)
        if parsed:
            return {
                **parsed,
                "suggestions": parsed.get("suggestions") or [],
                "mode": "llm",
                "model": model,
                "history_count": len(history),
            }
        raise RuntimeError("模型输出无法解析为 JSON")
    except Exception as exc:  # noqa: BLE001
        # 降级：明确告知不可用 + 基于历史根因的可用经验，绝不编造诊断。
        fallback = {
            "root_cause": f"AI 诊断不可用（{str(exc)[:120]}）。"
            + (f"但该类告警有 {len(history)} 条历史人工根因可参考。" if history else "且暂无历史根因可参考。"),
            "impact": "请结合下方历史根因与告警标签人工判断。",
            "suggestions": [
                {"title": "参考历史根因", "content": json.dumps(history, ensure_ascii=False)[:600]} if history
                else {"title": "配置 LLM", "content": "前往「AI 平台 → 模型配置」启用模型后重试 AI 诊断。"},
            ],
            "mode": "fallback",
            "model": "",
            "history_count": len(history),
        }
        return fallback


def save_analysis_draft(
    db: Session, alert: dict[str, Any], diagnosis: dict[str, Any], username: str,
) -> AlertRootCause:
    """AI 分析后立即落一条 pending 记录（人工反馈在同一记录上补齐）。"""
    record = AlertRootCause(
        fingerprint=str(alert.get("fingerprint") or "")[:120],
        alert_name=str(alert.get("alert_name") or "")[:200],
        service=str(alert.get("service") or alert.get("labels", {}).get("service") or "")[:200],
        instance=str(alert.get("instance") or "")[:200],
        severity=str(alert.get("severity") or ""),
        context_snapshot=alert,
        ai_diagnosis=diagnosis,
        ai_model=str(diagnosis.get("model") or ""),
        ai_rating="pending",
        username=username,
        created_at=format_app_datetime(now_dt()),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def accuracy_stats(db: Session) -> dict[str, Any]:
    """按 alert_name 统计 AI 准确率（仅统计已评价记录）。"""
    rows = db.query(AlertRootCause).filter(AlertRootCause.ai_rating != "pending").all()
    by_alert: dict[str, dict[str, int]] = {}
    total = {"accurate": 0, "partial": 0, "inaccurate": 0}
    for row in rows:
        name = row.alert_name or "(unknown)"
        bucket = by_alert.setdefault(name, {"accurate": 0, "partial": 0, "inaccurate": 0, "total": 0})
        bucket[row.ai_rating] = bucket.get(row.ai_rating, 0) + 1
        bucket["total"] += 1
        total[row.ai_rating] += 1
    rated = sum(total.values())
    items = [
        {
            "alert_name": name,
            **counts,
            "accuracy": round((counts["accurate"] + 0.5 * counts["partial"]) / counts["total"], 4),
        }
        for name, counts in sorted(by_alert.items(), key=lambda kv: -kv[1]["total"])
    ]
    return {
        "total_rated": rated,
        "total_accurate": total["accurate"],
        "overall_accuracy": round((total["accurate"] + 0.5 * total["partial"]) / rated, 4) if rated else None,
        "by_alert": items[:20],
    }
