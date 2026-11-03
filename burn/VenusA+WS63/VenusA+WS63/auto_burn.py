# -*- coding: utf-8 -*-
import argparse
import re
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import serial
    from serial import SerialException
except ImportError:
    serial = None
    SerialException = Exception


SCRIPT_ROOT = Path(__file__).resolve().parent
CONTROL_RESTART_COMMANDS = ["uut-switch1.off", "uut-switch1.on"]
VENUSA_PREPARE_COMMANDS = ["uut-switch1.off", "uut-csk-boot.on", "uut-switch1.on", "uut-csk-boot.off"]
WS63_ONLYBURN_ARGUMENTS = [
    "-onlyburn:root_loaderboot_sign.bin",
    "-onlyburn:root_params_sign.bin",
    "-onlyburn:ssb_sign.bin",
    "-onlyburn:flashboot_sign.bin",
    "-onlyburn:flashboot_backup_sign.bin",
    "-onlyburn:ws63-liteos-app-sign.bin",
    "-onlyburn:efuse_cfg.bin",
    "-onlyburn:ws63-liteos-mfg-sign.bin",
]


class BurnError(RuntimeError):
    pass


def write_step(message: str) -> None:
    print()
    print(f"==> {message}")


def log_serial_command(label: str, port_name: str, baud_rate: int, command: str, dry_run: bool = False) -> None:
    prefix = "[DryRun]" if dry_run else ""
    print(f"{prefix}[{label}][{port_name}@{baud_rate}] <= {command}")


def log_serial_response(label: str, port_name: str, baud_rate: int, response: str, prefix: str = "=>") -> None:
    for line in response.splitlines():
        stripped = line.strip()
        if stripped:
            print(f"[{label}][{port_name}@{baud_rate}] {prefix} {stripped}")


def normalize_port_name(port_name: str) -> str:
    if not port_name or not port_name.strip():
        raise BurnError("A COM port value is required.")

    trimmed = port_name.strip()
    if trimmed.isdigit():
        return f"COM{trimmed}"

    if re.fullmatch(r"(?i)COM\d+", trimmed):
        return trimmed.upper()

    raise BurnError(f"Unsupported COM port format: {port_name}. Use COM6 or 6.")


def unique_paths(paths):
    seen = set()
    result = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def resolve_firmware_root_path(requested_path: Optional[str]) -> Path:
    if requested_path:
        firmware_root = Path(requested_path).expanduser()
        if not firmware_root.is_dir():
            raise BurnError(f"Firmware root not found: {requested_path}")
        return firmware_root.resolve()

    firmware_dir_name = "\u56fa\u4ef6"
    firmware_download_dir_name = "1\u3001\u56fa\u4ef6\u5305\u4e0b\u8f7d"
    air_conditioner_dir_name = "\u7f8e\u7684\u7a7a\u8c03"
    candidate_roots = unique_paths(
        [
            SCRIPT_ROOT.parent / firmware_dir_name / firmware_download_dir_name / firmware_dir_name,
            Path(rf"D:\work\{air_conditioner_dir_name}\{firmware_dir_name}\{firmware_download_dir_name}\{firmware_dir_name}"),
        ]
    )

    for candidate_root in candidate_roots:
        if not candidate_root.is_dir():
            continue

        packages = [
            child
            for child in candidate_root.iterdir()
            if child.is_dir() and child.name.startswith("Midea_VenusA_WS63_")
        ]
        if not packages:
            continue

        packages.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return packages[0].resolve()

    raise BurnError("No firmware package was found automatically. Please pass --firmware-root explicitly.")


def get_first_existing_file(candidates: List[Path], description: str) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    checked = "; ".join(str(candidate) for candidate in candidates)
    raise BurnError(f"Unable to find {description}. Checked: {checked}")


def resolve_venusa_tool_path(firmware_root: Path) -> Path:
    return get_first_existing_file(
        [
            firmware_root / "VenusA" / "Uart_Burn_Tool.exe",
            SCRIPT_ROOT / "VenusA_Burn" / "Uart_Burn_Tool.exe",
        ],
        "VenusA burn tool",
    )


def resolve_venusa_firmware_path(firmware_root: Path, firmware_type: str) -> Path:
    firmware_type = firmware_type.lower()
    candidate_groups = {
        "hex": [
            firmware_root / "fw.hex",
            firmware_root / "VenusA" / "fw.hex",
        ],
        "img": [
            firmware_root / "fw.img",
            firmware_root / "VenusA" / "fw.img",
        ],
    }

    if firmware_type == "auto":
        candidates = candidate_groups["hex"] + candidate_groups["img"]
    elif firmware_type in candidate_groups:
        candidates = candidate_groups[firmware_type]
    else:
        raise BurnError(f"Unsupported VenusA firmware type: {firmware_type}")

    return get_first_existing_file(candidates, f"VenusA firmware ({firmware_type})")


