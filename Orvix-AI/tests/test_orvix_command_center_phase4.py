"""Command Center Phase 4 — Emergency Stop + Take Control.

Coverage, all headless with fakes at the existing boundaries:

* ``OrvixDesktop._stop_all`` composition on a ``__new__`` desktop
  double (the pattern from test_character_voices / the cancel tests):
  each existing sub-action is invoked, the Brain queue item is
  cancelled, the summary + AuditLog entry + SYSTEM message are
  honest, and a second call is an idempotent no-op report.
* Take Control at the delegation boundary (``orvix.app_bridge``):
  with the flag ON, a delegated desktop step that would otherwise
  pass ungated now requires approval — approved runs, refused does
  not, and no gate at all fails closed. With it OFF, behaviour is
  exactly as before.
* The gate's ``take_control`` category (fail-closed like the
  sensitive categories).
* Bridge methods ``stopAll`` / ``setTakeControl`` enqueue onto
  ``app.events`` and do nothing before the drain (the established
  bridge test pattern), and the payload carries ``takeControl``.
* The app's Take Control setter persists the settings flag and
  writes the audit entry, idempotently.
* Static checks on the web Emergency zone: the confirm step exists
  and gates the bridge call, the toggle renders from the payload,
  and the cannot-undo copy is on the page.

No real audio, no real processes, no live anything: the speech
engine, the Brain queue and the audit log are test doubles, and the
provider is a fake transport at the HTTP boundary only.
"""
import ast
import json
import queue
import re
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.app_bridge import (
    BridgedAgent, DesktopOutcome, build_app_chief, build_app_orchestrator,
    take_control_enabled,
)
from orvix.agents import AGENT_SPECS
from orvix.home_data import SystemStats
from orvix.router import BudgetTracker
from orvix.security import (
    TAKE_CONTROL_CATEGORY, ConfirmationGate, ConfirmationRequired,
)
from orvix.settings import DEFAULTS
from orvix.webview_home import OrvixWebHomeApi, build_state_payload

APP_DIR = Path(__file__).resolve().parents[1]
WEB_HOME_DIR = APP_DIR / "web_home"

LOCAL_SETTINGS = {"api_base_url": "http://localhost:11434/v1",
                  "model": "qwen2.5:3b", "autonomous_brain": True}


def fake_transport_factory(responder):
    def transport(method, url, headers, body, timeout):
        payload = json.loads(body.decode()) if body else {}
        return responder(method, url, payload)
    return transport


