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

    def _clear_conversation_from_web(self):
        self.calls.append(("_clear_conversation_from_web",))


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

    def test_four_new_html_actions_present(self):
        # Phase 4 completions: Downloads, Windows Settings, Task
        # Manager and System Information are real bridge actions.
        for key in ("downloads", "settings", "task_manager", "system_info"):
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


class PayloadPhase1Tests(unittest.TestCase):
    """Command Center Phase 1: the payload's stats block carries the
    expanded metrics additively — every pre-Phase-1 key is unchanged,
    and unavailable metrics stay None / empty so the page renders an
    honest "N/A" for them."""

    def setUp(self):
        self.app = FakeApp()

    def test_phase1_metrics_in_payload(self):
        stats = SystemStats(
            10.0, 20.0, 30.0, "Windows 11", "build", "AMD64", 60,
            network_upload_bps=1024.0, network_download_bps=2048.0,
            disk_read_bps=512.0, disk_write_bps=256.0,
            drives=({"mount": "C:\\", "percent": 61.0},
                    {"mount": "D:\\", "percent": 20.0}),
            battery={"percent": 82.0, "plugged": True},
            top_processes=({"name": "chrome", "cpu_percent": 12.0,
                            "memory_percent": 8.5},),
            own_cpu_percent=3.0, own_memory_percent=1.5)
        payload = build_state_payload(self.app, stats=stats)
        json.dumps(payload)  # must stay JSON-serializable
        block = payload["stats"]
        # Existing keys are untouched.
        self.assertEqual(block["cpu"], 10.0)
        self.assertEqual(block["ram"], 20.0)
        self.assertEqual(block["storage"], 30.0)
        self.assertEqual(block["osName"], "Windows 11")
        # New keys carry the real readings.
        self.assertEqual(block["networkUploadBps"], 1024.0)
        self.assertEqual(block["networkDownloadBps"], 2048.0)
        self.assertEqual(block["diskReadBps"], 512.0)
        self.assertEqual(block["diskWriteBps"], 256.0)
        self.assertEqual(block["drives"],
                         [{"mount": "C:\\", "percent": 61.0},
                          {"mount": "D:\\", "percent": 20.0}])
        self.assertEqual(block["battery"],
                         {"percent": 82.0, "plugged": True})
        self.assertEqual(block["topProcesses"],
                         [{"name": "chrome", "cpuPercent": 12.0,
                           "memoryPercent": 8.5}])
        self.assertEqual(block["ownCpuPercent"], 3.0)
        self.assertEqual(block["ownMemoryPercent"], 1.5)
        # No honest source on this stack: explicit None, not a value.
        self.assertIsNone(block["gpuPercent"])
        self.assertIsNone(block["temperatureC"])

    def test_phase1_defaults_are_honest_none_and_empty(self):
        stats = SystemStats(None, None, None, "—", "—", "—", None)
        payload = build_state_payload(self.app, stats=stats)
        json.dumps(payload)
        block = payload["stats"]
        self.assertIsNone(block["networkUploadBps"])
        self.assertIsNone(block["networkDownloadBps"])
        self.assertIsNone(block["diskReadBps"])
        self.assertIsNone(block["diskWriteBps"])
        self.assertEqual(block["drives"], [])
        self.assertIsNone(block["battery"])
        self.assertEqual(block["topProcesses"], [])
        self.assertIsNone(block["ownCpuPercent"])
        self.assertIsNone(block["ownMemoryPercent"])
        self.assertIsNone(block["gpuPercent"])
        self.assertIsNone(block["temperatureC"])

    def test_payload_tolerates_stats_without_phase1_fields(self):
        # A stats stand-in carrying only the original seven fields
        # must still produce the full Phase 1 key set (all None /
        # empty) instead of raising on the missing attributes.
        import types
        stats = types.SimpleNamespace(
            cpu_percent=1.0, ram_percent=2.0, storage_percent=3.0,
            os_name="—", os_build="—", machine="—", uptime_seconds=None)
        payload = build_state_payload(self.app, stats=stats)
        json.dumps(payload)
        self.assertIsNone(payload["stats"]["networkUploadBps"])
        self.assertEqual(payload["stats"]["drives"], [])
        self.assertIsNone(payload["stats"]["battery"])
        self.assertEqual(payload["stats"]["topProcesses"], [])


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
                set(entry), {"status", "mark", "command", "createdAt", "finishedAt",
                             "progress", "currentStep"})
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


