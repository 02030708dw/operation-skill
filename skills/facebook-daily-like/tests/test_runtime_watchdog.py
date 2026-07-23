import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "facebook_daily_like.py"
)
SPEC = importlib.util.spec_from_file_location("facebook_daily_like_runtime", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RuntimeWatchdogTests(unittest.TestCase):
    def test_default_disables_hard_runtime_and_enables_stall_watchdog(self):
        args = MODULE.build_parser().parse_args([])
        self.assertEqual(args.max_runtime, 0)
        self.assertEqual(float(args.stall_timeout), 120)

    def test_progress_refreshes_inactivity_deadline(self):
        with patch.object(MODULE.time, "monotonic", return_value=100.0):
            client = MODULE.MytClient(
                "127.0.0.1",
                10005,
                15,
                stall_timeout=120,
            )
            client.mark_progress("like tap verified")
        self.assertEqual(client.stall_deadline, 220.0)
        self.assertEqual(client.last_progress, "like tap verified")

    def test_inactivity_timeout_reports_last_progress(self):
        with patch.object(MODULE.time, "monotonic", return_value=100.0):
            client = MODULE.MytClient(
                "127.0.0.1",
                10005,
                15,
                stall_timeout=10,
            )
            client.mark_progress("Facebook screen prepared")
        with patch.object(MODULE.time, "monotonic", return_value=111.0):
            with self.assertRaisesRegex(
                MODULE.RuntimeLimitError,
                "inactivity timeout.*last progress=Facebook screen prepared",
            ):
                client.ensure_time("like search")

    def test_optional_hard_deadline_remains_available(self):
        with patch.object(MODULE.time, "monotonic", return_value=100.0):
            client = MODULE.MytClient(
                "127.0.0.1",
                10005,
                15,
                deadline=105.0,
                stall_timeout=120,
            )
        with patch.object(MODULE.time, "monotonic", return_value=106.0):
            with self.assertRaisesRegex(
                MODULE.RuntimeLimitError,
                "optional maximum runtime",
            ):
                client.ensure_time("like search")


if __name__ == "__main__":
    unittest.main()