def resolve_ws63_tool_path() -> Path:
    tool_dir = SCRIPT_ROOT / "BurnTool_Gold"
    tool_path = tool_dir / "BurnTool.exe"
    archive_path = SCRIPT_ROOT / "BurnTool_Gold.zip"
    if not tool_path.is_file() and archive_path.is_file():
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(tool_dir)
    return get_first_existing_file([tool_path], "WS63 burn tool")


def resolve_ws63_package_path(firmware_root: Path) -> Path:
    preferred_files = [
        firmware_root / "ws63-liteos-app_all.fwpkg",
        firmware_root / "ws63_liteos_app_all_in_one.fwpkg",
        firmware_root / "ws53_liteos_app_all_in_one.fwpkg",
        firmware_root / "WS63" / "ws63-liteos-app_all.fwpkg",
        firmware_root / "WS63" / "ws63_liteos_app_all_in_one.fwpkg",
        firmware_root / "WS63" / "ws53_liteos_app_all_in_one.fwpkg",
    ]
    for preferred_file in preferred_files:
        if preferred_file.is_file():
            return preferred_file.resolve()

    pattern = re.compile(r"(?i)^(ws63|ws53).*(all|all_in_one).*\.fwpkg$")
    matches = [path for path in firmware_root.rglob("*.fwpkg") if pattern.fullmatch(path.name)]
    matches.sort(key=lambda item: (len(item.parts), item.name.lower()))
    if matches:
        return matches[0].resolve()

    raise BurnError(f"Unable to find WS63 firmware package under: {firmware_root}")


def format_command_line(file_path: Path, arguments: List[str]) -> str:
    return subprocess.list2cmdline([str(file_path), *arguments])


def sleep_ms(delay_ms: int) -> None:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)


