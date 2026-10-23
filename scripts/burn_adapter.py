"""CSK 烧录压力测试的原始证据采集与烧录工具适配层。

该模块只负责：
- 持续采集串口原始行并写入 serial_logs；
- 通过控制串口发送单条命令并等待设备回执；
- 启动 Uart_Burn_Tool 并把原始输出逐行写入 serial_logs。

业务判定、结果账本和 tool.log 由 run.py 通过 lstest 公共 API 完成。
"""

from __future__ import annotations

import datetime as dt
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import serial


def now_human() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass
class LineEvent:
    port: str
    role: str
    source: str
    cursor: int
    timestamp: str
    monotonic: float
    text: str
    log_name: str = ""
    phase: str = ""

    @property
    def evidence(self) -> str:
        return f"serial_logs/{self.log_name}#{self.cursor}"


class SerialCollector:
    """连续读取一个串口，保留原始行并支持按游标取证据。"""

    def __init__(
        self,
        port: str,
        baudrate: int,
        role: str,
        source: str,
        log_path: Path,
        *,
        on_event: Callable[[LineEvent], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        reconnect_attempts: int = 3,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.role = role
        self.source = source
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_name = self.log_path.name
        self.on_event = on_event
        self.on_error = on_error
        self.reconnect_attempts = max(0, int(reconnect_attempts))
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._events: list[LineEvent] = []
        self._cursor = 0
        self._handle: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._log = None
        self.error = ""
        self.reconnects = 0

    def start(self) -> None:
        self._log = self.log_path.open("a", encoding="utf-8", newline="\n")
        self._open()
        self._thread = threading.Thread(target=self._run, name=f"collector-{self.port}", daemon=True)
        self._thread.start()

    def open_log_only(self) -> None:
        """仅打开证据文件，用于接收烧录工具 stdout 等非串口来源。"""
        self._log = self.log_path.open("a", encoding="utf-8", newline="\n")

    def _open(self) -> None:
        self._handle = serial.Serial(self.port, self.baudrate, timeout=0.2)
        try:
            self._handle.reset_input_buffer()
        except Exception:
            pass

    def _run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            try:
                if self._handle is None:
                    self._open()
                raw = self._handle.readline()
            except Exception as error:
                if self._stop.is_set():
                    break
                attempts += 1
                self.error = f"{type(error).__name__}: {error}"
                if self.on_error:
                    self.on_error("SERIAL_READ_ERROR", f"{self.port} 读取异常 {attempts}/{self.reconnect_attempts}: {error}")
                if attempts > self.reconnect_attempts:
                    self.error = f"SERIAL_LOSS {self.port}: {type(error).__name__}: {error}"
                    if self.on_error:
                        try:
                            self.on_error("SERIAL_LOSS", self.error)
                        except Exception:
                            pass
                    break
                self._close_handle()
                time.sleep(0.2)
                try:
                    self._open()
                    self.reconnects += 1
                    self.error = ""
                    if self.on_error:
                        self.on_error("SERIAL_RECONNECT", f"{self.port} 已重连 {self.reconnects} 次")
                except Exception as open_error:
                    self.error = f"{self.port} 重连失败: {type(open_error).__name__}: {open_error}"
                continue
            if not raw:
                continue
            attempts = 0
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            self._ingest(text)

    def _ingest(self, text: str, *, timestamp: str | None = None) -> LineEvent | None:
        with self._lock:
            self._cursor += 1
            event = LineEvent(
                port=self.port,
                role=self.role,
                source=self.source,
                cursor=self._cursor,
                timestamp=timestamp or now_human(),
                monotonic=time.monotonic(),
                text=text,
                log_name=self.log_name,
            )
            self._events.append(event)
            if self._log is not None:
                self._log.write(f"[{event.timestamp}] {self.port} {self.role} #{event.cursor} {text}\n")
                self._log.flush()
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass
        return event

    def inject(self, text: str) -> LineEvent | None:
        """把非串口来源（烧录工具 stdout）按同一证据格式写入。"""
        return self._ingest(text)

    def write(self, text: str) -> None:
        with self._write_lock:
            if self._handle is None:
                raise RuntimeError(f"serial port is not open: {self.port}")
            self._handle.write(text.encode("utf-8"))
            self._handle.flush()

    def restart(self) -> None:
        """重启读取线程和串口句柄，用于控制串口无响应后的软恢复。"""
        self._stop.set()
        self._close_handle()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._stop.clear()
        self.error = ""
        self._open()
        self._thread = threading.Thread(target=self._run, name=f"collector-{self.port}", daemon=True)
        self._thread.start()

    def snapshot(self) -> int:
        with self._lock:
            return self._cursor

    def events_since(self, cursor: int) -> list[LineEvent]:
        with self._lock:
            return list(self._events[cursor:])

    def text_since(self, cursor: int) -> str:
        return "\n".join(item.text for item in self.events_since(cursor))

    def wait_for(
        self,
        cursor: int,
        predicate: Callable[[str], bool],
        timeout_s: float,
    ) -> tuple[list[LineEvent], bool]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            events = self.events_since(cursor)
            if predicate("\n".join(item.text for item in events)):
                return events, True
            if time.monotonic() >= deadline or self._stop.is_set():
                return events, False
            time.sleep(0.05)

    def _close_handle(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
            self._handle = None

    def stop(self) -> None:
        self._stop.set()
        self._close_handle()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._log is not None:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None


@dataclass
class CommandResult:
    command: str
    ok: bool
    raw_text: str
    ack_text: str
    duration_ms: int
    error: str = ""
    infra_error: bool = False
    attempts: int = 1


def _send_control_once(
    collector: SerialCollector,
    command: str,
    ack_regex: str,
    *,
    gap_s: float,
    timeout_s: float,
) -> CommandResult:
    start_cursor = collector.snapshot()
    started = time.monotonic()
    error = ""
    infra_error = False
    try:
        collector.write(command + "\r")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        infra_error = True
    matched = False
    ack_text = ""
    deadline = started + max(gap_s, timeout_s)
    while time.monotonic() < deadline:
        text = collector.text_since(start_cursor)
        match = re.search(ack_regex, text, flags=re.IGNORECASE)
        if match:
            ack_text = text
            matched = True
            break
        time.sleep(0.02)
    remaining = gap_s - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    raw_text = collector.text_since(start_cursor)
    if not matched and not ack_text:
        ack_text = raw_text
    return CommandResult(
        command=command,
        ok=bool(matched and not error),
        raw_text=raw_text,
        ack_text=ack_text,
        duration_ms=round((time.monotonic() - started) * 1000),
        error=error,
        infra_error=infra_error,
    )


def send_control(
    collector: SerialCollector,
    command: str,
    ack_regex: str,
    *,
    gap_s: float = 0.5,
    timeout_s: float = 2.0,
    retries: int = 0,
) -> CommandResult:
    attempts = 0
    result: CommandResult | None = None
    for _ in range(max(0, int(retries)) + 1):
        attempts += 1
        result = _send_control_once(
            collector, command, ack_regex, gap_s=gap_s, timeout_s=timeout_s,
        )
        if result.ok:
            break
    assert result is not None
    result.attempts = attempts
    return result


@dataclass
class BurnResult:
    exit_code: int
    timed_out: bool
    duration_ms: int
    lines: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class BurnToolRunner:
    """运行 Uart_Burn_Tool，按行保留完整原始输出。"""

    def __init__(
        self,
        exe: Path,
        cwd: Path,
        collector: SerialCollector,
        *,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self.exe = Path(exe)
        self.cwd = Path(cwd)
        self.collector = collector
        self.on_error = on_error
        self._process: subprocess.Popen | None = None

    def build_args(self, fw_path: Path, *, port: str, baudrate: int = 1500000, timeout: int = 10) -> list[str]:
        args = [
            "-b", str(baudrate),
            "-p", port,
            "-c",
            "-t", str(timeout),
            "-f", str(fw_path),
            "-l", "-m", "-d",
        ]
        if fw_path.suffix.lower() == ".img":
            args += ["-a", "0x0"]
        else:
            args += ["0x00"]
        args += ["-s"]
        return args

    def run(
        self,
        fw_path: Path,
        *,
        port: str,
        timeout_s: float,
        baudrate: int = 1500000,
    ) -> BurnResult:
        args = self.build_args(fw_path, port=port, baudrate=baudrate)
        started = time.monotonic()
        lines: list[str] = []
        buffer = ""
        timed_out = False
        error = ""
        queue_: queue.Queue[bytes | None] = queue.Queue()
        try:
            self._process = subprocess.Popen(
                [str(self.exe), *args],
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return BurnResult(-1, False, 0, [], f"{type(exc).__name__}: {exc}")

        def reader() -> None:
            stream = self._process.stdout if self._process is not None else None
            if stream is None:
                queue_.put(None)
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    queue_.put(chunk)
            finally:
                queue_.put(None)

        thread = threading.Thread(target=reader, name="burn-tool-stdout", daemon=True)
        thread.start()
        eof = False
        try:
            while not eof:
                if time.monotonic() - started > timeout_s:
                    timed_out = True
                    self.terminate()
                    break
                try:
                    chunk = queue_.get(timeout=0.2)
                except queue.Empty:
                    continue
                if chunk is None:
                    eof = True
                    break
                buffer += chunk.decode("gbk", "replace")
                while True:
                    match = re.search(r"\r\n|\r|\n", buffer)
                    if not match:
                        break
                    line = buffer[: match.start()].strip()
                    buffer = buffer[match.end():]
                    if line:
                        lines.append(line)
                        self.collector.inject(line)
            if buffer.strip():
                lines.append(buffer.strip())
                self.collector.inject(buffer.strip())
        except KeyboardInterrupt:
            self.terminate()
            raise
        finally:
            code = self._process.wait() if self._process is not None else -1
            thread.join(timeout=1.0)
        if timed_out:
            error = f"burn tool timeout after {timeout_s:.0f}s"
            if self.on_error:
                try:
                    self.on_error("BURN_TIMEOUT", error)
                except Exception:
                    pass
        return BurnResult(
            exit_code=code,
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
            lines=lines,
            error=error,
        )

    def terminate(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            pass


def open_probe(port: str, baudrate: int) -> tuple[bool, str]:
    """打开并立即关闭串口，用于 preflight 判断占用情况。"""
    handle = None
    try:
        handle = serial.Serial(port, baudrate, timeout=0.1)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def port_available(port: str) -> bool:
    from serial.tools import list_ports

    return any(item.device.upper() == port.upper() for item in list_ports.comports())
