"""Headless tests for the home-page pure data helpers (orvix/home_data).

The home page must never invent a number: these tests pin the N/A
behaviour for unreadable stats, the gauge geometry, the rolling
performance history and the model-status line assembly.
"""
import unittest

from orvix.home_data import (
    GAUGE_SWEEP_DEG, GAUGE_TRACK, PerfHistory, clamp_percent,
    format_os_name, format_percent, format_uptime, gauge_color,
    gauge_extent_deg, model_status_lines, performance_caption,
    quick_action_tiles,
)


class ClampPercentTests(unittest.TestCase):
    def test_real_values_pass_through(self):
        self.assertEqual(clamp_percent(42), 42.0)
        self.assertEqual(clamp_percent("7.5"), 7.5)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(clamp_percent(140), 100.0)
        self.assertEqual(clamp_percent(-3), 0.0)

    def test_unreadable_values_are_none(self):
        for bad in (None, "n/a", object(), float("nan"), float("inf")):
            self.assertIsNone(clamp_percent(bad))


class GaugeGeometryTests(unittest.TestCase):
    def test_full_scale_sweeps_the_whole_dial_clockwise(self):
        self.assertEqual(gauge_extent_deg(100), -GAUGE_SWEEP_DEG)

    def test_zero_and_half(self):
        self.assertEqual(gauge_extent_deg(0), 0.0)
        self.assertAlmostEqual(gauge_extent_deg(50), -GAUGE_SWEEP_DEG / 2)

    def test_missing_value_draws_an_empty_dial(self):
        self.assertEqual(gauge_extent_deg(None), 0.0)
        self.assertEqual(gauge_extent_deg("junk"), 0.0)

    def test_format_percent(self):
        self.assertEqual(format_percent(42.4), "42%")
        self.assertEqual(format_percent(0), "0%")
        self.assertEqual(format_percent(None), "N/A")


class GaugeColorTests(unittest.TestCase):
    def test_missing_value_uses_neutral_track(self):
        self.assertEqual(gauge_color("cpu", None), GAUGE_TRACK)
        self.assertEqual(gauge_color("ram", None), GAUGE_TRACK)

    def test_thresholds_warm_up(self):
        self.assertNotEqual(gauge_color("cpu", 95), gauge_color("cpu", 10))
        self.assertNotEqual(gauge_color("ram", 85), gauge_color("ram", 10))
        self.assertNotEqual(gauge_color("storage", 97), gauge_color("storage", 10))

    def test_unknown_kind_still_returns_a_colour(self):
        self.assertTrue(gauge_color("other", 10).startswith("#"))


class FormatTests(unittest.TestCase):
    def test_uptime(self):
        self.assertEqual(format_uptime(0), "00:00:00")
        self.assertEqual(format_uptime(3661), "01:01:01")
        self.assertEqual(format_uptime(90061), "25:01:01")

    def test_uptime_unknown_is_dash(self):
        self.assertEqual(format_uptime(None), "—")
        self.assertEqual(format_uptime(-5), "—")
        self.assertEqual(format_uptime("junk"), "—")

    def test_os_name(self):
        self.assertEqual(format_os_name("Windows", "11", "Pro"), "Windows 11 Pro")
        self.assertEqual(format_os_name("Linux", "6.8.0"), "Linux 6.8.0")
        self.assertEqual(format_os_name("", ""), "—")
        self.assertEqual(format_os_name("Windows", "", None), "Windows")


class PerfHistoryTests(unittest.TestCase):
    def test_empty_history_has_no_data(self):
        history = PerfHistory()
        self.assertFalse(history.has_data)
        self.assertEqual(performance_caption(history), "N/A")

    def test_push_and_caption_use_latest_real_sample(self):
        history = PerfHistory()
        history.push(10, 20)
        history.push(30, None)
        self.assertTrue(history.has_data)
        self.assertEqual(history.cpu_series(), (10.0, 30.0))
        self.assertEqual(performance_caption(history), "CPU 30% · RAM N/A")

    def test_history_is_bounded(self):
        history = PerfHistory(maxlen=3)
        for value in range(6):
            history.push(value, value)
        self.assertEqual(len(history.samples()), 3)
        self.assertEqual(history.cpu_series(), (3.0, 4.0, 5.0))

    def test_all_unreadable_samples_have_no_data(self):
        history = PerfHistory()
        history.push(None, None)
        self.assertFalse(history.has_data)


class ModelStatusLineTests(unittest.TestCase):
    @staticmethod
    def _available(*names):
        return lambda name: name in names

    def test_local_endpoint_configuration(self):
        lines = model_status_lines(
            {"api_base_url": "http://localhost:11434/v1", "model": "qwen2.5:3b"},
            core_mode="READY", wake_enabled=False,
            module_available=self._available("edge_tts"))
        by_label = {line.label: line for line in lines}
        self.assertEqual(by_label["ENDPOINT"].value, "Local endpoint configured")
        self.assertTrue(by_label["ENDPOINT"].state)
        # The home page never probes the network: internet stays honest.
        self.assertEqual(by_label["INTERNET"].value, "Not checked")
        self.assertIsNone(by_label["INTERNET"].state)
        self.assertEqual(by_label["AI CORE"].value, "READY")
        self.assertEqual(by_label["VOICE"].value, "Wake word OFF")
        self.assertEqual(by_label["TTS"].value, "Engine ready")

    def test_unconfigured_endpoint_is_flagged(self):
        lines = model_status_lines({}, core_mode="", wake_enabled=True,
                                   module_available=self._available())
        by_label = {line.label: line for line in lines}
        self.assertEqual(by_label["ENDPOINT"].value, "Not configured")
        self.assertFalse(by_label["ENDPOINT"].state)
        self.assertEqual(by_label["AI CORE"].value, "READY")
        self.assertEqual(by_label["VOICE"].value, "Wake word ON")
        self.assertEqual(by_label["TTS"].value, "Engine not installed")
        self.assertFalse(by_label["TTS"].state)

    def test_remote_endpoint(self):
        lines = model_status_lines(
            {"api_base_url": "https://api.example.test/v1", "model": "m"},
            core_mode="EXECUTING", wake_enabled=False,
            module_available=self._available("pyttsx3"))
        by_label = {line.label: line for line in lines}
        self.assertEqual(by_label["ENDPOINT"].value, "Remote endpoint configured")
        self.assertEqual(by_label["AI CORE"].value, "EXECUTING")


class QuickTileTests(unittest.TestCase):
    def test_six_tiles_in_mockup_order(self):
        tiles = quick_action_tiles()
        self.assertEqual([t.key for t in tiles],
                         ["chrome", "youtube", "notepad", "calculator", "explorer", "power"])
        for tile in tiles:
            self.assertTrue(tile.label)
            self.assertTrue(tile.command)


if __name__ == "__main__":
    unittest.main()
