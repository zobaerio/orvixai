"""Command Center Phase 8 — AI Core page, personality themes,
hero controls made real (audit Bugs #1, Bugs #7, Gap (e)).

Coverage, all headless with fake app objects at the established
boundaries (no Tk root, no window):

* ``orvix/character_theme.py`` — the pure character -> ambient
  accent mapping: one distinct in-palette accent per character,
  case-insensitive lookup, the base-cyan fallback for unknown
  characters, and exact agreement with the stylesheet's
  ``[data-character="..."]`` rules so the two can never drift.
* The payload (``orvix/webview_home.py``): the additive
  ``characterAccent`` for the selected character, and the
  ``approvals`` block's truth conditions — dialog open/closed is
  read from the app's real approval-dialog state, the waiting
  request's title/message are carried only while the dialog is
  open, and nothing is invented for an app that exposes neither.
* Static web checks: the hero caption pills are real buttons
  wired to the existing bridge paths (Speak -> the composer mic
  path / startListening, Command -> composer focus, Get Results
  -> the latest assistant message in chat), disabled standalone
  like the other bridge controls; the Core nav destination and
  view render all eight core states with the live highlight
  following the payload state, plus the OFFLINE presentation
  that is the bridge-disconnected case only (not a ninth core
  state); the payload character drives the root data-character
  attribute; and the state-colour systems (FX_STATE_COLORS in
  app.js, STATE_PARAMS accents in orvix/core_states.py) are
  byte/value-identical to before — the accent never touches
  them.
* Tk parity statics (app.py): the selected character card tint
  comes from the shared mapping, and the approval dialog records
  / clears the current request the payload reads.
* The shipped character trait lines are byte-unchanged.
"""
import queue
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.character_theme import (
    CHARACTER_ACCENTS,
    DEFAULT_CHARACTER_ACCENT,
    character_accent,
)
from orvix.core_states import CoreState, STATE_PARAMS
from orvix.home_data import SystemStats
from orvix.webview_home import build_state_payload

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
        self._approval_dialog = None
        self._approval_request = None


def make_stats():
    return SystemStats(
        cpu_percent=10.0, ram_percent=20.0, storage_percent=30.0,
        os_name="Windows 11", os_build="26100", machine="AMD64",
        uptime_seconds=3600.0)


# ---------------------------------------------------------------------------
# Character -> ambient accent mapping
# ---------------------------------------------------------------------------

class CharacterAccentTests(unittest.TestCase):
    def test_each_character_has_its_accent(self):
        self.assertEqual(character_accent("luna"), "#38bdf8")
        self.assertEqual(character_accent("ruby"), "#e77cff")
        self.assertEqual(character_accent("maya"), "#ffb36b")

    def test_accents_are_distinct(self):
        self.assertEqual(len(set(CHARACTER_ACCENTS.values())), 3)

    def test_lookup_is_case_insensitive_and_trimmed(self):
        self.assertEqual(character_accent("LUNA"), "#38bdf8")
        self.assertEqual(character_accent(" Ruby "), "#e77cff")
        self.assertEqual(character_accent("MAYA"), "#ffb36b")

    def test_unknown_or_missing_character_gets_the_base_accent(self):
        self.assertEqual(character_accent("nobody"), DEFAULT_CHARACTER_ACCENT)
        self.assertEqual(character_accent(""), DEFAULT_CHARACTER_ACCENT)
        self.assertEqual(character_accent(None), DEFAULT_CHARACTER_ACCENT)
        self.assertEqual(DEFAULT_CHARACTER_ACCENT, "#00dfff")

    def test_stylesheet_mirrors_the_mapping_exactly(self):
        css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")
        for key, hex_value in CHARACTER_ACCENTS.items():
            with self.subTest(character=key):
                match = re.search(
                    r':root\[data-character="%s"\]\{--accent:(#[0-9a-fA-F]{6});'
                    r"--accent-rgb:(\d+),(\d+),(\d+)\}" % key.upper(), css)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1).lower(), hex_value.lower())
                rgb = tuple(int(match.group(i)) for i in (2, 3, 4))
                expected = tuple(
                    int(hex_value[i:i + 2], 16) for i in (1, 3, 5))
                self.assertEqual(rgb, expected)

    def test_payload_carries_the_selected_characters_accent(self):
        app = FakeWebApp()
        for key, hex_value in CHARACTER_ACCENTS.items():
            with self.subTest(character=key):
                app.selected_character = key
                payload = build_state_payload(app, stats=make_stats())
                self.assertEqual(payload["characterAccent"], hex_value)
                self.assertEqual(payload["character"], key)


