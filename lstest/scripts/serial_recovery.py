"""串口断开重连通用工具。"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence


def recover_serial(
    collector: Any,
    probe: Callable[[float], tuple[bool, str]],
    *,
    attempts: int = 5,
    delays_s: Sequence[float] = (0.5, 1.0, 2.0, 3.0, 5.0),
    prompt_timeout_s: float = 3.0,
    on_event: Callable[[str, dict], None] | None = None,
) -> tuple[bool, str, list[str]]:
    """多次关闭/重开串口，并用 probe 检查是否恢复。"""
    delays = list(delays_s) or [1.0]
    last_reply = ""
    evidence: list[str] = []
    for attempt in range(1, max(1, int(attempts)) + 1):
        delay = float(delays[min(attempt - 1, len(delays) - 1)])
        if on_event:
            on_event("START", {"attempt": attempt, "delay_s": delay})
        time.sleep(delay)
        try:
            collector.restart()
        except Exception as exc:
            last_reply = f"restart failed: {type(exc).__name__}: {exc}"
            if on_event:
                on_event("FAIL", {"attempt": attempt, "reason": last_reply})
            continue
        ok, reply = probe(prompt_timeout_s)
        last_reply = reply
        if on_event:
            on_event("PASS" if ok else "FAIL", {"attempt": attempt, "reason": reply})
        if ok:
            return True, reply, evidence
    return False, last_reply, evidence
