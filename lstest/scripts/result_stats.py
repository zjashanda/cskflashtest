"""长时间压测结果统计工具。

`results.csv` 在长时间压测中会很大，且单行包含 `facts_json`、`evidence` 等大字段。
统计时必须增量读取，禁止一次性使用 `list(csv.DictReader(...))` 全量加载。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


def empty_summary() -> dict:
    """返回一个空的统计结果。"""
    return {
        "rounds": 0, "pass": 0, "fail": 0,
        "hit": 0, "misheard": 0, "empty": 0, "asleep": 0,
        "multi_wake": 0, "multi_asr": 0, "multi_result": 0,
        "avg_round_s": 0.0,
    }


class ResultSummary:
    """增量读取 results.csv，避免长时间压测统计时内存持续上涨。"""

    def __init__(self, result_dir: Path | None):
        self.result_dir = Path(result_dir) if result_dir else None
        self.path = self.result_dir / "results.csv" if self.result_dir else None
        self.offset = 0
        self.fieldnames: list[str] | None = None
        self.summary = empty_summary()
        self.duration_total = 0.0
        self.duration_count = 0

    def update(self) -> dict:
        """只读取上次位置之后新增的完整行，并返回累计统计。"""
        if self.path is None or not self.path.exists():
            return self.summary
        try:
            size = self.path.stat().st_size
        except OSError:
            return self.summary
        if size < self.offset:
            self.offset = 0
            self.fieldnames = None
            self.summary = empty_summary()
            self.duration_total = 0.0
            self.duration_count = 0
        if size == self.offset:
            return self.summary
        try:
            with self.path.open("r", encoding="utf-8", newline="") as handle:
                handle.seek(self.offset)
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        handle.seek(line_start)
                        break
                    self.offset = handle.tell()
                    if self.fieldnames is None:
                        self.fieldnames = next(csv.reader([line.lstrip("\ufeff")]))
                        continue
                    try:
                        values = next(csv.reader([line.rstrip("\r\n")]))
                    except csv.Error:
                        continue
                    if len(values) != len(self.fieldnames):
                        continue
                    self._add(dict(zip(self.fieldnames, values)))
        except OSError:
            pass
        return self.summary

    def _add(self, record: dict) -> None:
        summary = self.summary
        summary["rounds"] += 1
        status = str(record.get("final_status", "")).upper()
        if status == "PASS":
            summary["pass"] += 1
        elif status == "FAIL":
            summary["fail"] += 1
        outcome = str(record.get("round_outcome", "")).strip().lower()
        if outcome in {"hit", "misheard", "empty", "asleep"}:
            summary[outcome] += 1
        try:
            if int(record.get("wake_result_count") or 0) > 1:
                summary["multi_wake"] += 1
            if int(record.get("asr_result_count") or 0) > 1:
                summary["multi_asr"] += 1
        except ValueError:
            pass
        if str(record.get("multi_result", "")).strip().lower() in {"true", "1", "yes"}:
            summary["multi_result"] += 1
        try:
            start = datetime.fromisoformat(str(record.get("started_at", "")).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(record.get("ended_at", "")).replace("Z", "+00:00"))
            if start.tzinfo is not None:
                start = start.astimezone().replace(tzinfo=None)
            if end.tzinfo is not None:
                end = end.astimezone().replace(tzinfo=None)
            self.duration_total += (end - start).total_seconds()
            self.duration_count += 1
            summary["avg_round_s"] = round(self.duration_total / self.duration_count, 1)
        except (TypeError, ValueError):
            pass


def summarize_result_dir(result_dir: Path | None) -> dict:
    """一次性扫描结果目录；内部仍按行增量读取，不会全量载入内存。"""
    return ResultSummary(result_dir).update()


def aggregate_summaries(items: list[dict]) -> dict:
    """汇总多个设备/任务的统计。"""
    total = empty_summary()
    weighted = 0.0
    for item in items:
        rounds = int(item.get("rounds", 0) or 0)
        for key in (
            "rounds", "pass", "fail", "hit", "misheard", "empty", "asleep",
            "multi_wake", "multi_asr", "multi_result",
        ):
            total[key] += int(item.get(key, 0) or 0)
        weighted += float(item.get("avg_round_s", 0.0) or 0.0) * rounds
    if total["rounds"]:
        total["avg_round_s"] = round(weighted / total["rounds"], 1)
    return total


def total_line(total: dict, completed: int, planned: int, suffix: str = "") -> str:
    """生成多设备实时汇总行。"""
    line = (
        f"[TOTAL] rounds={completed}/{planned} pass={total['pass']} fail={total['fail']} "
        f"hit={total['hit']} misheard={total['misheard']} empty={total['empty']} "
        f"asleep={total['asleep']} multi_wake={total['multi_wake']} "
        f"multi_asr={total['multi_asr']} multi_result={total['multi_result']}"
    )
    return f"{line} {suffix}".rstrip()
