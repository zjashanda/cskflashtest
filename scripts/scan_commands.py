import csv
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import serial

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = Path(__file__).resolve().parents[1]
WAV_DIR = ROOT / "wav" if (ROOT / "wav").is_dir() else SKILL_ROOT / "wav"
WORK = ROOT / "logs" / "command_scan"
NORM = WORK / "norm"
RAW = WORK / "raw"
NORM.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)
RESULTS = WORK / "results.csv"
SUMMARY = WORK / "summary.txt"
KEY = "VID_8765&PID_5678:9_2A847557_0_0000"
WAKE = str(WAV_DIR / "Wake" / "hellohaier.wav")
PLAY = ROOT / "lstest" / "scripts" / "listenai_play.py"
if not PLAY.exists():
    PLAY = SKILL_ROOT / "lstest" / "scripts" / "listenai_play.py"


def audio_files(folder):
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".wav", ".m4a", ".mp3")
    )


def select_items():
    items = []
    for folder in sorted(WAV_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith("_") or folder.name == "Wake":
            continue
        files = audio_files(folder)
        for src in files[:2]:
            items.append((folder.name, src))
    return items


def normalize(src, dst):
    if dst.exists():
        return dst
    tmp = NORM / ("_tmp_" + dst.name)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp)],
        check=True,
    )
    with wave.open(str(tmp), "rb") as reader:
        params = reader.getparams()
        data = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16).astype(np.float32)
    peak = float(np.abs(data).max()) or 1.0
    out = (data * (32000.0 / peak)).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(dst), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(out.tobytes())
    tmp.unlink(missing_ok=True)
    return dst


