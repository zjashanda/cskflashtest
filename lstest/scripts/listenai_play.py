#!/usr/bin/env python3
import argparse
import json
import os
import platform as host_platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Dict, List, Optional

TARGET_RATE = 44100
TARGET_CHANNELS = 2
PROBE_DURATION_SECONDS = 0.10
SUPPORTED_SCAN_DIRECTIONS = ("All", "Render", "Capture")

WINDOWS_SCAN_SCRIPT = r"""
function Get-ListenAIWaveFormatChannels {
    param([byte[]]$Blob)

    if (-not $Blob -or $Blob.Length -lt 12) {
        return $null
    }

    $offset = 0
    if ($Blob.Length -ge 8 -and [BitConverter]::ToUInt32($Blob, 0) -eq 65) {
        $offset = 8
    }

    if ($Blob.Length -lt ($offset + 4)) {
        return $null
    }

    return [int][BitConverter]::ToUInt16($Blob, $offset + 2)
}

function Get-ListenAIDeviceKey {
    param([string]$Interface)

    if ([string]::IsNullOrWhiteSpace($Interface)) {
        return $null
    }

    if ($Interface -notmatch 'USB\\(?<Head>[^\\]+)\\(?<Tail>.+)$') {
        return $null
    }

    $vidPid = ($matches['Head'] -replace '&MI_[0-9A-F]{2}$', '').ToUpperInvariant()
    if ($vidPid -notmatch '^VID_[0-9A-F]{4}&PID_[0-9A-F]{4}$') {
        return $null
    }

    $token = ($matches['Tail'] -replace '[^A-Za-z0-9]+', '_').Trim('_').ToUpperInvariant()
    if ($token -match '^([A-Z0-9]{4,})_0_([A-Z0-9]{2,})$') {
        $token = "$($matches[1])_$($matches[2])"
    }
    if (-not $token) {
        return $null
    }

    return "${vidPid}:$token"
}

$endpointMap = @{}
Get-PnpDevice -Class AudioEndpoint -PresentOnly | ForEach-Object {
    if ($_.InstanceId -match 'SWD\\MMDEVAPI\\\{[^}]+\}\.\{([0-9A-Fa-f-]+)\}$') {
        $endpointMap["{$($matches[1].ToLower())}"] = $_.FriendlyName
    }
}

$roots = @(
    @{ Direction = 'Render'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render' },
    @{ Direction = 'Capture'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture' }
)

$items = foreach ($root in $roots) {
    Get-ChildItem -LiteralPath $root.Path | ForEach-Object {
        $propsPath = Join-Path $_.PSPath 'Properties'
        if (-not (Test-Path -LiteralPath $propsPath)) { return }

        $state = (Get-ItemProperty -LiteralPath $_.PSPath -Name DeviceState -ErrorAction SilentlyContinue).DeviceState
        if (($state -band 0xF) -ne 1) { return }

        $props = Get-ItemProperty -LiteralPath $propsPath -ErrorAction SilentlyContinue
        if ($props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6' -ne 'ListenAI Audio') { return }

        [pscustomobject]@{
            Direction    = $root.Direction
            FriendlyName = $endpointMap[$_.PSChildName.ToLower()]
            Name         = $props.'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
            EndpointId   = $_.PSChildName
            Interface    = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},2'
            DeviceKey    = Get-ListenAIDeviceKey $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},2'
            Channels     = Get-ListenAIWaveFormatChannels $props.'{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0'
        }
    }
}

$items | ConvertTo-Json -Depth 6
"""


@dataclass
class DeviceRecord:
    platform: str
    direction: str
    device_key: str
    name: str
    channels: Optional[int]
    backend_target: str = ""
    endpoint_id: str = ""
    interface: str = ""
    card_index: Optional[int] = None
    pcm_device: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "platform": self.platform,
            "direction": self.direction,
            "device_key": self.device_key,
            "channels": self.channels,
            "name": self.name,
            "backend_target": self.backend_target,
            "endpoint_id": self.endpoint_id,
            "interface": self.interface,
            "card_index": self.card_index,
            "pcm_device": self.pcm_device,
        }


