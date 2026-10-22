"""固定时序工具日志账本。

项目适配器只向此模块提交结构化动作和事实。正式 ``tool.log`` 的
时间戳、标签、字段顺序和结果行均由这里统一生成。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


LOG_TAGS = frozenset({
    "SYSTEM", "CASE", "ACTION", "COMMAND", "DEVICE", "ONLINE", "PLAYER", "ERROR", "RESULT", "SUMMARY",
})
FACT_TAGS = {
    "WAKE": "DEVICE",
    "OFFLINE_ASR": "DEVICE",
    "REQUEST_ID": "ONLINE",
    "RESPONSE_ID": "ONLINE",
    "ONLINE_ASR": "ONLINE",
    "PLAY_URL": "PLAYER",
    "DEVICE_BROADCAST_ID": "PLAYER",
    "PLAYER": "PLAYER",
    "COMMAND_ACK": "COMMAND",
    "COMMAND_EVIDENCE": "COMMAND",
    "INIT_READY": "SYSTEM",
    "RESTART": "SYSTEM",
    "DEVICE_EXCEPTION": "ERROR",
}
PLAYER_STATE_CLASSES = frozenset({"preparation", "active", "terminal", "error", "unknown"})


def safe_basename(value: Any) -> str:
    """Render only a local audio filename, never an absolute host path."""
    text = str(value or "").strip()
    if not text:
        return ""
    return PureWindowsPath(text).name or text.replace("\\", "/").rsplit("/", 1)[-1]


def sanitize_url(value: Any) -> str:
    """Preserve the inspectable URL body while redacting credentials/query tokens."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    query = "…" if parsed.query else ""
    return urlunsplit((parsed.scheme, host, parsed.path, query, ""))


def short_evidence(values: Iterable[Any] | Any) -> str:
    """Convert a serial cursor reference into a compact human-facing pointer."""
    if isinstance(values, (str, bytes)) or values is None:
        refs = [values]
    else:
        refs = list(values)
    for item in refs:
        text = str(item or "").strip()
        if not text:
            continue
        match = re.search(r"serial_([^_./\\]+)(?:_[^./\\]+)?\.log#(\d+)", text, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)}#{match.group(2)}"
        cursor = re.search(r"#(\d+)$", text)
        if cursor:
            return f"#{cursor.group(1)}"
        return text
    return ""


@dataclass(frozen=True)
class FactOccurrence:
    key: str
    value: str
    tag: str
    timestamp: str
    monotonic_seconds: float
    case_id: str
    epoch: int
    port: str = ""
    role: str = ""
    evidence: tuple[str, ...] = ()
    phase: str = ""
    identity: str = ""
    mirror_policy: str = "distinct"
    duration_ms: int | None = None
    e2e_duration_ms: int | None = None


@dataclass
class CaseTimeline:
    case_id: str
    epoch: int
    scenario: str
    text: str
    position: int
    total: int
    expected_keys: set[str] = field(default_factory=set)
    mirror_policy: Mapping[str, str] = field(default_factory=dict)
    reason_labels: Mapping[str, str] = field(default_factory=dict)
    duplicate_policy: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    mirror_window_ms: int = 120
    require_player: bool = False
    opened_at: float = field(default_factory=time.monotonic)
    facts: list[FactOccurrence] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False


