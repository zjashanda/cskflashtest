"""Profile-controlled initialization recovery and restart monitoring."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable, Mapping

try:
    from .core import TaskArtifacts
    from .profile import DeviceProfile, ProfileError
except ImportError:  # direct execution fallback
    from core import TaskArtifacts
    from profile import DeviceProfile, ProfileError


def _patterns(rule: Mapping[str, Any], name: str) -> list[Any]:
    value = rule.get(name, [])
    return list(value) if isinstance(value, list) else []


def _positive_float(rule: Mapping[str, Any], name: str, default: float) -> float:
    try:
        return max(0.01, float(rule.get(name, default)))
    except (TypeError, ValueError):
        return default


def _nonnegative_int(rule: Mapping[str, Any], name: str, default: int) -> int:
    try:
        return max(0, int(rule.get(name, default)))
    except (TypeError, ValueError):
        return default


class ProfileCommandSender:
    """Send one profile-approved command with observable verification and retry."""

    def __init__(
        self,
        profile: DeviceProfile,
        artifacts: TaskArtifacts,
        writer: Callable[[str, str], str | None],
        manager: Any | None = None,
    ):
        self.profile = profile
        self.artifacts = artifacts
        self.writer = writer
        self.manager = manager

    def _resolve_port(self, rule: Mapping[str, Any], role: str | None, port: str | None) -> str | None:
        resolved = (port or str(rule.get("port", ""))).strip()
        if not resolved:
            return None
        declared_roles = {str(item.get("role")): item for item in self.profile.ports if item.get("role")}
        if role and role in declared_roles:
            allowed_port = str(declared_roles[role].get("port", "")).strip()
            if allowed_port and resolved != allowed_port:
                raise ProfileError(f"port {resolved} does not match profile role {role}")
        return resolved

    def _snapshot(self, port: str) -> dict[str, int]:
        if self.manager is None or not hasattr(self.manager, "snapshot"):
            return {port: 0}
        snapshot = self.manager.snapshot(port)
        return {port: int(snapshot.get(port, 0))}

    def _event_matches(self, patterns: Iterable[Any], event: Any, *, phase: str) -> bool:
        values = list(patterns)
        if not values:
            return False
        scope = {
            "port": getattr(event, "port", None),
            "role": getattr(event, "role", None),
            "phase": phase,
            "monotonic_seconds": getattr(event, "monotonic_seconds", None),
        }
        decisions = self.profile.match_details(values, str(getattr(event, "line", "")), **scope)
        matched = any(ok for _rule, ok, _reason in decisions)
        if not matched:
            rejected = [reason for _rule, _ok, reason in decisions if reason not in {"pattern_not_matched", "matched"}]
            if rejected:
                self.artifacts.emit(
                    "MARKER_SCOPE_REJECTED",
                    level="WARN",
                    message="串口 marker 命中文本但被作用域或去重规则拒绝。",
                    phase=phase,
                    port=scope["port"],
                    role=scope["role"],
                    raw_line=str(getattr(event, "line", "")),
                    reason=";".join(rejected),
                    evidence=ProfileCommandSender._evidence_refs([event]),
                )
        return matched

    def _matching_events(
        self,
        patterns: Iterable[Any],
        *,
        port: str,
        cursors: Mapping[str, int],
        timeout_s: float,
        phase: str,
    ) -> list[Any]:
        values = list(patterns)
        if not values or self.manager is None or not hasattr(self.manager, "wait_for"):
            return []
        events = self.manager.wait_for(
            lambda items: any(self._event_matches(values, item, phase=phase) for item in items),
            timeout_s,
            cursors={port: int(cursors.get(port, 0))},
        )
        return [item for item in events if self._event_matches(values, item, phase=phase)]

    @staticmethod
    def _evidence_refs(events: Iterable[Any]) -> list[str]:
        refs: list[str] = []
        for item in events:
            port = str(getattr(item, "port", "unknown"))
            role = str(getattr(item, "role", "unknown"))
            cursor = getattr(item, "cursor", "")
            refs.append(f"serial_logs/serial_{port}_{role}.log#{cursor}")
        return refs

    def _blocked_result(
        self,
        command: str,
        reason: str,
        *,
        role: str | None,
        port: str | None,
        status: str = "BLOCKED_COMMAND_POLICY",
    ) -> dict[str, Any]:
        self.artifacts.emit(
            "SERIAL_COMMAND_BLOCKED", level="ERROR", message=reason, task_log=True,
            command=command, role=role, port=port,
        )
        return {"status": status, "command": command, "reason": reason}

    def send(
        self,
        command: str,
        *,
        role: str | None = None,
        port: str | None = None,
        restart_guard: Callable[[], Any | None] | None = None,
        action_label: str = "profile 许可命令",
    ) -> dict[str, Any]:
        """Send and verify an approved command without treating silence as success."""
        try:
            rule = dict(self.profile.assert_command_allowed(command, role))
        except ProfileError as error:
            return self._blocked_result(command, str(error), role=role, port=port)
        try:
            resolved_port = self._resolve_port(rule, role, port)
            if not resolved_port:
                raise ProfileError("approved command has no target port")
        except ProfileError as error:
            return self._blocked_result(
                command,
                str(error),
                role=role,
                port=port,
                status="BLOCKED_PORT_POLICY",
            )

        success_patterns = _patterns(rule, "success_patterns")
        evidence_patterns = _patterns(rule, "evidence_patterns")
        if not success_patterns and not evidence_patterns:
            return self._blocked_result(
                command,
                "approved command requires success_patterns or evidence_patterns for verification",
                role=role,
                port=resolved_port,
            )

        retries = _nonnegative_int(rule, "retries", 1)
        max_attempts = retries + 1
        ack_timeout_s = _positive_float(rule, "timeout_s", 3.0)
        evidence_timeout_s = _positive_float(rule, "evidence_timeout_s", ack_timeout_s)
        retry_delay_s = _positive_float(rule, "retry_delay_s", 0.2)
        attempts: list[dict[str, Any]] = []

        for attempt in range(1, max_attempts + 1):
            restart_event = restart_guard() if restart_guard else None
            if restart_event:
                return {
                    "status": "CANCELLED",
                    "command": command,
                    "role": role,
                    "port": resolved_port,
                    "attempts": attempts,
                    "reason": "restart_detected_during_recovery",
                    "restart_event": restart_event,
                }
            started = time.monotonic()
            cursors = self._snapshot(resolved_port)
            self.artifacts.emit(
                "SERIAL_COMMAND_ATTEMPT",
                message=f"发送{action_label}，第 {attempt}/{max_attempts} 次: {command}。",
                task_log=True,
                command=command,
                role=role,
                port=resolved_port,
                action_label=action_label,
                attempt=attempt,
                max_attempts=max_attempts,
                cursors=cursors,
            )
            direct_reply = ""
            write_error = ""
            try:
                direct_reply = str(self.writer(command, resolved_port) or "")
            except Exception as error:
                write_error = f"{type(error).__name__}: {error}"

            validation_source = ""
            evidence_events: list[Any] = []
            failure_reason = ""
            if write_error:
                failure_reason = "command_write_failed"
            elif direct_reply.strip():
                if success_patterns and self.profile.match_any(
                    success_patterns, direct_reply, port=resolved_port, role=role, phase="command_direct_ack",
                ):
                    validation_source = "direct_ack"
                else:
                    failure_reason = "direct_ack_not_matched"
            else:
                def classify(events: Iterable[Any]) -> tuple[str, list[Any]]:
                    for event in events:
                        if success_patterns and self._event_matches(success_patterns, event, phase="command_serial_ack"):
                            return "serial_ack", [event]
                        if evidence_patterns and self._event_matches(evidence_patterns, event, phase="command_evidence"):
                            return "serial_evidence", [event]
                    return "", []

                # A reboot can expose only the later initialization marker.
                # Wait for an acknowledgement and an approved evidence marker
                # together so a missing optional acknowledgement does not add
                # its full timeout before recovery can begin.
                if success_patterns and evidence_patterns:
                    shared_timeout_s = min(ack_timeout_s, evidence_timeout_s)
                    shared_events = self._matching_events(
                        [*success_patterns, *evidence_patterns],
                        port=resolved_port,
                        cursors=cursors,
                        timeout_s=shared_timeout_s,
                        phase="command_ack_or_evidence",
                    )
                    validation_source, evidence_events = classify(shared_events)
                    if not validation_source:
                        longer_patterns = success_patterns if ack_timeout_s > evidence_timeout_s else evidence_patterns
                        longer_phase = "command_serial_ack" if ack_timeout_s > evidence_timeout_s else "command_evidence"
                        longer_timeout_s = abs(ack_timeout_s - evidence_timeout_s)
                        if longer_timeout_s > 0:
                            longer_events = self._matching_events(
                                longer_patterns,
                                port=resolved_port,
                                cursors=cursors,
                                timeout_s=longer_timeout_s,
                                phase=longer_phase,
                            )
                            validation_source, evidence_events = classify(longer_events)
                elif success_patterns:
                    validation_source, evidence_events = classify(self._matching_events(
                        success_patterns,
                        port=resolved_port,
                        cursors=cursors,
                        timeout_s=ack_timeout_s,
                        phase="command_serial_ack",
                    ))
                else:
                    validation_source, evidence_events = classify(self._matching_events(
                        evidence_patterns,
                        port=resolved_port,
                        cursors=cursors,
                        timeout_s=evidence_timeout_s,
                        phase="command_evidence",
                    ))
                if not validation_source:
                    failure_reason = "no_ack_or_evidence" if evidence_patterns else "no_ack"

            result = {
                "command": command,
                "action_label": action_label,
                "role": role,
                "port": resolved_port,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "reply": direct_reply,
                "write_error": write_error,
                "validation_source": validation_source,
                "success_patterns": success_patterns,
                "evidence_patterns": evidence_patterns,
                "evidence_refs": self._evidence_refs(evidence_events),
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            if validation_source:
                result["status"] = "PASS"
                attempts.append(result)
                self.artifacts.emit(
                    "SERIAL_COMMAND_VALIDATED",
                    message=f"{action_label}已验证成功: {command}；来源={validation_source}。",
                    task_log=True,
                    **result,
                )
                return {**result, "attempts": attempts}

            result.update({"status": "FAIL", "reason": failure_reason})
            attempts.append(result)
            level = "WARN" if attempt < max_attempts else "ERROR"
            self.artifacts.emit(
                "SERIAL_COMMAND_ATTEMPT_FAILED",
                level=level,
                message=f"{action_label}第 {attempt}/{max_attempts} 次未验证成功: {command}；{failure_reason}。",
                task_log=True,
                **result,
            )
            if attempt < max_attempts:
                restart_event = restart_guard() if restart_guard else None
                if restart_event:
                    return {
                        "status": "CANCELLED",
                        "command": command,
                        "role": role,
                        "port": resolved_port,
                        "attempts": attempts,
                        "reason": "restart_detected_during_recovery",
                        "restart_event": restart_event,
                    }
                self.artifacts.emit(
                    "SERIAL_COMMAND_RETRY",
                    level="WARN",
                    message=f"{action_label}将在 {retry_delay_s:.2f}s 后重试: {command}。",
                    task_log=True,
                    command=command,
                    role=role,
                    port=resolved_port,
                    next_attempt=attempt + 1,
                    max_attempts=max_attempts,
                )
                time.sleep(retry_delay_s)

        final = {**attempts[-1], "attempts": attempts}
        self.artifacts.emit(
            "SERIAL_COMMAND_RECOVERY_FAILED",
            level="ERROR",
            message=f"{action_label}重试耗尽仍未恢复: {command}。",
            task_log=True,
            **final,
        )
        return final


class ProfileRecoveryStateMachine:
    """Wait for completed initialization before recovering profile-approved commands."""

    def __init__(self, profile: DeviceProfile, artifacts: TaskArtifacts, manager: Any):
        self.profile = profile
        self.artifacts = artifacts
        self.manager = manager
        self.state = "CREATED"

    @staticmethod
    def _evidence_refs(events: Iterable[Any]) -> list[str]:
        return ProfileCommandSender._evidence_refs(events)

    def _complete(self, result: dict[str, Any]) -> dict[str, Any]:
        level = "INFO" if result["status"] == "PASS" else ("WARN" if result["status"] == "CANCELLED" else "ERROR")
        self.artifacts.emit(
            "PROFILE_RECOVERY_RESULT",
            level=level,
            message=f"初始化恢复 {result['status']}；原因={result['reason']}。",
            task_log=True,
            **result,
        )
        if result["status"] not in {"PASS", "CANCELLED"}:
            self.artifacts.record_anomaly(
                "INITIALIZATION_RECOVERY_FAILED",
                "初始化命令恢复未完成，已保留命令尝试、回执和旁证。",
                recovery=result,
            )
            self.artifacts.emit(
                "INITIALIZATION_RECOVERY_CONTINUES",
                level="WARN",
                message="初始化恢复失败已记录；不属于任务退出条件，继续可执行用例。",
                task_log=True,
                recovery_reason=result.get("recovery_reason", ""),
                recovery_state=result.get("state", ""),
            )
        return result

    def _event_matches(self, patterns: Iterable[Any], event: Any, *, phase: str) -> bool:
        return ProfileCommandSender(self.profile, self.artifacts, lambda _command, _port: "", self.manager)._event_matches(
            patterns, event, phase=phase,
        )

    @staticmethod
    def _cancelled_result(
        *,
        recovery_reason: str,
        epoch: int | None,
        state: str,
        restart_event: Any,
        commands: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "CANCELLED",
            "reason": "restart_detected_during_recovery",
            "state": state,
            "recovery_reason": recovery_reason,
            "epoch": epoch,
            "commands": list(commands or ()),
            "restart_event": restart_event,
        }

    def _wait_stable(self, stable_for_s: float, restart_guard: Callable[[], Any | None] | None) -> Any | None:
        deadline = time.monotonic() + stable_for_s
        while time.monotonic() < deadline:
            restart_event = restart_guard() if restart_guard else None
            if restart_event:
                return restart_event
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return restart_guard() if restart_guard else None

    def run(
        self,
        timeout_s: float = 5.0,
        *,
        cursors: Mapping[str, int] | None = None,
        initialization_events: Iterable[Any] | None = None,
        recovery_reason: str = "startup",
        epoch: int | None = None,
        restart_guard: Callable[[], Any | None] | None = None,
        stable_for_s: float | None = None,
        case_id: str = "",
    ) -> dict[str, Any]:
        enabled = [
            dict(item) for item in self.profile.payload.get("commands", [])
            if isinstance(item, Mapping) and item.get("safe_init", True)
        ]
        if not self.manager.handles:
            self.state = "BLOCKED"
            return self._complete({
                "status": "BLOCKED",
                "reason": "no serial port is open",
                "state": self.state,
                "recovery_reason": recovery_reason,
                "commands": [],
            })
        if not enabled:
            self.state = "READY"
            return self._complete({
                "status": "PASS",
                "reason": "no safe initialization commands configured",
                "state": self.state,
                "recovery_reason": recovery_reason,
                "commands": [],
            })
        if not self.profile.initialization_patterns:
            self.state = "BLOCKED"
            return self._complete({
                "status": "BLOCKED",
                "reason": "initialization_patterns are required before safe initialization commands",
                "state": self.state,
                "recovery_reason": recovery_reason,
                "commands": [],
            })

        start_cursors = dict(cursors or {port: 0 for port in self.manager.handles})
        restart_event = restart_guard() if restart_guard else None
        if restart_event:
            self.state = "CANCELLED"
            return self._complete(self._cancelled_result(
                recovery_reason=recovery_reason, epoch=epoch, state=self.state, restart_event=restart_event,
            ))
        observed_events = list(initialization_events or ())
        if observed_events:
            # A caller may have just completed a bounded initialization wait.
            # Reuse only evidence that still matches this same profile, rather
            # than requiring a second boot marker that an already-ready device
            # will never emit.
            self.state = "INIT_OBSERVED"
            events = observed_events
            init_events = [
                item for item in events
                if self._event_matches(self.profile.initialization_patterns, item, phase="initialization")
            ]
            self.artifacts.emit(
                "PROFILE_RECOVERY_USE_OBSERVED_INIT",
                message="复用已观测到的初始化标记，稳定后发送恢复命令。",
                task_log=True,
                recovery_reason=recovery_reason,
                marker_count=len(init_events),
                evidence=self._evidence_refs(init_events),
                epoch=epoch if epoch is not None else self.artifacts.current_epoch,
            )
        else:
            self.state = "WAIT_INIT"
            self.artifacts.emit(
                "PROFILE_RECOVERY_WAIT_INIT",
                message="等待设备完成初始化后再发送恢复命令。",
                task_log=True,
                recovery_reason=recovery_reason,
                initialization_patterns=self.profile.initialization_patterns,
                timeout_s=timeout_s,
                cursors=start_cursors,
                epoch=epoch if epoch is not None else self.artifacts.current_epoch,
            )
            events = self.manager.wait_for(
                lambda items: (
                    bool(restart_guard and restart_guard())
                    or any(self._event_matches(self.profile.initialization_patterns, item, phase="initialization") for item in items)
                ),
                timeout_s,
                cursors=start_cursors,
            )
            init_events = [
                item for item in events
                if self._event_matches(self.profile.initialization_patterns, item, phase="initialization")
            ]
        restart_event = restart_guard() if restart_guard else None
        if restart_event:
            self.state = "CANCELLED"
            return self._complete(self._cancelled_result(
                recovery_reason=recovery_reason, epoch=epoch, state=self.state, restart_event=restart_event,
            ))
        if not init_events:
            self.state = "BLOCKED"
            return self._complete({
                "status": "BLOCKED",
                "reason": "initialization marker missing",
                "state": self.state,
                "recovery_reason": recovery_reason,
                "marker_count": len(events),
                "initialization_evidence_refs": [],
                "commands": [],
            })

        # The marker regex is profile-owned; the main log only receives the
        # extracted lifecycle fact and a separate framework judgement.
        first_init = init_events[0]
        self.artifacts.emit_fact(
            "INIT_READY", "ready", tag="DEVICE",
            evidence=ProfileCommandSender._evidence_refs(init_events),
            port=getattr(first_init, "port", ""),
            role=getattr(first_init, "role", ""),
            epoch=epoch if epoch is not None else self.artifacts.current_epoch,
            case_id=case_id,
        )
        self.artifacts.emit_judgement(
            "初始化", "PASS", tag="RESULT",
            evidence=ProfileCommandSender._evidence_refs(init_events),
        )

        recovery = self.profile.recovery if hasattr(self.profile, "recovery") else {}
        resolved_stable_for_s = (
            max(0.0, float(stable_for_s))
            if stable_for_s is not None
            else (_positive_float(recovery, "stable_for_s", 0.0) if recovery.get("stable_for_s") else 0.0)
        )
        if resolved_stable_for_s:
            self.state = "STABILIZING"
            self.artifacts.emit(
                "PROFILE_RECOVERY_STABILIZING",
                message=f"初始化 marker 已到达，等待 {resolved_stable_for_s:.2f}s 稳定窗口。",
                epoch=epoch if epoch is not None else self.artifacts.current_epoch,
                phase="initialization_recovery",
                elapsed_ms=round(resolved_stable_for_s * 1000),
                evidence=ProfileCommandSender._evidence_refs(init_events),
            )
            restart_event = self._wait_stable(resolved_stable_for_s, restart_guard)
            if restart_event:
                self.state = "CANCELLED"
                return self._complete(self._cancelled_result(
                    recovery_reason=recovery_reason, epoch=epoch, state=self.state, restart_event=restart_event,
                ))
            self.artifacts.emit_fact(
                "INIT_STABLE",
                "ready",
                tag="DEVICE",
                evidence=ProfileCommandSender._evidence_refs(init_events),
                case_id=case_id,
            )

        self.state = "SEND_APPROVED"
        sender = ProfileCommandSender(self.profile, self.artifacts, self.manager.write, self.manager)
        results: list[dict[str, Any]] = []
        for rule in enabled:
            restart_event = restart_guard() if restart_guard else None
            if restart_event:
                self.state = "CANCELLED"
                return self._complete(self._cancelled_result(
                    recovery_reason=recovery_reason, epoch=epoch, state=self.state,
                    restart_event=restart_event, commands=results,
                ))
            command = str(rule.get("command", "")).strip()
            roles = rule.get("roles") or [None]
            role = str(roles[0]) if roles and roles[0] else None
            target = next((spec.port for spec in self.manager.ports if role is None or spec.role == role), None)
            command_result = sender.send(
                command,
                role=role,
                port=target,
                restart_guard=restart_guard,
                action_label="初始化恢复命令",
            )
            results.append(command_result)
            if command_result.get("status") == "CANCELLED":
                self.state = "CANCELLED"
                return self._complete(self._cancelled_result(
                    recovery_reason=recovery_reason, epoch=epoch, state=self.state,
                    restart_event=command_result.get("restart_event"), commands=results,
                ))

        all_passed = bool(results) and all(item.get("status") == "PASS" for item in results)
        self.state = "READY" if all_passed else "BLOCKED"
        return self._complete({
            "status": "PASS" if all_passed else "BLOCKED",
            "reason": "all initialization commands verified" if all_passed else "one or more initialization commands were not verified",
            "state": self.state,
            "recovery_reason": recovery_reason,
            "marker_count": len(events),
            "initialization_source": "observed" if observed_events else "waited",
            "initialization_evidence_refs": self._evidence_refs(init_events),
            "commands": results,
        })


class ProfileRestartRecoveryMonitor:
    """Continuously detect profile restart markers and rerun verified recovery."""

    def __init__(
        self,
        profile: DeviceProfile,
        artifacts: TaskArtifacts,
        manager: Any,
        *,
        initialization_timeout_s: float = 10.0,
        poll_interval_s: float = 0.1,
        stable_for_s: float = 0.0,
    ) -> None:
        self.profile = profile
        self.artifacts = artifacts
        self.manager = manager
        self.initialization_timeout_s = max(0.1, float(initialization_timeout_s))
        self.poll_interval_s = max(0.02, float(poll_interval_s))
        self.stable_for_s = max(0.0, float(stable_for_s))
        self.epoch = artifacts.current_epoch
        self.cursors: dict[str, int] = {}
        self.history: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.profile.restart_patterns:
            self.artifacts.emit(
                "RESTART_RECOVERY_MONITOR_SKIPPED",
                message="profile 未配置 restart_patterns，跳过重启恢复监控。",
                task_log=True,
            )
            return
        self.cursors = dict(self.manager.snapshot())
        self._thread = threading.Thread(target=self._run, name="lstest-restart-recovery", daemon=True)
        self._thread.start()
        self.artifacts.emit(
            "RESTART_RECOVERY_MONITOR_STARTED",
            message="已启动设备重启后的初始化恢复监控。",
            task_log=True,
            restart_patterns=self.profile.restart_patterns,
            cursors=self.cursors,
        )

    def _run(self) -> None:
        while not self._stop.is_set() and not self.manager.stop_event.is_set():
            self.poll()
            self._stop.wait(self.poll_interval_s)

    def poll(self) -> list[dict[str, Any]]:
        """Process currently available restart markers; exposed for deterministic tests."""
        if not self.profile.restart_patterns or self._stop.is_set():
            return []
        recovered: list[dict[str, Any]] = []
        while not self._stop.is_set():
            events = self.manager.since(dict(self.cursors))
            restart_event = next((
                item for item in events
                if self._event_matches(item, phase="restart")
            ), None)
            if restart_event is None:
                return recovered
            event = restart_event
            port = str(getattr(event, "port", ""))
            if port:
                self.cursors[port] = int(getattr(event, "cursor", self.cursors.get(port, 0)))
            baseline = dict(self.cursors)
            self.epoch += 1
            self.artifacts.set_epoch(self.epoch, reason="restart_marker")
            self.artifacts.emit(
                "DEVICE_RESTART_DETECTED",
                level="WARN",
                message="检测到设备重启 marker，等待重新初始化并恢复配置命令。",
                task_log=True,
                restart_line=event.line,
                restart_port=getattr(event, "port", ""),
                restart_role=getattr(event, "role", ""),
                restart_cursor=getattr(event, "cursor", 0),
                evidence_refs=ProfileCommandSender._evidence_refs([event]),
                epoch=self.epoch,
            )
            def restart_guard() -> Any | None:
                for candidate in self.manager.since(dict(self.cursors)):
                    if self._event_matches(candidate, phase="restart"):
                        return candidate
                return None

            recovery = ProfileRecoveryStateMachine(self.profile, self.artifacts, self.manager).run(
                self.initialization_timeout_s,
                cursors=baseline,
                recovery_reason="restart",
                epoch=self.epoch,
                restart_guard=restart_guard,
                stable_for_s=self.stable_for_s,
            )
            self.history.append(recovery)
            recovered.append(recovery)
            if recovery.get("status") == "CANCELLED":
                self.artifacts.emit(
                    "PROFILE_RECOVERY_CANCELLED",
                    level="WARN",
                    message="恢复期间检测到新的重启，旧 epoch 恢复已取消。",
                    epoch=self.epoch,
                    reason="restart_detected_during_recovery",
                    raw={"restart_event": self._event_payload(recovery.get("restart_event"))},
                )
                continue
            return recovered
        return recovered

    def _event_matches(self, event: Any, *, phase: str) -> bool:
        sender = ProfileCommandSender(self.profile, self.artifacts, lambda _command, _port: "", self.manager)
        return sender._event_matches(self.profile.restart_patterns, event, phase=phase)

    @staticmethod
    def _event_payload(event: Any) -> dict[str, Any]:
        return {
            "port": getattr(event, "port", ""),
            "role": getattr(event, "role", ""),
            "cursor": getattr(event, "cursor", ""),
            "line": getattr(event, "line", ""),
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(2.0, self.initialization_timeout_s + self.poll_interval_s))
        self.artifacts.emit(
            "RESTART_RECOVERY_MONITOR_STOPPED",
            message="设备重启恢复监控已停止。",
            task_log=True,
            recovery_count=len(self.history),
        )
