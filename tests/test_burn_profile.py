import hashlib
import json
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from lstest.scripts.profile import DeviceProfile, ProfileFixtureRunner, RawLogRecord  # noqa: E402

import run  # noqa: E402


@pytest.fixture(scope="module")
def profile():
    return DeviceProfile.load(SKILL_ROOT / "config" / "burn-profile.json")


@pytest.fixture(scope="module")
def manifest():
    return json.loads((SKILL_ROOT / "config" / "firmware-manifest.json").read_text(encoding="utf-8"))


def test_profile_fixtures(profile):
    results = ProfileFixtureRunner(profile).run()
    assert results
    assert all(item.passed for item in results), [item for item in results if not item.passed]
    profile.assert_formal_ready()


def test_control_ack_samples(profile):
    samples = {
        "switch_idx1_set: 0": ("control-switch-off", "0"),
        "switch_idx1_set: 1": ("control-switch-on", "1"),
        "csk6012_boot_set: 0": ("control-boot-off", "0"),
        "csk6012_boot_set: 1": ("control-boot-on", "1"),
    }
    for text, (rule_id, value) in samples.items():
        facts = profile.extract_facts(RawLogRecord(text=text, source="control", port="COM14", role="control"))
        assert any(fact.rule_id == rule_id and fact.presentation_value == value for fact in facts)


def test_burn_and_boot_samples(profile):
    samples = [
        ("CONNECT WITH CHIP SUCCESS", "burn-connect", "CONNECT WITH CHIP SUCCESS"),
        ("CHIP TYPE = 0x60120001", "burn-chip", "0x60120001"),
        ("WIRITE FLASH OFFSET ADDR:0x0 WRITE FILE SIZE: 16777216", "burn-write-size", "16777216"),
        ("WIRITE FLASH OFFSET ADDR:0x0 WRITE SIZE: 5685248", "burn-write-size", "5685248"),
        ("MD5 CHECK SUCCESS MD5_VALUE = 0x2dc44aa1d8f3c2e0dd318627a9a2fb98", "burn-md5", "0x2dc44aa1d8f3c2e0dd318627a9a2fb98"),
        ("FLASH DOWNLOAD SUCCESS", "burn-flash-img", "FLASH DOWNLOAD SUCCESS"),
        ("FLASH 0x0 SECTOR DOWNLOAD SUCCESS", "burn-flash-hex", "0x0"),
    ]
    for text, rule_id, value in samples:
        facts = profile.extract_facts(RawLogRecord(text=text, source="burn_tool", port="COM15", role="burn"))
        assert any(fact.rule_id == rule_id and fact.presentation_value == value for fact in facts), text

    boot_samples = [
        (">>>> bootloader application >>>>", "csk-bootloader", "bootloader application"),
        ("venus ap appliction(V4.99.8@Oct 20 2026 14:42:43)", "csk-version", "V4.99.8@Oct 20 2026 14:42:43"),
        ("Project Build Info: 2026-10-20 14:42:30_1ac783d", "csk-build", "2026-10-20 14:42:30_1ac783d"),
        ("Boot Reason: POWER|HWPIN 0x03", "csk-boot-reason", "POWER|HWPIN 0x03"),
        ("[A/W:181:BOOT] AP Boot Ready !!!", "csk-ready", "AP Boot Ready !!!"),
        ("flash set log_lev:4 success", "csk-loglevel-ok", "4"),
    ]
    for text, rule_id, value in boot_samples:
        facts = profile.extract_facts(RawLogRecord(text=text, source="csk", port="COM13", role="csk"))
        assert any(fact.rule_id == rule_id and fact.presentation_value == value for fact in facts), text


def test_profile_matches_any_device_port(profile):
    facts = profile.extract_facts(RawLogRecord(
        text="venus ap appliction(V4.99.8@Oct 20 2026 14:42:43)",
        source="csk", port="COM19", role="csk",
    ))
    assert any(fact.rule_id == "csk-version" for fact in facts)

    facts = profile.extract_facts(RawLogRecord(
        text="switch_idx1_set: 0", source="control", port="COM23", role="control",
    ))
    assert any(fact.rule_id == "control-switch-off" for fact in facts)


def test_csk_init_failure_sample(profile):
    facts = profile.extract_facts(RawLogRecord(
        text="flash write fail -1, addr 0x18ffe38d, size: 1",
        source="csk", port="COM13", role="csk",
    ))
    assert any(fact.rule_id == "csk-loglevel-fail" for fact in facts)


def test_manifest_matches_current_firmware(manifest):
    img = PROJECT_ROOT / manifest["firmware"]["img"]["path"]
    hex_file = PROJECT_ROOT / manifest["firmware"]["hex"]["path"]
    assert img.is_file()
    assert hex_file.is_file()

    img_sha = run.sha256_file(img)
    hex_sha = run.sha256_file(hex_file)
    assert img_sha == manifest["firmware"]["img"]["source_sha256"]
    assert hex_sha == manifest["firmware"]["hex"]["source_sha256"]

    assert run.md5_file(img) == manifest["firmware"]["img"]["expected_tool_md5"]
    assert run.hex_write_size(hex_file) == manifest["firmware"]["hex"]["expected_write_size"] == 5685248
    assert manifest["csk_init"]["command"] == "flash.setloglev 4"
    assert manifest["csk_init"]["success_pattern"] == "flash set log_lev:4 success"
    assert manifest["csk_init"]["retries"] == 5


def test_hex_maps_to_img(manifest):
    img = (PROJECT_ROOT / manifest["firmware"]["img"]["path"]).read_bytes()
    hex_file = PROJECT_ROOT / manifest["firmware"]["hex"]["path"]
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    base = 0
    for raw in hex_file.read_text(encoding="ascii").splitlines():
        if not raw.startswith(":"):
            continue
        record = bytes.fromhex(raw[1:])
        count = record[0]
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        payload = record[4:4 + count]
        if record_type == 0x04:
            base = ((payload[0] << 8) | payload[1]) << 16
        elif record_type == 0x00:
            absolute = base + address
            offset = absolute - 0x18000000 if absolute >= 0x18000000 else absolute
            image[offset:offset + count] = payload
    assert bytes(image) == img
    assert hashlib.md5(bytes(image)).hexdigest() == manifest["firmware"]["img"]["expected_tool_md5"]
