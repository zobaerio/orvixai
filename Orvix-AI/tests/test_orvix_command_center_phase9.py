"""Command Center Phase 9 — persistent command bar, Tasks centre
completion, Tools cards honesty fix (audit Bugs #2).

Coverage, all headless with fakes at the established boundaries
(no Tk root, no window):

* Static web structure: the ONE composer (mic / #commandInput /
  #sendButton) lives in the bottom command bar OUTSIDE every view
  container — no duplicate ids anywhere, the chat panel no longer
  holds a composer — and the bar carries the assistant status line
  plus a Stop control whose visibility follows the payload's
  running/busy state and which calls the Phase 4 stopAll bridge
  method. The existing handlers (sendCommand -> bridge sendCommand,
  mic -> bridge startListening) are unchanged and the settled
  placeholder wording is byte-identical.
* Task entries (orvix/hud_data.build_task_entries): the Phase 9
  fields — the record id, the recorded result summary, the
  safe-diagnostics summary (counts only, from
  build_safe_diagnostics — never command/result/provider text) and
  the cancellable flag (queued / running only).
* Bridge cancelTask: enqueue-only, a missing id enqueues nothing.
* App routing (OrvixDesktop._cancel_task_from_web on a __new__
  double with a REAL TaskStore): queued -> the Task History path
  (TaskStore.cancel_queued_task + queue disarm), running active
  task -> _cancel_current_task, and finished / cancelling /
  non-active / unknown records are honest no-ops.
* Tasks view statics: progress (Phase 2), result/diagnostics lines
  and a Cancel button rendered only on cancellable entries, wired
  through a delegated listener to the bridge cancelTask method.
* Tools / Settings / About honesty: the previously overpromising
  strings are gone, the Appearance card describes only the
  animations toggle + character selection that exist, the About
  Privacy card states this app's actual behaviour, and every card
  action targets an existing view or bridge action.
"""
import queue
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.home_data import SystemStats
from orvix.hud_data import build_task_entries
from orvix.task_store import TaskStore
from orvix.webview_home import OrvixWebHomeApi, build_state_payload

APP_DIR = Path(__file__).resolve().parents[1]
WEB_HOME_DIR = APP_DIR / "web_home"


class FakeWebApp:
    """The established FakeApp pattern (minimal bridge app)."""

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
        self.cancelled_from_web = []

    def _cancel_task_from_web(self, task_id):
        self.cancelled_from_web.append(task_id)
        return True


def make_stats():
    return SystemStats(
        cpu_percent=10.0, ram_percent=20.0, storage_percent=30.0,
        os_name="Windows 11", os_build="26100", machine="AMD64",
        uptime_seconds=3600.0)


def drain(app):
    executed = 0
    while True:
        try:
            callback, args = app.events.get_nowait()
        except queue.Empty:
            return executed
        callback(*args)
        executed += 1


# ---------------------------------------------------------------------------
# Task entries: Phase 9 fields (id / result / diagnostics / cancellable)
# ---------------------------------------------------------------------------

