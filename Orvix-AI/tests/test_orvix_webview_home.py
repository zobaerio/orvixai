"""Headless tests for the WebView home bridge (orvix/webview_home).

No Tk root and no pywebview window are created: the API is exercised
against a FakeApp whose ``events`` queue is drained by hand, which
also proves the API never touches Tk widgets directly — nothing
happens until the queued callback is executed, exactly like the real
app draining ``events`` on the Tk thread.

The static checks on the ``web_home/`` page files skip while that
directory does not exist yet (it is delivered separately).
"""
import json
import queue
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import webview_home
from orvix.home_data import SystemStats, quick_action_tiles
from orvix.task_store import TaskStore
from orvix.version import APP_VERSION
from orvix.webview_home import (
    ACTION_COMMANDS,
    ENV_VAR,
    OrvixWebHomeApi,
    WebViewHome,
    build_state_payload,
    core_state_key,
    webview_available,
    webview_home_enabled,
)

APP_DIR = Path(__file__).resolve().parents[1]
WEB_HOME_DIR = APP_DIR / "web_home"


class FakeRoot:
    def __init__(self):
        self.calls = []
        self.deiconified = False

    def deiconify(self):
        self.calls.append("deiconify")
        self.deiconified = True

    def lift(self):
        self.calls.append("lift")

    def focus_force(self):
        self.calls.append("focus_force")


