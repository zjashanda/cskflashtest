"""CSK 烧录压力测试入口。

每次运行产生 lstest 标准四类产物：
  result/<timestamp>_cskflashtest/
    serial_logs/*.log
    tool.log
    results.csv
    cases.csv

单轮流程：
  下电 -> 进 boot -> 上电 -> 烧录 -> 退出 boot -> 断电再上电 -> 观察 CSK 启动
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parent
sys.path.insert(0, str(SKILL_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from lstest.scripts.core import CaseResult, TaskArtifacts  # noqa: E402
from lstest.scripts.profile import DeviceProfile, ProfileFixtureRunner, RawLogRecord  # noqa: E402
from lstest.scripts.interaction_acceptance import (  # noqa: E402
    MODES,
    evaluate_round,
    evaluate_run,
    mode_label,
    rejects_multi_result,
)
from lstest.scripts.runtime import ScenarioRuntime  # noqa: E402
from lstest.scripts.serial_recovery import recover_serial  # noqa: E402
from lstest.scripts.timeline import ToolLogValidator  # noqa: E402

from burn_adapter import (  # noqa: E402
    BurnToolRunner,
    CommandResult,
    LineEvent,
    SerialCollector,
    now_human,
    open_probe,
    port_available,
    send_control,
)

CHILD_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FATAL_RE = re.compile(r"FATAL|ASSERT FAIL|HardFault|panic|watchdog reset|Stack overflow|Boot Reason:\s*CRASH", re.I)
BOOTLOADER_RE = re.compile(r"bootloader application", re.I)
VERSION_RE = re.compile(r"venus ap appliction\((?P<version>V[^)@]+@[^)]+)\)", re.I)
BUILD_RE = re.compile(r"Project Build Info:\s*(?P<build>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}_[0-9A-Za-z]+)", re.I)
MD5_RE = re.compile(r"MD5 CHECK SUCCESS MD5_VALUE\s*=\s*(?P<md5>0x[0-9A-Fa-f]+)", re.I)
CHIP_RE = re.compile(r"CHIP TYPE\s*=\s*(?P<chip>0x[0-9A-Fa-f]+)", re.I)
WRITE_SIZE_RE = re.compile(r"WIRITE FLASH OFFSET ADDR:(?P<addr>0x[0-9A-Fa-f]+)\s+WRITE (?:FILE )?SIZE:\s*(?P<size>\d+)", re.I)
FLASH_DONE_RE = re.compile(r"FLASH DOWNLOAD SUCCESS|FLASH\s+0x[0-9A-Fa-f]+\s+SECTOR DOWNLOAD SUCCESS", re.I)
PROGRESS_100_RE = re.compile(r"FLASH DATA SEND PROGESS:\s*100%", re.I)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hex_write_size(path: Path) -> int:
    blocks: set[int] = set()
    base = 0
    for raw in path.read_text(encoding="ascii", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith(":"):
            continue
        try:
            record = bytes.fromhex(line[1:])
        except ValueError:
            continue
        if len(record) < 5:
            continue
        count = record[0]
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        payload = record[4:4 + count]
        if record_type == 0x04 and len(payload) >= 2:
            base = ((payload[0] << 8) | payload[1]) << 16
        elif record_type == 0x02 and len(payload) >= 2:
            base = ((payload[0] << 8) | payload[1]) << 4
        elif record_type == 0x00:
            blocks.add((base + address) // 0x1000)
    return len(blocks) * 0x1000


def normalize_md5(value: str) -> str:
    return value.strip().lower().removeprefix("0x")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return path.name


def load_device_entry(path: Path, device_id: str | None) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    devices = [dict(item) for item in payload.get("devices", []) if item.get("enabled", True)]
    if not devices:
        raise ValueError(f"no enabled devices in {path}")
    if device_id:
        for item in devices:
            if str(item.get("id")) == device_id:
                return item
        raise ValueError(f"device id not found or disabled: {device_id}")
    if len(devices) > 1:
        raise ValueError("multiple enabled devices; --device-id is required")
    return devices[0]


def asset_path(relative: Path) -> Path:
    """Resolve an asset under the project first, then under the skill repo."""
    if relative.is_absolute():
        return relative
    for base in (PROJECT_ROOT, SKILL_ROOT):
        candidate = base / relative
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / relative


def lstest_script(name: str) -> Path:
    """Resolve a bundled lstest script under the project first, then the skill repo."""
    for base in (PROJECT_ROOT, SKILL_ROOT):
        candidate = base / "lstest" / "scripts" / name
        if candidate.is_file():
            return candidate
    return PROJECT_ROOT / "lstest" / "scripts" / name


def find_evidence(events: list[LineEvent], pattern: re.Pattern[str]) -> list[str]:
    result: list[str] = []
    for event in events:
        if pattern.search(event.text):
            result.append(event.evidence)
    return result[-3:]


class BurnScenario:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mode = str(getattr(args, "mode", "burn") or "burn").strip().lower()
        if self.mode not in ("burn", "powercycle"):
            raise ValueError(f"unsupported mode: {self.mode}")
        if self.mode == "powercycle":
            args.skip_burn = True
        self.scenario_name = "cskpowercycle" if self.mode == "powercycle" else "cskflashtest"
        self.case_prefix = "powercycle" if self.mode == "powercycle" else "burn"
        self.result_root = Path(args.result_root).resolve()
        self.profile_path = Path(args.profile).resolve()
        self.manifest_path = Path(args.manifest).resolve()
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.profile = DeviceProfile.load(self.profile_path)
        self.timing = dict(self.manifest.get("timing", {}))
        self.control_recovery = dict(self.manifest.get("control_recovery", {}))
        self.acceptance_mode = str(
            getattr(args, "acceptance", "")
            or self.manifest.get("interaction_acceptance", {}).get("mode", "any_asr")
        ).strip()
        if self.acceptance_mode not in MODES:
            raise ValueError(f"unsupported acceptance mode: {self.acceptance_mode}")
        if getattr(args, "acceptance", ""):
            args.wake_test = True
            args.interaction_test = self.acceptance_mode not in {"wake_only", "wake_playback"}
        self._asr_result_seen = False
        self._last_round_stats: dict = {}
        self.verification = dict(self.manifest.get("verification", {}))
        self.ports = dict(self.manifest.get("ports", {}))
        self.device_id = ""
        self.device_label = ""
        self.device_entry: dict = {}
        if getattr(args, "devices", ""):
            self.device_entry = load_device_entry(Path(args.devices), getattr(args, "device_id", None))
            self.device_id = str(self.device_entry["id"]).strip()
            self.device_label = str(self.device_entry.get("label") or self.device_id).strip()
            if self.device_entry.get("fw"):
                args.fw = str(self.device_entry["fw"])
            for role in ("control", "csk", "burn", "hisi"):
                value = str(self.device_entry.get(role, "") or "").strip()
                if value:
                    self.ports.setdefault(role, {})
                    self.ports[role] = {**self.ports[role], "port": value}
            if not str(self.device_entry.get("hisi", "") or "").strip():
                args.no_hisi = True
            for role, key in (("control", "control_baud"), ("csk", "csk_baud"), ("hisi", "hisi_baud")):
                if self.device_entry.get(key):
                    self.ports.setdefault(role, {})
                    self.ports[role]["baudrate"] = int(self.device_entry[key])
        self.require_spk_marker = bool(
            self.device_entry.get("require_spk_marker", True)
        ) if self.device_entry else True
        self.batch_stamp = str(getattr(args, "batch_stamp", "") or "").strip() or time.strftime("%Y%m%d_%H%M%S")
        self.task_summary = str(getattr(args, "task_summary", "") or "").strip()
        if not self.task_summary:
            self.task_summary = "burn-wake-asr" if self.mode == "burn" else "powercycle-init-wake-asr"
        device_part = self.device_id or "single"
        self.run_name = str(getattr(args, "run_name", "") or "").strip() or (
            f"{self.batch_stamp}_{self.scenario_name}_{device_part}_{self.task_summary}_{self.args.rounds}rounds"
        )
        self.task_slug = (
            f"{self.scenario_name}_{self.args.rounds}_{self.device_label}"
            if self.device_id else f"{self.scenario_name}_{self.args.rounds}"
        )

        raw_fw = args.fw.lower()
        if raw_fw == "auto":
            hex_path = PROJECT_ROOT / self.manifest["firmware"]["hex"]["path"]
            fw_key = "hex" if hex_path.is_file() else "img"
        elif raw_fw in ("img", "hex"):
            fw_key = raw_fw
        else:
            fw_key = "img" if Path(args.fw).suffix.lower() == ".img" else "hex"
        self.fw_key = fw_key
        entry = dict(self.manifest["firmware"][fw_key])
        self.fw_entry = entry
        if raw_fw not in ("auto", "img", "hex"):
            self.fw_path = asset_path(Path(args.fw))
        else:
            self.fw_path = asset_path(Path(entry["path"]))

        self.control_port = str(self.ports["control"]["port"])
        self.csk_port = str(self.ports["csk"]["port"])
        self.burn_port = str(self.ports["burn"]["port"])
        self.hisi_port = str(self.ports.get("hisi", {}).get("port", "COM18"))
        self.control_baud = int(self.ports["control"]["baudrate"])
        self.csk_baud = int(self.ports["csk"]["baudrate"])
        self.hisi_baud = int(self.ports.get("hisi", {}).get("baudrate", 1500000))

        self.command_gap = float(args.command_gap if args.command_gap is not None else self.timing.get("command_gap_s", 0.5))
        self.pre_burn_wait = float(self.timing.get("pre_burn_wait_s", 1.0))
        self.boot_timeout = float(args.boot_observe if args.boot_observe is not None else self.timing.get("boot_observe_timeout_s", 30.0))
        self.post_ready_settle = float(self.timing.get("post_ready_settle_s", 2.0))
        self.burn_timeout = float(args.burn_timeout if args.burn_timeout is not None else self.timing.get("burn_timeout_s", 600.0))
        self.ack_timeout = float(self.timing.get("control_ack_timeout_s", 2.0))
        self.control_retries = int(self.timing.get("control_retries", 2))
        self.csk_init = dict(self.manifest.get("csk_init", {}))
        self.csk_init_wait = float(self.timing.get("csk_init_wait_s", 2.0))
        self.audio_init = dict(self.manifest.get("audio_init", {}))
        self.interaction = dict(self.manifest.get("interaction", {}))
        self.audio_lock = dict(self.manifest.get("audio_lock", {}))
        self.wake = dict(self.manifest.get("wake", {}))
        if self.device_entry.get("render_device_key"):
            self.wake["render_device_key"] = str(self.device_entry["render_device_key"])
        if self.device_entry.get("audio_file"):
            self.wake["audio_file"] = str(self.device_entry["audio_file"])

        self.artifacts: TaskArtifacts | None = None
        self.runtime: ScenarioRuntime | None = None
        self.csk: SerialCollector | None = None
        self.control: SerialCollector | None = None
        self.hisi: SerialCollector | None = None
        self.burn_log: SerialCollector | None = None
        self.burn_runner: BurnToolRunner | None = None
        self._current_case = ""
        self._submit_lock = __import__("threading").RLock()
        self._infra_streak = 0
        self._round_infra_failure = False
        self._pa_enabled = False

    # ---------- preflight ----------
    def preflight(self) -> list[str]:
        errors: list[str] = []
        try:
            self.profile.assert_formal_ready()
            self._emit("SYSTEM", "PROFILE_READY", f"profile 校验通过: {self.profile.profile_id}", status="PASS")
        except Exception as exc:
            errors.append(f"profile contract: {type(exc).__name__}: {exc}")
            self._emit("SYSTEM", "PROFILE_BLOCKED", f"profile 校验失败: {exc}", level="ERROR", status="FAIL")

        if self.mode == "burn":
            if not self.fw_path.is_file():
                errors.append(f"firmware missing: {self.fw_path}")
            else:
                sha = sha256_file(self.fw_path)
                expected_sha = str(self.fw_entry.get("source_sha256", ""))
                if expected_sha and sha != expected_sha:
                    errors.append(f"firmware sha256 changed: {sha} != {expected_sha}")
                if self.fw_key == "img":
                    actual_md5 = md5_file(self.fw_path)
                    expected_md5 = normalize_md5(str(self.fw_entry.get("expected_tool_md5", "")))
                    if expected_md5 and actual_md5 != expected_md5:
                        errors.append(f"img md5 mismatch: {actual_md5} != {expected_md5}")
                else:
                    size = hex_write_size(self.fw_path)
                    expected_size = int(self.fw_entry.get("expected_write_size", 0) or 0)
                    if expected_size and size != expected_size:
                        errors.append(f"hex write size mismatch: {size} != {expected_size}")

            tool = asset_path(Path("burn") / "Uart_Burn_Tool.exe")
            if not tool.is_file():
                errors.append(f"burn tool missing: {tool}")

        all_ports = [self.control_port, self.csk_port]
        if self.mode == "burn":
            all_ports.append(self.burn_port)
        if not self.args.no_hisi:
            all_ports.append(self.hisi_port)
        for port in all_ports:
            if not port_available(port):
                errors.append(f"port not present: {port}")
                continue
            ok, detail = open_probe(port, self.hisi_baud if port == self.hisi_port else 115200)
            level = "WARN" if port == self.hisi_port else "ERROR"
            if not ok:
                if port == self.hisi_port and not self.args.no_hisi:
                    self._emit("SYSTEM", "PORT_WARN", f"{port} 不可用，HISI 采集将跳过: {detail}", level="WARN", status="WARN")
                else:
                    errors.append(f"port open failed: {port}: {detail}")
                    self._emit("SYSTEM", "PORT_BLOCKED", f"{port} 打开失败: {detail}", level="ERROR", status="FAIL")
            else:
                self._emit("SYSTEM", "PORT_READY", f"{port} 可用", status="PASS")

        try:
            self.control_prompt_ok()
            self._emit("SYSTEM", "CONTROL_PROMPT", "控制串口 shell 提示符正常", status="PASS")
        except Exception as exc:
            errors.append(f"control prompt: {type(exc).__name__}: {exc}")
            self._emit("SYSTEM", "CONTROL_PROMPT_FAIL", f"控制串口无提示符: {exc}", level="ERROR", status="FAIL")

        min_free = int(self.verification.get("min_free_bytes", 1024 ** 3))
        free = shutil.disk_usage(self.result_root).free if self.result_root.exists() else shutil.disk_usage(PROJECT_ROOT).free
        if free < min_free:
            errors.append(f"disk free {free} < {min_free}")
        self._emit("SYSTEM", "DISK_CHECK", f"剩余空间 {free} 字节", status="PASS" if free >= min_free else "FAIL")

        for name, value in (
            ("chip_type", self.manifest.get("chip_type", "")),
            ("expected_tool_md5", self.fw_entry.get("expected_tool_md5", "")),
            ("expected_write_size", self.fw_entry.get("expected_write_size", "")),
            ("expected_version", self.fw_entry.get("expected_version", "")),
            ("expected_build", self.fw_entry.get("expected_build", "")),
        ):
            if not value:
                errors.append(f"manifest missing {name}")
        return errors

    def control_prompt_ok(self) -> None:
        import serial

        handle = serial.Serial(self.control_port, self.control_baud, timeout=0.2)
        try:
            time.sleep(0.2)
            handle.reset_input_buffer()
            handle.write(b"\r")
            deadline = time.monotonic() + 2.0
            data = b""
            while time.monotonic() < deadline:
                chunk = handle.read(1024)
                if chunk:
                    data += chunk
                    if b"root:/$" in data:
                        return
                time.sleep(0.05)
            raise RuntimeError(f"prompt not found: {data[-120:]!r}")
        finally:
            handle.close()

    # ---------- runtime ----------
    def setup(self) -> None:
        self.artifacts = TaskArtifacts(
            self.result_root,
            self.task_slug,
            detail_fields=(
                "round_outcome", "acceptance_mode", "asr_skipped",
                "wake_result_count", "asr_result_count", "wake_results", "asr_results", "multi_result",
            ),
            run_name=self.run_name,
            append=True,
        )
        self._write_task_info()
        self.artifacts.enable_terminal_mirror(sys.stdout)
        self.artifacts.configure({
            "profile": self.profile.profile_id,
            "profile_sha256": self.profile.sha256,
            "firmware": self.fw_key,
            "firmware_sha256": self.fw_entry.get("source_sha256", ""),
            "rounds": self.args.rounds,
            "ports": {
                "control": self.control_port,
                "csk": self.csk_port,
                "burn": self.burn_port,
                "hisi": None if self.args.no_hisi else self.hisi_port,
            },
        })
        self.runtime = ScenarioRuntime(self.artifacts, profile=self.profile)
        self.runtime.begin_lazy_cases(
            scenario=self.scenario_name,
            planned_total=self.args.rounds,
            profile_version=self.profile.schema_version,
            profile_sha256=self.profile.sha256,
        )

    def _write_task_info(self) -> None:
        assert self.artifacts is not None
        device = f"{self.device_id}（板卡 {self.device_label}）" if self.device_id else "single"
        mode = "burn（烧录）" if self.mode == "burn" else "powercycle（上下电）"
        summary = (
            "烧录 -> CSK 初始化 -> 唤醒 -> ASR 识别"
            if self.mode == "burn"
            else "上下电 -> CSK 初始化 -> 唤醒 -> ASR 识别"
        )
        lines = [
            f"批次时间戳: {self.batch_stamp}",
            f"压测项: {mode}",
            f"压测设备: {device}",
            f"任务简介: {summary}",
            f"任务总次数: {self.args.rounds}",
        ]
        (self.artifacts.run_dir / "task-info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _emit(self, tag: str, event: str, message: str, *, level: str = "INFO", **fields) -> None:
        if self.artifacts is not None:
            self.artifacts.emit(event, message=message, level=level, **fields)

    def dispatch(self, event: LineEvent) -> None:
        runtime = self.runtime
        case_id = self._current_case
        if runtime is None or not case_id:
            return
        with self._submit_lock:
            try:
                runtime.submit_raw_record(
                    RawLogRecord(
                        text=event.text,
                        source=event.source,
                        port=event.port,
                        role=event.role,
                        cursor=event.cursor,
                        observed_at=event.timestamp,
                        phase=event.phase,
                        evidence=(event.evidence,),
                    ),
                    case_id=case_id,
                )
            except Exception as exc:
                try:
                    self.artifacts.record_anomaly(
                        "RAW_RECORD_SUBMIT_FAILED",
                        "原始记录提交失败，继续采集证据。",
                        case_id=case_id,
                        port=event.port,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass

    def on_serial_error(self, code: str, message: str) -> None:
        if self.artifacts is not None:
            try:
                self.artifacts.record_anomaly(
                    code, message, case_id=self._current_case,
                    handling="串口继续重连，当前轮按证据缺失处理",
                )
            except Exception:
                pass

    def control_probe(self, timeout_s: float = 1.5) -> tuple[bool, str]:
        """发送空行探测控制 shell；无提示符视为控制链路异常。"""
        if self.control is None:
            return False, "control collector not started"
        cursor = self.control.snapshot()
        try:
            self.control.write("\r")
        except Exception as exc:
            return False, f"write failed: {type(exc).__name__}: {exc}"
        events, found = self.control.wait_for(cursor, lambda text: "root:/$" in text, timeout_s)
        reply = "\n".join(item.text for item in events)[-300:]
        return found, reply

    def recover_control_serial(self) -> tuple[bool, str, list[str]]:
        """多次关闭/重开控制口，直到 shell 提示符恢复。"""
        if self.control is None:
            return False, "control collector not started", []
        attempts = max(1, int(self.control_recovery.get("attempts", 5)))
        delays = list(self.control_recovery.get("delays_s", [0.5, 1.0, 2.0, 3.0, 5.0]))
        prompt_timeout = float(self.control_recovery.get("prompt_timeout_s", 3.0))

        def on_event(status: str, fields: dict) -> None:
            attempt = fields.get("attempt")
            delay = fields.get("delay_s")
            if status == "START":
                self._emit(
                    "SYSTEM", "CONTROL_RECONNECT",
                    f"控制口重连 {attempt}/{attempts}，等待 {float(delay or 0):.1f}s",
                    level="INFO", status="RUN",
                )
                return
            ok = status == "PASS"
            self._emit(
                "SYSTEM", "CONTROL_RECONNECT",
                f"控制口重连 {attempt}/{attempts} {'成功' if ok else '失败'}",
                level="INFO" if ok else "WARN",
                status=status,
                reason=fields.get("reason", ""),
                raw=fields,
            )

        return recover_serial(
            self.control,
            self.control_probe,
            attempts=attempts,
            delays_s=delays,
            prompt_timeout_s=prompt_timeout,
            on_event=on_event,
        )

    def ensure_audio_enabled(self) -> tuple[bool, str, list[str]]:
        if self._pa_enabled:
            return True, "PA already enabled", []
        ok, reason, evidence = self.apply_audio_init()
        if ok:
            self._pa_enabled = True
        return ok, reason, evidence

    def apply_audio_init(self) -> tuple[bool, str, list[str]]:
        """通过控制串口设置 PA/声卡常开，并用 pa-enable.show 验证。"""
        commands = [str(item) for item in self.audio_init.get("commands", []) if str(item).strip()]
        if not commands or self.control is None:
            return True, "未配置 audio_init", []
        cursor = self.control.snapshot()
        retries = max(0, int(self.audio_init.get("retries", 2)))
        replies: list[str] = []
        for command in commands:
            last: CommandResult | None = None
            for _ in range(retries + 1):
                last = send_control(
                    self.control, command, r"root:/\$",
                    gap_s=0.5, timeout_s=2.0, retries=0,
                )
                replies.append(last.ack_text)
                if last.ok and "cmd not found" not in last.ack_text.lower():
                    break
            if last is None or not last.ok or "cmd not found" in last.ack_text.lower():
                evidence = find_evidence(
                    self.control.events_since(cursor),
                    re.compile(r"pa-enable\.(?:set|show)|cmd not found", re.I),
                )
                return False, f"{command} 执行失败: {last.ack_text[-200:] if last else 'no reply'}", evidence

        verify_command = str(self.audio_init.get("verify_command", "")).strip()
        verify_pattern = str(self.audio_init.get("verify_pattern", "")).strip()
        if verify_command:
            verify = send_control(
                self.control, verify_command, r"root:/\$",
                gap_s=0.5, timeout_s=2.0, retries=0,
            )
            replies.append(verify.ack_text)
            ok = bool(verify_pattern) and verify_pattern in verify.ack_text and "cmd not found" not in verify.ack_text.lower()
            evidence = find_evidence(
                self.control.events_since(cursor),
                re.compile(r"pa-enable\.(?:set|show)|PA Enable|cmd not found", re.I),
            )
            if not ok:
                return False, f"{verify_command} 未确认 {verify_pattern}: {verify.ack_text[-200:]}", evidence
        return True, "PA/声卡常开命令已发送并确认", find_evidence(
            self.control.events_since(cursor), re.compile(r"pa-enable\.show|PA Enable", re.I),
        )

    @contextmanager
    def audio_slot(self):
        """Cross-process semaphore for wake/command playback (default 4 slots)."""
        slots = max(1, int(self.audio_lock.get("slots", 4)))
        stale_after = float(self.audio_lock.get("stale_after_s", 300))
        wait_timeout = float(self.audio_lock.get("wait_timeout_s", 1800))
        base = PROJECT_ROOT / "logs" / "audio_slots"
        base.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        acquired: Path | None = None
        slot_index = -1
        while acquired is None:
            for index in range(slots):
                path = base / f"slot_{index}"
                try:
                    path.mkdir()
                except FileExistsError:
                    try:
                        if time.time() - path.stat().st_mtime > stale_after:
                            shutil.rmtree(path, ignore_errors=True)
                    except OSError:
                        pass
                    continue
                acquired = path
                slot_index = index
                try:
                    (path / "owner.json").write_text(
                        json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8",
                    )
                except OSError:
                    pass
                break
            if acquired is None:
                if time.monotonic() - started > wait_timeout:
                    raise TimeoutError(f"audio slot wait timeout ({wait_timeout:.0f}s)")
                time.sleep(0.2)
        waited = time.monotonic() - started
        if waited > 0.5:
            self._emit(
                "SYSTEM", "AUDIO_SLOT_WAIT",
                f"?????? {waited:.1f}s??? slot_{slot_index}",
                case_id=self._current_case, status="PASS", elapsed_ms=round(waited * 1000),
            )
        try:
            yield slot_index
        finally:
            shutil.rmtree(acquired, ignore_errors=True)

    def wake_once(self) -> tuple[bool, bool, str, list[str], dict]:
        """Play the wake word once and report whether the device woke."""
        wake_patterns = [re.compile(str(item), re.I) for item in self.wake.get("wake_patterns", []) if str(item)]
        wake_cursor = self.csk.snapshot()
        control_cursor = self.control.snapshot()
        play_file = asset_path(Path(str(self.wake.get("audio_file", "wav/Wake/hellohaier.wav"))))
        play_key = str(self.wake.get("render_device_key", ""))
        proc = subprocess.Popen(
            [sys.executable, "-u", str(lstest_script("listenai_play.py")),
             "play", "--audio-file", str(play_file), "--device-key", play_key],
            cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CHILD_CREATIONFLAGS,
        )
        self.csk.wait_for(
            wake_cursor,
            lambda text: any(pattern.search(text) for pattern in wake_patterns) or "bootloader application" in text,
            float(self.wake.get("playback_timeout_s", 12.0)),
        )
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        play_output = proc.stdout.read().decode("gbk", "replace") if proc.stdout is not None else ""
        time.sleep(0.3)
        csk_events = self.csk.events_since(wake_cursor)
        control_events = self.control.events_since(control_cursor)
        csk_text = "\n".join(item.text for item in csk_events)
        control_text = "\n".join(item.text for item in control_events)
        woke = any(pattern.search(csk_text) for pattern in wake_patterns)
        has_miniplayer = (
            "miniplayer status: PLAYING" in csk_text
            or "miniplayer status: COMPLETE" in csk_text
        )
        playback = "spk.uas@start!!" in control_text
        if not playback and not self.require_spk_marker and has_miniplayer:
            playback = True
        evidence = [item.evidence for item in [*csk_events, *control_events] if item.text]
        return woke, playback, csk_text, evidence[-10:], {
            "woke": woke, "playback": playback, "playback_csk": has_miniplayer,
            "play_output": play_output[-300:],
        }

    def run_wake_test(self) -> tuple[bool, bool, str, list[str], dict]:
        """切换英文后用指定 Render 声卡播报唤醒词，并校验控制口播放标记和 CSK 唤醒标记。"""
        if not self.wake:
            return False, False, "manifest 缺少 wake 配置", [], {}
        language_command = str(self.wake.get("language_command", "")).strip()
        language_pattern = str(self.wake.get("language_success_pattern", "")).strip()
        wake_patterns = [re.compile(str(item), re.I) for item in self.wake.get("wake_patterns", []) if str(item)]
        start_pattern = str(self.wake.get("playback_start_pattern", "spk.uas@start!!"))
        stop_pattern = str(self.wake.get("playback_stop_pattern", "spk.uas@stop!!"))
        playback_timeout = float(self.wake.get("playback_timeout_s", 12.0))
        attempts = max(1, int(self.wake.get("attempts", 5)))
        details: dict = {"language": {}, "attempts": []}
        evidence: list[str] = []

        language_ok = True
        language_text = ""
        language_cursor = self.csk.snapshot()
        if language_command:
            language_result = send_control(
                self.csk, language_command, language_pattern,
                gap_s=1.0, timeout_s=float(self.wake.get("language_timeout_s", 20.0)), retries=1,
            )
            language_text = language_result.ack_text
            language_ok = language_result.ok
            details["language"] = {
                "command": language_command,
                "ok": language_ok,
                "attempts": language_result.attempts,
                "reply": language_text[-500:],
            }
            if language_ok:
                events, booted = self.csk.wait_for(
                    language_cursor, lambda text: "bootloader application" in text, 3.0,
                )
                if booted:
                    events, ready = self.csk.wait_for(
                        language_cursor,
                        lambda text: "bootloader application" in text and "AP Boot Ready" in text,
                        self.boot_timeout,
                    )
                    details["language"]["post_switch_boot"] = bool(ready)
                time.sleep(float(self.wake.get("post_switch_settle_s", 3.0)))

        playback_ok = False
        wake_ok = False
        last_reason = ""
        for attempt in range(1, attempts + 1):
            play_cursor = self.csk.snapshot()
            control_cursor = self.control.snapshot()
            play_file = asset_path(Path(str(self.wake.get("audio_file", "wav/Wake/hellohaier.wav"))))
            play_key = str(self.wake.get("render_device_key", ""))
            command = [
                sys.executable, "-u", str(lstest_script("listenai_play.py")),
                "play", "--audio-file", str(play_file), "--device-key", play_key,
            ]
            proc = subprocess.Popen(
                command, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CHILD_CREATIONFLAGS,
            )
            predicate = lambda text: any(pattern.search(text) for pattern in wake_patterns) or "bootloader application" in text
            self.csk.wait_for(play_cursor, predicate, playback_timeout)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            play_output = proc.stdout.read().decode("gbk", "replace") if proc.stdout is not None else ""
            time.sleep(0.5)
            csk_events = self.csk.events_since(play_cursor)
            control_events = self.control.events_since(control_cursor)
            csk_text = "\n".join(item.text for item in csk_events)
            control_text = "\n".join(item.text for item in control_events)
            has_start = start_pattern in control_text
            has_stop = stop_pattern in control_text
            has_miniplayer = (
                "miniplayer status: PLAYING" in csk_text
                or "miniplayer status: COMPLETE" in csk_text
            )
            has_wake = any(pattern.search(csk_text) for pattern in wake_patterns)
            reset = "bootloader application" in csk_text
            playback_observed = has_start or (not self.require_spk_marker and has_miniplayer)
            playback_count = control_text.count(start_pattern) or csk_text.count("miniplayer status: PLAYING")
            playback_complete = (stop_pattern in control_text) or ("miniplayer status: COMPLETE" in csk_text)
            wake_results = [
                str(value).strip()
                for value in re.findall(r"keyword:\s*([^\"\r\n]+)", csk_text)
                if str(value).strip()
            ]
            details["attempts"].append({
                "attempt": attempt,
                "playback_start": has_start,
                "playback_stop": has_stop,
                "playback_csk": has_miniplayer,
                "playback_count": playback_count,
                "playback_complete": playback_complete,
                "wake": has_wake,
                "wake_results": wake_results,
                "wake_result_count": len(wake_results),
                "reset": reset,
                "play_output": play_output[-500:],
                "wake_lines": [
                    item.text for item in csk_events
                    if any(pattern.search(item.text) for pattern in wake_patterns)
                ][:5],
            })
            evidence.extend(item.evidence for item in control_events if "spk.uas@" in item.text)
            evidence.extend(
                item.evidence for item in csk_events
                if any(pattern.search(item.text) for pattern in wake_patterns)
            )
            playback_ok = playback_observed and not reset
            wake_ok = has_wake and not reset
            if playback_ok and wake_ok:
                last_reason = f"第 {attempt} 次播报唤醒成功"
                break
            last_reason = (
                f"第 {attempt} 次失败: playback={has_start}, playback_csk={has_miniplayer}, "
                f"stop={has_stop}, wake={has_wake}, reset={reset}"
            )
            if attempt < attempts:
                if not self._pa_enabled:
                    self.ensure_audio_enabled()
                time.sleep(1.0)

        failed_attempts = sum(
            1 for item in details["attempts"]
            if not (
                (item.get("playback_start") or (not self.require_spk_marker and item.get("playback_csk")))
                and item.get("wake") and not item.get("reset")
            )
        )
        details["failed_attempts"] = failed_attempts
        details["max_attempts"] = attempts
        details["success"] = bool(playback_ok and wake_ok and language_ok)
        successful = next(
            (item for item in reversed(details["attempts"]) if item.get("wake") and not item.get("reset")),
            {},
        )
        details["wake_playback_count"] = int(successful.get("playback_count", 0) or 0)
        details["wake_playback_complete"] = bool(successful.get("playback_complete", False))
        details["wake_results"] = [
            value for item in details["attempts"] for value in item.get("wake_results", [])
        ]
        details["wake_result_count"] = len(details["wake_results"])
        details["multi_wake"] = any(
            int(item.get("wake_result_count", 0) or 0) > 1 for item in details["attempts"]
        )
        if not language_ok:
            last_reason = "英文切换未确认: " + language_text[-200:]
        if not details["success"]:
            last_reason = f"{last_reason}; 唤醒失败次数={failed_attempts}/{attempts}"
        return playback_ok, wake_ok, last_reason, evidence[-20:], details

    def run_interaction_test(self) -> tuple[bool, bool, str, list[str], dict]:
        """Wake first, then give the command word up to N recognition attempts."""
        cfg = self.interaction
        if not cfg:
            return False, False, "manifest missing interaction config", [], {}
        expected = str(cfg.get("expected_keyword", "")).strip()
        timeout_s = float(cfg.get("command_timeout_s", 15.0))
        attempts = max(1, int(cfg.get("attempts", 5)))
        response_patterns = [re.compile(str(item), re.I) for item in cfg.get("response_playback_patterns", []) if str(item)]
        expected_re = re.compile(re.escape(expected), re.I)

        details: dict = {
            "expected_keyword": expected,
            "max_attempts": attempts,
            "attempts": [],
        }
        evidence: list[str] = []
        successful_attempt = 0

        def response_after_keyword(text: str) -> bool:
            matches = list(expected_re.finditer(text))
            if not matches:
                return False
            tail = text[matches[-1].end():]
            return any(pattern.search(tail) for pattern in response_patterns)

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                rewoke, rewake_playback, rewake_text, rewake_evidence, rewake_details = self.wake_once()
                details.setdefault("rewakes", []).append({"attempt": attempt, **rewake_details})
                evidence.extend(rewake_evidence)
                if not rewoke:
                    details["attempts"].append({
                        "attempt": attempt,
                        "rewake": False,
                        "keyword_ok": False,
                        "response_ok": False,
                        "reset": False,
                    })
                    time.sleep(0.5)
                    continue
            play_cursor = self.csk.snapshot()
            control_cursor = self.control.snapshot()
            play_file = asset_path(Path(str(cfg.get("audio_file", "wav/Turn-on-aircon/BH123_SE_F003_normal_061.wav"))))
            play_key = str(self.wake.get("render_device_key", ""))
            command = [
                sys.executable, "-u", str(lstest_script("listenai_play.py")),
                "play", "--audio-file", str(play_file), "--device-key", play_key,
            ]
            proc = subprocess.Popen(
                command, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CHILD_CREATIONFLAGS,
            )
            predicate = lambda text: response_after_keyword(text) or "bootloader application" in text
            self.csk.wait_for(play_cursor, predicate, timeout_s)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            play_output = proc.stdout.read().decode("gbk", "replace") if proc.stdout is not None else ""
            time.sleep(0.5)
            csk_events = self.csk.events_since(play_cursor)
            control_events = self.control.events_since(control_cursor)
            csk_text = "\n".join(item.text for item in csk_events)
            control_text = "\n".join(item.text for item in control_events)
            crash = "Boot Reason: CRASH" in csk_text or "CSK AP Ready after exception" in csk_text
            reset = crash or "bootloader application" in csk_text

            asr_pattern = str(cfg.get("asr_pattern", "")).strip()
            asr_results: list[str] = []
            actual_keyword = ""
            if asr_pattern:
                for match in re.finditer(asr_pattern, csk_text, re.I):
                    value = (match.groupdict().get("keyword") or match.group(0)).strip()
                    if value and value.upper() != "HELLO HAIER":
                        asr_results.append(value)
                        actual_keyword = value
            keyword_present = bool(asr_results)
            keyword_correct = bool(expected) and expected.lower() in actual_keyword.lower()
            misrecognized = keyword_present and not keyword_correct
            outcome = "hit" if keyword_correct else ("misheard" if misrecognized else "empty")
            kid_match = re.search(r"vir_kid:\s*(\d+)", csk_text)
            response_ok = False
            if keyword_correct:
                kw_idx = max(i for i, item in enumerate(csk_events) if expected_re.search(item.text))
                response_ok = any(
                    any(pattern.search(item.text) for pattern in response_patterns)
                    for item in csk_events[kw_idx + 1:]
                )
            if reset:
                keyword_present = False
                keyword_correct = False
                misrecognized = False
                outcome = "empty"
                response_ok = False

            start_ok = "spk.uas@start!!" in control_text
            asr_response_count = 0
            asr_playback_count = 0
            asr_playback_complete = False
            if keyword_present:
                kw_event_idx = max(
                    i for i, item in enumerate(csk_events)
                    if "keyword:" in item.text and "HELLO HAIER" not in item.text
                )
                tail_text = "\n".join(item.text for item in csk_events[kw_event_idx + 1:])
                asr_response_count = tail_text.count("offline_tts_callback")
                if not asr_response_count:
                    asr_response_count = sum(
                        1 for pattern in response_patterns for _ in pattern.finditer(tail_text)
                    )
                asr_playback_count = tail_text.count("miniplayer status: PLAYING")
                asr_playback_complete = "miniplayer status: COMPLETE" in tail_text
            attempt_ok = keyword_present and not reset
            details["attempts"].append({
                "attempt": attempt,
                "playback_start": start_ok,
                "keyword_ok": keyword_present,
                "keyword_correct": keyword_correct,
                "misrecognized": misrecognized,
                "outcome": outcome,
                "response_ok": response_ok,
                "asr_response_count": asr_response_count,
                "asr_playback_count": asr_playback_count,
                "asr_playback_complete": asr_playback_complete,
                "asr_results": asr_results,
                "asr_result_count": len(asr_results),
                "multi_asr": len(asr_results) > 1,
                "keyword": actual_keyword,
                "vir_kid": int(kid_match.group(1)) if kid_match else None,
                "reset": reset,
                "play_output": play_output[-300:],
            })
            evidence.extend(item.evidence for item in control_events if "spk.uas@" in item.text)
            evidence.extend(
                item.evidence for item in csk_events
                if expected_re.search(item.text) or any(pattern.search(item.text) for pattern in response_patterns)
                or (asr_pattern and re.search(asr_pattern, item.text, re.I))
            )
            if attempt_ok:
                successful_attempt = attempt
                break
            if reset:
                break
            if not self._pa_enabled:
                self.ensure_audio_enabled()
            time.sleep(0.5)

        details["success"] = bool(successful_attempt)
        details["pa_enabled"] = bool(self._pa_enabled)
        details["success_attempt"] = successful_attempt
        details["failed_attempts"] = sum(
            1 for item in details["attempts"]
            if not (item.get("keyword_ok") and not item.get("reset"))
        )
        last = details["attempts"][-1] if details["attempts"] else {}
        outcome = last.get("outcome", "empty")
        details["asr_response_count"] = int(last.get("asr_response_count", 0) or 0)
        details["asr_playback_count"] = int(last.get("asr_playback_count", 0) or 0)
        details["asr_playback_complete"] = bool(last.get("asr_playback_complete", False))
        details["asr_results"] = [
            value for item in details["attempts"] for value in item.get("asr_results", [])
        ]
        details["asr_result_count"] = len(details["asr_results"])
        details["multi_asr"] = any(bool(item.get("multi_asr", False)) for item in details["attempts"])
        reason = (
            f"ASR={expected}, attempts={len(details['attempts'])}/{attempts}, "
            f"result={outcome}, actual={last.get('keyword') or '-'}, reset={last.get('reset')}"
        )
        keyword_hit = any(item.get("keyword_ok") for item in details["attempts"])
        response_hit = any(item.get("keyword_ok") and item.get("response_ok") for item in details["attempts"])
        if reset:
            reason = "device CRASH/reset during interaction; " + reason
        elif not successful_attempt:
            reason = "wake/ASR command did not hit within attempts; " + reason
        return bool(keyword_hit), bool(response_hit), reason, evidence[-20:], details

    def start_collectors(self) -> None:
        assert self.artifacts is not None
        self.csk = SerialCollector(
            self.csk_port, self.csk_baud, "csk", "csk",
            self.artifacts.serial_log_path(self.csk_port, "csk"),
            on_event=self.dispatch, on_error=self.on_serial_error,
        )
        self.csk.start()
        if not self.args.no_hisi:
            try:
                self.hisi = SerialCollector(
                    self.hisi_port, self.hisi_baud, "hisi", "hisi",
                    self.artifacts.serial_log_path(self.hisi_port, "hisi"),
                    on_event=self.dispatch, on_error=self.on_serial_error,
                )
                self.hisi.start()
            except Exception as exc:
                self.hisi = None
                self._emit("SYSTEM", "HISI_SKIP", f"HISI 采集跳过: {exc}", level="WARN", status="WARN")
        self.control = SerialCollector(
            self.control_port, self.control_baud, "control", "control",
            self.artifacts.serial_log_path(self.control_port, "control"),
            on_event=self.dispatch, on_error=self.on_serial_error,
        )
        self.control.start()

        if self.mode == "burn":
            self.burn_log = SerialCollector(
                self.burn_port, 115200, "burn", "burn_tool",
                self.artifacts.serial_log_path(self.burn_port, "burn"),
                on_event=self.dispatch, on_error=self.on_serial_error,
            )
            self.burn_log.open_log_only()
            burn_tool = asset_path(Path("burn") / "Uart_Burn_Tool.exe")
            self.burn_runner = BurnToolRunner(
                burn_tool,
                burn_tool.parent,
                self.burn_log,
                on_error=self.on_serial_error,
            )

    def stop_collectors(self) -> None:
        for collector in (self.control, self.csk, self.hisi, self.burn_log):
            if collector is not None:
                try:
                    collector.stop()
                except Exception:
                    pass

    # ---------- round ----------
    def run_round(self, index: int) -> str:
        assert self.artifacts is not None and self.runtime is not None and self.control is not None and self.csk is not None
        case_id = f"{self.case_prefix}_{index:04d}"
        self._last_round_stats = {}
        required_facts = ["COMMAND_ACK", "COMMAND_EVIDENCE", "RESTART", "INIT_READY"]
        if self.args.wake_test:
            required_facts.append("WAKE")
        timeline = {
            "required_facts": required_facts,
            "mirror_policy": {},
            "mirror_window_ms": 120,
            "reason_labels": {
                "MISSING_COMMAND_ACK": "控制命令回执缺失",
                "MISSING_COMMAND_EVIDENCE": "烧录/启动旁证缺失",
                "MISSING_RESTART": "未观察到 CSK 重启",
                "MISSING_INIT_READY": "未观察到 CSK 应用启动就绪",
                "MISSING_WAKE": "未观察到唤醒事件",
            },
            "duplicate_policy": {},
        }
        try:
            self.runtime.declare_case({
                "case_id": case_id,
                "scenario": self.scenario_name,
                "input_text": self.fw_key,
                "timeline": timeline,
                "source_file": display_path(self.fw_path),
                "source_sha256": self.fw_entry.get("source_sha256", ""),
            })
            self.runtime.open_case_window(
                case_id,
                cursors={self.control_port: self.control.snapshot(), self.csk_port: self.csk.snapshot()},
            )
        except Exception as exc:
            self._current_case = ""
            self.artifacts.record_anomaly(
                "CASE_START_FAILED",
                "用例声明/窗口创建失败，跳过本轮并继续。",
                case_id=case_id, error=f"{type(exc).__name__}: {exc}",
                handling="跳过本轮，继续下一轮",
            )
            return "FAIL"
        self._current_case = case_id
        started_at = now_human()
        checks: list[dict] = []
        command_results: list[CommandResult] = []
        init_results: list[CommandResult] = []
        init_ok = False
        wake_details: dict = {}
        interaction_details: dict = {}
        round_outcome = "empty"
        asr_skipped = self.acceptance_mode in {"wake_only", "wake_playback"}
        wake_results: list[str] = []
        asr_results: list[str] = []
        multi_result = False
        multi_code = ""
        burn_result = None
        burn_events: list[LineEvent] = []
        boot_events: list[LineEvent] = []
        restored = False
        exception_text = ""

        def add_check(name: str, ok: bool, reason: str = "", evidence: list[str] | None = None, **fields) -> None:
            checks.append({"name": name, "ok": bool(ok), "reason": reason, **fields})
            self.artifacts.add_check(
                case_id, name, "PASS" if ok else "FAIL", required=True,
                reason="正常" if ok else reason, evidence=evidence or (), **fields,
            )

        try:
            self._round_infra_failure = False
            alive, reply = self.control_probe()
            if not alive:
                self._emit(
                    "SYSTEM", "CONTROL_PROBE_FAIL",
                    "控制串口无提示符，尝试重启采集串口。",
                    level="WARN", status="FAIL", reason=reply,
                )
                alive, reply, recovery_evidence = self.recover_control_serial()
            else:
                recovery_evidence = []
            if not alive:
                self._round_infra_failure = True
                add_check("control_alive", False, f"控制串口无响应: {reply}", recovery_evidence)
                raise RuntimeError(f"control serial unresponsive: {reply}")

            if self.mode == "powercycle":
                sequence = [
                    ("uut-csk-boot.off", r"csk6012_boot_set:\s*0\b", "boot_exit"),
                    ("uut-switch1.off", r"switch_idx1_set:\s*0\b", "power_off"),
                ]
            else:
                sequence = [
                    ("uut-switch1.off", r"switch_idx1_set:\s*0\b", "power_off"),
                    ("uut-csk-boot.off", r"csk6012_boot_set:\s*0\b", "boot_exit_pre"),
                    ("uut-csk-boot.on", r"csk6012_boot_set:\s*1\b", "boot_enter"),
                    ("uut-switch1.on", r"switch_idx1_set:\s*1\b", "power_on"),
                ]
            for command, ack, phase in sequence:
                result = send_control(
                    self.control, command, ack,
                    gap_s=self.command_gap, timeout_s=self.ack_timeout, retries=self.control_retries,
                )
                command_results.append(result)
                self._emit(
                    "ACTION", "CONTROL_COMMAND",
                    f"发送 {command}，回执{'正常' if result.ok else '异常'}",
                    phase=phase, status="PASS" if result.ok else "FAIL",
                    reason=result.error, raw={"command": command, "reply": result.ack_text[-200:]},
                )
            if command_results and all(not item.ok for item in command_results) and all(
                not item.raw_text.strip() for item in command_results
            ):
                self._round_infra_failure = True
                self._emit(
                    "SYSTEM", "CONTROL_NO_RESPONSE",
                    "控制命令均无任何回执，判定控制链路异常。",
                    level="ERROR", status="FAIL",
                )

            time.sleep(self.pre_burn_wait)
            if self.mode == "powercycle":
                self._emit("ACTION", "BURN_SKIPPED", "powercycle 模式不烧录", phase="burn", status="SKIP")
                boot_cursor = self.csk.snapshot()
                result = send_control(
                    self.control, "uut-switch1.on", r"switch_idx1_set:\s*1\b",
                    gap_s=self.command_gap, timeout_s=self.ack_timeout, retries=self.control_retries,
                )
                command_results.append(result)
                self._emit(
                    "ACTION", "CONTROL_COMMAND",
                    f"发送 uut-switch1.on，回执{'正常' if result.ok else '异常'}",
                    phase="power_on", status="PASS" if result.ok else "FAIL",
                    reason=result.error, raw={"command": "uut-switch1.on", "reply": result.ack_text[-200:]},
                )
            else:
                if self.args.skip_burn:
                    self._emit("ACTION", "BURN_SKIPPED", "按 --skip-burn 跳过烧录", phase="burn", status="SKIP")
                else:
                    assert self.burn_runner is not None and self.burn_log is not None
                    burn_cursor = self.burn_log.snapshot()
                    burn_result = self.burn_runner.run(
                        self.fw_path, port=self.burn_port, timeout_s=self.burn_timeout,
                    )
                    burn_events = self.burn_log.events_since(burn_cursor)
                    self._emit(
                        "ACTION", "BURN_FINISHED",
                        f"烧录结束，退出码={burn_result.exit_code}，超时={burn_result.timed_out}",
                        phase="burn", status="PASS" if burn_result.exit_code == 0 and not burn_result.timed_out else "FAIL",
                        reason=burn_result.error, elapsed_ms=burn_result.duration_ms,
                    )

                boot_cursor = self.csk.snapshot()
                restore_sequence = [
                    ("uut-csk-boot.off", r"csk6012_boot_set:\s*0\b", "boot_exit"),
                    ("uut-switch1.off", r"switch_idx1_set:\s*0\b", "power_cycle_off"),
                    ("uut-switch1.on", r"switch_idx1_set:\s*1\b", "power_cycle_on"),
                ]
                for command, ack, phase in restore_sequence:
                    result = send_control(
                        self.control, command, ack,
                        gap_s=self.command_gap, timeout_s=self.ack_timeout, retries=self.control_retries,
                    )
                    command_results.append(result)
                    self._emit(
                        "ACTION", "CONTROL_COMMAND",
                        f"发送 {command}，回执{'正常' if result.ok else '异常'}",
                        phase=phase, status="PASS" if result.ok else "FAIL",
                        reason=result.error, raw={"command": command, "reply": result.ack_text[-200:]},
                    )
            restored = True

            self._emit("ACTION", "BOOT_OBSERVE", f"等待 CSK 启动证据，最长 {self.boot_timeout:.0f}s", phase="boot_observe", status="RUN")
            boot_events, ready_found = self.csk.wait_for(
                boot_cursor,
                lambda text: bool(FATAL_RE.search(text) or "AP Boot Ready !!!" in text),
                self.boot_timeout,
            )
            if ready_found and "AP Boot Ready !!!" in "\n".join(item.text for item in boot_events):
                time.sleep(self.post_ready_settle)
                boot_events = self.csk.events_since(boot_cursor)
            boot_text = "\n".join(item.text for item in boot_events)
            volume_matches = re.findall(r"Volume\s*:\s*(\d+)", boot_text)
            current_volume = int(volume_matches[-1]) if volume_matches else None
            all_events = [*burn_events, *boot_events]

            add_check("control_ack", all(item.ok for item in command_results),
                      "控制命令回执异常: " + "; ".join(item.command for item in command_results if not item.ok),
                      [event.evidence for event in self.control.events_since(0)][-8:])

            if not self.args.skip_burn and burn_result is not None:
                text = burn_result.text
                exit_ok = burn_result.exit_code == 0 and not burn_result.timed_out
                add_check("burn_exit", exit_ok,
                          f"exit={burn_result.exit_code}, timeout={burn_result.timed_out}, error={burn_result.error}",
                          find_evidence(burn_events, re.compile(r"FLASH|MD5|CONNECT", re.I)))
                add_check("burn_connect", "CONNECT WITH CHIP SUCCESS" in text,
                          "未出现 CONNECT WITH CHIP SUCCESS", find_evidence(burn_events, re.compile(r"CONNECT WITH CHIP", re.I)))
                chip_match = CHIP_RE.search(text)
                expected_chip = str(self.manifest.get("chip_type", ""))
                add_check("burn_chip", bool(chip_match) and chip_match.group("chip").lower() == expected_chip.lower(),
                          f"chip={chip_match.group('chip') if chip_match else 'missing'} expected={expected_chip}",
                          find_evidence(burn_events, CHIP_RE))
                add_check("burn_baud", "CHANGE BAUDRATE COMMAND SUCCESS" in text,
                          "未出现 CHANGE BAUDRATE COMMAND SUCCESS", find_evidence(burn_events, re.compile(r"CHANGE BAUDRATE", re.I)))
                size_match = WRITE_SIZE_RE.search(text)
                expected_size = int(self.fw_entry.get("expected_write_size", 0) or 0)
                size_ok = bool(size_match) and (not expected_size or int(size_match.group("size")) == expected_size)
                add_check("burn_write_size", size_ok,
                          f"size={size_match.group('size') if size_match else 'missing'} expected={expected_size}",
                          find_evidence(burn_events, WRITE_SIZE_RE))
                md5_match = MD5_RE.search(text)
                expected_md5 = normalize_md5(str(self.fw_entry.get("expected_tool_md5", "")))
                md5_ok = bool(md5_match) and (not expected_md5 or normalize_md5(md5_match.group("md5")) == expected_md5)
                add_check("burn_md5", md5_ok,
                          f"md5={md5_match.group('md5') if md5_match else 'missing'} expected=0x{expected_md5}",
                          find_evidence(burn_events, MD5_RE))
                add_check("burn_done", bool(FLASH_DONE_RE.search(text)),
                          "未出现 FLASH DOWNLOAD SUCCESS / SECTOR DOWNLOAD SUCCESS",
                          find_evidence(burn_events, FLASH_DONE_RE))
                add_check("burn_send_end", "FLASH DATA SEND END SUCCESS" in text,
                          "未出现 FLASH DATA SEND END SUCCESS",
                          find_evidence(burn_events, re.compile(r"FLASH DATA SEND END", re.I)))
                add_check("burn_progress", bool(PROGRESS_100_RE.search(text)),
                          "未出现 FLASH DATA SEND PROGESS: 100%",
                          find_evidence(burn_events, PROGRESS_100_RE))

            version_match = None
            for match in VERSION_RE.finditer(boot_text):
                version_match = match
            expected_version = str(self.fw_entry.get("expected_version", ""))
            version_ok = bool(version_match) and version_match.group("version").startswith(expected_version)
            add_check("csk_version", version_ok,
                      f"version={version_match.group('version') if version_match else 'missing'} expected_prefix={expected_version}",
                      find_evidence(boot_events, VERSION_RE))
            build_match = None
            for match in BUILD_RE.finditer(boot_text):
                build_match = match
            expected_build = str(self.fw_entry.get("expected_build", ""))
            build_ok = bool(build_match) and build_match.group("build") == expected_build
            add_check("csk_build", build_ok,
                      f"build={build_match.group('build') if build_match else 'missing'} expected={expected_build}",
                      find_evidence(boot_events, BUILD_RE))
            add_check("csk_bootloader", bool(BOOTLOADER_RE.search(boot_text)),
                      "未观察到 bootloader 启动", find_evidence(boot_events, BOOTLOADER_RE))
            add_check("csk_ready", "AP Boot Ready !!!" in boot_text,
                      "未观察到 AP Boot Ready !!!", find_evidence(boot_events, re.compile(r"AP Boot Ready", re.I)))
            fatal_matches = FATAL_RE.findall(boot_text)
            add_check("csk_no_fatal", not fatal_matches,
                      "检测到致命日志: " + ",".join(sorted(set(fatal_matches))),
                      find_evidence(boot_events, FATAL_RE))
            boot_count = len(BOOTLOADER_RE.findall(boot_text))
            max_boot = int(self.verification.get("max_bootloader_count", 5))
            add_check("csk_boot_count", boot_count <= max_boot,
                      f"bootloader 出现 {boot_count} 次，上限 {max_boot}", [])

            init_command = str(self.csk_init.get("command", "")).strip()
            if init_command:
                time.sleep(self.csk_init_wait)
                init_cursor = self.csk.snapshot()
                success_pattern = str(self.csk_init.get("success_pattern", "")).strip()
                max_attempts = max(1, int(self.csk_init.get("retries", 5)) + 1)
                attempt_timeout = float(self.csk_init.get("attempt_timeout_s", 3.0))
                attempt_gap = float(self.csk_init.get("attempt_gap_s", 0.8))
                for attempt in range(1, max_attempts + 1):
                    result = send_control(
                        self.csk, init_command, success_pattern,
                        gap_s=attempt_gap, timeout_s=attempt_timeout, retries=0,
                    )
                    init_results.append(result)
                    self._emit(
                        "ACTION", "CSK_INIT_COMMAND",
                        f"发送 {init_command}，第 {attempt} 次{'成功' if result.ok else '失败'}",
                        phase="init_command", status="PASS" if result.ok else "FAIL",
                        reason=result.error, raw={"command": init_command, "reply": result.ack_text[-300:], "attempt": attempt},
                    )
                    if result.ok:
                        break
                    time.sleep(0.2)
                init_ok = any(item.ok for item in init_results)
                init_events = self.csk.events_since(init_cursor)
                last_reply = init_results[-1].ack_text[-300:] if init_results else ""
                add_check(
                    "loglevel_set", init_ok,
                    f"command={init_command}, attempts={len(init_results)}, success={success_pattern}, last={last_reply}",
                    find_evidence(init_events, re.compile(r"flash set log_lev|flash write fail|cmd not found", re.I)),
                )

            if self.args.interaction_test:
                pre_volume = self.interaction.get("pre_command_volume")
                if pre_volume not in (None, ""):
                    send_control(
                        self.csk, f"player.vol.set {int(pre_volume)}",
                        r"vol\s+\d+\s*->\s*\d+|root:/\$",
                        gap_s=0.5, timeout_s=2.0, retries=0,
                    )
                    current_volume = int(pre_volume)

            with self.audio_slot():
                if self.args.wake_test:
                    playback_ok, wake_ok, wake_reason, wake_evidence, wake_details = self.run_wake_test()
                    language_ok = bool(wake_details.get("language", {}).get("ok", True))
                    add_check("wake_language", language_ok,
                              "英文切换未确认" if not language_ok else "英文模式已确认",
                              wake_evidence)
                    add_check("wake_playback", playback_ok, wake_reason, wake_evidence)
                    add_check("wake_result", wake_ok, wake_reason, wake_evidence)
                    wake_failures = int(wake_details.get("failed_attempts", 0))
                    max_attempts = int(wake_details.get("max_attempts", 0))
                    self._emit(
                        "SYSTEM", "WAKE_ATTEMPTS",
                        f"本轮唤醒失败次数={wake_failures}/{max_attempts}",
                        case_id=case_id,
                        status="PASS" if wake_ok else "FAIL",
                        raw={"wake_failures": wake_failures, "max_attempts": max_attempts},
                    )
                    wake_results = list(wake_details.get("wake_results", []) or [])

                    def acceptance():
                        return evaluate_round(
                            round_outcome,
                            self.acceptance_mode,
                            wake_playback_count=int(wake_details.get("wake_playback_count", 0) or 0),
                            wake_playback_complete=bool(wake_details.get("wake_playback_complete", False)),
                            asr_response_count=int(interaction_details.get("asr_response_count", 0) or 0),
                            asr_playback_count=int(interaction_details.get("asr_playback_count", 0) or 0),
                            asr_playback_complete=bool(interaction_details.get("asr_playback_complete", False)),
                            multi_result=bool(
                                wake_details.get("multi_wake", False)
                                or interaction_details.get("multi_asr", False)
                            ),
                        )

                    if self.args.interaction_test:
                        min_volume = int(self.interaction.get("min_volume", 40))
                        if not wake_ok:
                            interaction_details = {"skipped": "wake_failed"}
                            round_outcome = "asleep"
                            self._emit(
                                "SYSTEM", "INTERACTION_SKIPPED",
                                "唤醒未成功，跳过命令词识别",
                                case_id=case_id, status="FAIL", raw=interaction_details,
                            )
                            accepted, accept_reason = acceptance()
                            add_check(
                                "interaction_acceptance", accepted, accept_reason, [],
                                outcome=round_outcome, mode=self.acceptance_mode,
                            )
                        elif current_volume is not None and current_volume <= min_volume:
                            interaction_details = {
                                "boundary": True,
                                "current_volume": current_volume,
                                "min_volume": min_volume,
                            }
                            add_check(
                                "volume_boundary", True,
                                f"???? {current_volume} <= ???? {min_volume}????????",
                                [],
                            )
                            self._emit(
                                "SYSTEM", "VOLUME_BOUNDARY",
                                f"???????? {current_volume}/{min_volume}????????",
                                case_id=case_id, status="PASS", raw=interaction_details,
                            )
                        else:
                            if self.acceptance_mode in {"wake_only", "wake_playback"}:
                                asr_skipped = True
                                round_outcome = "empty"
                                self._emit(
                                    "SYSTEM", "INTERACTION_SKIPPED",
                                    "wake_only 验收，跳过 ASR",
                                    case_id=case_id, status="SKIP",
                                )
                            else:
                                keyword_ok, response_ok, interaction_reason, interaction_evidence, interaction_details = self.run_interaction_test()
                                attempts = interaction_details.get("attempts", []) if interaction_details else []
                                round_outcome = str(attempts[-1].get("outcome", "empty")) if attempts else "empty"
                                asr_results = list(interaction_details.get("asr_results", []) or [])
                                if round_outcome in {"hit", "misheard"}:
                                    self._asr_result_seen = True
                                self._emit(
                                    "SYSTEM", "ASR_RESPONSE",
                                    f"识别播报={'已观察到' if response_ok else '未观察到（仅记录）'}",
                                    case_id=case_id,
                                    status="PASS" if response_ok else "WARN",
                                    raw={"response_ok": response_ok},
                                )
                                self._emit(
                                    "SYSTEM", "INTERACTION_RESULT",
                                    interaction_reason,
                                    case_id=case_id,
                                    status="PASS" if round_outcome in {"hit", "misheard"} else "FAIL",
                                    raw=interaction_details,
                                )
                            accepted, accept_reason = acceptance()
                            add_check(
                                "interaction_acceptance", accepted, accept_reason, [],
                                outcome=round_outcome, mode=self.acceptance_mode,
                            )
                    if self.acceptance_mode in {"wake_only", "wake_playback"}:
                        round_outcome = "empty" if wake_ok else "asleep"
                        accepted, accept_reason = acceptance()
                        add_check(
                            "interaction_acceptance", accepted, accept_reason, [],
                            outcome=round_outcome, mode=self.acceptance_mode,
                        )
                    multi_result = bool(
                        wake_details.get("multi_wake", False)
                        or interaction_details.get("multi_asr", False)
                    )
                    if multi_result:
                        if wake_details.get("multi_wake") and interaction_details.get("multi_asr"):
                            multi_code = "MULTI_RESULT"
                        elif wake_details.get("multi_wake"):
                            multi_code = "MULTI_WAKE"
                        else:
                            multi_code = "MULTI_ASR"
                        if rejects_multi_result(self.acceptance_mode):
                            self.artifacts.record_anomaly(
                                multi_code, "单轮出现多个唤醒或识别结果",
                                case_id=case_id, wake_results=wake_results, asr_results=asr_results,
                            )
                        self._emit(
                            "SYSTEM", "MULTI_RESULT",
                            f"{multi_code}: 单轮出现多个唤醒或识别结果（{self.acceptance_mode} 仅记录）",
                            case_id=case_id, level="WARN", status="WARN",
                            raw={"wake_results": wake_results, "asr_results": asr_results},
                        )
        except Exception as exc:
            exception_text = f"{type(exc).__name__}: {exc}"
            self.artifacts.record_anomaly(
                "ROUND_EXCEPTION",
                "本轮执行异常，已完成动作保留证据并继续下一轮。",
                case_id=case_id, error=exception_text, handling="本轮收尾后继续",
            )
            add_check("round_exception", False, exception_text)
        finally:
            if not restored and self.control is not None:
                for command, ack in (
                    ("uut-csk-boot.off", r"csk6012_boot_set:\s*0\b"),
                    ("uut-switch1.off", r"switch_idx1_set:\s*0\b"),
                    ("uut-switch1.on", r"switch_idx1_set:\s*1\b"),
                ):
                    try:
                        send_control(self.control, command, ack, gap_s=0.5, timeout_s=1.0, retries=self.control_retries)
                    except Exception:
                        pass
            self._current_case = ""
            try:
                self.runtime.close_case_window(case_id)
            except Exception as exc:
                self.artifacts.record_anomaly(
                    "CASE_CLOSE_FAILED", "??????????????",
                    case_id=case_id, error=f"{type(exc).__name__}: {exc}", handling="????",
                )
            failed = [item for item in checks if not item["ok"]]
            status = "PASS" if checks and not failed else "FAIL"
            failure_category = ""
            if status != "PASS":
                names = [item["name"] for item in failed]
                burn_names = {
                    "control_ack", "control_alive", "audio_pa_enable", "loglevel_set",
                    "burn_exit", "burn_connect", "burn_chip", "burn_baud",
                    "burn_write_size", "burn_md5", "burn_done", "burn_send_end",
                    "burn_progress", "csk_version", "csk_build", "csk_bootloader",
                    "csk_ready", "csk_no_fatal", "csk_boot_count", "round_exception",
                }
                if any(name in burn_names or name.startswith("burn_") or name.startswith("csk_") for name in names):
                    failure_category = "burn"
                elif any(name.startswith("wake_") for name in names):
                    failure_category = "wake"
                elif any(name in {"asr_keyword"} for name in names):
                    failure_category = "recognition"
                else:
                    failure_category = "other"
                self._emit(
                    "SYSTEM", "FAILURE_CATEGORY",
                    f"本轮失败分类={failure_category}",
                    case_id=case_id, status="FAIL",
                    raw={"category": failure_category, "checks": names},
                )
            reason = "OK" if status == "PASS" else "; ".join(
                f"{item['name']}:{item['reason']}" for item in failed
            ) or "无有效检查结果"
            evidence = []
            for event in [*burn_events, *boot_events]:
                evidence.append(event.evidence)
            facts = {
                "started_at": started_at,
                "scenario": self.scenario_name,
                "round_outcome": round_outcome,
                "acceptance_mode": self.acceptance_mode,
                "asr_skipped": asr_skipped,
                "wake_results": wake_results,
                "asr_results": asr_results,
                "wake_result_count": len(wake_results),
                "asr_result_count": len(asr_results),
                "multi_result": multi_result,
                "anomaly_codes": [multi_code] if multi_code else [],
                "input_text": self.fw_key,
                "firmware": {
                    "kind": self.fw_key,
                    "path": display_path(self.fw_path),
                    "sha256": self.fw_entry.get("source_sha256", ""),
                },
                "commands": [
                    {"command": item.command, "ok": item.ok, "attempts": item.attempts,
                     "reply": item.ack_text[-400:], "duration_ms": item.duration_ms}
                    for item in command_results
                ],
                "wake": wake_details,
                "interaction": interaction_details,
                "wake_failures": int(wake_details.get("failed_attempts", 0)),
                "csk_init": {
                    "command": self.csk_init.get("command", ""),
                    "ok": init_ok,
                    "attempts": len(init_results),
                    "replies": [item.ack_text[-300:] for item in init_results],
                },
                "burn": None if burn_result is None else {
                    "exit_code": burn_result.exit_code,
                    "timed_out": burn_result.timed_out,
                    "duration_ms": burn_result.duration_ms,
                    "md5": MD5_RE.search(burn_result.text).group("md5") if MD5_RE.search(burn_result.text) else "",
                    "write_size": WRITE_SIZE_RE.search(burn_result.text).group("size") if WRITE_SIZE_RE.search(burn_result.text) else "",
                    "error": burn_result.error,
                },
                "checks": checks,
                "failure_category": failure_category,
                "exception": exception_text,
            }
            case_result = CaseResult(
                case_id=case_id,
                raw_status=status,
                reason=reason,
                evidence=evidence[-20:],
                facts=facts,
            )
            try:
                self.runtime.record_case(case_result)
            except Exception as exc:
                self.artifacts.record_anomaly(
                    "CASE_RECORD_FAILED", "???????????????",
                    case_id=case_id, error=f"{type(exc).__name__}: {exc}", handling="?????",
                )
            status = case_result.reviewed_status or status
            print(f"[{now_human()}] ROUND {index}/{self.args.rounds} {status} {case_result.reason or reason}", flush=True)
            if status == "FAIL":
                if (
                    self._round_infra_failure
                    or any(item.infra_error for item in [*command_results, *init_results])
                    or (self.control and self.control.error)
                    or (self.csk and self.csk.error)
                ):
                    self._infra_streak += 1
                else:
                    self._infra_streak = 0
            else:
                self._infra_streak = 0
        self._last_round_stats = {
            "round": index,
            "status": status,
            "outcome": round_outcome,
            "wake_results": list(wake_results),
            "asr_results": list(asr_results),
            "multi_wake": bool(wake_details.get("multi_wake", False)),
            "multi_asr": bool(interaction_details.get("multi_asr", False)),
            "multi_result": bool(multi_result),
            "asr_skipped": bool(asr_skipped),
        }
        return status

    # ---------- main ----------
    def run(self) -> int:
        self.setup()
        assert self.artifacts is not None
        errors = self.preflight()
        if errors:
            for error in errors:
                self._emit("SYSTEM", "PREFLIGHT_ERROR", error, level="ERROR", status="FAIL")
                self.artifacts.add_sticky("PREFLIGHT_FAILED", error)
            code = "BLOCKED_PROFILE_CONTRACT" if any("profile" in item or "manifest" in item for item in errors) else "INFRASTRUCTURE_UNAVAILABLE"
            self.artifacts.request_stop(code, "预检失败: " + "; ".join(errors[:3]))
            summary = self.artifacts.finalize("BLOCKED", "预检失败")
            print(f"[{now_human()}] PREFLIGHT BLOCKED: {summary['result_directory']}", flush=True)
            return 2
        if self.args.preflight:
            summary = self.artifacts.finalize("PASS", "preflight 通过")
            print(f"[{now_human()}] PREFLIGHT PASS: {summary['result_directory']}", flush=True)
            return 0

        stats = {
            "rounds": 0, "pass": 0, "fail": 0,
            "hit": 0, "misheard": 0, "empty": 0, "asleep": 0,
            "multi_wake": 0, "multi_asr": 0, "multi_result": 0,
        }
        try:
            self.start_collectors()
            for index in range(1, self.args.rounds + 1):
                stop_reason = self.artifacts.check_runtime()
                if stop_reason:
                    self._emit("SYSTEM", "STOP_REQUESTED", f"收到停止请求: {stop_reason}", level="WARN", status="STOP")
                    break
                round_status = "FAIL"
                try:
                    round_status = self.run_round(index)
                except Exception as exc:
                    self._current_case = ""
                    try:
                        self.artifacts.record_anomaly(
                            "ROUND_UNCAUGHT_EXCEPTION",
                            "本轮出现未捕获异常，已跳过并继续下一轮。",
                            case_id=f"round_{index:04d}",
                            error=f"{type(exc).__name__}: {exc}",
                            handling="继续下一轮",
                        )
                    except Exception:
                        pass
                last = dict(self._last_round_stats or {})
                stats["rounds"] += 1
                if round_status == "PASS":
                    stats["pass"] += 1
                else:
                    stats["fail"] += 1
                outcome = str(last.get("outcome") or "empty")
                if outcome in {"hit", "misheard", "empty", "asleep"}:
                    stats[outcome] += 1
                if last.get("multi_wake"):
                    stats["multi_wake"] += 1
                if last.get("multi_asr"):
                    stats["multi_asr"] += 1
                if last.get("multi_result"):
                    stats["multi_result"] += 1
                print(
                    f"[STATS] round={index}/{self.args.rounds} status={round_status} "
                    f"outcome={outcome} wake_results={len(last.get('wake_results', []))} "
                    f"asr_results={len(last.get('asr_results', []))} "
                    f"multi_wake={bool(last.get('multi_wake'))} "
                    f"multi_asr={bool(last.get('multi_asr'))} "
                    f"| pass={stats['pass']} fail={stats['fail']} "
                    f"hit={stats['hit']} misheard={stats['misheard']} "
                    f"empty={stats['empty']} asleep={stats['asleep']} "
                    f"multi_result={stats['multi_result']}",
                    flush=True,
                )
                if self._infra_streak >= 3:
                    self.artifacts.request_stop("INFRASTRUCTURE_UNAVAILABLE", "连续 3 轮控制/CSK 基础设施异常")
                    break
        except KeyboardInterrupt:
            self.artifacts.request_stop("USER_STOP", "用户中断")
            self._emit("SYSTEM", "USER_STOP", "用户中断，正在收尾", level="WARN", status="STOP")
        except Exception as exc:
            self.artifacts.record_anomaly(
                "COLLECTOR_START_FAILED",
                "采集器启动失败，任务受控收尾。",
                error=f"{type(exc).__name__}: {exc}",
                handling="请求基础设施停止",
            )
            self.artifacts.request_stop("INFRASTRUCTURE_UNAVAILABLE", f"采集器启动失败: {exc}")
        finally:
            self.stop_collectors()

        print(
            f"[STATS] FINAL mode={self.acceptance_mode} rounds={stats['rounds']} "
            f"pass={stats['pass']} fail={stats['fail']} hit={stats['hit']} "
            f"misheard={stats['misheard']} empty={stats['empty']} asleep={stats['asleep']} "
            f"multi_wake={stats['multi_wake']} multi_asr={stats['multi_asr']} "
            f"multi_result={stats['multi_result']}",
            flush=True,
        )
        counts = dict(self.artifacts.counts)
        failed = counts.get("FAIL", 0)
        passed = counts.get("PASS", 0)
        if failed:
            status, reason = "FAIL", f"完成 {passed + failed} 轮，失败 {failed} 轮"
        elif passed:
            status, reason = "PASS", f"完成 {passed} 轮，全部通过"
        else:
            status, reason = "BLOCKED", "没有完成任何轮次"
        if self.acceptance_mode == "asr_optional" and status == "PASS" and not self._asr_result_seen:
            status, reason = "FAIL", "ASR 全程没有识别结果"
        summary = self.artifacts.finalize(status, reason)
        print(f"[{now_human()}] TASK {summary['status']}: {summary['result_directory']}", flush=True)
        self.validate_tool_log()
        return 0 if summary["status"] in ("PASS", "WARN") else 1

    def validate_tool_log(self) -> None:
        if self.artifacts is None:
            return
        try:
            text = self.artifacts.tool_log_path.read_text(encoding="utf-8")
            errors = ToolLogValidator().validate_text(text)
        except Exception as exc:
            print(f"tool.log validation failed: {exc}", flush=True)
            return
        if errors:
            print("tool.log validation errors:", flush=True)
            for error in errors[:20]:
                print(f"  - {error}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CSK 烧录/上下电压测")
    parser.add_argument("--mode", choices=["burn", "powercycle"], default="burn",
                        help="burn=烧录+唤醒ASR；powercycle=上下电+初始化+唤醒ASR")
    parser.add_argument("--rounds", type=int, default=1, help="轮数")
    parser.add_argument("--fw", default="auto", help="auto（默认优先 hex，无 hex 用 img）、img、hex 或固件文件路径")
    parser.add_argument("--preflight", action="store_true", help="只做预检，不执行设备动作")
    parser.add_argument("--skip-burn", action="store_true", help="跳过烧录，只验证控制时序和启动日志")
    parser.add_argument("--wake-test", action="store_true", help="初始化后切换英文、播报唤醒词并校验唤醒")
    parser.add_argument("--interaction-test", action="store_true", help="唤醒后播报命令词并校验 ASR 与响应")
    parser.add_argument("--acceptance", choices=sorted(MODES), default="",
                        help="单轮交互验收模式：strict/any_asr/asr_optional/wake_only")
    parser.add_argument("--no-hisi", action="store_true", help="不采集 HISI 日志")
    parser.add_argument("--result-root", default=str(SKILL_ROOT / "result"))
    parser.add_argument("--run-name", default="",
                        help="固定结果目录名；同一设备同一任务多次运行时追加日志")
    parser.add_argument("--batch-stamp", default="",
                        help="同一批设备的统一时间戳，例如 20261022_181500")
    parser.add_argument("--task-summary", default="",
                        help="任务简介，用于结果目录名，例如 burn-wake-asr")
    parser.add_argument("--profile", default=str(SKILL_ROOT / "config" / "burn-profile.json"))
    parser.add_argument("--manifest", default=str(SKILL_ROOT / "config" / "firmware-manifest.json"))
    parser.add_argument("--devices", default="", help="多设备配置文件；单设备运行时留空")
    parser.add_argument("--device-id", default="", help="多设备配置中的设备 id")
    parser.add_argument("--command-gap", type=float, default=None)
    parser.add_argument("--boot-observe", type=float, default=None)
    parser.add_argument("--burn-timeout", type=float, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.rounds <= 0:
        raise SystemExit("--rounds must be > 0")
    if args.interaction_test:
        args.wake_test = True
    return BurnScenario(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