class TaskEntryPhase9Tests(unittest.TestCase):
    @staticmethod
    def record(**extra):
        row = {"id": "t1", "command": "do the thing", "status": "completed",
               "created_at": "2026-11-16T05:30:45+06:00",
               "finished_at": "2026-11-16T05:31:05+06:00",
               "result": "", "steps": [], "actions": []}
        row.update(extra)
        return row

    def test_entry_carries_the_record_id(self):
        (entry,) = build_task_entries([self.record(id="abc123")])
        self.assertEqual(entry["id"], "abc123")

    def test_result_summary_is_the_recorded_result_collapsed(self):
        (entry,) = build_task_entries([self.record(
            result="Opened   Chrome\n successfully")])
        self.assertEqual(entry["result"], "Opened Chrome successfully")

    def test_no_result_recorded_yet_stays_none(self):
        (entry,) = build_task_entries([self.record(result="")])
        self.assertIsNone(entry["result"])
        (entry,) = build_task_entries([self.record(result=None)])
        self.assertIsNone(entry["result"])

    def test_failed_entry_carries_result_and_safe_diagnostics(self):
        (entry,) = build_task_entries([self.record(
            status="failed",
            result="Provider request failed; nothing was changed.",
            steps=[{"time": "t", "text": "started"},
                   {"time": "t", "text": "calling provider"}],
            actions=[{"name": "chat", "status": "failed"}])])
        self.assertEqual(entry["result"],
                         "Provider request failed; nothing was changed.")
        self.assertIsNotNone(entry["diagnostics"])
        self.assertIn("2 progress events", entry["diagnostics"])
        self.assertIn("1 tool action", entry["diagnostics"])
        self.assertIn("1 failed", entry["diagnostics"])

    def test_diagnostics_never_carry_record_content(self):
        secret = "hunter2-secret-command-text"
        (entry,) = build_task_entries([self.record(
            command=secret, status="failed", result=secret,
            steps=[{"time": "t", "text": secret}],
            actions=[{"name": "chat", "status": "failed"}])])
        self.assertIsNotNone(entry["diagnostics"])
        self.assertNotIn(secret, entry["diagnostics"])
        self.assertNotIn("secret", entry["diagnostics"])

    def test_record_without_events_or_actions_has_no_diagnostics(self):
        (entry,) = build_task_entries([self.record()])
        self.assertIsNone(entry["diagnostics"])

    def test_cancellable_only_for_queued_and_running(self):
        cases = {"queued": True, "running": True, "cancelling": False,
                 "completed": False, "partial": False, "failed": False,
                 "cancelled": False, "paused": False, "unknown": False}
        for status, expected in cases.items():
            with self.subTest(status=status):
                (entry,) = build_task_entries([self.record(status=status)])
                self.assertIs(entry["cancellable"], expected)

    def test_payload_tasks_carry_the_phase9_fields(self):
        app = FakeWebApp()

        class Store:
            def list_tasks(self, limit=100):
                return [{"id": "x9", "command": "Open Chrome",
                         "status": "queued",
                         "created_at": "2026-11-16T05:30:45+06:00",
                         "finished_at": None, "result": "",
                         "steps": [], "actions": []}]

        app.task_store = Store()
        payload = build_state_payload(app, stats=make_stats())
        (entry,) = payload["tasks"]
        self.assertEqual(entry["id"], "x9")
        self.assertIs(entry["cancellable"], True)
        self.assertIsNone(entry["result"])
        self.assertIsNone(entry["diagnostics"])


# ---------------------------------------------------------------------------
# Bridge: cancelTask is enqueue-only
# ---------------------------------------------------------------------------

class CancelTaskBridgeTests(unittest.TestCase):
    def test_cancel_task_enqueues_the_app_method(self):
        app = FakeWebApp()
        api = OrvixWebHomeApi(app)
        self.assertEqual(api.cancelTask("task-42"), {"ok": True})
        # Nothing ran yet — the app method is only enqueued.
        self.assertEqual(app.cancelled_from_web, [])
        self.assertEqual(drain(app), 1)
        self.assertEqual(app.cancelled_from_web, ["task-42"])

    def test_missing_task_id_enqueues_nothing(self):
        app = FakeWebApp()
        api = OrvixWebHomeApi(app)
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                result = api.cancelTask(bad)
                self.assertFalse(result["ok"])
        self.assertEqual(drain(app), 0)
        self.assertEqual(app.cancelled_from_web, [])


# ---------------------------------------------------------------------------
# App routing: _cancel_task_from_web lands on the existing paths
# ---------------------------------------------------------------------------

def make_desktop(testcase):
    try:
        import app as app_module
    except Exception as exc:  # tkinter unavailable on this runner
        testcase.skipTest(f"app module unavailable: {exc}")
    return app_module.OrvixDesktop.__new__(app_module.OrvixDesktop)


class CancelTaskRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / "history.json")
        self.desktop = make_desktop(self)
        self.desktop.task_store = self.store
        self.desktop._queue_armed = False
        self.desktop._active_task_id = None
        self.messages = []
        self.activities = []
        self.desktop._write_message = (
            lambda speaker, text, kind: self.messages.append((speaker, text, kind)))
        self.desktop._push_activity = lambda text: self.activities.append(text)
        self.current_cancel_calls = []
        self.desktop._cancel_current_task = (
            lambda: self.current_cancel_calls.append("called"))

    def test_queued_task_uses_the_task_history_cancel_path(self):
        queued = self.store.create_task("Queued command", status="queued")
        self.desktop._queue_armed = True
        result = self.desktop._cancel_task_from_web(queued["id"])
        self.assertTrue(result)
        record = self.store.get_task(queued["id"])
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("before execution", record["result"])
        # The only queued command is gone, so auto-run disarms —
        # exactly what Tk Task History's Cancel Queued does.
        self.assertFalse(self.desktop._queue_armed)
        self.assertEqual(self.current_cancel_calls, [])
        self.assertEqual(len(self.messages), 1)
        self.assertIn("cancelled before execution", self.messages[0][1])

    def test_queue_stays_armed_while_queued_commands_remain(self):
        first = self.store.create_task("First", status="queued")
        self.store.create_task("Second", status="queued")
        self.desktop._queue_armed = True
        self.assertTrue(self.desktop._cancel_task_from_web(first["id"]))
        self.assertTrue(self.desktop._queue_armed)
        self.assertEqual(self.store.queued_count(), 1)

    def test_running_active_task_uses_cancel_current_task(self):
        running = self.store.create_task("Long job")
        self.desktop._active_task_id = running["id"]
        self.assertTrue(self.desktop._cancel_task_from_web(running["id"]))
        self.assertEqual(self.current_cancel_calls, ["called"])
        # The record itself is untouched here: _cancel_current_task
        # owns the cancelling transition on its own path.
        self.assertEqual(self.store.get_task(running["id"])["status"], "running")

    def test_running_record_that_is_not_the_active_task_is_a_noop(self):
        running = self.store.create_task("Someone else's job")
        self.desktop._active_task_id = "a-different-task"
        self.assertFalse(self.desktop._cancel_task_from_web(running["id"]))
        self.assertEqual(self.current_cancel_calls, [])
        self.assertEqual(self.store.get_task(running["id"])["status"], "running")

    def test_finished_task_is_an_honest_noop(self):
        done = self.store.create_task("Finished job")
        self.store.update_task(done["id"], status="completed", result="Done")
        self.assertFalse(self.desktop._cancel_task_from_web(done["id"]))
        record = self.store.get_task(done["id"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["result"], "Done")
        self.assertEqual(self.messages, [])

    def test_cancelling_task_is_an_honest_noop(self):
        task = self.store.create_task("Stopping job")
        self.store.update_task(task["id"], status="cancelling")
        self.desktop._active_task_id = task["id"]
        self.assertFalse(self.desktop._cancel_task_from_web(task["id"]))
        self.assertEqual(self.current_cancel_calls, [])

    def test_unknown_id_and_missing_store_are_noops(self):
        self.assertFalse(self.desktop._cancel_task_from_web("no-such-id"))
        self.assertFalse(self.desktop._cancel_task_from_web(""))
        self.assertFalse(self.desktop._cancel_task_from_web(None))
        bare = make_desktop(self)
        self.assertFalse(bare._cancel_task_from_web("anything"))


# ---------------------------------------------------------------------------
# Static web checks: command bar, Tasks view, Tools/About honesty
# ---------------------------------------------------------------------------

class StaticPhase9Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")
        cls.app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")
        cls.bridge_py = (APP_DIR / "orvix" / "webview_home.py").read_text(
            encoding="utf-8")

    # -- Persistent command bar ------------------------------------
    def test_single_composer_no_duplicate_ids(self):
        for element_id in ("micButton", "commandInput", "sendButton"):
            with self.subTest(element_id=element_id):
                self.assertEqual(
                    self.index_html.count(f'id="{element_id}"'), 1)

    def test_command_bar_is_outside_every_view_container(self):
        bar_at = self.index_html.index('id="commandBar"')
        for view_id in ("homeView", "tasksView", "memoryView", "modelsView",
                        "performanceView", "coreView", "secondaryView"):
            with self.subTest(view=view_id):
                self.assertLess(self.index_html.index(f'id="{view_id}"'), bar_at)
        # The bar comes after the main dashboard closes.
        self.assertLess(self.index_html.index("</main>"), bar_at)
        # The chat panel no longer holds a composer.
        chat_panel = self.index_html.split(
            '<section class="panel chat-panel">', 1)[1].split("</section>", 1)[0]
        self.assertNotIn("commandInput", chat_panel)
        self.assertNotIn("micButton", chat_panel)
        self.assertNotIn("sendButton", chat_panel)

    def test_command_bar_holds_composer_status_and_stop(self):
        bar = self.index_html.split('id="commandBar"', 1)[1].split(
            "</section>", 1)[0]
        for fragment in ('id="micButton"', 'id="commandInput"',
                         'id="sendButton"', 'id="commandBarStatus"',
                         'id="commandStopButton"'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, bar)
        # The stop control starts hidden: nothing is running yet.
        self.assertIn('class="command-stop-button hidden"', bar)

    def test_placeholder_wording_is_the_settled_one(self):
        self.assertEqual(
            self.index_html.count(
                'placeholder="Ask ORVIX anything or type a command…"'), 1)

    def test_composer_handlers_still_call_the_existing_bridge_methods(self):
        send = re.search(r"function sendCommand\(\) \{(.*?)\n\}", self.app_js, re.S)
        self.assertIsNotNone(send)
        self.assertIn('bridgeApi.sendCommand', send.group(1))
        self.assertIn('callBridge("sendCommand", [command])', self.app_js)
        self.assertIn('$("#sendButton").addEventListener("click", sendCommand)',
                      self.app_js)
        self.assertIn('callBridge("startListening", [])', self.app_js)
        self.assertIn('callBridge("stopSpeaking", [])', self.app_js)

    def test_stop_control_visibility_follows_the_running_state(self):
        fn = re.search(r"function updateCommandBar\(state\) \{(.*?)\n\}",
                       self.app_js, re.S)
        self.assertIsNotNone(fn)
        body = fn.group(1)
        self.assertIn("state.running", body)
        self.assertIn("state.busy", body)
        self.assertIn('stop.classList.toggle("hidden", !running)', body)
        self.assertIn("stop.disabled = !running", body)
        self.assertIn('"NOT CONNECTED"', body)
        # Refreshed on every state push and on connect/disconnect.
        self.assertIn("updateCommandBar(state);", self.app_js)
        self.assertIn("updateCommandBar(lastState);", self.app_js)
        # The click calls the Phase 4 stopAll bridge method.
        handler = re.search(
            r'\$\("#commandStopButton"\)\.addEventListener\("click", '
            r"\(\) => \{(.*?)\n\}\);", self.app_js, re.S)
        self.assertIsNotNone(handler)
        self.assertIn('callBridge("stopAll", [])', handler.group(1))
        self.assertIn(".command-bar{", self.style_css)
        self.assertIn(".command-stop-button{", self.style_css)

    def test_bridge_cancel_task_is_enqueue_only(self):
        method = re.search(r"def cancelTask\(self.*?\n    def ", self.bridge_py, re.S)
        self.assertIsNotNone(method)
        self.assertIn('self._enqueue("_cancel_task_from_web", (task_id,))',
                      method.group(0))
        self.assertIn("cancel_queued_task", self.app_py)
        routing = re.search(
            r"def _cancel_task_from_web\(self.*?\n    def ", self.app_py, re.S)
        self.assertIsNotNone(routing)
        self.assertIn("store.cancel_queued_task(ident)", routing.group(0))
        self.assertIn("self._cancel_current_task()", routing.group(0))

    # -- Tasks view completion --------------------------------------
    def test_tasks_view_renders_result_diagnostics_and_cancel(self):
        self.assertIn("task.result", self.app_js)
        self.assertIn('"task-result"', self.app_js)
        self.assertIn("task.diagnostics", self.app_js)
        self.assertIn('"task-diagnostics"', self.app_js)
        self.assertIn("task.cancellable", self.app_js)
        self.assertIn('"task-cancel-button"', self.app_js)
        self.assertIn("cancel.dataset.taskId = String(task.id)", self.app_js)
        self.assertIn(".task-result{", self.style_css)
        self.assertIn(".task-diagnostics{", self.style_css)
        self.assertIn(".task-cancel-button{", self.style_css)

    def test_tasks_cancel_uses_a_delegated_bridge_call(self):
        listener = re.search(
            r'\$\("#tasksList"\)\.addEventListener\("click", '
            r"\(event\) => \{(.*?)\n\}\);", self.app_js, re.S)
        self.assertIsNotNone(listener)
        self.assertIn('callBridge("cancelTask", [taskId])', listener.group(1))

    def test_tasks_intro_no_longer_claims_read_only(self):
        self.assertNotIn("This view is read-only", self.index_html)
        self.assertIn("A queued or running task can be cancelled here",
                      self.index_html)

    # -- Tools / Settings / About honesty (Bugs #2) ------------------
    def test_previously_overpromising_strings_are_gone(self):
        for text in ("Customize theme, animations, density and assistant character",
                     "Values shown here are interface placeholders",
                     "Keep credentials in the desktop main process; do not expose API keys in frontend code.",
                     "Connect your local or cloud AI model for conversational responses.",
                     "Search, organize and manage local files (desktop bridge required).",
                     "Open websites and perform browser-based tasks."):
            with self.subTest(text=text):
                self.assertNotIn(text, self.app_js)
                self.assertNotIn(text, self.index_html)

    def test_appearance_card_describes_only_what_exists(self):
        appearance = re.search(r'\["Appearance","(.*?)"', self.app_js)
        self.assertIsNotNone(appearance)
        text = appearance.group(1)
        self.assertIn("animations toggle", text)
        self.assertIn("character selection", text)
        self.assertNotIn("Customize", text)
        # No promise of theme/density customisation: the words only
        # appear inside the honest absence statement.
        self.assertIn("there is no light theme or density setting", text)

    def test_about_privacy_card_states_the_apps_actual_behaviour(self):
        privacy = re.search(r'\["Privacy","(.*?)"\]', self.app_js)
        self.assertIsNotNone(privacy)
        text = privacy.group(1)
        self.assertIn("stored locally on this PC", text)
        self.assertIn("Windows Credential Manager via keyring", text)
        self.assertIn("never in settings.json", text)
        self.assertIn("environment variables only", text)
        self.assertIn("redact secrets", text)
        self.assertIn("counts and timings only", text)

    def test_every_card_action_targets_an_existing_destination(self):
        # Every gotoTab names a view showTab actually implements,
        # and every gotoAction is handled by the delegated listener.
        tabs = set(re.findall(r'\{tab:"(\w+)"\}', self.app_js))
        self.assertEqual(tabs, {"models", "memory", "core", "tasks",
                                "performance"})
        for tab in tabs:
            self.assertIn(f'tab === "{tab}"', self.app_js)
        actions = set(re.findall(r'\{action:"(\w+)"\}', self.app_js))
        self.assertEqual(actions, {"voice", "notifications", "settings"})
        listener = re.search(
            r'\$\("#secondaryCards"\)\.addEventListener\("click", '
            r"\(event\) => \{(.*?)\n\}\);", self.app_js, re.S)
        self.assertIsNotNone(listener)
        body = listener.group(1)
        self.assertIn("showTab(button.dataset.gotoTab)", body)
        self.assertIn("focusVoicePanel()", body)
        self.assertIn('$("#notificationsButton")', body)
        self.assertIn('callBridge("openConsole", [])', body)
        # The render loop builds the Open button from the target.
        self.assertIn('"secondary-card-action"', self.app_js)
        self.assertIn("open.dataset.gotoTab = target.tab", self.app_js)
        self.assertIn("open.dataset.gotoAction = target.action", self.app_js)
        self.assertIn(".secondary-card-action{", self.style_css)

    def test_tools_cards_cover_the_existing_centres(self):
        for title in ("Voice Assistant", "AI Models", "Memory Center",
                      "AI Core", "Tasks", "Live Performance",
                      "Notifications", "Settings"):
            with self.subTest(card=title):
                self.assertIn(f'["{title}"', self.app_js)

    def test_character_trait_lines_are_byte_unchanged(self):
        for line in ("Calm strategy companion",
                     "Confident action companion",
                     "Warm creative companion"):
            with self.subTest(line=line):
                self.assertIn(line, self.app_py)
                self.assertIn(line, self.index_html)


class NoBannedWordPhase9Tests(unittest.TestCase):
    def test_phase9_files_have_no_banned_word(self):
        # Fragments are joined at runtime so this guard does not
        # itself contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("orvix/hud_data.py", "orvix/webview_home.py",
                    "app.py", "web_home/index.html", "web_home/app.js",
                    "web_home/style.css",
                    "tests/test_orvix_command_center_phase9.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