class PlaybackThread(threading.Thread):
    def __init__(
        self,
        label: str,
        script_path: Path,
        platform_name: str,
        device_key: Optional[str],
        audio_file: Path,
        repeat_count: int,
        start_barrier: threading.Barrier,
        done_barrier: threading.Barrier,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=label)
        self.label = label
        self.script_path = script_path
        self.platform_name = platform_name
        self.device_key = device_key
        self.audio_file = audio_file
        self.repeat_count = repeat_count
        self.start_barrier = start_barrier
        self.done_barrier = done_barrier
        self.stop_event = stop_event
        self.error: Optional[Exception] = None

    def abort_barriers(self) -> None:
        for barrier in (self.start_barrier, self.done_barrier):
            try:
                barrier.abort()
            except threading.BrokenBarrierError:
                pass

    def run(self) -> None:
        try:
            for iteration in range(1, self.repeat_count + 1):
                if self.stop_event.is_set():
                    return
                self.start_barrier.wait()
                print(f"[{self.label}] iteration {iteration} start", flush=True)
                invoke_worker_once(
                    script_path=self.script_path,
                    platform_name=self.platform_name,
                    device_key=self.device_key,
                    audio_file=self.audio_file,
                    probe_only=False,
                )
                print(f"[{self.label}] iteration {iteration} done", flush=True)
                self.done_barrier.wait()
        except Exception as exc:  # pylint: disable=broad-except
            self.error = exc
            self.stop_event.set()
            self.abort_barriers()
            print(f"[{self.label}] failed: {exc}", file=sys.stderr, flush=True)


def detect_runtime_platform() -> str:
    system_name = host_platform.system().lower()
    if system_name.startswith("win"):
        return "windows"
    if system_name == "linux":
        return "linux"
    raise RuntimeError(f"Unsupported runtime platform: {host_platform.system()}")


def resolve_platform(requested: str) -> str:
    runtime = detect_runtime_platform()
    if requested == "auto":
        return runtime
    if requested != runtime:
        raise RuntimeError(f"Requested platform {requested} but current runtime is {runtime}")
    return requested


def run_command(command: List[str], *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        check=False,
    )


def require_executable(name: str) -> str:
    resolved = which(name)
    if not resolved:
        raise RuntimeError(f"{name} not found in PATH")
    return resolved


def parse_int(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def powershell_json(script: str) -> object:
    wrapped = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::UTF8\n" + script
    result = run_command(["powershell", "-NoProfile", "-Command", wrapped])
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "PowerShell failed").strip()
        raise RuntimeError(details)
    payload = (result.stdout or "").strip()
    if not payload:
        return []
    return json.loads(payload)


def derive_device_key_from_interface(interface: str) -> str:
    match = re.search(r"USB\\(?P<head>[^\\]+)\\(?P<tail>.+)$", interface or "", flags=re.IGNORECASE)
    if not match:
        return ""

    vid_pid = re.sub(r"&MI_[0-9A-F]{2}$", "", match.group("head"), flags=re.IGNORECASE).upper()
    if not re.fullmatch(r"VID_[0-9A-F]{4}&PID_[0-9A-F]{4}", vid_pid):
        return ""

    token = compact_token(match.group("tail"))
    if not token:
        return ""

    return f"{vid_pid}:{token}"


def scan_windows() -> List[DeviceRecord]:
    rows = powershell_json(WINDOWS_SCAN_SCRIPT)
    if isinstance(rows, dict):
        rows = [rows]

    items: List[DeviceRecord] = []
    for row in rows:
        interface = row.get("Interface", "")
        device_key = row.get("DeviceKey") or derive_device_key_from_interface(interface)
        if not device_key:
            continue
        name = row.get("FriendlyName") or row.get("Name") or ""
        items.append(
            DeviceRecord(
                platform="windows",
                direction=row.get("Direction", ""),
                device_key=device_key,
                name=name,
                channels=parse_int(row.get("Channels")),
                backend_target=name,
                endpoint_id=row.get("EndpointId", ""),
                interface=interface,
            )
        )

    return sorted(items, key=lambda item: (item.direction, item.device_key, item.name))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def read_text_if_exists(path: Path) -> str:
    try:
        return read_text(path)
    except OSError:
        return ""


