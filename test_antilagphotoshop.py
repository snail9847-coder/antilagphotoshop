import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

from antilagphotoshop import RestartPolicy, find_photoshop


def load_supervisor():
    path = Path(__file__).with_name("antilagphotoshop_supervisor.pyw")
    loader = importlib.machinery.SourceFileLoader("antilagphotoshop_supervisor", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


supervisor = load_supervisor()


class AntilagTests(unittest.TestCase):
    def test_photoshop_circuit_breaker_1000_cycles(self):
        for cycle in range(2000):
            policy = RestartPolicy(max_attempts=3, window_seconds=60)
            for stamp in (0, 1, 2):
                self.assertTrue(policy.can_attempt(stamp), cycle)
                policy.record_attempt(stamp)
            self.assertFalse(policy.can_attempt(3), cycle)
            self.assertTrue(policy.can_attempt(61), cycle)
            policy.reset()
            self.assertEqual(policy.attempts(61), 0)

    def test_backoff_is_bounded_1000_cycles(self):
        expected = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
        for _ in range(2000):
            self.assertEqual([supervisor.restart_delay(i) for i in range(1, 9)], expected)

    def test_missing_photoshop_path_is_safe(self):
        self.assertIsNone(find_photoshop(r"Z:\\does-not-exist\\Photoshop.exe"))

    def test_heartbeat_missing_is_infinite_age(self):
        old = supervisor.HEARTBEAT
        try:
            supervisor.HEARTBEAT = Path("/__definitely_missing__/heartbeat")
            self.assertEqual(supervisor.heartbeat_age(), float("inf"))
        finally:
            supervisor.HEARTBEAT = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
