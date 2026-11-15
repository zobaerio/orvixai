"""Tests for orvix.app_bridge — app -> Chief wiring.

Fake transport is a TEST DOUBLE at the HTTP boundary only.
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.app_bridge import (
    BridgedAgent, DesktopOutcome, app_brain_configured, build_app_chief,
    build_app_orchestrator, classify_desktop_action, format_brain_result,
    map_brain_status, route_decision, run_brain_command, sensitive_category,
)
from orvix.agents import AGENT_SPECS
from orvix.providers import APP_PROVIDER_NAME, build_app_client
from orvix.router import BudgetTracker, HealthMonitor, Router
from orvix.security import ConfirmationGate
from orvix.taskqueue import TaskQueue


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


LOCAL_SETTINGS = {"api_base_url": "http://localhost:11434/v1", "model": "qwen2.5:3b", "autonomous_brain": True}
REMOTE_SETTINGS = {"api_base_url": "https://api.example.com/v1", "model": "some-model", "autonomous_brain": True}


def is_complex(text):
    return "and then" in text or len(text) > 50


class BuildAppClientTests(unittest.TestCase):
    def test_chat_via_fake_transport(self):
        client, info = build_app_client("http://localhost:11434/v1/", "qwen2.5:3b", api_key="k",
                                        transport=fake_transport_factory(openai_ok))
        self.assertEqual(info.provider, "app")
        self.assertEqual(info.provider, APP_PROVIDER_NAME)
        self.assertEqual(info.model, "qwen2.5:3b")
        self.assertEqual(client.spec.base_url, "http://localhost:11434/v1")
        res = client.chat([{"role": "user", "content": "hi"}], "qwen2.5:3b")
        self.assertEqual(res.text, "hello from model")

    def test_configured_with_and_without_key(self):
        client, _ = build_app_client("http://localhost:11434/v1", "m", api_key="k",
                                     transport=fake_transport_factory(openai_ok))
        self.assertTrue(client.configured)
        client2, _ = build_app_client("http://localhost:11434/v1", "m",
                                      transport=fake_transport_factory(openai_ok))
        self.assertTrue(client2.configured)


class OrchestratorExtraModelsTests(unittest.TestCase):
    def test_complete_uses_app_provider_and_budget_counts_call_at_zero_cost(self):
        budget = BudgetTracker(limit_usd=10)
        orch = build_app_orchestrator(LOCAL_SETTINGS, "", budget=budget,
                                      include_env_providers=False,
                                      transport=fake_transport_factory(openai_ok))
        res = orch.complete("hello", task_type="chat")
        self.assertEqual(res.text, "hello from model")
        self.assertEqual(res.provider, "app")
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.spent_usd, 0.0)

    def test_router_rank_models_param(self):
        router = Router()
        self.assertEqual(router.rank("chat", 0.2, configured={"openai": True}, health=HealthMonitor(),
                                     budget_remaining=None, models=()), [])
        ranked = router.rank("chat", 0.2, configured={"openai": True}, health=HealthMonitor(),
                             budget_remaining=None)
        self.assertTrue(ranked)
        self.assertTrue(all(m.provider == "openai" for m in ranked))


class RouteDecisionTests(unittest.TestCase):
    def test_matrix(self):
        off = dict(LOCAL_SETTINGS, autonomous_brain=False)
        self.assertEqual(route_decision(off, "", "do a and then b complex thing", is_complex), "legacy")
        self.assertEqual(route_decision(LOCAL_SETTINGS, "", "hi", is_complex), "legacy")
        self.assertEqual(route_decision(LOCAL_SETTINGS, "", "research x and then open y", is_complex), "brain")
        self.assertEqual(route_decision(REMOTE_SETTINGS, "", "research x and then open y", is_complex), "brain_unconfigured")
        self.assertEqual(route_decision(REMOTE_SETTINGS, "secret-key", "research x and then open y", is_complex), "brain")


class AppBrainConfiguredTests(unittest.TestCase):
    def test_cases(self):
        ok, _ = app_brain_configured({"api_base_url": "http://localhost:11434/v1", "model": ""}, "")
        self.assertFalse(ok)
        ok, _ = app_brain_configured({"api_base_url": "http://api.example.com/v1", "model": "m"}, "key")
        self.assertFalse(ok)
        ok, _ = app_brain_configured({"api_base_url": "https://api.example.com/v1", "model": "m"}, "key")
        self.assertTrue(ok)
        ok, _ = app_brain_configured(LOCAL_SETTINGS, "")
        self.assertTrue(ok)
        ok, _ = app_brain_configured(REMOTE_SETTINGS, "")
        self.assertFalse(ok)


class ClassifyTests(unittest.TestCase):
    def test_true_cases(self):
        for text in ("open Notepad", "Create a file named notes.txt in Documents",
                     "open the Downloads folder", "Downloads folder kholo",
                     "delete the file report.pdf from Desktop",
                     "take a screenshot", "search files for report"):
            self.assertTrue(classify_desktop_action(text), text)

    def test_false_cases(self):
        for text in ("Analyse requirements and design the approach",
                     "Implement: build a website",
                     "Test the implementation and fix errors",
                     "Review quality of the final implementation",
                     "Verify the results against the original objective",
                     "Research: solar panels",
                     "Analyse and compare findings from sources",
                     "Compile the research report",
                     "Read and extract the document content: some file",
                     "Research the history of Dhaka",
                     "Explain quantum computing",
                     "Write a python function to sort a list"):
            self.assertFalse(classify_desktop_action(text), text)


class SensitiveTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(sensitive_category("delete the file report.pdf"), "destructive")
        self.assertEqual(sensitive_category("send an email to someone"), "publishing")
        self.assertIsNone(sensitive_category("open Notepad"))
        self.assertEqual(sensitive_category("change password for my account"), "account_change")
        self.assertEqual(sensitive_category("buy a ticket and checkout"), "financial")


class BridgedAgentTests(unittest.TestCase):
    def _agent(self, desktop_execute, gate, transport=None):
        orch = build_app_orchestrator(LOCAL_SETTINGS, "", include_env_providers=False,
                                       transport=fake_transport_factory(transport or openai_ok),
                                       budget=BudgetTracker(limit_usd=10))
        spec = next(s for s in AGENT_SPECS if s.name == "Personal Assistant Agent")
        return BridgedAgent(spec, orch, desktop_execute=desktop_execute, gate=gate)

    def test_sensitive_blocked_when_confirmer_false(self):
        calls = []
        def spy(task):
            calls.append(task)
            return DesktopOutcome(True, "done")
        gate = ConfirmationGate(confirmer=lambda a, c: False)
        agent = self._agent(spy, gate)
        result = agent.run("delete the file report.pdf from Desktop")
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])

    def test_sensitive_allowed_when_confirmer_true(self):
        calls = []
        def spy(task):
            calls.append(task)
            return DesktopOutcome(True, "done")
        gate = ConfirmationGate(confirmer=lambda a, c: True)
        agent = self._agent(spy, gate)
        result = agent.run("delete the file report.pdf from Desktop")
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)

    def test_sensitive_blocked_without_gate(self):
        calls = []
        def spy(task):
            calls.append(task)
            return DesktopOutcome(True, "done")
        agent = self._agent(spy, None)
        result = agent.run("delete the file report.pdf from Desktop")
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])

    def test_non_desktop_falls_through_to_orchestrator(self):
        agent = self._agent(lambda t: DesktopOutcome(True, "desktop"), ConfirmationGate(confirmer=lambda a, c: True))
        result = agent.run("Explain quantum computing")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "hello from model")

    def test_document_text_uses_local_path(self):
        calls = []
        def spy(task):
            calls.append(task)
            return DesktopOutcome(True, "desktop")
        spec = next(s for s in AGENT_SPECS if s.name == "File Management Agent")
        orch = build_app_orchestrator(LOCAL_SETTINGS, "", include_env_providers=False,
                                       transport=fake_transport_factory(openai_ok),
                                       budget=BudgetTracker(limit_usd=10))
        agent = BridgedAgent(spec, orch, desktop_execute=spy, gate=None)
        long_text = ("Solar energy is renewable and clean. " * 10) + "Solar panels convert sunlight into electricity for homes and businesses today."
        result = agent.run("summarize this", context={"document_text": long_text})
        self.assertTrue(result.ok)
        self.assertTrue(result.text)
        self.assertEqual(calls, [])


class ChiefEndToEndTests(unittest.TestCase):
    def test_research_then_open_notepad(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            desktop_calls = []
            def spy(task):
                desktop_calls.append(task)
                return DesktopOutcome(True, "opened notepad")
            budget = BudgetTracker(path=data_dir / "budget.json", limit_usd=10)
            chief = build_app_chief(LOCAL_SETTINGS, "", data_dir=data_dir, budget=budget,
                                    desktop_execute=spy, confirmer=lambda a, c: True,
                                    transport=fake_transport_factory(openai_ok), env={})
            # env providers would also be built with same fake transport; restrict by
            # using chief's orchestrator as built (app + env) — fake answers all.
            queue = TaskQueue(data_dir / "task_queue.db")
            result = run_brain_command(chief, "Research solar panels and then open Notepad", queue=queue)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(len(desktop_calls), 1, desktop_calls)
            self.assertIn("open Notepad", desktop_calls[0])
            tasks = queue.list(limit=10)
            brain_tasks = [t for t in tasks if t["title"].startswith("[Brain]")]
            self.assertTrue(brain_tasks)
            self.assertEqual(brain_tasks[0]["status"], "completed")
            self.assertGreaterEqual(budget.calls, 1)
            from orvix.security import AuditLog
            audit = AuditLog(data_dir / "audit.jsonl")
            tail = audit.tail(50)
            self.assertTrue(any(e.get("event") == "command_executed" for e in tail), tail)


class TaskQueueStartFinishTests(unittest.TestCase):
    def test_start_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = TaskQueue(Path(tmp) / "q.db")
            qid = q.enqueue("hello")
            self.assertTrue(q.start(qid))
            task = q.get(qid)
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["attempts"], 1)
            self.assertTrue(q.finish(qid, "completed", result="done result"))
            task = q.get(qid)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["result"], "done result")
            with self.assertRaises(ValueError):
                q.finish(qid, "running")
            self.assertFalse(q.start("missing-id"))


class FormatAndMapTests(unittest.TestCase):
    def test_format_includes_status_and_steps(self):
        result = {"status": "partial", "summary": "partial: 1/2 steps completed", "blockers": ["x failed"],
                  "steps": [{"title": "Do thing", "agent": "Research Agent", "status": "completed", "result": "ok result", "error": ""},
                            {"title": "Other", "agent": "Coding Agent", "status": "failed", "result": "", "error": "boom"}]}
        text = format_brain_result(result)
        self.assertIn("partial", text)
        self.assertIn("Do thing", text)
        self.assertIn("Other", text)

    def test_map(self):
        self.assertEqual(map_brain_status("blocked"), "failed")
        self.assertEqual(map_brain_status("completed"), "completed")
        self.assertEqual(map_brain_status("partial"), "partial")
        self.assertEqual(map_brain_status("failed"), "failed")


class AppWiringAstTests(unittest.TestCase):
    def test_work_contains_route_and_brain_calls(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        work_src = None
        settings_src = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OrvixDesktop":
                for member in node.body:
                    if isinstance(member, ast.FunctionDef) and member.name == "_work":
                        work_src = ast.get_source_segment(app_path.read_text(encoding="utf-8"), member)
                    if isinstance(member, ast.FunctionDef) and member.name == "_open_settings":
                        settings_src = ast.get_source_segment(app_path.read_text(encoding="utf-8"), member)
        self.assertIsNotNone(work_src)
        self.assertIn("route_decision", work_src)
        self.assertIn("_run_brain_task", work_src)
        self.assertIsNotNone(settings_src)
        self.assertIn("autonomous_brain", settings_src)

    def test_settings_defaults(self):
        from orvix.settings import DEFAULTS
        self.assertIn("autonomous_brain", DEFAULTS)
        self.assertIs(DEFAULTS["autonomous_brain"], False)


if __name__ == "__main__":
    unittest.main()
