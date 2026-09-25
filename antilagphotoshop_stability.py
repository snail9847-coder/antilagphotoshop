"""Small, deterministic policies. No processes, threads or Windows calls."""
from __future__ import annotations

import threading


class RestartSchedule:
    """Delay observed exits and repeated attempts without sleeping the UI/worker.

    Call from the guard's launch lock. Unknown snapshots do not establish exit.
    The existing RestartPolicy still limits attempts to three per ten minutes.
    """
    def __init__(self):
        self.ready_at = 0.0
        self.was_running = False

    def observe(self, running, now: float) -> None:
        if running is True:
            self.was_running = True
        elif running is False and self.was_running:
            self.ready_at = max(self.ready_at, now + 10.0)
            self.was_running = False

    def can_attempt(self, now: float) -> bool:
        return now >= self.ready_at

    def record_attempt(self, now: float, attempt: int) -> None:
        delay = min(60.0, 10.0 * (2 ** min(3, max(0, attempt - 1))))
        self.ready_at = now + delay


class ResourceWarnings:
    """Check once per 30 seconds; notify no more than once per 5 minutes."""
    def __init__(self):
        self.next_check = 0.0
        self.next_notice = 0.0

    def check_due(self, now: float) -> bool:
        if now < self.next_check:
            return False
        self.next_check = now + 30.0
        return True

    def should_notify(self, problem, now: float) -> bool:
        if not problem or now < self.next_notice:
            return False
        self.next_notice = now + 300.0
        return True


class ProcessSnapshot:
    """Share a successful recent PID snapshot with window inspection.

    Fresh launch checks still execute tasklist. Failed queries clear the cache.
    The lock protects only publication, never an external command.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._pids = None
        self._at = float('-inf')

    def publish(self, pids, now: float) -> None:
        with self._lock:
            self._pids = None if pids is None else frozenset(pids)
            self._at = now

    def recent(self, now: float):
        with self._lock:
            if self._pids is None or not 0 <= now - self._at <= 5.0:
                return None
            return set(self._pids)
