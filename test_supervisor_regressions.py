# -*- coding: utf-8 -*-
"""Регрессионные проверки supervisor.

Реальные процессы не запускаются и не завершаются.
Основной файл antilagphotoshop.py для этих тестов не требуется.
"""

import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch


def load_supervisor():
    path = Path(__file__).with_name(
        "antilagphotoshop_supervisor.pyw"
    )

    loader = importlib.machinery.SourceFileLoader(
        "supervisor_under_test",
        str(path),
    )

    spec = importlib.util.spec_from_loader(
        loader.name,
        loader,
    )

    if spec is None:
        raise RuntimeError(
            "Не удалось подготовить загрузку supervisor"
        )

    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


supervisor = load_supervisor()


class SupervisorRegressionTests(unittest.TestCase):
    def test_heartbeat_updated_during_read_is_valid(self):
        clock = [100.0]

        def read_heartbeat(**kwargs):
            clock[0] = 101.0
            return "101.0"

        heartbeat = Mock()
        heartbeat.read_text.side_effect = read_heartbeat

        with patch.object(
            supervisor,
            "HEARTBEAT",
            heartbeat,
        ), patch.object(
            supervisor.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ):
            self.assertEqual(supervisor.heartbeat_age(), 0.0)

    def test_valid_heartbeat_age(self):
        heartbeat = Mock()
        heartbeat.read_text.return_value = "80.5"

        with patch.object(supervisor, "HEARTBEAT", heartbeat):
            self.assertEqual(
                supervisor.heartbeat_age(now=100),
                19.5,
            )

    def test_invalid_heartbeat_is_rejected(self):
        heartbeat = Mock()

        with patch.object(supervisor, "HEARTBEAT", heartbeat):
            for value in (
                "nan",
                "inf",
                "-inf",
                "-1",
                "101",
                "broken",
                "",
            ):
                with self.subTest(value=value):
                    heartbeat.read_text.return_value = value

                    self.assertEqual(
                        supervisor.heartbeat_age(now=100),
                        float("inf"),
                    )

    def test_missing_heartbeat_is_rejected(self):
        heartbeat = Mock()
        heartbeat.read_text.side_effect = FileNotFoundError()

        with patch.object(supervisor, "HEARTBEAT", heartbeat):
            self.assertEqual(
                supervisor.heartbeat_age(),
                float("inf"),
            )

    def test_normal_exit_does_not_check_heartbeat(self):
        child = Mock()
        child.wait.return_value = 0

        with patch.object(
            supervisor,
            "heartbeat_age",
        ) as heartbeat, patch.object(
            supervisor,
            "stop_child",
        ) as stop:
            result = supervisor.wait_for_guard(child, Mock())

        self.assertEqual(result, 0)
        heartbeat.assert_not_called()
        stop.assert_not_called()

    def test_startup_grace_skips_heartbeat_check(self):
        child = Mock()
        child.wait.side_effect = [
            subprocess.TimeoutExpired("guard", 3),
            0,
        ]

        with patch.object(
            supervisor.time,
            "monotonic",
            side_effect=[0.0, 10.0],
        ), patch.object(
            supervisor,
            "heartbeat_age",
        ) as heartbeat, patch.object(
            supervisor,
            "stop_child",
        ) as stop:
            result = supervisor.wait_for_guard(child, Mock())

        self.assertEqual(result, 0)
        heartbeat.assert_not_called()
        stop.assert_not_called()

    def test_healthy_heartbeat_does_not_stop_process(self):
        child = Mock()
        child.wait.side_effect = [
            subprocess.TimeoutExpired("guard", 3),
            0,
        ]

        with patch.object(
            supervisor.time,
            "monotonic",
            side_effect=[0.0, 36.0],
        ), patch.object(
            supervisor,
            "heartbeat_age",
            return_value=1.0,
        ), patch.object(
            supervisor,
            "stop_child",
        ) as stop:
            result = supervisor.wait_for_guard(child, Mock())

        self.assertEqual(result, 0)
        stop.assert_not_called()

    def test_exit_after_timeout_is_not_force_stopped(self):
        child = Mock()
        child.wait.side_effect = subprocess.TimeoutExpired(
            "guard",
            3,
        )
        child.poll.return_value = 0

        with patch.object(
            supervisor.time,
            "monotonic",
            side_effect=[0.0, 36.0],
        ), patch.object(
            supervisor,
            "heartbeat_age",
            return_value=float("inf"),
        ), patch.object(
            supervisor,
            "stop_child",
        ) as stop:
            result = supervisor.wait_for_guard(child, Mock())

        self.assertEqual(result, 0)
        stop.assert_not_called()

    def test_stale_heartbeat_stops_guard(self):
        child = Mock()
        child.wait.side_effect = subprocess.TimeoutExpired(
            "guard",
            3,
        )
        child.poll.side_effect = [None, 1]
        log = Mock()

        with patch.object(
            supervisor.time,
            "monotonic",
            side_effect=[0.0, 36.0],
        ), patch.object(
            supervisor,
            "heartbeat_age",
            return_value=30.0,
        ), patch.object(
            supervisor,
            "stop_child",
            return_value=True,
        ) as stop:
            result = supervisor.wait_for_guard(child, log)

        self.assertEqual(result, 1)
        stop.assert_called_once_with(child, log)

    def test_unconfirmed_stop_aborts_monitoring(self):
        child = Mock()
        child.wait.side_effect = subprocess.TimeoutExpired(
            "guard",
            3,
        )
        child.poll.return_value = None

        with patch.object(
            supervisor.time,
            "monotonic",
            side_effect=[0.0, 36.0],
        ), patch.object(
            supervisor,
            "heartbeat_age",
            return_value=float("inf"),
        ), patch.object(
            supervisor,
            "stop_child",
            return_value=False,
        ):
            with self.assertRaises(RuntimeError):
                supervisor.wait_for_guard(child, Mock())

    def test_already_exited_process_is_not_terminated(self):
        child = Mock()
        child.poll.return_value = 0

        self.assertTrue(
            supervisor.stop_child(child, Mock())
        )

        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        child.wait.assert_not_called()

    def test_successful_terminate_does_not_call_kill(self):
        child = Mock()
        child.poll.side_effect = [None, 1]
        child.wait.return_value = 1

        self.assertTrue(
            supervisor.stop_child(child, Mock())
        )

        child.terminate.assert_called_once()
        child.kill.assert_not_called()

    def test_timeout_falls_back_to_kill(self):
        child = Mock()
        child.poll.side_effect = [None, None, 1]
        child.wait.side_effect = [
            subprocess.TimeoutExpired("guard", 5),
            1,
        ]

        self.assertTrue(
            supervisor.stop_child(child, Mock())
        )

        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    def test_termination_errors_do_not_escape(self):
        child = Mock()
        child.poll.return_value = None
        child.terminate.side_effect = OSError(
            "terminate failed"
        )
        child.kill.side_effect = OSError(
            "kill failed"
        )

        self.assertFalse(
            supervisor.stop_child(child, Mock())
        )

        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    def test_stop_timeouts_report_failure(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired(
            "guard",
            5,
        )

        self.assertFalse(
            supervisor.stop_child(child, Mock())
        )

        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    def test_restart_delay_is_bounded(self):
        expected = [
            1.0,
            2.0,
            4.0,
            8.0,
            16.0,
            30.0,
            30.0,
            30.0,
        ]

        actual = [
            supervisor.restart_delay(number)
            for number in range(1, 9)
        ]

        self.assertEqual(actual, expected)
        self.assertEqual(
            supervisor.restart_delay(100_000),
            30.0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)