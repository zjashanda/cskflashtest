"""多设备 CSK 烧录压测编排器。

每台设备使用独立的 control/csk/burn 串口，启动一个独立的 run.py 进程，
最多同时 10 台。每台设备仍生成独立的 lstest 标准结果目录。
"""

from __future__ import annotations

import argparse
import atexit
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parent
RUN_PY = SKILL_ROOT / "scripts" / "run.py"
MAX_DEVICES = 10
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lstest.scripts.environment_cleanup import (  # noqa: E402
    KillOnCloseJob,
    clear_directory,
    cleanup_temp_files,
    find_processes_by_command,
    kill_processes_by_command,
    register_process_cleanup,
)
from lstest.scripts.interaction_acceptance import MODES  # noqa: E402
from lstest.scripts.result_stats import (  # noqa: E402
    ResultSummary,
    aggregate_summaries,
    empty_summary,
    total_line,
)


def now_human() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_devices(path: Path, mode: str = "burn") -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    devices = [dict(item) for item in payload.get("devices", []) if item.get("enabled", True)]
    if not devices:
        raise ValueError(f"no enabled devices in {path}")
    if len(devices) > MAX_DEVICES:
        raise ValueError(f"too many enabled devices: {len(devices)} > {MAX_DEVICES}")
    ids: set[str] = set()
    used_ports: dict[str, str] = {}
    required_roles = ("control", "csk", "burn") if mode == "burn" else ("control", "csk")
    for item in devices:
        device_id = str(item.get("id", "")).strip()
        if not device_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", device_id):
            raise ValueError(f"invalid device id: {device_id!r}")
        if device_id in ids:
            raise ValueError(f"duplicate device id: {device_id}")
        ids.add(device_id)
        for role in required_roles:
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


def result_dir_for(result_root: Path, label: str, rounds: int, before: set[Path] | None = None,
                   prefix: str = "cskflashtest", run_name: str | None = None) -> Path | None:
    if run_name:
        fixed = result_root / run_name
        if fixed.is_dir():
            return fixed
    candidates = [p for p in result_root.glob(f"*_{prefix}_{rounds}_{label}") if p.is_dir()]
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