def sanitize_token(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", raw or "").strip("_").upper()


def compact_token(raw: str) -> str:
    token = sanitize_token(raw)
    if not token:
        return ""

    usb_marker = "_USB_"
    marker_index = token.find(usb_marker)
    if marker_index >= 0:
        compacted = token[marker_index + 1 :]
        if compacted:
            token = compacted

    serial_port_match = re.fullmatch(r"([A-Z0-9]{4,})_0_([A-Z0-9]{2,})", token)
    if serial_port_match:
        return f"{serial_port_match.group(1)}_{serial_port_match.group(2)}"

    return token


def parse_udev_properties(dev_path: Path) -> Dict[str, str]:
    udevadm = which("udevadm")
    if not udevadm:
        return {}
    result = run_command([udevadm, "info", "-q", "property", "-n", str(dev_path)])
    if result.returncode != 0:
        return {}
    props: Dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def derive_token_from_sysfs_path(path: Path) -> str:
    parts: List[str] = []
    for part in path.parts:
        if re.match(r"^\d+-[\d.]+(?::\d+\.\d+)?$", part):
            parts.append(sanitize_token(part))
    if parts:
        return "_".join(parts)
    return sanitize_token(path.name)


def linux_identity(card_index: int, control_path: Path) -> Optional[Dict[str, str]]:
    props = parse_udev_properties(control_path)
    vid = props.get("ID_VENDOR_ID", "").upper()
    pid = props.get("ID_MODEL_ID", "").upper()
    # Some ListenAI Linux devices expose the same USB serial, so prefer the USB path tag.
    token = compact_token(props.get("ID_PATH_TAG") or props.get("ID_SERIAL_SHORT") or "")
    if vid and pid:
        return {
            "vid": vid,
            "pid": pid,
            "token": token or f"CARD{card_index}",
        }

    sysfs_path = Path(f"/sys/class/sound/card{card_index}/device")
    if not sysfs_path.exists():
        return None

    resolved = sysfs_path.resolve()
    vid = ""
    pid = ""
    serial = ""
    for candidate in [resolved, *resolved.parents]:
        if not vid:
            maybe_vid = read_text_if_exists(candidate / "idVendor")
            maybe_pid = read_text_if_exists(candidate / "idProduct")
            if maybe_vid and maybe_pid:
                vid = maybe_vid.upper()
                pid = maybe_pid.upper()
        if not serial:
            serial = compact_token(read_text_if_exists(candidate / "serial"))
        if vid and pid and serial:
            break

    if not vid or not pid:
        return None

    return {
        "vid": vid,
        "pid": pid,
        "token": compact_token(derive_token_from_sysfs_path(resolved)) or serial or f"CARD{card_index}",
    }


def linux_stream_channels(card_index: int, section: str) -> Optional[int]:
    card_path = Path(f"/proc/asound/card{card_index}")
    max_channels: Optional[int] = None
    for stream_file in sorted(card_path.glob("stream*")):
        in_section = False
        for raw_line in read_text_if_exists(stream_file).splitlines():
            line = raw_line.strip()
            if re.match(r"^[A-Za-z][A-Za-z ]*:$", line):
                if line == f"{section}:":
                    in_section = True
                    continue
                if in_section:
                    in_section = False
            if in_section and line.startswith("Channels:"):
                value = parse_int(line.split(":", 1)[1].strip())
                if value is not None and (max_channels is None or value > max_channels):
                    max_channels = value
    return max_channels


def linux_pcm_devices(card_index: int, suffix: str) -> List[int]:
    card_path = Path(f"/proc/asound/card{card_index}")
    device_numbers: List[int] = []
    if not card_path.exists():
        return device_numbers

    for item in sorted(card_path.iterdir()):
        match = re.match(rf"pcm(\d+){suffix}$", item.name)
        if not match:
            continue
        if (item / "info").exists() or item.is_dir():
            device_numbers.append(int(match.group(1)))
    return device_numbers


def scan_linux() -> List[DeviceRecord]:
    snd_dir = Path("/dev/snd")
    if not snd_dir.exists():
        return []

    items: List[DeviceRecord] = []
    for control_path in sorted(snd_dir.glob("controlC*")):
        match = re.match(r"controlC(\d+)$", control_path.name)
        if not match:
            continue
        card_index = int(match.group(1))
        identity = linux_identity(card_index, control_path)
        if not identity:
            continue

        device_key = f"VID_{identity['vid']}&PID_{identity['pid']}:{identity['token']}"
        card_name = read_text_if_exists(Path(f"/proc/asound/card{card_index}/id")) or f"card{card_index}"
        playback_channels = linux_stream_channels(card_index, "Playback")
        capture_channels = linux_stream_channels(card_index, "Capture")
        playback_devices = linux_pcm_devices(card_index, "p")
        capture_devices = linux_pcm_devices(card_index, "c")

        if playback_devices or playback_channels is not None:
            playback_device = playback_devices[0] if playback_devices else 0
            items.append(
                DeviceRecord(
                    platform="linux",
                    direction="Render",
                    device_key=device_key,
                    name=card_name,
                    channels=playback_channels,
                    backend_target=f"plughw:{card_index},{playback_device}",
                    card_index=card_index,
                    pcm_device=playback_device,
                )
            )
        if capture_devices or capture_channels is not None:
            capture_device = capture_devices[0] if capture_devices else 0
            items.append(
                DeviceRecord(
                    platform="linux",
                    direction="Capture",
                    device_key=device_key,
                    name=card_name,
                    channels=capture_channels,
                    backend_target=f"hw:{card_index},{capture_device}",
                    card_index=card_index,
                    pcm_device=capture_device,
                )
            )

    return sorted(items, key=lambda item: (item.direction, item.device_key, item.name))


def scan_devices(platform_name: str) -> List[DeviceRecord]:
    if platform_name == "windows":
        return scan_windows()
    if platform_name == "linux":
        return scan_linux()
    raise RuntimeError(f"Unsupported platform for scan: {platform_name}")


def filter_direction(items: List[DeviceRecord], direction: str) -> List[DeviceRecord]:
    if direction == "All":
        return items
    return [item for item in items if item.direction == direction]


def resolve_device_key(platform_name: str, device_key: str, direction: str = "Render") -> DeviceRecord:
    normalized_key = (device_key or "").upper()
    matches = [
        item
        for item in scan_devices(platform_name)
        if item.direction == direction and item.device_key.upper() == normalized_key
    ]
    if not matches:
        raise RuntimeError(f"No active {direction} endpoint matches device key: {device_key}")
    if len(matches) > 1:
        names = ", ".join(match.name for match in matches)
        raise RuntimeError(f"Multiple active {direction} endpoints match {device_key}: {names}")
    return matches[0]


def print_table(items: List[DeviceRecord]) -> None:
    if not items:
        print("No active device records found.")
        return

    rows = [
        (
            item.direction,
            item.device_key,
            str(item.channels) if item.channels is not None else "?",
            item.backend_target,
            item.name,
        )
        for item in items
    ]
    widths = [
        max(len(header), max(len(row[index]) for row in rows))
        for index, header in enumerate(("Direction", "DeviceKey", "Channels", "BackendTarget", "Name"))
    ]
    header = ("Direction", "DeviceKey", "Channels", "BackendTarget", "Name")
    print(
        f"{header[0]:<{widths[0]}} {header[1]:<{widths[1]}} {header[2]:<{widths[2]}} {header[3]:<{widths[3]}} {header[4]}"
    )
    for row in rows:
        print(
            f"{row[0]:<{widths[0]}} {row[1]:<{widths[1]}} {row[2]:<{widths[2]}} {row[3]:<{widths[3]}} {row[4]}"
        )


def require_ffmpeg() -> str:
    return require_executable("ffmpeg")


def normalize_audio(source: Path, target: Path) -> None:
    ffmpeg_bin = require_ffmpeg()
    result = run_command(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_RATE),
            str(target),
        ],
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "ffmpeg normalize failed").strip()
        raise RuntimeError(details)


