"""定位外部 lstest 运行时（本仓库不再内置 lstest 副本）。

解析顺序：
1. 已可导入的 ``lstest`` 包（pip 安装，或调用方已把父目录放进 ``sys.path``）；
2. 环境变量 ``LSTEST_HOME``：指向 lstest 仓库根目录（内含 ``scripts/``）；
3. 同级目录 ``<项目根>/lstest``：现场机器历史布局，``cskflashtest/`` 与 ``lstest/`` 并列。

找不到或版本过旧时直接抛 ``RuntimeError`` 并给出安装命令，不会回退到任何内置副本。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parent

# cskflashtest 直接使用的公共模块；缺任意一个都说明装的是旧版 lstest。
REQUIRED_MODULES = (
    "core",
    "profile",
    "runtime",
    "timeline",
    "interaction_acceptance",
    "environment_cleanup",
    "result_stats",
    "serial_recovery",
)

INSTALL_HINT = (
    "python -m pip install "
    '"lstest @ git+https://github.com/zjashanda/lstest.git@<commit>"'
)


def _find_spec_root() -> Path | None:
    spec = importlib.util.find_spec("lstest")
    if spec is None or not spec.submodule_search_locations:
        return None
    location = Path(next(iter(spec.submodule_search_locations))).resolve()
    parent = str(location.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return location


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("LSTEST_HOME", "").strip()
    if env:
        roots.append(Path(env).expanduser().resolve())
    roots.append((PROJECT_ROOT / "lstest").resolve())
    roots.append((SKILL_ROOT / "lstest").resolve())
    return roots


def _missing_modules(root: Path) -> list[str]:
    scripts = root / "scripts"
    return [name for name in REQUIRED_MODULES if not (scripts / f"{name}.py").is_file()]


def resolve_lstest_root() -> Path:
    """返回 lstest 仓库根目录；必要时把它的父目录加入 ``sys.path``。"""
    root = _find_spec_root()
    if root is not None:
        missing = _missing_modules(root)
        if missing:
            raise RuntimeError(
                f"已导入的 lstest 版本过旧，缺少模块：{', '.join(missing)}。"
                f"请安装新版：{INSTALL_HINT}"
            )
        return root

    for candidate in _candidate_roots():
        if not (candidate / "scripts").is_dir():
            continue
        if candidate.name != "lstest":
            raise RuntimeError(
                f"LSTEST_HOME 必须指向名为 lstest 的仓库根目录，当前为：{candidate}"
            )
        parent = str(candidate.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        found = _find_spec_root()
        if found is None:
            continue
        missing = _missing_modules(found)
        if missing:
            raise RuntimeError(
                f"lstest（{found}）缺少模块：{', '.join(missing)}。"
                f"请更新到含这些模块的版本：{INSTALL_HINT}"
            )
        return found

    raise RuntimeError(
        "找不到 lstest 运行时。本 skill 不再内置副本，请任选其一：\n"
        f"  1) 安装：{INSTALL_HINT}\n"
        "  2) 设置 LSTEST_HOME=<lstest 仓库根目录>\n"
        "  3) 把 lstest 仓库检出到与 cskflashtest 同级的位置"
    )


def ensure_lstest_runtime() -> Path:
    """解析并返回 lstest 根目录，供入口脚本在导入前调用。"""
    return resolve_lstest_root()


def lstest_script(name: str) -> Path:
    """返回 lstest 内的脚本路径（例如 ``listenai_play.py``）。"""
    path = resolve_lstest_root() / "scripts" / name
    if not path.is_file():
        raise RuntimeError(f"lstest 中缺少脚本：{path}")
    return path
