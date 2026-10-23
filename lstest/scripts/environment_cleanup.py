"""压测前环境清理工具。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable

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
