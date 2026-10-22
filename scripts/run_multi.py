"""多设备 CSK 烧录压测编排器。

每台设备使用独立的 control/csk/burn 串口，启动一个独立的 run.py 进程，
最多同时 10 台。每台设备仍生成独立的 lstest 标准结果目录。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parent
RUN_PY = SKILL_ROOT / "scripts" / "run.py"
MAX_DEVICES = 10


def now_human() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_devices(path: Path) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    devices = [dict(item) for item in payload.get("devices", []) if item.get("enabled", True)]
    if not devices:
        raise ValueError(f"no enabled devices in {path}")
    if len(devices) > MAX_DEVICES:
        raise ValueError(f"too many enabled devices: {len(devices)} > {MAX_DEVICES}")
    ids: set[str] = set()
    used_ports: dict[str, str] = {}
    for item in devices:
        device_id = str(item.get("id", "")).strip()
        if not device_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", device_id):
            raise ValueError(f"invalid device id: {device_id!r}")
        if device_id in ids:
            raise ValueError(f"duplicate device id: {device_id}")
        ids.add(device_id)
        for role in ("control", "csk", "burn"):
            port = str(item.get(role, "")).strip().upper()
            if not port:
                raise ValueError(f"{device_id} missing {role} port")
            if port in used_ports:
                raise ValueError(f"port {port} used by both {used_ports[port]} and {device_id}/{role}")
            used_ports[port] = f"{device_id}/{role}"
        hisi = str(item.get("hisi", "")).strip().upper()
        if hisi:
            if hisi in used_ports:
                raise ValueError(f"port {hisi} used by both {used_ports[hisi]} and {device_id}/hisi")
            used_ports[hisi] = f"{device_id}/hisi"
    return devices


def result_dir_for(result_root: Path, label: str, rounds: int, before: set[Path] | None = None) -> Path | None:
    candidates = [p for p in result_root.glob(f"*_cskflashtest_{rounds}_{label}") if p.is_dir()]
    if before is not None:
        fresh = [p for p in candidates if p.resolve() not in before]
        if fresh:
            candidates = fresh
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return candidates[-1]


def completed_rounds(result_dir: Path | None) -> int:
    if result_dir is None:
        return 0
    path = result_dir / "results.csv"
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return max(0, sum(1 for _ in csv.DictReader(handle)))
    except OSError:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CSK 多设备并行烧录压测")
    parser.add_argument("--devices", default=str(SKILL_ROOT / "config" / "devices.json"))
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--max-parallel", type=int, default=MAX_DEVICES)
    parser.add_argument("--stagger", type=float, default=2.0, help="启动设备之间的间隔秒数")
    parser.add_argument("--fw", default="", help="全局固件类型；留空则使用 auto/设备配置")
    parser.add_argument("--no-hisi", action="store_true")
    parser.add_argument("--wake-test", action="store_true", help="每轮初始化后切换英文并验证唤醒")
    parser.add_argument("--interaction-test", action="store_true", help="唤醒后播报命令词并校验 ASR/响应")
    parser.add_argument("--result-root", default=str(PROJECT_ROOT / "result"))
    parser.add_argument("--summary-dir", default="")
    parser.add_argument("--dry-run", action="store_true", help="只校验设备和端口配置，不启动烧录")
    parser.add_argument("--only", default="", help="only run selected devices, comma separated id or label")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.rounds <= 0:
        raise SystemExit("--rounds must be > 0")
    devices = load_devices(Path(args.devices))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        devices = [item for item in devices if item["id"] in wanted or str(item.get("label", "")) in wanted]
        if not devices:
            raise SystemExit(f"no devices matched --only {args.only}")
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        devices = [item for item in devices if item["id"] in wanted or str(item.get("label", "")) in wanted]
        if not devices:
            raise SystemExit(f"no devices matched --only {args.only}")
    max_parallel = max(1, min(MAX_DEVICES, int(args.max_parallel), len(devices)))
    if args.dry_run:
        print(f"devices={len(devices)} max_parallel={max_parallel}")
        for item in devices:
            print(f"  {item['id']}: control={item['control']} csk={item['csk']} burn={item['burn']} hisi={item.get('hisi', '')}")
        return 0

    result_root = Path(args.result_root).resolve()
    result_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    summary_dir = Path(args.summary_dir) if args.summary_dir else PROJECT_ROOT / "multi_logs" / f"{stamp}_cskflashtest_{args.rounds}_{len(devices)}dev"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "summary.csv"
    progress_path = summary_dir / "progress.log"
    progress_handle = progress_path.open("w", encoding="utf-8", newline="\n")
    rows: list[dict] = []
    last_progress = time.monotonic()
    pending = deque(devices)
    running: list[dict] = []
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})

    with summary_path.open("w", encoding="utf-8-sig", newline="") as summary:
        writer = csv.DictWriter(summary, fieldnames=[
            "device_id", "label", "control", "csk", "burn", "hisi", "rounds", "completed",
            "returncode", "status", "result_dir", "started_at", "ended_at", "duration_s",
        ])
        writer.writeheader()
        summary.flush()

        while pending or running:
            while pending and len(running) < max_parallel:
                device = pending.popleft()
                label = str(device.get("label") or device["id"])
                before = {p.resolve() for p in result_root.glob(f"*_cskflashtest_{args.rounds}_{label}")}
                log_path = summary_dir / f"{label}.console.log"
                log_handle = log_path.open("w", encoding="utf-8", newline="\n")
                cmd = [
                    sys.executable, "-u", str(RUN_PY),
                    "--devices", str(Path(args.devices).resolve()),
                    "--device-id", str(device["id"]),
                    "--rounds", str(args.rounds),
                    "--result-root", str(result_root),
                ]
                if args.fw:
                    cmd += ["--fw", args.fw]
                if args.no_hisi:
                    cmd += ["--no-hisi"]
                if args.wake_test or args.interaction_test:
                    cmd += ["--wake-test"]
                if args.interaction_test:
                    cmd += ["--interaction-test"]
                started = time.monotonic()
                try:
                    process = subprocess.Popen(
                        cmd, cwd=str(PROJECT_ROOT), env=env,
                        stdout=log_handle, stderr=subprocess.STDOUT,
                    )
                except Exception as exc:
                    log_handle.write(f"launcher failed: {type(exc).__name__}: {exc}\n")
                    log_handle.close()
                    row = {
                        "device_id": device["id"], "label": device.get("label", ""), "control": device["control"],
                        "csk": device["csk"], "burn": device["burn"], "hisi": device.get("hisi", ""),
                        "rounds": args.rounds, "completed": 0, "returncode": -1,
                        "status": "FAIL", "result_dir": "",
                        "started_at": now_human(), "ended_at": now_human(), "duration_s": 0.0,
                    }
                    rows.append(row)
                    writer.writerow(row)
                    summary.flush()
                    print(f"[{now_human()}] START_FAILED {device['id']}: {exc}", flush=True)
                    continue
                running.append({
                    "device": device, "process": process, "log": log_handle,
                    "log_path": log_path, "started": started, "before": before,
                })
                print(f"[{now_human()}] START {device['id']} (parallel {len(running)}/{max_parallel})", flush=True)
                if pending:
                    time.sleep(max(0.0, args.stagger))

            now = time.monotonic()
            if now - last_progress >= 30:
                parts = []
                for item in running:
                    rdir = result_dir_for(result_root, str(item["device"].get("label") or item["device"]["id"]), args.rounds, item["before"])
                    parts.append(f"{item['device'].get('label', item['device']['id'])}={completed_rounds(rdir)}/{args.rounds}")
                    item["result_dir"] = rdir
                line = f"{now_human()} PROGRESS " + " ".join(parts)
                print(line, flush=True)
                progress_handle.write(line + "\n")
                progress_handle.flush()
                last_progress = now

            for item in list(running):
                code = item["process"].poll()
                if code is None:
                    continue
                finished = time.monotonic()
                item["log"].close()
                running.remove(item)
                device = item["device"]
                result_dir_path = result_dir_for(result_root, str(device.get("label") or device["id"]), args.rounds, item["before"])
                result_dir = result_dir_path or ""
                completed = completed_rounds(result_dir_path)
                status = "PASS" if code == 0 else ("BLOCKED" if code == 2 else "FAIL")
                row = {
                    "device_id": device["id"],
                    "label": device.get("label", ""),
                    "control": device["control"],
                    "csk": device["csk"],
                    "burn": device["burn"],
                    "hisi": device.get("hisi", ""),
                    "rounds": args.rounds,
                    "completed": completed,
                    "returncode": code,
                    "status": status,
                    "result_dir": str(result_dir),
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["started"] + time.time() - time.monotonic())),
                    "ended_at": now_human(),
                    "duration_s": round(finished - item["started"], 1),
                }
                rows.append(row)
                writer.writerow(row)
                summary.flush()
                print(f"[{now_human()}] DONE {device['id']} {status} code={code} dir={result_dir}", flush=True)

            time.sleep(0.2)

    progress_handle.close()
    print(f"[{now_human()}] summary: {summary_path}", flush=True)
    for row in rows:
        print(f"  {row.get('label', row['device_id'])}: {row['status']} completed={row['completed']}/{row['rounds']} {row['result_dir']}", flush=True)
    return 0 if all(row["returncode"] == 0 for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