def read_text_best_effort(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030", "utf-16", "utf-16le"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def build_serial_error_message(label: str, port_name: str, baud_rate: int, exc: Exception) -> str:
    message = f"Failed to use {label} port {port_name} @ {baud_rate}: {exc}"
    lowered = str(exc).lower()
    if "access is denied" in lowered or "permissionerror" in lowered or "could not open port" in lowered:
        message += (
            f" The port is likely occupied by another program. Please close any serial assistant, "
            f"terminal, log tool, or other burn script using {port_name}, then retry."
        )
    return message


def open_serial_port(
    port_name: str,
    baud_rate: int,
    label: str,
    timeout: float = 0.5,
    write_timeout: float = 0.5,
    reset_buffers: bool = True,
):
    if serial is None:
        raise BurnError("pyserial is not installed. Please run: pip install pyserial")

    try:
        serial_port = serial.Serial(
            port=port_name,
            baudrate=baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=write_timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
    except SerialException as exc:
        raise BurnError(build_serial_error_message(label, port_name, baud_rate, exc))

    try:
        serial_port.dtr = False
        serial_port.rts = False
    except Exception:
        pass

    if reset_buffers:
        try:
            serial_port.reset_input_buffer()
        except Exception:
            pass
        try:
            serial_port.reset_output_buffer()
        except Exception:
            pass

    return serial_port


def decode_serial_bytes(data: bytes) -> str:
    if not data:
        return ""

    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def collect_serial_output(serial_port, wait_ms: int, poll_interval_ms: int = 100) -> str:
    deadline = time.time() + (wait_ms / 1000.0)
    chunks: List[bytes] = []

    while time.time() < deadline:
        waiting = getattr(serial_port, "in_waiting", 0)
        if waiting:
            chunks.append(serial_port.read(waiting))
            continue

        remaining_ms = max(0, int((deadline - time.time()) * 1000))
        if remaining_ms <= 0:
            break
        sleep_ms(min(poll_interval_ms, remaining_ms))

    waiting = getattr(serial_port, "in_waiting", 0)
    if waiting:
        chunks.append(serial_port.read(waiting))

    return decode_serial_bytes(b"".join(chunks)).strip()


def write_serial_line(serial_port, command: str) -> None:
    serial_port.write((command + "\r\n").encode("ascii"))
    serial_port.flush()


def run_control_sequence(
    args,
    control_port: str,
    description: str,
    commands: List[str],
    read_delay_ms: int = 200,
    inter_command_delay_ms: Optional[int] = None,
) -> str:
    write_step(description)
    return send_serial_commands(
        control_port,
        args.control_baud,
        commands,
        read_delay_ms=read_delay_ms,
        inter_command_delay_ms=args.power_cycle_delay_ms if inter_command_delay_ms is None else inter_command_delay_ms,
        label="CTRL",
        dry_run=args.dry_run,
    )


def send_serial_commands(
    port_name: str,
    baud_rate: int,
    commands: List[str],
    read_delay_ms: int = 250,
    inter_command_delay_ms: int = 0,
    label: str = "Serial",
    capture_response: bool = False,
    dry_run: bool = False,
) -> str:
    for command in commands:
        log_serial_command(label, port_name, baud_rate, command, dry_run=dry_run)

    if dry_run:
        return "OK (dry run)" if capture_response else ""

    responses: List[str] = []
    try:
        with open_serial_port(port_name, baud_rate, label, timeout=0.5, write_timeout=0.5, reset_buffers=True) as serial_port:
            for index, command in enumerate(commands):
                write_serial_line(serial_port, command)

                if read_delay_ms > 0:
                    sleep_ms(read_delay_ms)

                response = collect_serial_output(serial_port, 200)
                if response:
                    log_serial_response(label, port_name, baud_rate, response)
                    responses.append(response)

                if inter_command_delay_ms > 0 and index < len(commands) - 1:
                    sleep_ms(inter_command_delay_ms)
    except SerialException as exc:
        raise BurnError(build_serial_error_message(label, port_name, baud_rate, exc))

    return "\n".join(responses) if capture_response else ""


def run_ws63_at_session(args, ws63_port: str) -> Tuple[bool, str]:
    if args.dry_run:
        for attempt in range(1, args.ws63_at_retry_count + 1):
            write_step(f"Send WS63 AT+FTM=0 (attempt {attempt}/{args.ws63_at_retry_count})")
            log_serial_command("WS63", ws63_port, args.ws63_at_baud, "AT+FTM=0", dry_run=True)
        return True, "OK (dry run)"

    diagnostics: List[str] = []
    try:
        with open_serial_port(ws63_port, args.ws63_at_baud, "WS63", timeout=0.2, write_timeout=0.5, reset_buffers=False) as serial_port:
            if args.ws63_at_open_wait_ms > 0:
                sleep_ms(args.ws63_at_open_wait_ms)

            startup = collect_serial_output(serial_port, 500)
            if startup:
                diagnostics.append("[BOOT]")
                diagnostics.append(startup)
                log_serial_response("WS63", ws63_port, args.ws63_at_baud, startup, prefix="boot =>")

            for attempt in range(1, args.ws63_at_retry_count + 1):
                write_step(f"Send WS63 AT+FTM=0 (attempt {attempt}/{args.ws63_at_retry_count})")
                log_serial_command("WS63", ws63_port, args.ws63_at_baud, "AT+FTM=0")
                write_serial_line(serial_port, "AT+FTM=0")

                response = collect_serial_output(serial_port, args.ws63_at_response_wait_ms, poll_interval_ms=200)
                if response:
                    diagnostics.append(f"[ATTEMPT]{attempt}")
                    diagnostics.append(response)
                    log_serial_response("WS63", ws63_port, args.ws63_at_baud, response)

                if re.search(r"(?i)\bOK\b|AT\+FTM=0|FTM=0", response or ""):
                    diagnostic = "\n".join(diagnostics).strip() or "OK"
                    return True, diagnostic

                if attempt < args.ws63_at_retry_count and args.ws63_at_retry_delay_ms > 0:
                    sleep_ms(args.ws63_at_retry_delay_ms)
    except SerialException as exc:
        raise BurnError(build_serial_error_message("WS63", ws63_port, args.ws63_at_baud, exc))

    diagnostic = "\n".join(diagnostics).strip() or "No serial output was captured."
    return False, diagnostic


def invoke_external_process(
    file_path: Path,
    arguments: List[str],
    working_directory: Path,
    description: str,
    wait: bool,
    dry_run: bool,
):
    write_step(description)
    print(format_command_line(file_path, arguments))

    if dry_run:
        return None

    if wait:
        result = subprocess.run([str(file_path), *arguments], cwd=working_directory)
        if result.returncode != 0:
            raise BurnError(f"{description} failed with exit code {result.returncode}")
        return result

    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen([str(file_path), *arguments], cwd=working_directory, creationflags=creation_flags)


def find_ws63_result_in_log(log_path: Path) -> Optional[str]:
    try:
        text = read_text_best_effort(log_path)
    except OSError:
        return None

    result_line = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("烧写结果："):
            result_line = stripped
    return result_line


def wait_for_ws63_burn_result(log_dir: Path, known_logs: Dict[str, float], timeout_ms: int) -> Tuple[Path, str]:
    deadline = time.time() + (timeout_ms / 1000.0)
    last_changed_log: Optional[Path] = None

    while time.time() < deadline:
        current_logs = list(log_dir.glob("optLog_*.txt"))
        current_logs.sort(key=lambda item: item.stat().st_mtime, reverse=True)

        for log_path in current_logs:
            stat = log_path.stat()
            previous_mtime = known_logs.get(str(log_path))
            if previous_mtime is None or stat.st_mtime > previous_mtime:
                known_logs[str(log_path)] = stat.st_mtime
                last_changed_log = log_path
                result_line = find_ws63_result_in_log(log_path)
                if result_line:
                    return log_path, result_line

        time.sleep(1)

    if last_changed_log is not None:
        raise BurnError(f"Timed out waiting for a final WS63 burn result in {last_changed_log}.")

    raise BurnError(f"Timed out waiting for WS63 burn result log in {log_dir}.")


def stop_ws63_burn_process(process, timeout_ms: int, dry_run: bool) -> None:
    write_step("Stop WS63 burn tool with Ctrl+C")

    if dry_run:
        print("[DryRun] Send Ctrl+C to WS63 burn tool")
        return

    if process is None or process.poll() is not None:
        print("WS63 burn tool already exited.")
        return

    try:
        process.send_signal(signal.CTRL_C_EVENT)
    except Exception as exc:
        raise BurnError(f"Failed to send Ctrl+C to WS63 burn tool: {exc}")

    try:
        process.wait(timeout=timeout_ms / 1000.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise BurnError("WS63 burn tool did not exit after Ctrl+C and was force-terminated.")


def invoke_venusa_burn(args, firmware_root: Path, control_port: str, venusa_port: str) -> None:
    venusa_tool_path = resolve_venusa_tool_path(firmware_root)
    venusa_firmware_path = resolve_venusa_firmware_path(firmware_root, args.venusa_firmware_type)
    venusa_working_directory = venusa_tool_path.parent

    run_control_sequence(
        args,
        control_port,
        "Prepare VenusA power sequence",
        VENUSA_PREPARE_COMMANDS,
        read_delay_ms=200,
        inter_command_delay_ms=args.csk_prep_delay_ms,
    )
    sleep_ms(args.power_cycle_delay_ms)

    arguments = [
        "-s",
        "-b",
        str(args.venusa_baud),
        "-p",
        venusa_port,
        "-f",
        str(venusa_firmware_path),
        "-l",
        "-m",
        "-d",
    ]
    if venusa_firmware_path.suffix.lower() == ".img":
        arguments.extend(["-a", "0"])

    invoke_external_process(
        venusa_tool_path,
        arguments,
        venusa_working_directory,
        f"Burn VenusA firmware ({venusa_firmware_path.name})",
        wait=True,
        dry_run=args.dry_run,
    )


def invoke_ws63_at_setup(args, ws63_port: str) -> None:
    ok, diagnostic = run_ws63_at_session(args, ws63_port)
    if ok:
        return

    raise BurnError(
        f"WS63 AT initialization did not return a valid response on {ws63_port}. "
        f"Captured output: {diagnostic}"
    )


def invoke_ws63_burn(args, firmware_root: Path, control_port: str, ws63_port: str) -> None:
    ws63_tool_path = resolve_ws63_tool_path()
    ws63_package_path = resolve_ws63_package_path(firmware_root)
    ws63_working_directory = ws63_tool_path.parent
    ws63_log_dir = ws63_working_directory / "optLog"
    known_logs = {}
    if ws63_log_dir.is_dir():
        known_logs = {str(path): path.stat().st_mtime for path in ws63_log_dir.glob("optLog_*.txt")}

    arguments = [
        f"-com:{ws63_port.replace('COM', '')}",
        f"-bin:{ws63_package_path}",
        f"-signalbaud:{args.ws63_signal_baud}",
    ]
    arguments.extend(WS63_ONLYBURN_ARGUMENTS)

    process = invoke_external_process(
        ws63_tool_path,
        arguments,
        ws63_working_directory,
        "Start WS63 burn tool",
        wait=False,
        dry_run=args.dry_run,
    )

    sleep_ms(args.ws63_burn_kick_delay_ms)

    run_control_sequence(args, control_port, "Kick device into WS63 burn mode", CONTROL_RESTART_COMMANDS)

    write_step("Wait for WS63 burn result")
    if not args.dry_run:
        log_path, result_line = wait_for_ws63_burn_result(ws63_log_dir, known_logs, args.ws63_burn_timeout_ms)
        print(f"WS63 log      : {log_path}")
        print(f"WS63 result   : {result_line}")

        stop_ws63_burn_process(process, args.ws63_exit_timeout_ms, args.dry_run)

        if "成功" not in result_line:
            raise BurnError(f"WS63 burn failed: {result_line} ({log_path})")
    else:
        print("[DryRun] Monitor WS63 optLog for '烧写结果' and send Ctrl+C after completion")

    run_control_sequence(args, control_port, "Restart device after WS63 burn tool exit", CONTROL_RESTART_COMMANDS)

    write_step(f"Wait {args.ws63_post_exit_wait_ms} ms before WS63 AT command")
    sleep_ms(args.ws63_post_exit_wait_ms)
    invoke_ws63_at_setup(args, ws63_port)

    run_control_sequence(args, control_port, "Restart device after WS63 exits factory test mode", CONTROL_RESTART_COMMANDS)


def restart_device_via_control_port(args, control_port: str, description: str) -> None:
    run_control_sequence(args, control_port, description, CONTROL_RESTART_COMMANDS)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto burn VenusA and WS63 firmware.")
    parser.add_argument("--firmware-root", dest="firmware_root")
    parser.add_argument("--control-port", required=True)
    parser.add_argument("--venusa-port")
    parser.add_argument("--ws63-port")
    parser.add_argument("--control-baud", type=int, default=115200)
    parser.add_argument("--venusa-baud", type=int, default=3000000)
    parser.add_argument("--venusa-firmware-type", choices=["auto", "hex", "img"], default="auto")
    parser.add_argument("--ws63-signal-baud", type=int, default=1000000)
    parser.add_argument("--ws63-at-baud", type=int, default=921600)
    parser.add_argument("--csk-prep-delay-ms", type=int, default=2000)
    parser.add_argument("--power-cycle-delay-ms", type=int, default=2000)
    parser.add_argument("--ws63-burn-kick-delay-ms", type=int, default=800)
    parser.add_argument("--ws63-post-exit-wait-ms", "--ws63-boot-ready-delay-ms", dest="ws63_post_exit_wait_ms", type=int, default=5000)
    parser.add_argument("--ws63-at-open-wait-ms", type=int, default=2000)
    parser.add_argument("--ws63-at-response-wait-ms", type=int, default=3000)
    parser.add_argument("--ws63-at-retry-count", type=int, default=5)
    parser.add_argument("--ws63-at-retry-delay-ms", type=int, default=1500)
    parser.add_argument("--ws63-burn-timeout-ms", type=int, default=600000)
    parser.add_argument("--ws63-exit-timeout-ms", type=int, default=15000)
    parser.add_argument("--skip-venusa", action="store_true")
    parser.add_argument("--skip-ws63", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.skip_venusa and args.skip_ws63:
        raise BurnError("skip-venusa and skip-ws63 cannot both be specified.")

    if not args.skip_venusa and not args.venusa_port:
        raise BurnError("venusa-port is required when VenusA burn is enabled.")

    if not args.skip_ws63 and not args.ws63_port:
        raise BurnError("ws63-port is required when WS63 burn is enabled.")

    firmware_root = resolve_firmware_root_path(args.firmware_root)
    control_port = normalize_port_name(args.control_port)
    venusa_port = normalize_port_name(args.venusa_port) if not args.skip_venusa else ""
    ws63_port = normalize_port_name(args.ws63_port) if not args.skip_ws63 else ""

    write_step("Resolved configuration")
    print(f"FirmwareRoot : {firmware_root}")
    print(f"ControlPort  : {control_port} @ {args.control_baud}")
    if not args.skip_venusa:
        print(f"VenusAPort   : {venusa_port} @ {args.venusa_baud}")
    if not args.skip_ws63:
        print(f"WS63Port     : {ws63_port} @ {args.ws63_signal_baud}")
    print(f"DryRun       : {args.dry_run}")

    if not args.skip_venusa:
        invoke_venusa_burn(args, firmware_root, control_port, venusa_port)
        if args.skip_ws63:
            restart_device_via_control_port(args, control_port, "Restart device after CSK-only burn")

    if not args.skip_ws63:
        invoke_ws63_burn(args, firmware_root, control_port, ws63_port)

    write_step("All requested burn steps completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BurnError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
