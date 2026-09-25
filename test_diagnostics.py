from __future__ import annotations

import json
import shutil
import subprocess
import sys
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
        payload = json.dumps(
            {
                "status": "ok",
                "events": [{"TimeCreated": "2026-09-25T18:00:00+03:00", "Id": 1000}],
            }
        )
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout=payload, stderr="")
        with mock.patch.object(diag, "is_windows", return_value=True), mock.patch.object(
            diag.subprocess, "run", return_value=completed
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"][0]["Id"], 1000)

    def test_collect_windows_application_events_handles_timeout(self) -> None:
        with mock.patch.object(diag, "is_windows", return_value=True), mock.patch.object(
            diag.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=20),
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "timeout")

    def test_collect_windows_application_events_handles_oserror(self) -> None:
        with mock.patch.object(diag, "is_windows", return_value=True), mock.patch.object(
            diag.subprocess, "run", side_effect=OSError("access denied")
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "error")
        self.assertIn("access denied", result["reason"])

    def test_collect_windows_application_events_nonzero_with_json_is_error(self) -> None:
        payload = json.dumps({"status": "ok", "events": [{"Id": 1000}]})
        completed = subprocess.CompletedProcess(["powershell"], 1, stdout=payload, stderr="")
        with mock.patch.object(diag, "is_windows", return_value=True), mock.patch.object(
            diag.subprocess, "run", return_value=completed
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["returncode"], 1)

    def test_collect_windows_application_events_no_events(self) -> None:
        payload = json.dumps({"status": "no_events", "events": []})
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout=payload, stderr="")
        with mock.patch.object(diag, "is_windows", return_value=True), mock.patch.object(
            diag.subprocess, "run", return_value=completed
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "no_events")
        self.assertEqual(result["events"], [])

    def test_collect_windows_application_events_malformed_json(self) -> None:
        completed = subprocess.CompletedProcess(["powershell"], 0, stdout="not json", stderr="")
        with mock.patch.object(diag, "is_windows", return_value=True), mock.patch.object(
            diag.subprocess, "run", return_value=completed
        ):
            result = diag.collect_windows_application_events()
        self.assertEqual(result["status"], "error")
        self.assertIn("non-JSON", result["reason"])

    def test_write_report_bundle_does_not_overwrite_existing_file(self) -> None:
        report = {
            "created_at_local": "2026-09-25T18:00:00+03:00",
            "privacy_note": "note",
            "scope": [],
            "system": {
                "platform": "p",
                "python_version": "3.11",
                "python_executable": "python",
                "available_ram_mb": 1,
            },
            "configured_photoshop": {
                "config_file": "c",
                "configured_path": None,
                "configured_path_exists": False,
            },
            "disk_free_space": {},
            "logs": {"guard_log": {"text": ""}, "supervisor_log": {"text": ""}},
            "windows_application_events": {
                "status": "no_events",
                "events": [],
                "scan_scope": "scope",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "diagnostics.json").write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                diag.write_report_bundle(report, output_dir)

    def test_build_report_reads_expected_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            app = base / "app"
            data = base / "AppData" / "antilagphotoshop"
            app.mkdir(parents=True)
            data.mkdir(parents=True)
            (app / "photoshop_path.txt").write_text(
                r"E:\Adobe Photoshop 2020\Photoshop.exe", encoding="utf-8"
            )
            (data / "antilagphotoshop.log").write_text("guard tail", encoding="utf-8")
            (data / "supervisor.log").write_text("supervisor tail", encoding="utf-8")
            with mock.patch.object(diag, "app_dir", return_value=app), mock.patch.dict(
                diag.os.environ,
                {"APPDATA": str(base / "AppData"), "TEMP": str(base / "Temp")},
                clear=False,
            ), mock.patch.object(diag, "available_ram_mb", return_value=1234), mock.patch.object(
                diag, "disk_free_bytes", return_value=5 * 1024 * 1024
            ), mock.patch.object(
                diag,
                "collect_windows_application_events",
                return_value={"status": "no_events", "events": [], "scan_scope": "scope"},
            ):
                report = diag.build_report()
        self.assertEqual(
            report["configured_photoshop"]["configured_path"],
            r"E:\Adobe Photoshop 2020\Photoshop.exe",
        )
        self.assertFalse(report["configured_photoshop"]["configured_path_exists"])
        self.assertEqual(report["logs"]["guard_log"]["text"], "guard tail")
        self.assertEqual(report["windows_application_events"]["status"], "no_events")
        self.assertIn("+", report["created_at_local"])

    @unittest.skipUnless(diag.is_windows(), "Windows only")
    def test_collect_diagnostics_bat_preserves_nonzero_exit_code(self) -> None:
        if not (shutil.which("py") or shutil.which("python")):
            self.skipTest("py or python is required")
        with tempfile.TemporaryDirectory(prefix="diag-test-") as temp_dir:
            base = Path(temp_dir) / "wrapper space ! dir"
            base.mkdir()
            shutil.copy(Path(__file__).with_name("collect_diagnostics.bat"), base / "collect_diagnostics.bat")
            (base / "antilagphotoshop_diagnostics.py").write_text(
                "import sys\nraise SystemExit(7)\n", encoding="utf-8"
            )
            result = subprocess.run(
                ["cmd", "/c", str(base / "collect_diagnostics.bat")],
                cwd=str(base),
                input="\n",
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 7, msg=result.stdout + result.stderr)
        self.assertIn("Exit code: 7", result.stdout)

    @unittest.skipUnless(diag.is_windows(), "Windows only")
    def test_collect_diagnostics_bat_preserves_zero_exit_code(self) -> None:
        if not (shutil.which("py") or shutil.which("python")):
            self.skipTest("py or python is required")
        with tempfile.TemporaryDirectory(prefix="diag-test-") as temp_dir:
            base = Path(temp_dir) / "wrapper space ! dir"
            base.mkdir()
            shutil.copy(Path(__file__).with_name("collect_diagnostics.bat"), base / "collect_diagnostics.bat")
            (base / "antilagphotoshop_diagnostics.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            result = subprocess.run(
                ["cmd", "/c", str(base / "collect_diagnostics.bat")],
                cwd=str(base),
                input="\n",
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("Exit code: 0", result.stdout)

    @unittest.skipUnless(diag.is_windows(), "Windows only")
    def test_windows_smoke_actual_diagnostics_script(self) -> None:
        with tempfile.TemporaryDirectory(prefix="diag-smoke-") as temp_dir:
            base = Path(temp_dir) / "smoke space ! dir"
            base.mkdir()
            output_dir = base / "new report"
            script = Path(__file__).with_name("antilagphotoshop_diagnostics.py")
            result = subprocess.run(
                [sys.executable, str(script), "--output", str(output_dir)],
                text=True,
                capture_output=True,
                timeout=35,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertTrue((output_dir / "diagnostics.txt").is_file())
            self.assertTrue((output_dir / "diagnostics.json").is_file())
            report = json.loads((output_dir / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertRegex(report["created_at_local"], r".*[+-]\d{2}:\d{2}$")
        self.assertIn("No network or upload", report["scope"])
        self.assertIn("Diagnostics created locally.", result.stdout)


if __name__ == "__main__":
    unittest.main()
