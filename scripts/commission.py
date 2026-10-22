"""更新或检查 CSK 烧录固件清单。

用法：
  python scripts/commission.py --key img
  python scripts/commission.py --key hex --tool-md5 0x...
  python scripts/commission.py --key img --update
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_hex(path: Path) -> tuple[int, str]:
    """返回 (4KB 对齐后的写入字节数, 映射到 16MB 镜像后的 MD5)。"""
    blocks: dict[int, bytearray] = {}
    base = 0
    for raw in path.read_text(encoding="ascii", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith(":"):
            continue
        try:
            record = bytes.fromhex(line[1:])
        except ValueError:
            continue
        if len(record) < 5:
            continue
        count = record[0]
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        payload = record[4:4 + count]
        if record_type == 0x04 and len(payload) >= 2:
            base = ((payload[0] << 8) | payload[1]) << 16
        elif record_type == 0x02 and len(payload) >= 2:
            base = ((payload[0] << 8) | payload[1]) << 4
        elif record_type == 0x00:
            absolute = base + address
            block_index = absolute // 0x1000
            block = blocks.setdefault(block_index, bytearray(b"\xff" * 0x1000))
            offset = absolute % 0x1000
            block[offset:offset + count] = payload
    write_size = sum(len(block) for block in blocks.values())
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    for block_index in sorted(blocks):
        absolute = block_index * 0x1000
        offset = absolute - 0x18000000 if absolute >= 0x18000000 else absolute
        if 0 <= offset < len(image):
            block = blocks[block_index]
            image[offset:offset + len(block)] = block
    return write_size, hashlib.md5(bytes(image)).hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    skill_root = Path(__file__).resolve().parents[1]
    default_manifest = root / "cskflashtest" / "config" / "firmware-manifest.json"
    if not default_manifest.exists():
        default_manifest = skill_root / "config" / "firmware-manifest.json"
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(default_manifest))
    parser.add_argument("--key", choices=("img", "hex"), required=True)
    parser.add_argument("--fw", default="")
    parser.add_argument("--tool-md5", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--build", default="")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["firmware"][args.key]
    fw_path = Path(args.fw) if args.fw else root / entry["path"]
    if not fw_path.is_absolute():
        fw_path = root / fw_path
    if args.fw and not fw_path.exists():
        fw_path = skill_root / args.fw
    if not fw_path.exists():
        fallback = skill_root / entry["path"]
        if fallback.exists():
            fw_path = fallback
    if not fw_path.is_file():
        raise SystemExit(f"firmware not found: {fw_path}")

    sha256 = sha256_file(fw_path)
    if args.key == "img":
        write_size = fw_path.stat().st_size
        image_md5 = md5_file(fw_path)
        tool_md5 = image_md5
    else:
        write_size, image_md5 = parse_hex(fw_path)
        tool_md5 = args.tool_md5.lower().removeprefix("0x") or entry.get("expected_tool_md5", "")

    print(json.dumps({
        "key": args.key,
        "path": str(fw_path),
        "size": fw_path.stat().st_size,
        "sha256": sha256,
        "expected_write_size": write_size,
        "mapped_image_md5": image_md5,
        "expected_tool_md5": tool_md5,
    }, ensure_ascii=False, indent=2))

    if args.update:
        entry["source_sha256"] = sha256
        entry["expected_write_size"] = write_size
        if tool_md5:
            entry["expected_tool_md5"] = tool_md5
        if args.version:
            entry["expected_version"] = args.version
        if args.build:
            entry["expected_build"] = args.build
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"updated {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
