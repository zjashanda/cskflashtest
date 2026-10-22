"""Independent serial collectors with injectable factories for fake tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any, Iterable

try:
    import serial
except ImportError:  # pragma: no cover - preflight reports the missing dependency
    serial = None

try:
    from .core import PortSpec, TaskArtifacts, now_iso
except ImportError:  # direct execution fallback
    from core import PortSpec, TaskArtifacts, now_iso


@dataclass(frozen=True)
class SerialEvent:
    port: str
    role: str
    cursor: int
    timestamp: str
    monotonic_seconds: float
    line: str


class SerialManager:
    def __init__(self, ports: tuple[PortSpec, ...], artifacts: TaskArtifacts, factory: Callable[..., Any] | None = None):
        self.ports = ports
        self.artifacts = artifacts
        self.factory = factory or (serial.Serial if serial is not None else None)
        self.handles: dict[str, Any] = {}
        self.threads: list[threading.Thread] = []
        self.stop_event = threading.Event()
        self.errors: dict[str, str] = {}
        self.cursors: dict[str, int] = {}
        self.reconnect_attempts: dict[str, int] = {}
        self.events: dict[str, list[SerialEvent]] = {}
        self._events_lock = threading.RLock()
        self.open_failures: dict[str, str] = {}

    def start(self) -> None:
        self.artifacts.require_cases_frozen("serial_capture")
        if not self.ports:
            self.artifacts.emit("SERIAL_SKIP", message="未声明串口，设备语音能力将按缺失证据处理。", task_log=True)
            return
        if self.factory is None:
            raise RuntimeError("pyserial is required for serial capture")
        for spec in self.ports:
            # Create the continuous fact source before opening or writing to a
            # device so an early open/write failure still has a stable path.
            self.artifacts.serial_log_path(spec.port, spec.role).touch(exist_ok=True)
            try:
                handle = self.factory(spec.port, spec.baudrate, timeout=0.2)
            except Exception as error:
                self.open_failures[spec.port] = f"{type(error).__name__}: {error}"
                self.artifacts.emit(
                    "SERIAL_OPEN_BLOCKED", level="ERROR", message=f"串口 {spec.port} 无法打开。",
                    task_log=True, port=spec.port, role=spec.role or "unknown", error=str(error),
                )
                continue
            self.handles[spec.port] = handle
            self.cursors[spec.port] = 0
            self.events[spec.port] = []
            role = spec.role or "unknown"
            self.artifacts.emit("SERIAL_OPEN", message=f"串口 {spec.port} 已打开。", task_log=True, port=spec.port, role=role, baudrate=spec.baudrate)
            thread = threading.Thread(target=self._reader, args=(spec,), name=f"lstest-{spec.port}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def _reader(self, spec: PortSpec) -> None:
        role = spec.role or "unknown"
        path = self.artifacts.serial_log_path(spec.port, role)
        try:
            with path.open("a", encoding="utf-8") as log:
                while not self.stop_event.is_set():
                    try:
                        raw = self.handles[spec.port].readline()
                    except Exception as error:
                        if self.stop_event.is_set():
                            break
                        attempts = self.reconnect_attempts.get(spec.port, 0) + 1
                        self.reconnect_attempts[spec.port] = attempts
                        self.artifacts.emit("SERIAL_RECONNECT", level="WARN", message=f"串口 {spec.port} 读取异常，尝试重连 {attempts}/3。", task_log=True, port=spec.port, error=str(error))
                        if attempts > 3:
                            raise
                        time.sleep(0.2)
                        self.handles[spec.port] = self.factory(spec.port, spec.baudrate, timeout=0.2)
                        continue
                    if not raw:
                        continue
                    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    self.cursors[spec.port] += 1
                    timestamp = now_iso()
                    event = SerialEvent(spec.port, role, self.cursors[spec.port], timestamp, time.monotonic(), text)
                    with self._events_lock:
                        self.events[spec.port].append(event)
                    # Each captured event occupies exactly one physical line;
                    # cursor therefore maps directly to the human log line.
                    log.write(f"[{timestamp}] {spec.port} {role} #{self.cursors[spec.port]} {text}\n")
                    log.flush()
                    self.artifacts.emit("SERIAL_LINE", message=f"{spec.port}: {text[:180]}", task_log=False, port=spec.port, role=role, cursor=self.cursors[spec.port], line=text)
        except Exception as error:
            self.errors[spec.port] = f"{type(error).__name__}: {error}"
            self.artifacts.add_sticky("SERIAL_LOSS", f"串口 {spec.port} 采集失败。", port=spec.port, error=str(error))

    def stop(self) -> None:
        self.stop_event.set()
        for handle in self.handles.values():
            try:
                handle.close()
            except Exception:
                pass
        for thread in self.threads:
            thread.join(timeout=1.0)
        self.artifacts.emit("SERIAL_CLOSE", message="分端口串口采集已停止。", task_log=True, cursors=dict(self.cursors), errors=dict(self.errors))

    def write(self, command: str, port: str) -> str:
        """仅写入已打开的显式端口；回显仍由连续采集保留。"""
        if port not in self.handles:
            raise RuntimeError(f"serial port is not open: {port}")
        payload = (command.rstrip("\r\n") + "\r\n").encode("utf-8")
        handle = self.handles[port]
        handle.write(payload)
        if hasattr(handle, "flush"):
            handle.flush()
        return ""

    def snapshot(self, port: str | None = None) -> dict[str, int]:
        with self._events_lock:
            ports: Iterable[str] = (port,) if port else self.events.keys()
            return {name: len(self.events.get(name, ())) for name in ports}

    def since(self, cursors: dict[str, int], *, ports: Iterable[str] | None = None) -> list[SerialEvent]:
        with self._events_lock:
            result: list[SerialEvent] = []
            selected = set(ports) if ports is not None else set(cursors)
            # A cursor map is an explicit event scope.  This prevents a
            # single-port wait from accidentally consuming another port.
            for port, values in self.events.items():
                if selected and port not in selected:
                    continue
                result.extend(values[int(cursors.get(port, 0)):])
        return sorted(result, key=lambda item: item.monotonic_seconds)

    def wait_for(self, predicate: Callable[[list[SerialEvent]], bool], timeout_s: float, *, cursors: dict[str, int] | None = None) -> list[SerialEvent]:
        start = cursors or self.snapshot()
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline and not self.stop_event.is_set():
            events = self.since(start)
            if predicate(events):
                return events
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return self.since(start)

    def infer_roles(self, infer: Callable[[str], tuple[str | None, float]]) -> dict[str, dict[str, Any]]:
        """以 profile marker 推断未声明角色，永不依赖端口名称。"""
        result: dict[str, dict[str, Any]] = {}
        for spec in self.ports:
            if spec.role:
                continue
            text = "\n".join(item.line for item in self.since({spec.port: 0}, ports=(spec.port,)))
            role, confidence = infer(text)
            result[spec.port] = {"role": role, "confidence": confidence, "role_inferred": bool(role)}
            self.artifacts.emit(
                "SERIAL_ROLE_INFERENCE", level="INFO" if role else "WARN",
                message=f"串口 {spec.port} 角色{'推断为 ' + role if role else '无法推断'}。",
                task_log=True, port=spec.port, **result[spec.port],
            )
        return result