def write_silence_wav(target: Path, duration_seconds: float = PROBE_DURATION_SECONDS) -> None:
    frame_count = max(1, int(TARGET_RATE * duration_seconds))
    silence_frame = b"\x00\x00" * TARGET_CHANNELS
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(TARGET_CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(TARGET_RATE)
        handle.writeframes(silence_frame * frame_count)


def play_once_windows(device_name: str, audio_file: Optional[Path], probe_only: bool) -> None:
    os.environ["SDL_AUDIODRIVER"] = "directsound"
    try:
        import pygame  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise RuntimeError("pygame is required on Windows for device-bound playback") from exc

    init_kwargs = {
        "frequency": TARGET_RATE,
        "size": -16,
        "channels": TARGET_CHANNELS,
        "buffer": 1024,
        "allowedchanges": 0,
    }
    if device_name:
        init_kwargs["devicename"] = device_name

    pygame.mixer.init(**init_kwargs)
    try:
        if probe_only:
            return
        if not audio_file:
            raise RuntimeError("audio file is required for Windows playback")
        sound = pygame.mixer.Sound(str(audio_file))
        channel = sound.play()
        if channel is None:
            raise RuntimeError("pygame failed to start playback")
        while channel.get_busy():
            pygame.time.wait(20)
    finally:
        pygame.mixer.quit()


def play_once_linux(backend_target: str, audio_file: Path) -> None:
    aplay_bin = require_executable("aplay")
    command = [aplay_bin, "-q"]
    if backend_target:
        command.extend(["-D", backend_target])
    command.append(str(audio_file))
    result = run_command(command, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "aplay failed").strip()
        raise RuntimeError(details)


def execute_probe_linux(backend_target: str) -> None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as handle:
        probe_path = Path(handle.name)
    try:
        write_silence_wav(probe_path)
        play_once_linux(backend_target, probe_path)
    finally:
        probe_path.unlink(missing_ok=True)


def describe_target(platform_name: str, device_key: Optional[str]) -> str:
    if device_key:
        record = resolve_device_key(platform_name, device_key, direction="Render")
        return f"{record.device_key} -> {record.backend_target} ({record.name})"
    if platform_name == "windows":
        return "default render device -> directsound default"
    if platform_name == "linux":
        return "default render device -> ALSA default"
    return "default render device"


def execute_worker(
    platform_name: str,
    device_key: Optional[str],
    audio_file: Optional[Path],
    probe_only: bool,
) -> None:
    record = None
    if device_key:
        record = resolve_device_key(platform_name, device_key, direction="Render")
    if platform_name == "windows":
        device_name = record.backend_target if record else ""
        play_once_windows(device_name, audio_file, probe_only)
        return
    if platform_name == "linux":
        backend_target = record.backend_target if record else ""
        if probe_only:
            execute_probe_linux(backend_target)
            return
        if not audio_file:
            raise RuntimeError("audio file is required for Linux playback")
        play_once_linux(backend_target, audio_file)
        return
    raise RuntimeError(f"Unsupported platform for playback: {platform_name}")


def invoke_worker_once(
    script_path: Path,
    platform_name: str,
    device_key: Optional[str],
    audio_file: Optional[Path],
    probe_only: bool,
) -> None:
    command = [
        sys.executable,
        str(script_path.resolve()),
        "internal-play-once",
        "--platform",
        platform_name,
    ]
    if device_key:
        command.extend(["--device-key", device_key])
    if audio_file is not None:
        command.extend(["--audio-file", str(audio_file)])
    if probe_only:
        command.append("--probe-only")

    result = run_command(command, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"worker exited with {result.returncode}").strip()
        raise RuntimeError(details)


def make_temp_wav() -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as handle:
        return Path(handle.name)


def laid_available(platform_name: str) -> bool:
    if platform_name == "windows":
        result = run_command(
            [
                "powershell",
                "-Command",
                "Get-Command laid -ErrorAction Stop | Select-Object -First 1 -ExpandProperty Name",
            ]
        )
        return result.returncode == 0 and bool((result.stdout or "").strip())
    if platform_name == "linux":
        shell = which("bash") or which("zsh")
        if not shell:
            return False
        result = run_command([shell, "-ic", "command -v laid"])
        return result.returncode == 0 and bool((result.stdout or "").strip())
    raise RuntimeError(f"Unsupported platform for laid detection: {platform_name}")


def install_laid(platform_name: str) -> None:
    script_dir = Path(__file__).resolve().parent
    if platform_name == "windows":
        installer = script_dir / "install_laid_windows.ps1"
        command = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
        ]
    elif platform_name == "linux":
        installer = script_dir / "install_laid_linux.sh"
        shell = which("bash") or "bash"
        command = [shell, str(installer)]
    else:
        raise RuntimeError(f"Unsupported platform for laid install: {platform_name}")

    if not installer.exists():
        raise RuntimeError(f"laid installer script not found: {installer}")

    result = run_command(command)
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "laid installer failed").strip()
        raise RuntimeError(details)