def fake_snapshot(**overrides):
    snap = {
        "agents": [{"name": "Chief"}, {"name": "Coding"}],
        "models": [
            {"provider": "openai", "configured": True, "healthy": True},
            {"provider": "gemini", "configured": False, "healthy": None},
        ],
        "tasks": {"pending": 1, "running": 0, "completed": 4, "failed": 1},
        "recent_tasks": [{"id": "abcdef12", "title": "bg job",
                          "status": "completed", "attempts": 1}],
        "budget": {"limit_usd": 5.0, "spent_usd": 1.25, "calls": 7,
                   "remaining_usd": 3.75},
        "audit_tail": [{"at": 1.0, "event": "task_started", "actor": "chief"}],
    }
    snap.update(overrides)
    return snap


class PayloadPhase2Tests(unittest.TestCase):
    """Command Center Phase 2: the payload gains the Control Center
    diagnostics block, the recent-activity block and the current
    operation — all from capabilities the app object actually
    exposes, absent/honest-empty when it does not."""

    def setUp(self):
        self.app = FakeApp()
        self.stats = SystemStats(None, None, None, "—", "—", "—", None)

    def payload(self):
        result = build_state_payload(self.app, stats=self.stats)
        json.dumps(result)  # every Phase 2 block stays JSON-serializable
        return result

    def test_blocks_absent_for_minimal_app(self):
        payload = self.payload()
        self.assertIsNone(payload["controlCenter"])
        self.assertEqual(payload["activity"], [])
        self.assertIsNone(payload["currentOperation"])

    def test_control_center_from_snapshot_accessor(self):
        self.app._hud_control_center_snapshot = lambda: fake_snapshot()
        payload = self.payload()
        block = payload["controlCenter"]
        self.assertIsNotNone(block)
        self.assertEqual(block["agents"], "AGENTS  2 REGISTERED")
        self.assertEqual(block["models"], "PROVIDERS  1/2 CONFIGURED")
        self.assertEqual(block["health"], "PROVIDER HEALTH  1/1 UP")
        self.assertEqual(block["queue"],
                         "TASKS  1 QUEUED · 0 RUNNING · 4 DONE · 1 FAILED")
        self.assertEqual(block["budget"], "BUDGET  $1.25 SPENT / $5.00 CAP · 7 CALLS")
        self.assertEqual(block["memory"], "MEMORY  —")
        self.assertEqual(block["recentTasks"], ["✓ bg job"])
        self.assertEqual(block["logLines"], ["task_started · chief"])

    def test_control_center_from_panels_accessor(self):
        from orvix.hud_data import build_hud_panels
        self.app._web_hud_panels = lambda: build_hud_panels(
            fake_snapshot(), None, memory_count=3, lesson_count=9)
        payload = self.payload()
        block = payload["controlCenter"]
        self.assertIsNotNone(block)
        self.assertEqual(block["agents"], "AGENTS  2 REGISTERED")
        self.assertEqual(block["memory"], "MEMORY  3 ITEMS · 9 LESSONS")

    def test_control_center_snapshot_none_keeps_real_task_counts(self):
        # The capability exists but the snapshot could not be built:
        # the Tk HUD still shows the app-task counts and dashes for
        # the rest — the web block mirrors that exactly.
        self.app._hud_control_center_snapshot = lambda: None
        self.app.task_store = FakeTaskStore([
            {"id": "a1", "command": "Open Chrome", "status": "completed",
             "created_at": "2026-11-16T05:30:45+06:00", "finished_at": None},
        ])
        payload = self.payload()
        block = payload["controlCenter"]
        self.assertIsNotNone(block)
        self.assertIn("—", block["agents"])
        self.assertEqual(block["queue"],
                         "TASKS  0 QUEUED · 0 RUNNING · 1 DONE · 0 FAILED")

    def test_control_center_broken_snapshot_never_raises(self):
        def boom():
            raise RuntimeError("snapshot unavailable")
        self.app._hud_control_center_snapshot = boom
        payload = self.payload()
        self.assertIsNotNone(payload["controlCenter"])
        self.assertIn("—", payload["controlCenter"]["agents"])

    def test_activity_from_legacy_string_feed(self):
        self.app.recent_activity = [
            "Working on: Open Chrome",
            "Queued: YouTube kholo",
            "Waiting for your first command",
        ]
        payload = self.payload()
        # Newest-first order preserved, placeholder skipped, and no
        # timestamp is invented for the legacy string feed.
        self.assertEqual(payload["activity"], [
            {"text": "Working on: Open Chrome", "time": None},
            {"text": "Queued: YouTube kholo", "time": None},
        ])

    def test_activity_from_timestamped_accessor_keeps_times(self):
        self.app._web_activity_entries = lambda: [
            {"text": "Task queue complete", "time": "09:41"},
            {"text": "Working on: Open Chrome", "time": "09:40"},
        ]
        payload = self.payload()
        self.assertEqual(payload["activity"], [
            {"text": "Task queue complete", "time": "09:41"},
            {"text": "Working on: Open Chrome", "time": "09:40"},
        ])

    def test_activity_truncated_to_ten(self):
        self.app._web_activity_entries = lambda: [
            {"text": f"event {i}", "time": "09:41"} for i in range(15)
        ]
        payload = self.payload()
        self.assertEqual(len(payload["activity"]), 10)
        self.assertEqual(payload["activity"][0]["text"], "event 0")
        self.assertEqual(payload["activity"][-1]["text"], "event 9")

    def test_activity_broken_accessor_yields_empty(self):
        def boom():
            raise RuntimeError("feed unavailable")
        self.app._web_activity_entries = boom
        payload = self.payload()
        self.assertEqual(payload["activity"], [])

    def test_current_operation_uses_running_plan_step(self):
        self.app.task_store = FakeTaskStore([
            {"id": "a1", "command": "Build the report", "status": "running",
             "created_at": "2026-11-16T05:30:45+06:00", "finished_at": None,
             "plan": [{"title": "Gather data", "status": "completed"},
                      {"title": "Write summary", "status": "running"},
                      {"title": "Send", "status": "pending"}],
             "steps": [], "actions": []},
        ])
        payload = self.payload()
        self.assertEqual(payload["currentOperation"], {
            "text": "Write summary", "status": "running",
            "command": "Build the report"})
        # The same record's task entry carries the plan progress.
        self.assertAlmostEqual(payload["tasks"][0]["progress"], 1 / 3)
        self.assertEqual(payload["tasks"][0]["currentStep"], "Write summary")

    def test_current_operation_falls_back_to_latest_progress_event(self):
        self.app.task_store = FakeTaskStore([
            {"id": "a1", "command": "Open Chrome", "status": "running",
             "created_at": "2026-11-16T05:30:45+06:00", "finished_at": None,
             "steps": [{"time": "t1", "text": "Launching browser"}],
             "actions": []},
        ])
        payload = self.payload()
        self.assertEqual(payload["currentOperation"]["text"], "Launching browser")

    def test_current_operation_none_when_nothing_running(self):
        self.app.task_store = FakeTaskStore([
            {"id": "a1", "command": "Open Chrome", "status": "completed",
             "created_at": "2026-11-16T05:30:45+06:00",
             "finished_at": "2026-11-16T05:31:05+06:00",
             "steps": [], "actions": []},
        ])
        payload = self.payload()
        self.assertIsNone(payload["currentOperation"])
        # A completed task without a plan has no progress fraction.
        self.assertIsNone(payload["tasks"][0]["progress"])


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

    def test_pushed_messages_carry_wellformed_time(self):
        # Phase 5: every pushed message is stamped with an HH:MM
        # time (the shared ui_formatters format) at creation.
        home = WebViewHome(FakeApp())
        home.push_message("ORVIX", "timed", "assistant")
        (message,) = home.drain_messages()
        json.dumps(message)  # the stamp must stay JSON-serializable
        self.assertIn("time", message)
        match = re.fullmatch(r"(\d{2}):(\d{2})", message["time"])
        self.assertIsNotNone(match, message["time"])
        hour, minute = int(match.group(1)), int(match.group(2))
        self.assertTrue(0 <= hour <= 23)
        self.assertTrue(0 <= minute <= 59)

    def test_clear_messages_empties_buffer(self):
        home = WebViewHome(FakeApp())
        home.push_message("USER", "hello", "user")
        home.push_message("ORVIX", "hi", "assistant")
        home.clear_messages()
        self.assertEqual(home.drain_messages(), [])
        # Messages pushed after the clear are unaffected.
        home.push_message("ORVIX", "fresh start", "assistant")
        self.assertEqual(len(home.drain_messages()), 1)

    def test_buffer_stays_bounded(self):
        home = WebViewHome(FakeApp())
        for i in range(webview_home.MAX_MESSAGE_BUFFER + 50):
            home.push_message("ORVIX", f"msg {i}", "assistant")
        messages = home.drain_messages()
        self.assertEqual(len(messages), webview_home.MAX_MESSAGE_BUFFER)
        # The newest message is always kept; the oldest are dropped.
        self.assertEqual(
            messages[-1]["text"],
            f"msg {webview_home.MAX_MESSAGE_BUFFER + 49}")


