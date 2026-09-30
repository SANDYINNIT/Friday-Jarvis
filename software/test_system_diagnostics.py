"""Tests for the system diagnostics helpers (headless, no mutation)."""

import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server import system_diagnostics as diag


class SystemDiagnosticsTests(unittest.TestCase):
    def test_host_info_has_identity_fields(self):
        info = diag.host_info()
        for key in ("computer_name", "os", "architecture", "python", "collected_at"):
            self.assertIn(key, info)
        self.assertTrue(info["python"].startswith("3."))

    def test_cpu_snapshot_shape(self):
        cpu = diag.cpu_snapshot(interval=0.05)
        self.assertTrue(cpu["ok"], cpu)
        self.assertIsInstance(cpu["percent"], float)
        self.assertIsInstance(cpu["per_core_percent"], list)

    def test_memory_snapshot_shape(self):
        mem = diag.memory_snapshot()
        self.assertTrue(mem["ok"], mem)
        self.assertGreater(mem["total_gb"], 0)
        self.assertLessEqual(mem["used_gb"], mem["total_gb"])

    def test_disk_snapshot_reports_free_space(self):
        disk = diag.disk_snapshot(paths=["C:\\"])
        self.assertTrue(disk["ok"], disk)
        volume = disk["volumes"][0]
        self.assertTrue(volume["ok"])
        self.assertIn("free_gb", volume)

    def test_top_processes_sorted_and_bounded(self):
        top = diag.top_processes(limit=5)
        self.assertTrue(top["ok"], top)
        self.assertLessEqual(len(top["processes"]), 5)
        values = [p["memory_mb"] for p in top["processes"]]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_top_processes_clamps_limit(self):
        # A silly limit must be clamped, never explode.
        top = diag.top_processes(limit=100000)
        self.assertTrue(top["ok"])
        self.assertLessEqual(len(top["processes"]), diag.MAX_TOP_N)

    def test_service_status_rejects_blank_name(self):
        result = diag.service_status("")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_health_report_shape(self):
        report = diag.health_report()
        self.assertTrue(report["ok"])
        self.assertIn(report["verdict"], ("ok", "attention"))
        self.assertIsInstance(report["findings"], list)
        self.assertIn("checks", report)

    def test_system_health_is_readable_text(self):
        text = diag.system_health()
        self.assertIn("FRIDAY health check", text)
        self.assertIn("CPU:", text)
        self.assertIn("RAM:", text)

    def test_system_health_brief_is_shorter(self):
        self.assertLess(len(diag.system_health(brief=True)), len(diag.system_health()))

    def test_network_snapshot_never_raises(self):
        net = diag.network_snapshot()
        self.assertTrue(net["ok"])
        self.assertIn("online", net)
        self.assertIsInstance(net["checks"], list)

    def test_diagnose_narrowed_areas(self):
        result = diag.diagnose(areas=["health"])
        self.assertTrue(result["ok"])
        self.assertIn("health", result)
        self.assertNotIn("services", result)


if __name__ == "__main__":
    unittest.main()
    print("PASS: system diagnostics")