# ---------------------------------------------------------------------------
# Approvals payload: truth conditions of the honest panel
# ---------------------------------------------------------------------------

class ApprovalsPayloadTests(unittest.TestCase):
    def test_no_dialog_open_reports_no_pending_approval(self):
        payload = build_state_payload(FakeWebApp(), stats=make_stats())
        self.assertEqual(payload["approvals"],
                         {"dialogOpen": False, "title": None,
                          "message": None})

    def test_open_dialog_carries_the_waiting_request(self):
        app = FakeWebApp()
        app._approval_dialog = object()  # any live dialog object
        app._approval_request = {
            "title": "Approve file action",
            "message": "Delete report.txt from Downloads?",
        }
        payload = build_state_payload(app, stats=make_stats())
        self.assertEqual(payload["approvals"], {
            "dialogOpen": True,
            "title": "Approve file action",
            "message": "Delete report.txt from Downloads?",
        })

    def test_open_dialog_without_a_recorded_request_stays_honest(self):
        app = FakeWebApp()
        app._approval_dialog = object()
        app._approval_request = None
        payload = build_state_payload(app, stats=make_stats())
        self.assertEqual(payload["approvals"],
                         {"dialogOpen": True, "title": None,
                          "message": None})

    def test_stale_request_is_never_reported_without_the_dialog(self):
        app = FakeWebApp()
        app._approval_dialog = None
        app._approval_request = {"title": "Old", "message": "Stale"}
        payload = build_state_payload(app, stats=make_stats())
        self.assertEqual(payload["approvals"],
                         {"dialogOpen": False, "title": None,
                          "message": None})

    def test_app_without_approval_attributes_reports_none_pending(self):
        app = FakeWebApp()
        del app._approval_dialog
        del app._approval_request
        payload = build_state_payload(app, stats=make_stats())
        self.assertEqual(payload["approvals"]["dialogOpen"], False)
        self.assertIsNone(payload["approvals"]["title"])


# ---------------------------------------------------------------------------
# Static web checks: hero controls, Core page, themes
# ---------------------------------------------------------------------------

