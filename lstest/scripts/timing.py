"""Project-neutral event timing and asynchronous correlation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class EventClock:
    events: dict[str, float] = field(default_factory=dict)
    native: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str, monotonic_seconds: float, **fields: Any) -> None:
        self.events[name] = monotonic_seconds
        self.native.update(fields)

    def latency(self, start: str, end: str) -> float | None:
        if start not in self.events or end not in self.events:
            return None
        value = self.events[end] - self.events[start]
        return round(value, 3) if value >= 0 else None

    def duration_ms(self, start: str, end: str) -> int | None:
        value = self.latency(start, end)
        return int(round(value * 1000)) if value is not None else None

    def mark_standard(self, name: str, monotonic_seconds: float, **fields: Any) -> None:
        """记录公共口径端点；name 由项目 profile 映射到实际 marker。"""
        self.mark(name, monotonic_seconds, **fields)

    def to_dict(self) -> dict[str, Any]:
        durations: dict[str, int] = {}
        pairs = {
            "host_audio_ms": ("HOST_AUDIO_START", "HOST_AUDIO_END"),
            "wakeup_ms": ("WAKE_START", "WAKE_END"),
            "offline_asr_ms": ("OFFLINE_ASR_START", "OFFLINE_ASR_END"),
            "online_ms": ("ONLINE_REQUEST_START", "ONLINE_RESULT_END"),
            "player_ms": ("PLAYER_START", "PLAYER_END"),
        }
        for label, (start, end) in pairs.items():
            value = self.duration_ms(start, end)
            if value is not None:
                durations[label] = value
        return {"events": dict(self.events), "durations_ms": durations, "native_ids": dict(self.native)}


@dataclass(frozen=True)
class CorrelationResult:
    correlation_id_type: str
    request_id: str | None
    response_id: str | None
    correlation_id: str | None
    correlation_rule_id: str
    correlation_valid: bool
    native_ids: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def correlate(request: Mapping[str, Any], response: Mapping[str, Any], rule: Mapping[str, Any]) -> CorrelationResult:
    fields = rule.get("fields") or ["queryId"]
    rule_id = str(rule.get("id", "configured-correlation"))
    for field in fields:
        req = request.get(field)
        resp = response.get(field)
        if req is not None and resp is not None and req == resp:
            return CorrelationResult(str(field), str(req), str(resp), str(req), rule_id, True, {"request": dict(request), "response": dict(response)})
    return CorrelationResult(str(rule.get("type", "unknown")), None, None, None, rule_id, False, {"request": dict(request), "response": dict(response)})
