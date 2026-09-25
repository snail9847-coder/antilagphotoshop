"""Regression checks: Photoshop execution/termination is replaced only at the OS boundary."""
import ctypes
import logging
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import antilagphotoshop as app
from test_antilagphotoshop import supervisor


class RegressionTests(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger('regression')
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.log_patch = patch.object(app, 'configure_logging', return_value=logger)
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.guard = app.AntilagPhotoshop(auto_start=False)

    def test_failed_launch_counts_towards_circuit_breaker(self):
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', return_value=None), \
             patch.object(app.subprocess, 'Popen', side_effect=OSError('Denied')) as launch:
            base = app.time.monotonic()
            with patch.object(app.time, "monotonic", return_value=base) as clock:
                for attempt in range(10):
                    clock.return_value = base + attempt * 61
                    self.guard.launch_photoshop()
        self.assertEqual(launch.call_count, app.MAX_ATTEMPTS)
        self.assertEqual(self.guard.policy.attempts(), app.MAX_ATTEMPTS)
        self.assertTrue(self.guard.paused)

    def test_missing_path_is_bounded(self):
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=None):
            base = app.time.monotonic()
            with patch.object(app.time, "monotonic", return_value=base) as clock:
                for attempt in range(10):
                    clock.return_value = base + attempt * 61
                    self.guard.launch_photoshop()
        self.assertEqual(self.guard.policy.attempts(), app.MAX_ATTEMPTS)
        self.assertTrue(self.guard.paused)

    def test_failed_tasklist_means_unknown_not_stopped(self):
        result = subprocess.CompletedProcess([], 1, '', 'Access denied')
        with patch.object(app.subprocess, 'run', return_value=result):
            self.assertIsNone(self.guard.is_photoshop_running())

    def test_timeout_means_unknown_not_stopped(self):
        with patch.object(app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tasklist', 4)):
            self.assertIsNone(self.guard.is_photoshop_running())

    def test_invalid_config_encoding_does_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'photoshop_path.txt'
            config.write_bytes(b'\xff\xfe\x80')
            with patch.object(app, 'CONFIG_PATH_FILE', config), patch.object(app, 'photoshop_candidates', return_value=[]):
                self.assertIsNone(app.find_photoshop())

    def test_nonfinite_heartbeat_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            heartbeat = Path(folder) / 'heartbeat'
            with patch.object(supervisor, 'HEARTBEAT', heartbeat):
                for value in ['nan', 'inf', '-inf', '-1', '10000']:
                    heartbeat.write_text(value, encoding='ascii')
                    self.assertEqual(supervisor.heartbeat_age(now=100), float('inf'), value)

    @unittest.skipUnless(app.os.name == 'nt', 'Windows API signatures')
    def test_handle_apis_are_pointer_sized(self):
        # __init__ must prepare signatures before any HWND, HICON or mutex call.
        self.assertEqual(ctypes.windll.user32.LoadImageW.restype, ctypes.wintypes.HANDLE)
        self.assertEqual(ctypes.windll.user32.CreatePopupMenu.restype, ctypes.wintypes.HMENU)
        self.assertEqual(ctypes.windll.kernel32.CreateMutexW.restype, ctypes.wintypes.HANDLE)

    def test_unknown_process_state_never_launches(self):
        result = subprocess.CompletedProcess([], 1, '', 'Access denied')
        with patch.object(app.subprocess, 'run', return_value=result), \
             patch.object(app.subprocess, 'Popen') as launch:
            self.assertFalse(self.guard.launch_photoshop(manual=True))
        launch.assert_not_called()
        self.assertEqual(self.guard.policy.attempts(), 0)

    def test_process_csv_matches_exact_executable(self):
        for output, expected in [
            ('"Photoshop.exe","123","Console","1","12,000 K"', True),
            ('"NotPhotoshop.exe","123","Console","1","12,000 K"', False),
            ('INFO: No tasks are running which match the specified criteria.', False),
        ]:
            with self.subTest(output=output), patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                self.assertIs(self.guard.is_photoshop_running(), expected)

    def test_utf8_bom_config_and_explicit_path_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            saved_exe = Path(folder) / 'Photoshop.exe'
            saved_exe.touch()
            explicit_exe = Path(folder) / 'other.exe'
            explicit_exe.touch()
            config = Path(folder) / 'photoshop_path.txt'
            config.write_text(str(saved_exe), encoding='utf-8-sig')
            with patch.object(app, 'CONFIG_PATH_FILE', config):
                self.assertEqual(app.find_photoshop(), saved_exe)
                self.assertEqual(app.find_photoshop(str(explicit_exe)), explicit_exe)
                self.assertIsNone(app.find_photoshop(str(Path(folder) / 'missing.exe')))

    def test_concurrent_launches_create_only_one_process(self):
        entered, release = threading.Event(), threading.Event()
        child = Mock()
        child.poll.return_value = None
        def spawn(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Test launch was not released')
            return child
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', return_value=None), \
             patch.object(self.guard, '_window_after_launch'), \
             patch.object(app.subprocess, 'Popen', side_effect=spawn) as launch:
            results = []
            worker = threading.Thread(target=lambda: results.append(self.guard.launch_photoshop()))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(self.guard.launch_photoshop(manual=True))
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, [True])
            # tasklist may lag behind process creation; the live Popen is enough.
            self.assertTrue(self.guard.launch_photoshop(manual=True))
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(self.guard.policy.attempts(), 1)

    def test_manual_retry_can_reset_limit_but_exit_blocks_launch(self):
        for _ in range(app.MAX_ATTEMPTS):
            self.guard.policy.record_attempt()
        self.guard.paused = True
        child = Mock()
        child.poll.return_value = None
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', return_value=None), \
             patch.object(self.guard, '_window_after_launch'), \
             patch.object(app.subprocess, 'Popen', return_value=child) as launch:
            self.assertFalse(self.guard.launch_photoshop())
            self.assertTrue(self.guard.launch_photoshop(manual=True))
            self.assertEqual(self.guard.policy.attempts(), 1)
            self.guard.stop_event.set()
            self.assertFalse(self.guard.launch_photoshop(manual=True))
            self.assertEqual(launch.call_count, 1)

    @unittest.skipUnless(app.os.name == 'nt', 'Windows close request')
    def test_graceful_close_pauses_and_targets_main_window_only(self):
        hwnd = 0x100000001
        with patch.object(self.guard, '_main_window', return_value=hwnd), \
             patch.object(ctypes.windll.user32, 'PostMessageW', return_value=1) as send:
            self.assertEqual(self.guard.close_photoshop_graceful(), 1)
            send.assert_called_once_with(hwnd, 0x0010, 0, 0)
        self.assertTrue(self.guard.paused)
        with patch.object(app.subprocess, 'Popen') as launch:
            self.assertFalse(self.guard.launch_photoshop())
        launch.assert_not_called()

    @unittest.skipUnless(app.os.name == 'nt', 'Windows close request')
    def test_failed_close_restores_previous_pause_state(self):
        with patch.object(self.guard, '_main_window', return_value=123), \
             patch.object(ctypes.windll.user32, 'PostMessageW', return_value=0):
            self.assertEqual(self.guard.close_photoshop_graceful(), 0)
        self.assertFalse(self.guard.paused)

    def test_force_close_is_explicit_and_pauses_relaunch(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(app.subprocess, 'run', return_value=result) as terminate:
            self.assertTrue(self.guard.close_photoshop_forced())
        self.assertEqual(terminate.call_args.args[0], ['taskkill', '/F', '/IM', 'Photoshop.exe'])
        self.assertTrue(self.guard.paused)

    @unittest.skipUnless(app.os.name == 'nt', 'Windows menu')
    def test_force_menu_requires_two_yes_answers(self):
        user = ctypes.windll.user32
        for answers, expected in [([7], False), ([6, 7], False), ([6, 6], True)]:
            with self.subTest(answers=answers), \
                 patch.object(user, 'TrackPopupMenu', return_value=1006), \
                 patch.object(user, 'MessageBoxW', side_effect=answers) as confirm, \
                 patch.object(self.guard, '_run_async') as schedule:
                self.guard.show_menu()
                self.assertEqual(confirm.call_count, len(answers))
                if expected:
                    schedule.assert_called_once_with(self.guard.close_photoshop_forced)
                else:
                    schedule.assert_not_called()
                for call in confirm.call_args_list:
                    self.assertTrue(call.args[-1] & 0x100, 'NO must be the default answer')

    @unittest.skipUnless(app.os.name == 'nt', 'Windows timer')
    def test_heartbeat_requires_live_ui_and_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            self.guard._heartbeat_path = Path(folder) / 'heartbeat'
            self.guard.worker = Mock()
            self.guard.worker.is_alive.return_value = True
            self.guard._last_monitor_tick = app.time.monotonic()
            self.guard._wnd_proc(None, 0x0113, 1, 0)
            self.assertTrue(self.guard._heartbeat_path.is_file())
            value = float(self.guard._heartbeat_path.read_text(encoding='ascii'))
            self.assertLessEqual(value, app.time.monotonic())
            self.assertFalse(self.guard._heartbeat_path.with_suffix('.tmp').exists())
            self.guard._heartbeat_path.unlink()
            self.guard._last_monitor_tick -= 30
            self.guard._wnd_proc(None, 0x0113, 1, 0)
            self.assertFalse(self.guard._heartbeat_path.exists())
            self.guard._last_monitor_tick = app.time.monotonic()
            self.guard.worker.is_alive.return_value = False
            self.guard._wnd_proc(None, 0x0113, 1, 0)
            self.assertFalse(self.guard._heartbeat_path.exists())

    def test_valid_heartbeat_age(self):
        with tempfile.TemporaryDirectory() as folder:
            heartbeat = Path(folder) / 'heartbeat'
            heartbeat.write_text('80.5', encoding='ascii')
            with patch.object(supervisor, 'HEARTBEAT', heartbeat):
                self.assertEqual(supervisor.heartbeat_age(now=100), 19.5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