class Device:
    def __init__(self):
        self.ctl = serial.Serial("COM14", 115200, timeout=0.1)
        self.csk = serial.Serial("COM13", 115200, timeout=0.1)
        time.sleep(0.3)
        self.ctl.reset_input_buffer()
        self.csk.reset_input_buffer()

    def ctl_send(self, command, wait=0.5):
        self.ctl.write((command + "\r").encode())
        end = time.time() + wait
        out = b""
        while time.time() < end:
            data = self.ctl.read(4096)
            if data:
                out += data
        return out.decode("utf-8", "replace")

    def csk_send(self, command, wait=0.5):
        self.csk.write((command + "\r").encode())
        end = time.time() + wait
        out = b""
        while time.time() < end:
            data = self.csk.read(8192)
            if data:
                out += data
        return out.decode("utf-8", "replace")

    def apply_pa(self):
        self.ctl_send("pa-enable.set 1 2 0 1", 0.4)
        self.ctl_send("pa-enable.set 0 17 0 1", 0.4)

    def set_volume(self, value=100):
        self.csk_send(f"player.vol.set {value}", 0.5)

    def play(self, path, seconds):
        proc = subprocess.Popen(
            [sys.executable, "-u", str(PLAY), "play", "--device-key", KEY, "--audio-file", str(path)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        ctl_buf = b""
        csk_buf = b""
        start = time.time()
        while time.time() - start < seconds:
            data = self.ctl.read(4096)
            if data:
                ctl_buf += data
            data = self.csk.read(8192)
            if data:
                csk_buf += data
            if proc.poll() is not None and time.time() - start > 1:
                break
            time.sleep(0.02)
        try:
            out = proc.stdout.read().decode("gbk", "replace") if proc.stdout is not None else ""
        except Exception:
            out = ""
        time.sleep(0.3)
        ctl_buf += self.ctl.read(8192) or b""
        csk_buf += self.csk.read(8192) or b""
        return ctl_buf.decode("utf-8", "replace"), csk_buf.decode("utf-8", "replace"), out

    def wait_ap(self, timeout=30):
        self.csk.reset_input_buffer()
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            data = self.csk.read(8192)
            if data:
                buf += data
                if b"AP Boot Ready" in buf:
                    break
            time.sleep(0.02)
        return buf.decode("utf-8", "replace")

    def power_cycle(self):
        self.ctl_send("uut-switch1.off", 0.5)
        self.ctl_send("uut-csk-boot.off", 0.5)
        time.sleep(0.5)
        self.ctl_send("uut-switch1.on", 0.5)
        text = self.wait_ap(30)
        self.apply_pa()
        self.set_volume(100)
        return text


def norm_text(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def parse(text):
    keywords = re.findall(r"keyword:([^\"\r\n]+)", text)
    keyword = keywords[-1].strip() if keywords else ""
    kid = re.findall(r"vir_kid:\s*(\d+)", text)
    volumes = re.findall(r"refresh volume from\s+(\d+)\s+to\s+(\d+)", text)
    before, after = (int(volumes[-1][0]), int(volumes[-1][1])) if volumes else (None, None)
    crash = "Boot Reason: CRASH" in text or "CSK AP Ready after exception" in text
    crash_code = ""
    match = re.search(r"Boot Reason: CRASH\s+([^\r\n]+)", text)
    if match:
        crash_code = match.group(1).strip()
    response_lines = [
        line.strip() for line in text.splitlines()
        if re.search(r"_volume_adjust|refresh volume|miniplayer status|TONE_LANG|toneid", line, re.I)
    ]
    return {
        "keyword": keyword,
        "vir_kid": int(kid[-1]) if kid else None,
        "before": before,
        "after": after,
        "crash": crash,
        "crash_code": crash_code,
        "response": response_lines[-1][:200] if response_lines else "",
    }


def main():
    items = select_items()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if limit:
        items = items[:limit]
    print(f"items={len(items)}", flush=True)
    fields = [
        "folder", "file", "wake_ok", "wake_retries", "keyword", "intent_match",
        "vir_kid", "volume_before", "volume_after", "crash", "crash_code", "response",
    ]
    exists = RESULTS.exists()
    with RESULTS.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        device = Device()
        try:
            device.apply_pa()
            device.set_volume(100)
            for index, (intent, src) in enumerate(items, 1):
                norm_path = normalize(src, NORM / f"{intent}__{src.stem}.wav")
                device.set_volume(100)
                device.apply_pa()
                device.ctl.reset_input_buffer()
                device.csk.reset_input_buffer()
                wake_ok = False
                wake_retries = 0
                for attempt in range(1, 4):
                    wake_retries = attempt
                    _, wake_text, _ = device.play(WAKE, 6)
                    if "start local wakeup" in wake_text:
                        wake_ok = True
                        break
                    time.sleep(1.5)
                if not wake_ok:
                    device.power_cycle()
                    _, wake_text, _ = device.play(WAKE, 6)
                    wake_ok = "start local wakeup" in wake_text
                time.sleep(0.5)
                _, cmd_text, play_out = device.play(norm_path, 10)
                info = parse(cmd_text)
                if info["crash"]:
                    boot = device.wait_ap(30)
                    info["response"] = (info["response"] + " | " + ("reboot CRASH" if "AP Boot Ready" in boot else "no boot")).strip(" |")
                    device.apply_pa()
                    device.set_volume(100)
                (RAW / f"{intent}__{src.stem}.log").write_text(cmd_text, encoding="utf-8")
                row = {
                    "folder": intent,
                    "file": src.name,
                    "wake_ok": wake_ok,
                    "wake_retries": wake_retries,
                    "keyword": info["keyword"],
                    "intent_match": bool(info["keyword"]) and (
                        norm_text(intent) in norm_text(info["keyword"]) or norm_text(info["keyword"]) in norm_text(intent)
                    ),
                    "vir_kid": info["vir_kid"],
                    "volume_before": info["before"],
                    "volume_after": info["after"],
                    "crash": info["crash"],
                    "crash_code": info["crash_code"],
                    "response": info["response"],
                }
                writer.writerow(row)
                handle.flush()
                print(
                    f"[{index}/{len(items)}] {intent} {src.name} "
                    f"wake={wake_ok} kw={info['keyword']} crash={info['crash']}",
                    flush=True,
                )
                time.sleep(6 if wake_ok else 2)
        finally:
            device.ctl.close()
            device.csk.close()

    rows = list(csv.DictReader(RESULTS.open(encoding="utf-8-sig")))
    crashes = [row for row in rows if str(row.get("crash")).lower() == "true"]
    stable = [row for row in rows if str(row.get("wake_ok")).lower() == "true" and row.get("keyword") and str(row.get("crash")).lower() != "true"]
    with SUMMARY.open("w", encoding="utf-8") as handle:
        handle.write(f"total={len(rows)} wake_ok={sum(1 for r in rows if str(r.get('wake_ok')).lower()=='true')} asr_ok={sum(1 for r in rows if r.get('keyword'))} stable={len(stable)} crashes={len(crashes)}\n")
        for row in crashes:
            handle.write(f"CRASH {row['folder']} {row['file']} {row['crash_code']}\n")
        for row in stable:
            handle.write(f"STABLE {row['folder']} {row['file']} {row['keyword']}\n")
    print(f"done results={RESULTS} summary={SUMMARY}", flush=True)


if __name__ == "__main__":
    main()