def openai_ok(method, url, payload):
    if url.endswith("/models"):
        return 200, {}, json.dumps({"data": []}).encode()
    return 200, {}, json.dumps({
        "choices": [{"message": {"content": "hello from model"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------

class FakeAudit:
    def __init__(self):
        self.entries = []

    def log(self, event, *, actor="system", details=None):
        entry = {"event": event, "actor": actor, "details": details or {}}
        self.entries.append(entry)
        return entry


class FakeBrainQueue:
    """TaskQueue-shaped double: list(status=...) + cancel(id)."""

    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.cancelled = []

    def list(self, status=None, limit=100):
        rows = [row for row in self.rows
                if status is None or row["status"] == status]
        return [dict(row) for row in rows[:limit]]

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        for row in self.rows:
            if row["id"] == task_id:
                row["status"] = "cancelled"


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
# _stop_all composition (real method, __new__ desktop double)
# ---------------------------------------------------------------------------

def make_desktop(testcase):
    try:
        import app as app_module
    except Exception as exc:  # tkinter unavailable on this runner
        testcase.skipTest(f"app module unavailable: {exc}")
    desktop = app_module.OrvixDesktop.__new__(app_module.OrvixDesktop)
    return app_module, desktop


class StopAllCompositionTests(unittest.TestCase):
    def _desktop(self, *, busy, speaking, queue_armed, brain_rows):
        app_module, desktop = make_desktop(self)
        calls = []
        messages = []
        cancel_event = threading.Event()
        desktop._busy = busy
        desktop._active_task_cancel_event = cancel_event if busy else None
        desktop._active_task_id = "task-1" if busy else None
        desktop._queue_armed = queue_armed
        desktop.speech = SimpleNamespace(_playing=speaking)

        def fake_cancel():
            calls.append("_cancel_current_task")
            cancel_event.set()

        def fake_stop_speaking():
            calls.append("_stop_speaking")
            desktop.speech._playing = False

        desktop._cancel_current_task = fake_cancel
        desktop._stop_speaking = fake_stop_speaking
        desktop._write_message = (
            lambda speaker, text, kind: messages.append((speaker, text, kind)))
        desktop._push_activity = lambda text: calls.append(("_push_activity", text))
        brain_queue = FakeBrainQueue(brain_rows)
        audit = FakeAudit()
        desktop._hud_cc_objects = (brain_queue, None, audit, None, None)
        return desktop, calls, messages, brain_queue, audit

    def test_stop_all_composes_every_sub_action(self):
        desktop, calls, messages, brain_queue, audit = self._desktop(
            busy=True, speaking=True, queue_armed=True,
            brain_rows=[{"id": "brain-1", "status": "running"}])
        summary = desktop._stop_all()
        # Each existing sub-action was invoked, cancel before speech.
        self.assertEqual(calls[0], "_cancel_current_task")
        self.assertEqual(calls[1], "_stop_speaking")
        self.assertTrue(desktop._active_task_cancel_event.is_set())
        self.assertFalse(desktop._queue_armed)
        self.assertEqual(brain_queue.cancelled, ["brain-1"])
        # The summary reports exactly what was active.
        self.assertEqual(summary, {
            "task_active": True,
            "cancellation_requested": True,
            "speaking": True,
            "speech_stop_requested": True,
            "queue_was_armed": True,
            "queue_disarmed": True,
            "brain_running": 1,
            "brain_cancelled": 1,
            "anything_active": True,
        })
        # The user-facing message carries the honest cannot-undo limit.
        self.assertEqual(len(messages), 1)
        speaker, text, kind = messages[0]
        self.assertEqual((speaker, kind), ("SYSTEM", "system"))
        self.assertIn("Emergency Stop", text)
        self.assertIn("cannot be undone", text)
        self.assertIn("will not auto-run", text)
        # The stop was written to the shared AuditLog.
        self.assertEqual([e["event"] for e in audit.entries], ["emergency_stop"])
        self.assertEqual(audit.entries[0]["details"], summary)

    def test_stop_all_when_nothing_is_running(self):
        desktop, calls, messages, brain_queue, audit = self._desktop(
            busy=False, speaking=False, queue_armed=False, brain_rows=[])
        summary = desktop._stop_all()
        # The composed sub-actions are still invoked (they are the
        # safe no-ops in the real app), but nothing is claimed stopped.
        self.assertEqual(calls[0], "_cancel_current_task")
        self.assertEqual(calls[1], "_stop_speaking")
        self.assertFalse(summary["anything_active"])
        self.assertFalse(summary["task_active"])
        self.assertFalse(summary["cancellation_requested"])
        self.assertFalse(summary["speaking"])
        self.assertEqual(summary["brain_running"], 0)
        self.assertEqual(summary["brain_cancelled"], 0)
        self.assertEqual(brain_queue.cancelled, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("nothing was running", messages[0][1])
        self.assertIn("cannot be undone", messages[0][1])
        self.assertEqual([e["event"] for e in audit.entries], ["emergency_stop"])

    def test_stop_all_second_call_is_idempotent(self):
        desktop, calls, messages, brain_queue, audit = self._desktop(
            busy=True, speaking=True, queue_armed=True,
            brain_rows=[{"id": "brain-1", "status": "running"}])
        first = desktop._stop_all()
        second = desktop._stop_all()
        self.assertTrue(first["anything_active"])
        # After the first stop: cancel event set, speech stopped,
        # queue disarmed, Brain item cancelled — the second call
        # finds nothing active and changes nothing further.
        self.assertFalse(second["anything_active"])
        self.assertEqual(second["brain_cancelled"], 0)
        self.assertEqual(brain_queue.cancelled, ["brain-1"])
        self.assertFalse(desktop._queue_armed)
        self.assertIn("nothing was running", messages[-1][1])
        # Each press is still audited — idempotent in effect, not
        # in the audit trail.
        self.assertEqual([e["event"] for e in audit.entries],
                         ["emergency_stop", "emergency_stop"])

    def test_stop_all_disarms_queue_even_without_active_task(self):
        # _cancel_current_task only disarms on its active-task path;
        # _stop_all must disarm unconditionally so queued commands
        # cannot auto-start after an Emergency Stop.
        desktop, calls, messages, brain_queue, audit = self._desktop(
            busy=False, speaking=False, queue_armed=True, brain_rows=[])
        summary = desktop._stop_all()
        self.assertFalse(desktop._queue_armed)
        self.assertTrue(summary["queue_was_armed"])
        self.assertTrue(summary["queue_disarmed"])
        self.assertTrue(summary["anything_active"])


class StopAllSourceTests(unittest.TestCase):
    """AST wiring guards, in the style of the existing app tests."""

    @classmethod
    def setUpClass(cls):
        cls.source = (APP_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.methods = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OrvixDesktop":
                for member in node.body:
                    if isinstance(member, ast.FunctionDef):
                        cls.methods[member.name] = ast.get_source_segment(
                            cls.source, member)

    def test_stop_all_composes_existing_machinery(self):
        body = self.methods["_stop_all"]
        self.assertIn("self._cancel_current_task()", body)
        self.assertIn("self._stop_speaking()", body)
        self.assertIn("self._queue_armed = False", body)
        self.assertIn("brain_queue.cancel(", body)
        self.assertIn("cannot be undone", body)
        self.assertIn("emergency_stop", body)

    def test_run_brain_task_passes_live_take_control(self):
        body = self.methods["_run_brain_task"]
        self.assertIn("take_control", body)
        self.assertIn("take_control_enabled(self.settings)", body)
        self.assertIn("confirmer=self._brain_step_confirmer", body)

    def test_emergency_stop_button_in_tk_task_panel(self):
        self.assertIn("EMERGENCY STOP", self.source)
        self.assertIn("command=self._stop_all", self.source)


# ---------------------------------------------------------------------------
# Take Control — settings flag + gate category
# ---------------------------------------------------------------------------

class TakeControlFlagTests(unittest.TestCase):
    def test_default_off_in_settings_defaults(self):
        self.assertIn("take_control", DEFAULTS)
        self.assertIs(DEFAULTS["take_control"], False)

    def test_take_control_enabled_helper(self):
        self.assertFalse(take_control_enabled(None))
        self.assertFalse(take_control_enabled({}))
        self.assertFalse(take_control_enabled({"take_control": False}))
        self.assertTrue(take_control_enabled({"take_control": True}))

    def test_gate_take_control_category_is_fail_closed(self):
        gate = ConfirmationGate()
        with self.assertRaises(ConfirmationRequired):
            gate.check("open Notepad", TAKE_CONTROL_CATEGORY)
        approving = ConfirmationGate(confirmer=lambda a, c: True)
        self.assertTrue(approving.check("open Notepad", TAKE_CONTROL_CATEGORY))
        refusing = ConfirmationGate(confirmer=lambda a, c: False)
        with self.assertRaises(ConfirmationRequired):
            refusing.check("open Notepad", TAKE_CONTROL_CATEGORY)

    def test_gate_take_control_check_is_audited(self):
        import tempfile
        from orvix.security import AuditLog
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLog(Path(tmp) / "audit.jsonl")
            gate = ConfirmationGate(confirmer=lambda a, c: True, audit=audit)
            self.assertTrue(gate.check("open Notepad", TAKE_CONTROL_CATEGORY))
            tail = audit.tail(5)
            self.assertEqual(len(tail), 1)
            self.assertEqual(tail[0]["details"]["category"], TAKE_CONTROL_CATEGORY)
            self.assertTrue(tail[0]["details"]["approved"])

    def test_gate_other_categories_unchanged(self):
        # A non-sensitive, non-take-control category still passes
        # without a confirmer, and sensitive categories still gate.
        gate = ConfirmationGate()
        self.assertTrue(gate.check("read a file", "read"))
        with self.assertRaises(ConfirmationRequired):
            gate.check("delete all files", "destructive")


# ---------------------------------------------------------------------------
# Take Control — the delegation boundary (BridgedAgent)
# ---------------------------------------------------------------------------

class TakeControlBoundaryTests(unittest.TestCase):
    def _agent(self, desktop_execute, gate, take_control=False):
        orch = build_app_orchestrator(
            LOCAL_SETTINGS, "", include_env_providers=False,
            transport=fake_transport_factory(openai_ok),
            budget=BudgetTracker(limit_usd=10))
        spec = next(s for s in AGENT_SPECS if s.name == "Personal Assistant Agent")
        return BridgedAgent(spec, orch, desktop_execute=desktop_execute,
                            gate=gate, take_control=take_control)

    def _spy(self):
        calls = []

        def spy(task):
            calls.append(task)
            return DesktopOutcome(True, "done")
        return calls, spy

    def test_off_non_sensitive_step_passes_ungated_as_before(self):
        calls, spy = self._spy()
        confirmer_calls = []

        def confirmer(action, category):
            confirmer_calls.append((action, category))
            return False  # would refuse — must not be consulted at all

        agent = self._agent(spy, ConfirmationGate(confirmer=confirmer),
                            take_control=False)
        result = agent.run("open Notepad")
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(confirmer_calls, [])

    def test_on_forces_approval_for_otherwise_ungated_step(self):
        calls, spy = self._spy()
        confirmer_calls = []

        def confirmer(action, category):
            confirmer_calls.append((action, category))
            return False

        agent = self._agent(spy, ConfirmationGate(confirmer=confirmer),
                            take_control=True)
        result = agent.run("open Notepad")
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])  # executor never called
        self.assertEqual(len(confirmer_calls), 1)
        self.assertEqual(confirmer_calls[0][1], TAKE_CONTROL_CATEGORY)

    def test_on_approved_step_runs(self):
        calls, spy = self._spy()
        agent = self._agent(
            spy, ConfirmationGate(confirmer=lambda a, c: True),
            take_control=True)
        result = agent.run("open Notepad")
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)

    def test_on_without_gate_fails_closed(self):
        calls, spy = self._spy()
        agent = self._agent(spy, None, take_control=True)
        result = agent.run("open Notepad")
        self.assertFalse(result.ok)
        self.assertIn("Take Control", result.blocked_reason)
        self.assertEqual(calls, [])

    def test_on_sensitive_step_keeps_its_own_category(self):
        calls, spy = self._spy()
        confirmer_calls = []

        def confirmer(action, category):
            confirmer_calls.append((action, category))
            return True

        agent = self._agent(spy, ConfirmationGate(confirmer=confirmer),
                            take_control=True)
        result = agent.run("delete the file report.pdf from Desktop")
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual([c for _, c in confirmer_calls], ["destructive"])

    def test_callable_flag_is_consulted_per_step(self):
        calls, spy = self._spy()
        state = {"on": False}
        agent = self._agent(
            spy, ConfirmationGate(confirmer=lambda a, c: False),
            take_control=lambda: state["on"])
        # Off: the ungated step runs even though the confirmer refuses.
        self.assertTrue(agent.run("open Notepad").ok)
        self.assertEqual(len(calls), 1)
        # Toggled on mid-task: the very next step is forced to ask
        # and is refused, so the executor is not called again.
        state["on"] = True
        self.assertFalse(agent.run("open Notepad").ok)
        self.assertEqual(len(calls), 1)

    def test_broken_callable_fails_closed_to_on(self):
        calls, spy = self._spy()

        def broken():
            raise RuntimeError("flag unreadable")

        agent = self._agent(
            spy, ConfirmationGate(confirmer=lambda a, c: False),
            take_control=broken)
        result = agent.run("open Notepad")
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])

    def test_build_app_chief_threads_the_flag_to_agents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            chief = build_app_chief(
                LOCAL_SETTINGS, "", data_dir=Path(tmp),
                desktop_execute=lambda t: DesktopOutcome(True, "done"),
                confirmer=lambda a, c: True, take_control=True,
                transport=fake_transport_factory(openai_ok), env={})
            agents = list(chief.agents.values())
            self.assertTrue(agents)
            self.assertTrue(all(isinstance(a, BridgedAgent) for a in agents))
            self.assertTrue(all(a.take_control is True for a in agents))


# ---------------------------------------------------------------------------
# App-side Take Control setter (real method, __new__ desktop double)
# ---------------------------------------------------------------------------

class AppTakeControlSetterTests(unittest.TestCase):
    def _desktop(self):
        app_module, desktop = make_desktop(self)
        desktop.settings = {}
        desktop.messages = []
        desktop._write_message = (
            lambda speaker, text, kind: desktop.messages.append(text))
        audit = FakeAudit()
        desktop._hud_cc_objects = (None, None, audit, None, None)
        return app_module, desktop, audit

    def test_set_persists_audits_and_is_idempotent(self):
        app_module, desktop, audit = self._desktop()
        saved = []
        with mock.patch.object(app_module, "save_settings",
                               lambda data: saved.append(dict(data))):
            self.assertTrue(desktop._set_take_control_from_web(True))
            self.assertIs(desktop.settings["take_control"], True)
            self.assertEqual(len(saved), 1)
            self.assertIs(saved[0]["take_control"], True)
            # Repeating the current state saves/logs nothing again.
            self.assertTrue(desktop._set_take_control_from_web(True))
            self.assertEqual(len(saved), 1)
            self.assertFalse(desktop._set_take_control_from_web(False))
            self.assertIs(desktop.settings["take_control"], False)
            self.assertEqual(len(saved), 2)
        self.assertEqual([e["event"] for e in audit.entries],
                         ["take_control_changed", "take_control_changed"])
        self.assertEqual([e["details"] for e in audit.entries],
                         [{"enabled": True}, {"enabled": False}])
        self.assertTrue(any("Take Control is ON" in m for m in desktop.messages))
        self.assertTrue(any("Take Control is OFF" in m for m in desktop.messages))


# ---------------------------------------------------------------------------
# Bridge methods + payload (the established FakeApp pattern)
# ---------------------------------------------------------------------------

class FakeWebApp:
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
        self.calls = []

    def _stop_all(self):
        self.calls.append(("_stop_all",))
        return {}

    def _set_take_control_from_web(self, enabled=False):
        self.calls.append(("_set_take_control_from_web", enabled))
        self.settings["take_control"] = bool(enabled)
        return bool(enabled)


class BridgePhase4Tests(unittest.TestCase):
    def setUp(self):
        self.app = FakeWebApp()
        self.api = OrvixWebHomeApi(self.app)

    def test_stop_all_enqueues_app_stop_all(self):
        self.assertEqual(self.api.stopAll(), {"ok": True})
        self.assertEqual(self.app.calls, [])  # nothing before the drain
        drain(self.app)
        self.assertEqual(self.app.calls, [("_stop_all",)])

    def test_set_take_control_enqueues_with_bool(self):
        self.assertEqual(self.api.setTakeControl(True), {"ok": True})
        self.assertEqual(self.app.calls, [])
        drain(self.app)
        self.assertEqual(self.app.calls,
                         [("_set_take_control_from_web", True)])
        self.assertEqual(self.api.setTakeControl(False), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls[-1],
                         ("_set_take_control_from_web", False))

    def test_missing_app_handlers_report_not_ok(self):
        class MinimalApp:
            events = queue.Queue()

        api = OrvixWebHomeApi(MinimalApp())
        self.assertFalse(api.stopAll()["ok"])
        self.assertFalse(api.setTakeControl(True)["ok"])

    def test_payload_take_control_flag_and_running(self):
        stats = SystemStats(None, None, None, "—", "—", "—", None)
        payload = build_state_payload(self.app, stats=stats)
        json.dumps(payload)
        self.assertIs(payload["takeControl"], False)
        self.assertIs(payload["running"], False)
        self.assertIs(payload["busy"], False)
        self.app.settings["take_control"] = True
        self.app._busy = True
        payload = build_state_payload(self.app, stats=stats)
        json.dumps(payload)
        self.assertIs(payload["takeControl"], True)
        self.assertIs(payload["running"], True)
        self.assertIs(payload["busy"], True)


# ---------------------------------------------------------------------------
# Static web checks for the Emergency zone
# ---------------------------------------------------------------------------

@unittest.skipIf(
    not WEB_HOME_DIR.is_dir()
    or any(not (WEB_HOME_DIR / name).is_file()
           for name in ("index.html", "app.js", "style.css")),
    "web_home/ not fully created yet (delivered separately)")
class StaticPhase4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")

    def test_emergency_zone_controls_exist(self):
        self.assertIn('id="emergencyStopButton"', self.index_html)
        self.assertIn("EMERGENCY STOP", self.index_html)
        self.assertIn('id="takeControlToggle"', self.index_html)
        self.assertIn('id="takeControlState"', self.index_html)
        self.assertIn("Take Control", self.index_html)
        self.assertIn(".emergency-panel", self.style_css)
        self.assertIn(".emergency-stop-button", self.style_css)
        self.assertIn(".take-control-row", self.style_css)

    def test_cannot_undo_limit_is_stated_on_the_page(self):
        self.assertIn("cannot be undone", self.index_html)

    def test_stop_requires_the_in_page_confirm_step(self):
        # The confirm box exists in the markup (hidden by default),
        # the stop button's handler only reveals it, and the bridge
        # stopAll call lives in the confirm button's handler — an
        # accidental click on EMERGENCY STOP fires nothing.
        self.assertIn('id="emergencyConfirm"', self.index_html)
        self.assertIn('id="emergencyStopConfirm"', self.index_html)
        self.assertIn('id="emergencyStopCancel"', self.index_html)
        self.assertIn("emergencyStopButton", self.app_js)
        self.assertIn("emergencyStopConfirm", self.app_js)
        self.assertIn('callBridge("stopAll"', self.app_js)
        stop_handler = self.app_js.split(
            '$("#emergencyStopButton").addEventListener')[1].split("});")[0]
        self.assertNotIn("stopAll", stop_handler)
        self.assertIn("emergencyConfirm", stop_handler)
        confirm_handler = self.app_js.split(
            '$("#emergencyStopConfirm").addEventListener')[1].split("});")[0]
        self.assertIn('callBridge("stopAll"', confirm_handler)

    def test_toggle_renders_from_payload_never_local_only(self):
        # renderEmergency is driven by every state push and reads
        # the payload's takeControl value for the checked state; the
        # change handler re-renders from the last payload after
        # sending the request, instead of trusting the click.
        self.assertIn("renderEmergency", self.app_js)
        self.assertIn("renderEmergency(state)", self.app_js)
        self.assertIn("renderEmergency(null)", self.app_js)
        self.assertIn("state.takeControl", self.app_js)
        self.assertIn('callBridge("setTakeControl"', self.app_js)
        self.assertIn("renderEmergency(lastState)", self.app_js)

    def test_standalone_controls_disabled_honestly(self):
        self.assertIn("takeControlToggle", self.app_js)
        self.assertIn("disabled = !connected", self.app_js)
        self.assertIn(
            "Not connected — desktop bridge required.", self.app_js)


class NoBannedWordPhase4Tests(unittest.TestCase):
    def test_phase4_files_have_no_banned_word(self):
        # Fragments are joined at runtime so this guard does not
        # itself contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("app.py", "orvix/app_bridge.py", "orvix/security.py",
                    "orvix/settings.py", "orvix/webview_home.py",
                    "web_home/index.html", "web_home/app.js",
                    "web_home/style.css"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
