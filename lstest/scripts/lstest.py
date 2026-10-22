#!/usr/bin/env python3
"""Command-line entry point for the reusable lstest foundation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
SCRIPT_ROOT = Path(__file__).resolve().parent
for import_root in (PROJECT_ROOT, SCRIPT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

try:
    from .audio_synthesis import (
        DEFAULT_PITCH, DEFAULT_RATE, DEFAULT_VOICE, DEFAULT_VOLUME, load_text_manifest,
        synthesize_edge_tts, synthesize_manifest,
    )
    from .core import ConnectionSpec
    from .profile import DeviceProfile, ProfileError
    from .smoke import run_smoke
except ImportError:
    from audio_synthesis import (
        DEFAULT_PITCH, DEFAULT_RATE, DEFAULT_VOICE, DEFAULT_VOLUME, load_text_manifest,
        synthesize_edge_tts, synthesize_manifest,
    )
    from core import ConnectionSpec
    from profile import DeviceProfile, ProfileError
    from smoke import run_smoke


def parse_ports(values: list[str], baudrate: int) -> list[dict[str, object]]:
    result = []
    for value in values:
        parts = value.split(":")
        port = parts[0].strip()
        role = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        if not port:
            raise ValueError("port cannot be empty")
        result.append({"port": port, "role": role, "baudrate": baudrate})
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="lstest 通用设备初始化与基础测试运行时")
    sub = value.add_subparsers(dest="command", required=True)
    list_cmd = sub.add_parser("list", help="列出公共能力")
    list_cmd.add_argument("--json", action="store_true")

    tts = sub.add_parser("tts", help="使用 Edge TTS 合成并校验一条 MP3 音频")
    text_source = tts.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text")
    text_source.add_argument("--text-file", type=Path, help="包含单条 UTF-8 语料的文本文件")
    tts.add_argument("--output", type=Path, required=True)
    tts.add_argument("--case-id", default="")
    tts.add_argument("--voice", default=DEFAULT_VOICE)
    tts.add_argument("--rate", default=DEFAULT_RATE)
    tts.add_argument("--pitch", default=DEFAULT_PITCH)
    tts.add_argument("--volume", default=DEFAULT_VOLUME)
    tts.add_argument("--timeout-s", type=float, default=60.0)
    tts.add_argument("--retries", type=int, default=1)

    tts_manifest = sub.add_parser("tts-manifest", help="按文本 manifest 批量合成并写入音频 manifest")
    tts_manifest.add_argument("--input", type=Path, required=True, help="包含 text 列的 UTF-8 CSV 或 JSONL")
    tts_manifest.add_argument("--output-dir", type=Path, required=True)
    tts_manifest.add_argument("--manifest-output", type=Path, required=True)
    tts_manifest.add_argument("--voice", default=DEFAULT_VOICE)
    tts_manifest.add_argument("--rate", default=DEFAULT_RATE)
    tts_manifest.add_argument("--pitch", default=DEFAULT_PITCH)
    tts_manifest.add_argument("--volume", default=DEFAULT_VOLUME)
    tts_manifest.add_argument("--timeout-s", type=float, default=60.0)
    tts_manifest.add_argument("--retries", type=int, default=1)

    audio = sub.add_parser("audio", help="管理 laid、枚举声卡、探测或播放音频")
    audio_sub = audio.add_subparsers(dest="audio_command", required=True)
    ensure_laid = audio_sub.add_parser("ensure-laid", help="安装或刷新 laid / audio-list 到用户 shell profile")
    ensure_laid.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    ensure_laid.add_argument("--force", action="store_true")
    scan = audio_sub.add_parser("scan", help="扫描活动 ListenAI 声卡及稳定设备 key")
    scan.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    scan.add_argument("--direction", choices=("All", "Render", "Capture"), default="All")
    scan.add_argument("--json", action="store_true")
    probe = audio_sub.add_parser("probe", help="探测默认播放设备或指定稳定设备 key")
    probe.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    probe.add_argument("--device-key")
    play = audio_sub.add_parser("play", help="在默认或指定稳定设备 key 上播放单个音频")
    play.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    play.add_argument("--audio-file", type=Path, required=True)
    play.add_argument("--device-key")
    play.add_argument("--repeat", type=int, default=1)
    play.add_argument("--gap", type=float, default=0.0)
    play.add_argument("--skip-probe", action="store_true")
    dual = audio_sub.add_parser("dual-play", help="在两个默认或指定稳定设备 key 上并行播放")
    dual.add_argument("--platform", choices=("auto", "windows", "linux"), default="auto")
    dual.add_argument("--left-file", type=Path, required=True)
    dual.add_argument("--right-file", type=Path, required=True)
    dual.add_argument("--left-device-key")
    dual.add_argument("--right-device-key")
    dual.add_argument("--repeat", type=int, default=1)
    dual.add_argument("--gap", type=float, default=0.0)
    dual.add_argument("--skip-probe", action="store_true")
    pre = sub.add_parser("preflight", help="只检查 profile 和输入，不打开硬件")
    pre.add_argument("--profile", type=Path, required=True)
    pre.add_argument("--port", action="append", default=[])
    pre.add_argument("--baudrate", type=int, default=0)
    pre.add_argument("--playback-device-key")
    pre.add_argument("--result-root", type=Path, default=Path("result"))
    for name in ("init", "smoke"):
        command = sub.add_parser(name, help="执行基础初始化 smoke")
        command.add_argument("--profile", type=Path, required=True)
        command.add_argument("--port", action="append", default=[])
        command.add_argument("--baudrate", type=int, default=0)
        command.add_argument("--playback-device-key")
        command.add_argument("--capture-device-key")
        command.add_argument("--result-root", type=Path, default=Path("result"))
        command.add_argument("--hardware", action="store_true", help="允许打开串口和播放设备")
    return value


def run_audio_tool(args: argparse.Namespace) -> int:
    """Delegate device binding to the bundled, independently testable tool."""
    script = SCRIPT_ROOT / "listenai_play.py"
    if not script.is_file():
        raise RuntimeError(f"bundled audio tool is missing: {script}")
    command = [sys.executable, str(script), args.audio_command, "--platform", args.platform]
    if args.audio_command == "ensure-laid":
        if args.force:
            command.append("--force")
    elif args.audio_command == "scan":
        command.extend(["--direction", args.direction])
        if args.json:
            command.append("--json")
    elif args.audio_command == "probe":
        if args.device_key:
            command.extend(["--device-key", args.device_key])
    elif args.audio_command == "play":
        command.extend(["--audio-file", str(args.audio_file), "--repeat", str(args.repeat), "--gap", str(args.gap)])
        if args.device_key:
            command.extend(["--device-key", args.device_key])
        if args.skip_probe:
            command.append("--skip-probe")
    elif args.audio_command == "dual-play":
        command.extend([
            "--left-file", str(args.left_file), "--right-file", str(args.right_file),
            "--repeat", str(args.repeat), "--gap", str(args.gap),
        ])
        if args.left_device_key:
            command.extend(["--left-device-key", args.left_device_key])
        if args.right_device_key:
            command.extend(["--right-device-key", args.right_device_key])
        if args.skip_probe:
            command.append("--skip-probe")
    else:  # pragma: no cover - argparse limits this value
        raise ValueError(f"unsupported audio command: {args.audio_command}")
    completed = subprocess.run(command, check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "list":
        payload = {
            "skill": "lstest",
            "capabilities": [
                "dynamic-serial", "profile", "evidence", "basic-smoke", "safe-stop",
                "edge-tts", "audio-device-scan", "laid-installer", "device-key-playback",
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "\n".join(payload["capabilities"]))
        return 0
    try:
        if args.command == "tts":
            text = args.text
            if args.text_file is not None:
                text = args.text_file.read_text(encoding="utf-8").strip()
            payload = synthesize_edge_tts(
                text, args.output, case_id=args.case_id, voice=args.voice, rate=args.rate,
                pitch=args.pitch, volume=args.volume, timeout_s=args.timeout_s, retries=args.retries,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "tts-manifest":
            payload = synthesize_manifest(
                load_text_manifest(args.input), args.output_dir, args.manifest_output,
                voice=args.voice, rate=args.rate, pitch=args.pitch, volume=args.volume,
                timeout_s=args.timeout_s, retries=args.retries,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["status"] == "PASS" else 2
        if args.command == "audio":
            return run_audio_tool(args)
        if args.baudrate <= 0 and args.port:
            raise ValueError("--baudrate must be positive when ports are supplied")
        connection = ConnectionSpec.from_mapping({"ports": parse_ports(args.port, args.baudrate) if args.port else [], "playback_device_key": args.playback_device_key, "capture_device_key": getattr(args, "capture_device_key", None), "profile_id": str(args.profile), "result_root": str(args.result_root)})
        profile = DeviceProfile.load(args.profile)
        if args.command == "preflight":
            print(json.dumps({"status": "PASS", "profile_id": profile.profile_id, "profile_sha256": profile.sha256, "connection": connection.to_dict()}, ensure_ascii=False, indent=2))
            return 0
        summary = run_smoke(connection, args.profile, hardware=args.hardware)
        return 0 if summary["status"] in {"PASS", "WARN"} else 2
    except (ValueError, ProfileError, RuntimeError, OSError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
