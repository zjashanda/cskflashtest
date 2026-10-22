"""Edge TTS synthesis with reproducible manifests and audio validation."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


BEIJING = timezone(timedelta(hours=8))
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_RATE = "-10%"
DEFAULT_PITCH = "+0Hz"
DEFAULT_VOLUME = "+0%"
MANIFEST_FIELDS = [
    "case_id", "text", "audio_path", "provider", "model", "voice", "rate", "pitch", "volume",
    "container", "codec", "sample_rate", "channels", "duration_seconds", "size_bytes",
    "mean_volume_db", "peak_volume_db", "non_silent", "clipping_status", "sha256", "generated_at",
    "tool", "tool_version", "validation_status", "error",
]


def now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
            "stream=codec_name,sample_rate,channels,sample_fmt:format=format_name,duration", "-of", "json", str(path),
        ],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, timeout=20,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "ffprobe failed").strip())
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    media_format = payload.get("format") or {}
    return {
        "container": str(media_format.get("format_name") or "").split(",")[0],
        "codec": str(stream.get("codec_name") or ""),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "sample_fmt": str(stream.get("sample_fmt") or ""),
        "duration_seconds": round(float(media_format.get("duration") or 0), 6),
    }


def volume_metrics(path: Path) -> tuple[float, float]:
    sink = "NUL" if os.name == "nt" else "/dev/null"
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af", "volumedetect", "-f", "null", sink],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, timeout=30,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "ffmpeg decode failed").strip())
    output = completed.stderr or ""
    mean_match = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", output)
    peak_match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", output)
    if not mean_match or not peak_match:
        raise RuntimeError(f"volumedetect output missing for {path}")
    return float(mean_match.group(1)), float(peak_match.group(1))


def inspect_edge_tts_mp3(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"audio file is missing or empty: {path}")
    metadata = ffprobe(path)
    mean_db, peak_db = volume_metrics(path)
    valid = (
        metadata["container"] == "mp3"
        and metadata["codec"] == "mp3"
        and metadata["sample_rate"] == 24000
        and metadata["channels"] == 1
        and metadata["duration_seconds"] > 0.2
        and mean_db > -60.0
        and peak_db <= 0.0
    )
    if not valid:
        raise RuntimeError(
            "Edge TTS output validation failed: "
            f"metadata={metadata}, mean_volume_db={mean_db}, peak_volume_db={peak_db}"
        )
    return {**metadata, "mean_volume_db": mean_db, "peak_volume_db": peak_db}


async def _save_edge_tts(
    text: str,
    destination: Path,
    *,
    voice: str,
    rate: str,
    pitch: str,
    volume: str,
    timeout_s: float,
) -> None:
    try:
        import edge_tts
    except ImportError as error:
        raise RuntimeError("edge-tts is required; install lstest/config/requirements.txt") from error
    communication = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch, volume=volume)
    await asyncio.wait_for(communication.save(str(destination)), timeout=max(0.1, float(timeout_s)))


def edge_tts_version() -> str:
    try:
        return importlib.metadata.version("edge-tts")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def synthesize_edge_tts(
    text: str,
    output: Path,
    *,
    case_id: str = "",
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    volume: str = DEFAULT_VOLUME,
    timeout_s: float = 60.0,
    retries: int = 1,
) -> dict[str, Any]:
    """Generate one validated Edge TTS MP3; retry transient synthesis failures once by default."""
    normalized_text = (text or "").strip()
    if not normalized_text:
        raise ValueError("text cannot be empty")
    if output.suffix.lower() != ".mp3":
        raise ValueError("Edge TTS output must use the .mp3 extension")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if retries < 0:
        raise ValueError("retries must be >= 0")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    failure: Exception | None = None
    for attempt in range(1, retries + 2):
        temporary.unlink(missing_ok=True)
        try:
            asyncio.run(
                _save_edge_tts(
                    normalized_text,
                    temporary,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                    volume=volume,
                    timeout_s=timeout_s,
                )
            )
            metadata = inspect_edge_tts_mp3(temporary)
            os.replace(temporary, output)
            return {
                "case_id": case_id,
                "text": normalized_text,
                "audio_path": str(output),
                "provider": "microsoft_edge_tts",
                "model": "not_exposed_by_provider",
                "voice": voice,
                "rate": rate,
                "pitch": pitch,
                "volume": volume,
                **{name: metadata[name] for name in ("container", "codec", "sample_rate", "channels", "duration_seconds")},
                "size_bytes": output.stat().st_size,
                "mean_volume_db": metadata["mean_volume_db"],
                "peak_volume_db": metadata["peak_volume_db"],
                "non_silent": "true",
                "clipping_status": "PASS" if metadata["peak_volume_db"] <= -0.1 else "WARN_NEAR_ZERO_DBFS",
                "sha256": sha256_file(output),
                "generated_at": now_iso(),
                "tool": "edge-tts",
                "tool_version": edge_tts_version(),
                "validation_status": "PASS",
                "error": "",
            }
        except Exception as error:
            failure = error
            temporary.unlink(missing_ok=True)
            if attempt > retries:
                break
    raise RuntimeError(f"Edge TTS synthesis failed after {retries + 1} attempt(s): {failure}") from failure


def safe_audio_filename(value: str, *, fallback_text: str) -> str:
    candidate = Path(value or "").name
    stem = Path(candidate).stem if candidate else ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    if not stem:
        stem = hashlib.sha256(fallback_text.encode("utf-8")).hexdigest()[:16]
    return f"{stem}.mp3"


def load_text_manifest(path: Path) -> list[dict[str, str]]:
    """Load a UTF-8 CSV or JSONL manifest containing at least a text field."""
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif suffix in {".jsonl", ".ndjson"}:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        raise ValueError("text manifest must be .csv, .jsonl, or .ndjson")
    result: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=2):
        if not isinstance(row, Mapping):
            raise ValueError(f"manifest row {index} is not an object")
        text = str(row.get("text") or "").strip()
        if not text:
            raise ValueError(f"manifest row {index} has empty text")
        result.append({
            "case_id": str(row.get("case_id") or f"row-{index - 1}"),
            "text": text,
            "filename": str(row.get("filename") or row.get("audio_filename") or ""),
        })
    if not result:
        raise ValueError("text manifest has no rows")
    return result


def write_audio_manifest(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def synthesize_manifest(
    rows: Iterable[Mapping[str, str]],
    output_dir: Path,
    manifest_output: Path,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    volume: str = DEFAULT_VOLUME,
    timeout_s: float = 60.0,
    retries: int = 1,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for row in rows:
        case_id = str(row.get("case_id") or "")
        text = str(row.get("text") or "").strip()
        target = output_dir / safe_audio_filename(str(row.get("filename") or case_id), fallback_text=text)
        try:
            records.append(
                synthesize_edge_tts(
                    text, target, case_id=case_id, voice=voice, rate=rate, pitch=pitch,
                    volume=volume, timeout_s=timeout_s, retries=retries,
                )
            )
        except Exception as error:
            records.append({
                "case_id": case_id,
                "text": text,
                "audio_path": str(target),
                "provider": "microsoft_edge_tts",
                "voice": voice,
                "rate": rate,
                "pitch": pitch,
                "volume": volume,
                "generated_at": now_iso(),
                "tool": "edge-tts",
                "tool_version": edge_tts_version(),
                "validation_status": "FAIL",
                "error": f"{type(error).__name__}: {error}",
            })
    write_audio_manifest(manifest_output, records)
    failures = [row for row in records if row["validation_status"] != "PASS"]
    return {
        "status": "PASS" if not failures else "FAIL",
        "requested": len(records),
        "generated": len(records) - len(failures),
        "failed": len(failures),
        "manifest": str(manifest_output),
        "failures": [{"case_id": row["case_id"], "error": row["error"]} for row in failures],
    }