class ClearConversationTests(unittest.TestCase):
    def test_api_clear_enqueues_app_clear(self):
        app = FakeApp()
        api = OrvixWebHomeApi(app)
        self.assertEqual(api.clearConversation(), {"ok": True})
        self.assertEqual(app.calls, [])  # nothing before the drain
        drain(app)
        self.assertEqual(app.calls, [("_clear_conversation_from_web",)])

    def test_bridge_clear_empties_buffer_and_keeps_task_history(self):
        # The enqueued app-side clear empties ONLY the bridge buffer:
        # the app's TaskStore records must survive untouched.
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "task_history.json")
            record = store.create_task("Notepad kholo")
            store.update_task(record["id"], status="completed", result="opened")
            app = FakeApp()
            app.task_store = store
            home = WebViewHome(app)
            app._clear_conversation_from_web = home.clear_messages
            home.push_message("YOU", "Notepad kholo", "user")
            home.push_message("ORVIX", "opened", "assistant")
            api = OrvixWebHomeApi(app)
            self.assertEqual(api.clearConversation(), {"ok": True})
            drain(app)
            self.assertEqual(home.drain_messages(), [])
            remaining = store.list_tasks()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["command"], "Notepad kholo")
            self.assertEqual(remaining[0]["status"], "completed")


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

    def test_quick_grid_has_all_ten_tiles_and_honest_power_label(self):
        # Phase 4: the web quick grid carries the four completion
        # tiles, and the power tile is labelled for what it does —
        # it opens the deterministic power options, not a free-text
        # "menu" routed to the AI model.
        for action in ("downloads", "settings", "task_manager", "system_info"):
            self.assertIn(f'data-action="{action}"', self.index_html)
        self.assertIn("Windows Settings", self.index_html)
        self.assertIn("Task Manager", self.index_html)
        self.assertIn("System Information", self.index_html)
        self.assertIn("Power Options", self.index_html)
        self.assertNotIn("Power Menu", self.index_html)

    def test_chat_messages_render_time_and_copy_button(self):
        # Phase 5: addMessage renders the bridge-stamped time next to
        # the speaker label and a per-message Copy button backed by
        # the clipboard API with an honest unavailable fallback.
        self.assertIn("m.time", self.app_js)
        self.assertIn("message-time", self.app_js)
        self.assertIn(".message-time", self.style_css)
        self.assertIn("copyMessageText", self.app_js)
        self.assertIn("copy-button", self.app_js)
        self.assertIn(".copy-button", self.style_css)
        self.assertIn("navigator.clipboard", self.app_js)
        self.assertIn("clipboard unavailable", self.app_js)

    def test_clear_conversation_control_and_cleared_hook(self):
        # Phase 5: a Clear control in the chat panel, a confirm that
        # states task history is kept, the bridge clearConversation
        # call, and the orvixChatCleared push hook that keeps both
        # UIs in sync after a clear.
        self.assertIn('id="clearChatButton"', self.index_html)
        self.assertIn("clearChatButton", self.app_js)
        self.assertIn("clearConversation", self.app_js)
        self.assertIn("Task history and settings are NOT deleted.", self.app_js)
        self.assertIn("orvixChatCleared", self.app_js)
        self.assertIn("window.confirm", self.app_js)

    def test_typing_indicator_driven_by_busy_flag(self):
        # Phase 5: the typing indicator exists in the chat panel and
        # is updated from the payload's busy flag on every state push
        # (the same flag updateChatPill already consumes).
        self.assertIn('id="typingIndicator"', self.index_html)
        self.assertIn("updateTypingIndicator", self.app_js)
        self.assertIn("updateTypingIndicator(state)", self.app_js)
        self.assertIn(".typing-indicator", self.style_css)

    def test_chat_dom_growth_is_bounded(self):
        # Phase 5 (§13): addMessage trims the oldest message nodes
        # beyond the same 200 bound as the bridge buffer.
        self.assertIn("MAX_CHAT_MESSAGES = 200", self.app_js)
        self.assertIn("trimChatLog", self.app_js)

    def test_performance_chart_label_is_accurate(self):
        # The Live Performance chart bars are real CPU/RAM samples
        # when the bridge is connected, so its aria-label must not
        # claim the graph is decorative; it names what it shows.
        self.assertIn('id="performanceChart"', self.index_html)
        self.assertNotIn("Decorative live performance graph", self.index_html)
        self.assertNotIn('aria-label="Decorative', self.index_html)
        self.assertIn(
            'aria-label="Live performance graph — CPU and RAM samples"',
            self.index_html,
        )

    def test_footer_links_point_at_real_repo_only(self):
        self.assertIn("https://github.com/zobaerio/orvixai", self.index_html)
        self.assertNotIn('aria-label="YouTube"', self.index_html)
        self.assertNotIn('aria-label="Telegram"', self.index_html)

    def test_voice_nav_entry_focuses_voice_panel(self):
        # Phase 11: Voice joins the nav as a real entry — it shows the
        # home view and scrolls/flashes the Voice Control panel (the
        # panel's own controls keep their existing wiring); it must
        # not be a dead link or a fake separate page.
        self.assertIn('data-tab="voice"', self.index_html)
        self.assertIn("focusVoicePanel", self.app_js)
        self.assertIn("scrollIntoView", self.app_js)
        self.assertIn("focus-flash", self.app_js)
        self.assertIn(".focus-flash", self.style_css)

    def test_hero_status_line_reflects_core_state(self):
        # Phase 11: the hero carries a status line whose label comes
        # from the payload's core state; the idle label is the exact
        # READY TO ASSIST wording, applied by the renderer.
        self.assertIn('id="coreStatusLabel"', self.index_html)
        self.assertIn("updateCoreStatus", self.app_js)
        self.assertIn("updateCoreStatus(state)", self.app_js)
        self.assertIn("READY TO ASSIST", self.app_js)
        self.assertIn("TASK COMPLETED", self.app_js)
        self.assertIn(".hero-status", self.style_css)

    def test_glassmorphism_and_ambient_fx_hooks(self):
        # Phase 11: true glassmorphism (backdrop blur over translucent
        # panels) and the ambient FX layers (background canvas, hero
        # reactor particle canvas, sweep/tick/orbit reactor layers).
        self.assertIn("backdrop-filter", self.style_css)
        self.assertIn('id="bgCanvas"', self.index_html)
        self.assertIn('id="reactorParticles"', self.index_html)
        self.assertIn("reactorParticles", self.app_js)
        self.assertIn("reactor ticks", self.index_html)
        self.assertIn("reactor sweep", self.index_html)
        self.assertIn(".ticks", self.style_css)
        self.assertIn(".sweep", self.style_css)
        self.assertIn(".orbit", self.style_css)

    def test_character_portraits_come_from_bridge(self):
        # Phase 11: the page requests the real portraits from the
        # bridge once after connecting; no external image URL and no
        # mockup artwork is referenced anywhere in the page files.
        self.assertIn("getCharacterPortraits", self.app_js)
        self.assertIn("requestPortraits", self.app_js)
        self.assertIn(".character-photo", self.style_css)
        self.assertNotIn("<img", self.index_html)

    def test_performance_strip_is_dual_series_equalizer(self):
        # Phase 11: the Live Performance columns carry BOTH real
        # series — CPU in front, RAM behind — from the pushed state.
        self.assertIn("perfRamData", self.app_js)
        self.assertIn("cpu-bar", self.app_js)
        self.assertIn("ram-bar", self.app_js)
        self.assertIn(".cpu-bar", self.style_css)
        self.assertIn(".ram-bar", self.style_css)

    def test_system_overview_phase1_rows(self):
        # Command Center Phase 1: the System Overview panel gains
        # network, disk-activity, battery, GPU and temperature rows
        # plus a per-drive list. GPU/temperature start as an explicit
        # "N/A" in the markup itself, and the renderer formats rates
        # from the payload (null -> "N/A") instead of inventing any.
        for element_id in ("networkDown", "networkUp", "diskRead",
                           "diskWrite", "batteryValue", "gpuValue",
                           "temperatureValue", "ownProcessValue",
                           "driveList", "systemDetails"):
            self.assertIn(f'id="{element_id}"', self.index_html)
        self.assertIn('id="gpuValue">N/A', self.index_html)
        self.assertIn('id="temperatureValue">N/A', self.index_html)
        self.assertIn('id="batteryValue">N/A', self.index_html)
        self.assertIn("updateSystemDetails", self.app_js)
        self.assertIn("updateSystemDetails(stats)", self.app_js)
        self.assertIn("fmtRate", self.app_js)
        self.assertIn("fmtBattery", self.app_js)
        self.assertIn("networkUploadBps", self.app_js)
        self.assertIn("networkDownloadBps", self.app_js)
        self.assertIn("diskReadBps", self.app_js)
        self.assertIn("gpuPercent", self.app_js)
        self.assertIn("temperatureC", self.app_js)
        self.assertIn("renderDrives", self.app_js)
        self.assertIn(".system-details", self.style_css)
        self.assertIn(".detail-row", self.style_css)
        self.assertIn(".drive-row", self.style_css)

    def test_phase2_home_density_panels(self):
        # Command Center Phase 2: centre Diagnostics list, right-
        # column Active Tasks with progress bars, a Current
        # Operation line and a Recent Activity panel — all rendered
        # from the Phase 2 payload blocks, with honest empty states
        # in the markup itself.
        for element_id in ("diagnosticsList", "diagnosticsEmpty",
                           "homeTasksList", "homeTasksEmpty",
                           "currentOperation",
                           "activityList", "activityEmpty"):
            self.assertIn(f'id="{element_id}"', self.index_html)
        self.assertIn("Diagnostics", self.index_html)
        self.assertIn("Active Tasks", self.index_html)
        self.assertIn("Recent Activity", self.index_html)
        self.assertIn("Current Operation", self.index_html)
        self.assertIn("No diagnostics available", self.index_html)
        self.assertIn("No active tasks", self.index_html)
        self.assertIn("No recent activity", self.index_html)
        self.assertIn("Idle — no task running", self.index_html)
        self.assertIn("renderDiagnostics", self.app_js)
        self.assertIn("renderHomeTasks", self.app_js)
        self.assertIn("renderActivity", self.app_js)
        self.assertIn("renderDiagnostics(state)", self.app_js)
        self.assertIn("renderHomeTasks(state)", self.app_js)
        self.assertIn("renderActivity(state)", self.app_js)
        self.assertIn("state.controlCenter", self.app_js)
        self.assertIn("state.activity", self.app_js)
        self.assertIn("state.currentOperation", self.app_js)
        self.assertIn("No recent activity", self.app_js)
        # Progress bars are driven by the payload's real fraction:
        # the renderer reads task.progress and never invents a %.
        self.assertIn("taskProgressPercent", self.app_js)
        self.assertIn("taskProgressElement", self.app_js)
        self.assertIn("task.progress", self.app_js)
        self.assertIn(".diagnostic-row", self.style_css)
        self.assertIn(".diagnostics-list", self.style_css)
        self.assertIn(".home-task-row", self.style_css)
        self.assertIn(".task-progress", self.style_css)
        self.assertIn(".task-progress-fill", self.style_css)
        self.assertIn(".current-operation", self.style_css)
        self.assertIn(".activity-row", self.style_css)
        self.assertIn(".activity-list", self.style_css)

    def test_responsive_breakpoints_rearrange_layout(self):
        # Phase 11 (§9): dedicated breakpoints rearrange the rails —
        # right rail drops below at 1250px, single-column stack at
        # 900px — instead of merely shrinking the desktop grid.
        self.assertIn("@media(max-width:1250px)", self.style_css)
        self.assertIn("@media(max-width:900px)", self.style_css)