def cleanup_before_run(devices: list[dict]) -> None:
    """清理上次压测遗留的音频锁、播放进程和临时音频文件。"""
    active = find_processes_by_command(
        [r"scripts[\\/]run\.py", r"run_multi\.py"],
        exclude_pids=[os.getpid()],
    )
    if active:
        print(
            f"[AUDIO_CLEANUP] active stress run detected ({len(active)} processes); aborting",
            flush=True,
        )
        raise SystemExit(2)
    clear_directory(PROJECT_ROOT / "logs" / "audio_slots")
    killed = kill_processes_by_command(
        [r"listenai_play\.py", r"internal-play-once"],
        exclude_pids=[os.getpid()],
    )
    removed = cleanup_temp_files(Path(tempfile.gettempdir()), "tmp*.wav", 3600)
    render_keys: set[str] = set()
    play_script = SKILL_ROOT / "lstest" / "scripts" / "listenai_play.py"
    if play_script.is_file():
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(play_script), "scan"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in result.stdout.splitlines():
                if line.startswith("Render"):
                    parts = line.split()
                    if len(parts) >= 2:
                        render_keys.add(parts[1])
        except Exception as exc:
            print(f"[AUDIO_CLEANUP] scan failed: {type(exc).__name__}: {exc}", flush=True)
    configured = {
        str(item.get("render_device_key", ""))
        for item in devices
        if item.get("render_device_key")
    }
    unused = sorted(render_keys - configured)
    missing = sorted(configured - render_keys)
    print(
        f"[AUDIO_CLEANUP] slots=cleared killed={killed} temp_wav_removed={removed} "
        f"render={len(render_keys)} unused={unused} missing={missing}",
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CSK 多设备并行压测")
    parser.add_argument("--mode", choices=["burn", "powercycle"], default="burn",
                        help="burn=烧录+唤醒ASR；powercycle=上下电+初始化+唤醒ASR")
    parser.add_argument("--devices", default=str(SKILL_ROOT / "config" / "devices.json"))
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--max-parallel", type=int, default=MAX_DEVICES)
    parser.add_argument("--stagger", type=float, default=2.0, help="启动设备之间的间隔秒数")
    parser.add_argument("--fw", default="", help="全局固件类型；留空则使用 auto/设备配置")
    parser.add_argument("--no-hisi", action="store_true")
    parser.add_argument("--wake-test", action="store_true", help="每轮初始化后切换英文并验证唤醒")
    parser.add_argument("--interaction-test", action="store_true", help="唤醒后播报命令词并校验 ASR/响应")
    parser.add_argument("--acceptance", choices=sorted(MODES), default="",
                        help="单轮交互验收模式：strict/any_asr/asr_optional/wake_only")
    parser.add_argument("--result-root", default=str(SKILL_ROOT / "result"))
    parser.add_argument("--summary-dir", default="")
    parser.add_argument("--dry-run", action="store_true", help="只校验设备和端口配置，不启动烧录")
    parser.add_argument("--stream", action="store_true", help="把每台设备的实时流水线输出到当前终端")
    parser.add_argument("--only", default="", help="only run selected devices, comma separated id or label")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.rounds <= 0:
        raise SystemExit("--rounds must be > 0")
    scenario = "cskpowercycle" if args.mode == "powercycle" else "cskflashtest"
    devices = load_devices(Path(args.devices), args.mode)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        devices = [item for item in devices if item["id"] in wanted or str(item.get("label", "")) in wanted]
        if not devices:
            raise SystemExit(f"no devices matched --only {args.only}")
    max_parallel = max(1, min(MAX_DEVICES, int(args.max_parallel), len(devices)))
    if args.dry_run:
        print(f"mode={args.mode} devices={len(devices)} max_parallel={max_parallel}")
        for item in devices:
            print(f"  {item['id']}: control={item['control']} csk={item['csk']} "
                  f"burn={item.get('burn', '')} hisi={item.get('hisi', '')}")
        return 0

    cleanup_before_run(devices)

    result_root = Path(args.result_root).resolve()
    result_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    batch_stamp = stamp
    task_summary = "burn-wake-asr" if args.mode == "burn" else "powercycle-init-wake-asr"
    summary_dir = Path(args.summary_dir) if args.summary_dir else SKILL_ROOT / "multi_logs" / f"{stamp}_{scenario}_{args.rounds}_{len(devices)}dev"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "summary.csv"
    progress_path = summary_dir / "progress.log"
    progress_handle = progress_path.open("w", encoding="utf-8", newline="\n")
    rows: list[dict] = []
    last_progress = time.monotonic()
    planned_rounds = len(devices) * args.rounds
    last_live_rounds = 0
    last_live_poll = 0.0
    summary_states: dict[str, ResultSummary] = {}

    def summary_for(path: Path | None) -> dict:
        if path is None:
            return empty_summary()
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        state = summary_states.get(key)
        if state is None:
            state = ResultSummary(path)
            summary_states[key] = state
        return state.update()

    pending = deque(devices)
    running: list[dict] = []
    completed_normally = False
    partial_summary_written = False

    def write_partial_summary() -> None:
        nonlocal partial_summary_written
        if completed_normally or partial_summary_written:
            return
        partial_summary_written = True
        active = list(running)
        if not active:
            return
        fieldnames = [
            "device_id", "label", "control", "csk", "burn", "hisi", "rounds", "completed",
            "returncode", "status", "result_dir", "started_at", "ended_at", "duration_s",
        ]
        try:
            with summary_path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                for item in active:
                    device = item["device"]
                    rdir = result_dir_for(
                        result_root, str(device.get("label") or device["id"]),
                        args.rounds, item["before"], prefix=scenario,
                        run_name=item.get("run_name"),
                    )
                    completed = summary_for(rdir)["rounds"] if rdir is not None else 0
                    writer.writerow({
                        "device_id": device["id"],
                        "label": device.get("label", ""),
                        "control": device["control"],
                        "csk": device["csk"],
                        "burn": device["burn"],
                        "hisi": device.get("hisi", ""),
                        "rounds": args.rounds,
                        "completed": completed,
                        "returncode": -1,
                        "status": "FAIL",
                        "result_dir": str(rdir or ""),
                        "started_at": time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.localtime(item["started"] + time.time() - time.monotonic()),
                        ),
                        "ended_at": now_human(),
                        "duration_s": round(time.monotonic() - item["started"], 1),
                    })
                handle.flush()
            print(f"[{now_human()}] PARTIAL_SUMMARY written for {len(active)} running devices", flush=True)
        except Exception:
            pass

    atexit.register(write_partial_summary)

    job = KillOnCloseJob()
    job.create()
    register_process_cleanup(lambda: [item.get("process") for item in running])

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
                run_name = f"{batch_stamp}_{scenario}_{device['id']}_{task_summary}_{args.rounds}rounds"
                before = {p.resolve() for p in result_root.glob(f"*_{scenario}_{args.rounds}_{label}")}
                log_path = summary_dir / f"{label}.console.log"
                log_handle = log_path.open("w", encoding="utf-8", newline="\n")
                cmd = [
                    sys.executable, "-u", str(RUN_PY),
                    "--mode", args.mode,
                    "--devices", str(Path(args.devices).resolve()),
                    "--device-id", str(device["id"]),
                    "--rounds", str(args.rounds),
                    "--result-root", str(result_root),
                    "--run-name", run_name,
                    "--batch-stamp", batch_stamp,
                    "--task-summary", task_summary,
                ]
                if args.fw:
                    cmd += ["--fw", args.fw]
                if args.no_hisi:
                    cmd += ["--no-hisi"]
                if args.wake_test or args.interaction_test:
                    cmd += ["--wake-test"]
                if args.interaction_test:
                    cmd += ["--interaction-test"]
                if args.acceptance:
                    cmd += ["--acceptance", args.acceptance]
                started = time.monotonic()
                try:
                    if args.stream:
                        process = subprocess.Popen(
                            cmd, cwd=str(PROJECT_ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )

                        def _pump(proc=process, handle=log_handle, tag=label):
                            try:
                                for line in proc.stdout:
                                    handle.write(line)
                                    handle.flush()
                                    print(f"[{tag}] {line.rstrip()}", flush=True)
                            finally:
                                try:
                                    handle.flush()
                                except Exception:
                                    pass

                        stream_thread = threading.Thread(target=_pump, daemon=True)
                        stream_thread.start()
                    else:
                        process = subprocess.Popen(
                            cmd, cwd=str(PROJECT_ROOT), env=env,
                            stdout=log_handle, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        stream_thread = None
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
                job.assign(process)
                running.append({
                    "device": device, "process": process, "log": log_handle,
                    "log_path": log_path, "started": started, "before": before,
                    "run_name": run_name, "thread": stream_thread,
                })
                print(f"[{now_human()}] START {device['id']} (parallel {len(running)}/{max_parallel})", flush=True)
                if pending:
                    time.sleep(max(0.0, args.stagger))

            now = time.monotonic()
            if now - last_live_poll >= 0.5:
                live_dirs: list[Path] = []
                for item in running:
                    rdir = result_dir_for(result_root, str(item["device"].get("label") or item["device"]["id"]),
                                          args.rounds, item["before"], prefix=scenario,
                                          run_name=item.get("run_name"))
                    item["result_dir"] = rdir
                    if rdir is not None:
                        live_dirs.append(rdir)
                for row in rows:
                    if row.get("result_dir"):
                        live_dirs.append(Path(row["result_dir"]))
                unique_dirs: list[Path] = []
                seen_dirs: set[str] = set()
                for path in live_dirs:
                    try:
                        key = str(path.resolve())
                    except OSError:
                        key = str(path)
                    if key not in seen_dirs:
                        seen_dirs.add(key)
                        unique_dirs.append(path)
                summaries = [summary_for(path) for path in unique_dirs]
                total = aggregate_summaries(summaries)
                completed = total["rounds"]
                if completed != last_live_rounds:
                    print(total_line(total, completed, planned_rounds), flush=True)
                    last_live_rounds = completed
                last_live_poll = now

            if now - last_progress >= 30:
                parts = []
                for item in running:
                    rdir = result_dir_for(result_root, str(item["device"].get("label") or item["device"]["id"]),
                                          args.rounds, item["before"], prefix=scenario,
                                          run_name=item.get("run_name"))
                    parts.append(f"{item['device'].get('label', item['device']['id'])}={summary_for(rdir)['rounds']}/{args.rounds}")
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
                stream_thread = item.get("thread")
                if stream_thread is not None:
                    stream_thread.join(timeout=5)
                item["log"].close()
                running.remove(item)
                device = item["device"]
                result_dir_path = result_dir_for(result_root, str(device.get("label") or device["id"]),
                                                 args.rounds, item["before"], prefix=scenario,
                                                 run_name=item.get("run_name"))
                result_dir = result_dir_path or ""
                completed = summary_for(result_dir_path)["rounds"]
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

    completed_normally = True
    progress_handle.close()
    print(f"[{now_human()}] summary: {summary_path}", flush=True)
    for row in rows:
        print(f"  {row.get('label', row['device_id'])}: {row['status']} completed={row['completed']}/{row['rounds']} {row['result_dir']}", flush=True)

    final_summaries: list[tuple[dict, dict]] = []
    for row in rows:
        result_dir = Path(row["result_dir"]) if row.get("result_dir") else None
        final_summaries.append((row, summary_for(result_dir)))

    outcomes_path = summary_dir / "summary_outcomes.csv"
    with outcomes_path.open("w", encoding="utf-8-sig", newline="") as outcomes_handle:
        outcomes_writer = csv.DictWriter(outcomes_handle, fieldnames=[
            "device_id", "label", "status", "rounds", "pass", "fail",
            "hit", "misheard", "empty", "asleep",
            "multi_wake", "multi_asr", "multi_result", "avg_round_s", "result_dir",
        ])
        outcomes_writer.writeheader()
        for row, item in final_summaries:
            outcomes_writer.writerow({
                "device_id": row["device_id"],
                "label": row.get("label", ""),
                "status": row["status"],
                "rounds": item["rounds"],
                "pass": item["pass"],
                "fail": item["fail"],
                "hit": item["hit"],
                "misheard": item["misheard"],
                "empty": item["empty"],
                "asleep": item["asleep"],
                "multi_wake": item["multi_wake"],
                "multi_asr": item["multi_asr"],
                "multi_result": item["multi_result"],
                "avg_round_s": item["avg_round_s"],
                "result_dir": row.get("result_dir", ""),
            })
        outcomes_handle.flush()

    total = aggregate_summaries([item for _, item in final_summaries])
    print("[OUTCOME] device rounds pass fail hit misheard empty asleep multi_wake multi_asr avg_s", flush=True)
    for row, item in final_summaries:
        print(
            f"  {row['device_id']}: {item['rounds']}/{row['rounds']} "
            f"pass={item['pass']} fail={item['fail']} "
            f"hit={item['hit']} misheard={item['misheard']} "
            f"empty={item['empty']} asleep={item['asleep']} "
            f"multi_wake={item['multi_wake']} multi_asr={item['multi_asr']} "
            f"avg={item['avg_round_s']}s",
            flush=True,
        )
    print(total_line(total, total["rounds"], planned_rounds, suffix="FINAL"), flush=True)
    print(f"outcomes: {outcomes_path}", flush=True)
    return 0 if all(row["returncode"] == 0 for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
