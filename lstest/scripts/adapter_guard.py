"""Static guard for project adapters that must use the public raw-record API."""

from __future__ import annotations

from pathlib import Path


FORBIDDEN = (
    "write_tool_log(", "append_tool_jsonl(", "open(\"tool.log", "open('tool.log",
    "emit_fact(", "emit_judgement(", "emit_observation(", "timeline.",
    "record_recognition(", "record_wakeup(", "record_player_marker(",
)


def validate_adapter_source(path: Path) -> list[str]:
    """Return violations; adapters may only submit raw records and actions."""
    text = Path(path).read_text(encoding="utf-8")
    return [f"{path.name}: 禁止适配器调用 {token}" for token in FORBIDDEN if token in text]
