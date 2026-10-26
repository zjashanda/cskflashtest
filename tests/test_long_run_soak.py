from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from lstest.scripts.timeline import TimelineLedger, ToolLogValidator
from scripts.burn_adapter import MAX_PARSER_BUFFER, SerialCollector


class LongRunSoakTests(unittest.TestCase):
    def test_100k_facts_stay_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = SerialCollector(
                "COM_TEST", 115200, "csk", "csk", Path(directory) / "serial.log",
            )
            collector.open_log_only()
            try:
                for index in range(100000):
                    collector.inject(f"line {index}")
                events, found = collector.wait_for(
                    0, lambda text: "line 99999" in text, 10.0,
                )
                self.assertTrue(found)
                self.assertEqual(collector.snapshot(), 100000)
                self.assertLessEqual(len(collector.events_since(0)), 20000)
                self.assertIn("line 99999", collector.text_since(99999))
                self.assertGreater(len(events), 0)
            finally:
                collector.stop()

    def test_parser_buffer_is_capped(self):
        errors: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            collector = SerialCollector(
                "COM_TEST", 115200, "csk", "csk", Path(directory) / "serial.log",
                on_error=lambda code, message: errors.append(code),
            )
            collector.open_log_only()
            try:
                with collector.log_path.open("ab") as handle:
                    handle.write(b"x" * (MAX_PARSER_BUFFER + 1024))
                    handle.flush()
                deadline = time.monotonic() + 5.0
                while not errors and time.monotonic() < deadline:
                    time.sleep(0.05)
                with collector.log_path.open("ab") as handle:
                    handle.write(b"\n")
                    handle.flush()
                collector.inject("after-oversized-line")
                events, found = collector.wait_for(
                    0, lambda text: "after-oversized-line" in text, 5.0,
                )
                self.assertTrue(found)
                self.assertIn("SERIAL_LINE_OVERSIZED", errors)
                self.assertGreater(len(events), 0)
            finally:
                collector.stop()

    def test_10k_timeline_cases_are_released(self):
        ledger = TimelineLedger(lambda _: None, lambda: "2026-01-01 00:00:00.000")
        for index in range(10000):
            case_id = f"case_{index:05d}"
            ledger.start_case(
                case_id, epoch=0, position=index + 1, total=10000,
                scenario="powercycle", text="img",
            )
            ledger.close_case(case_id, status="PASS")
        self.assertEqual(len(ledger._cases), 0)
        self.assertEqual(len(ledger._closed_cases), 10000)

    def test_tool_log_validator_streams_large_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tool.log"
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for _ in range(20000):
                    handle.write("2026-01-01 00:00:00.000 [SYSTEM] ok\n")
            errors = ToolLogValidator().validate_path(path)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
