# -*- coding: utf-8 -*-
"""Supervisor приложения antilagphotoshop.

Следит только за процессом antilagphotoshop.
Photoshop этот supervisor не запускает и не завершает напрямую.

Требования:
    Windows 10/11
    Python 3.9+
    antilagphotoshop.py и antilagphotoshop_winapi.py в той же папке
"""

from __future__ import annotations

import ctypes
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from antilagphotoshop_winapi import configure_winapi


APP_DIR = Path(__file__).resolve().parent
GUARD = APP_DIR / "antilagphotoshop.py"

HEARTBEAT = (
    Path(os.environ.get("TEMP", str(Path.home())))
    / f"antilagphotoshop-supervisor-{os.getpid()}.heartbeat"
)

HANG_TIMEOUT = 25.0
STARTUP_GRACE = 35.0
POLL_INTERVAL = 3.0
STOP_TIMEOUT = 5.0

MAX_SUPERVISOR_RESTARTS = 8
RESTART_WINDOW = 15 * 60.0

MUTEX_NAME = "Local\\antilagphotoshopSupervisor"
ERROR_ALREADY_EXISTS = 183


def get_logger() -> logging.Logger:
    log = logging.getLogger("antilagphotoshop_supervisor")

    if log.handlers:
        return log

    log.setLevel(logging.INFO)
    log.propagate = False

    folder = (
        Path(os.environ.get("APPDATA", str(Path.home())))
        / "antilagphotoshop"
    )

    try:
        folder.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            folder / "supervisor.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            )
        )
        log.addHandler(handler)

    except OSError:
        log.addHandler(logging.NullHandler())

    return log


def heartbeat_age(now: float | None = None) -> float:
    """Возраст heartbeat или бесконечность при ошибке чтения."""
    try:
        text = HEARTBEAT.read_text(encoding="ascii").strip()
        value = float(text)

        # Измеряем время после чтения.
        # Дочерний процесс может обновить файл одновременно с нами.
        current = time.monotonic() if now is None else now

        if not math.isfinite(value):
            return float("inf")

        if value < 0 or value > current:
            return float("inf")

        return current - value

    except (OSError, ValueError, UnicodeError):
        return float("inf")


def restart_delay(failures: int) -> float:
    """Задержки: 1, 2, 4, 8, 16, затем максимум 30 секунд."""
    if failures >= 6:
        return 30.0

    return float(2 ** max(0, failures - 1))


def python_for_child() -> str:
    """По возможности используем pythonw без консольного окна."""
    exe = Path(sys.executable)

    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")

        if candidate.is_file():
            return str(candidate)

    return str(exe)


def remove_heartbeat_files(log: logging.Logger) -> None:
    """Удаляет heartbeat и временный файл его атомарной записи."""
    for path in (HEARTBEAT, HEARTBEAT.with_suffix(".tmp")):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "Не удалось удалить файл heartbeat %s: %s",
                path,
                exc,
            )


def stop_child(
    child: subprocess.Popen,
    log: logging.Logger,
) -> bool:
    """Останавливает только дочерний процесс antilagphotoshop.

    Возвращает True только после подтверждённого завершения.
    """
    if child.poll() is not None:
        return True

    for action in (child.terminate, child.kill):
        try:
            action()
            child.wait(timeout=STOP_TIMEOUT)

        except subprocess.TimeoutExpired:
            log.warning(
                "Истекло время ожидания остановки antilagphotoshop"
            )

        except OSError as exc:
            log.warning(
                "Ошибка остановки antilagphotoshop: %s",
                exc,
            )

        if child.poll() is not None:
            return True

    log.error(
        "Остановка antilagphotoshop не подтверждена. "
        "Новый экземпляр запускаться не будет."
    )
    return False