def run_ensure_laid(args: argparse.Namespace) -> int:
    platform_name = resolve_platform(args.platform)
    if laid_available(platform_name) and not args.force:
        print("laid is already available.")
        return 0

    print("laid is not available or refresh was requested, installing now...")
    install_laid(platform_name)

    if not laid_available(platform_name):
        raise RuntimeError(
            "laid installer completed but the command is still unavailable. Open a new shell or reload the profile."
        )

    print("laid is available.")
    return 0


def run_scan(args: argparse.Namespace) -> int:
    platform_name = resolve_platform(args.platform)
    items = filter_direction(scan_devices(platform_name), args.direction)
    if args.json:
        print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
    else:
        print_table(items)
    return 0


def run_probe(args: argparse.Namespace) -> int:
    platform_name = resolve_platform(args.platform)
    print(f"Target {describe_target(platform_name, args.device_key)}")
    invoke_worker_once(
        script_path=Path(__file__),
        platform_name=platform_name,
        device_key=args.device_key,
        audio_file=None,
        probe_only=True,
    )
    print("Probe succeeded.")
    return 0


def run_play(args: argparse.Namespace) -> int:
    platform_name = resolve_platform(args.platform)
    audio_file = Path(args.audio_file).resolve()
    if not audio_file.exists():
        raise FileNotFoundError(audio_file)

    normalized = make_temp_wav()
    try:
        normalize_audio(audio_file, normalized)
        print(f"Target {describe_target(platform_name, args.device_key)}")
        if not args.skip_probe:
            invoke_worker_once(
                script_path=Path(__file__),
                platform_name=platform_name,
                device_key=args.device_key,
                audio_file=None,
                probe_only=True,
            )
            print("Probe succeeded.")

        for iteration in range(1, args.repeat + 1):
            print(f"Play iteration {iteration}/{args.repeat}", flush=True)
            invoke_worker_once(
                script_path=Path(__file__),
                platform_name=platform_name,
                device_key=args.device_key,
                audio_file=normalized,
                probe_only=False,
            )
            if iteration < args.repeat and args.gap > 0:
                time.sleep(args.gap)
        print("Playback finished.")
        return 0
    finally:
        normalized.unlink(missing_ok=True)


