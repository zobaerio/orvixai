"""Headless tests for the Phase 4 power controls (orvix/automation).

The OS boundary is mocked throughout: the platform check
(``_is_windows_platform``) and the OS helpers (``_os_lock_workstation``
/ ``_os_shutdown``) are patched, so no real lock / restart / shut
down can happen on any host. The tests pin the gating contract:
lock runs immediately on Windows and refuses elsewhere; restart and
shut down run the OS call exactly once, and only after approval AND
a completed (uncancelled) countdown; every result string reflects
the mocked OS outcome — never a fake success.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import automation


def _windows():
    return patch.object(automation, "_is_windows_platform", return_value=True)


def _not_windows():
    return patch.object(automation, "_is_windows_platform", return_value=False)


class LockWorkstationTests(unittest.TestCase):
    def test_lock_invokes_os_call_on_windows(self):
        with _windows(), patch.object(
                automation, "_os_lock_workstation", return_value=True) as os_call:
            result = automation.lock_workstation()
        os_call.assert_called_once_with()
        self.assertEqual(result, "Workstation locked.")

    def test_lock_refuses_off_windows_without_os_call(self):
        with _not_windows(), patch.object(
                automation, "_os_lock_workstation") as os_call:
            result = automation.lock_workstation()
        os_call.assert_not_called()
        self.assertIn("only available on Windows", result)
        self.assertNotEqual(result, "Workstation locked.")

    def test_lock_reports_os_failure_not_success(self):
        with _windows(), patch.object(
                automation, "_os_lock_workstation", return_value=False):
            result = automation.lock_workstation()
        self.assertIn("failed", result)
        self.assertNotEqual(result, "Workstation locked.")

    def test_lock_reports_os_exception_safely(self):
        with _windows(), patch.object(
                automation, "_os_lock_workstation", side_effect=OSError("nope")):
            result = automation.lock_workstation()
        self.assertIn("failed safely", result)
        self.assertIn("was not locked", result)


class RawPowerFunctionTests(unittest.TestCase):
    def test_restart_refuses_off_windows(self):
        with _not_windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.restart_windows()
        os_call.assert_not_called()
        self.assertIn("only available on Windows", result)

    def test_shutdown_refuses_off_windows(self):
        with _not_windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.shutdown_windows()
        os_call.assert_not_called()
        self.assertIn("only available on Windows", result)

    def test_restart_uses_fixed_shutdown_args(self):
        completed = Mock(returncode=0)
        with _windows(), patch.object(
                automation.subprocess, "run", return_value=completed) as run:
            result = automation.restart_windows()
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["shutdown.exe", "/r", "/t", "0"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(result, "Restart initiated.")

    def test_shutdown_uses_fixed_shutdown_args(self):
        completed = Mock(returncode=0)
        with _windows(), patch.object(
                automation.subprocess, "run", return_value=completed) as run:
            result = automation.shutdown_windows()
        args, _kwargs = run.call_args
        self.assertEqual(args[0], ["shutdown.exe", "/s", "/t", "0"])
        self.assertEqual(result, "Shut down initiated.")

    def test_restart_reports_nonzero_os_result(self):
        completed = Mock(returncode=1)
        with _windows(), patch.object(
                automation.subprocess, "run", return_value=completed):
            result = automation.restart_windows()
        self.assertIn("failed", result)
        self.assertNotEqual(result, "Restart initiated.")


class GatedRestartShutdownTests(unittest.TestCase):
    def _approve_yes(self):
        return Mock(return_value=True)

    def _countdown_done(self):
        return Mock(return_value=True)

    def test_restart_without_approval_never_calls_os(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_restart(
                approve=None, countdown_wait=self._countdown_done())
        os_call.assert_not_called()
        self.assertIn("cancelled", result)

    def test_shutdown_without_approval_never_calls_os(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_shutdown(
                approve=None, countdown_wait=self._countdown_done())
        os_call.assert_not_called()
        self.assertIn("cancelled", result)

    def test_restart_denied_approval_never_calls_os_or_countdown(self):
        countdown = self._countdown_done()
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_restart(
                approve=Mock(return_value=False), countdown_wait=countdown)
        os_call.assert_not_called()
        countdown.assert_not_called()
        self.assertIn("approval was not given", result)

    def test_shutdown_denied_approval_never_calls_os(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_shutdown(
                approve=Mock(return_value=False),
                countdown_wait=self._countdown_done())
        os_call.assert_not_called()
        self.assertIn("approval was not given", result)

    def test_restart_without_countdown_mechanism_never_calls_os(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_restart(
                approve=self._approve_yes(), countdown_wait=None)
        os_call.assert_not_called()
        self.assertIn("cancelled", result)

    def test_restart_countdown_cancel_prevents_os_call(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_restart(
                approve=self._approve_yes(),
                countdown_wait=Mock(return_value=False))
        os_call.assert_not_called()
        self.assertIn("cancelled before it was initiated", result)

    def test_shutdown_countdown_cancel_prevents_os_call(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.request_shutdown(
                approve=self._approve_yes(),
                countdown_wait=Mock(return_value=False))
        os_call.assert_not_called()
        self.assertIn("cancelled before it was initiated", result)

    def test_restart_countdown_completion_invokes_os_exactly_once(self):
        countdown = self._countdown_done()
        with _windows(), patch.object(
                automation, "_os_shutdown", return_value=True) as os_call:
            result = automation.request_restart(
                approve=self._approve_yes(), countdown_wait=countdown)
        countdown.assert_called_once_with(automation.POWER_COUNTDOWN_SECONDS)
        os_call.assert_called_once_with(["shutdown.exe", "/r", "/t", "0"])
        self.assertEqual(result, "Restart initiated.")

    def test_shutdown_countdown_completion_invokes_os_exactly_once(self):
        countdown = self._countdown_done()
        with _windows(), patch.object(
                automation, "_os_shutdown", return_value=True) as os_call:
            result = automation.request_shutdown(
                approve=self._approve_yes(), countdown_wait=countdown)
        countdown.assert_called_once_with(automation.POWER_COUNTDOWN_SECONDS)
        os_call.assert_called_once_with(["shutdown.exe", "/s", "/t", "0"])
        self.assertEqual(result, "Shut down initiated.")

    def test_gated_result_matches_mocked_os_failure(self):
        with _windows(), patch.object(
                automation, "_os_shutdown", return_value=False) as os_call:
            result = automation.request_restart(
                approve=self._approve_yes(),
                countdown_wait=self._countdown_done())
        os_call.assert_called_once()
        self.assertIn("failed", result)
        self.assertNotEqual(result, "Restart initiated.")

    def test_gated_actions_refuse_off_windows_before_approval(self):
        approve = self._approve_yes()
        with _not_windows(), patch.object(automation, "_os_shutdown") as os_call:
            restart = automation.request_restart(
                approve=approve, countdown_wait=self._countdown_done())
            shutdown = automation.request_shutdown(
                approve=approve, countdown_wait=self._countdown_done())
        os_call.assert_not_called()
        approve.assert_not_called()
        self.assertIn("only available on Windows", restart)
        self.assertIn("only available on Windows", shutdown)


class PowerCommandRoutingTests(unittest.TestCase):
    def test_power_options_is_deterministic_and_runs_nothing(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call, \
                patch.object(automation, "_os_lock_workstation") as lock_call:
            for text in ("power options", "Power menu", "power menu"):
                result = automation.handle_power_command(text)
                self.assertIsNotNone(result)
                self.assertIn("Lock workstation", result)
                self.assertIn("Restart", result)
                self.assertIn("Shut down", result)
        os_call.assert_not_called()
        lock_call.assert_not_called()

    def test_non_power_text_is_not_a_power_command(self):
        self.assertIsNone(automation.handle_power_command("open notepad please"))
        self.assertFalse(automation.is_power_command("tell me a joke"))
        self.assertTrue(automation.is_power_command("lock workstation"))
        self.assertTrue(automation.is_power_command("restart computer"))
        self.assertTrue(automation.is_power_command("shut down computer"))

    def test_direct_command_lock_runs_immediately_on_windows(self):
        with _windows(), patch.object(
                automation, "_os_lock_workstation", return_value=True) as os_call:
            result = automation.direct_command("lock workstation")
        os_call.assert_called_once_with()
        self.assertEqual(result, "Workstation locked.")

    def test_direct_command_restart_fails_closed_without_callbacks(self):
        # direct_command with no approval / countdown callbacks (the
        # old call shape) must never run a restart.
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.direct_command("restart computer")
        os_call.assert_not_called()
        self.assertIn("cancelled", result)

    def test_direct_command_restart_with_gates_invokes_os_once(self):
        with _windows(), patch.object(
                automation, "_os_shutdown", return_value=True) as os_call:
            result = automation.direct_command(
                "restart computer",
                approve=Mock(return_value=True),
                countdown_wait=Mock(return_value=True))
        os_call.assert_called_once()
        self.assertEqual(result, "Restart initiated.")

    def test_direct_command_shutdown_countdown_cancel_blocks_os(self):
        with _windows(), patch.object(automation, "_os_shutdown") as os_call:
            result = automation.direct_command(
                "shut down computer",
                approve=Mock(return_value=True),
                countdown_wait=Mock(return_value=False))
        os_call.assert_not_called()
        self.assertIn("cancelled", result)

    def test_execute_power_action_dispatches_with_gating(self):
        with _windows(), patch.object(
                automation, "_os_shutdown", return_value=True) as os_call:
            result = automation.execute_power_action(
                "restart", approve=Mock(return_value=True),
                countdown_wait=Mock(return_value=True))
        os_call.assert_called_once()
        self.assertEqual(result, "Restart initiated.")
        self.assertEqual(
            automation.execute_power_action("something else"),
            automation.POWER_OPTIONS_MESSAGE)


class NewTileCommandRoutingTests(unittest.TestCase):
    """The four new quick-action tile commands resolve through the
    existing deterministic handlers."""

    def test_downloads_tile_command_opens_downloads(self):
        with patch.object(automation, "open_folder", return_value="Opened Downloads.") as open_folder:
            result = automation.direct_command("Downloads kholo")
        open_folder.assert_called_once_with("downloads")
        self.assertEqual(result, "Opened Downloads.")

    def test_settings_tile_command_opens_windows_settings(self):
        with patch.object(automation, "open_application", return_value="Windows Settings opened.") as open_app:
            result = automation.direct_command("Windows Settings kholo")
        open_app.assert_called_once()
        self.assertEqual(open_app.call_args[0][0].casefold(), "windows settings")
        self.assertEqual(result, "Windows Settings opened.")

    def test_task_manager_tile_command_uses_taskmgr_alias(self):
        with patch.object(automation, "open_application", return_value="Opened Task Manager.") as open_app:
            result = automation.direct_command("Task Manager kholo")
        open_app.assert_called_once()
        self.assertEqual(open_app.call_args[0][0].casefold(), "task manager")
        self.assertEqual(result, "Opened Task Manager.")
        # The alias table itself maps the name to the taskmgr binary.
        self.assertEqual(automation.APP_ALIASES["task manager"], ["taskmgr"])

    def test_system_info_tile_command_returns_system_info(self):
        result = automation.direct_command("system information")
        self.assertIn("Operating system:", result)


if __name__ == "__main__":
    unittest.main()
