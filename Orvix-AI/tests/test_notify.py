"""Tests for Premium Phase 6 — native notifications + preferences.

Covers the new ``orvix.notify`` layer:

- event -> notification mapping: each kind fires the tray icon's
  ``notify(message, title)`` with a sensible title/message when its
  toggle is enabled;
- toggle gating: a disabled kind makes no call, other kinds still
  fire;
- safety: a missing tray, a tray without a callable ``notify``, and
  a tray whose ``notify`` raises are all swallowed — no exception,
  and the return value never claims a delivery that did not happen;
- the pure gating/content helpers behave separately from the OS
  call;
- the four new settings keys round-trip through save/load and
  default to True;
- app.py wiring: notifications fire at the real finalised task
  outcome, at wake-word detection, and at the applied connection
  test result — statically checked, following the existing
  source-inspection test style (no Tk root is created).

No real tray, OS notification, or Tk root is used: the tray icon is
a fake recording its calls.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import notify as notify_mod
from orvix import settings
from orvix.notify import (
    ENDPOINT_HEALTH,
    EVENT_KINDS,
    EVENT_SETTING_KEYS,
    TASK_COMPLETED,
    TASK_FAILED,
    WAKE_ACTIVATION,
    Notifier,
    notification_content,
    notification_enabled,
    notify_event,
)

APP_DIR = Path(__file__).resolve().parents[1]

ALL_ON = {
    "notify_task_completed": True,
    "notify_task_failed": True,
    "notify_endpoint_health": True,
    "notify_wake_activation": True,
}


class FakeTrayIcon:
    """Records notify(message, title) calls like pystray's Icon."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, message, title=None):
        self.calls.append((message, title))


class RaisingTrayIcon:
    def notify(self, message, title=None):
        raise RuntimeError("tray exploded")


class NoNotifyTrayIcon:
    pass


# ---------------------------------------------------------------------------
# Pure gating decision
# ---------------------------------------------------------------------------

class GatingDecisionTests(unittest.TestCase):
    def test_each_kind_maps_to_its_own_setting_key(self):
        self.assertEqual(
            EVENT_SETTING_KEYS,
            {
                "task_completed": "notify_task_completed",
                "task_failed": "notify_task_failed",
                "endpoint_health": "notify_endpoint_health",
                "wake_activation": "notify_wake_activation",
            },
        )
        self.assertEqual(set(EVENT_KINDS), set(EVENT_SETTING_KEYS))

    def test_enabled_when_toggle_on(self):
        for kind in EVENT_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(notification_enabled(ALL_ON, kind))

    def test_disabled_only_for_the_toggled_off_kind(self):
        for kind in EVENT_KINDS:
            with self.subTest(kind=kind):
                config = dict(ALL_ON)
                config[EVENT_SETTING_KEYS[kind]] = False
                self.assertFalse(notification_enabled(config, kind))
                for other in EVENT_KINDS:
                    if other != kind:
                        self.assertTrue(notification_enabled(config, other))

    def test_missing_key_defaults_to_enabled(self):
        for kind in EVENT_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(notification_enabled({}, kind))

    def test_unknown_kind_is_never_enabled(self):
        self.assertFalse(notification_enabled(ALL_ON, "task_cancelled"))
        self.assertFalse(notification_enabled(ALL_ON, ""))
        self.assertFalse(notification_enabled(ALL_ON, "TASK_COMPLETED"))

    def test_non_dict_settings_disable_everything(self):
        for bad in (None, [], "settings", 42):
            with self.subTest(settings=bad):
                self.assertFalse(notification_enabled(bad, TASK_COMPLETED))


# ---------------------------------------------------------------------------
# Pure content mapping
# ---------------------------------------------------------------------------

class ContentMappingTests(unittest.TestCase):
    def test_default_title_and_message_per_kind(self):
        expected_titles = {
            TASK_COMPLETED: "ORVIX — Task completed",
            TASK_FAILED: "ORVIX — Task failed",
            ENDPOINT_HEALTH: "ORVIX — Endpoint health",
            WAKE_ACTIVATION: "ORVIX — Listening",
        }
        for kind, title in expected_titles.items():
            with self.subTest(kind=kind):
                content = notification_content(kind)
                self.assertIsNotNone(content)
                actual_title, message = content
                self.assertEqual(actual_title, title)
                self.assertTrue(message)

    def test_detail_becomes_the_message_first_line_only(self):
        title, message = notification_content(
            TASK_COMPLETED, "  Open Notepad  \nsecond line ignored")
        self.assertEqual(title, "ORVIX — Task completed")
        self.assertEqual(message, "Open Notepad")

    def test_blank_detail_falls_back_to_default_message(self):
        for detail in ("", "   ", "\n\n"):
            with self.subTest(detail=detail):
                _title, message = notification_content(TASK_FAILED, detail)
                self.assertIn("error", message)

    def test_long_detail_is_capped(self):
        _title, message = notification_content(TASK_FAILED, "x" * 500)
        self.assertLessEqual(len(message), 200)

    def test_unknown_kind_has_no_content(self):
        self.assertIsNone(notification_content("nope"))


