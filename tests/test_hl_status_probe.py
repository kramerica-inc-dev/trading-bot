"""Tests for the out-of-band HL statuspage probe (Layer 4). Pure build() logic —
no network (the live fetch is exercised by the deploy smoke-check)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hl_status_probe as P


class TestStatusProbeBuild(unittest.TestCase):
    NOW = "2026-06-29T00:00:00+00:00"

    def test_healthy_summary(self):
        out = P.build({"status": {"indicator": "none", "description": "All Systems Operational"},
                       "scheduled_maintenances": [], "incidents": []}, self.NOW)
        self.assertFalse(out["maintenance_active"])
        self.assertEqual(out["active_maintenance"], [])
        self.assertEqual(out["upcoming_maintenance"], [])
        self.assertEqual(out["open_incidents"], 0)
        self.assertEqual(out["indicator"], "none")
        self.assertEqual(out["ts"], self.NOW)

    def test_active_and_upcoming_maintenance_split(self):
        summ = {"status": {"indicator": "maintenance", "description": "Network upgrade in progress"},
                "scheduled_maintenances": [
                    {"name": "Network upgrade", "status": "in_progress",
                     "scheduled_for": "2026-06-29T00:00:00Z", "scheduled_until": "2026-06-29T00:10:00Z"},
                    {"name": "Future upgrade", "status": "scheduled",
                     "scheduled_for": "2026-07-01T00:00:00Z", "scheduled_until": "2026-07-01T00:10:00Z"},
                    {"name": "Old one", "status": "completed"}],
                "incidents": [{"id": "abc"}]}
        out = P.build(summ, self.NOW)
        self.assertTrue(out["maintenance_active"])
        self.assertEqual([m["name"] for m in out["active_maintenance"]], ["Network upgrade"])
        self.assertEqual([m["name"] for m in out["upcoming_maintenance"]], ["Future upgrade"])
        self.assertEqual(out["open_incidents"], 1)

    def test_missing_keys_default_safely(self):
        out = P.build({}, self.NOW)
        self.assertFalse(out["maintenance_active"])
        self.assertIsNone(out["indicator"])
        self.assertEqual(out["open_incidents"], 0)


if __name__ == "__main__":
    unittest.main()
