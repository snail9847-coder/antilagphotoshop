"""Real Windows checks. Only antilagphotoshop processes are started/stopped."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

from antilagphotoshop_winapi import configure_winapi

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == 'nt', 'Requires the Windows desktop')
class WindowsSmokeTests(unittest.TestCase):
    def setUp(self):
        configure_winapi()
        self.user = ctypes.windll.user32
        self.user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.user.FindWindowW.restype = wintypes.HWND
        self.temp = tempfile.TemporaryDirectory(prefix='antilagphotoshop-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = dict(os.environ, APPDATA=self.temp.name, TEMP=self.temp.name, PYTHONIOENCODING='utf-8')

    def wait_until(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        logs = '\n'.join(path.read_text(encoding='utf-8', errors='replace') for path in self.directory.glob('antilagphotoshop/*.log'))
        self.fail('Timed out waiting for application.\n' + logs)

    def window(self, pid):
        return self.user.FindWindowW(f'antilagphotoshop_{pid}', 'antilagphotoshop')

    def stop(self, process, child_pid=None):
        # Always address the unique window class of our test child, not Photoshop.
        hwnd = self.window(child_pid or process.pid)
        if hwnd:
            self.user.PostMessageW(hwnd, 0x0010, 0, 0)
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)

    def test_native_icon_menu_tray_and_resource_cleanup(self):
        code = r'''
import ctypes
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch
from antilagphotoshop import AntilagPhotoshop
app = AntilagPhotoshop(auto_start=False)
user = ctypes.windll.user32
for size in (16, 24, 32, 48, 64, 128, 256):
    icon = user.LoadImageW(None, str(app._icon_path), 1, size, size, 0x10)
    assert icon, ('ICO load failed', size, ctypes.GetLastError())
    assert user.DestroyIcon(icon)
app.create_tray()
try:
    assert app.hwnd and app._owns_icon and app.icon_handle
    user.GetMenuItemCount.argtypes = [wintypes.HMENU]
    user.GetMenuItemCount.restype = ctypes.c_int
    user.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
    user.GetMenuStringW.restype = ctypes.c_int
    def inspect_menu(menu, *args):
        assert user.GetMenuItemCount(menu) == 11
        label = ctypes.create_unicode_buffer(256)
        assert user.GetMenuStringW(menu, 1008, label, 256, 0)
        assert 'antilagphotoshop' in label.value
        return 0  # Cancel at the user-input boundary; native menu creation is real.
    with patch.object(user, 'TrackPopupMenu', side_effect=inspect_menu) as popup:
        app.show_menu()
        assert popup.call_count == 1
    nid = app._notify_cls()
    nid.cbSize = ctypes.sizeof(nid)
    nid.hWnd = app.hwnd
    nid.uID = 1
    assert ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
    app._wnd_proc(app.hwnd, app._taskbar_created, 0, 0)
    assert ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(nid))
    assert app.free_ram_mb() is not None
    assert app.free_disk_mb(str(Path.cwd())) is not None
finally:
    app.shutdown()
    assert app.hwnd is None and app.icon_handle is None
    assert user.UnregisterClassW(app._class_name, ctypes.windll.kernel32.GetModuleHandleW(None))
print('native tray/icon/menu/resources OK')
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('native tray/icon/menu/resources OK', result.stdout)

    def test_real_app_heartbeat_duplicate_and_clean_exit(self):
        heartbeat = self.directory / 'app.heartbeat'
        env = dict(self.env, ANTILAGPHOTOSHOP_HEARTBEAT=str(heartbeat))
        child = subprocess.Popen([sys.executable, str(ROOT / 'antilagphotoshop.py'), '--no-auto-start'], cwd=ROOT, env=env)
        self.addCleanup(self.stop, child)
        hwnd = self.wait_until(lambda: self.window(child.pid))
        self.wait_until(heartbeat.is_file)
        first = float(heartbeat.read_text(encoding='ascii'))
        self.wait_until(lambda: heartbeat.is_file() and float(heartbeat.read_text(encoding='ascii')) > first)
        duplicate = subprocess.run([sys.executable, str(ROOT / 'antilagphotoshop.py'), '--no-auto-start'], cwd=ROOT, env=env, timeout=10)
        self.assertEqual(duplicate.returncode, 0)
        self.assertIsNone(child.poll())
        self.assertTrue(heartbeat.is_file())
        self.assertTrue(self.user.PostMessageW(hwnd, 0x0010, 0, 0))
        self.assertEqual(child.wait(timeout=12), 0)
        self.assertFalse(heartbeat.exists())
        log = (self.directory / 'antilagphotoshop' / 'antilagphotoshop.log').read_text(encoding='utf-8')
        self.assertNotIn('ERROR', log)
        self.assertNotIn('Запущен Photoshop:', log)
        self.assertFalse(self.window(child.pid))

    def test_supervisor_duplicate_and_normal_exit(self):
        script = str(ROOT / 'antilagphotoshop_supervisor.pyw')
        parent = subprocess.Popen([sys.executable, script, '--no-auto-start'], cwd=ROOT, env=self.env)
        child_pid = None
        try:
            log_path = self.directory / 'antilagphotoshop' / 'supervisor.log'
            def get_child_pid():
                if log_path.is_file():
                    match = re.search(r'PID=(\d+)', log_path.read_text(encoding='utf-8'))
                    return int(match.group(1)) if match else None
            child_pid = self.wait_until(get_child_pid)
            hwnd = self.wait_until(lambda: self.window(child_pid))
            heartbeat = self.directory / f'antilagphotoshop-supervisor-{parent.pid}.heartbeat'
            self.wait_until(heartbeat.is_file)
            duplicate = subprocess.run([sys.executable, script, '--no-auto-start'], cwd=ROOT, env=self.env, timeout=10)
            self.assertEqual(duplicate.returncode, 0)
            self.assertIsNone(parent.poll())
            self.assertTrue(self.user.PostMessageW(hwnd, 0x0010, 0, 0))
            self.assertEqual(parent.wait(timeout=15), 0)
            self.assertFalse(self.window(child_pid))
            self.assertFalse(heartbeat.exists())
            log = log_path.read_text(encoding='utf-8')
            self.assertEqual(len(re.findall(r'PID=\d+', log)), 1)
            self.assertNotIn('ERROR', log)
        finally:
            self.stop(parent, child_pid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
