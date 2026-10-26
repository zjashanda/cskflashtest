from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.burn_adapter import SerialCollector


class SerialFactStoreTests(unittest.TestCase):
    def test_log_only_fact_store_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = SerialCollector(
                "COM_TEST", 115200, "csk", "csk", Path(directory) / "serial.log",
            )
            collector.open_log_only()
            try:
                for index in range(50000):
                    collector.inject(f"line {index}")
                events, found = collector.wait_for(
                    0, lambda text: "line 49999" in text, 5.0,
                )
                self.assertTrue(found)
                self.assertEqual(collector.snapshot(), 50000)
                self.assertLessEqual(len(collector.events_since(0)), 20000)
                self.assertIn("line 49999", collector.text_since(49999))
                self.assertGreater(len(events), 0)
            finally:
                collector.stop()


if __name__ == "__main__":
    unittest.main()