# ---------------------------------------------------------------------------
# The guarded OS call (fake tray)
# ---------------------------------------------------------------------------

class NotifyEventTests(unittest.TestCase):
    def test_each_kind_fires_with_sensible_title_and_message(self):
        cases = {
            TASK_COMPLETED: ("ORVIX — Task completed", "Notepad opened"),
            TASK_FAILED: ("ORVIX — Task failed", "Provider unreachable"),
            ENDPOINT_HEALTH: ("ORVIX — Endpoint health", "Connected (HTTP 200)"),
            WAKE_ACTIVATION: ("ORVIX — Listening", "hey orvix"),
        }
        for kind, (title, detail) in cases.items():
            with self.subTest(kind=kind):
                tray = FakeTrayIcon()
                sent = notify_event(ALL_ON, tray, kind, detail=detail)
                self.assertTrue(sent)
                # pystray signature: notify(message, title).
                self.assertEqual(tray.calls, [(detail, title)])

    def test_default_message_used_without_detail(self):
        tray = FakeTrayIcon()
        self.assertTrue(notify_event(ALL_ON, tray, TASK_COMPLETED))
        message, title = tray.calls[0]
        self.assertEqual(title, "ORVIX — Task completed")
        self.assertTrue(message)

    def test_disabled_kind_makes_no_call(self):
        for kind in EVENT_KINDS:
            with self.subTest(kind=kind):
                config = dict(ALL_ON)
                config[EVENT_SETTING_KEYS[kind]] = False
                tray = FakeTrayIcon()
                self.assertFalse(notify_event(config, tray, kind, detail="x"))
                self.assertEqual(tray.calls, [])

    def test_missing_tray_makes_no_call_and_does_not_raise(self):
        for tray in (None,):
            with self.subTest(tray=tray):
                self.assertFalse(
                    notify_event(ALL_ON, tray, TASK_COMPLETED, detail="x"))

    def test_tray_without_callable_notify_is_ignored(self):
        self.assertFalse(notify_event(ALL_ON, NoNotifyTrayIcon(), TASK_COMPLETED))
        self.assertFalse(notify_event(ALL_ON, object(), TASK_COMPLETED))

        class NoneNotify:
            notify = None

        self.assertFalse(notify_event(ALL_ON, NoneNotify(), TASK_COMPLETED))

    def test_tray_raising_is_swallowed(self):
        tray = RaisingTrayIcon()
        self.assertFalse(
            notify_event(ALL_ON, tray, TASK_FAILED, detail="boom"))

    def test_unknown_kind_makes_no_call(self):
        tray = FakeTrayIcon()
        self.assertFalse(notify_event(ALL_ON, tray, "task_cancelled"))
        self.assertEqual(tray.calls, [])

    def test_non_dict_settings_make_no_call(self):
        tray = FakeTrayIcon()
        self.assertFalse(notify_event(None, tray, TASK_COMPLETED))
        self.assertEqual(tray.calls, [])


class NotifierClassTests(unittest.TestCase):
    def test_named_helpers_route_to_their_kinds(self):
        tray = FakeTrayIcon()
        notifier = Notifier(ALL_ON, tray)
        self.assertTrue(notifier.task_completed("done"))
        self.assertTrue(notifier.task_failed("bad"))
        self.assertTrue(notifier.endpoint_health("ok"))
        self.assertTrue(notifier.wake_activation("hey orvix"))
        self.assertEqual(
            [title for _message, title in tray.calls],
            [
                "ORVIX — Task completed",
                "ORVIX — Task failed",
                "ORVIX — Endpoint health",
                "ORVIX — Listening",
            ],
        )

    def test_notifier_respects_toggles_and_missing_tray(self):
        config = dict(ALL_ON, notify_task_failed=False)
        tray = FakeTrayIcon()
        notifier = Notifier(config, tray)
        self.assertTrue(notifier.enabled(TASK_COMPLETED))
        self.assertFalse(notifier.enabled(TASK_FAILED))
        self.assertFalse(notifier.task_failed("bad"))
        self.assertEqual(tray.calls, [])
        self.assertFalse(Notifier(ALL_ON, None).task_completed("done"))


