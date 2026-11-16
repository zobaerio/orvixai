from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from orvix.settings import (
    ENDPOINT_HEALTH_INTERVAL_SECONDS,
    ENDPOINT_HEALTH_STATES,
    HEALTH_CHECKING,
    HEALTH_NOT_CONFIGURED,
    HEALTH_REACHABLE,
    HEALTH_UNREACHABLE,
    advance_endpoint_health,
    auto_configure_from_api_key,
    endpoint_health_should_notify,
    endpoint_is_configured,
    fetch_model_list,
    initial_endpoint_health,
    is_local_api_endpoint,
    probe_endpoint_health,
    test_api_connection,
    validate_api_base_url,
    validate_api_key_transport,
)


class ApiEndpointValidationTests(unittest.TestCase):
    def test_accepts_local_and_https_endpoints(self) -> None:
        self.assertEqual(validate_api_base_url(" http://localhost:11434/v1/ "), "http://localhost:11434/v1")
        self.assertTrue(is_local_api_endpoint("http://localhost:11434/v1"))
        self.assertTrue(is_local_api_endpoint("http://127.0.0.2:8000/v1"))
        self.assertTrue(is_local_api_endpoint("http://[::1]:8000/v1"))
        self.assertFalse(is_local_api_endpoint("https://localhost.example.com/v1"))

    def test_normalizes_common_full_openrouter_endpoint_urls(self) -> None:
        self.assertEqual(
            validate_api_base_url("https://openrouter.ai/api/v1/chat/completions"),
            "https://openrouter.ai/api/v1",
        )
        self.assertEqual(
            validate_api_base_url("https://openrouter.ai/api/v1/models"),
            "https://openrouter.ai/api/v1",
        )
        self.assertEqual(
            validate_api_base_url("https://openrouter.ai/api/v1/"),
            "https://openrouter.ai/api/v1",
        )

    def test_rejects_embedded_credentials_query_fragment_and_bad_schemes(self) -> None:
        bad_values = [
            "ftp://api.example.com/v1",
            "https://user:secret@api.example.com/v1",
            "https://api.example.com/v1?token=secret",
            "https://api.example.com/v1#fragment",
            "https:///v1",
            "https://api.example.com:bad/v1",
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_api_base_url(value)

    def test_blocks_api_key_over_remote_plain_http(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_api_key_transport("http://api.example.com/v1", "very-secret")
        self.assertEqual(
            validate_api_key_transport("http://localhost:11434/v1", "local-secret"),
            "http://localhost:11434/v1",
        )
        self.assertEqual(
            validate_api_key_transport("https://api.example.com/v1", "remote-secret"),
            "https://api.example.com/v1",
        )

    @patch("requests.get")
    def test_auto_setup_detects_openrouter_and_selects_preferred_model(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "openai/gpt-4o-mini"}, {"id": "other-model"}]}
        get.return_value = response
        result = auto_configure_from_api_key("sk-or-v1-test-secret")
        self.assertEqual(result["provider"], "OpenRouter")
        self.assertEqual(result["api_base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(result["model"], "openai/gpt-4o-mini")
        get.assert_called_once_with(
            "https://openrouter.ai/api/v1/models",
            headers={"Accept": "application/json", "Authorization": "Bearer sk-or-v1-test-secret"},
            timeout=(3, 8.0),
        )

    @patch("requests.get")
    def test_auto_setup_detects_google_gemini_and_selects_available_model(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "gemini-2.5-flash"}, {"id": "gemini-embedding-001"}]}
        get.return_value = response
        result = auto_configure_from_api_key("AIza-test-secret")
        self.assertEqual(result["provider"], "Google Gemini")
        self.assertEqual(result["api_base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(result["model"], "gemini-2.5-flash")
        get.assert_called_once_with(
            "https://generativelanguage.googleapis.com/v1beta/openai/models",
            headers={"Accept": "application/json", "Authorization": "Bearer AIza-test-secret"},
            timeout=(3, 8.0),
        )


    @patch("requests.get")
    def test_auto_setup_fallback_skips_embedding_models(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {"id": "gemini-embedding-001"},
                {"id": "gemini-2.5-pro-preview"},
                {"id": "text-embedding-3-large"},
            ]
        }
        get.return_value = response
        result = auto_configure_from_api_key("AIza-test-secret")
        self.assertEqual(result["model"], "gemini-2.5-pro-preview")

    @patch("requests.get")
    def test_auto_setup_rejects_catalog_without_recognizable_chat_model(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "gemini-embedding-001"}]}
        get.return_value = response
        with self.assertRaisesRegex(ValueError, "no safely recognizable chat model"):
            auto_configure_from_api_key("AIza-test-secret")

    @patch("requests.get")
    def test_auto_setup_prefers_current_groq_models_over_retired_ids(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {"id": "llama-3.3-70b-versatile"},
                {"id": "llama-3.1-8b-instant"},
                {"id": "openai/gpt-oss-120b"},
                {"id": "openai/gpt-oss-20b"},
            ]
        }
        get.return_value = response
        result = auto_configure_from_api_key("gsk_test-secret")
        self.assertEqual(result["provider"], "Groq")
        self.assertEqual(result["model"], "openai/gpt-oss-120b")
        self.assertEqual(result["api_base_url"], "https://api.groq.com/openai/v1")
        get.assert_called_once_with(
            "https://api.groq.com/openai/v1/models",
            headers={"Accept": "application/json", "Authorization": "Bearer gsk_test-secret"},
            timeout=(3, 8.0),
        )

    @patch("requests.get")
    def test_auto_setup_detects_xai_and_selects_available_grok_model(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "grok-4.7"}, {"id": "grok-3-mini"}]}
        get.return_value = response
        result = auto_configure_from_api_key("xai-test-secret")
        self.assertEqual(result["provider"], "xAI")
        self.assertEqual(result["api_base_url"], "https://api.x.ai/v1")
        self.assertEqual(result["model"], "grok-4.7")
        get.assert_called_once_with(
            "https://api.x.ai/v1/models",
            headers={"Accept": "application/json", "Authorization": "Bearer xai-test-secret"},
            timeout=(3, 8.0),
        )

    @patch("requests.get")
    def test_auto_setup_never_probes_unknown_or_ambiguous_provider_keys(self, get) -> None:
        # Generic sk-* values overlap across providers (for example Anthropic and
        # legacy OpenAI-style keys); do not send them to an assumed provider.
        for key in ("unknown-provider-secret", "sk-ant-api03-test-secret", "sk-legacy-test-secret"):
            with self.subTest(key_prefix=key.split("-")[0]):
                with self.assertRaisesRegex(ValueError, "could not be identified safely"):
                    auto_configure_from_api_key(key)
        get.assert_not_called()

    @patch("requests.get")
    def test_auto_setup_handles_invalid_key_without_echoing_secret(self, get) -> None:
        get.return_value = Mock(status_code=401)
        with self.assertRaisesRegex(ValueError, "rejected this API key"):
            auto_configure_from_api_key("sk-or-v1-test-secret")
        get.assert_called_once()

    @patch("requests.get")
    def test_connection_check_uses_models_endpoint_and_matches_model(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        get.return_value = response
        result = test_api_connection("https://api.example.com/v1/", "model-b", "test-key")
        self.assertTrue(result["ok"])
        self.assertTrue(result["model_available"])
        self.assertEqual(result["status_code"], 200)
        get.assert_called_once_with(
            "https://api.example.com/v1/models",
            headers={"Accept": "application/json", "Authorization": "Bearer test-key"},
            timeout=(3, 7.0),
        )

    @patch("requests.get")
    def test_connection_test_blocks_insecure_endpoint_before_network_call(self, get) -> None:
        result = test_api_connection("http://api.example.com/v1", "model-a", "test-key")
        self.assertFalse(result["ok"])
        self.assertIn("HTTPS", result["message"])
        get.assert_not_called()

    @patch("requests.get")
    def test_connection_test_handles_unauthorized_without_exposing_key(self, get) -> None:
        get.return_value = Mock(status_code=401)
        result = test_api_connection("https://api.example.com/v1", "model-a", "very-secret-key")
        self.assertFalse(result["ok"])
        self.assertIn("401", result["message"])
        self.assertNotIn("very-secret-key", result["message"])

    @patch("requests.get")
    def test_connection_check_does_not_generate_a_chat_completion(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "qwen2.5:3b"}]}
        get.return_value = response
        result = test_api_connection("http://localhost:11434/v1", "qwen2.5:3b", "")
        self.assertTrue(result["ok"])
        self.assertIn("/models", get.call_args.args[0])
        self.assertNotIn("/chat/completions", get.call_args.args[0])


    def test_rejects_blank_control_character_and_oversized_urls(self) -> None:
        bad_values = [
            "",
            "   ",
            "https://api.example.com/v1\nAuthorization: Bearer secret",
            "https://api.example.com/" + ("a" * 2050),
        ]
        for value in bad_values:
            with self.subTest(value=value[:40]):
                with self.assertRaises(ValueError):
                    validate_api_base_url(value)

    def test_rejects_non_string_like_empty_key_configuration_safely(self) -> None:
        with self.assertRaisesRegex(ValueError, "Paste your provider API key first"):
            auto_configure_from_api_key("  ")


