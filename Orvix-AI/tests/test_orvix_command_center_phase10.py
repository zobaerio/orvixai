"""Command Center Phase 10 — appearance accent presets, the
INTERNET status line fix (audit Bugs #5), responsive CSS guards,
and the Appearance-tab wiring.

Coverage, all headless with fake app objects at the established
boundaries (no Tk root, no window):

* ``orvix/settings.py`` — the accent preset table and its
  validation contract: 3-4 curated presets, the default is the
  shipped palette, unknown stored values resolve to the default
  through the never-raising resolver, and the strict validator
  rejects unknown keys like the neighbouring validators.
* The payload (``orvix/webview_home.py``): the additive
  ``accentPreset`` carries the resolved preset, and the model
  panel's INTERNET line now reflects the Phase 8a probe verdict
  in the probe's own vocabulary for every health state.
* ``orvix/home_data.model_status_lines``: every probe state maps
  to its line, and with no probe verdict the line stays honestly
  "Not checked" (no probe ran in that context).
* Static web checks: the stylesheet mirrors ACCENT_PRESETS
  exactly in its [data-accent="..."] rules, the brand-token rules
  never touch the character layer's surfaces, app.js drives the
  root data-accent attribute from the payload, and the small-size
  responsive guards (nav scroll, column stacking, command-bar
  fit) are present.
* Tk statics (app.py): the Appearance tab picker is wired
  through the normal save path with validation, and the preset
  application is scoped to the AI-model panel accent surfaces.
"""
import queue
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix import settings as settings_module
from orvix.home_data import SystemStats, model_status_lines
from orvix.webview_home import build_state_payload

APP_DIR = Path(__file__).resolve().parents[1]
WEB_HOME_DIR = APP_DIR / "web_home"