class CharacterPortraitTests(unittest.TestCase):
    """Phase 11: the bridge resolves the owner's real reference
    portraits (same roots the desktop app scans) and hands them to
    the page as data URIs; missing files are omitted, never faked."""

    def _write(self, path, data=b"\xff\xd8\xff\xe0fake-jpeg-bytes"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def test_portraits_found_in_primary_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Orvix-AI"
            data = self._write(
                base / "assets" / "characters" / "reference" / "luna.jpg")
            result = webview_home.character_portrait_data_uris(
                {"luna": {"name": "LUNA", "image": "luna.jpg"},
                 "ruby": {"name": "RUBY", "image": "ruby.jpg"}},
                base_dir=base)
            # Only the character whose file exists is present, and the
            # data URI round-trips to the exact file bytes.
            self.assertEqual(list(result), ["luna"])
            prefix, _, encoded = result["luna"].partition(",")
            self.assertEqual(prefix, "data:image/jpeg;base64")
            import base64 as _b64
            self.assertEqual(_b64.b64decode(encoded), data)

    def test_portraits_found_in_sibling_root(self):
        # The JPGs may still live in a sibling app folder (the move
        # into this folder is a separate owner step): discovery globs
        # siblings exactly like the desktop app does.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Orvix-AI"
            base.mkdir(parents=True)
            self._write(
                Path(tmp) / "Sibling-App" / "assets" / "characters"
                / "reference" / "maya.jpg")
            result = webview_home.character_portrait_data_uris(
                {"maya": {"name": "MAYA", "image": "maya.jpg"}},
                base_dir=base)
            self.assertIn("maya", result)
            self.assertTrue(
                result["maya"].startswith("data:image/jpeg;base64,"))

    def test_missing_portraits_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Orvix-AI"
            base.mkdir(parents=True)
            result = webview_home.character_portrait_data_uris(
                {"luna": {"name": "LUNA", "image": "luna.jpg"}},
                base_dir=base)
            self.assertEqual(result, {})

    def test_api_returns_portraits_and_caches(self):
        app = FakeApp()
        api = OrvixWebHomeApi(app)
        fixed = {"luna": "data:image/jpeg;base64,AAAA"}
        with mock.patch.object(
                webview_home, "character_portrait_data_uris",
                return_value=fixed) as lookup:
            first = api.getCharacterPortraits()
            second = api.getCharacterPortraits()
        self.assertEqual(first, {"ok": True, "portraits": fixed})
        self.assertEqual(second, first)
        self.assertEqual(lookup.call_count, 1)

    def test_api_portraits_never_raises_for_minimal_app(self):
        class MinimalApp:
            events = queue.Queue()
        api = OrvixWebHomeApi(MinimalApp())
        with mock.patch.object(
                webview_home, "character_portrait_data_uris",
                side_effect=RuntimeError("boom")):
            result = api.getCharacterPortraits()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
