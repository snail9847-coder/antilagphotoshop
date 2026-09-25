from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import antilagphotoshop_diagnostics as diag


class DiagnosticsTests(unittest.TestCase):
    def test_prepare_output_dir_rejects_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "existing"
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                diag.prepare_output_dir(str(existing))

    def test_read_text_tail_reads_only_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "guard.log"
            path.write_text("A" * 200 + "TAIL", encoding="utf-8")
            result = diag.read_text_tail(path, max_bytes=16)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["truncated"])
            self.assertIn("TAIL", result["text"])
            self.assertLessEqual(result["bytes_read"], 16)

    def test_collect_windows_application_events_parses_json(self) -> None:
        payload = json.dumps([
            {
                "TimeCreated": "2026-09-25T18:00:00",
                "Id": 1000,
                "ProviderName": "Application Error",
                "LevelDisplayName": "Error",
                "Message": "Photoshop.exe crashed",
            }
        ])
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout=payload, stderr="")
        with mock.patch.object(diag.os, "name", "nt"), mock.patch.object(diag.subprocess, "run", return_value=completed):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"][0]["Id"], 1000)

    def test_collect_windows_application_events_handles_timeout(self) -> None:
        with mock.patch.object(diag.os, "name", "nt"), mock.patch.object(
            diag.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=20),
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "timeout")

    def test_build_report_reads_expected_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            app = base / "app"
            data = base / "AppData" / "antilagphotoshop"
            app.mkdir(parents=True)
            data.mkdir(parents=True)
            (app / "photoshop_path.txt").write_text(r"E:\Adobe Photoshop 2020\Photoshop.exe", encoding="utf-8")
            (data / "antilagphotoshop.log").write_text("guard tail", encoding="utf-8")
            (data / "supervisor.log").write_text("supervisor tail", encoding="utf-8")
            with mock.patch.object(diag, "app_dir", return_value=app), mock.patch.dict(diag.os.environ, {"APPDATA": str(base / "AppData"), "TEMP": str(base / "Temp")}, clear=False), mock.patch.object(diag, "available_ram_mb", return_value=1234), mock.patch.object(diag, "disk_free_bytes", return_value=5 * 1024 * 1024), mock.patch.object(diag, "collect_windows_application_events", return_value={"status": "no_events", "events": []}):
                report = diag.build_report()
        self.assertEqual(report["configured_photoshop"]["configured_path"], r"E:\Adobe Photoshop 2020\Photoshop.exe")
        self.assertFalse(report["configured_photoshop"]["configured_path_exists"])
        self.assertEqual(report["logs"]["guard_log"]["text"], "guard tail")
        self.assertEqual(report["windows_application_events"]["status"], "no_events")


if __name__ == "__main__":
    unittest.main()