class FakeWebApp:
    """The established FakeApp pattern (minimal bridge app)."""

    def __init__(self, settings=None, endpoint_health=None):
        self.settings = settings if settings is not None else {
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
        if endpoint_health is not None:
            self.endpoint_health = endpoint_health


def make_stats():
    return SystemStats(
        cpu_percent=10.0, ram_percent=20.0, storage_percent=30.0,
        os_name="Windows 11", os_build="26100", machine="AMD64",
        uptime_seconds=3600.0)


def _available(*names):
    return lambda name: name in names


# ---------------------------------------------------------------------------
# Accent preset table + validation
# ---------------------------------------------------------------------------

class AccentPresetSettingsTests(unittest.TestCase):
    def test_three_to_four_curated_presets_exist(self):
        self.assertGreaterEqual(len(settings_module.ACCENT_PRESETS), 3)
        self.assertLessEqual(len(settings_module.ACCENT_PRESETS), 4)
        for key, colors in settings_module.ACCENT_PRESETS.items():
            with self.subTest(preset=key):
                self.assertEqual(key, key.lower())
                for field in ("label", "accent", "primary", "secondary"):
                    self.assertIn(field, colors)
                for field in ("accent", "primary", "secondary"):
                    self.assertRegex(colors[field], r"^#[0-9a-fA-F]{6}$")

    def test_default_preset_is_the_shipped_palette(self):
        self.assertEqual(settings_module.ACCENT_PRESET_DEFAULT, "neon_blue")
        self.assertEqual(settings_module.DEFAULTS["accent_preset"], "neon_blue")
        colors = settings_module.ACCENT_PRESETS["neon_blue"]
        # The Phase 11 design tokens exactly: nothing changes for
        # existing users under the default preset.
        self.assertEqual(colors["accent"], "#00dfff")
        self.assertEqual(colors["primary"], "#008cff")
        self.assertEqual(colors["secondary"], "#146bff")

    def test_presets_are_distinct(self):
        accents = [c["accent"] for c in settings_module.ACCENT_PRESETS.values()]
        self.assertEqual(len(set(accents)), len(accents))

    def test_validate_accepts_known_keys_case_insensitively(self):
        for key in settings_module.ACCENT_PRESETS:
            with self.subTest(preset=key):
                self.assertEqual(
                    settings_module.validate_accent_preset(key), key)
                self.assertEqual(
                    settings_module.validate_accent_preset(key.upper()), key)
                self.assertEqual(
                    settings_module.validate_accent_preset(f" {key} "), key)

    def test_validate_rejects_unknown_values(self):
        for bad in ("", "neon", "blue", "custom", None, 42):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    settings_module.validate_accent_preset(bad)

    def test_effective_resolver_falls_back_to_default(self):
        eff = settings_module.effective_accent_preset
        self.assertEqual(eff({}), "neon_blue")
        self.assertEqual(eff(None), "neon_blue")
        self.assertEqual(eff({"accent_preset": "bogus"}), "neon_blue")
        self.assertEqual(eff({"accent_preset": None}), "neon_blue")
        for key in settings_module.ACCENT_PRESETS:
            self.assertEqual(eff({"accent_preset": key}), key)
            self.assertEqual(eff({"accent_preset": key.upper()}), key)

    def test_colors_lookup_never_raises_and_returns_a_copy(self):
        colors = settings_module.accent_preset_colors("emerald")
        self.assertEqual(colors, settings_module.ACCENT_PRESETS["emerald"])
        colors["accent"] = "#000000"
        self.assertNotEqual(
            settings_module.ACCENT_PRESETS["emerald"]["accent"], "#000000")
        self.assertEqual(settings_module.accent_preset_colors("bogus"),
                         settings_module.ACCENT_PRESETS["neon_blue"])
        self.assertEqual(settings_module.accent_preset_colors(None),
                         settings_module.ACCENT_PRESETS["neon_blue"])


# ---------------------------------------------------------------------------
# Payload: accentPreset + INTERNET line
# ---------------------------------------------------------------------------

class AccentPayloadTests(unittest.TestCase):
    def test_payload_carries_the_default_preset(self):
        payload = build_state_payload(FakeWebApp(), stats=make_stats())
        self.assertEqual(payload["accentPreset"], "neon_blue")

    def test_payload_carries_each_chosen_preset(self):
        for key in settings_module.ACCENT_PRESETS:
            with self.subTest(preset=key):
                app = FakeWebApp(settings={
                    "model": "qwen2.5:3b",
                    "api_base_url": "http://localhost:11434/v1",
                    "accent_preset": key,
                })
                payload = build_state_payload(app, stats=make_stats())
                self.assertEqual(payload["accentPreset"], key)

    def test_payload_unknown_stored_preset_resolves_to_default(self):
        app = FakeWebApp(settings={
            "model": "qwen2.5:3b",
            "api_base_url": "http://localhost:11434/v1",
            "accent_preset": "hand-edited-nonsense",
        })
        payload = build_state_payload(app, stats=make_stats())
        self.assertEqual(payload["accentPreset"], "neon_blue")

    def test_character_layer_still_carried_alongside(self):
        payload = build_state_payload(FakeWebApp(), stats=make_stats())
        self.assertEqual(payload["character"], "luna")
        self.assertEqual(payload["characterAccent"], "#38bdf8")


class InternetLinePayloadTests(unittest.TestCase):
    def _internet_line(self, app):
        payload = build_state_payload(app, stats=make_stats())
        by_label = {line["label"]: line
                    for line in payload["model"]["statusLines"]}
        return by_label["INTERNET"], payload

    def test_no_probe_attribute_stays_not_checked(self):
        line, _ = self._internet_line(FakeWebApp())
        self.assertEqual(line["value"], "Not checked")
        self.assertIsNone(line["state"])

    def test_each_probe_state_is_reflected(self):
        expected = {
            "reachable": ("Reachable", True),
            "unreachable": ("Unreachable", False),
            "checking": ("Checking", None),
            "not_configured": ("Not configured", False),
        }
        for health, (value, state) in expected.items():
            with self.subTest(health=health):
                line, payload = self._internet_line(
                    FakeWebApp(endpoint_health=health))
                self.assertEqual(line["value"], value)
                self.assertEqual(line["state"], state)
                # The line and the model.health block agree: both
                # come from the one probe verdict.
                self.assertEqual(payload["model"]["health"], health)

    def test_unrecognised_probe_value_stays_not_checked(self):
        line, payload = self._internet_line(
            FakeWebApp(endpoint_health="something-else"))
        self.assertEqual(line["value"], "Not checked")
        self.assertIsNone(payload["model"]["health"])


class InternetLineUnitTests(unittest.TestCase):
    def _lines(self, **kwargs):
        return model_status_lines(
            {"api_base_url": "http://localhost:11434/v1",
             "model": "qwen2.5:3b"},
            core_mode="READY", wake_enabled=False,
            module_available=_available("edge_tts"), **kwargs)

    def test_all_probe_states(self):
        expected = {
            "reachable": ("Reachable", True),
            "unreachable": ("Unreachable", False),
            "checking": ("Checking", None),
            "not_configured": ("Not configured", False),
        }
        for health, (value, state) in expected.items():
            with self.subTest(health=health):
                by_label = {line.label: line
                            for line in self._lines(endpoint_health=health)}
                self.assertEqual(by_label["INTERNET"].value, value)
                self.assertEqual(by_label["INTERNET"].state, state)

    def test_no_verdict_and_unknown_verdict_stay_not_checked(self):
        for health in (None, "", "bogus"):
            with self.subTest(health=health):
                by_label = {line.label: line
                            for line in self._lines(endpoint_health=health)}
                self.assertEqual(by_label["INTERNET"].value, "Not checked")
                self.assertIsNone(by_label["INTERNET"].state)

    def test_other_lines_are_unchanged_by_the_probe(self):
        by_label = {line.label: line
                    for line in self._lines(endpoint_health="reachable")}
        self.assertEqual(by_label["ENDPOINT"].value,
                         "Local endpoint configured")
        self.assertEqual(by_label["AI CORE"].value, "READY")
        self.assertEqual(by_label["VOICE"].value, "Wake word OFF")
        self.assertEqual(by_label["TTS"].value, "Engine ready")


# ---------------------------------------------------------------------------
# Static web checks: presets, composition, responsive guards
# ---------------------------------------------------------------------------

def _rgb(hex_value):
    return tuple(int(hex_value[i:i + 2], 16) for i in (1, 3, 5))


class StaticPhase10Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")
        cls.app_py = (APP_DIR / "app.py").read_text(encoding="utf-8")

    def test_stylesheet_mirrors_the_preset_table_exactly(self):
        for key, colors in settings_module.ACCENT_PRESETS.items():
            with self.subTest(preset=key):
                match = re.search(
                    r':root\[data-accent="%s"\]\{--cyan:(#[0-9a-fA-F]{6});'
                    r"--primary:(#[0-9a-fA-F]{6});"
                    r"--secondary:(#[0-9a-fA-F]{6});"
                    r"--brand:(#[0-9a-fA-F]{6});"
                    r"--brand-rgb:(\d+),(\d+),(\d+);"
                    r"--brand-primary:(#[0-9a-fA-F]{6});"
                    r"--brand-primary-rgb:(\d+),(\d+),(\d+);"
                    r"--brand-secondary:(#[0-9a-fA-F]{6});"
                    r"--brand-secondary-rgb:(\d+),(\d+),(\d+)\}"
                    % key.upper(), self.style_css)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1).lower(), colors["accent"])
                self.assertEqual(match.group(2).lower(), colors["primary"])
                self.assertEqual(match.group(3).lower(), colors["secondary"])
                self.assertEqual(match.group(4).lower(), colors["accent"])
                self.assertEqual(
                    tuple(int(match.group(i)) for i in (5, 6, 7)),
                    _rgb(colors["accent"]))
                self.assertEqual(match.group(8).lower(), colors["primary"])
                self.assertEqual(
                    tuple(int(match.group(i)) for i in (9, 10, 11)),
                    _rgb(colors["primary"]))
                self.assertEqual(match.group(12).lower(), colors["secondary"])
                self.assertEqual(
                    tuple(int(match.group(i)) for i in (13, 14, 15)),
                    _rgb(colors["secondary"]))

    def test_root_carries_the_default_accent_attribute(self):
        self.assertIn('data-accent="NEON_BLUE"', self.index_html)

    def test_app_js_drives_data_accent_from_the_payload(self):
        self.assertIn("state.accentPreset", self.app_js)
        self.assertIn(
            "document.documentElement.dataset.accent = "
            "String(state.accentPreset).toUpperCase();", self.app_js)

    def test_brand_layer_never_touches_character_surfaces(self):
        phase10 = self.style_css.split("Command Center Phase 10", 1)[1]
        # The character layer owns these surfaces (Phase 8 rules).
        for selector in (".aurora", ".character.selected", "--accent:"):
            self.assertNotIn(selector, phase10)
        # The character rules themselves are still present.
        self.assertIn(':root[data-character="MAYA"]', self.style_css)
        self.assertIn(":root[data-character] .aurora{", self.style_css)

    def test_brand_surfaces_consume_the_brand_tokens(self):
        for selector in (".brand-mark", ".send-button", ".mic-button",
                         ".nav-item.active"):
            with self.subTest(selector=selector):
                self.assertIn(
                    f":root[data-accent] {selector}{{", self.style_css)
        self.assertIn("var(--brand)", self.style_css)

    def test_responsive_guards_for_the_smallest_sizes(self):
        # Short-height guard: the hero shrinks instead of crowding
        # the panels below it at 720px-class heights.
        self.assertIn("@media(max-height:800px)", self.style_css)
        self.assertIn(".hero{min-height:300px", self.style_css)
        # Narrow guard: the top navigation scrolls horizontally
        # instead of overflowing, and the dashboard stacks.
        self.assertIn("overflow-x:auto", self.style_css)
        self.assertIn(".main-nav{", self.style_css)

    # -- Tk wiring (app.py statics) ---------------------------------
    def test_appearance_tab_has_the_preset_picker(self):
        self.assertIn("accent_preset_var", self.app_py)
        self.assertIn("effective_accent_preset(self.settings)", self.app_py)
        self.assertIn("values=list(ACCENT_PRESETS)", self.app_py)
        self.assertIn('text="Accent preset"', self.app_py)

    def test_preset_is_saved_through_the_normal_save_path(self):
        self.assertIn('new["accent_preset"] = accent_preset_var.get()',
                      self.app_py)
        self.assertIn(
            'new["accent_preset"] = validate_accent_preset(new["accent_preset"])',
            self.app_py)

    def test_tk_application_is_scoped_to_the_model_panel(self):
        self.assertIn(
            "self.accent_color = accent_preset_colors(", self.app_py)
        self.assertIn('fg=self.accent_color', self.app_py)
        self.assertIn("self.configure_model_button", self.app_py)
        # The module-level constant itself is NOT recoloured: the
        # broad widget styles keep the shipped constant.
        self.assertIn('ACCENT = "#22d3ee"', self.app_py)

    def test_tk_model_lines_receive_the_probe_verdict(self):
        self.assertIn(
            'endpoint_health=getattr(self, "_endpoint_health_state", None)',
            self.app_py)


class NoBannedWordPhase10Tests(unittest.TestCase):
    def test_phase10_files_have_no_banned_word(self):
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("orvix/settings.py", "orvix/home_data.py",
                    "orvix/webview_home.py", "app.py",
                    "web_home/index.html", "web_home/app.js",
                    "web_home/style.css", "FEATURE_STATUS.md",
                    "WEBVIEW_HOME.md",
                    "tests/test_orvix_command_center_phase10.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
