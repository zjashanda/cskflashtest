"""公共场景运行时边界。

该模块只处理设备无关的播放、观察窗口、证据边界和结果落盘动作。
项目适配层负责串口事件读取、marker 解析以及业务 oracle，并通过回调注入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .core import CaseResult, TaskArtifacts, sha256_file
from .observations import RawTag, ToolJudgement, judge_wakeup
from .playback import PlaybackBackend
from .profile import DeviceProfile, EVENT_REGISTRY, ProfileFact, RawLogRecord


@dataclass
class BroadcastRecognitionTracker:
    """Enforces the one broadcast to one recognition contract for a task."""

    broadcasts: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    recognition_count: dict[str, int] = field(default_factory=dict)
    last_broadcast_id: str = ""

    def begin_broadcast(
        self,
        broadcast_id: str,
        *,
        case_id: str,
        audio_file: Path,
        audio_sha256: str,
        expected_recognition: Mapping[str, Any] | None = None,
        accepted_raw_variants: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
        epoch: int = 0,
    ) -> dict[str, Any]:
        key = str(broadcast_id).strip()
        if not key:
            raise ValueError("broadcast_id cannot be empty")
        if key in self.broadcasts:
            raise ValueError(f"duplicate broadcast_id: {key}")
        record = {
            "broadcast_id": key,
            "case_id": case_id,
            "audio_file": str(audio_file),
            "audio_sha256": audio_sha256,
            "expected_recognition": dict(expected_recognition or {}),
            "accepted_raw_variants": accepted_raw_variants or {},
            "epoch": int(epoch),
            "started_monotonic": time.monotonic(),
        }
        self.broadcasts[key] = record
        self.pending.append(key)
        self.recognition_count[key] = 0
        self.last_broadcast_id = key
        return record

    def discard_broadcast(self, broadcast_id: str) -> None:
        """Discard a failed host playback so later results stay visibly unmatched."""
        self.broadcasts.pop(broadcast_id, None)
        self.recognition_count.pop(broadcast_id, None)
        if broadcast_id in self.pending:
            self.pending.remove(broadcast_id)
        if self.last_broadcast_id == broadcast_id:
            self.last_broadcast_id = ""

    def observe_recognition(self, broadcast_id: str | None = None, *, case_id: str = "") -> tuple[str | None, str | None, int]:
        """Return (broadcast id, anomaly code, number of results for that broadcast)."""
        key = str(broadcast_id).strip() if broadcast_id else ""
        if not key:
            matching_pending = [
                item for item in self.pending
                if not case_id or self.broadcasts.get(item, {}).get("case_id") == case_id
            ]
            if matching_pending:
                key = matching_pending[0]
            elif case_id and self.last_broadcast_id and self.broadcasts.get(self.last_broadcast_id, {}).get("case_id") == case_id:
                # A second result within the same case must remain attributable
                # to that broadcast instead of being mistaken for an orphan.
                key = self.last_broadcast_id
        if not key or key not in self.broadcasts:
            return None, "UNEXPECTED_RECOGNITION", 0
        self.recognition_count[key] = self.recognition_count.get(key, 0) + 1
        count = self.recognition_count[key]
        if count == 1:
            if key in self.pending:
                self.pending.remove(key)
            return key, None, count
        return key, "MULTIPLE_RECOGNITIONS_FOR_PLAYBACK", count


@dataclass
class CaseWindow:
    """A bounded case/epoch scope that prevents late async results leaking."""

    case_id: str
    epoch: int
    opened_monotonic: float
    opened_cursors: dict[str, int] = field(default_factory=dict)
    closed_monotonic: float | None = None
    closed_cursors: dict[str, int] = field(default_factory=dict)
    broadcast_ids: list[str] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.closed_monotonic is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "epoch": self.epoch,
            "opened_monotonic": self.opened_monotonic,
            "opened_cursors": dict(self.opened_cursors),
            "closed_monotonic": self.closed_monotonic,
            "closed_cursors": dict(self.closed_cursors),
            "broadcast_ids": list(self.broadcast_ids),
        }


@dataclass
class WakeWordSequence:
    """Ordered wake word requirements loaded from the project requirements table."""

    requirements: list[dict[str, Any]] = field(default_factory=list)
    index: int = 0

    @classmethod
    def from_items(cls, values: Iterable[Mapping[str, Any]]) -> "WakeWordSequence":
        items = [dict(item) for item in values]
        seen: set[str] = set()
        for item in items:
            wake_word_id = str(item.get("wake_word_id") or "").strip()
            if not wake_word_id or not str(item.get("spoken_text") or "").strip() or not dict(item.get("expected_raw") or {}):
                raise ValueError("each wake word requires wake_word_id, spoken_text, and expected_raw")
            if wake_word_id in seen:
                raise ValueError(f"duplicate wake_word_id: {wake_word_id}")
            seen.add(wake_word_id)
        return cls(items)

    def current(self) -> dict[str, Any]:
        if self.index >= len(self.requirements):
            raise RuntimeError("all configured wake words have already been verified")
        return dict(self.requirements[self.index])

    def matches_current(self, wake_word_id: str) -> bool:
        if not self.requirements:
            return True
        current = self.current()
        return wake_word_id == str(current["wake_word_id"])

    def advance(self) -> None:
        if not self.requirements:
            return
        self.current()
        self.index += 1


class ScenarioRuntime:
    """为项目场景提供统一、可复用的运行时动作边界。"""

    def __init__(
        self,
        artifacts: TaskArtifacts,
        *,
        playback_device_key: str | None = None,
        playback_script: Path | None = None,
        profile: DeviceProfile | None = None,
        wake_words: Iterable[Mapping[str, Any]] = (),
        health_recovery: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.player = PlaybackBackend(artifacts, playback_device_key, playback_script)
        self.profile = profile
        self.broadcast_tracker = BroadcastRecognitionTracker()
        self._next_broadcast_index = 0
        self._recognition_associations: dict[str, list[dict[str, Any]]] = {}
        self._case_facts: dict[str, dict[str, Any]] = {}
        self._attempts: dict[str, dict[str, int]] = {}
        self._online_requests: dict[tuple[int, str], dict[str, Any]] = {}
        self._profile_player_started: dict[tuple[int, str, str], float] = {}
        self.case_windows: dict[str, CaseWindow] = {}
        self.wake_word_sequence = WakeWordSequence.from_items(wake_words)
        self.health_recovery = health_recovery
        self.artifacts.register_epoch_listener(self.invalidate_epoch)

    def freeze_cases(
        self,
        cases: Iterable[Any],
        *,
        random_seed: Any = "",
        profile_version: Any = "",
        profile_sha256: Any = "",
    ) -> int:
        """Freeze the adapter's final case sequence before any device action."""
        frozen_cases = list(cases)
        if self.profile is not None and self.profile.is_formal:
            required_types: set[str] = set()
            for item in frozen_cases:
                metadata = getattr(item, "metadata", {}) if not isinstance(item, Mapping) else item.get("metadata", item)
                contract = self.profile.case_contract(metadata if isinstance(metadata, Mapping) else {})
                required_keys = {str(key).upper() for key in contract.get("required_facts", ())}
                for event_type, registry in EVENT_REGISTRY.items():
                    if registry["key"] in required_keys:
                        required_types.add(event_type)
            self.profile.assert_formal_ready(required_types)
        return self.artifacts.freeze_cases(
            frozen_cases,
            random_seed=random_seed,
            profile_version=profile_version,
            profile_sha256=profile_sha256,
        )

    def begin_lazy_cases(
        self,
        *,
        scenario: str,
        planned_total: int | None = None,
        random_seed: Any = "",
        profile_version: Any = "",
        profile_sha256: Any = "",
    ) -> None:
        if self.profile is not None and self.profile.is_formal:
            self.profile.assert_formal_ready()
        self.artifacts.begin_lazy_cases(
            scenario=scenario,
            planned_total=planned_total,
            random_seed=random_seed,
            profile_version=profile_version,
            profile_sha256=profile_sha256,
        )

    def declare_case(self, case: Any) -> int:
        metadata = getattr(case, "metadata", {}) if not isinstance(case, Mapping) else case.get("metadata", case)
        if self.profile is not None and self.profile.is_formal:
            self.profile.case_contract(metadata if isinstance(metadata, Mapping) else {})
        return self.artifacts.declare_case(case)

    def current_wake_word(self) -> dict[str, Any]:
        """Return the only wake word currently allowed by the ordered requirements table."""
        return self.wake_word_sequence.current()

    def submit_raw_record(self, record: RawLogRecord | Mapping[str, Any], *, case_id: str) -> list[ProfileFact]:
        """Submit one raw source record through the project profile.

        This is the only supported device/cloud/player fact entry point for
        new project adapters. The profile decides what matches and which
        capture is displayed; the runtime only preserves arrival order and
        forwards the extracted device-native value to the timeline ledger.
        """
        if self.profile is None:
            raise RuntimeError("profile-driven raw record submission requires a DeviceProfile")
        if isinstance(record, Mapping):
            raw = RawLogRecord(
                text=str(record.get("text", record.get("line", ""))),
                source=str(record.get("source", "")),
                port=str(record.get("port", "")),
                role=str(record.get("role", "")),
                cursor=int(record["cursor"]) if record.get("cursor") not in (None, "") else None,
                observed_at=str(record.get("observed_at", record.get("timestamp", ""))),
                sequence=int(record.get("sequence", 0) or 0),
                epoch=int(record.get("epoch", self.artifacts.current_epoch) or 0),
                phase=str(record.get("phase", "")),
                evidence=tuple(
                    str(item) for item in (
                        [record.get("evidence")] if isinstance(record.get("evidence"), (str, bytes)) else record.get("evidence", ())
                    ) if str(item)
                ),
            )
        else:
            raw = record
        if not raw.text:
            return []
        refs = raw.evidence or (() if raw.cursor is None or not raw.port else (
            f"serial_logs/serial_{raw.port}_{raw.role or 'unknown'}.log#{raw.cursor}",
        ))
        if refs != raw.evidence:
            raw = RawLogRecord(
                text=raw.text, source=raw.source, port=raw.port, role=raw.role,
                cursor=raw.cursor, observed_at=raw.observed_at, sequence=raw.sequence,
                epoch=raw.epoch, phase=raw.phase, evidence=refs,
            )
        facts = self.profile.extract_facts(raw)
        for fact in facts:
            if fact.event_type == "player_state":
                player_key = (raw.epoch, case_id, fact.identity or case_id)
                duration_ms = None
                if fact.state_class == "active":
                    self._profile_player_started[player_key] = time.monotonic()
                elif fact.state_class in {"terminal", "error"}:
                    started = self._profile_player_started.pop(player_key, None)
                    if started is not None:
                        duration_ms = round((time.monotonic() - started) * 1000)
                self.artifacts.timeline_player(
                    case_id,
                    fact.presentation_value,
                    port=fact.port,
                    role=fact.role,
                    phase=fact.phase,
                    identity=fact.identity,
                    mirror_policy=fact.mirror_policy,
                    evidence=fact.evidence,
                    state_class=fact.state_class,
                    render_policy=fact.render_policy,
                    duration_ms=duration_ms,
                )
            else:
                for index, (tag_name, value) in enumerate(fact.display_fields):
                    self.artifacts.timeline_fact(
                        case_id,
                        fact.key,
                        value,
                        tag=fact.tag,
                        port=fact.port,
                        role=fact.role,
                        phase=fact.phase,
                        identity=fact.identity,
                        mirror_policy=fact.mirror_policy,
                        evidence=fact.evidence,
                        display_key=tag_name,
                        track=index == 0,
                    )
            self._update_case_facts(
                case_id,
                profile_facts=[
                    *list(self._case_facts.get(case_id, {}).get("profile_facts", ()),),
                    {
                        "event_type": fact.event_type,
                        "key": fact.key,
                        "display": fact.presentation_value,
                        "captures": dict(fact.captures),
                        "rule_id": fact.rule_id,
                        "evidence": list(fact.evidence),
                    },
                ],
            )
        safety_reason = self.profile.safety_stop_reason(facts)
        if safety_reason:
            self.artifacts.request_stop(
                "PROFILE_SAFETY_STOP", safety_reason, case_id=case_id, evidence=refs,
            )
        return facts

    def _reject_legacy_fact_api(self, api: str) -> None:
        if self.profile is not None and self.profile.is_formal:
            raise RuntimeError(f"{api} 不适用于正式 profile；项目适配器必须调用 submit_raw_record")

    def open_case_window(self, case_id: str, *, cursors: Mapping[str, int] | None = None) -> CaseWindow:
        """Open the only recognition scope allowed to consume this case's results."""
        key = str(case_id).strip()
        if not key:
            raise ValueError("case_id is required to open a CaseWindow")
        existing = self.case_windows.get(key)
        if existing and not existing.closed:
            return existing
        window = CaseWindow(
            case_id=key,
            epoch=self.artifacts.current_epoch,
            opened_monotonic=time.monotonic(),
            opened_cursors=dict(cursors or {}),
        )
        self.case_windows[key] = window
        frozen = self.artifacts.case_metadata(key)
        self._case_facts.setdefault(key, {
            "started_at": self._beijing_now(),
            "scenario": frozen.get("scenario", ""),
            "input_text": frozen.get("input_text", ""),
        })
        self.artifacts.timeline_case_start(key, epoch=window.epoch)
        return window

    @staticmethod
    def _beijing_now() -> str:
        # Import locally to keep the public runtime import surface unchanged.
        from .core import now_iso
        return now_iso()

    def _update_case_facts(self, case_id: str, **fields: Any) -> None:
        if not case_id:
            return
        frozen = self.artifacts.case_metadata(case_id)
        current = self._case_facts.setdefault(case_id, {
            "started_at": self._beijing_now(),
            "scenario": frozen.get("scenario", ""),
            "input_text": frozen.get("input_text", ""),
        })
        for name, value in fields.items():
            if isinstance(value, Mapping) and isinstance(current.get(name), Mapping):
                current[name] = {**dict(current[name]), **dict(value)}
            else:
                current[name] = value

    def _next_attempt(self, case_id: str, stage: str) -> int:
        attempts = self._attempts.setdefault(case_id, {})
        attempts[stage] = attempts.get(stage, 0) + 1
        return attempts[stage]

    @staticmethod
    def _first_value(payload: Mapping[str, Any], *names: str) -> str:
        for name in names:
            value = payload.get(name)
            if value is not None and str(value).strip():
                return str(value)
        return ""

    def record_online_request(
        self,
        raw_values: Mapping[str, Any],
        *,
        case_id: str,
        request_id: str | None = None,
        evidence_refs: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Register an original online request before its asynchronous response.

        The project adapter supplies its native request object unchanged.  This
        method deliberately does not infer IDs from a later response: without
        a recorded request, `results.csv` leaves online timing empty.
        """
        self._reject_legacy_fact_api("record_online_request")
        payload = dict(raw_values)
        raw_id = str(request_id or self._first_value(payload, "request_id", "requestId", "queryId", "traceId", "id")).strip()
        refs = [str(item) for item in evidence_refs]
        if not case_id or not raw_id:
            raise ValueError("online request requires case_id and a native request id")
        key = (self.artifacts.current_epoch, raw_id)
        previous = self._online_requests.get(key)
        record = {
            "request_id": raw_id,
            "case_id": case_id,
            "epoch": self.artifacts.current_epoch,
            "raw": payload,
            "started_monotonic": time.monotonic(),
            "evidence_refs": refs,
        }
        self._online_requests[key] = record
        if previous is not None:
            self.artifacts.record_anomaly(
                "DUPLICATE_ONLINE_REQUEST_ID",
                "当前 epoch 中出现重复在线请求 ID，后续响应不得计算耗时。",
                case_id=case_id,
                request_id=raw_id,
                previous=previous,
                current=record,
            )
            record["duplicate"] = True
        self.artifacts.timeline_fact(
            case_id, "REQUEST_ID", raw_id,
            evidence=refs, phase="online_request", identity=raw_id,
        )
        self._update_case_facts(
            case_id,
            online={"request_id": raw_id, "raw_request": payload},
            correlation={"request_id": raw_id, "valid": False},
        )
        return record

    def _resolve_online_response(self, raw_values: Mapping[str, Any], *, case_id: str) -> dict[str, Any]:
        correlation_id = self._first_value(raw_values, "request_id", "requestId", "queryId", "traceId", "correlation_id")
        response_id = self._first_value(raw_values, "response_id", "responseId", "resultId", "id")
        key = (self.artifacts.current_epoch, correlation_id)
        request = self._online_requests.get(key) if correlation_id else None
        window = self.case_windows.get(case_id)
        valid = bool(
            request
            and not request.get("duplicate")
            and request.get("case_id") == case_id
            and request.get("epoch") == self.artifacts.current_epoch
            and window is not None
            and not window.closed
        )
        reason = ""
        if not correlation_id:
            reason = "online_response_correlation_id_missing"
        elif request is None:
            reason = "online_request_not_found_or_cross_epoch"
        elif request.get("duplicate"):
            reason = "duplicate_online_request_id"
        elif request.get("case_id") != case_id:
            reason = "online_response_case_mismatch"
        elif window is None:
            reason = "online_response_case_window_missing"
        elif window.closed:
            reason = "online_response_after_case_close"
        timing_ms: int | None = None
        if valid:
            timing_ms = round((time.monotonic() - float(request["started_monotonic"])) * 1000)
        return {
            "request_id": correlation_id,
            "response_id": response_id,
            "valid": valid,
            "reason": reason,
            "recognition_latency_ms": timing_ms,
            "request": request,
        }

    def record_no_wakeup(self, case_id: str, *, reason: str = "wakeup_timeout", evidence_refs: Iterable[str] = ()) -> dict[str, Any]:
        """Record a completed wake observation window with no wake result."""
        refs = [str(item) for item in evidence_refs]
        anomaly = self.artifacts.record_anomaly(
            "NO_WAKEUP",
            "播报窗口内未收到唤醒结果。",
            case_id=case_id,
            reason=reason,
            evidence_refs=refs,
        )
        self._record_health("NO_WAKEUP", failed=True, case_id=case_id, evidence_refs=refs)
        self._recognition_associations.setdefault(case_id, []).append({
            "status": "FAIL", "reason": anomaly["code"], "evidence_refs": refs,
        })
        return anomaly

    def record_no_recognition(self, case_id: str, *, reason: str = "recognition_timeout", evidence_refs: Iterable[str] = ()) -> dict[str, Any]:
        """Record a completed observation window with no final recognition."""
        refs = [str(item) for item in evidence_refs]
        anomaly = self.artifacts.record_anomaly(
            "NO_RECOGNITION",
            "播报窗口内未收到最终识别结果。",
            case_id=case_id,
            reason=reason,
            evidence_refs=refs,
        )
        self._record_health("NO_RECOGNITION", failed=True, case_id=case_id, evidence_refs=refs)
        self._recognition_associations.setdefault(case_id, []).append({
            "status": "FAIL", "reason": anomaly["code"], "evidence_refs": refs,
        })
        return anomaly

    def _record_health(self, category: str, *, failed: bool, case_id: str = "", **fields: Any) -> dict[str, Any]:
        record = self.artifacts.record_health(category, failed=failed, case_id=case_id, **fields)
        handling = str((self.artifacts.health_policy.get(category) or self.artifacts.health_policy.get(category.lower()) or {}).get("handling") or "")
        if not record.get("crossed") or self.health_recovery is None or ("session" not in handling and "recover" not in handling):
            return record
        try:
            result = self.health_recovery(category, {"case_id": case_id, **record, **fields})
            self.artifacts.emit(
                "HEALTH_SESSION_RECOVERY_COMPLETED",
                level="INFO",
                message="项目适配器会话恢复回调已完成，继续执行压测。",
                case_id=case_id,
                phase="health_policy",
                status="PASS",
                handling=handling,
                raw={"category": category, "result": result},
            )
        except Exception as error:
            self.artifacts.record_anomaly(
                "HEALTH_SESSION_RECOVERY_EXCEPTION",
                "项目适配器会话恢复回调异常，压测将继续。",
                case_id=case_id,
                category=category,
                error=f"{type(error).__name__}: {error}",
            )
        return record

    def close_case_window(self, case_id: str, *, cursors: Mapping[str, int] | None = None, reason: str = "case_complete") -> CaseWindow | None:
        window = self.case_windows.get(str(case_id).strip())
        if window is None or window.closed:
            return window
        window.closed_monotonic = time.monotonic()
        window.closed_cursors = dict(cursors or {})
        self.artifacts.emit(
            "CASE_WINDOW_CLOSED",
            message=f"用例窗口已关闭：{window.case_id}。",
            case_id=window.case_id,
            epoch=window.epoch,
            phase="case_close",
            reason=reason,
            raw=window.to_dict(),
        )
        return window

    def invalidate_epoch(self, epoch: int, *, reason: str) -> None:
        """Close all old windows when a restart starts a new device session."""
        for window in self.case_windows.values():
            if not window.closed and window.epoch < epoch:
                self.close_case_window(window.case_id, reason=f"epoch_changed:{reason}")

    def probe(self) -> dict[str, Any]:
        """探测显式播放设备，失败时返回可审计结果。"""
        ok = self.player.probe()
        result = {
            "status": "PASS" if ok else "BLOCKED",
            "device_key": self.player.device_key or "",
            "playback_target": self.player.target_label,
            "playback_target_mode": self.player.target_mode,
        }
        if not ok:
            result["reason"] = "playback_probe_failed"
            raise RuntimeError("BLOCKED_AUDIO_DEVICE: lstest playback probe failed")
        return result

    def play(
        self,
        audio_file: Path,
        *,
        case_id: str = "",
        expected_recognition: Mapping[str, Any] | None = None,
        accepted_raw_variants: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
        timeout: float = 120.0,
        spoken_text: str | None = None,
        phase: str = "command",
    ) -> dict[str, Any]:
        """播放音频，并建立与期望原始识别字段一一对应的播报窗口。"""
        self.artifacts.require_cases_frozen("audio_playback")
        if case_id:
            self.artifacts.require_case_declared(case_id, "audio_playback")
        window = self.open_case_window(case_id) if case_id else None
        self._next_broadcast_index += 1
        broadcast_id = f"broadcast-{self._next_broadcast_index:06d}"
        audio_sha256 = sha256_file(audio_file) if audio_file.is_file() else ""
        broadcast = self.broadcast_tracker.begin_broadcast(
            broadcast_id,
            case_id=case_id,
            audio_file=audio_file,
            audio_sha256=audio_sha256,
            expected_recognition=expected_recognition,
            accepted_raw_variants=accepted_raw_variants,
            epoch=window.epoch if window else self.artifacts.current_epoch,
        )
        if window is not None:
            window.broadcast_ids.append(broadcast_id)
        self._update_case_facts(
            case_id,
            broadcast_id=broadcast_id,
            audio_path=str(audio_file),
            audio_sha256=audio_sha256,
            player={
                "broadcast_id": broadcast_id,
                "status": "REQUESTED",
                "device_playback_status": "UNVERIFIED",
            },
        )
        self.artifacts.timeline_action(
            case_id,
            text=spoken_text if spoken_text is not None else self.artifacts.case_metadata(case_id).get("input_text", ""),
            audio_file=audio_file,
            phase=phase,
        )
        try:
            ok = self.player.play(audio_file, case_id=case_id, broadcast_id=broadcast_id, timeout=timeout)
        except Exception as error:
            self.broadcast_tracker.discard_broadcast(broadcast_id)
            playback = dict(self.player.last_playback)
            anomaly = self.artifacts.record_anomaly(
                "PLAYER_PLAYBACK_EXCEPTION",
                f"播报窗口 {broadcast_id} 的主机播放器发生未处理异常。",
                case_id=case_id,
                broadcast_id=broadcast_id,
                audio_file=str(audio_file),
                playback=playback,
                error=f"{type(error).__name__}: {error}",
            )
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": broadcast_id,
                    "playback": playback,
                })
            self._record_health("PLAYER_FAILURE", failed=True, case_id=case_id, broadcast_id=broadcast_id)
            self._update_case_facts(case_id, player={**playback, "broadcast_id": broadcast_id})
            self.artifacts.emit(
                "BROADCAST_FAILED", level="ERROR", message=f"播报窗口 {broadcast_id} 未成功播放。",
                task_log=True, case_id=case_id, broadcast_id=broadcast_id,
                error=f"{type(error).__name__}: {error}", playback=playback,
            )
            return {
                "returncode": 1,
                "error": f"{type(error).__name__}: {error}",
                "audio_file": str(audio_file),
                "broadcast_id": broadcast_id,
                "playback": playback,
            }
        result: dict[str, Any] = {
            "returncode": 0 if ok else 1,
            "audio_file": str(audio_file),
            "audio_sha256": audio_sha256,
            "broadcast_id": broadcast_id,
        }
        if not ok:
            self.broadcast_tracker.discard_broadcast(broadcast_id)
            playback = dict(self.player.last_playback)
            code = {
                "TIMEOUT": "PLAYER_PLAYBACK_TIMEOUT",
                "BLOCKED": "PLAYER_PLAYBACK_BLOCKED",
            }.get(str(playback.get("status") or ""), "PLAYER_PLAYBACK_FAILED")
            anomaly = self.artifacts.record_anomaly(
                code,
                f"播报窗口 {broadcast_id} 的主机播放器未成功完成。",
                case_id=case_id,
                broadcast_id=broadcast_id,
                audio_file=str(audio_file),
                playback=playback,
            )
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": broadcast_id,
                    "playback": playback,
                })
            self._record_health("PLAYER_FAILURE", failed=True, case_id=case_id, broadcast_id=broadcast_id)
            self._update_case_facts(case_id, player={**playback, "broadcast_id": broadcast_id})
            self.artifacts.emit(
                "BROADCAST_FAILED", level="ERROR", message=f"播报窗口 {broadcast_id} 未成功播放。",
                task_log=True, case_id=case_id, broadcast_id=broadcast_id, error="playback_failed", playback=playback,
            )
            result["error"] = "playback_failed"
            result["playback"] = playback
        else:
            self._record_health("PLAYER_FAILURE", failed=False, case_id=case_id, broadcast_id=broadcast_id)
            self._update_case_facts(case_id, player={**dict(self.player.last_playback), "broadcast_id": broadcast_id})
        return result

    def record_player_marker(
        self,
        marker: str,
        *,
        case_id: str = "",
        broadcast_id: str | None = None,
        port: str | None = None,
        raw_line: str = "",
        evidence_refs: Iterable[str] = (),
        presentation_value: str | None = None,
        play_url: str = "",
        device_broadcast_id: str = "",
        state_class: str = "",
        render_policy: str = "all",
    ) -> dict[str, Any]:
        """Record a project player marker and make device-side errors actionable.

        A successful host-player process only means the request was submitted.
        Project adapters call this method for serial/device player markers to
        prove device-side start/end or retain the exact marker on failure.
        """
        self._reject_legacy_fact_api("record_player_marker")
        refs = tuple(str(item) for item in evidence_refs)
        broadcast = self.broadcast_tracker.broadcasts.get(str(broadcast_id or ""), {})
        audio_file = Path(str(broadcast["audio_file"])) if broadcast.get("audio_file") else None
        record = self.player.observe_marker(
            marker,
            case_id=case_id,
            broadcast_id=str(broadcast_id or ""),
            audio=audio_file,
            port=port,
            raw_line=raw_line,
            evidence_refs=refs,
            state_class=state_class,
        )
        self.artifacts.timeline_player(
            case_id,
            presentation_value if presentation_value is not None else marker,
            broadcast_id=str(broadcast_id or ""),
            play_url=play_url,
            device_broadcast_id=device_broadcast_id,
            duration_ms=record.get("duration_ms"),
            evidence=refs,
            state_class=state_class,
            render_policy=render_policy,
        )
        if record.get("player_state") == "error":
            anomaly = self.artifacts.record_anomaly(
                "PLAYER_DEVICE_MARKER_ERROR",
                f"设备侧播放器返回异常 marker: {marker}。",
                case_id=case_id,
                broadcast_id=str(broadcast_id or ""),
                marker=marker,
                port=port or "",
                raw_line=raw_line,
                evidence_refs=list(refs),
            )
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": str(broadcast_id or ""),
                    "player_marker": marker,
                })
            record["anomaly_code"] = anomaly["code"]
        self._update_case_facts(
            case_id,
            player={
                "broadcast_id": str(broadcast_id or ""),
                "device_playback_status": record.get("device_playback_status", record.get("player_state", "")),
                "last_marker": marker,
            },
        )
        return record

    def observe_recognition(
        self,
        recognition: Any,
        *,
        case_id: str = "",
        broadcast_id: str | None = None,
        evidence_refs: Iterable[str] = (),
        suppress_content_mismatch: bool = False,
        accepted_raw_variants: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Associate one project recognition result with one host broadcast window.

        Project adapters must call this exactly once for every final recognition
        result they observe.  Omit ``broadcast_id`` to consume the oldest
        unmatched broadcast.  An unmatched result or a second result for the
        same broadcast is recorded as a task anomaly immediately.
        """
        window = self.case_windows.get(case_id) if case_id else None
        if window and (window.closed or window.epoch != self.artifacts.current_epoch):
            late_payload = {
                "case_id": case_id,
                "broadcast_id": broadcast_id or "",
                "recognition": recognition,
                "window": window.to_dict(),
                "current_epoch": self.artifacts.current_epoch,
                "evidence_refs": [str(item) for item in evidence_refs],
            }
            anomaly = self.artifacts.record_anomaly(
                "LATE_RESULT_AFTER_CASE_CLOSE",
                f"用例 {case_id} 关闭或跨 epoch 后才收到识别结果。",
                **late_payload,
            )
            return {
                "status": "FAIL",
                "reason": anomaly["code"],
                "anomaly_codes": [anomaly["code"]],
                "broadcast_id": "",
                "case_id": case_id,
                "recognition": recognition,
                "recognition_count": 0,
                "evidence_refs": late_payload["evidence_refs"],
            }
        explicit_broadcast = self.broadcast_tracker.broadcasts.get(str(broadcast_id or ""))
        if explicit_broadcast and int(explicit_broadcast.get("epoch", -1)) != self.artifacts.current_epoch:
            anomaly = self.artifacts.record_anomaly(
                "LATE_RESULT_AFTER_CASE_CLOSE",
                "旧 epoch 的播报窗口收到迟到识别结果。",
                case_id=case_id or str(explicit_broadcast.get("case_id") or ""),
                broadcast_id=str(broadcast_id or ""),
                recognition=recognition,
                broadcast_epoch=explicit_broadcast.get("epoch"),
                current_epoch=self.artifacts.current_epoch,
                evidence_refs=[str(item) for item in evidence_refs],
            )
            return {
                "status": "FAIL", "reason": anomaly["code"], "anomaly_codes": [anomaly["code"]],
                "broadcast_id": "", "case_id": case_id, "recognition": recognition,
                "recognition_count": 0, "evidence_refs": [str(item) for item in evidence_refs],
            }
        assigned_id, anomaly_code, count = self.broadcast_tracker.observe_recognition(broadcast_id, case_id=case_id)
        refs = [str(item) for item in evidence_refs]
        payload = {
            "broadcast_id": assigned_id or "",
            "case_id": case_id,
            "recognition": recognition,
            "recognition_count": count,
            "evidence_refs": refs,
        }
        expected = dict(self.broadcast_tracker.broadcasts.get(assigned_id or "", {}).get("expected_recognition") or {})
        configured_variants = accepted_raw_variants
        if configured_variants is None:
            configured_variants = self.broadcast_tracker.broadcasts.get(assigned_id or "", {}).get("accepted_raw_variants")
        recognition_values = dict(recognition) if isinstance(recognition, Mapping) else {}
        raw_exact_status, semantic_status = self._recognition_match_status(
            recognition_values, expected, configured_variants,
        )
        content_mismatch = not suppress_content_mismatch and bool(expected) and semantic_status == "FAIL"
        anomaly_codes = [code for code in (anomaly_code,) if code]
        if assigned_id and not expected:
            anomaly_codes.append("MISSING_EXPECTED_RECOGNITION_CONTRACT")
        if anomaly_code:
            message = (
                "发现未关联播报的识别结果。"
                if anomaly_code == "UNEXPECTED_RECOGNITION"
                else f"播报窗口 {assigned_id} 出现第 {count} 个识别结果。"
            )
            anomaly = self.artifacts.record_anomaly(anomaly_code, message, **payload)
            self.artifacts.emit("RECOGNITION_ASSOCIATION_ANOMALY", level="ERROR", message=message, phase="recognition", raw={**anomaly, **payload})
        if content_mismatch:
            content_code = "RECOGNITION_RESULT_MISMATCH"
            anomaly_codes.append(content_code)
            mismatch_payload = {**payload, "expected_recognition": expected}
            anomaly = self.artifacts.record_anomaly(
                content_code,
                f"播报窗口 {assigned_id} 的识别原始结果与当前播报语料期望不一致。",
                **mismatch_payload,
            )
            self.artifacts.emit("RECOGNITION_ASSOCIATION_ANOMALY", level="ERROR", message="识别原始结果错配。", phase="recognition", raw={**anomaly, **mismatch_payload})
        if assigned_id and not expected:
            missing_code = "MISSING_EXPECTED_RECOGNITION_CONTRACT"
            missing_payload = {**payload, "expected_recognition": expected}
            anomaly = self.artifacts.record_anomaly(
                missing_code,
                f"播报窗口 {assigned_id} 未声明当前语料的期望原始识别字段。",
                **missing_payload,
            )
            self.artifacts.emit("RECOGNITION_ASSOCIATION_ANOMALY", level="ERROR", message="缺少期望识别契约。", phase="recognition", raw={**anomaly, **missing_payload})
        if anomaly_codes:
            return {
                "status": "FAIL",
                "reason": anomaly_codes[0],
                "anomaly_codes": anomaly_codes,
                "expected_recognition": expected,
                "accepted_raw_variants": configured_variants or {},
                "raw_exact_status": raw_exact_status,
                "semantic_status": semantic_status,
                **payload,
            }
        self.artifacts.emit(
            "RECOGNITION_ASSOCIATED",
            message=f"识别结果已关联至播报窗口 {assigned_id}。",
            task_log=True,
            expected_recognition=expected,
            accepted_raw_variants=configured_variants or {},
            raw_exact_status=raw_exact_status,
            semantic_status=semantic_status,
            **payload,
        )
        return {
            "status": "PASS",
            "reason": "ONE_TO_ONE_BROADCAST_RECOGNITION",
            "expected_recognition": expected,
            "accepted_raw_variants": configured_variants or {},
            "raw_exact_status": raw_exact_status,
            "semantic_status": semantic_status,
            **payload,
        }

    @staticmethod
    def _recognition_match_status(
        actual: Mapping[str, Any],
        expected: Mapping[str, Any],
        accepted_raw_variants: Mapping[str, Any] | list[Mapping[str, Any]] | None,
    ) -> tuple[str, str]:
        """Keep literal equality separate from profile-approved alternatives."""
        if not expected:
            return "NOT_APPLICABLE", "NOT_CONFIGURED"
        exact = all(actual.get(name) == value for name, value in expected.items())
        if exact:
            return "PASS", "PASS"
        variants = accepted_raw_variants or {}
        if isinstance(variants, Mapping):
            allowed = True
            for name, expected_value in expected.items():
                options = variants.get(name, [])
                if not isinstance(options, (list, tuple, set)):
                    options = [options]
                allowed_values = {expected_value, *options}
                if actual.get(name) not in allowed_values:
                    allowed = False
                    break
            return "FAIL", "PASS" if allowed and variants else "FAIL"
        if isinstance(variants, list):
            for variant in variants:
                candidate = {**expected, **dict(variant)}
                if all(actual.get(name) == value for name, value in candidate.items()):
                    return "FAIL", "PASS"
        return "FAIL", "FAIL"

    def record_recognition(
        self,
        raw_values: Mapping[str, Any],
        *,
        source: str,
        case_id: str = "",
        broadcast_id: str | None = None,
        normalized: Mapping[str, Any] | None = None,
        accepted_raw_variants: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
        judgement: ToolJudgement | None = None,
        evidence_refs: Iterable[str] = (),
        suppress_content_mismatch: bool = False,
        human_log: bool = True,
        presentation_field: str | None = None,
        duration_ms: int | None = None,
        port: str = "",
        role: str = "",
        identity: str = "",
        mirror_policy: str | None = None,
    ) -> dict[str, Any]:
        """Record raw device recognition before any project-side normalization.

        ``raw_values`` is the literal algorithm or cloud output.  In
        particular, an offline result such as ``ni3 hao3 kong1 tiao2`` must be
        supplied unchanged for ``keyword``/``intent``.  Normalized text is
        stored separately and can never overwrite the raw tag value.
        """
        self._reject_legacy_fact_api("record_recognition")
        source_name = str(source or "").strip().lower()
        if source_name not in {"offline", "online"}:
            raise ValueError("recognition source must be offline or online")
        raw_payload = dict(raw_values)
        if not raw_payload:
            raise ValueError("raw recognition values cannot be empty")
        refs = [str(item) for item in evidence_refs]
        online_correlation = self._resolve_online_response(raw_payload, case_id=case_id) if source_name == "online" else {}
        evidence = refs[0] if refs else None
        channel = f"{source_name.upper()}_RECOGNITION"
        tags = [
            RawTag(name, value, source="device", evidence=evidence)
            for name, value in raw_payload.items()
        ]
        event_fields = self.artifacts.emit_observation(
            channel,
            tags,
            event="RAW_RECOGNITION",
            normalized=dict(normalized or {}),
            judgement=judgement,
            case_id=case_id,
            recognition_source=source_name,
            evidence_refs=refs,
            # The timeline below renders exactly one configured raw field.
            # ``emit_observation`` remains for structured compatibility only.
            human_log=False,
        )
        if human_log:
            if source_name == "offline":
                selected = presentation_field or next(
                    (name for name in ("keyword", "intent", "text", "asr_text") if raw_payload.get(name) not in (None, "")),
                    "",
                )
                if selected:
                    self.artifacts.timeline_fact(
                        case_id, "OFFLINE_ASR", raw_payload.get(selected), evidence=refs,
                        phase="offline_asr", port=port, role=role, identity=identity,
                        mirror_policy=mirror_policy or "distinct",
                        duration_ms=duration_ms if duration_ms is not None else (judgement.duration_ms if judgement else None),
                    )
            else:
                response_id = self._first_value(raw_payload, "response_id", "responseId", "resultId", "id")
                if response_id:
                    self.artifacts.timeline_fact(
                        case_id, "RESPONSE_ID", response_id, evidence=refs,
                        phase="online_response", port=port, role=role, identity=identity or response_id,
                        mirror_policy=mirror_policy or "distinct",
                    )
                selected = presentation_field or next(
                    (name for name in ("asr_text", "text", "asr", "result") if raw_payload.get(name) not in (None, "")),
                    "",
                )
                if selected:
                    broadcast = self.broadcast_tracker.broadcasts.get(str(broadcast_id or ""), {})
                    e2e_ms = None
                    if broadcast and int(broadcast.get("epoch", -1)) == self.artifacts.current_epoch:
                        e2e_ms = round((time.monotonic() - float(broadcast.get("started_monotonic", time.monotonic()))) * 1000)
                    self.artifacts.timeline_fact(
                        case_id, "ONLINE_ASR", raw_payload.get(selected), evidence=refs,
                        phase="online_response", port=port, role=role, identity=identity or online_correlation.get("request_id", ""),
                        mirror_policy=mirror_policy or "distinct",
                        duration_ms=online_correlation.get("recognition_latency_ms") if online_correlation.get("valid") else None,
                        e2e_duration_ms=e2e_ms,
                    )
        tool_entry = {
            "channel": channel,
            "case_id": case_id,
            "broadcast_id": broadcast_id or "",
            "recognition_source": source_name,
            "raw": raw_payload,
            "normalized": dict(normalized or {}),
            "evidence_refs": refs,
        }
        association = self.observe_recognition(
            raw_payload,
            case_id=case_id,
            broadcast_id=broadcast_id,
            evidence_refs=refs,
            suppress_content_mismatch=suppress_content_mismatch,
            accepted_raw_variants=accepted_raw_variants,
        )
        self._record_health(
            "NO_RECOGNITION",
            failed=False,
            case_id=case_id,
            broadcast_id=association.get("broadcast_id", ""),
        )
        recognition_fact = {
            "raw": raw_payload,
            "source": source_name,
            "raw_exact_status": association.get("raw_exact_status", ""),
            "semantic_status": association.get("semantic_status", ""),
        }
        if source_name == "offline":
            recognition_fact.update({
                "attempts": self._next_attempt(case_id, "offline_recognition"),
                "keyword": raw_payload.get("keyword", ""), "intent": raw_payload.get("intent", ""),
            })
            self._update_case_facts(
                case_id,
                asr=recognition_fact,
                raw_exact_status=association.get("raw_exact_status", ""),
                semantic_status=association.get("semantic_status", ""),
            )
        else:
            recognition_fact.update({
                "attempts": self._next_attempt(case_id, "online_recognition"),
                "request_id": online_correlation.get("request_id", ""),
                "response_id": online_correlation.get("response_id", ""),
                "text": raw_payload.get("text", raw_payload.get("asr_text", "")),
            })
            if not online_correlation.get("valid", False):
                self.artifacts.record_anomaly(
                    "ONLINE_CORRELATION_INVALID",
                    "在线响应缺少唯一有效的同用例请求关联，耗时保持为空。",
                    case_id=case_id,
                    correlation=online_correlation,
                    raw_response=raw_payload,
                    evidence_refs=refs,
                )
            self._update_case_facts(
                case_id,
                online=recognition_fact,
                correlation={
                    "request_id": recognition_fact["request_id"],
                    "response_id": recognition_fact["response_id"],
                    "valid": online_correlation.get("valid", False),
                    "reason": online_correlation.get("reason", ""),
                },
                timing={
                    "recognition_latency_ms": online_correlation.get("recognition_latency_ms")
                    if online_correlation.get("valid", False) else "",
                },
                raw_exact_status=association.get("raw_exact_status", ""),
                semantic_status=association.get("semantic_status", ""),
            )
        if case_id:
            self._recognition_associations.setdefault(case_id, []).append(dict(association))
        return {
            "raw_recognition_log": str(self.artifacts.tool_log_path),
            "observation": event_fields,
            "association": association,
            "online_correlation": online_correlation,
            "status": association["status"],
            "reason": association["reason"],
        }

    def record_wakeup(
        self,
        wake_word: Mapping[str, Any],
        raw_values: Mapping[str, Any],
        *,
        case_id: str = "",
        broadcast_id: str | None = None,
        evidence_refs: Iterable[str] = (),
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Confirm one explicitly selected wake word against its raw device output.

        Callers select the next profile/requirements-table item in order and
        pass it here.  A generic wake-up marker is not sufficient when a
        device has several active wake words.
        """
        self._reject_legacy_fact_api("record_wakeup")
        requirement = dict(wake_word)
        wake_word_id = str(requirement.get("wake_word_id") or "").strip()
        spoken_text = str(requirement.get("spoken_text") or "").strip()
        expected_raw = dict(requirement.get("expected_raw") or {})
        if not wake_word_id or not spoken_text or not expected_raw:
            raise ValueError("wake word requires wake_word_id, spoken_text, and expected_raw")
        expected_current: dict[str, Any] = {}
        if self.wake_word_sequence.requirements:
            try:
                expected_current = self.current_wake_word()
                order_ok = self.wake_word_sequence.matches_current(wake_word_id)
            except RuntimeError:
                # A device reporting another wake-up after all rows have been
                # verified is still a test anomaly, not an uncaught tool error.
                order_ok = False
        else:
            order_ok = True
        if broadcast_id and broadcast_id in self.broadcast_tracker.broadcasts:
            self.broadcast_tracker.broadcasts[broadcast_id]["expected_recognition"] = expected_raw
        observed = dict(raw_values)
        expected_keyword = expected_raw.get("keyword")
        actual_keyword = observed.get("keyword")
        exact_match = all(observed.get(name) == expected for name, expected in expected_raw.items())
        normalized, judgement = judge_wakeup(
            str(actual_keyword) if actual_keyword is not None else None,
            str(expected_keyword) if expected_keyword is not None else None,
            final_observed=bool(observed), duration_ms=duration_ms, evidence_refs=tuple(str(item) for item in evidence_refs),
        )
        if not exact_match and judgement.tool_status == "PASS":
            normalized = {**normalized, "wakeup_status": "FAIL", "active_wake_word_id": wake_word_id}
            judgement = ToolJudgement(
                "FAIL", "EXACT_MATCH", "wakeup_word_raw_output_mismatch",
                expected=expected_raw, actual=observed, duration_ms=duration_ms,
                evidence_refs=tuple(str(item) for item in evidence_refs),
            )
        else:
            normalized = {**normalized, "active_wake_word_id": wake_word_id, "spoken_wake_word": spoken_text}
        if not order_ok:
            normalized = {**normalized, "wakeup_status": "FAIL"}
            judgement = ToolJudgement(
                "FAIL", "ORDERED_EXACT_MATCH", "wake_word_order_violation",
                expected=expected_current, actual=requirement, duration_ms=duration_ms,
                evidence_refs=tuple(str(item) for item in evidence_refs),
            )
        recognition = self.record_recognition(
            observed,
            source="offline",
            case_id=case_id,
            broadcast_id=broadcast_id,
            normalized=normalized,
            judgement=judgement,
            evidence_refs=evidence_refs,
            suppress_content_mismatch=True,
            human_log=False,
        )
        association = dict(recognition["association"])
        wake_payload = {
            "wake_word_id": wake_word_id,
            "spoken_text": spoken_text,
            "expected_raw": expected_raw,
            "raw_values": observed,
            "wakeup_status": judgement.tool_status,
            "attempt": self._next_attempt(case_id, "wake"),
            "recognition": recognition,
        }
        association_reason = str(association.get("reason") or "")
        if association_reason == "UNEXPECTED_RECOGNITION":
            anomaly = self.artifacts.record_anomaly(
                "WAKE_WORD_WITHOUT_PLAYBACK",
                f"唤醒词 {wake_word_id} 在没有可关联播报的情况下出现。",
                case_id=case_id, broadcast_id=broadcast_id or "", **wake_payload,
            )
            wake_payload.setdefault("anomaly_codes", []).append(anomaly["code"])
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": broadcast_id or "",
                    "wake_word_id": wake_word_id,
                })
        elif association_reason == "MULTIPLE_RECOGNITIONS_FOR_PLAYBACK":
            anomaly = self.artifacts.record_anomaly(
                "WAKE_WORD_MULTIPLE_RESULTS_FOR_PLAYBACK",
                f"播报窗口 {association.get('broadcast_id', '')} 出现第 {association.get('recognition_count', 0)} 个唤醒词结果。",
                case_id=case_id, broadcast_id=str(association.get("broadcast_id") or broadcast_id or ""),
                **wake_payload,
            )
            wake_payload.setdefault("anomaly_codes", []).append(anomaly["code"])
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": str(association.get("broadcast_id") or broadcast_id or ""),
                    "wake_word_id": wake_word_id,
                })
        if order_ok and judgement.tool_status == "PASS":
            self.wake_word_sequence.advance()
        if not order_ok:
            anomaly = self.artifacts.record_anomaly(
                "WAKE_WORD_ORDER_VIOLATION",
                f"当前应验证唤醒词 {expected_current.get('wake_word_id', '')}，却提交了 {wake_word_id}。",
                case_id=case_id,
                broadcast_id=broadcast_id or "",
                expected_wake_word=expected_current,
                actual_wake_word=requirement,
            )
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": broadcast_id or "",
                    "wake_word_id": wake_word_id,
                })
        elif judgement.tool_status != "PASS":
            anomaly = self.artifacts.record_anomaly(
                "WAKE_WORD_MISMATCH",
                f"当前唤醒词 {wake_word_id} 的设备原始结果与需求表不一致。",
                case_id=case_id, broadcast_id=broadcast_id or "", **wake_payload,
            )
            if case_id:
                self._recognition_associations.setdefault(case_id, []).append({
                    "status": "FAIL",
                    "reason": anomaly["code"],
                    "broadcast_id": broadcast_id or "",
                    "wake_word_id": wake_word_id,
                })
        self.artifacts.emit(
            "WAKE_WORD_VERIFIED",
            level="INFO" if judgement.tool_status == "PASS" else "ERROR",
            message=f"唤醒词 {wake_word_id} 校验：{judgement.tool_status}。",
            case_id=case_id,
            broadcast_id=broadcast_id or "",
            phase="wake_word",
            status=judgement.tool_status,
            raw=wake_payload,
            evidence=list(evidence_refs),
        )
        self._record_health(
            "NO_WAKEUP",
            failed=judgement.tool_status != "PASS",
            case_id=case_id,
            broadcast_id=broadcast_id or "",
        )
        self._update_case_facts(
            case_id,
            wake={
                "attempts": wake_payload["attempt"],
                "raw": observed.get("keyword", ""),
                "keyword": observed.get("keyword", ""),
                "wake_word_id": wake_word_id,
                "status": judgement.tool_status,
            },
        )
        return wake_payload

    def apply_recognition_contract(self, result: CaseResult, associations: Iterable[Mapping[str, Any]]) -> CaseResult:
        """Upgrade an otherwise successful case to FAIL when its association is abnormal."""
        values = [dict(item) for item in associations]
        codes = [str(item.get("reason")) for item in values if item.get("status") != "PASS"]
        if not codes:
            return result
        status = result.reviewed_status or result.raw_status
        if status in {"PASS", "EXPECTED"}:
            result.reviewed_status = "FAIL"
            result.reason = "; ".join(filter(None, [result.reason, *codes]))
        result.facts = {**result.facts, "broadcast_recognition_associations": values}
        return result

    def wait_observation_window(
        self,
        seconds: float,
        *,
        fetch: Callable[[], tuple[list[Any], bool]],
        parse: Callable[[list[Any]], list[Any]],
        stop_reason: Callable[[], str | None],
        predicate: Callable[[list[Any]], bool] | None = None,
        settle_seconds: float = 0.75,
    ) -> tuple[list[Any], list[Any], str | None, bool]:
        """在有界观察窗口内轮询事件，避免项目层重复实现等待逻辑。"""
        deadline = time.monotonic() + max(0.0, float(seconds))
        events: list[Any] = []
        markers: list[Any] = []
        complete = True
        while time.monotonic() < deadline:
            reason = stop_reason()
            events, complete = fetch()
            markers = parse(events)
            if predicate and predicate(markers):
                settle_deadline = min(deadline, time.monotonic() + max(0.0, settle_seconds))
                while time.monotonic() < settle_deadline and not stop_reason():
                    time.sleep(min(0.1, max(0.0, settle_deadline - time.monotonic())))
                events, complete = fetch()
                return events, parse(events), stop_reason(), complete
            if reason:
                return events, markers, reason, complete
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        events, complete = fetch()
        return events, parse(events), stop_reason(), complete

    def record_case(self, row: dict[str, Any] | CaseResult) -> None:
        """统一交给任务产物对象增量写入 CSV/状态计数。"""
        if isinstance(row, CaseResult) and hasattr(self.artifacts, "record_case"):
            window = self.case_windows.get(row.case_id)
            missing: list[dict[str, Any]] = []
            if window and not window.closed:
                for broadcast_id in window.broadcast_ids:
                    broadcast = self.broadcast_tracker.broadcasts.get(broadcast_id)
                    if broadcast and self.broadcast_tracker.recognition_count.get(broadcast_id, 0) == 0:
                        anomaly = self.artifacts.record_anomaly(
                            "NO_RECOGNITION_FOR_PLAYBACK",
                            f"播报窗口 {broadcast_id} 关闭前未收到最终识别结果。",
                            case_id=row.case_id,
                            broadcast_id=broadcast_id,
                            expected_recognition=broadcast.get("expected_recognition", {}),
                            window=window.to_dict(),
                        )
                        missing.append({"status": "FAIL", "reason": anomaly["code"], "broadcast_id": broadcast_id})
                        self._record_health("NO_RECOGNITION", failed=True, case_id=row.case_id, broadcast_id=broadcast_id)
            associations = [
                *list(row.facts.get("broadcast_recognition_associations", ())),
                *self._recognition_associations.pop(row.case_id, []),
                *missing,
            ]
            if associations:
                row = self.apply_recognition_contract(row, associations)
                self.artifacts.add_check(
                    row.case_id,
                    "broadcast_recognition_contract",
                    "FAIL" if any(item.get("status") != "PASS" for item in associations) else "PASS",
                    required=True,
                    reason=";".join(str(item.get("reason") or "") for item in associations),
                    evidence=[
                        str(ref)
                        for item in associations
                        for ref in item.get("evidence_refs", ())
                    ],
                    associations=associations,
                )
            self.close_case_window(row.case_id)
            generated = self._case_facts.pop(row.case_id, {})
            self._attempts.pop(row.case_id, None)
            row.facts = {**generated, **row.facts}
            self.artifacts.record_case(row)
            return
        if hasattr(self.artifacts, "add_row"):
            self.artifacts.add_row(row)  # type: ignore[arg-type]
            return
        raise TypeError("artifacts must implement record_case or add_row")