class PlayerWindowReducer:
    """Reduce only profile-classified player events, never device markers."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}

    def should_render(self, context: str, state_class: str, render_policy: str) -> bool:
        """Apply profile-owned state classes without inspecting raw marker text."""
        if state_class not in PLAYER_STATE_CLASSES:
            # Unknown classification must stay visible; silently dropping an
            # unclassified device marker would hide the evidence we need to
            # debug the project profile.
            return True
        entry = self._states.setdefault(context, {"seen": set(), "terminal": False})
        if render_policy == "all":
            entry["seen"].add(state_class)
            if state_class in {"terminal", "error"}:
                entry["terminal"] = True
            return True
        if render_policy == "terminal_and_error" and state_class == "preparation":
            return False
        if state_class == "preparation" and render_policy == "first_per_state":
            return False
        if state_class in entry["seen"] and state_class != "error":
            return False
        if entry["terminal"] and state_class in {"active", "terminal"}:
            return False
        entry["seen"].add(state_class)
        if state_class in {"terminal", "error"}:
            entry["terminal"] = True
        return True


class TimelineLedger:
    """Thread-safe writer-independent renderer for the human tool ledger."""

    def __init__(self, write: Callable[[str], None], now: Callable[[], str]) -> None:
        self._write = write
        self._now = now
        self._cases: dict[str, CaseTimeline] = {}
        self._player = PlayerWindowReducer()
        self._task_anomalies: Counter[str] = Counter()

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").replace("\r", " ").replace("\n", " ").strip()

    def _line(self, tag: str, body: str, *, timestamp: str | None = None) -> None:
        if tag not in LOG_TAGS:
            raise ValueError(f"unsupported tool.log tag: {tag}")
        self._write(f"{timestamp or self._now()} [{tag}] {body}\n")

    @staticmethod
    def _append(body: str, name: str, value: Any) -> str:
        text = str(value or "").strip()
        return f"{body}，{name}={text}" if text else body

    def system(self, message: str, *, epoch: int | None = None) -> None:
        body = self._text(message)
        if epoch is not None:
            body = self._append(body, "epoch", epoch)
        self._line("SYSTEM", body)

    def command(
        self,
        action: str,
        command: str,
        *,
        attempt: int | None = None,
        max_attempts: int | None = None,
        result: str = "",
        reason: str = "",
        elapsed_ms: int | None = None,
    ) -> None:
        body = f"{self._text(action)}: {self._text(command)}"
        if attempt is not None:
            body = self._append(body, "尝试", f"{attempt}/{max_attempts or attempt}")
        if result:
            body = self._append(body, "结果", result)
        if reason:
            body = self._append(body, "原因", reason)
        if elapsed_ms is not None:
            body = self._append(body, "命令耗时", f"{elapsed_ms}ms")
        self._line("COMMAND", body)

    def start_case(
        self,
        case_id: str,
        *,
        epoch: int,
        position: int,
        total: int,
        scenario: str,
        text: str,
        expected_keys: Iterable[str] = (),
        mirror_policy: Mapping[str, str] | None = None,
        mirror_window_ms: int = 120,
        reason_labels: Mapping[str, str] | None = None,
        duplicate_policy: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        existing = self._cases.get(case_id)
        if existing and not existing.closed:
            return
        timeline = CaseTimeline(
            case_id=case_id,
            epoch=epoch,
            scenario=self._text(scenario),
            text=self._text(text),
            position=max(1, int(position or 1)),
            total=max(0, int(total or 0)),
            expected_keys={str(item).upper() for item in expected_keys},
            mirror_policy={str(key).upper(): str(value).lower() for key, value in dict(mirror_policy or {}).items()},
            mirror_window_ms=max(0, int(mirror_window_ms or 0)),
            reason_labels={str(key).upper(): self._text(value) for key, value in dict(reason_labels or {}).items()},
            duplicate_policy={str(key).upper(): dict(value) for key, value in dict(duplicate_policy or {}).items() if isinstance(value, Mapping)},
        )
        self._cases[case_id] = timeline
        self._line(
            "CASE",
            f"{timeline.position}/{timeline.total or '-'} START 场景={timeline.scenario} 文本={timeline.text} case_id={case_id}",
        )

    def action(self, case_id: str, text: str, audio_file: Any, *, phase: str = "command") -> None:
        filename = safe_basename(audio_file)
        body = f"主机播放开始: {self._text(text)}"
        if filename:
            body = self._append(body, "文件", filename)
        self._line("ACTION", body)

    def action_end(
        self,
        case_id: str,
        text: str,
        audio_file: Any,
        *,
        status: str,
        duration_ms: int | None = None,
    ) -> None:
        filename = safe_basename(audio_file)
        body = f"主机播放结束: {self._text(text)}"
        if filename:
            body = self._append(body, "文件", filename)
        body = self._append(body, "结果", self._text(status))
        if duration_ms is not None:
            body = self._append(body, "主机播放耗时", f"{duration_ms}ms")
        self._line("ACTION", body)

    def wait(self, case_id: str, action: str, *, planned_ms: int) -> None:
        """Render a case-scoped device-window wait without inventing a fact."""
        body = self._text(action)
        if planned_ms > 0:
            body = self._append(body, "等待", f"{planned_ms}ms")
        self._line("ACTION", body)

    def fact(
        self,
        case_id: str,
        key: str,
        value: Any,
        *,
        epoch: int,
        tag: str | None = None,
        port: str = "",
        role: str = "",
        evidence: Iterable[str] = (),
        phase: str = "",
        identity: str = "",
        mirror_policy: str | None = None,
        duration_ms: int | None = None,
        e2e_duration_ms: int | None = None,
        render: bool = True,
        display_key: str = "",
        track: bool = True,
    ) -> FactOccurrence:
        normalized_key = str(key or "").upper()
        rendered_tag = tag or FACT_TAGS.get(normalized_key, "DEVICE")
        if rendered_tag not in LOG_TAGS:
            raise ValueError(f"unsupported fact tag: {rendered_tag}")
        occurrence = FactOccurrence(
            key=normalized_key,
            value=self._text(value),
            tag=rendered_tag,
            timestamp=self._now(),
            monotonic_seconds=time.monotonic(),
            case_id=case_id,
            epoch=epoch,
            port=self._text(port),
            role=self._text(role),
            evidence=tuple(str(item) for item in evidence),
            phase=self._text(phase),
            identity=self._text(identity),
            mirror_policy=str(mirror_policy or "").lower(),
            duration_ms=duration_ms,
            e2e_duration_ms=e2e_duration_ms,
        )
        timeline = self._cases.get(case_id)
        if timeline and not timeline.closed and track:
            timeline.facts.append(occurrence)
        elif not timeline:
            # A profile may legitimately expose a fact before the host action.
            # Preserve it instead of guessing its business meaning or dropping it.
            self._line(rendered_tag, f"{normalized_key}: {occurrence.value}", timestamp=occurrence.timestamp)
            self.error("", "FACT_OUTSIDE_CASE", "当前没有活动用例，已保留原始事实", evidence=occurrence.evidence)
            return occurrence

        if not render:
            return occurrence
        if normalized_key == "PLAY_URL":
            rendered_value = sanitize_url(occurrence.value)
        else:
            rendered_value = occurrence.value
        body = f"{self._text(display_key) or normalized_key}: {rendered_value}"
        if duration_ms is not None:
            body = self._append(body, "耗时", f"{duration_ms}ms")
        if e2e_duration_ms is not None:
            body = self._append(body, "端到端耗时", f"{e2e_duration_ms}ms")
        self._line(rendered_tag, body, timestamp=occurrence.timestamp)
        return occurrence

    def player(
        self,
        case_id: str,
        state: Any,
        *,
        epoch: int,
        broadcast_id: str = "",
        play_url: str = "",
        device_broadcast_id: str = "",
        duration_ms: int | None = None,
        evidence: Iterable[str] = (),
        state_class: str = "",
        render_policy: str = "all",
        port: str = "",
        role: str = "",
        phase: str = "",
        identity: str = "",
        mirror_policy: str = "distinct",
    ) -> None:
        context = broadcast_id or device_broadcast_id or case_id or "unassociated"
        if play_url:
            self.fact(case_id, "PLAY_URL", play_url, epoch=epoch, tag="PLAYER", evidence=evidence)
        if device_broadcast_id:
            self.fact(case_id, "DEVICE_BROADCAST_ID", device_broadcast_id, epoch=epoch, tag="PLAYER", evidence=evidence)
        raw_state = self._text(state)
        internal_state = self._text(state_class).lower()
        policy = self._text(render_policy).lower() or "all"
        if not self._player.should_render(context, internal_state, policy):
            return
        # ``state`` is the project regex extraction.  Keep it verbatim so a
        # tester can compare this line directly against serial_logs/.
        body = f"PLAYER: {raw_state}"
        if internal_state == "terminal" and duration_ms is not None:
            body = self._append(body, "播放耗时", f"{duration_ms}ms")
        self._line("PLAYER", body)
        self.fact(
            case_id, "PLAYER", raw_state, epoch=epoch, tag="SYSTEM",
            evidence=evidence, identity=identity or context, port=port, role=role,
            phase=phase, mirror_policy=mirror_policy, render=False,
        )

    def error(
        self,
        case_id: str,
        code: str,
        message: str,
        *,
        evidence: Iterable[str] = (),
        handling: str = "",
    ) -> None:
        normalized = str(code or "UNKNOWN_ANOMALY").upper()
        record = {"code": normalized, "message": self._text(message), "evidence": tuple(str(item) for item in evidence)}
        timeline = self._cases.get(case_id)
        if timeline and not timeline.closed:
            timeline.anomalies.append(record)
        self._task_anomalies[normalized] += 1
        body = f"异常: {record['message']}"
        evidence_text = short_evidence(record["evidence"])
        if handling:
            body = self._append(body, "处理", handling)
        if evidence_text:
            body = self._append(body, "证据", evidence_text)
        self._line("ERROR", body)

    @staticmethod
    def _logical_count(timeline: CaseTimeline, key: str) -> tuple[int, int]:
        facts = [item for item in timeline.facts if item.key == key]
        raw_count = len(facts)
        policy = timeline.mirror_policy.get(key, "")
        if not policy and facts:
            policy = facts[0].mirror_policy
        if policy != "mirror" or raw_count < 2:
            return raw_count, raw_count
        groups: list[list[FactOccurrence]] = []
        for fact in facts:
            identity = fact.identity or fact.value
            matched = False
            for group in groups:
                head = group[0]
                head_identity = head.identity or head.value
                within_window = abs(fact.monotonic_seconds - head.monotonic_seconds) * 1000 <= timeline.mirror_window_ms
                different_port = bool(fact.port and head.port and fact.port != head.port)
                if identity == head_identity and within_window and (different_port or not fact.port or not head.port):
                    group.append(fact)
                    matched = True
                    break
            if not matched:
                groups.append([fact])
        return raw_count, len(groups)

    def close_case(
        self,
        case_id: str,
        *,
        status: str,
        reason: str = "",
        expected_keys: Iterable[str] = (),
        current_anomaly_counts: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        timeline = self._cases.get(case_id)
        if timeline is None:
            timeline = CaseTimeline(case_id, 0, "", "", 1, 0)
            self._cases[case_id] = timeline
        if timeline.closed:
            return {"status": status, "reason": reason}
        timeline.closed = True
        required = set(timeline.expected_keys) | {str(item).upper() for item in expected_keys}
        missing = [key for key in sorted(required) if not any(item.key == key for item in timeline.facts)]
        for key in missing:
            tag = FACT_TAGS.get(key, "DEVICE")
            self._line(tag, f"{key}:")
            self.error(case_id, f"MISSING_{key}", timeline.reason_labels.get(f"MISSING_{key}", f"缺少必需事实 {key}"))

        relevant_keys = sorted(set(required) | {item.key for item in timeline.facts})
        raw_counts: dict[str, int] = {}
        logical_counts: dict[str, int] = {}
        for key in relevant_keys:
            raw_counts[key], logical_counts[key] = self._logical_count(timeline, key)

        anomaly_codes = [item["code"] for item in timeline.anomalies]
        for key, policy in timeline.duplicate_policy.items():
            count = logical_counts.get(key, 0)
            code = str(policy.get("code") or f"DUPLICATE_{key}").upper()
            if count > 1 and code not in anomaly_codes:
                self.error(case_id, code, self._text(policy.get("reason") or f"{key} 出现多个逻辑结果"), handling="本轮收尾后继续")
                anomaly_codes.append(code)

        final_status = str(status or "FAIL").upper()
        final_reason = self._text(reason)
        if missing or anomaly_codes:
            if final_status in {"PASS", "EXPECTED", "WARN", ""}:
                final_status = "FAIL"
            candidates = [*anomaly_codes, *(f"MISSING_{key}" for key in missing)]
            priority = candidates[0] if candidates else ""
            final_reason = timeline.reason_labels.get(priority, final_reason or priority)
        body = f"本轮={final_status}"
        if final_reason:
            body = self._append(body, "原因", final_reason)
        if logical_counts:
            body = self._append(body, "逻辑事实", "/".join(f"{key}={logical_counts[key]}" for key in sorted(logical_counts)))
        mirror_parts: list[str] = []
        for key, raw_count, logical_count in ((key, raw_counts[key], logical_counts[key]) for key in sorted(raw_counts)):
            if raw_count != logical_count:
                mirror_parts.append(f"{key}={raw_count}")
        if mirror_parts:
            body = self._append(body, "镜像命中", "/".join(mirror_parts))
        self._line("RESULT", body)

        current = Counter(current_anomaly_counts or self._task_anomalies)
        case_total = len(timeline.anomalies) + len(missing)
        summary = f"本轮异常={case_total}，累计异常={sum(current.values())}"
        for code, count in sorted(current.items()):
            summary = self._append(summary, timeline.reason_labels.get(code, code), count)
        self._line("SUMMARY", summary)
        self._write("\n")
        return {
            "status": final_status,
            "reason": final_reason,
            "raw_counts": raw_counts,
            "logical_counts": logical_counts,
            "missing": missing,
            "anomaly_codes": anomaly_codes,
        }

    def task_summary(self, *, planned: int, completed: int, counts: Mapping[str, int], reason: str, status: str) -> None:
        body = f"任务完成: 计划={planned}，完成={completed}"
        for name in ("PASS", "FAIL", "BLOCKED", "ABORTED"):
            body = self._append(body, name, counts.get(name, 0))
        self._line("SUMMARY", body)
        total = sum(self._task_anomalies.values())
        detail = f"累计异常={total}"
        if reason:
            detail = self._append(detail, "停止原因", reason)
        self._line("SUMMARY", detail)
        self._line("RESULT", f"最终={status}" + (f"，原因={reason}" if reason else ""))


class ToolLogValidator:
    """Validate the stable format produced by :class:`TimelineLedger`."""

    LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[([A-Z]+)(?: \d+/(?:\d+|-))?\] .+$")

    def validate_text(self, text: str) -> list[str]:
        errors: list[str] = []
        active_case = False
        result_seen = False
        for number, line in enumerate(text.splitlines(), start=1):
            if not line:
                continue
            match = self.LINE.match(line)
            if not match:
                errors.append(f"第 {number} 行时间戳或标签不符合固定格式")
                continue
            tag = match.group(1)
            if tag not in LOG_TAGS:
                errors.append(f"第 {number} 行使用未注册标签 {tag}")
            if "BROADCAST_ID" in line or "播报ID=" in line:
                errors.append(f"第 {number} 行泄露内部播报 ID")
            if re.search(r"[A-Za-z]:\\", line):
                errors.append(f"第 {number} 行出现绝对本地路径")
            if tag == "CASE" and " START " in line:
                if active_case and not result_seen:
                    errors.append(f"第 {number} 行开始新用例前未结束上一用例")
                active_case, result_seen = True, False
            elif tag == "RESULT" and "本轮=" in line:
                if not active_case:
                    errors.append(f"第 {number} 行存在没有 CASE 的本轮结果")
                if result_seen:
                    errors.append(f"第 {number} 行同一用例存在多个本轮结果")
                result_seen = True
            elif active_case and result_seen and tag in {"DEVICE", "ONLINE", "PLAYER", "ACTION"}:
                errors.append(f"第 {number} 行在本轮结果后仍写入事实")
        if active_case and not result_seen:
            errors.append("最后一个用例缺少本轮结果")
        return errors

    def validate_run(self, run_dir: Path) -> list[str]:
        """Validate the delivery boundary and human ledger of one finished run."""
        root = Path(run_dir)
        errors: list[str] = []
        expected_files = {"tool.log", "results.csv", "cases.csv", "serial_logs"}
        actual = {item.name for item in root.iterdir()} if root.is_dir() else set()
        unexpected = actual - expected_files - {"STOP"}
        if unexpected:
            errors.append("存在非标准运行产物: " + ", ".join(sorted(unexpected)))
        required = {"tool.log", "results.csv", "cases.csv", "serial_logs"}
        missing = required - actual
        if missing:
            errors.append("缺少标准运行产物: " + ", ".join(sorted(missing)))
        serial = root / "serial_logs"
        if serial.is_dir():
            for item in serial.iterdir():
                if not item.is_file() or item.suffix.lower() != ".log":
                    errors.append(f"串口证据不是 .log: {item.name}")
        tool = root / "tool.log"
        if tool.is_file():
            errors.extend(self.validate_text(tool.read_text(encoding="utf-8")))
        return errors
