"""Safe, profile-aware initialization smoke orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .core import ConnectionSpec, DeviceRuntime, TaskArtifacts
    from .playback import PlaybackBackend
    from .profile import DeviceProfile, ProfileError
    from .serial_capture import SerialManager
    from .shell import ProfileRecoveryStateMachine
except ImportError:  # direct execution fallback
    from core import ConnectionSpec, DeviceRuntime, TaskArtifacts
    from playback import PlaybackBackend
    from profile import DeviceProfile, ProfileError
    from serial_capture import SerialManager
    from shell import ProfileRecoveryStateMachine


def collect_initialization_evidence(
    capture: SerialManager,
    profile: DeviceProfile,
    *,
    timeout_s: float,
    cursors: dict[str, int],
) -> list[Any]:
    """Return only profile-validated initialization records from one wait window."""
    matched: list[Any] = []
    inspected: set[tuple[str, int]] = set()

    def has_initialization(events: list[Any]) -> bool:
        for item in events:
            identity = (str(getattr(item, "port", "")), int(getattr(item, "cursor", 0)))
            if identity in inspected:
                continue
            inspected.add(identity)
            if profile.match_any(
                profile.initialization_patterns,
                str(getattr(item, "line", "")),
                port=getattr(item, "port", None),
                role=getattr(item, "role", None),
                phase="smoke_initialization",
                monotonic_seconds=getattr(item, "monotonic_seconds", None),
            ):
                matched.append(item)
        return bool(matched)

    capture.wait_for(has_initialization, timeout_s, cursors=cursors)
    return matched


def run_smoke(connection: ConnectionSpec, profile_path: Path, *, hardware: bool = False) -> dict[str, Any]:
    artifacts = TaskArtifacts(connection.result_root, "lstest_basic_smoke", ["case_id", "raw_status", "reviewed_status", "reason", "facts", "evidence"])
    artifacts.configure({"connection": connection.to_dict(), "profile_path": str(profile_path), "hardware": hardware})
    status = "PASS"
    reason = "dry-run preflight complete"
    capture = None
    runtime = None
    try:
        profile = DeviceProfile.load(profile_path)
        artifacts.configure_health_policy(profile.health_policy)
        # Generic smoke has no business corpus, but it must still create the
        # immutable case ledger before probing hardware or sending commands.
        artifacts.freeze_cases([], profile_version=profile.schema_version, profile_sha256=profile.sha256)
        artifacts.set_capability("profile", "PASS", "profile loaded", profile_id=profile.profile_id, profile_sha256=profile.sha256)
        capture = SerialManager(connection.ports, artifacts)
        player = PlaybackBackend(artifacts, connection.playback_device_key)
        runtime = DeviceRuntime(connection, profile, artifacts)
        runtime.preflight()
        if not connection.ports:
            artifacts.set_capability("serial", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("initialization", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("wake", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("offline_asr", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("offline_intent", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("online_asr", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("online_correlation", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
            artifacts.set_capability("player_evidence", "BLOCKED", "BLOCKED_SERIAL_UNAVAILABLE")
        if hardware and not player.probe():
            artifacts.set_capability("player", "BLOCKED", "playback probe failed")
            status, reason = "BLOCKED", "playback probe failed"
        elif not hardware:
            artifacts.emit("SMOKE_DRY_RUN", message="无硬件 smoke 只完成配置和资源检查，未打开设备。", task_log=True)
            artifacts.set_capability("player", "UNAVAILABLE", "hardware probe not requested")
            artifacts.set_capability("basic_voice", "UNAVAILABLE", "generic runtime has no project smoke audio")
        else:
            capture.start()
            if capture.open_failures:
                artifacts.set_capability("serial", "BLOCKED", "one or more declared ports could not be opened", open_failures=capture.open_failures)
                status, reason = "BLOCKED", "serial open failed"
            else:
                artifacts.set_capability("serial", "PASS", "declared ports opened", ports=list(connection.to_dict()["ports"]))
            init_cursor = capture.snapshot()
            init_events = collect_initialization_evidence(
                capture,
                profile,
                timeout_s=3.0,
                cursors=init_cursor,
            ) if not capture.open_failures else []
            init_ok = bool(init_events)
            artifacts.set_capability("initialization", "PASS" if init_ok else "WARN", "profile initialization marker observed" if init_ok else "no new initialization marker in smoke window", marker_count=len(init_events))
            artifacts.set_capability("player", "PASS", "explicit player probe passed")
            if status != "BLOCKED":
                recovery = runtime.recover_initialization(
                    capture,
                    initialization_events=init_events if init_ok else None,
                )
                command_results = recovery.get("commands", [])
                if command_results:
                    command_statuses = {str(item.get("status")) for item in command_results}
                    recovery_status = "PASS" if command_statuses == {"PASS"} else ("WARN" if command_statuses <= {"PASS", "WARN"} else "FAIL")
                    artifacts.set_capability("log_recovery", recovery_status, "profile commands sent", commands=command_results)
                elif recovery.get("status") == "BLOCKED":
                    artifacts.set_capability("log_recovery", "BLOCKED", recovery.get("reason", "profile recovery blocked"), recovery=recovery)
                artifacts.set_capability("basic_voice", "WARN", "project smoke audio must be supplied by adapter")
                for name in ("wake", "offline_asr", "offline_intent", "online_asr", "online_correlation", "player_evidence"):
                    artifacts.set_capability(name, "UNAVAILABLE", "project adapter audio/oracle not configured")
                artifacts.emit("SMOKE_LIMITED", message="基础硬件 smoke 已启动；日志恢复和播放器探测完成，唤醒/业务语义由项目场景提供安全音频。", task_log=True)
                status, reason = "WARN", "generic runtime initialized; project smoke audio not configured"
    except ProfileError as error:
        artifacts.emit("PROFILE_BLOCKED", level="ERROR", message=str(error), task_log=True)
        status, reason = "BLOCKED", "profile invalid"
    except Exception as error:
        artifacts.add_sticky("TOOL_EXCEPTION", str(error))
        status, reason = "FAIL", str(error)
    finally:
        if capture is not None:
            capture.stop()
        if runtime is not None:
            runtime.close()
    return artifacts.finalize(status, reason)