def run_dual_play(args: argparse.Namespace) -> int:
    platform_name = resolve_platform(args.platform)
    left_file = Path(args.left_file).resolve()
    right_file = Path(args.right_file).resolve()
    if not left_file.exists():
        raise FileNotFoundError(left_file)
    if not right_file.exists():
        raise FileNotFoundError(right_file)

    left_normalized = make_temp_wav()
    right_normalized = make_temp_wav()
    try:
        normalize_audio(left_file, left_normalized)
        normalize_audio(right_file, right_normalized)
        print(f"Left  {describe_target(platform_name, args.left_device_key)}")
        print(f"Right {describe_target(platform_name, args.right_device_key)}")

        if not args.skip_probe:
            invoke_worker_once(
                script_path=Path(__file__),
                platform_name=platform_name,
                device_key=args.left_device_key,
                audio_file=None,
                probe_only=True,
            )
            invoke_worker_once(
                script_path=Path(__file__),
                platform_name=platform_name,
                device_key=args.right_device_key,
                audio_file=None,
                probe_only=True,
            )
            print("Both device probes succeeded.")

        start_barrier = threading.Barrier(3, timeout=180)
        done_barrier = threading.Barrier(3, timeout=180)
        stop_event = threading.Event()

        left_worker = PlaybackThread(
            label="left",
            script_path=Path(__file__),
            platform_name=platform_name,
            device_key=args.left_device_key,
            audio_file=left_normalized,
            repeat_count=args.repeat,
            start_barrier=start_barrier,
            done_barrier=done_barrier,
            stop_event=stop_event,
        )
        right_worker = PlaybackThread(
            label="right",
            script_path=Path(__file__),
            platform_name=platform_name,
            device_key=args.right_device_key,
            audio_file=right_normalized,
            repeat_count=args.repeat,
            start_barrier=start_barrier,
            done_barrier=done_barrier,
            stop_event=stop_event,
        )
        left_worker.start()
        right_worker.start()

        try:
            for iteration in range(1, args.repeat + 1):
                if stop_event.is_set():
                    break
                print(f"[main] iteration {iteration} waiting for both workers", flush=True)
                start_barrier.wait()
                print(f"[main] iteration {iteration} running in parallel", flush=True)
                done_barrier.wait()
                print(f"[main] iteration {iteration} finished on both devices", flush=True)
                if iteration < args.repeat and args.gap > 0:
                    time.sleep(args.gap)
        except threading.BrokenBarrierError:
            stop_event.set()
        finally:
            left_worker.join()
            right_worker.join()

        if left_worker.error:
            raise RuntimeError(f"left worker failed: {left_worker.error}") from left_worker.error
        if right_worker.error:
            raise RuntimeError(f"right worker failed: {right_worker.error}") from right_worker.error

        print("Dual playback finished.")
        return 0
    finally:
        left_normalized.unlink(missing_ok=True)
        right_normalized.unlink(missing_ok=True)