# ---------------------------------------------------------------------------
# Premium Phase 8a — model list fetch (fake transport)
# ---------------------------------------------------------------------------

def _models_response(model_ids, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = {"data": [{"id": m} for m in model_ids]}
    return response


class FetchModelListTests(unittest.TestCase):
    @patch("requests.get")
    def test_success_parses_list_and_keeps_current_model(self, get) -> None:
        get.return_value = _models_response(["model-a", "model-b", "model-c"])
        result = fetch_model_list("https://api.example.com/v1/", "test-key")
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["model-a", "model-b", "model-c"])
        # The configured model is in the fetched list, so the Settings
        # combo can keep it selected (retention is driven by this list).
        self.assertIn("model-b", result["models"])
        get.assert_called_once_with(
            "https://api.example.com/v1/models",
            headers={"Accept": "application/json", "Authorization": "Bearer test-key"},
            timeout=(3, 7.0),
        )

    @patch("requests.get")
    def test_shares_transport_with_connection_test(self, get) -> None:
        # The shared-helper contract: the model-list fetch and the
        # connection test issue the identical /models request.
        get.return_value = _models_response(["model-a"])
        fetch_model_list("https://api.example.com/v1", "test-key")
        list_call = get.call_args
        get.reset_mock()
        get.return_value = _models_response(["model-a"])
        test_api_connection("https://api.example.com/v1", "model-a", "test-key")
        self.assertEqual(get.call_args, list_call)

    @patch("requests.get")
    def test_http_failure_is_honest_and_fabricates_no_list(self, get) -> None:
        get.return_value = _models_response([], status_code=500)
        result = fetch_model_list("https://api.example.com/v1", "test-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertIn("500", result["message"])

    @patch("requests.get")
    def test_unauthorized_is_honest_and_fabricates_no_list(self, get) -> None:
        get.return_value = Mock(status_code=401)
        result = fetch_model_list("https://api.example.com/v1", "very-secret-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertIn("401", result["message"])
        self.assertNotIn("very-secret-key", result["message"])

    @patch("requests.get")
    def test_network_failure_is_honest_and_fabricates_no_list(self, get) -> None:
        get.side_effect = ConnectionError("down")
        result = fetch_model_list("https://api.example.com/v1", "test-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertIn("Could not reach the endpoint", result["message"])

    @patch("requests.get")
    def test_invalid_json_is_honest_and_fabricates_no_list(self, get) -> None:
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("not json")
        get.return_value = response
        result = fetch_model_list("https://api.example.com/v1", "test-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertIn("did not return valid JSON", result["message"])

    @patch("requests.get")
    def test_insecure_transport_blocks_before_network_call(self, get) -> None:
        result = fetch_model_list("http://api.example.com/v1", "test-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["models"], [])
        self.assertIn("HTTPS", result["message"])
        get.assert_not_called()

    @patch("requests.get")
    def test_empty_catalogue_is_an_honest_empty_list(self, get) -> None:
        get.return_value = _models_response([])
        result = fetch_model_list("http://localhost:11434/v1", "")
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], [])

    @patch("requests.get")
    def test_malformed_records_are_ignored_not_invented(self, get) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [{"id": "real"}, {"name": "no-id"}, "junk", {"id": 7}]}
        get.return_value = response
        result = fetch_model_list("http://localhost:11434/v1", "")
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["real"])


# ---------------------------------------------------------------------------
# Premium Phase 8a — endpoint health state machine (pure)
# ---------------------------------------------------------------------------

class EndpointHealthStateTests(unittest.TestCase):
    def test_states_are_exactly_the_four_documented_values(self):
        self.assertEqual(
            set(ENDPOINT_HEALTH_STATES),
            {"not_configured", "checking", "reachable", "unreachable"},
        )
        self.assertEqual(HEALTH_NOT_CONFIGURED, "not_configured")
        self.assertEqual(HEALTH_CHECKING, "checking")
        self.assertEqual(HEALTH_REACHABLE, "reachable")
        self.assertEqual(HEALTH_UNREACHABLE, "unreachable")

    def test_probe_interval_is_bounded(self):
        self.assertEqual(ENDPOINT_HEALTH_INTERVAL_SECONDS, 30.0)
        self.assertGreaterEqual(ENDPOINT_HEALTH_INTERVAL_SECONDS, 10.0)

    def test_configured_needs_both_url_and_model(self):
        self.assertTrue(endpoint_is_configured("http://localhost:11434/v1", "m"))
        self.assertFalse(endpoint_is_configured("", "m"))
        self.assertFalse(endpoint_is_configured("http://localhost:11434/v1", ""))
        self.assertFalse(endpoint_is_configured("  ", "  "))
        self.assertFalse(endpoint_is_configured(None, None))

    def test_initial_state_never_claims_reachable(self):
        self.assertEqual(initial_endpoint_health(True), "checking")
        self.assertEqual(initial_endpoint_health(False), "not_configured")

    def test_first_probe_verdicts_do_not_notify(self):
        self.assertEqual(
            advance_endpoint_health("checking", configured=True, probe_ok=True),
            ("reachable", False),
        )
        self.assertEqual(
            advance_endpoint_health("checking", configured=True, probe_ok=False),
            ("unreachable", False),
        )
        self.assertEqual(
            advance_endpoint_health("not_configured", configured=True, probe_ok=True),
            ("reachable", False),
        )
        self.assertEqual(
            advance_endpoint_health(None, configured=True, probe_ok=True),
            ("reachable", False),
        )

    def test_flips_between_verdicts_notify(self):
        self.assertEqual(
            advance_endpoint_health("reachable", configured=True, probe_ok=False),
            ("unreachable", True),
        )
        self.assertEqual(
            advance_endpoint_health("unreachable", configured=True, probe_ok=True),
            ("reachable", True),
        )

    def test_steady_verdicts_do_not_notify(self):
        self.assertEqual(
            advance_endpoint_health("reachable", configured=True, probe_ok=True),
            ("reachable", False),
        )
        self.assertEqual(
            advance_endpoint_health("unreachable", configured=True, probe_ok=False),
            ("unreachable", False),
        )

    def test_unconfigured_never_probes_and_never_notifies(self):
        for current in ENDPOINT_HEALTH_STATES:
            with self.subTest(current=current):
                self.assertEqual(
                    advance_endpoint_health(current, configured=False, probe_ok=None),
                    ("not_configured", False),
                )

    def test_no_probe_tick_keeps_verdict_or_moves_to_checking(self):
        self.assertEqual(
            advance_endpoint_health("reachable", configured=True, probe_ok=None),
            ("reachable", False),
        )
        self.assertEqual(
            advance_endpoint_health("not_configured", configured=True, probe_ok=None),
            ("checking", False),
        )
        self.assertEqual(
            advance_endpoint_health("bogus", configured=True, probe_ok=None),
            ("checking", False),
        )

    def test_should_notify_predicate(self):
        self.assertTrue(endpoint_health_should_notify("reachable", "unreachable"))
        self.assertTrue(endpoint_health_should_notify("unreachable", "reachable"))
        self.assertFalse(endpoint_health_should_notify("checking", "reachable"))
        self.assertFalse(endpoint_health_should_notify("reachable", "reachable"))
        self.assertFalse(endpoint_health_should_notify("reachable", "not_configured"))
        self.assertFalse(endpoint_health_should_notify("not_configured", "unreachable"))


# ---------------------------------------------------------------------------
# Premium Phase 8a — the probe itself (fake transport)
# ---------------------------------------------------------------------------

class ProbeEndpointHealthTests(unittest.TestCase):
    @patch("requests.get")
    def test_unconfigured_endpoint_is_never_probed(self, get) -> None:
        for base_url, model in (("", "m"), ("http://localhost:11434/v1", ""), ("", "")):
            with self.subTest(base_url=base_url, model=model):
                result = probe_endpoint_health(base_url, model, "test-key")
                self.assertFalse(result["ok"])
                self.assertFalse(result["configured"])
        get.assert_not_called()

    @patch("requests.get")
    def test_configured_probe_success(self, get) -> None:
        get.return_value = _models_response(["model-a"])
        result = probe_endpoint_health("https://api.example.com/v1", "model-a", "test-key")
        self.assertTrue(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["status_code"], 200)
        get.assert_called_once_with(
            "https://api.example.com/v1/models",
            headers={"Accept": "application/json", "Authorization": "Bearer test-key"},
            timeout=(3, 7.0),
        )

    @patch("requests.get")
    def test_configured_probe_failure(self, get) -> None:
        get.return_value = _models_response([], status_code=503)
        result = probe_endpoint_health("https://api.example.com/v1", "model-a", "test-key")
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertIn("503", result["message"])

    @patch("requests.get")
    def test_configured_probe_network_error(self, get) -> None:
        get.side_effect = TimeoutError("slow")
        result = probe_endpoint_health("https://api.example.com/v1", "model-a", "test-key")
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])


