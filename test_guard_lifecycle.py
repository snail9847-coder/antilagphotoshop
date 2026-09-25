"""Deterministic lifecycle regressions. All external process calls are mocked."""
import ctypes
import logging
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import antilagphotoshop as app


class GuardLifecycleTests(unittest.TestCase):
    def setUp(self):
        with patch.object(app, 'configure_logging', return_value=Mock(spec=logging.Logger)):
            self.guard = app.AntilagPhotoshop(auto_start=False)
        # Tests must never open/close a real application or start background actions.
        self.popen = self.enter_patch(app.subprocess, 'Popen')
        self.async_action = self.enter_patch(self.guard, '_run_async')

    def enter_patch(self, target, attribute, **kwargs):
        patcher = patch.object(target, attribute, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_exit_during_health_check_cancels_pending_launch(self):
        def health(_path):
            self.guard.stop_event.set()
            return None
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', side_effect=health):
            self.assertFalse(self.guard.launch_photoshop(manual=True))
        self.popen.assert_not_called()

    def test_pause_during_health_check_cancels_automatic_launch(self):
        def health(_path):
            self.guard.paused = True
            return None
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', side_effect=health):
            self.assertFalse(self.guard.launch_photoshop())
        self.popen.assert_not_called()

    def test_force_close_serializes_manual_launch(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        errors = []
        def taskkill(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release taskkill')
            return subprocess.CompletedProcess([], 0, '', '')
        def close():
            try:
                results.append(self.guard.close_photoshop_forced())
            except BaseException as exc:
                errors.append(exc)
        with patch.object(app.subprocess, 'run', side_effect=taskkill), \
             patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', return_value=None):
            worker = threading.Thread(target=close)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(self.guard.launch_photoshop(manual=True))
                self.popen.assert_not_called()
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertTrue(self.guard.paused)

    def test_shutdown_before_tray_creation_is_safe_and_idempotent(self):
        # No real Win32 functions are used, even on Windows.
        user = Mock()
        shell = Mock()
        with patch.object(app.ctypes, 'windll', Mock(user32=user, shell32=shell), create=True):
            self.guard.shutdown()
            self.guard.shutdown()
        self.assertTrue(self.guard.stop_event.is_set())
        self.assertIsNone(self.guard.hwnd)
        user.DestroyWindow.assert_not_called()
        shell.Shell_NotifyIconW.assert_not_called()

    @unittest.skipUnless(app.os.name == 'nt', 'Windows menu')
    def test_resume_from_no_auto_start_takes_one_click(self):
        user = ctypes.windll.user32
        for _ in range(app.MAX_ATTEMPTS):
            self.guard.policy.record_attempt()
        with patch.object(user, 'TrackPopupMenu', return_value=1003):
            self.guard.show_menu()
        self.assertTrue(self.guard.auto_start)
        self.assertFalse(self.guard.paused)
        self.assertEqual(self.guard.policy.attempts(), 0)
        with patch.object(user, 'TrackPopupMenu', return_value=1003):
            self.guard.show_menu()
        self.assertTrue(self.guard.paused)

    @unittest.skipUnless(app.os.name == 'nt', 'Windows menu')
    def test_menu_handle_is_freed_on_failure(self):
        user = ctypes.windll.user32
        with patch.object(user, 'CreatePopupMenu', return_value=123), \
             patch.object(user, 'AppendMenuW', side_effect=RuntimeError('test failure')), \
             patch.object(user, 'DestroyMenu') as destroy:
            self.guard.show_menu()
        destroy.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main(verbosity=2)
