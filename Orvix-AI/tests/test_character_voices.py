"""Tests for Premium Phase 7 — tabbed settings + per-character voices.

Covers:

- the ``effective_voice`` resolver: a character's own configured
  voice wins, the global ``voice`` is the fallback, and an unknown
  character falls back to the global voice;
- voice validation: only identifiers from the existing Settings
  combo list (``VOICE_IDS``) are accepted, for per-character voices
  exactly as for the global voice;
- Bug #10's fix: selecting a character persists ONLY the selection
  and no longer overwrites a user-customised global ``voice`` —
  pinned behaviourally (headless desktop double) and at source
  level, following the existing tests' style;
- the tabbed Settings dialog: a ttk.Notebook with the 10 category
  tabs, the Character tab's per-character combos fed from the same
  voice list, and the Appearance toggle wired end-to-end
  (settings -> bridge payload -> web_home/app.js + style.css).

No Tk root is created and no real audio is used.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import settings
from orvix.home_data import SystemStats
from orvix.webview_home import build_state_payload

APP_DIR = Path(__file__).resolve().parents[1]

GLOBAL = "en-US-GuyNeural"  # a user-customised global voice


def _settings(**overrides):
    data = {
        "voice": GLOBAL,
        "selected_character": "luna",
        "character_voice_luna": "en-GB-SoniaNeural",
        "character_voice_ruby": "bn-BD-PradeepNeural",
        "character_voice_maya": "en-US-JennyNeural",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# effective_voice resolver
# ---------------------------------------------------------------------------

class EffectiveVoiceResolverTests(unittest.TestCase):
    def test_per_character_value_wins_for_selected_character(self):
        self.assertEqual(
            settings.effective_voice(_settings()), "en-GB-SoniaNeural")
        self.assertEqual(
            settings.effective_voice(_settings(selected_character="ruby")),
            "bn-BD-PradeepNeural")
        self.assertEqual(
            settings.effective_voice(_settings(selected_character="maya")),
            "en-US-JennyNeural")

    def test_explicit_character_argument_wins_over_selection(self):
        self.assertEqual(
            settings.effective_voice(_settings(), "maya"), "en-US-JennyNeural")
        self.assertEqual(
            settings.effective_voice(_settings(), "RUBY"), "bn-BD-PradeepNeural")

    def test_missing_per_character_key_falls_back_to_global(self):
        data = _settings()
        del data["character_voice_luna"]
        self.assertEqual(settings.effective_voice(data), GLOBAL)

    def test_invalid_stored_per_character_value_falls_back_to_global(self):
        self.assertEqual(
            settings.effective_voice(
                _settings(character_voice_luna="not-a-real-voice")),
            GLOBAL)

    def test_unknown_character_falls_back_to_global(self):
        self.assertEqual(
            settings.effective_voice(_settings(selected_character="nova")),
            GLOBAL)
        self.assertEqual(settings.effective_voice(_settings(), "nova"), GLOBAL)

    def test_existing_saved_settings_without_new_keys_still_work(self):
        legacy = {"voice": GLOBAL, "selected_character": "ruby"}
        self.assertEqual(settings.effective_voice(legacy), GLOBAL)
        self.assertEqual(settings.effective_voice({}), settings.DEFAULTS["voice"])
        self.assertEqual(settings.effective_voice(None), settings.DEFAULTS["voice"])

    def test_character_voice_defaults_match_the_shipped_global_default(self):
        for key in ("luna", "ruby", "maya"):
            with self.subTest(character=key):
                self.assertEqual(
                    settings.DEFAULTS[f"character_voice_{key}"],
                    settings.DEFAULTS["voice"])
                self.assertIn(
                    settings.DEFAULTS[f"character_voice_{key}"],
                    settings.VOICE_IDS)


# ---------------------------------------------------------------------------
# Voice identifier validation
# ---------------------------------------------------------------------------

class VoiceIdValidationTests(unittest.TestCase):
    def test_voice_ids_are_the_existing_settings_combo_list(self):
        self.assertEqual(
            list(settings.VOICE_IDS),
            ["bn-BD-PradeepNeural", "bn-BD-NabanitaNeural",
             "en-US-GuyNeural", "en-US-JennyNeural",
             "en-GB-RyanNeural", "en-GB-SoniaNeural"])

    def test_every_listed_voice_is_accepted(self):
        for voice in settings.VOICE_IDS:
            with self.subTest(voice=voice):
                self.assertEqual(settings.validate_voice_id(voice), voice)

    def test_unlisted_identifiers_are_rejected(self):
        for raw in ("en-US-AriaNeural", "bn-BD-Nabanita", "aria", "",
                    "  ", None, "BN-BD-NABANITANEURAL"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    settings.validate_voice_id(raw)

    def test_character_voice_keys_cover_exactly_the_three_characters(self):
        self.assertEqual(
            settings.CHARACTER_VOICE_KEYS,
            {"luna": "character_voice_luna",
             "ruby": "character_voice_ruby",
             "maya": "character_voice_maya"})


# ---------------------------------------------------------------------------
# Bug #10: character selection must not clobber the global voice
# ---------------------------------------------------------------------------

class SelectCharacterBehaviourTests(unittest.TestCase):
    """Behavioural pin, run headless on a __new__ desktop double."""

    def _desktop(self):
        try:
            import app as app_module
        except Exception as exc:  # tkinter unavailable on this runner
            self.skipTest(f"app module unavailable: {exc}")
        desktop = app_module.OrvixDesktop.__new__(app_module.OrvixDesktop)
        desktop.characters = {
            "luna": {"name": "LUNA", "role": "Calm strategy companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "luna.jpg"},
            "ruby": {"name": "RUBY", "role": "Confident action companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "ruby.jpg"},
            "maya": {"name": "MAYA", "role": "Warm creative companion",
                     "voice": "bn-BD-NabanitaNeural", "image": "maya.jpg"},
        }
        desktop.selected_character = "luna"
        desktop.settings = _settings()
        desktop._refresh_character_cards = lambda: None
        desktop._render_core = lambda: None
        return app_module, desktop

    def test_selecting_a_character_does_not_modify_global_voice(self):
        app_module, desktop = self._desktop()
        saved = []
        with patch.object(app_module, "save_settings",
                          lambda data: saved.append(dict(data))):
            desktop._select_character("ruby")
        self.assertEqual(desktop.selected_character, "ruby")
        self.assertEqual(desktop.settings["selected_character"], "ruby")
        self.assertEqual(desktop.settings["voice"], GLOBAL)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["voice"], GLOBAL)
        self.assertEqual(saved[0]["selected_character"], "ruby")

    def test_selecting_each_character_keeps_customised_global_voice(self):
        app_module, desktop = self._desktop()
        with patch.object(app_module, "save_settings", lambda data: None):
            for key in ("maya", "luna", "ruby"):
                desktop._select_character(key)
                self.assertEqual(desktop.settings["voice"], GLOBAL)


class SelectCharacterSourceTests(unittest.TestCase):
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

    def test_select_character_never_writes_the_voice_setting(self):
        body = self.methods["_select_character"]
        self.assertNotIn('settings["voice"]', body)
        self.assertNotIn("settings['voice']", body)
        self.assertIn('settings["selected_character"]', body)

    def test_speak_path_resolves_through_effective_voice(self):
        self.assertIn("effective_voice(self.settings)",
                      self.methods["_speak_safely"])

    def test_character_dicts_keep_their_shipped_voice_identifier(self):
        # The character definitions themselves are untouched: all
        # three still carry the identifier they ship with today.
        self.assertEqual(
            self.source.count('"voice": "bn-BD-NabanitaNeural"'), 3)


# ---------------------------------------------------------------------------
# Tabbed settings dialog (static wiring, existing source-check style)
# ---------------------------------------------------------------------------

class TabbedSettingsStaticTests(unittest.TestCase):
    EXPECTED_TABS = ["General", "AI Model", "Voice", "Character",
                     "Appearance", "Startup", "Permissions",
                     "Notifications", "Privacy", "About"]

    @classmethod
    def setUpClass(cls):
        cls.app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")
        start = cls.app_py.index("    def _open_settings(self)")
        end = cls.app_py.index("    def _drain_events(self)", start)
        cls.dialog = cls.app_py[start:end]

    def test_notebook_has_the_ten_tabs_in_order(self):
        self.assertIn("ttk.Notebook(win)", self.dialog)
        positions = []
        for title in self.EXPECTED_TABS:
            marker = f'_settings_tab("{title}")'
            self.assertIn(marker, self.dialog)
            positions.append(self.dialog.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_existing_controls_live_on_their_tabs(self):
        expectations = {
            "general_tab": ('general_tab,\n            text="Autonomous Brain (multi-agent)',
                            'general_tab,\n            text="WebView Home — open the HTML home window'),
            "ai_model_tab": ('("API base URL", "api_base_url"',
                             '("Model name", "model"',
                             'ttk.Button(ai_model_tab, text="AUTO SETUP FROM API KEY"',
                             'ai_model_tab,\n            text="Test Connection requests /models only;'),
            "voice_tab": ('voice_tab, text="Speech recognition language"',
                          'voice_tab, text="Spoken reply voice"',
                          'voice_tab, text="Wake phrase (the phrase that activates'),
            "startup_tab": ('startup_tab,\n            text="Start ORVIX automatically when I sign in',),
            "notifications_tab": ('notifications_tab,\n            text="Notify when a task completes"',),
        }
        for parent, markers in expectations.items():
            for marker in markers:
                with self.subTest(parent=parent, marker=marker):
                    self.assertIn(marker, self.dialog)

    def test_character_tab_has_one_combo_per_character_from_voice_ids(self):
        self.assertIn("character_voice_vars", self.dialog)
        self.assertIn('("luna", "LUNA"), ("ruby", "RUBY"), ("maya", "MAYA")',
                      self.dialog)
        self.assertIn("values=list(VOICE_IDS)", self.dialog)
        for key in ("character_voice_luna", "character_voice_ruby",
                    "character_voice_maya"):
            self.assertIn(f'new["{key}"]', self.dialog)
        self.assertIn("validate_voice_id", self.dialog)
        # The note keeps the global Voice-tab voice as the fallback.
        self.assertIn("remains the fallback", self.dialog)

    def test_global_voice_combo_uses_the_same_voice_ids(self):
        self.assertIn(
            "voice_combo = ttk.Combobox(voice_tab, textvariable=voice_var, "
            "state=\"readonly\", values=list(VOICE_IDS))", self.dialog)

    def test_permissions_and_privacy_tabs_are_read_only_summaries(self):
        self.assertIn("Recycle Bin only", self.dialog)
        self.assertIn("Desktop, Documents and Downloads", self.dialog)
        self.assertIn("OS keyring (Windows Credential Manager)", self.dialog)
        self.assertIn("refused over remote plain HTTP", self.dialog)
        self.assertIn("privacy-safe", self.dialog)
        # No toggles were invented for those two tabs.
        permissions_start = self.dialog.index("# -- Permissions tab")
        about_start = self.dialog.index("# -- About tab")
        self.assertNotIn("Checkbutton", self.dialog[permissions_start:about_start])

    def test_about_tab_carries_the_real_version_facts(self):
        self.assertIn('f"Version {APP_VERSION}"', self.dialog)
        self.assertIn("https://github.com/zobaerio/orvixai", self.dialog)

    def test_save_path_still_applies_startup_before_persisting(self):
        save_start = self.dialog.index("        def save_and_close()")
        body = self.dialog[save_start:]
        self.assertLess(body.index("set_windows_startup"),
                        body.index("save_settings(new)"))
        self.assertIn('new["web_home_animations"] = bool(animations_var.get())',
                      body)


# ---------------------------------------------------------------------------
# Appearance toggle: settings -> payload -> page behaviour
# ---------------------------------------------------------------------------

class FakeAppearanceApp:
    def __init__(self, settings_dict):
        self.settings = settings_dict
        self.characters = {}
        self.selected_character = "luna"
        self.core_mode = "READY"
        self.wake_enabled = False
        self._busy = False


class AppearanceToggleTests(unittest.TestCase):
    def setUp(self):
        self.stats = SystemStats(None, None, None, "—", "—", "—", None)

    def test_payload_carries_animations_off(self):
        payload = build_state_payload(
            FakeAppearanceApp({"web_home_animations": False}),
            stats=self.stats)
        self.assertIs(payload["webHomeAnimations"], False)

    def test_payload_carries_animations_on_and_defaults_on(self):
        payload = build_state_payload(
            FakeAppearanceApp({"web_home_animations": True}),
            stats=self.stats)
        self.assertIs(payload["webHomeAnimations"], True)
        payload = build_state_payload(FakeAppearanceApp({}), stats=self.stats)
        self.assertIs(payload["webHomeAnimations"], True)

    def test_page_honours_the_payload_flag(self):
        app_js = (APP_DIR / "web_home" / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.webHomeAnimations === false", app_js)
        self.assertIn('classList.toggle("no-animations"', app_js)

    def test_stylesheet_disables_animations_under_the_class(self):
        css = (APP_DIR / "web_home" / "style.css").read_text(encoding="utf-8")
        self.assertIn("body.no-animations", css)
        self.assertIn("animation:none!important", css)
        self.assertIn("transition:none!important", css)

    def test_settings_dialog_collects_the_toggle(self):
        app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn('self.settings.get("web_home_animations", True)', app_py)
        self.assertIn("animations_var", app_py)


if __name__ == "__main__":
    unittest.main()