# ---------------------------------------------------------------------------
# Premium Phase 8a — transitions fire the Phase 6 notification, via the
# real notify_event + a fake tray (the composition app.py wires up).
# ---------------------------------------------------------------------------

class HealthNotificationFlowTests(unittest.TestCase):
    def _run_sequence(self, settings_dict, probe_results):
        from orvix.notify import notify_event

        class FakeTray:
            def __init__(self):
                self.calls = []

            def notify(self, message, title=None):
                self.calls.append((message, title))

        tray = FakeTray()
        state = initial_endpoint_health(True)
        for ok in probe_results:
            state, should_notify = advance_endpoint_health(
                state, configured=True, probe_ok=ok)
            if should_notify:
                notify_event(settings_dict, tray, "endpoint_health",
                             detail="probe detail")
        return state, tray.calls

    def test_notifications_fire_on_transitions_only(self):
        all_on = {"notify_endpoint_health": True}
        # ok, ok (steady), fail (flip), fail (steady), ok (flip)
        state, calls = self._run_sequence(all_on, [True, True, False, False, True])
        self.assertEqual(state, "reachable")
        self.assertEqual(len(calls), 2)
        for _message, title in calls:
            self.assertEqual(title, "ORVIX — Endpoint health")

    def test_first_probe_verdict_never_notifies(self):
        _state, calls = self._run_sequence({"notify_endpoint_health": True}, [False])
        self.assertEqual(calls, [])

    def test_toggle_off_suppresses_transition_notifications(self):
        _state, calls = self._run_sequence(
            {"notify_endpoint_health": False}, [True, False, True])
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# Premium Phase 8a — web payload health field + precedence contract
# ---------------------------------------------------------------------------

