"""Command Center Phase 6 — Model Management Center.

Coverage, all headless with fakes at the existing boundaries:

* The Brain registry gains Groq (Bugs #4): OpenAI-compatible kind,
  GROQ_API_KEY env key, the real Groq base URL (the same base the
  Settings auto-setup already used for the desktop endpoint), and
  a small set of well-known Groq-hosted models — and the router /
  orchestrator can select it when a fake env configures it.
* New settings ``temperature`` (default 0.4, clamped 0.0–2.0) and
  ``max_response_tokens`` (default 1024, clamped 1–8192): defaults,
  clamping, bad values rejected by the validators, and the
  never-raising effective_* resolvers falling back to defaults.
* The desktop agent's request payload carries the settings values
  (fake transport capturing the payload); vision/planner call
  sites are untouched (their 0.2 stays in their own modules).
* The payload's ``providers`` block: registry shape, configured
  flags from a FAKE env (booleans only — no key value ever appears
  in the serialised payload), health starting at "not_checked",
  and a recorded check result flowing into the next payload.
* The ``testProvider`` bridge method: enqueue discipline, unknown
  providers rejected, and the app-side handler running a FAKE
  client's health() and recording reachable / unreachable.
* Settings > AI Model tab wiring (source-level, following the
  existing settings test patterns) + a settings round-trip.
* Static web checks for the Models page: nav destination, view,
  provider cards, capability chips, Test buttons wired to
  testProvider, the desktop-endpoint section pointing at Settings,
  honest not-checked states, and no key-like strings anywhere.

No live provider calls, no real env keys, no Tk root, no window.
"""
import json
import queue
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import settings as settings_module
from orvix.agent import OrvixAgent
from orvix.home_data import SystemStats
from orvix.providers import (
    PROVIDERS,
    ProviderClient,
    configured_providers,
)
from orvix.router import HealthMonitor, Orchestrator, Router
from orvix.webview_home import OrvixWebHomeApi, build_state_payload

APP_DIR = Path(__file__).resolve().parents[1]
WEB_HOME_DIR = APP_DIR / "web_home"
STATS = SystemStats(None, None, None, "—", "—", "—", None)

FAKE_ENV = {"GROQ_API_KEY": "fake-groq-key-for-tests", "OPENAI_API_KEY": "fake-openai-key"}


def drain(app):
    executed = 0
    while True:
        try:
            callback, args = app.events.get_nowait()
        except queue.Empty:
            return executed
        callback(*args)
        executed += 1


class FakeWebApp:
    """The established FakeApp pattern, plus the Phase 6 provider
    health accessor and the app-side test handler recording calls
    and simulating a completed check the way the real handler does
    (it records the verdict the next payload then reports)."""

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
        self.provider_health_results = {}

    def _web_provider_health(self):
        return dict(self.provider_health_results)

    def _test_provider_from_web(self, provider_id):
        self.calls.append(("_test_provider_from_web", provider_id))
        # Simulate the real handler's recorded verdict for the one
        # provider this fake "checks" successfully.
        self.provider_health_results[provider_id] = (
            "reachable" if provider_id == "ollama" else "unreachable")
        return provider_id == "ollama"


# ---------------------------------------------------------------------------
# Groq in the Brain registry (Bugs #4)
# ---------------------------------------------------------------------------