def run_internal_worker(args: argparse.Namespace) -> int:
    platform_name = resolve_platform(args.platform)
    audio_file = Path(args.audio_file).resolve() if args.audio_file else None
    execute_worker(platform_name, args.device_key, audio_file, args.probe_only)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind playback to a stable ListenAI device key on Windows or Linux."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="List active device keys and backend targets")
    scan_parser.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    scan_parser.add_argument("--direction", choices=SUPPORTED_SCAN_DIRECTIONS, default="All")
    scan_parser.add_argument("--json", action="store_true")
    scan_parser.set_defaults(func=run_scan)

    ensure_laid_parser = subparsers.add_parser(
        "ensure-laid", help="Install or refresh the laid command when it is unavailable"
    )
    ensure_laid_parser.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    ensure_laid_parser.add_argument("--force", action="store_true")
    ensure_laid_parser.set_defaults(func=run_ensure_laid)

    probe_parser = subparsers.add_parser(
        "probe", help="Probe the default render device or a specific render device key"
    )
    probe_parser.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    probe_parser.add_argument("--device-key")
    probe_parser.set_defaults(func=run_probe)

    play_parser = subparsers.add_parser(
        "play", help="Play one file on the default render device or a specific render device key"
    )
    play_parser.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    play_parser.add_argument("--device-key")
    play_parser.add_argument("--audio-file", required=True)
    play_parser.add_argument("--repeat", type=int, default=1)
    play_parser.add_argument("--gap", type=float, default=0.0)
    play_parser.add_argument("--skip-probe", action="store_true")
    play_parser.set_defaults(func=run_play)

    dual_parser = subparsers.add_parser(
        "dual-play",
        help="Play two files in parallel; each side uses a specific render device key or the default device",
    )
    dual_parser.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    dual_parser.add_argument("--left-device-key")
    dual_parser.add_argument("--left-file", required=True)
    dual_parser.add_argument("--right-device-key")
    dual_parser.add_argument("--right-file", required=True)
    dual_parser.add_argument("--repeat", type=int, default=1)
    dual_parser.add_argument("--gap", type=float, default=0.0)
    dual_parser.add_argument("--skip-probe", action="store_true")
    dual_parser.set_defaults(func=run_dual_play)

    internal_parser = subparsers.add_parser("internal-play-once", help=argparse.SUPPRESS)
    internal_parser.add_argument("--platform", choices=("auto", "windows", "linux"), required=True)
    internal_parser.add_argument("--device-key")
    internal_parser.add_argument("--audio-file")
    internal_parser.add_argument("--probe-only", action="store_true")
    internal_parser.set_defaults(func=run_internal_worker)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "repeat", 1) < 1:
        raise RuntimeError("repeat must be at least 1")
    if getattr(args, "gap", 0.0) < 0:
        raise RuntimeError("gap must be >= 0")
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
