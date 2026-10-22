import json
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import run_multi  # noqa: E402


def test_current_devices_config():
    devices = run_multi.load_devices(SKILL_ROOT / "config" / "devices.json")
    assert len(devices) == 8
    by_id = {item["id"]: item for item in devices}
    assert by_id["dev01"]["control"] == "COM27"
    assert by_id["dev01"]["csk"] == "COM26"
    assert by_id["dev01"]["burn"] == "COM28"
    assert by_id["dev02"]["control"] == "COM23"
    assert by_id["dev03"]["control"] == "COM54"
    assert by_id["dev04"]["control"] == "COM38"
    assert by_id["dev05"]["control"] == "COM11"
    assert by_id["dev06"]["control"] == "COM7"
    assert by_id["dev07"]["control"] == "COM44"
    assert by_id["dev08"]["control"] == "COM14"


def test_ten_device_example_config():
    devices = run_multi.load_devices(SKILL_ROOT / "config" / "devices.example.json")
    assert len(devices) == 10
    ports = [str(item[role]).upper() for item in devices for role in ("control", "csk", "burn", "hisi")]
    assert len(ports) == len(set(ports)) == 40


def test_duplicate_port_rejected(tmp_path):
    config = tmp_path / "devices.json"
    config.write_text(json.dumps({"devices": [
        {"id": "dev01", "control": "COM1", "csk": "COM2", "burn": "COM3", "enabled": True},
        {"id": "dev02", "control": "COM1", "csk": "COM5", "burn": "COM6", "enabled": True},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="used by both"):
        run_multi.load_devices(config)


def test_more_than_ten_rejected(tmp_path):
    devices = [
        {"id": f"dev{index:02d}", "control": f"COM{index}1", "csk": f"COM{index}2", "burn": f"COM{index}3", "enabled": True}
        for index in range(1, 12)
    ]
    config = tmp_path / "devices.json"
    config.write_text(json.dumps({"devices": devices}), encoding="utf-8")
    with pytest.raises(ValueError, match="too many"):
        run_multi.load_devices(config)