# ---------------------------------------------------------------------------
# Settings defaults + round-trip
# ---------------------------------------------------------------------------

class NotifySettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.primary = self.root / "settings.json"
        self.app_dir_patch = patch.object(settings, "APP_DIR", self.root)
        self.file_patch = patch.object(settings, "SETTINGS_FILE", self.primary)
        self.app_dir_patch.start()
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)
        self.addCleanup(self.app_dir_patch.stop)

    def test_defaults_are_all_on(self):
        for key in EVENT_SETTING_KEYS.values():
            with self.subTest(key=key):
                self.assertIn(key, settings.DEFAULTS)
                self.assertIs(settings.DEFAULTS[key], True)

    def test_round_trip_each_key(self):
        for key in EVENT_SETTING_KEYS.values():
            with self.subTest(key=key):
                settings.save_settings({**settings.DEFAULTS, key: False})
                self.assertIs(settings.load_settings()[key], False)
                stored = json.loads(self.primary.read_text(encoding="utf-8"))
                self.assertIs(stored[key], False)
                settings.save_settings({**settings.DEFAULTS, key: True})
                self.assertIs(settings.load_settings()[key], True)


# ---------------------------------------------------------------------------
# app.py wiring (static, following the existing source-check style)
# ---------------------------------------------------------------------------

class NotifyWiringStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")
        cls.notify_py = (APP_DIR / "orvix" / "notify.py").read_text(encoding="utf-8")

    def test_app_imports_and_routes_through_notify_event(self):
        self.assertIn("from orvix.notify import notify_event", self.app_py)
        self.assertIn("def _notify(self, kind: str, detail: str = \"\")", self.app_py)
        self.assertIn('getattr(self, "_tray_icon", None)', self.app_py)

    def test_task_outcome_fires_at_finish_response(self):
        start = self.app_py.index("    def _finish_response(self")
        end = self.app_py.index("    def _speak_safely(self", start)
        body = self.app_py[start:end]
        self.assertIn('self._notify("task_failed"', body)
        self.assertIn('self._notify("task_completed"', body)
        # The core-state outcome lines stay: notifications decorate,
        # never replace, the existing finalisation.
        self.assertIn("CoreState.ERROR", body)
        self.assertIn("CoreState.COMPLETED", body)

    def test_wake_detection_fires_wake_activation(self):
        start = self.app_py.index("    def _wake_word_detected(self")
        end = self.app_py.index("    def _wake_listener_failed(self", start)
        body = self.app_py[start:end]
        self.assertIn('self._notify("wake_activation", heard)', body)
        self.assertIn('"Wake word detected: {heard}"', body)

    def test_connection_result_fires_endpoint_health(self):
        start = self.app_py.index("        def show_connection_result(result: dict) -> None:")
        end = self.app_py.index("        def test_connection() -> None:", start)
        body = self.app_py[start:end]
        self.assertIn('self._notify("endpoint_health"', body)

    def test_settings_dialog_has_notifications_section_and_save_path(self):
        self.assertIn('text="Notifications"', self.app_py)
        for var in (
            "notify_task_completed_var",
            "notify_task_failed_var",
            "notify_endpoint_health_var",
            "notify_wake_var",
        ):
            self.assertIn(var, self.app_py)
        for key in EVENT_SETTING_KEYS.values():
            self.assertIn(f'new["{key}"]', self.app_py)
            self.assertIn(f'self.settings.get("{key}", True)', self.app_py)

    def test_notify_module_never_imports_a_new_dependency(self):
        # The layer duck-types the tray icon: no pystray import, so
        # no new dependency and headless tests need no tray at all.
        self.assertNotIn("import pystray", self.notify_py)
        requirements = (APP_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pystray>=0.19.5", requirements)

    def test_no_polling_loop_was_invented_for_endpoint_health(self):
        # endpoint_health fires only at the connection-test result;
        # notify.py itself contains no timers, threads or polling.
        self.assertNotIn("threading", self.notify_py)
        self.assertNotIn("time.sleep", self.notify_py)
        self.assertNotIn(".after(", self.notify_py)


if __name__ == "__main__":
    unittest.main()
