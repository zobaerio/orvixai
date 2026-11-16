"""Tests for Premium Phase 3 — voice completion.

Covers the three fixes:

- Bug #9: speech rate / volume settings (validated signed-percent
  strings) plumbed from settings into ``edge_tts.Communicate``;
- Bug #7: Stop Speaking — ``SpeechEngine.stop_speaking()`` is safe
  and idempotent, and is wired to a Tk control and a WebView bridge
  method (``stopSpeaking``);
- Bug #8: the wake-word matcher matches the CONFIGURED wake phrase
  (default "hey orvix") — the same phrase both UIs display — instead
  of the bare assistant name anywhere in the utterance.

No real audio, network or Tk root is used: edge-tts and pygame are
fakes injected into ``sys.modules``, following test_speech_timeouts.
"""
from __future__ import annotations

import asyncio
import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import settings
from orvix.home_data import SystemStats
from orvix.speech import SpeechEngine, match_wake_phrase
from orvix.webview_home import OrvixWebHomeApi, build_state_payload

APP_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

class SpeechRateVolumeValidationTests(unittest.TestCase):
    def test_valid_rates_are_normalised(self):
        cases = {
            "+0%": "+0%", "-0%": "+0%", "-50%": "-50%", "+100%": "+100%",
            "-10%": "-10%", " +25% ": "+25%", "+007%": "+7%",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(settings.validate_speech_rate(raw), expected)

    def test_malformed_rates_are_rejected(self):
        for raw in ("0%", "+0", "fast", "", "  ", "+1.5%", "+-10%", "10", None):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    settings.validate_speech_rate(raw)

    def test_out_of_range_rates_are_rejected(self):
        for raw in ("+101%", "-51%", "+200%", "-100%"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    settings.validate_speech_rate(raw)

    def test_valid_volumes_are_normalised(self):
        cases = {"+0%": "+0%", "-100%": "-100%", "+100%": "+100%", "-25%": "-25%"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(settings.validate_speech_volume(raw), expected)

    def test_malformed_and_out_of_range_volumes_are_rejected(self):
        for raw in ("0%", "+0", "loud", "", "+101%", "-101%"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    settings.validate_speech_volume(raw)

    def test_defaults_preserve_previous_behaviour(self):
        self.assertEqual(settings.DEFAULTS["speech_rate"], "+0%")
        self.assertEqual(settings.DEFAULTS["speech_volume"], "+0%")
        self.assertEqual(settings.DEFAULTS["wake_phrase"], "hey orvix")


class WakePhraseValidationTests(unittest.TestCase):
    def test_valid_phrases_are_normalised(self):
        self.assertEqual(settings.validate_wake_phrase("hey orvix"), "hey orvix")
        self.assertEqual(settings.validate_wake_phrase("  hey   orvix  "), "hey orvix")
        self.assertEqual(settings.validate_wake_phrase("don't wake"), "don't wake")

    def test_invalid_phrases_are_rejected(self):
        for raw in ("", "   ", "hey orvix!", "hey orvix 2", "hey-orvix",
                    "a" * (settings.WAKE_PHRASE_MAX_LENGTH + 1), None):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    settings.validate_wake_phrase(raw)


class VoiceSettingsPersistenceTests(unittest.TestCase):
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

    def test_voice_settings_round_trip(self) -> None:
        settings.save_settings({
            **settings.DEFAULTS,
            "speech_rate": "-25%",
            "speech_volume": "+50%",
            "wake_phrase": "okay computer",
        })
        loaded = settings.load_settings()
        self.assertEqual(loaded["speech_rate"], "-25%")
        self.assertEqual(loaded["speech_volume"], "+50%")
        self.assertEqual(loaded["wake_phrase"], "okay computer")
        stored = json.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual(stored["wake_phrase"], "okay computer")


# ---------------------------------------------------------------------------
# Rate / volume plumbing into edge-tts
# ---------------------------------------------------------------------------

def _fake_audio_modules(captured):
    """Fake edge_tts/pygame pair; Communicate kwargs land in `captured`."""
    def communicate_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(save=AsyncMock())

    music = SimpleNamespace(
        load=Mock(), play=Mock(), get_busy=Mock(return_value=False),
        stop=Mock(), unload=Mock())
    pygame = SimpleNamespace(mixer=SimpleNamespace(init=Mock(), music=music))
    edge_tts = SimpleNamespace(Communicate=communicate_factory)
    return edge_tts, pygame


class RateVolumePlumbingTests(unittest.TestCase):
    def test_configured_rate_and_volume_reach_communicate(self):
        captured = {}
        edge_tts, pygame = _fake_audio_modules(captured)
        engine = SpeechEngine()
        with patch.dict(sys.modules, {"edge_tts": edge_tts, "pygame": pygame}):
            asyncio.run(engine._edge_speak(
                "hello", "test-voice", rate="-25%", volume="+50%"))
        self.assertEqual(captured["rate"], "-25%")
        self.assertEqual(captured["volume"], "+50%")
        self.assertEqual(captured["voice"], "test-voice")
        self.assertEqual(captured["text"], "hello")

    def test_defaults_reach_communicate_unchanged(self):
        captured = {}
        edge_tts, pygame = _fake_audio_modules(captured)
        engine = SpeechEngine()
        with patch.dict(sys.modules, {"edge_tts": edge_tts, "pygame": pygame}):
            asyncio.run(engine._edge_speak("hello", "test-voice"))
        self.assertEqual(captured["rate"], "+0%")
        self.assertEqual(captured["volume"], "+0%")

    def test_speak_passes_rate_and_volume_through(self):
        captured = {}
        edge_tts, pygame = _fake_audio_modules(captured)
        engine = SpeechEngine()
        with patch.dict(sys.modules, {"edge_tts": edge_tts, "pygame": pygame}):
            engine.speak("hello", "test-voice", rate="+25%", volume="-25%")
        self.assertEqual(captured["rate"], "+25%")
        self.assertEqual(captured["volume"], "-25%")

    def test_invalid_values_fall_back_to_neutral(self):
        captured = {}
        edge_tts, pygame = _fake_audio_modules(captured)
        engine = SpeechEngine()
        with patch.dict(sys.modules, {"edge_tts": edge_tts, "pygame": pygame}):
            asyncio.run(engine._edge_speak(
                "hello", "test-voice", rate="loud", volume="+999%"))
        self.assertEqual(captured["rate"], "+0%")
        self.assertEqual(captured["volume"], "+0%")


# ---------------------------------------------------------------------------
# Wake phrase matcher
# ---------------------------------------------------------------------------

class WakePhraseMatcherTests(unittest.TestCase):
    def test_default_phrase_triggers_with_no_command(self):
        self.assertEqual(match_wake_phrase("hey orvix", "hey orvix"), "")

    def test_default_phrase_is_case_insensitive(self):
        self.assertEqual(match_wake_phrase("HEY ORVIX", "hey orvix"), "")
        self.assertEqual(match_wake_phrase("Hey Orvix", "hey orvix"), "")

    def test_extra_spacing_and_comma_still_trigger(self):
        self.assertEqual(
            match_wake_phrase("hey   orvix, open Notepad", "hey orvix"),
            "open Notepad")

    def test_trailing_command_is_extracted(self):
        self.assertEqual(
            match_wake_phrase("Hey Orvix, open Notepad", "hey orvix"),
            "open Notepad")
        self.assertEqual(
            match_wake_phrase("hey orvix what time is it", "hey orvix"),
            "what time is it")

    def test_trailing_punctuation_alone_is_no_command(self):
        self.assertEqual(match_wake_phrase("hey orvix!", "hey orvix"), "")

    def test_bare_name_alone_does_not_trigger(self):
        # The behaviour change: under the default phrase the bare name
        # is no longer enough, wherever it appears in the utterance.
        self.assertIsNone(match_wake_phrase("orvix", "hey orvix"))
        self.assertIsNone(match_wake_phrase("say orvix loudly", "hey orvix"))
        self.assertIsNone(match_wake_phrase("orvix open notepad", "hey orvix"))

    def test_partial_phrase_does_not_trigger(self):
        self.assertIsNone(match_wake_phrase("hey there", "hey orvix"))
        self.assertIsNone(match_wake_phrase("hey orvi", "hey orvix"))

    def test_custom_phrase_triggers_and_default_does_not(self):
        self.assertEqual(
            match_wake_phrase("Okay Computer, lights on", "okay computer"),
            "lights on")
        self.assertIsNone(match_wake_phrase("hey orvix", "okay computer"))

    def test_bengali_script_name_variant_triggers(self):
        self.assertEqual(
            match_wake_phrase("hey অরভিক্স Notepad kholo", "hey orvix"),
            "Notepad kholo")

    def test_empty_or_unrelated_speech_does_not_trigger(self):
        self.assertIsNone(match_wake_phrase("", "hey orvix"))
        self.assertIsNone(match_wake_phrase("   ", "hey orvix"))
        self.assertIsNone(match_wake_phrase("open notepad please", "hey orvix"))

    def test_invalid_configured_phrase_falls_back_to_default(self):
        self.assertEqual(match_wake_phrase("hey orvix go", "!!!"), "go")
        self.assertIsNone(match_wake_phrase("orvix go", "!!!"))


# ---------------------------------------------------------------------------
# Stop speaking: engine safety + bridge/Tk wiring
# ---------------------------------------------------------------------------

class StopSpeakingEngineTests(unittest.TestCase):
    def test_stop_speaking_is_safe_when_nothing_is_playing(self):
        engine = SpeechEngine()
        engine.stop_speaking()  # must not raise, pygame or not
        self.assertTrue(engine._stop_event.is_set())

    def test_stop_speaking_is_idempotent(self):
        engine = SpeechEngine()
        for _ in range(3):
            engine.stop_speaking()
        self.assertTrue(engine._stop_event.is_set())

    def test_stop_speaking_stops_pygame_music(self):
        music = SimpleNamespace(stop=Mock())
        pygame = SimpleNamespace(mixer=SimpleNamespace(music=music))
        engine = SpeechEngine()
        with patch.dict(sys.modules, {"pygame": pygame}):
            engine.stop_speaking()
        music.stop.assert_called_once()


class FakeSpeech:
    def __init__(self):
        self.stop_calls = 0

    def stop_speaking(self):
        self.stop_calls += 1


class FakeStopApp:
    """Minimal app double for the bridge: records routed stop calls."""

    def __init__(self):
        self.settings = {"wake_phrase": "okay computer"}
        self.characters = {}
        self.selected_character = "luna"
        self.core_mode = "READY"
        self.wake_enabled = False
        self._busy = False
        self.events = queue.Queue()
        self.speech = FakeSpeech()

    def _stop_speaking(self):
        # Mirrors OrvixDesktop._stop_speaking: route to the engine.
        self.speech.stop_speaking()


def drain(app):
    executed = 0
    while True:
        try:
            callback, args = app.events.get_nowait()
        except queue.Empty:
            return executed
        callback(*args)
        executed += 1


class StopSpeakingBridgeTests(unittest.TestCase):
    def test_stop_speaking_bridge_enqueues_app_stop(self):
        app = FakeStopApp()
        api = OrvixWebHomeApi(app)
        self.assertEqual(api.stopSpeaking(), {"ok": True})
        self.assertEqual(app.speech.stop_calls, 0)  # nothing before drain
        drain(app)
        self.assertEqual(app.speech.stop_calls, 1)

    def test_payload_carries_configured_wake_phrase(self):
        stats = SystemStats(None, None, None, "—", "—", "—", None)
        payload = build_state_payload(FakeStopApp(), stats=stats)
        self.assertEqual(payload["wakePhrase"], "okay computer")

    def test_payload_wake_phrase_defaults_when_unconfigured(self):
        app = FakeStopApp()
        app.settings = {}
        stats = SystemStats(None, None, None, "—", "—", "—", None)
        payload = build_state_payload(app, stats=stats)
        self.assertEqual(payload["wakePhrase"], "hey orvix")


class VoiceWiringStaticTests(unittest.TestCase):
    """Static wiring checks, following test_orvix_webview_home style."""

    @classmethod
    def setUpClass(cls):
        cls.app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")
        cls.index_html = (APP_DIR / "web_home" / "index.html").read_text(encoding="utf-8")
        cls.app_js = (APP_DIR / "web_home" / "app.js").read_text(encoding="utf-8")

    def test_tk_stop_button_routes_to_engine_stop(self):
        self.assertIn("command=self._stop_speaking", self.app_py)
        self.assertIn("def _stop_speaking", self.app_py)
        self.assertIn("self.speech.stop_speaking()", self.app_py)

    def test_tk_wake_label_uses_configured_phrase(self):
        self.assertIn("wake_phrase_label", self.app_py)
        self.assertNotIn('text="Hey ORVIX"', self.app_py)

    def test_tk_wake_loop_uses_phrase_matcher(self):
        self.assertIn("match_wake_phrase", self.app_py)
        self.assertIn('self.settings.get("wake_phrase", "hey orvix")', self.app_py)

    def test_tk_speak_call_passes_rate_and_volume(self):
        self.assertIn('rate=str(self.settings.get("speech_rate", "+0%"))', self.app_py)
        self.assertIn('volume=str(self.settings.get("speech_volume", "+0%"))', self.app_py)

    def test_web_stop_button_and_bridge_call_present(self):
        self.assertIn('id="stopSpeakingButton"', self.index_html)
        self.assertIn("stopSpeaking", self.app_js)

    def test_web_wake_pill_uses_payload_phrase(self):
        self.assertIn("state.wakePhrase", self.app_js)
        self.assertIn("wakeButton.textContent = state.wakePhrase", self.app_js)


if __name__ == "__main__":
    unittest.main()
