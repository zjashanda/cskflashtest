"""压测前环境清理工具。"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def clear_directory(path: Path) -> None:
    """清空目录内容，目录不存在时忽略。"""
    shutil.rmtree(path, ignore_errors=True)


def cleanup_temp_files(directory: Path, pattern: str, older_than_s: float) -> int:
    """删除目录下满足模式且超过指定年龄的文件，返回删除数量。"""
    if not directory.is_dir():
        return 0
    now = time.time()
    removed = 0
    for item in directory.glob(pattern):
        try:
            if item.is_file() and now - item.stat().st_mtime >= older_than_s:
                item.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def kill_processes_by_command(patterns: Iterable[str], *, exclude_pids: Iterable[int] = ()) -> int:
    """按命令行正则清理残留 Python 子进程，仅支持 Windows。"""
    if os.name != "nt":
        return 0
    pattern = "|".join(item for item in patterns if item)
    if not pattern:
        return 0
    excluded = ",".join(str(int(pid)) for pid in exclude_pids)
    exclude_clause = f" -and $_.ProcessId -notin @({excluded})" if excluded else ""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{pattern}'{exclude_clause} }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def kill_process_tree(pid: int) -> bool:
    """终止指定 PID 及其子进程树；Windows 使用 taskkill /T /F。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def register_process_cleanup(process_getter: Callable[[], Iterable[object]]) -> Callable[[], None]:
    """注册退场清理：异常退出时终止仍在运行的子进程树，避免孤儿进程。

    `process_getter` 返回 Popen 等进程对象列表；清理时只处理仍存活的进程。
    """
    def cleanup() -> None:
        try:
            processes = list(process_getter())
        except Exception:
            return
        for process in processes:
            if process is None:
                continue
            try:
                if process.poll() is not None:
                    continue
            except Exception:
                pass
            pid = getattr(process, "pid", None)
            if pid and kill_process_tree(int(pid)):
                continue
            try:
                process.terminate()
            except Exception:
                pass

    atexit.register(cleanup)
    return cleanup