class PayloadHealthTests(unittest.TestCase):
    def _payload(self, app):
        import json

        from orvix.home_data import SystemStats
        from orvix.webview_home import build_state_payload

        payload = build_state_payload(
            app, stats=SystemStats(None, None, None, "—", "—", "—", None))
        json.dumps(payload)  # must stay JSON-serializable
        return payload

    def _app(self, health_marker="absent"):
        class FakePayloadApp:
            settings = {
                "model": "qwen2.5:3b",
                "api_base_url": "http://localhost:11434/v1",
            }
            characters = {}
            selected_character = "luna"
            core_mode = "READY"
            wake_enabled = False
            _busy = False

        app = FakePayloadApp()
        if health_marker != "absent":
            app.endpoint_health = health_marker
        return app

    def test_no_probe_state_keeps_phase2_behaviour(self):
        payload = self._payload(self._app())
        self.assertIsNone(payload["model"]["health"])
        self.assertEqual(payload["model"]["endpointState"], "configured")

    def test_probe_verdict_is_carried_alongside_config_state(self):
        # Precedence contract: the payload carries BOTH fields; the
        # probe verdict (health) is the one the page prefers, while
        # endpointState keeps its Phase 2 configuration meaning.
        for verdict in ("reachable", "unreachable"):
            with self.subTest(verdict=verdict):
                payload = self._payload(self._app(verdict))
                self.assertEqual(payload["model"]["health"], verdict)
                self.assertEqual(payload["model"]["endpointState"], "configured")

    def test_checking_and_not_configured_states_pass_through(self):
        for state in ("checking", "not_configured"):
            with self.subTest(state=state):
                payload = self._payload(self._app(state))
                self.assertEqual(payload["model"]["health"], state)

    def test_unrecognised_health_value_becomes_none(self):
        payload = self._payload(self._app("connected-ish"))
        self.assertIsNone(payload["model"]["health"])


