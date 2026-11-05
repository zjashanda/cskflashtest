"""Download and restore large local burn assets only when they are missing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


class AssetBootstrapError(RuntimeError):
    pass


SKILL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = SKILL_ROOT / "config" / "asset-bootstrap.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetBootstrapError(f"asset manifest cannot be read: {path}: {exc}") from exc


def asset_status(root: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    missing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for asset in manifest["assets"]:
        path = root / str(asset["path"])
        if not path.is_file():
            missing.append(asset)
            continue
        expected_size = int(asset["size"])
        expected_hash = str(asset["sha256"]).lower()
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_size != expected_size or actual_hash != expected_hash:
            invalid.append({
                **asset,
                "actual_size": actual_size,
                "actual_sha256": actual_hash,
            })
    return missing, invalid


def acquire_lock(lock_path: Path, timeout_s: float = 900.0) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise AssetBootstrapError(f"timed out waiting for asset bootstrap lock: {lock_path}")
            time.sleep(1.0)


def download_archive(url: str, expected_size: int, expected_hash: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "cskflashtest-asset-bootstrap/1"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    except OSError as exc:
        raise AssetBootstrapError(f"asset archive download failed: {url}: {exc}") from exc
    actual_size = destination.stat().st_size
    actual_hash = sha256_file(destination)
    if actual_size != expected_size or actual_hash.lower() != expected_hash.lower():
        raise AssetBootstrapError(
            f"asset archive verification failed: size={actual_size}/{expected_size}, "
            f"sha256={actual_hash}/{expected_hash}"
        )


def restore_missing(root: Path, manifest: dict[str, Any], archive_file: Path | None = None) -> dict[str, Any]:
    missing, invalid = asset_status(root, manifest)
    if invalid:
        details = "; ".join(
            f"{item['path']} size={item['actual_size']}/{item['size']} "
            f"sha256={item['actual_sha256']}/{item['sha256']}"
            for item in invalid
        )
        raise AssetBootstrapError(f"refusing to overwrite existing asset(s) with mismatched hash: {details}")
    if not missing:
        return {"downloaded": False, "restored": [], "missing": []}

    lock_path = root / ".asset-bootstrap.lock"
    lock_fd = acquire_lock(lock_path)
    archive_temp: Path | None = None
    try:
        os.close(lock_fd)
        missing, invalid = asset_status(root, manifest)
        if invalid:
            raise AssetBootstrapError("asset changed while waiting for bootstrap lock")
        if not missing:
            return {"downloaded": False, "restored": [], "missing": []}

        if archive_file is None:
            with tempfile.NamedTemporaryFile(prefix="csk6hisiflashtools-", suffix=".zip", dir=root, delete=False) as temp:
                archive_temp = Path(temp.name)
            download_archive(
                str(manifest["archive_url"]),
                int(manifest["archive_size"]),
                str(manifest["archive_sha256"]),
                archive_temp,
            )
            archive_path = archive_temp
        else:
            archive_path = archive_file
            if not archive_path.is_file():
                raise AssetBootstrapError(f"asset archive not found: {archive_path}")
            if archive_path.stat().st_size != int(manifest["archive_size"]) or sha256_file(archive_path) != str(manifest["archive_sha256"]).lower():
                raise AssetBootstrapError(f"asset archive verification failed: {archive_path}")

        restored: list[str] = []
        root_resolved = root.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            prefix = str(manifest["archive_root"])
            for member in archive.infolist():
                target = (root / member.filename).resolve()
                if target != root_resolved and root_resolved not in target.parents:
                    raise AssetBootstrapError(f"archive contains unsafe path: {member.filename}")
            for asset in missing:
                relative = str(asset["path"])
                member = prefix + relative
                if member not in names:
                    raise AssetBootstrapError(f"asset missing from archive: {member}")
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
                os.close(fd)
                temp_path = Path(temp_name)
                try:
                    with archive.open(member) as source, temp_path.open("wb") as target:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                    if temp_path.stat().st_size != int(asset["size"]) or sha256_file(temp_path) != str(asset["sha256"]).lower():
                        raise AssetBootstrapError(f"restored asset verification failed: {relative}")
                    if destination.exists():
                        raise AssetBootstrapError(f"asset appeared during restore; refusing overwrite: {relative}")
                    os.replace(temp_path, destination)
                    restored.append(relative)
                finally:
                    temp_path.unlink(missing_ok=True)
        missing_after, invalid_after = asset_status(root, manifest)
        if missing_after or invalid_after:
            raise AssetBootstrapError("asset restore did not produce a complete verified set")
        return {"downloaded": archive_file is None, "restored": restored, "missing": []}
    finally:
        if archive_temp is not None:
            archive_temp.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def ensure_assets(root: Path = SKILL_ROOT, manifest_path: Path = MANIFEST_PATH, archive_file: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    return restore_missing(root.resolve(), manifest, archive_file=archive_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore missing CSK/HISI burn assets from the public asset archive.")
    parser.add_argument("--root", default=str(SKILL_ROOT), help="Skill root where assets are restored")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--archive-file", default="", help="Use a local archive instead of downloading")
    parser.add_argument("--dry-run", action="store_true", help="Only report missing/mismatched assets")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    manifest = load_manifest(Path(args.manifest).resolve())
    missing, invalid = asset_status(root, manifest)
    if invalid:
        raise AssetBootstrapError("existing asset hash mismatch; refusing overwrite")
    if args.dry_run:
        print(json.dumps({"missing": [item["path"] for item in missing], "invalid": []}, ensure_ascii=False, indent=2))
        return 0
    result = restore_missing(root, manifest, archive_file=Path(args.archive_file).resolve() if args.archive_file else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssetBootstrapError as exc:
        print(f"ASSET_BOOTSTRAP_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
