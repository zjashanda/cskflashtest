"""Playback probe and lifecycle abstraction."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .core import TaskArtifacts, now_iso
except ImportError:  # direct execution fallback
    from core import TaskArtifacts, now_iso


class PlaybackBackend:
    def __init__(self, artifacts: TaskArtifacts, device_key: str | None, script: Path | None = None):
        self.artifacts = artifacts
        self.device_key = device_key
        self.script = script or Path(__file__).with_name("listenai_play.py")
        self.normalizer = PlayerEventNormalizer()
        self.lifecycle: list[dict[str, Any]] = []
        self._lifecycle_times: dict[tuple[str, str], float] = {}
        self.last_playback: dict[str, Any] = {}

    @property
    def target_label(self) -> str:
        """Return an auditable target without inventing a default-device key."""
        return self.device_key or "system_default_render"

    @property
    def target_mode(self) -> str:
        return "specified_device_key" if self.device_key else "system_default_render"

    def _player_command(self, action: str, *, audio: Path | None = None) -> list[str]:
        command = [sys.executable, str(self.script), action]
        if audio is not None:
            command.extend(["--audio-file", str(audio)])
        # No key deliberately delegates to the operating system's current
        # default render endpoint. A supplied key is passed through intact so
        # the bundled player rejects unavailable/ambiguous hardware instead
        # of silently falling back to the default device.
        if self.device_key:
            command.extend(["--device-key", self.device_key])
        return command

    def _record_lifecycle(
        self,
        lifecycle_status: str,
        *,
        event: str,
        message: str,
        level: str = "INFO",
        case_id: str = "",
        broadcast_id: str = "",
        audio: Path | None = None,
        source: str = "host_player",
        **fields: Any,
    ) -> dict[str, Any]:
        """Persist one player state without claiming device-side playback."""
        record = {
            "at": now_iso(),
            "event": event,
            "level": level,
            "message": message,
            "lifecycle_status": lifecycle_status,
            "source": source,
            "case_id": case_id,
            "broadcast_id": broadcast_id,
            "audio": str(audio) if audio is not None else "",
            "playback_target": self.target_label,
            "playback_target_mode": self.target_mode,
            "player_script": str(self.script),
            **fields,
        }
        # Host player lifecycle is internal evidence.  The human timeline is
        # driven by the device-side marker regex submitted by the adapter.
        # ScenarioRuntime records host failures as normal ERROR facts.
        return record

    @staticmethod
    def _timeout_output(error: subprocess.TimeoutExpired) -> str:
        """Keep partial child output as evidence when a bounded wait expires."""
        values = [
            getattr(error, "stdout", None),
            getattr(error, "stderr", None),
            getattr(error, "output", None),
        ]
        output = "".join(str(item) for item in values if item)
        return output or str(error)

    def probe(self) -> bool:
        if not self.script.is_file():
            self._record_lifecycle(
                "BLOCKED", event="PLAYER_BLOCKED", level="ERROR", message="播放工具不存在。",
                reason="player_script_missing", tool_status="BLOCKED",
            )
            return False
        started = time.monotonic()
        self._record_lifecycle(
            "PROBE_REQUESTED", event="PLAYER_PROBE_REQUESTED", message="请求探测播放设备。",
            timeout_s=30.0, tool_status="PENDING",
        )
        try:
            completed = subprocess.run(
                self._player_command("probe"), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, check=False,
            )
        except subprocess.TimeoutExpired as error:
            duration_s = round(time.monotonic() - started, 3)
            self.artifacts.record_anomaly(
                "PLAYER_PROBE_TIMEOUT", "播放器探测超时。",
                evidence_refs=(), handling="记录后继续", output=self._timeout_output(error)[-500:],
            )
            self._record_lifecycle(
                "PROBE_TIMEOUT", event="PLAYER_PROBE_TIMEOUT", level="ERROR",
                message=f"播放器探测超时，已等待 {duration_s:.3f}s。", timeout_s=30.0,
                duration_s=duration_s, evidence="tool.log", tool_status="FAIL",
            )
            return False
        except OSError as error:
            self._record_lifecycle(
                "PROBE_ERROR", event="PLAYER_PROBE_FAIL", level="ERROR", message=f"播放器探测失败: {error}",
                error=f"{type(error).__name__}: {error}", tool_status="FAIL",
            )
            return False
        ok = completed.returncode == 0
        output = completed.stdout or completed.stderr or ""
        duration_s = round(time.monotonic() - started, 3)
        self._record_lifecycle(
            "PROBE_COMPLETED" if ok else "PROBE_FAILED",
            event="PLAYER_PROBE",
            level="INFO" if ok else "ERROR",
            message=f"播放器探测{'通过' if ok else '失败'}。",
            returncode=completed.returncode,
            tool_output=output[-500:],
            evidence="tool.log",
            duration_s=duration_s,
            tool_status="PASS" if ok else "FAIL",
        )
        return ok

    def play(
        self,
        audio: Path,
        *,
        case_id: str = "",
        broadcast_id: str = "",
        timeout: float = 120.0,
    ) -> bool:
        """Run the host player and record host/device evidence separately."""
        self.last_playback = {
            "status": "UNKNOWN",
            "case_id": case_id,
            "broadcast_id": broadcast_id,
            "audio": str(audio),
        }
        if not self.script.is_file():
            self.last_playback.update({"status": "BLOCKED", "reason": "player_script_missing"})
            self._record_lifecycle(
                "BLOCKED", event="PLAYER_BLOCKED", level="ERROR", message="播放工具不存在。",
                case_id=case_id, broadcast_id=broadcast_id, audio=audio,
                reason="player_script_missing", tool_status="BLOCKED",
            )
            return False
        started = time.monotonic()
        timeout_s = max(0.1, float(timeout))
        self._record_lifecycle(
            "REQUESTED", event="PLAYER_REQUESTED", message=f"请求播放音频: {audio.name}。",
            case_id=case_id, broadcast_id=broadcast_id, audio=audio, timeout_s=timeout_s,
            tool_status="PENDING", device_playback_status="UNVERIFIED",
        )
        self._record_lifecycle(
            "PROCESS_STARTED", event="HOST_AUDIO_START",
            message=f"主机播放器进程已启动: {audio.name}；尚未证明设备侧播放。",
            case_id=case_id, broadcast_id=broadcast_id, audio=audio, timeout_s=timeout_s,
            monotonic_start=started, tool_status="RUNNING", device_playback_status="UNVERIFIED",
        )
        try:
            completed = subprocess.run(
                self._player_command("play", audio=audio), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as error:
            duration_s = round(time.monotonic() - started, 3)
            timeout_output = self._timeout_output(error)[-500:]
            self.last_playback.update({
                "status": "TIMEOUT", "reason": "player_process_timeout", "duration_s": duration_s,
                "evidence": "",
            })
            self._record_lifecycle(
                "TIMEOUT", event="HOST_AUDIO_TIMEOUT", level="ERROR",
                message=f"主机播放器超时，已等待 {duration_s:.3f}s。",
                case_id=case_id, broadcast_id=broadcast_id, audio=audio, timeout_s=timeout_s,
                duration_s=duration_s, evidence="", tool_status="FAIL", tool_output=timeout_output,
                device_playback_status="UNVERIFIED", error=f"{type(error).__name__}: {error}",
            )
            return False
        except OSError as error:
            self.last_playback.update({"status": "ERROR", "reason": "player_process_error", "error": str(error)})
            self._record_lifecycle(
                "ERROR", event="HOST_AUDIO_ERROR", level="ERROR", message=f"主机播放失败: {error}",
                case_id=case_id, broadcast_id=broadcast_id, audio=audio,
                tool_status="FAIL", device_playback_status="UNVERIFIED", error=f"{type(error).__name__}: {error}",
            )
            return False
        ended = time.monotonic()
        output = (completed.stdout or "") + (completed.stderr or "")
        duration_s = round(ended - started, 3)
        ok = completed.returncode == 0
        self.last_playback.update({
            "status": "COMPLETED" if ok else "FAILED",
            "reason": "host_player_completed" if ok else "player_returncode_nonzero",
            "returncode": completed.returncode,
            "duration_s": duration_s,
            "evidence": "",
        })
        self._record_lifecycle(
            "COMPLETED" if ok else "FAILED", event="HOST_AUDIO_END",
            level="INFO" if ok else "ERROR",
            message=(
                f"主机播放器进程正常返回，耗时 {duration_s:.3f}s；设备侧播放仍需 marker 佐证。"
                if ok else f"主机播放器返回失败，退出码 {completed.returncode}。"
            ),
            case_id=case_id, broadcast_id=broadcast_id, audio=audio,
            returncode=completed.returncode, duration_s=duration_s, evidence="",
            tool_status="PASS" if ok else "FAIL", device_playback_status="UNVERIFIED",
            tool_output=output[-500:],
        )
        return ok

    def observe_marker(
        self,
        marker: str,
        *,
        case_id: str = "",
        broadcast_id: str = "",
        audio: Path | None = None,
        port: str | None = None,
        raw_line: str = "",
        evidence_refs: tuple[str, ...] = (),
        state_class: str = "",
    ) -> dict[str, Any]:
        """记录项目原始播放器 marker，并附加公共生命周期状态。"""
        observed_at = time.monotonic()
        normalized = self.normalizer.observe(marker, state_class=state_class)
        record = {
            **normalized,
            "case_id": case_id,
            "broadcast_id": broadcast_id,
            "audio": str(audio) if audio is not None else "",
            "port": port,
            "raw_line": raw_line,
            "monotonic_seconds": observed_at,
            "evidence_refs": list(evidence_refs),
        }
        self.lifecycle.append(record)
        state = normalized.get("player_state")
        context = broadcast_id or case_id or "unassociated"
        if state == "active":
            self._lifecycle_times.setdefault((context, str(state)), observed_at)
        if state == "terminal" and (context, "active") in self._lifecycle_times:
            record["duration_ms"] = round((observed_at - self._lifecycle_times[(context, "active")]) * 1000)
        if state == "error":
            record["tool_status"] = "FAIL"
        level = "ERROR" if state == "error" else ("INFO" if state else "WARN")
        lifecycle = self._record_lifecycle(
            "DEVICE_PLAYER_EVENT",
            event="PLAYER_LIFECYCLE", level=level,
            message=f"播放器 marker: {marker}；state={state or 'UNKNOWN'}。",
            source="device_marker",
            device_playback_status=("FAILED" if state == "error" else ("OBSERVED" if state else "UNKNOWN")),
            **record,
        )
        return {**record, "lifecycle_log": str(self.artifacts.tool_log_path)}


class CaptureBackend:
    """可选录音适配器；没有显式 key/adapter 时绝不回退默认麦克风。"""

    def __init__(self, artifacts: TaskArtifacts, device_key: str | None, adapter: Any = None):
        self.artifacts = artifacts
        self.device_key = device_key
        self.adapter = adapter

    def probe(self) -> dict[str, Any]:
        if not self.device_key:
            self.artifacts.emit("CAPTURE_UNAVAILABLE", level="WARN", message="未提供录音声卡稳定 key，跳过录音。", task_log=True)
            return {"status": "UNAVAILABLE", "reason": "capture device key missing"}
        if self.adapter is None:
            self.artifacts.emit("CAPTURE_UNAVAILABLE", level="WARN", message="未注入录音 adapter，跳过录音且不使用默认麦克风。", task_log=True, device_key=self.device_key)
            return {"status": "UNAVAILABLE", "reason": "capture adapter missing", "device_key": self.device_key}
        try:
            result = self.adapter.probe(self.device_key)
        except Exception as error:
            self.artifacts.emit("CAPTURE_PROBE_FAIL", level="ERROR", message=f"录音设备探测失败: {error}", task_log=True, device_key=self.device_key)
            return {"status": "FAIL", "reason": str(error), "device_key": self.device_key}
        self.artifacts.emit("CAPTURE_PROBE", message="录音设备探测通过。", task_log=True, device_key=self.device_key)
        return {"status": "PASS", "device_key": self.device_key, "result": result}


class PlayerEventNormalizer:
    """Carry profile-classified state without interpreting device marker text."""

    STATE_CLASSES = {"preparation", "active", "terminal", "error", "unknown"}

    def __init__(self, mapping: dict[str, str] | None = None):
        # ``mapping`` remains for callers that explicitly supply a profile
        # mapping. There is intentionally no marker.upper() fallback.
        self.mapping = mapping or {}

    def normalize(self, marker: str, state_class: str = "") -> str | None:
        state = state_class or self.mapping.get(marker, "")
        normalized = str(state).lower()
        return normalized if normalized in self.STATE_CLASSES else None

    def observe(self, marker: str, *, state_class: str = "") -> dict[str, str | None]:
        """Keep the actual marker untouched; classification is profile input."""
        classification = self.normalize(marker, state_class)
        return {
            "raw_marker": marker,
            "player_state": classification,
            "tool_status": "OBSERVED" if classification else "WARN",
        }