# ---------------------------------------------------------------------------
# Premium Phase 8a — app.py / app.js wiring (static, existing style)
# ---------------------------------------------------------------------------

class Phase8aStaticWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[1]
        cls.app_py = (app_dir / "app.py").read_text(encoding="utf-8")
        cls.app_js = (app_dir / "web_home" / "app.js").read_text(encoding="utf-8")
        cls.settings_py = (app_dir / "orvix" / "settings.py").read_text(encoding="utf-8")

    def test_settings_module_exposes_shared_fetch_and_health_machine(self):
        for name in (
            "def _models_request(",
            "def fetch_model_list(",
            "def probe_endpoint_health(",
            "def advance_endpoint_health(",
            "def initial_endpoint_health(",
            "def endpoint_is_configured(",
            "ENDPOINT_HEALTH_INTERVAL_SECONDS = 30.0",
        ):
            self.assertIn(name, self.settings_py)
        # The connection test reuses the shared fetch, not its own GET:
        # the whole shared-fetch region (parser, _models_request, the
        # connection test, the list fetch and the probe) contains
        # exactly one requests.get call. (auto_configure_from_api_key
        # keeps its separate provider-discovery fetch, above it.)
        start = self.settings_py.index("def test_api_connection(")
        end = self.settings_py.index("def fetch_model_list(", start)
        self.assertIn("_models_request(", self.settings_py[start:end])
        self.assertNotIn("requests.get(", self.settings_py[start:end])
        region_start = self.settings_py.index("def _parse_model_ids(")
        region_end = self.settings_py.index("_SETTINGS_LOCK", region_start)
        self.assertEqual(self.settings_py[region_start:region_end].count("requests.get("), 1)

    def test_model_field_is_an_editable_combobox(self):
        self.assertIn('if key == "model":', self.app_py)
        start = self.app_py.index('if key == "model":')
        body = self.app_py[start:start + 600]
        self.assertIn("ttk.Combobox(ai_model_tab, textvariable=var)", body)
        self.assertNotIn('state="readonly"', body)
        # The base-URL field keeps its plain Entry.
        self.assertIn("ttk.Entry(ai_model_tab, textvariable=var)", self.app_py)

    def test_load_models_button_uses_shared_fetch_on_a_daemon_thread(self):
        self.assertIn('text="Load models", command=load_models', self.app_py)
        start = self.app_py.index("        def load_models() -> None:")
        end = self.app_py.index("load_models_button = ttk.Button", start)
        body = self.app_py[start:end]
        self.assertIn("fetch_model_list(base_value, api_key_value)", body)
        self.assertIn("threading.Thread(target=worker, daemon=True)", body)
        # The fetch happens only from this button path — never on
        # dialog open or anywhere else in the app.
        self.assertEqual(self.app_py.count("fetch_model_list("), 1)

    def test_load_models_result_handling_retains_and_never_fabricates(self):
        start = self.app_py.index("        def show_model_list(result: dict")
        end = self.app_py.index("        def load_models() -> None:", start)
        body = self.app_py[start:end]
        # Failure is handled before any dropdown update.
        self.assertLess(
            body.index('if not result.get("ok"):'),
            body.index("model_combo.configure(values=models)"),
        )
        self.assertIn('if requested_model in models:', body)
        self.assertIn('variables["model"].set(requested_model)', body)
        self.assertIn("Could not load models:", body)

    def test_health_monitor_is_a_bounded_daemon_thread(self):
        self.assertIn("self._start_endpoint_health_monitor()", self.app_py)
        start = self.app_py.index("    def _endpoint_health_loop(self) -> None:")
        end = self.app_py.index("    def _probe_endpoint_health_once(self)", start)
        body = self.app_py[start:end]
        self.assertIn("stop.wait(ENDPOINT_HEALTH_INTERVAL_SECONDS)", body)
        start = self.app_py.index("    def _start_endpoint_health_monitor(self) -> None:")
        end = self.app_py.index("    def _endpoint_health_loop(self)", start)
        body = self.app_py[start:end]
        self.assertIn('name="orvix-endpoint-health"', body)
        self.assertIn("daemon=True", body)
        # Exit stops the monitor.
        start = self.app_py.index("    def _exit_application(self) -> None:")
        end = self.app_py.index("    def _maybe_start_webview_home(self)", start)
        self.assertIn("_endpoint_health_stop", self.app_py[start:end])

    def test_probe_tick_skips_unconfigured_and_enqueues_result(self):
        start = self.app_py.index("    def _probe_endpoint_health_once(self) -> None:")
        end = self.app_py.index("    def _apply_endpoint_health(self", start)
        body = self.app_py[start:end]
        self.assertIn("endpoint_is_configured(base_url, model)", body)
        self.assertIn("probe_endpoint_health(base_url, model, api_key)", body)
        self.assertIn("self.events.put(", body)
        # The probe call exists only here — the monitor is its only caller.
        self.assertEqual(self.app_py.count("probe_endpoint_health("), 1)

    def test_apply_fires_notification_only_on_transitions(self):
        start = self.app_py.index("    def _apply_endpoint_health(self")
        end = self.app_py.index("    def _reset_endpoint_health(self)", start)
        body = self.app_py[start:end]
        self.assertIn("advance_endpoint_health(", body)
        self.assertIn("if should_notify:", body)
        self.assertIn('self._notify("endpoint_health", detail)', body)
        self.assertIn("self._model_status_text()", body)

    def test_tk_pill_reflects_probe_backed_state(self):
        start = self.app_py.index("    def _model_status_text(self) -> str:")
        end = self.app_py.index("    def _write_message(self", start)
        body = self.app_py[start:end]
        for text in (
            "● AI ENDPOINT REACHABLE",
            "● AI ENDPOINT UNREACHABLE",
            "● CHECKING AI ENDPOINT",
            "● AI ENDPOINT NOT CONFIGURED",
            "● LOCAL AI ENDPOINT",
            "● AI ENDPOINT SET",
        ):
            self.assertIn(text, body)

    def test_endpoint_health_property_and_settings_reset(self):
        self.assertIn("    @property\n    def endpoint_health(self) -> str:", self.app_py)
        self.assertIn("def _reset_endpoint_health(self) -> None:", self.app_py)
        self.assertEqual(self.app_py.count("self._reset_endpoint_health()"), 2)

    def test_web_pill_prefers_probe_verdict_and_keeps_fallback(self):
        self.assertIn("MODEL_HEALTH_PILL_TEXT", self.app_js)
        self.assertIn('"reachable": "ENDPOINT REACHABLE"', self.app_js)
        self.assertIn('"unreachable": "ENDPOINT UNREACHABLE"', self.app_js)
        start = self.app_js.index("function updateModelPill(state)")
        end = self.app_js.index("function updateChatPill(state)", start)
        body = self.app_js[start:end]
        # The probe verdict is consulted before the config state, and
        # the older-payload fallback (statusLines) is still present.
        self.assertLess(
            body.index("MODEL_HEALTH_PILL_TEXT[model.health]"),
            body.index("model.endpointState"),
        )
        self.assertIn("statusLines", body)
        self.assertIn("ENDPOINT CONFIGURED", self.app_js)


if __name__ == "__main__":
    unittest.main()
