"""原始设备标签、工具判定和人读日志渲染契约。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


def _raw_text(value: Any) -> str:
    """保留原始字面量；复杂值使用 JSON 但不翻译内容。"""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return "<missing>"
    return str(value)


@dataclass(frozen=True)
class RawTag:
    tag_name: str
    raw_value: Any
    source: str = "device"
    port: str | None = None
    role: str | None = None
    cursor: int | None = None
    observed_at: str | None = None
    evidence: str | None = None
    raw_line: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolJudgement:
    tool_status: str
    tool_judgement: str = ""
    tool_reason: str = ""
    expected: Mapping[str, Any] = field(default_factory=dict)
    actual: Mapping[str, Any] = field(default_factory=dict)
    duration_ms: int | None = None
    evidence_refs: Sequence[str] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["expected"] = dict(self.expected)
        result["actual"] = dict(self.actual)
        result["evidence_refs"] = list(self.evidence_refs)
        return result


def render_observation(
    channel: str,
    tags: Sequence[RawTag],
    *,
    normalized: Mapping[str, Any] | None = None,
    judgement: ToolJudgement | None = None,
) -> str:
    """渲染单行人读日志，原始标签和值始终排在工具判断之前。"""
    parts = [f"[{channel}]"]
    parts.extend(f"{tag.tag_name}: {_raw_text(tag.raw_value)}" for tag in tags)
    if normalized:
        parts.extend(f"{name}: {_raw_text(value)}" for name, value in normalized.items())
    if judgement:
        parts.append(f"tool_status: {judgement.tool_status}")
        if judgement.tool_judgement:
            parts.append(f"tool_judgement: {judgement.tool_judgement}")
        if judgement.tool_reason:
            parts.append(f"tool_reason: {judgement.tool_reason}")
        for name, value in judgement.expected.items():
            parts.append(f"expected_{name}: {_raw_text(value)}")
        for name, value in judgement.actual.items():
            parts.append(f"actual_{name}: {_raw_text(value)}")
        if judgement.duration_ms is not None:
            parts.append(f"duration_ms: {judgement.duration_ms}")
        if judgement.evidence_refs:
            parts.append(f"evidence: {','.join(str(item) for item in judgement.evidence_refs)}")
    return " | ".join(parts)


def raw_tags_from_mappings(values: Sequence[Mapping[str, Any] | RawTag]) -> list[RawTag]:
    result: list[RawTag] = []
    for value in values:
        if isinstance(value, RawTag):
            result.append(value)
            continue
        if not isinstance(value, Mapping):
            raise TypeError("raw tag must be RawTag or mapping")
        if not str(value.get("tag_name", "")).strip():
            raise ValueError("raw tag requires tag_name")
        result.append(RawTag(
            tag_name=str(value["tag_name"]),
            raw_value=value.get("raw_value"),
            source=str(value.get("source", "device")),
            port=str(value["port"]) if value.get("port") is not None else None,
            role=str(value["role"]) if value.get("role") is not None else None,
            cursor=int(value["cursor"]) if value.get("cursor") is not None else None,
            observed_at=str(value["observed_at"]) if value.get("observed_at") is not None else None,
            evidence=str(value["evidence"]) if value.get("evidence") is not None else None,
            raw_line=str(value["raw_line"]) if value.get("raw_line") is not None else None,
        ))
    return result


def judge_wakeup(
    actual_keyword: str | None,
    expected_keyword: str | None = None,
    *,
    final_observed: bool = True,
    duration_ms: int | None = None,
    evidence_refs: Sequence[str] = (),
) -> tuple[dict[str, Any], ToolJudgement]:
    """生成唤醒独立判定；候选/预唤醒不算最终通过。"""
    if not final_observed:
        status, reason = "BLOCKED", "final_wakeup_marker_missing"
    elif actual_keyword is None:
        status, reason = "FAIL", "wakeup_keyword_missing"
    elif expected_keyword is not None and actual_keyword != expected_keyword:
        status, reason = "FAIL", "wakeup_keyword_mismatch"
    else:
        status, reason = "PASS", "wakeup_marker_observed"
    normalized = {"wakeup_status": status, "wake_keyword": actual_keyword}
    judgement = ToolJudgement(
        status,
        "EXACT_MATCH" if status == "PASS" and expected_keyword is not None else "OBSERVED",
        reason,
        expected={"wake_keyword": expected_keyword} if expected_keyword is not None else {},
        actual={"wake_keyword": actual_keyword},
        duration_ms=duration_ms,
        evidence_refs=evidence_refs,
    )
    return normalized, judgement


def judge_command(
    actual_keyword: str | None,
    actual_intent: str | None,
    expected_keyword: str | None,
    expected_intent: str | None,
    *,
    duration_ms: int | None = None,
    evidence_refs: Sequence[str] = (),
) -> tuple[dict[str, Any], ToolJudgement]:
    """分别逐字符比较命令词 keyword 和 intent。"""
    missing = [name for name, value in (("keyword", actual_keyword), ("intent", actual_intent)) if value is None]
    mismatched = [
        name for name, actual, expected in (
            ("keyword", actual_keyword, expected_keyword),
            ("intent", actual_intent, expected_intent),
        )
        if expected is not None and actual != expected
    ]
    if missing:
        status, reason = "BLOCKED", f"command_{missing[0]}_missing"
    elif mismatched:
        status, reason = "FAIL", "command_" + "_and_".join(mismatched) + "_mismatch"
    else:
        status, reason = "PASS", "keyword_intent_exact_match"
    normalized = {"command_status": status}
    judgement = ToolJudgement(
        status,
        "EXACT_MATCH" if status == "PASS" else "COMPARE",
        reason,
        expected={"keyword": expected_keyword, "intent": expected_intent},
        actual={"keyword": actual_keyword, "intent": actual_intent},
        duration_ms=duration_ms,
        evidence_refs=evidence_refs,
    )
    return normalized, judgement


def judge_online(
    *,
    correlation_valid: bool,
    actual: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None = None,
    duration_ms: int | None = None,
    evidence_refs: Sequence[str] = (),
) -> tuple[dict[str, Any], ToolJudgement]:
    """生成在线 ID 配对、结果一致性和耗时完整性的工具判定。"""
    actual_values = dict(actual or {})
    expected_values = dict(expected or {})
    if not correlation_valid:
        status, reason = "BLOCKED", "online_correlation_not_matched"
    elif not actual_values:
        status, reason = "BLOCKED", "online_result_missing"
    elif duration_ms is None:
        status, reason = "WARN", "online_latency_endpoint_missing"
    else:
        mismatched = [name for name, value in expected_values.items() if actual_values.get(name) != value]
        status, reason = ("FAIL", "online_result_mismatch") if mismatched else ("PASS", "online_result_exact_match")
    normalized = {"online_status": status, "latency_ms": duration_ms}
    judgement = ToolJudgement(
        status,
        "EXACT_MATCH" if status == "PASS" else "CORRELATE_AND_COMPARE",
        reason,
        expected=expected_values,
        actual=actual_values,
        duration_ms=duration_ms,
        evidence_refs=evidence_refs,
    )
    return normalized, judgement
