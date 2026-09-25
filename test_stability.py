"""Stability regressions; external application calls are mocked."""
import logging
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import antilagphotoshop as app
from antilagphotoshop_stability import ProcessSnapshot, ResourceWarnings, RestartSchedule


class StabilityPolicyTests(unittest.TestCase):
    def test_initial_start_and_exit_delay(self):
        schedule = RestartSchedule()
        self.assertTrue(schedule.can_attempt(0))
        schedule.observe(True, 1)
        schedule.observe(False, 2)
        self.assertFalse(schedule.can_attempt(11.9))
        self.assertTrue(schedule.can_attempt(12))
        schedule.observe(False, 13)
        self.assertTrue(schedule.can_attempt(13))

    def test_unknown_does_not_start_exit_timer(self):
        schedule = RestartSchedule()
        schedule.observe(True, 0)
        schedule.observe(None, 1)
        self.assertEqual(schedule.ready_at, 0)
        schedule.observe(False, 20)
        self.assertEqual(schedule.ready_at, 30)

    def test_attempt_delay_is_capped(self):
        schedule = RestartSchedule()
        for attempt, delay in [(1, 10), (2, 20), (3, 40), (4, 60), (10000, 60)]:
            schedule.record_attempt(100, attempt)
            self.assertEqual(schedule.ready_at, 100 + delay)

    def test_resource_check_and_notice_are_rate_limited(self):
        warnings = ResourceWarnings()
        self.assertTrue(warnings.check_due(0))
        self.assertFalse(warnings.check_due(29))
        self.assertTrue(warnings.check_due(30))
        self.assertFalse(warnings.should_notify(None, 0))
        self.assertTrue(warnings.should_notify('low RAM', 0))
        self.assertFalse(warnings.should_notify('low disk', 299))
        self.assertTrue(warnings.should_notify('low disk', 300))

    def test_snapshot_invalidates_errors_and_copies_pids(self):
        cache = ProcessSnapshot()
        self.assertIsNone(cache.recent(0))
        cache.publish({42}, 1)
        result = cache.recent(2)
        result.clear()
        self.assertEqual(cache.recent(2), {42})
        self.assertIsNone(cache.recent(7))
        cache.publish(None, 8)
        self.assertIsNone(cache.recent(8))


class StabilityAppTests(unittest.TestCase):
    def setUp(self):
        with patch.object(app, 'configure_logging', return_value=Mock(spec=logging.Logger)):
            self.guard = app.AntilagPhotoshop(auto_start=False)

    def test_successful_launch_does_not_change_window(self):
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', return_value=None), \
             patch.object(app.subprocess, 'Popen') as spawn, \
             patch.object(self.guard, '_run_async') as background:
            self.assertTrue(self.guard.launch_photoshop(manual=True))
        spawn.assert_called_once()
        background.assert_not_called()

    def test_repeated_failure_waits_without_consuming_extra_attempt(self):
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=None), \
             patch.object(app.time, 'monotonic', return_value=100), \
             patch.object(app.subprocess, 'Popen') as spawn:
            for _ in range(20):
                self.assertFalse(self.guard.launch_photoshop())
            self.assertEqual(self.guard.policy.attempts(), 1)
        spawn.assert_not_called()

    def test_manual_launch_can_bypass_cooldown(self):
        self.guard._restart_schedule.ready_at = float('inf')
        with patch.object(self.guard, 'is_photoshop_running', return_value=False), \
             patch.object(app, 'find_photoshop', return_value=Path('C:/Photoshop.exe')), \
             patch.object(self.guard, 'environment_health', return_value=None), \
             patch.object(app.subprocess, 'Popen'):
            self.assertTrue(self.guard.launch_photoshop(manual=True))

    def test_resource_warning_does_not_launch_or_terminate(self):
        with patch.object(self.guard, 'environment_health', return_value='Low RAM'), \
             patch.object(app, 'find_photoshop', return_value=None), \
             patch.object(self.guard, 'balloon') as notify, \
             patch.object(app.time, 'monotonic', return_value=100), \
             patch.object(app.subprocess, 'Popen') as spawn, \
             patch.object(app.subprocess, 'run') as run:
            self.guard._check_resources()
            self.guard._check_resources()
        notify.assert_called_once()
        spawn.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.guard.paused)

    def test_pid_lookup_reuses_successful_monitor_snapshot(self):
        output = '"Photoshop.exe","123","Console","1","12,000 K"'
        with patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')) as query:
            self.assertTrue(self.guard.is_photoshop_running())
            self.assertEqual(self.guard.photoshop_pids(), {123})
        self.assertEqual(query.call_count, 1)

    def test_failed_query_clears_pid_cache(self):
        output = '"Photoshop.exe","123","Console","1","12,000 K"'
        with patch.object(app.subprocess, 'run', side_effect=[
            subprocess.CompletedProcess([], 0, output, ''),
            subprocess.CompletedProcess([], 1, '', 'Denied'),
            subprocess.CompletedProcess([], 1, '', 'Denied'),
        ]) as query:
            self.assertTrue(self.guard.is_photoshop_running())
            self.assertIsNone(self.guard.is_photoshop_running())
            self.assertEqual(self.guard.photoshop_pids(), set())
        self.assertEqual(query.call_count, 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