def wait_for_guard(
    child: subprocess.Popen,
    log: logging.Logger,
) -> int:
    """Ожидает завершения дочернего процесса и проверяет heartbeat."""
    started = time.monotonic()

    while True:
        try:
            # При штатном завершении сразу возвращаем код выхода.
            # Удалённый при выходе heartbeat проверять уже не нужно.
            return child.wait(timeout=POLL_INTERVAL)

        except subprocess.TimeoutExpired:
            pass

        running_for = time.monotonic() - started

        if running_for <= STARTUP_GRACE:
            continue

        age = heartbeat_age()

        if age <= HANG_TIMEOUT:
            continue

        # Процесс мог завершиться после истечения времени ожидания.
        code = child.poll()

        if code is not None:
            return code

        log.error(
            "Heartbeat antilagphotoshop не обновлялся %.1f сек.; "
            "остановка только antilagphotoshop",
            age,
        )

        if not stop_child(child, log):
            raise RuntimeError(
                "Предыдущий процесс antilagphotoshop ещё работает"
            )

        code = child.poll()

        if code is None:
            raise RuntimeError(
                "Завершение antilagphotoshop не подтверждено"
            )

        return code


def run() -> int:
    if os.name != "nt":
        print("Supervisor antilagphotoshop предназначен для Windows.")
        return 2

    configure_winapi()

    kernel = ctypes.windll.kernel32
    mutex = kernel.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.GetLastError()

    if not mutex:
        log = get_logger()
        log.error("Не удалось создать mutex: %s", error)
        return 1

    if error == ERROR_ALREADY_EXISTS:
        kernel.CloseHandle(mutex)
        return 0

    # Дубликат supervisor не открывает общий файл журнала.
    log = get_logger()
    child = None

    try:
        if not GUARD.is_file():
            log.critical(
                "Отсутствует основной файл: %s. "
                "Положите antilagphotoshop.py рядом с supervisor.",
                GUARD,
            )
            return 2

        args = [
            python_for_child(),
            str(GUARD),
            *sys.argv[1:],
        ]

        env = dict(
            os.environ,
            ANTILAGPHOTOSHOP_HEARTBEAT=str(HEARTBEAT),
        )

        failures: list[float] = []

        while True:
            remove_heartbeat_files(log)

            try:
                child = subprocess.Popen(
                    args,
                    cwd=str(APP_DIR),
                    close_fds=True,
                    env=env,
                )

            except OSError:
                log.exception(
                    "Не удалось запустить antilagphotoshop"
                )

            else:
                log.info(
                    "antilagphotoshop запущен, PID=%s",
                    child.pid,
                )

                try:
                    code = wait_for_guard(child, log)

                except (RuntimeError, OSError) as exc:
                    log.critical(
                        "Наблюдение остановлено: %s. "
                        "Запуск нового экземпляра отменён.",
                        exc,
                    )
                    return 4

                if code == 0:
                    log.info(
                        "antilagphotoshop завершён штатно; "
                        "supervisor остановлен"
                    )
                    return 0

                log.warning(
                    "antilagphotoshop завершился с кодом %s",
                    code,
                )

            # Старые сбои не должны увеличивать текущую задержку.
            now = time.monotonic()
            failures = [
                stamp
                for stamp in failures
                if now - stamp <= RESTART_WINDOW
            ]
            failures.append(now)

            if len(failures) >= MAX_SUPERVISOR_RESTARTS:
                log.critical(
                    "Supervisor остановлен: %d сбоев "
                    "antilagphotoshop за 15 минут",
                    len(failures),
                )
                return 3

            delay = restart_delay(len(failures))

            log.warning(
                "Повтор запуска antilagphotoshop через %.0f сек.",
                delay,
            )
            time.sleep(delay)

    except KeyboardInterrupt:
        log.info("Получена команда остановки supervisor")
        return 0

    finally:
        try:
            child_stopped = True

            if child is not None and child.poll() is None:
                child_stopped = stop_child(child, log)

            if child_stopped:
                remove_heartbeat_files(log)
            else:
                log.error(
                    "Supervisor завершает работу, но остановить "
                    "antilagphotoshop не удалось. "
                    "Файлы heartbeat оставлены."
                )

        finally:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(run())