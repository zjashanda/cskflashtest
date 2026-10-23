"""压测前环境清理工具。"""

from __future__ import annotations

import atexit
import ctypes
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


class KillOnCloseJob:
    """Windows Job Object：父进程退出或崩溃时自动终止整棵子进程树。"""

    def __init__(self):
        self.handle: int | None = None

    def create(self) -> bool:
        if os.name != "nt":
            return False
        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return False
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def assign(self, process) -> bool:
        if os.name != "nt" or not self.handle:
            return False
        pid = getattr(process, "pid", None)
        if not pid:
            return False
        kernel32 = _kernel32()
        process_handle = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not process_handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self.handle, process_handle))
        finally:
            kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            _kernel32().CloseHandle(self.handle)
            self.handle = None


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