class StaticPhase8Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")
        cls.app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")

    # -- Hero controls made real (Bugs #1) -------------------------
    def test_hero_caption_pills_are_real_buttons(self):
        for element_id, label in (("heroSpeakButton", "Speak"),
                                  ("heroCommandButton", "Command"),
                                  ("heroResultsButton", "Get Results")):
            with self.subTest(button=element_id):
                self.assertIn(
                    f'<button class="hero-action" id="{element_id}" '
                    f'type="button">{label}</button>', self.index_html)
        # The inert caption spans are gone.
        self.assertNotIn("<span>Speak</span>", self.index_html)
        self.assertNotIn("<span>Get Results</span>", self.index_html)
        # The pill container styling is kept.
        self.assertIn('class="hero-caption"', self.index_html)
        self.assertIn(".hero-caption{", self.style_css)
        self.assertIn(".hero-action{", self.style_css)

    def test_speak_button_uses_the_existing_mic_listen_path(self):
        # The mic button's own handler is the one bridge
        # startListening path; the hero Speak button clicks it.
        self.assertIn('callBridge("startListening", [])', self.app_js)
        hero = re.search(
            r'\$\("#heroSpeakButton"\)\.addEventListener\("click", '
            r"\(\) => \{(.*?)\n\}\);", self.app_js, re.S)
        self.assertIsNotNone(hero)
        self.assertIn('$("#micButton")', hero.group(1))
        self.assertIn(".click()", hero.group(1))

    def test_command_button_focuses_the_composer(self):
        hero = re.search(
            r'\$\("#heroCommandButton"\)\.addEventListener\("click", '
            r"\(\) => \{(.*?)\n\}\);", self.app_js, re.S)
        self.assertIsNotNone(hero)
        self.assertIn('$("#commandInput")', hero.group(1))
        self.assertIn(".focus()", hero.group(1))

    def test_get_results_targets_the_latest_assistant_message(self):
        hero = re.search(
            r'\$\("#heroResultsButton"\)\.addEventListener\("click", '
            r"\(\) => \{(.*?)\n\}\);", self.app_js, re.S)
        self.assertIsNotNone(hero)
        body = hero.group(1)
        self.assertIn('querySelectorAll(".assistant-message")', body)
        self.assertIn("scrollIntoView", body)
        self.assertIn("result-flash", body)
        # Honest empty case: no assistant message yet.
        self.assertIn("No results yet", body)

    def test_hero_buttons_disabled_standalone_like_bridge_controls(self):
        self.assertIn("function updateHeroButtons()", self.app_js)
        self.assertIn("button.disabled = !connected", self.app_js)
        for element_id in ("heroSpeakButton", "heroCommandButton",
                           "heroResultsButton"):
            self.assertIn(f'"#{element_id}"', self.app_js)
        # setConnectedUI refreshes them on connect/disconnect.
        self.assertIn("updateHeroButtons();", self.app_js)
        self.assertIn(".hero-action:disabled", self.style_css)

    # -- AI Core page ----------------------------------------------
    def test_core_nav_destination_and_view_exist(self):
        self.assertIn('data-tab="core"', self.index_html)
        self.assertIn("<span>AI Core</span>", self.index_html)
        self.assertIn('id="coreView"', self.index_html)
        self.assertIn('id="coreBackHome"', self.index_html)
        self.assertIn('tab === "core"', self.app_js)
        self.assertIn("renderCore(lastState)", self.app_js)
        self.assertIn("renderCore(state)", self.app_js)
        self.assertIn("renderCore(null)", self.app_js)

    def test_state_panel_lists_exactly_the_eight_core_states(self):
        expected = [state.value for state in CoreState]
        self.assertEqual(len(expected), 8)
        for key in expected:
            with self.subTest(state=key):
                self.assertIn(f'data-core-state="{key}"', self.index_html)
                self.assertIn(f'"{key}"', self.app_js)
        self.assertIn("CORE_STATE_KEYS", self.app_js)
        # The panel's key list in app.js matches the state machine.
        match = re.search(
            r"const CORE_STATE_KEYS = \[(.*?)\];", self.app_js, re.S)
        self.assertIsNotNone(match)
        listed = re.findall(r'"([a-z]+)"', match.group(1))
        self.assertEqual(listed, expected)

    def test_live_highlight_follows_the_payload_state(self):
        self.assertIn("corePanelStateKey", self.app_js)
        # The bridge's "ready" key is the idle state on the panel.
        self.assertIn('if (key === "ready") return "idle";', self.app_js)
        self.assertIn('row.classList.toggle("live"', self.app_js)
        self.assertIn(".core-state-row.live", self.style_css)

    def test_offline_is_a_page_presentation_not_a_core_state(self):
        self.assertIn('data-core-state="offline"', self.index_html)
        self.assertIn("OFFLINE is not a core state", self.index_html)
        # Disconnected -> the OFFLINE row is the highlighted one.
        self.assertIn('key === "offline"', self.app_js)
        # The state machine itself is untouched: still 8 states,
        # none of them offline.
        self.assertNotIn("offline", [state.value for state in CoreState])
        core_states_src = (APP_DIR / "orvix" / "core_states.py").read_text(
            encoding="utf-8")
        self.assertNotIn("OFFLINE", core_states_src)

    def test_core_page_reuses_operation_activity_and_approvals(self):
        self.assertIn('id="coreCurrentOperation"', self.index_html)
        self.assertIn('id="coreActivityList"', self.index_html)
        self.assertIn('id="coreApprovals"', self.index_html)
        self.assertIn("state.currentOperation", self.app_js)
        self.assertIn("state.activity", self.app_js)
        self.assertIn("state.approvals", self.app_js)
        self.assertIn("renderCoreApprovals", self.app_js)

    def test_approvals_panel_is_read_only_and_honest(self):
        self.assertIn("No pending approvals — approval requests "
                      "appear as dialogs in the desktop app.",
                      self.index_html)
        self.assertIn("Answer it in the desktop dialog", self.app_js)
        self.assertIn("waiting in the desktop app", self.app_js)
        # No bridge method answers an approval from the page.
        self.assertNotIn("answerApproval", self.app_js)
        self.assertNotIn("approveRequest", self.app_js)

    # -- Personality themes -----------------------------------------
    def test_root_data_character_attribute_and_payload_driver(self):
        self.assertIn('<html lang="en" data-character="LUNA">',
                      self.index_html)
        self.assertIn("document.documentElement.dataset.character = key;",
                      self.app_js)
        # The accent variables feed ambient surfaces only.
        self.assertIn(":root[data-character] .aurora{", self.style_css)
        self.assertIn(":root[data-character] .panel{", self.style_css)
        self.assertIn(":root[data-character] .character.selected{",
                      self.style_css)

    def test_state_colours_are_untouched_by_themes(self):
        # The FX canvas palette is byte-identical to before.
        fx = re.search(
            r"const FX_STATE_COLORS = \{(.*?)\};", self.app_js, re.S)
        self.assertIsNotNone(fx)
        self.assertEqual(
            re.findall(r'(\w+): "(#[0-9a-f]+)"', fx.group(1)),
            [("idle", "#00dfff"), ("ready", "#00dfff"),
             ("listening", "#00e88f"), ("thinking", "#a78bfa"),
             ("processing", "#ffb020"), ("executing", "#38bdf8"),
             ("speaking", "#2dd4bf"), ("completed", "#00e88f"),
             ("error", "#ff4d5e")])
        # The state machine's per-state accents are unchanged.
        self.assertEqual(
            {state: params.accent for state, params in STATE_PARAMS.items()},
            {CoreState.IDLE: "#2fb9ff", CoreState.LISTENING: "#22d3ee",
             CoreState.THINKING: "#8b7cff", CoreState.PROCESSING: "#2fb9ff",
             CoreState.EXECUTING: "#2dd4bf", CoreState.SPEAKING: "#22d3ee",
             CoreState.COMPLETED: "#2dd4bf", CoreState.ERROR: "#ff5a5a"})
        # No Phase 8 CSS rule recolours a reactor/state surface.
        phase8 = self.style_css.split("Command Center Phase 8", 1)[1]
        self.assertNotIn(".reactor", phase8)
        self.assertNotIn("[data-state", phase8)

    # -- Tk parity + approval record (app.py statics) ---------------
    def test_tk_selected_character_card_uses_the_shared_accent(self):
        self.assertIn("from orvix.character_theme import character_accent",
                      self.app_py)
        refresh = re.search(
            r"def _refresh_character_cards\(self\)(.*?)\n    def ",
            self.app_py, re.S)
        self.assertIsNotNone(refresh)
        self.assertIn("accent = character_accent(key)", refresh.group(1))
        self.assertIn("highlightbackground=accent if selected",
                      refresh.group(1))

    def test_tk_approval_dialog_records_and_clears_the_request(self):
        self.assertIn(
            'self._approval_request = {"title": str(title), '
            '"message": str(message)}', self.app_py)
        dismiss = re.search(
            r"def _dismiss_approval_dialog\(self\)(.*?)\n    def ",
            self.app_py, re.S)
        self.assertIsNotNone(dismiss)
        self.assertIn("self._approval_request = None", dismiss.group(1))

    def test_character_trait_lines_are_byte_unchanged(self):
        for line in ("Calm strategy companion",
                     "Confident action companion",
                     "Warm creative companion"):
            with self.subTest(line=line):
                self.assertIn(line, self.app_py)
                self.assertIn(line, self.index_html)


class NoBannedWordPhase8Tests(unittest.TestCase):
    def test_phase8_files_have_no_banned_word(self):
        # Fragments are joined at runtime so this guard does not
        # itself contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("orvix/character_theme.py", "orvix/webview_home.py",
                    "app.py", "web_home/index.html", "web_home/app.js",
                    "web_home/style.css",
                    "tests/test_orvix_command_center_phase8.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