class GroqRegistryTests(unittest.TestCase):
    def test_groq_spec_matches_desktop_autosetup_world(self):
        self.assertIn("groq", PROVIDERS)
        spec = PROVIDERS["groq"]
        self.assertEqual(spec.name, "groq")
        # OpenAI-compatible kind, like deepseek / xai / mistral.
        self.assertEqual(spec.kind, "openai")
        self.assertEqual(spec.env_key, "GROQ_API_KEY")
        # The same base URL Settings auto-setup configures for the
        # desktop endpoint, so both model worlds agree.
        self.assertEqual(spec.base_url, "https://api.groq.com/openai/v1")
        source = (APP_DIR / "orvix" / "settings.py").read_text(encoding="utf-8")
        self.assertIn('"https://api.groq.com/openai/v1"', source)

    def test_groq_models_are_well_known_with_capabilities(self):
        spec = PROVIDERS["groq"]
        names = [m.model for m in spec.models]
        self.assertIn("llama-3.3-70b-versatile", names)
        self.assertIn("llama-3.1-8b-instant", names)
        self.assertIn("openai/gpt-oss-120b", names)
        for info in spec.models:
            self.assertEqual(info.provider, "groq")
            self.assertTrue(info.capabilities)
            self.assertIn("chat", info.capabilities)
            self.assertGreater(info.context_tokens, 0)

    def test_groq_configured_only_with_its_env_key(self):
        cfg = configured_providers(FAKE_ENV)
        self.assertTrue(cfg["groq"])
        self.assertFalse(configured_providers({})["groq"])
        client = ProviderClient(PROVIDERS["groq"], env=FAKE_ENV)
        self.assertTrue(client.configured)
        self.assertFalse(ProviderClient(PROVIDERS["groq"], env={}).configured)

    def test_groq_health_uses_models_endpoint_via_fake_transport(self):
        seen = {}

        def transport(method, url, headers, body, timeout):
            seen["method"] = method
            seen["url"] = url
            return 200, {}, json.dumps({"data": []}).encode()

        client = ProviderClient(PROVIDERS["groq"], env=FAKE_ENV, transport=transport)
        self.assertTrue(client.health())
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], "https://api.groq.com/openai/v1/models")

    def test_router_can_select_groq_when_configured(self):
        ranked = Router().rank(
            "chat", 0.5, configured=configured_providers(FAKE_ENV),
            health=HealthMonitor(), budget_remaining=None)
        groq_models = [m for m in ranked if m.provider == "groq"]
        self.assertTrue(groq_models)
        self.assertTrue(all(m.provider in ("groq", "openai", "ollama")
                            for m in ranked))

    def test_orchestrator_completes_via_groq_with_fake_transport(self):
        def groq_ok(method, url, payload):
            if url.endswith("/models"):
                return 200, {}, json.dumps({"data": []}).encode()
            return 200, {}, json.dumps({
                "choices": [{"message": {"content": "hello from groq"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3}}).encode()

        def transport(method, url, headers, body, timeout):
            payload = json.loads(body.decode()) if body else {}
            return groq_ok(method, url, payload)

        from orvix.providers import build_clients
        from orvix.router import BudgetTracker
        clients = build_clients(env=FAKE_ENV, transport=transport)
        orch = Orchestrator(clients=clients, budget=BudgetTracker(limit_usd=10))
        res = orch.complete("say hello", task_type="chat")
        self.assertEqual(res.text, "hello from groq")
        self.assertIn(res.provider, ("groq", "openai", "ollama"))


# ---------------------------------------------------------------------------
# Settings: temperature + max_response_tokens
# ---------------------------------------------------------------------------

class TemperatureSettingsTests(unittest.TestCase):
    def test_defaults_reproduce_previous_behaviour(self):
        self.assertEqual(settings_module.DEFAULTS["temperature"], 0.4)
        self.assertEqual(settings_module.DEFAULTS["max_response_tokens"], 1024)
        self.assertEqual(settings_module.TEMPERATURE_DEFAULT, 0.4)
        self.assertEqual(settings_module.MAX_RESPONSE_TOKENS_DEFAULT, 1024)

    def test_validate_temperature_accepts_and_clamps(self):
        v = settings_module.validate_temperature
        self.assertEqual(v(0.4), 0.4)
        self.assertEqual(v("0.7"), 0.7)
        self.assertEqual(v(0), 0.0)
        self.assertEqual(v(2), 2.0)
        self.assertEqual(v(2.5), 2.0)   # clamped, not rejected
        self.assertEqual(v(-1), 0.0)    # clamped, not rejected
        self.assertIsInstance(v(1), float)

    def test_validate_temperature_rejects_non_numbers(self):
        for bad in ("hot", "", None, True, float("nan"), float("inf"), [0.4]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    settings_module.validate_temperature(bad)

    def test_validate_max_response_tokens_accepts_and_clamps(self):
        v = settings_module.validate_max_response_tokens
        self.assertEqual(v(1024), 1024)
        self.assertEqual(v("2048"), 2048)
        self.assertEqual(v(512.0), 512)
        self.assertEqual(v(0), 1)        # clamped to the positive minimum
        self.assertEqual(v(-5), 1)
        self.assertEqual(v(999999), 8192)  # clamped to the cap
        self.assertIsInstance(v(10), int)

    def test_validate_max_response_tokens_rejects_non_whole_numbers(self):
        for bad in ("many", "", None, False, 12.5, float("nan"), {"x": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    settings_module.validate_max_response_tokens(bad)

    def test_effective_resolvers_never_raise_and_fall_back(self):
        eff_t = settings_module.effective_temperature
        eff_m = settings_module.effective_max_response_tokens
        self.assertEqual(eff_t(None), 0.4)
        self.assertEqual(eff_t({}), 0.4)
        self.assertEqual(eff_t({"temperature": "bogus"}), 0.4)
        self.assertEqual(eff_t({"temperature": 0.9}), 0.9)
        self.assertEqual(eff_t({"temperature": 7}), 2.0)
        self.assertEqual(eff_m(None), 1024)
        self.assertEqual(eff_m({}), 1024)
        self.assertEqual(eff_m({"max_response_tokens": "bogus"}), 1024)
        self.assertEqual(eff_m({"max_response_tokens": 321}), 321)
        self.assertEqual(eff_m({"max_response_tokens": 10 ** 9}), 8192)

    def test_new_keys_survive_settings_round_trip(self):
        import tempfile
        from unittest.mock import patch as _patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _patch.object(settings_module, "APP_DIR", root), \
                    _patch.object(settings_module, "SETTINGS_FILE", root / "settings.json"):
                settings_module.save_settings(
                    {**settings_module.DEFAULTS, "temperature": 0.7,
                     "max_response_tokens": 2048})
                loaded = settings_module.load_settings()
                self.assertEqual(loaded["temperature"], 0.7)
                self.assertEqual(loaded["max_response_tokens"], 2048)
                stored = json.loads((root / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(stored["temperature"], 0.7)
                self.assertEqual(stored["max_response_tokens"], 2048)


# ---------------------------------------------------------------------------
# Agent payload uses the settings
# ---------------------------------------------------------------------------

class AgentPayloadSettingsTests(unittest.TestCase):
    def _capture_payload(self, agent_settings):
        agent = OrvixAgent(
            {"api_base_url": "http://localhost:11434/v1",
             "model": "test-model", **agent_settings},
            approval=lambda *_: True)
        response = Mock(status_code=200)
        response.headers = {}
        response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        with patch("orvix.agent.get_api_key", return_value=""), patch(
                "orvix.agent.requests.post", return_value=response) as post:
            result = agent.respond("Say hello.")
        self.assertEqual(result, "ok")
        _args, kwargs = post.call_args
        return kwargs["json"]

    def test_payload_uses_configured_temperature_and_cap(self):
        payload = self._capture_payload(
            {"temperature": 0.9, "max_response_tokens": 321})
        self.assertEqual(payload["temperature"], 0.9)
        self.assertEqual(payload["max_tokens"], 321)

    def test_payload_defaults_when_settings_absent(self):
        payload = self._capture_payload({})
        self.assertEqual(payload["temperature"], 0.4)
        self.assertEqual(payload["max_tokens"], 1024)

    def test_payload_clamps_and_falls_back_on_bad_stored_values(self):
        payload = self._capture_payload(
            {"temperature": 9, "max_response_tokens": 10 ** 9})
        self.assertEqual(payload["temperature"], 2.0)
        self.assertEqual(payload["max_tokens"], 8192)
        payload = self._capture_payload(
            {"temperature": "bogus", "max_response_tokens": "bogus"})
        self.assertEqual(payload["temperature"], 0.4)
        self.assertEqual(payload["max_tokens"], 1024)

    def test_vision_and_planner_keep_their_own_hardcoded_values(self):
        # Out of scope per the phase brief: those call sites do not
        # read the new settings and keep their own 0.2 tuning.
        for rel in ("orvix/vision.py", "orvix/camera_vision.py",
                    "orvix/task_planner.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIn('"temperature": 0.2', text)
                self.assertNotIn("effective_temperature", text)


# ---------------------------------------------------------------------------
# Providers payload
# ---------------------------------------------------------------------------

class ProvidersPayloadTests(unittest.TestCase):
    def test_block_shape_for_every_registry_provider(self):
        payload = build_state_payload(
            FakeWebApp(), stats=STATS, provider_env=FAKE_ENV)
        json.dumps(payload)  # must stay JSON-serializable
        providers = payload["providers"]
        self.assertEqual([p["id"] for p in providers], list(PROVIDERS))
        for entry in providers:
            self.assertEqual(entry["name"], PROVIDERS[entry["id"]].name)
            self.assertIsInstance(entry["configured"], bool)
            self.assertEqual(entry["keyless"],
                             PROVIDERS[entry["id"]].env_key is None)
            self.assertEqual(entry["health"], "not_checked")
            registry_models = PROVIDERS[entry["id"]].models
            self.assertEqual([m["name"] for m in entry["models"]],
                             [m.model for m in registry_models])
            for model_entry, info in zip(entry["models"], registry_models):
                self.assertEqual(model_entry["capabilities"],
                                 sorted(info.capabilities))
                self.assertEqual(model_entry["contextTokens"],
                                 info.context_tokens)

    def test_configured_flags_come_from_the_env_booleans_only(self):
        payload = build_state_payload(
            FakeWebApp(), stats=STATS, provider_env=FAKE_ENV)
        by_id = {p["id"]: p for p in payload["providers"]}
        self.assertTrue(by_id["groq"]["configured"])
        self.assertTrue(by_id["openai"]["configured"])
        self.assertFalse(by_id["anthropic"]["configured"])
        self.assertTrue(by_id["ollama"]["configured"])  # keyless local
        self.assertTrue(by_id["ollama"]["keyless"])
        self.assertFalse(by_id["groq"]["keyless"])
        # The fake key VALUES never appear anywhere in the payload.
        blob = json.dumps(payload)
        self.assertNotIn("fake-groq-key-for-tests", blob)
        self.assertNotIn("fake-openai-key", blob)

    def test_health_starts_not_checked_even_when_configured(self):
        payload = build_state_payload(
            FakeWebApp(), stats=STATS, provider_env=FAKE_ENV)
        for entry in payload["providers"]:
            self.assertEqual(entry["health"], "not_checked")

    def test_recorded_check_result_flows_into_next_payload(self):
        app = FakeWebApp()
        before = build_state_payload(app, stats=STATS, provider_env=FAKE_ENV)
        groq_before = {p["id"]: p for p in before["providers"]}["groq"]
        self.assertEqual(groq_before["health"], "not_checked")
        app.provider_health_results["groq"] = "reachable"
        app.provider_health_results["openai"] = "unreachable"
        after = build_state_payload(app, stats=STATS, provider_env=FAKE_ENV)
        by_id = {p["id"]: p for p in after["providers"]}
        self.assertEqual(by_id["groq"]["health"], "reachable")
        self.assertEqual(by_id["openai"]["health"], "unreachable")
        self.assertEqual(by_id["ollama"]["health"], "not_checked")

    def test_health_from_explicit_mapping_and_snapshot_booleans(self):
        payload = build_state_payload(
            FakeWebApp(), stats=STATS, provider_env={},
            provider_health={"groq": True, "xai": False, "bogus": True})
        by_id = {p["id"]: p for p in payload["providers"]}
        self.assertEqual(by_id["groq"]["health"], "reachable")
        self.assertEqual(by_id["xai"]["health"], "unreachable")
        self.assertNotIn("bogus", by_id)

    def test_health_from_snapshot_healthy_flags(self):
        app = FakeWebApp()
        app._web_provider_health = lambda: {}  # no last-check results
        app._hud_control_center_snapshot = lambda: {"models": [
            {"provider": "groq", "healthy": True},
            {"provider": "openai", "healthy": None},
        ]}
        payload = build_state_payload(app, stats=STATS, provider_env={})
        by_id = {p["id"]: p for p in payload["providers"]}
        self.assertEqual(by_id["groq"]["health"], "reachable")
        self.assertEqual(by_id["openai"]["health"], "not_checked")

    def test_minimal_app_still_gets_the_full_registry_block(self):
        class MinimalApp:
            settings = {}
            core_mode = "READY"
            wake_enabled = False

        payload = build_state_payload(MinimalApp(), stats=STATS, provider_env={})
        self.assertEqual(len(payload["providers"]), len(PROVIDERS))
        self.assertTrue(all(p["health"] == "not_checked"
                            for p in payload["providers"]))


# ---------------------------------------------------------------------------
# testProvider bridge — enqueue discipline
# ---------------------------------------------------------------------------

class TestProviderBridgeTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeWebApp()
        self.api = OrvixWebHomeApi(self.app)

    def test_test_provider_enqueues_with_provider_id(self):
        self.assertEqual(self.api.testProvider("groq"), {"ok": True})
        self.assertEqual(self.app.calls, [])  # nothing before the drain
        drain(self.app)
        self.assertEqual(self.app.calls,
                         [("_test_provider_from_web", "groq")])

    def test_test_provider_normalises_the_id(self):
        self.assertEqual(self.api.testProvider("  GROQ "), {"ok": True})
        drain(self.app)
        self.assertEqual(self.app.calls,
                         [("_test_provider_from_web", "groq")])

    def test_unknown_provider_is_rejected_and_enqueues_nothing(self):
        for bad in (None, "", "not-a-provider", "gpt"):
            with self.subTest(bad=bad):
                result = self.api.testProvider(bad)
                self.assertFalse(result["ok"])
        self.assertTrue(self.app.events.empty())
        self.assertEqual(self.app.calls, [])

    def test_missing_app_handler_reports_not_ok(self):
        class MinimalApp:
            events = queue.Queue()

        api = OrvixWebHomeApi(MinimalApp())
        self.assertFalse(api.testProvider("groq")["ok"])

    def test_result_lands_in_next_payload_after_drain(self):
        self.api.testProvider("ollama")
        drain(self.app)
        payload = build_state_payload(
            self.app, stats=STATS, provider_env={})
        by_id = {p["id"]: p for p in payload["providers"]}
        self.assertEqual(by_id["ollama"]["health"], "reachable")
        self.assertEqual(by_id["groq"]["health"], "not_checked")


# ---------------------------------------------------------------------------
# App-side handler — real method, fake client health()
# ---------------------------------------------------------------------------

def make_desktop(testcase):
    try:
        import app as app_module
    except Exception as exc:  # tkinter unavailable on this runner
        testcase.skipTest(f"app module unavailable: {exc}")
    desktop = app_module.OrvixDesktop.__new__(app_module.OrvixDesktop)
    return app_module, desktop


class FakeHealthClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def health(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class AppProviderHealthHandlerTests(unittest.TestCase):
    def test_web_provider_health_starts_empty(self):
        _module, desktop = make_desktop(self)
        desktop._provider_health_results = {}
        self.assertEqual(desktop._web_provider_health(), {})

    def test_test_records_reachable_and_unreachable(self):
        _module, desktop = make_desktop(self)
        desktop._provider_health_results = {}
        clients = {"groq": FakeHealthClient(True),
                   "openai": FakeHealthClient(False)}
        desktop._provider_health_client = lambda pid: clients[pid]
        self.assertTrue(desktop._test_provider_from_web("groq"))
        self.assertFalse(desktop._test_provider_from_web("openai"))
        self.assertEqual(desktop._web_provider_health(),
                         {"groq": "reachable", "openai": "unreachable"})
        self.assertEqual(clients["groq"].calls, 1)

    def test_checking_state_is_recorded_before_the_check_runs(self):
        _module, desktop = make_desktop(self)
        desktop._provider_health_results = {}
        seen = {}

        class ProbingClient:
            def health(self):
                seen["during"] = dict(desktop._provider_health_results)
                return True

        desktop._provider_health_client = lambda pid: ProbingClient()
        desktop._test_provider_from_web("ollama")
        self.assertEqual(seen["during"], {"ollama": "checking"})
        self.assertEqual(desktop._web_provider_health(),
                         {"ollama": "reachable"})

    def test_health_exception_is_an_honest_unreachable(self):
        _module, desktop = make_desktop(self)
        desktop._provider_health_results = {}
        desktop._provider_health_client = (
            lambda pid: FakeHealthClient(RuntimeError("boom")))
        self.assertFalse(desktop._test_provider_from_web("groq"))
        self.assertEqual(desktop._web_provider_health(),
                         {"groq": "unreachable"})

    def test_unknown_provider_records_nothing(self):
        _module, desktop = make_desktop(self)
        desktop._provider_health_results = {}
        desktop._provider_health_client = lambda pid: FakeHealthClient(True)
        self.assertIsNone(desktop._test_provider_from_web("nope"))
        self.assertIsNone(desktop._test_provider_from_web(""))
        self.assertEqual(desktop._web_provider_health(), {})

    def test_real_client_factory_uses_the_registry_spec(self):
        app_module, desktop = make_desktop(self)
        client = desktop._provider_health_client("groq")
        self.assertIs(client.spec, app_module._PROVIDER_SPECS["groq"])


# ---------------------------------------------------------------------------
# Settings tab wiring (source-level, existing pattern)
# ---------------------------------------------------------------------------

class SettingsTabWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = (APP_DIR / "app.py").read_text(encoding="utf-8")

    def test_ai_model_tab_has_temperature_and_limit_controls(self):
        self.assertIn('text="Temperature (0.0–2.0;', self.app_source)
        self.assertIn("temperature_var = tk.StringVar", self.app_source)
        self.assertIn('self.settings.get("temperature", 0.4)', self.app_source)
        self.assertIn('text="Response limit (max tokens per reply, 1–8192)"',
                      self.app_source)
        self.assertIn("max_tokens_var = tk.StringVar", self.app_source)
        self.assertIn('self.settings.get("max_response_tokens", 1024)',
                      self.app_source)

    def test_save_path_validates_both_controls(self):
        self.assertIn(
            'new["temperature"] = validate_temperature(temperature_var.get())',
            self.app_source)
        self.assertIn(
            'new["max_response_tokens"] = validate_max_response_tokens(max_tokens_var.get())',
            self.app_source)
        self.assertIn("validate_temperature", self.app_source)
        self.assertIn("validate_max_response_tokens", self.app_source)


# ---------------------------------------------------------------------------
# Static web checks for the Models page
# ---------------------------------------------------------------------------

@unittest.skipIf(
    not WEB_HOME_DIR.is_dir()
    or any(not (WEB_HOME_DIR / name).is_file()
           for name in ("index.html", "app.js", "style.css")),
    "web_home/ not fully created yet (delivered separately)")
class StaticPhase6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")

    def test_models_nav_destination_and_view_exist(self):
        self.assertIn('data-tab="models"', self.index_html)
        self.assertIn('id="modelsView"', self.index_html)
        self.assertIn("Model Management", self.index_html)
        self.assertIn('id="modelsBackHome"', self.index_html)
        self.assertIn('tab === "models"', self.app_js)
        self.assertIn("modelsView", self.app_js)
        self.assertIn("renderModels(lastState)", self.app_js)

    def test_desktop_endpoint_section_points_at_settings(self):
        for element_id in ("modelsEndpoint", "modelsEndpointUrl",
                           "modelsEndpointModel", "modelsEndpointHealth",
                           "modelsSettingsButton"):
            self.assertIn(f'id="{element_id}"', self.index_html)
        self.assertIn("Endpoint and key editing lives in Settings", self.index_html)
        self.assertIn('$("#modelsSettingsButton")', self.app_js)
        self.assertIn('callBridge("openConsole"', self.app_js)

    def test_renderer_reads_the_payload_providers_block(self):
        self.assertIn("renderModels", self.app_js)
        self.assertIn("renderModels(state)", self.app_js)
        self.assertIn("state.providers", self.app_js)
        self.assertIn("provider-card", self.app_js)
        self.assertIn(".provider-card", self.style_css)
        self.assertIn(".capability-chip", self.style_css)
        self.assertIn("capability-chip", self.app_js)
        self.assertIn("capabilities", self.app_js)

    def test_health_states_are_honest_and_complete(self):
        for key, label in (("not_checked", "Not checked"),
                           ("checking", "Checking"),
                           ("reachable", "Reachable"),
                           ("unreachable", "Unreachable")):
            self.assertIn(key, self.app_js)
            self.assertIn(label, self.app_js)
        # Configuration alone never renders as a health verdict.
        self.assertIn("provider.configured", self.app_js)
        self.assertIn("Not configured", self.app_js)
        self.assertIn("renderModels(null)", self.app_js)

    def test_test_buttons_route_via_the_bridge(self):
        self.assertIn("provider-test-button", self.app_js)
        self.assertIn(".provider-test-button", self.style_css)
        self.assertIn("dataset.providerId", self.app_js)
        self.assertIn('callBridge("testProvider"', self.app_js)
        self.assertIn('id="providerGrid"', self.index_html)
        self.assertIn('id="providersEmpty"', self.index_html)
        self.assertIn("No providers available", self.index_html)

    def test_no_key_material_anywhere_in_the_page(self):
        # No env-key names, no key-shaped strings, no storage of
        # credentials in the frontend — booleans only.
        for text in (self.index_html, self.app_js, self.style_css):
            self.assertNotIn("API_KEY", text)
            self.assertNotIn("api_key", text)
            self.assertNotIn("Bearer", text)


class NoBannedWordPhase6Tests(unittest.TestCase):
    def test_phase6_files_have_no_banned_word(self):
        # Fragments are joined at runtime so this guard does not
        # itself contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("app.py", "orvix/providers.py", "orvix/settings.py",
                    "orvix/agent.py", "orvix/webview_home.py",
                    "web_home/index.html", "web_home/app.js",
                    "web_home/style.css",
                    "tests/test_orvix_command_center_phase6.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
