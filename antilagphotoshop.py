# -*- coding: utf-8 -*-
"""
antilagphotoshop

Небольшой Windows-tray watchdog для Adobe Photoshop 2020.
- Значок с белой молнией находится в области уведомлений.
- Photoshop запускается только если его нет среди процессов.
- При падении делается ограниченное число попыток перезапуска, без бесконечного цикла.
- Автоматического принудительного завершения Photoshop нет; ручное требует двух подтверждений.

Python 3.9+ / Windows 10/11. Сторонние библиотеки не нужны.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
from collections import deque
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Optional

from antilagphotoshop_winapi import configure_winapi
from antilagphotoshop_stability import ProcessSnapshot, ResourceWarnings, RestartSchedule

APP_NAME = "antilagphotoshop"
PROCESS_NAME = "Photoshop.exe"
CHECK_INTERVAL_SECONDS = 5
MAX_ATTEMPTS = 3
ATTEMPT_WINDOW_SECONDS = 10 * 60
HANG_NOTIFY_SECONDS = 45
HANG_CHECK_EVERY = 2  # in monitor ticks (~10 s of wall time)
MIN_FREE_RAM_MB = 400
MIN_FREE_DISK_MB = 2048
CONFIG_PATH_FILE = Path(__file__).with_name("photoshop_path.txt")


class RestartPolicy:
    """Circuit breaker: prevents a damaged install from restart-spamming Windows."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS, window_seconds: float = ATTEMPT_WINDOW_SECONDS):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._attempts and now - self._attempts[0] > self.window_seconds:
            self._attempts.popleft()

    def can_attempt(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            return len(self._attempts) < self.max_attempts

    def record_attempt(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            self._attempts.append(now)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()

    def attempts(self, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            return len(self._attempts)


def photoshop_candidates() -> list[Path]:
    """Return likely Photoshop 2020 locations without scanning the whole disk."""
    roots: list[Path] = []
    for key in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(
            [
                root / "Adobe" / "Adobe Photoshop 2020" / "Photoshop.exe",
                root / "Adobe" / "Adobe Photoshop 2020 (64 Bit)" / "Photoshop.exe",
            ]
        )
    # Non-standard install locations used on this machine.
    candidates.append(Path("E:/Adobe Photoshop 2020/Photoshop.exe"))
    # Remove duplicates while preserving order.
    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = os.path.normcase(str(item))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def find_photoshop(explicit: Optional[str] = None) -> Optional[Path]:
    # Persisted path from photoshop_path.txt wins if the file exists and is valid.
    if not explicit and CONFIG_PATH_FILE.is_file():
        try:
            saved = CONFIG_PATH_FILE.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            saved = ""
        if saved and Path(saved).is_file():
            return Path(saved)
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for path in photoshop_candidates():
        if path.is_file():
            return path
    return None


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("antilagphotoshop")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        folder = Path(os.environ.get("APPDATA", Path.home())) / "antilagphotoshop"
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / "antilagphotoshop.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        logger.addHandler(logging.NullHandler())
    return logger


class AntilagPhotoshop:
    """Windows tray application. UI callbacks are guarded so one exception cannot kill the loop."""

    def __init__(self, explicit_path: Optional[str] = None, auto_start: bool = True):
        configure_winapi()
        self.log = configure_logging()
        self._launch_lock = threading.Lock()
        self._restart_schedule = RestartSchedule()
        self._resource_warnings = ResourceWarnings()
        self._process_snapshot = ProcessSnapshot()
        self._last_monitor_tick = time.monotonic()
        self._owns_icon = False
        self._taskbar_created = 0
        self.explicit_path = explicit_path
        self.auto_start = auto_start
        self.paused = False
        self.stop_event = threading.Event()
        self.policy = RestartPolicy()
        self.worker: Optional[threading.Thread] = None
        self.our_process: Optional[subprocess.Popen] = None
        self.last_state: Optional[bool] = None
        self.last_balloon = 0.0
        self._tick = 0
        self.hung_since: Optional[float] = None
        self.hang_notified = False
        self.hwnd = None
        self.icon_handle = None
        self._mutex = None
        self._wndproc_ref = None
        self._class_name = f"antilagphotoshop_{os.getpid()}"
        self._tray_callback = 0x8001
        self._icon_path = Path(__file__).with_name("antilagphotoshop.ico")
        self._heartbeat_path = Path(os.environ.get(
            "ANTILAGPHOTOSHOP_HEARTBEAT",
            str(Path(os.environ.get("TEMP", str(Path.home()))) / f"antilagphotoshop-{os.getpid()}.heartbeat"),
        ))

    # -------------------- Process layer --------------------
    def _query_photoshop_pids(self):
        """Fresh query for launch safety. Unknown never becomes an empty set."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
            if result.returncode != 0:
                raise OSError(f"tasklist exit code {result.returncode}")
            pids = set()
            for row in csv.reader((result.stdout or "").splitlines()):
                if row and row[0].lower() == PROCESS_NAME.lower():
                    if len(row) < 2:
                        raise ValueError("Missing Photoshop PID")
                    pid = int(row[1])
                    if pid <= 0:
                        raise ValueError("Invalid Photoshop PID")
                    pids.add(pid)
            self._process_snapshot.publish(pids, time.monotonic())
            return pids
        except (OSError, ValueError, csv.Error, subprocess.SubprocessError) as exc:
            self._process_snapshot.publish(None, time.monotonic())
            self.log.warning("Не удалось проверить процесс Photoshop: %s", exc)
            return None

    def is_photoshop_running(self) -> Optional[bool]:
        pids = self._query_photoshop_pids()
        return None if pids is None else bool(pids)

    def photoshop_pids(self) -> set[int]:
        pids = self._process_snapshot.recent(time.monotonic())
        if pids is None:
            pids = self._query_photoshop_pids()
        return set() if pids is None else pids

    def free_ram_mb(self) -> Optional[int]:
        """Available physical memory in MB, or None if the API failed."""
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullAvailPhys // (1024 * 1024))
        except Exception:
            self.log.exception("Не удалось получить данные о памяти")
        return None

    def free_disk_mb(self, path: str) -> Optional[int]:
        """Free space on the drive of `path` in MB, or None if the API failed."""
        try:
            root = str(Path(path).anchor or Path(path).resolve().anchor)
            if root.endswith(":"):
                root += "\\"
            free = ctypes.c_ulonglong()
            total = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free), ctypes.byref(total), None):
                return int(free.value // (1024 * 1024))
        except Exception:
            self.log.exception("Не удалось получить данные о диске: %s", path)
        return None

    def environment_health(self, photoshop_path: Optional[Path]) -> Optional[str]:
        """Read-only pressure checks; install drive is not the scratch disk."""
        problems = []
        ram = self.free_ram_mb()
        if ram is not None and ram < MIN_FREE_RAM_MB:
            problems.append(f"Мало свободной RAM: {ram} МБ")
        seen = set()
        paths = [
            ("системный диск", os.environ.get("SYSTEMDRIVE", "C:")),
            ("диск установки", str(photoshop_path) if photoshop_path else None),
            ("диск TEMP", os.environ.get("TEMP")),
        ]
        for label, value in paths:
            if not value:
                continue
            root = str(Path(value).anchor or Path(value).resolve().anchor)
            root = root.rstrip("\\/").casefold()
            if root in seen:
                continue
            seen.add(root)
            disk = self.free_disk_mb(value)
            if disk is not None and disk < MIN_FREE_DISK_MB:
                problems.append(f"Мало места ({label}): {disk} МБ")
        return "; ".join(problems) if problems else None

    def _check_resources(self) -> None:
        now = time.monotonic()
        if not self._resource_warnings.check_due(now):
            return
        try:
            problem = self.environment_health(find_photoshop(self.explicit_path))
            if self._resource_warnings.should_notify(problem, now):
                self.log.warning("Недостаточно ресурсов: %s", problem)
                self.balloon("Мало ресурсов", (problem + ". Сохраните работу; проверьте RAM и диски.")[:255], error=True)
        except Exception:
            self.log.exception("Не удалось проверить доступные ресурсы")

    def _photoshop_windows(self) -> list[tuple[int, int]]:
        """Visible top-level windows of Photoshop: [(hwnd, pid), ...]."""
        pids = self.photoshop_pids()
        if not pids:
            return []
        user32 = ctypes.windll.user32
        found: list[tuple[int, int]] = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def on_window(hwnd, _lparam):
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids and user32.IsWindowVisible(hwnd):
                    found.append((hwnd, pid.value))
                return True
            except Exception:
                return True

        # Keep a reference so the callback is not collected mid-enumeration.
        proc = EnumProc(on_window)
        user32.EnumWindows(proc, 0)
        return found

    def hung_photoshop_windows(self) -> list[int]:
        """HWNDs of Photoshop windows that stopped responding (IsHungAppWindow)."""
        user32 = ctypes.windll.user32
        return [hwnd for hwnd, _pid in self._photoshop_windows() if user32.IsHungAppWindow(hwnd)]

    def close_photoshop_graceful(self) -> int:
        """Send WM_CLOSE: Photoshop itself shows the 'Save changes?' dialog.
        Returns how many windows were asked to close."""
        # Target the main window, not document windows or a save dialog.
        with self._launch_lock:
            hwnd = self._main_window()
            if not hwnd:
                return 0
            was_paused = self.paused
            self.paused = True
            if not ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0):
                self.paused = was_paused
                return 0
        self.log.info("Отправлен WM_CLOSE; автозапуск на паузе")
        self.balloon(
            "Photoshop", "Запрос закрытия отправлен. Сохранение предлагает сам Photoshop, если отвечает. Автозапуск на паузе",
        )
        return 1

    def close_photoshop_forced(self) -> bool:
        """Last resort for a fully hung Photoshop. Data loss is possible."""
        # This method is reachable from the UI only after two confirmations.
        with self._launch_lock:
            self.paused = True
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                result = subprocess.run(
                    ["taskkill", "/F", "/IM", PROCESS_NAME],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=flags,
                    check=False,
                )
                ok = result.returncode == 0
                if ok:
                    self.log.warning("Photoshop закрыт принудительно по команде пользователя")
                else:
                    self.log.error("taskkill завершился с кодом %s", result.returncode)
                return ok
            except (OSError, subprocess.SubprocessError) as exc:
                self.log.error("Не удалось принудительно закрыть Photoshop: %s", exc)
                return False

    def _main_window(self) -> Optional[int]:
        """HWND of the Photoshop main window (class name starts with 'Photoshop')."""
        user32 = ctypes.windll.user32
        for hwnd, _pid in self._photoshop_windows():
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value.startswith("Photoshop"):
                return hwnd
        return None

    def set_photoshop_windowed(self) -> bool:
        """SW_RESTORE: windowed mode instead of full-screen."""
        user32 = ctypes.windll.user32
        hwnd = self._main_window()
        if not hwnd:
            self.balloon("Photoshop не запущен", "Нет окна Photoshop")
            return False
        user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        self.log.info("Photoshop переведён в оконный режим")
        return True

    def set_photoshop_fullscreen(self) -> bool:
        """SW_MAXIMIZE: full-screen mode."""
        user32 = ctypes.windll.user32
        hwnd = self._main_window()
        if not hwnd:
            self.balloon("Photoshop не запущен", "Нет окна Photoshop")
            return False
        user32.ShowWindowAsync(hwnd, 3)  # SW_MAXIMIZE
        self.log.info("Photoshop развёрнут на весь экран")
        return True

    def fix_black_screen(self) -> bool:
        """Nudge the Photoshop window: minimize+restore forces a GPU redraw.
        This is the standard non-destructive cure for a black canvas with a
        live cursor; unsaved work is untouched."""
        user32 = ctypes.windll.user32
        hwnd = self._main_window()
        if not hwnd:
            self.balloon("Photoshop не запущен", "Нет окна Photoshop")
            return False
        user32.ShowWindowAsync(hwnd, 6)  # SW_MINIMIZE
        time.sleep(0.4)
        user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        # Ask the window to repaint itself fully.
        user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0080)  # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN (asynchronous)
        self.log.info("Принудительна перерисовка окна Photoshop (лечение чёрного экрана)")
        self.balloon("Чёрный экран", "Окно Photoshop обновлено; если чёрный фон остался, повторите ещё раз")
        return True

    def open_logs_folder(self) -> None:
        """Open the log folder in Explorer so the user can read it in one click."""
        try:
            folder = Path(os.environ.get("APPDATA", str(Path.home()))) / "antilagphotoshop"
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except (OSError, AttributeError) as exc:
            self.log.error("Не удалось открыть папку журнала: %s", exc)
            self.balloon("Ошибка", f"Не удалось открыть журнал: {exc}"[:180], error=True)

    def open_tray_settings(self) -> None:
        """Open the Windows 'Notification area icons' page to pin the lightning icon."""
        try:
            subprocess.Popen(
                ["control.exe", "/name", "Microsoft.NotificationAreaIcons"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.balloon(
                "Настройка значков",
                "Найдите antilagphotoshop и выберите «Показывать значок и уведомления»",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.error("Не удалось открыть настройку значков: %s", exc)

    def _run_async(self, func, *args) -> None:
        """Run a menu action off the UI thread so the tray can never freeze."""
        threading.Thread(target=func, args=args, name="MenuAction", daemon=True).start()

    def launch_photoshop(self, manual: bool = False) -> bool:
        # Tray clicks and the monitor can arrive concurrently. Only one may launch.
        if not self._launch_lock.acquire(blocking=False):
            return False
        try:
            if self.stop_event.is_set() or (self.paused and not manual):
                return False
            running = self.is_photoshop_running()
            self._restart_schedule.observe(running, time.monotonic())
            if running is None:
                self.balloon("Проверка недоступна", "Не удалось проверить процессы. Повторный запуск отменён", error=True)
                return False
            if running or (self.our_process is not None and self.our_process.poll() is None):
                self.log.info("Photoshop уже запущен или запускается")
                return True
            if manual:
                self.policy.reset()
            elif not self.policy.can_attempt():
                self.paused = True
                self.log.error("Автозапуск приостановлен: слишком много попыток")
                self.balloon("Автозапуск приостановлен", "Проверьте путь к Photoshop и журнал", error=True)
                return False
            if not manual and not self._restart_schedule.can_attempt(time.monotonic()):
                return False
            # Record BEFORE lookup/Popen: failed launches must count as attempts.
            self.policy.record_attempt()
            self._restart_schedule.record_attempt(time.monotonic(), self.policy.attempts())
            path = find_photoshop(self.explicit_path)
            if not path:
                self.log.error("Photoshop 2020 не найден")
                self.balloon("Photoshop 2020 не найден", "Укажите путь через --photoshop", error=True)
                return False
            health = self.environment_health(path)
            if health:
                self.log.error("Запуск отменён: %s", health)
                self.balloon("Запуск Photoshop отменён", health, error=True)
                if not manual:
                    self.paused = True
                return False
            # Honour Exit or Pause requested during the slow preflight checks.
            if self.stop_event.is_set() or (self.paused and not manual):
                return False
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.our_process = subprocess.Popen(
                [str(path)], cwd=str(path.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=flags,
            )
            self.log.info("Запущен Photoshop: %s", path)
            self.balloon("Photoshop 2020", "Запуск выполнен")
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.exception("Ошибка запуска Photoshop: %s", exc)
            self.balloon("Ошибка запуска Photoshop", str(exc)[:180], error=True)
            return False
        finally:
            self._launch_lock.release()

    def write_heartbeat(self) -> None:
        """Called by the UI timer only while the monitor is also alive."""
        temporary = self._heartbeat_path.with_suffix(".tmp")
        try:
            temporary.write_text(str(time.monotonic()), encoding="ascii")
            temporary.replace(self._heartbeat_path)
        except OSError as exc:
            self.log.warning("Не удалось записать heartbeat: %s", exc)

    def monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            self._last_monitor_tick = time.monotonic()
            try:
                running = self.is_photoshop_running()
                if self._launch_lock.acquire(blocking=False):
                    try:
                        self._restart_schedule.observe(running, time.monotonic())
                    finally:
                        self._launch_lock.release()
                self._tick += 1
                if running is True:
                    self._check_resources()
                    if self.last_state is False:
                        self.log.info("Photoshop снова работает")
                    self.hang_notified = self.hang_notified if self.last_state else False
                    if self._tick % HANG_CHECK_EVERY == 0:
                        self._check_hang()
                elif running is False:
                    if self.last_state is True:
                        self.log.warning("Photoshop завершился или упал")
                    self.hung_since = None
                    self.hang_notified = False
                    if self.auto_start and not self.paused:
                        self.launch_photoshop(manual=False)
                # Unknown is not treated as stopped: never launch on tasklist failure.
                self.last_state = running
                self.update_tooltip()
            except Exception:
                self.log.exception("Ошибка цикла мониторинга; цикл продолжен")
            self._last_monitor_tick = time.monotonic()
            if self.stop_event.wait(CHECK_INTERVAL_SECONDS):
                return

    def _window_after_launch(self) -> None:
        """Compatibility hook. Never manipulate Photoshop windows automatically."""
        return None

    def _check_hang(self) -> None:
        """Detect a not-responding Photoshop. Nothing is ever killed here."""
        try:
            if self.hung_photoshop_windows():
                if self.hung_since is None:
                    self.hung_since = time.monotonic()
                    self.log.warning("Photoshop не отвечает; наблюдение")
                elif (
                    time.monotonic() - self.hung_since >= HANG_NOTIFY_SECONDS
                    and not self.hang_notified
                ):
                    self.hang_notified = True
                    self.log.error("Photoshop завис надолго")
                    self.balloon(
                        "Photoshop не отвечает",
                        "antilagphotoshop ничего не закрывает. Меню antilagphotoshop: закрыть с запросом сохранения или принудительно",
                        error=True,
                    )
            else:
                if self.hung_since is not None:
                    self.log.info("Photoshop снова отвечает")
                self.hung_since = None
                self.hang_notified = False
        except Exception:
            self.log.exception("Ошибка проверки зависания")

    # -------------------- Tray layer --------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == 0x0113 and wparam == 1:  # WM_TIMER: UI and worker liveness
                if (self.worker and self.worker.is_alive()
                        and time.monotonic() - self._last_monitor_tick < 20):
                    self.write_heartbeat()
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                self._add_tray_icon()
                return 0
            if msg == self._tray_callback:
                event = lparam & 0xFFFF
                if event == 0x0202:  # WM_LBUTTONUP
                    self._run_async(self.launch_photoshop, True)
                elif event == 0x0205:  # WM_RBUTTONUP
                    self.show_menu()
                return 0
            if msg == 0x0010:  # WM_CLOSE
                self.shutdown()
                return 0
            if msg == 0x0002:  # WM_DESTROY
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
        except Exception:
            self.log.exception("Ошибка tray callback; продолжение работы")
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def create_tray(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Эта программа предназначена для Windows")
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        # On 64-bit Windows LRESULT, WPARAM and LPARAM are pointer-sized.
        # ctypes.c_long / wintypes.LPARAM are only 32-bit and cause:
        # OverflowError: int too long to convert.
        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        self._wndproc_ref = WNDPROC(self._wnd_proc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
            ]

        self._notify_cls = NOTIFYICONDATAW
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassW(ctypes.byref(wc)) and ctypes.GetLastError() != 1410:
            raise ctypes.WinError()
        self.hwnd = user32.CreateWindowExW(0, self._class_name, APP_NAME, 0, 0, 0, 0, 0, None, None, hinstance, None)
        if not self.hwnd:
            raise ctypes.WinError()

        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        IMAGE_ICON = 1
        if self._icon_path.is_file():
            self.icon_handle = user32.LoadImageW(None, str(self._icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            self._owns_icon = bool(self.icon_handle)
        if not self.icon_handle:
            self.log.warning("Не удалось загрузить иконку %s", self._icon_path)
            self.icon_handle = user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
        if not self.icon_handle:
            raise ctypes.WinError()
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._add_tray_icon()
        if not user32.SetTimer(self.hwnd, 1, CHECK_INTERVAL_SECONDS * 1000, None):
            raise ctypes.WinError()

    def _add_tray_icon(self) -> None:
        nid = self._notify_cls()
        nid.cbSize = ctypes.sizeof(self._notify_cls)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 0x0001 | 0x0002 | 0x0004  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self._tray_callback
        nid.hIcon = self.icon_handle
        nid.szTip = APP_NAME
        if not ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):
            raise ctypes.WinError()
        self.update_tooltip()

    def update_tooltip(self) -> None:
        if not self.hwnd or not hasattr(self, "_notify_cls"):
            return
        try:
            # The monitor owns process polling; UI callbacks must not run tasklist.
            status = {True: "запущен", False: "не запущен", None: "проверка недоступна"}[self.last_state]
            if self.paused:
                status += "; автозапуск на паузе"
            if self.hung_since is not None:
                status += "; НЕ ОТВЕЧАЕТ"
            nid = self._notify_cls()
            nid.cbSize = ctypes.sizeof(self._notify_cls)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = 0x0004  # NIF_TIP
            nid.szTip = f"Photoshop 2020: {status} | antilagphotoshop"[:127]
            ctypes.windll.shell32.Shell_NotifyIconW(0x00000001, ctypes.byref(nid))  # NIM_MODIFY
        except Exception:
            self.log.exception("Не удалось обновить tooltip")

    def balloon(self, title: str, text: str, error: bool = False) -> None:
        if not self.hwnd or not hasattr(self, "_notify_cls"):
            return
        now = time.monotonic()
        if now - self.last_balloon < 20 and not error:
            return
        self.last_balloon = now
        try:
            nid = self._notify_cls()
            nid.cbSize = ctypes.sizeof(self._notify_cls)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = 0x0010  # NIF_INFO
            nid.szInfo = str(text)[:255]
            nid.szInfoTitle = str(title)[:63]
            nid.dwInfoFlags = 0x00000003 if error else 0x00000001  # NIIF_ERROR / NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(0x00000001, ctypes.byref(nid))
        except Exception:
            self.log.exception("Не удалось показать уведомление")

    def show_menu(self) -> None:
        menu = None
        try:
            user32 = ctypes.windll.user32
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            # IDs are intentionally simple and local to this window.
            items = [
                (1001, "Открыть Photoshop 2020"),
                (1002, "Проверить сейчас"),
                (1003, "Пауза автозапуска" if self.auto_start and not self.paused else "Продолжить автозапуск"),
                (1005, "Закрыть Photoshop (спросит про сохранение)"),
                (1006, "Принудительно закрыть Photoshop (риск потери данных)"),
                (1011, "Убрать чёрный экран (обновить окно Photoshop)"),
                (1009, "Photoshop в окно (не на весь экран)"),
                (1010, "Развернуть Photoshop"),
                (1007, "Настроить показ значка antilagphotoshop всегда видимым"),
                (1008, "Открыть журнал antilagphotoshop"),
                (1004, "Выход"),
            ]
            for command, label in items:
                user32.AppendMenuW(menu, 0x0000, command, label)
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)
            selected = user32.TrackPopupMenu(menu, 0x0100 | 0x0000, point.x, point.y, 0, self.hwnd, None)
            if selected == 1001:
                self._run_async(self.launch_photoshop, True)
            elif selected == 1002:
                self.last_state = None
                self._run_async(self._check_now)
            elif selected == 1003:
                # Resume also works when started with --no-auto-start.
                self.paused = bool(self.auto_start and not self.paused)
                if not self.paused:
                    self.auto_start = True
                    self.policy.reset()
                self.log.info("Пауза автозапуска: %s", self.paused)
                self.update_tooltip()
            elif selected == 1005:
                self.log.info("Пользователь запросил корректное закрытие Photoshop")
                self._run_async(self._close_graceful_safe)
            elif selected == 1006:
                # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2
                answer = user32.MessageBoxW(
                    self.hwnd,
                    "Photoshop будет закрыт ПРИНУДИТЕЛЬНО.\n\n"
                    "Несохранённые изменения будут ПОТЕРЯНЫ.\n\n"
                    "Если Photoshop просто завис, сначала попробуйте пункт\n"
                    "«Закрыть Photoshop (спросит про сохранение)».\n\n"
                    "Всё равно продолжить?",
                    "Photoshop 2020 antilagphotoshop",
                    0x4 | 0x30 | 0x100,
                )
                if answer == 6 and user32.MessageBoxW(
                    self.hwnd,
                    "Последнее подтверждение: все несохранённые изменения будут потеряны.\n"
                    "Принудительно завершить Photoshop?",
                    APP_NAME,
                    0x4 | 0x30 | 0x100,
                ) == 6:  # Two explicit YES answers; NO is the default.
                    self._run_async(self.close_photoshop_forced)
                else:
                    self.log.info("Принудительное закрытие отменено пользователем")
            elif selected == 1007:
                self._run_async(self.open_tray_settings)
            elif selected == 1008:
                self._run_async(self.open_logs_folder)
            elif selected == 1009:
                self._run_async(self.set_photoshop_windowed)
            elif selected == 1010:
                self._run_async(self.set_photoshop_fullscreen)
            elif selected == 1011:
                self._run_async(self.fix_black_screen)
            elif selected == 1004:
                self.shutdown()
        except Exception:
            self.log.exception("Ошибка контекстного меню")
        finally:
            if menu:
                ctypes.windll.user32.DestroyMenu(menu)

    def _check_now(self) -> None:
        self.last_state = None
        self.launch_photoshop(manual=True)
        self.update_tooltip()

    def _close_graceful_safe(self) -> None:
        if not self.close_photoshop_graceful():
            self.balloon("Photoshop не запущен", "Нет открытых окон Photoshop")

    def shutdown(self) -> None:
        self.stop_event.set()
        hwnd = self.hwnd
        try:
            if hwnd:
                ctypes.windll.user32.KillTimer(hwnd, 1)
                try:
                    if hasattr(self, "_notify_cls"):
                        nid = self._notify_cls()
                        nid.cbSize = ctypes.sizeof(self._notify_cls)
                        nid.hWnd = hwnd
                        nid.uID = 1
                        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
                finally:
                    ctypes.windll.user32.DestroyWindow(hwnd)
        finally:
            self.hwnd = None
            try:
                if self.icon_handle and self._owns_icon:
                    ctypes.windll.user32.DestroyIcon(self.icon_handle)
            finally:
                self.icon_handle = None
                self._owns_icon = False

    def run(self) -> int:
        if os.name != "nt":
            print("Эта программа запускается только в Windows.")
            return 2
        kernel32 = ctypes.windll.kernel32
        self._mutex = kernel32.CreateMutexW(None, False, "Local\\antilagphotoshopMutex")
        error = ctypes.GetLastError()
        if not self._mutex:
            self.log.error("Не удалось создать mutex: %s; запуск отменён", error)
            return 1
        if error == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(self._mutex)
            self._mutex = None
            self.log.info("antilagphotoshop уже запущен")
            return 0
        try:
            self.create_tray()
            self.worker = threading.Thread(target=self.monitor_loop, name="PhotoshopMonitor", daemon=True)
            self.worker.start()
            self.log.info("antilagphotoshop запущен")
            msg = wintypes.MSG()
            while not self.stop_event.is_set():
                result = ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError()
                if result == 0:
                    break
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            self.log.exception("Критическая ошибка tray: %s", exc)
            return 1
        finally:
            self.shutdown()
            if self.worker:
                self.worker.join(timeout=6)
            ctypes.windll.user32.UnregisterClassW(self._class_name, kernel32.GetModuleHandleW(None))
            for path in (self._heartbeat_path, self._heartbeat_path.with_suffix(".tmp")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            kernel32.CloseHandle(self._mutex)
            self._mutex = None
            self.log.info("antilagphotoshop остановлен; Photoshop не завершается")


def self_test() -> int:
    """Deterministic stress test for the crash-loop protection; works on any OS."""
    for cycle in range(2000):
        policy = RestartPolicy(max_attempts=3, window_seconds=60)
        assert policy.can_attempt(0)
        policy.record_attempt(0)
        policy.record_attempt(1)
        policy.record_attempt(2)
        assert not policy.can_attempt(3), f"cycle {cycle}: circuit breaker failed"
        assert policy.can_attempt(61), f"cycle {cycle}: time window did not expire"
        policy.reset()
        assert policy.attempts(61) == 0
    assert find_photoshop("Z:\\this\\path\\cannot\\exist\\Photoshop.exe") is None
    print("SELF-TEST PASS: 2000/2000 cycles; circuit breaker and path handling OK")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--photoshop", help="полный путь к Photoshop.exe, если он установлен не по стандартному пути")
    parser.add_argument("--no-auto-start", action="store_true", help="не запускать Photoshop автоматически при старте antilagphotoshop")
    parser.add_argument("--self-test", action="store_true", help="выполнить 2000 циклов встроенного теста без запуска Windows tray")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    return AntilagPhotoshop(args.photoshop, auto_start=not args.no_auto_start).run()


if __name__ == "__main__":
    raise SystemExit(main())
