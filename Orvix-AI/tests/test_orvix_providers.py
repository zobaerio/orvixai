"""Tests for orvix.providers / router.

The fake transport below is a TEST DOUBLE at the HTTP boundary only; it is
never used by production code paths, which always call the real provider
APIs through urllib.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.providers import PROVIDERS, ProviderClient, ProviderNotConfigured, build_clients, configured_providers
from orvix.router import BudgetTracker, HealthMonitor, Orchestrator, Router, classify_task, complexity_score


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


class ProviderTests(unittest.TestCase):
    def test_registry_covers_all_spec_providers(self):
        for name in ("openai", "anthropic", "gemini", "deepseek", "xai", "mistral", "openrouter", "ollama"):
            self.assertIn(name, PROVIDERS)

    def test_unconfigured_provider_raises_honest_error(self):
        client = ProviderClient(PROVIDERS["openai"], env={})
        self.assertFalse(client.configured)
        with self.assertRaises(ProviderNotConfigured) as ctx:
            client.chat([{"role": "user", "content": "hi"}], "gpt-4o")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_openai_chat_parses_response(self):
        client = ProviderClient(PROVIDERS["openai"], api_key="test",
                                transport=fake_transport_factory(openai_ok))
        res = client.chat([{"role": "user", "content": "hi"}], "gpt-4o")
        self.assertEqual(res.text, "hello from model")
        self.assertEqual(res.total_tokens, 15)
        self.assertGreater(res.estimated_cost(), 0)

    def test_anthropic_chat_shape(self):
        def anthropic_ok(method, url, payload):
            self.assertIn("system", payload)
            return 200, {}, json.dumps({"content": [{"type": "text", "text": "claude says hi"}],
                                        "usage": {"input_tokens": 3, "output_tokens": 4}}).encode()
        client = ProviderClient(PROVIDERS["anthropic"], api_key="test",
                                transport=fake_transport_factory(anthropic_ok))
        res = client.chat([{"role": "system", "content": "be nice"},
                           {"role": "user", "content": "hi"}], "claude-3-5-haiku-latest")
        self.assertEqual(res.text, "claude says hi")

    def test_gemini_chat_shape(self):
        def gemini_ok(method, url, payload):
            return 200, {}, json.dumps({"candidates": [{"content": {"parts": [{"text": "gemini hi"}]}}],
                                        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2}}).encode()
        client = ProviderClient(PROVIDERS["gemini"], api_key="test",
                                transport=fake_transport_factory(gemini_ok))
        res = client.chat([{"role": "user", "content": "hi"}], "gemini-2.0-flash")
        self.assertEqual(res.text, "gemini hi")

    def test_health_false_when_unconfigured(self):
        client = ProviderClient(PROVIDERS["deepseek"], env={})
        self.assertFalse(client.health())

    def test_configured_map(self):
        cfg = configured_providers({"OPENAI_API_KEY": "x"})
        self.assertTrue(cfg["openai"])
        self.assertFalse(cfg["anthropic"])
        self.assertTrue(cfg["ollama"])  # keyless local


class RouterTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_task("please debug this python function"), "coding")
        self.assertEqual(classify_task("research and compare the sources"), "research")
        self.assertEqual(classify_task("hello there"), "chat")

    def test_complexity_bounds(self):
        self.assertGreaterEqual(complexity_score("hi"), 0)
        self.assertLessEqual(complexity_score("x" * 5000), 1.0)

    def test_router_prefers_capable_configured_model(self):
        router = Router()
        ranked = router.rank("coding", 0.5, configured={"openai": True}, health=HealthMonitor(),
                             budget_remaining=None)
        self.assertTrue(ranked)
        self.assertTrue(all(m.provider == "openai" for m in ranked))
        self.assertIn("coding", ranked[0].capabilities)

    def test_router_skips_unhealthy(self):
        health = HealthMonitor()
        for _ in range(3):
            health.record("openai", False)
        ranked = Router().rank("chat", 0.2, configured={"openai": True}, health=health, budget_remaining=None)
        self.assertEqual(ranked, [])


class OrchestratorTests(unittest.TestCase):
    def _orchestrator(self, transport, env=None):
        clients = build_clients(env=env or {"OPENAI_API_KEY": "t", "ANTHROPIC_API_KEY": "t"}, transport=transport)
        return Orchestrator(clients=clients, budget=BudgetTracker(limit_usd=10))

    def test_complete_uses_configured_provider(self):
        orch = self._orchestrator(fake_transport_factory(openai_ok))
        res = orch.complete("say hello", task_type="chat")
        self.assertEqual(res.text, "hello from model")
        self.assertEqual(orch.budget.calls, 1)

    def test_fallback_to_second_provider(self):
        def flaky(method, url, payload):
            if "openai" in url or "11434" in url:
                return 500, {}, b"server error"
            if "anthropic" in url and url.endswith("/messages"):
                return 200, {}, json.dumps({"content": [{"type": "text", "text": "fallback worked"}],
                                            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
            return 200, {}, json.dumps({"data": []}).encode()
        orch = self._orchestrator(fake_transport_factory(flaky))
        res = orch.complete("say hello", task_type="chat")
        self.assertEqual(res.text, "fallback worked")
        self.assertEqual(res.provider, "anthropic")

    def test_no_provider_configured_is_honest(self):
        from orvix.router import NoProviderConfigured
        clients = build_clients(env={}, transport=fake_transport_factory(openai_ok))
        # ollama is keyless-configured; make it unhealthy so nothing is available
        orch = Orchestrator(clients=clients, budget=BudgetTracker(limit_usd=10))
        for _ in range(3):
            orch.health.record("ollama", False)
        with self.assertRaises(NoProviderConfigured):
            orch.complete("hi", task_type="chat")

    def test_budget_never_silently_exceeded(self):
        from orvix.router import BudgetExceeded
        budget = BudgetTracker(limit_usd=0.001)
        budget.record(0.0009)
        with self.assertRaises(BudgetExceeded):
            budget.check(0.001)


if __name__ == "__main__":
    unittest.main()