class FakeApp:
    def __init__(self):
        self.settings = {
            "model": "qwen2.5:3b",
            "api_base_url": "http://localhost:11434/v1",
        }
        self.characters = {
            "luna": {"name": "LUNA", "role": "Calm strategy companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "luna.jpg"},
            "ruby": {"name": "RUBY", "role": "Confident action companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "ruby.jpg"},
            "maya": {"name": "MAYA", "role": "Warm creative companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "maya.jpg"},
        }
        self.selected_character = "luna"
        self.core_mode = "READY"
        self.wake_enabled = False
        self._busy = False
        self.events = queue.Queue()
        self.root = FakeRoot()
        self.calls = []

    def _send_text(self, text):
        self.calls.append(("_send_text", text))

    def _listen(self):
        self.calls.append(("_listen",))

    def _select_character(self, key):
        self.calls.append(("_select_character", key))

    def _toggle_wake_word(self):
        self.calls.append(("_toggle_wake_word",))


def drain(app):
    """Execute every queued (callback, args) pair, Tk-thread style."""
    executed = 0
    while True:
        try:
            callback, args = app.events.get_nowait()
        except queue.Empty:
            return executed
        callback(*args)
        executed += 1


class EnabledLogicTests(unittest.TestCase):
    def test_env_zero_beats_setting_true(self):
        self.assertFalse(webview_home_enabled(
            {"webview_home": True}, environ={ENV_VAR: "0"}))

    def test_env_one_beats_setting_false(self):
        self.assertTrue(webview_home_enabled(
            {"webview_home": False}, environ={ENV_VAR: "1"}))

    def test_unset_env_uses_setting(self):
        self.assertTrue(webview_home_enabled(
            {"webview_home": True}, environ={}))
        self.assertFalse(webview_home_enabled(
            {"webview_home": False}, environ={}))

    def test_absent_setting_defaults_true(self):
        self.assertTrue(webview_home_enabled({}, environ={}))
        self.assertTrue(webview_home_enabled(None, environ={}))


class AvailabilityTests(unittest.TestCase):
    def test_missing_pywebview_reports_reason(self):
        with mock.patch.object(webview_home, "_webview", None):
            available, reason = webview_available()
        self.assertFalse(available)
        self.assertTrue(reason)

    def test_present_pywebview_reports_available(self):
        with mock.patch.object(webview_home, "_webview", object()):
            available, reason = webview_available()
        self.assertTrue(available)
        self.assertEqual(reason, "")


class CoreStateKeyTests(unittest.TestCase):
    def test_known_modes(self):
        cases = {
            "READY": "ready",
            "EXECUTING": "executing",
            "WAKE WORD ACTIVE": "listening",
            "CANCELLATION REQUESTED": "processing",
            "": "idle",
            "SPEAKING": "speaking",
            "ERROR": "error",
        }
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                self.assertEqual(core_state_key(mode), expected)

    def test_more_modes(self):
        self.assertEqual(core_state_key("LISTENING"), "listening")
        self.assertEqual(core_state_key("THINKING"), "thinking")
        self.assertEqual(core_state_key("COMPLETED"), "completed")
        self.assertEqual(core_state_key("TASK FAILED"), "error")
        self.assertEqual(core_state_key(None), "idle")
        self.assertEqual(core_state_key("SOMETHING ELSE"), "idle")


class ActionCommandsTests(unittest.TestCase):
    def test_files_alias_matches_explorer_command(self):
        self.assertEqual(ACTION_COMMANDS["files"], ACTION_COMMANDS["explorer"])

    def test_all_six_html_actions_present(self):
        for key in ("chrome", "youtube", "notepad", "calculator", "files", "power"):
            self.assertIn(key, ACTION_COMMANDS)

    def test_commands_equal_quick_action_tiles(self):
        tiles = {tile.key: tile.command for tile in quick_action_tiles()}
        for key, command in tiles.items():
            self.assertEqual(ACTION_COMMANDS[key], command)
        self.assertEqual(ACTION_COMMANDS["files"], tiles["explorer"])


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()

    def test_payload_shape_and_honest_stats(self):
        stats = SystemStats(None, None, 25.0, "—", "—", "x86_64", None)
        payload = build_state_payload(self.app, stats=stats)
        json.dumps(payload)  # must be JSON-serializable
        self.assertTrue(payload["connected"])
        self.assertIsNone(payload["stats"]["cpu"])
        self.assertIsNone(payload["stats"]["ram"])
        self.assertEqual(payload["stats"]["storage"], 25.0)
        self.assertEqual(payload["stats"]["uptime"], "—")
        self.assertIsNone(payload["stats"]["uptimeSeconds"])
        self.assertEqual(payload["stats"]["machine"], "x86_64")
        self.assertEqual(payload["version"], APP_VERSION)
        self.assertEqual(payload["model"]["name"], "qwen2.5:3b")
        self.assertEqual(payload["model"]["endpoint"], "http://localhost:11434/v1")
        self.assertEqual(
            [c["name"] for c in payload["characters"]],
            ["LUNA", "RUBY", "MAYA"])
        self.assertEqual(payload["character"], "luna")
        lines = payload["model"]["statusLines"]
        self.assertTrue(lines)
        by_label = {line["label"]: line for line in lines}
        self.assertEqual(by_label["INTERNET"]["value"], "Not checked")
        self.assertEqual(payload["core"]["mode"], "READY")
        self.assertEqual(payload["core"]["state"], "ready")
        self.assertFalse(payload["wakeEnabled"])
        self.assertFalse(payload["busy"])

    def test_payload_uptime_formatting(self):
        stats = SystemStats(None, None, None, "—", "—", "x86_64", 3661)
        payload = build_state_payload(self.app, stats=stats)
        self.assertEqual(payload["stats"]["uptime"], "01:01:01")
        self.assertEqual(payload["stats"]["uptimeSeconds"], 3661)

    def test_payload_fallback_characters_for_minimal_app(self):
        class MinimalApp:
            settings = {}
            core_mode = "EXECUTING"
            wake_enabled = True

        stats = SystemStats(None, None, None, "—", "—", "—", None)
        payload = build_state_payload(MinimalApp(), stats=stats)
        json.dumps(payload)
        self.assertEqual(
            [c["name"] for c in payload["characters"]],
            ["LUNA", "RUBY", "MAYA"])
        self.assertEqual(payload["core"]["state"], "executing")
        self.assertTrue(payload["wakeEnabled"])


class FakeTaskStore:
    """Minimal stand-in with TaskStore.list_tasks' newest-first shape."""

    def __init__(self, records):
        self._records = records
        self.limits = []

    def list_tasks(self, limit=100):
        self.limits.append(limit)
        return [dict(row) for row in self._records[:limit]]


class PayloadTasksTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.stats = SystemStats(None, None, None, "—", "—", "—", None)

    def test_tasks_block_shape_when_no_store(self):
        payload = build_state_payload(self.app, stats=self.stats)
        json.dumps(payload)  # must be JSON-serializable
        self.assertEqual(payload["tasks"], [])

    def test_tasks_block_empty_history(self):
        self.app.task_store = FakeTaskStore([])
        payload = build_state_payload(self.app, stats=self.stats)
        self.assertEqual(payload["tasks"], [])

    def test_tasks_block_populated_from_fake_store(self):
        self.app.task_store = FakeTaskStore([
            {"id": "a1", "command": "Open Chrome", "status": "completed",
             "created_at": "2026-11-16T05:30:45+06:00",
             "finished_at": "2026-11-16T05:31:05+06:00",
             "result": "done", "steps": [], "actions": []},
            {"id": "a2", "command": "YouTube kholo", "status": "running",
             "created_at": "2026-11-16T05:32:00+06:00",
             "finished_at": None, "result": "", "steps": [], "actions": []},
        ])
        payload = build_state_payload(self.app, stats=self.stats)
        json.dumps(payload)
        tasks = payload["tasks"]
        self.assertEqual(len(tasks), 2)
        for entry in tasks:
            self.assertEqual(
                set(entry), {"status", "mark", "command", "createdAt", "finishedAt"})
            self.assertIsInstance(entry["status"], str)
            self.assertIsInstance(entry["command"], str)
            self.assertIsInstance(entry["createdAt"], str)
        self.assertEqual(tasks[0]["status"], "completed")
        self.assertEqual(tasks[0]["command"], "Open Chrome")
        self.assertEqual(tasks[0]["createdAt"], "2026-11-16 05:30")
        self.assertEqual(tasks[0]["finishedAt"], "2026-11-16 05:31")
        self.assertEqual(tasks[1]["status"], "running")
        self.assertIsNone(tasks[1]["finishedAt"])
        # The store is asked for exactly the bounded payload count.
        self.assertEqual(self.app.task_store.limits, [webview_home.MAX_PAYLOAD_TASKS])

    def test_tasks_block_bounded_to_latest_ten(self):
        records = [
            {"id": str(i), "command": f"cmd {i}", "status": "queued",
             "created_at": "2026-11-16T05:30:45+06:00", "finished_at": None}
            for i in range(15)
        ]
        self.app.task_store = FakeTaskStore(records)
        payload = build_state_payload(self.app, stats=self.stats)
        self.assertEqual(len(payload["tasks"]), 10)

    def test_tasks_block_from_real_seeded_task_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "task_history.json")
            record = store.create_task("Notepad kholo")
            store.update_task(record["id"], status="completed", result="opened")
            store.create_task("Calculator kholo")
            self.app.task_store = store
            payload = build_state_payload(self.app, stats=self.stats)
        json.dumps(payload)
        tasks = payload["tasks"]
        self.assertEqual(len(tasks), 2)
        # list_tasks is newest-first: the still-running task leads.
        self.assertEqual(tasks[0]["command"], "Calculator kholo")
        self.assertEqual(tasks[0]["status"], "running")
        self.assertIsNone(tasks[0]["finishedAt"])
        self.assertEqual(tasks[1]["command"], "Notepad kholo")
        self.assertEqual(tasks[1]["status"], "completed")
        self.assertIsNotNone(tasks[1]["finishedAt"])
        self.assertNotEqual(tasks[1]["createdAt"], "—")

    def test_tasks_block_broken_store_yields_empty(self):
        class BrokenStore:
            def list_tasks(self, limit=100):
                raise RuntimeError("store unavailable")

        self.app.task_store = BrokenStore()
        payload = build_state_payload(self.app, stats=self.stats)
        self.assertEqual(payload["tasks"], [])


class PayloadEndpointStateTests(unittest.TestCase):
    """The model pill's source field: derived from the ENDPOINT status
    line (configuration), never from the bridge connection."""

    def setUp(self):
        self.stats = SystemStats(None, None, None, "—", "—", "—", None)

    def test_configured_endpoint(self):
        payload = build_state_payload(FakeApp(), stats=self.stats)
        self.assertEqual(payload["model"]["endpointState"], "configured")

    def test_not_configured_endpoint(self):
        app = FakeApp()
        app.settings = {}
        payload = build_state_payload(app, stats=self.stats)
        self.assertEqual(payload["model"]["endpointState"], "not-configured")

    def test_partially_configured_endpoint(self):
        app = FakeApp()
        app.settings = {"model": "qwen2.5:3b"}
        payload = build_state_payload(app, stats=self.stats)
        self.assertEqual(payload["model"]["endpointState"], "partial")


class ApiDispatchTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.api = OrvixWebHomeApi(self.app)

    def test_send_command_strips_and_enqueues(self):
        self.assertEqual(self.api.sendCommand("  hello "), {"ok": True})
        self.assertEqual(self.app.calls, [])  # nothing before the drain
        drain(self.app)
        self.assertEqual(self.app.calls, [("_send_text", "hello")])

    def test_send_command_empty_is_rejected(self):
        result = self.api.sendCommand(" ")
        self.assertFalse(result["ok"])
        self.assertTrue(self.app.events.empty())
        drain(self.app)
        self.assertEqual(self.app.calls, [])

    def test_run_action_files_alias(self):
        self.assertEqual(self.api.runAction("files"), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls, [("_send_text", "File Explorer kholo")])

    def test_run_action_unknown_is_rejected(self):
        result = self.api.runAction("bogus")
        self.assertFalse(result["ok"])
        self.assertTrue(self.app.events.empty())

    def test_select_character_case_insensitive(self):
        self.assertEqual(self.api.selectCharacter("MAYA"), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls, [("_select_character", "maya")])

    def test_select_character_unknown_profile_is_rejected(self):
        result = self.api.selectCharacter("morgan")
        self.assertFalse(result["ok"])
        self.assertTrue(self.app.events.empty())
        drain(self.app)
        self.assertEqual(self.app.calls, [])

    def test_start_listening_enqueues_listen(self):
        self.assertEqual(self.api.startListening(), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls, [("_listen",)])

    def test_set_wake_word_toggles_only_on_change(self):
        self.assertEqual(self.api.setWakeWord(True), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls, [("_toggle_wake_word",)])

    def test_set_wake_word_noop_when_already_in_state(self):
        self.assertEqual(self.api.setWakeWord(False), {"ok": True})
        self.assertTrue(self.app.events.empty())
        drain(self.app)
        self.assertEqual(self.app.calls, [])

    def test_open_console_raises_root_only_after_drain(self):
        self.assertEqual(self.api.openConsole(), {"ok": True})
        # The API thread itself must not touch the root widget.
        self.assertFalse(self.app.root.deiconified)
        self.assertEqual(self.app.root.calls, [])
        drain(self.app)
        self.assertTrue(self.app.root.deiconified)
        self.assertEqual(
            self.app.root.calls, ["deiconify", "lift", "focus_force"])


class MessageBufferTests(unittest.TestCase):
    def test_push_and_drain_in_order(self):
        home = WebViewHome(FakeApp())
        home.push_message("USER", "hello", "user")
        home.push_message("ORVIX", "hi there", "assistant")
        home.push_message("SYSTEM", "note", "system")
        messages = home.drain_messages()
        self.assertEqual(len(messages), 3)
        self.assertEqual(
            [(m["speaker"], m["text"], m["kind"]) for m in messages],
            [("USER", "hello", "user"),
             ("ORVIX", "hi there", "assistant"),
             ("SYSTEM", "note", "system")])
        self.assertEqual(home.drain_messages(), [])


_WEB_HOME_FILES = ("index.html", "app.js", "style.css")


@unittest.skipIf(
    not WEB_HOME_DIR.is_dir()
    or any(not (WEB_HOME_DIR / name).is_file() for name in _WEB_HOME_FILES),
    "web_home/ not fully created yet (delivered separately)")
class StaticWebHomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")

    def test_no_banned_brand_words_anywhere(self):
        # Fragments are joined at runtime so this guard does not itself
        # contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for name, text in (("index.html", self.index_html),
                           ("app.js", self.app_js),
                           ("style.css", self.style_css)):
            with self.subTest(file=name):
                self.assertIsNone(banned.search(text))

    def test_no_invented_character_in_page(self):
        self.assertNotIn("MORGAN", self.index_html)

    def test_no_randomized_meters(self):
        self.assertNotIn("randomizeMeters", self.app_js)

    def test_bridge_entry_points_present(self):
        self.assertIn("orvixPushState", self.app_js)
        self.assertIn("orvixPushMessages", self.app_js)

    def test_no_hardcoded_mockup_percentages(self):
        for fake in ("16%", "72%", "49%"):
            self.assertNotIn(fake, self.index_html)

    def test_stats_are_data_driven(self):
        self.assertIn('data-stat="cpu"', self.index_html)

    def test_profile_button_is_wired(self):
        # The avatar/profile button must have an id and a real handler:
        # it opens the console through the bridge, like the Settings
        # nav, instead of being a dead button.
        self.assertIn('id="profileButton"', self.index_html)
        self.assertIn("profileButton", self.app_js)
        self.assertIn("openConsole", self.app_js)

    def test_notifications_panel_collects_system_messages(self):
        # The bell toggles a real panel fed by the SYSTEM messages the
        # bridge pushes; the old canned toast must be gone.
        self.assertIn('id="notificationsPanel"', self.index_html)
        self.assertIn('id="notificationsList"', self.index_html)
        self.assertIn("No notifications yet.", self.index_html)
        self.assertIn("notificationsPanel", self.app_js)
        self.assertIn("renderNotifications", self.app_js)
        self.assertIn("isSystemMessage", self.app_js)
        self.assertNotIn('toast("No new notifications.")', self.app_js)

    def test_tasks_nav_and_view_present(self):
        # Tasks is a real nav entry backed by its own view, rendered
        # from the payload's tasks block with an honest empty state.
        self.assertIn('data-tab="tasks"', self.index_html)
        self.assertIn('id="tasksView"', self.index_html)
        self.assertIn('id="tasksList"', self.index_html)
        self.assertIn('id="tasksEmpty"', self.index_html)
        self.assertIn("No tasks yet", self.index_html)
        self.assertIn("renderTasks", self.app_js)
        self.assertIn("lastState.tasks", self.app_js)
        self.assertIn(".task-row", self.style_css)

    def test_tasks_view_standalone_honesty(self):
        # Without the bridge the Tasks view must say so, not invent
        # tasks: the not-connected wording lives in the renderer.
        self.assertIn("Not connected — desktop bridge required.", self.app_js)

    def test_chat_heading_pill_reflects_core_state(self):
        # Bug #6: the pill is driven by the payload's busy flag / core
        # mode on every push, not a hardcoded static READY label.
        self.assertIn('id="chatStatePill"', self.index_html)
        self.assertIn('id="chatStateLabel"', self.index_html)
        self.assertIn("updateChatPill", self.app_js)
        self.assertIn("state.busy", self.app_js)
        self.assertIn("updateChatPill(state)", self.app_js)

    def test_model_pill_distinguishes_bridge_from_endpoint(self):
        # Bug #17: connecting the bridge must not blanket-label the
        # model CONNECTED; the pill text comes from the payload's
        # endpoint state / ENDPOINT status line.
        self.assertNotIn('"<i></i> CONNECTED"', self.app_js)
        self.assertIn("updateModelPill", self.app_js)
        self.assertIn("endpointState", self.app_js)
        self.assertIn("ENDPOINT CONFIGURED", self.app_js)
        self.assertIn("NOT CONFIGURED", self.app_js)

    def test_footer_links_point_at_real_repo_only(self):
        self.assertIn("https://github.com/zobaerio/orvixai", self.index_html)
        self.assertNotIn('aria-label="YouTube"', self.index_html)
        self.assertNotIn('aria-label="Telegram"', self.index_html)


if __name__ == "__main__":
    unittest.main()
