"""Command Center Phase 7 — Live Performance Center.

Coverage, all headless with fake timestamps / fake stats at the
existing boundaries (no real psutil dependence beyond the existing
patterns, no Tk root, no window):

* ``PerformanceHistory`` in orvix/home_data.py — the ONE recorder
  for the web performance series: percent clamping, rate gaps
  staying None (never invented zeros), aligned series for cpu /
  ram / network (up+down) / disk (read+write) / own-process, the
  explicit 600-sample cap (~15 min at the ~1.5 s push cadence) and
  the recorded span growing then capping at the cap.
* Window math: a 1/5/15-minute window is enabled only once that
  much history has actually been recorded, and window slicing
  returns the right tail (boundary sample kept).
* The payload's ``performance`` block (orvix/webview_home.py):
  series + ``historySpanSeconds`` + sample count + cap, windows
  enabling as fake-clock history accrues through repeated payload
  builds, the Phase 1 top-processes snapshot and own-process
  values referenced from the same stats reading, GPU/temperature
  explicit None, and the ephemeral one-sample block a caller
  without a persistent recorder gets.
* Static web checks for the Performance page: nav destination and
  view, the four charts, the window selector disabling honestly
  with a recording reason, the present-and-honest "N/A on Windows"
  temperature/GPU rows, the top-processes table, the app-usage
  line, the standalone not-connected state, and the home Live
  Performance strip regression (same ids, now fed from the same
  pushed performance series).
"""
import queue
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orvix.home_data import (
    MAX_PERFORMANCE_SAMPLES,
    PERFORMANCE_SAMPLE_INTERVAL_SECONDS,
    PERFORMANCE_SERIES_KEYS,
    PERFORMANCE_WINDOWS_SECONDS,
    PerformanceHistory,
    SystemStats,
    performance_enabled_windows,
)
from orvix.webview_home import WebViewHome, build_state_payload

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


def make_stats(cpu=10.0, ram=20.0, **overrides):
    values = dict(
        cpu_percent=cpu,
        ram_percent=ram,
        storage_percent=30.0,
        os_name="Windows 11",
        os_build="26100",
        machine="AMD64",
        uptime_seconds=3600.0,
        network_upload_bps=1000.0,
        network_download_bps=2000.0,
        disk_read_bps=3000.0,
        disk_write_bps=4000.0,
        drives=(),
        battery=None,
        top_processes=(
            {"name": "code.exe", "cpu_percent": 12.0,
             "memory_percent": 8.0},
            {"name": "chrome.exe", "cpu_percent": 5.0,
             "memory_percent": 15.0},
        ),
        own_cpu_percent=3.0,
        own_memory_percent=1.5,
    )
    values.update(overrides)
    return SystemStats(**values)


# ---------------------------------------------------------------------------
# PerformanceHistory: recording, normalisation, cap and span
# ---------------------------------------------------------------------------

class PerformanceHistoryTests(unittest.TestCase):
    def test_push_records_all_series_aligned(self):
        history = PerformanceHistory()
        history.push(cpu=10, ram=20, network_up=100, network_down=200,
                     disk_read=300, disk_write=400, own_cpu=1, own_memory=2,
                     timestamp=100.0)
        history.push(cpu=11, ram=21, network_up=101, network_down=201,
                     disk_read=301, disk_write=401, own_cpu=2, own_memory=3,
                     timestamp=101.5)
        series = history.series()
        self.assertEqual(series["cpu"], [10.0, 11.0])
        self.assertEqual(series["ram"], [20.0, 21.0])
        self.assertEqual(series["networkUp"], [100.0, 101.0])
        self.assertEqual(series["networkDown"], [200.0, 201.0])
        self.assertEqual(series["diskRead"], [300.0, 301.0])
        self.assertEqual(series["diskWrite"], [400.0, 401.0])
        self.assertEqual(series["ownCpu"], [1.0, 2.0])
        self.assertEqual(series["ownMemory"], [2.0, 3.0])
        self.assertEqual(series["elapsedSeconds"], [0.0, 1.5])
        for key in PERFORMANCE_SERIES_KEYS:
            self.assertEqual(len(series[key]), 2)
        self.assertTrue(history.has_data)
        self.assertEqual(len(history), 2)

    def test_unreadable_values_stay_none_never_zero(self):
        history = PerformanceHistory()
        history.push(cpu=None, ram="bad", network_up=-5,
                     network_down=None, disk_read=None, disk_write=None,
                     own_cpu=None, own_memory=None, timestamp=0.0)
        series = history.series()
        for key in PERFORMANCE_SERIES_KEYS:
            self.assertEqual(series[key], [None], key)
        self.assertFalse(history.has_data)

    def test_percents_are_clamped_rates_are_not(self):
        history = PerformanceHistory()
        history.push(cpu=250, ram=-3, network_up=10 ** 9,
                     timestamp=0.0)
        series = history.series()
        self.assertEqual(series["cpu"], [100.0])
        self.assertEqual(series["ram"], [0.0])
        self.assertEqual(series["networkUp"], [float(10 ** 9)])

    def test_push_stats_reads_phase1_fields(self):
        history = PerformanceHistory()
        history.push_stats(make_stats(), timestamp=5.0)
        series = history.series()
        self.assertEqual(series["cpu"], [10.0])
        self.assertEqual(series["networkDown"], [2000.0])
        self.assertEqual(series["diskWrite"], [4000.0])
        self.assertEqual(series["ownMemory"], [1.5])
        self.assertEqual(series["elapsedSeconds"], [0.0])

    def test_span_is_zero_until_two_samples(self):
        history = PerformanceHistory()
        self.assertEqual(history.span_seconds(), 0.0)
        history.push(cpu=1, timestamp=100.0)
        self.assertEqual(history.span_seconds(), 0.0)
        history.push(cpu=2, timestamp=160.0)
        self.assertEqual(history.span_seconds(), 60.0)

    def test_cap_is_explicit_and_span_caps_with_it(self):
        self.assertEqual(MAX_PERFORMANCE_SAMPLES, 600)
        # 600 samples at the nominal cadence cover the 15 minutes.
        self.assertGreaterEqual(
            MAX_PERFORMANCE_SAMPLES * PERFORMANCE_SAMPLE_INTERVAL_SECONDS,
            900)
        history = PerformanceHistory()
        for index in range(MAX_PERFORMANCE_SAMPLES + 50):
            history.push(cpu=index % 100,
                         timestamp=index * PERFORMANCE_SAMPLE_INTERVAL_SECONDS)
        self.assertEqual(len(history), MAX_PERFORMANCE_SAMPLES)
        # Oldest 50 samples dropped: span is (last - first retained).
        expected = ((MAX_PERFORMANCE_SAMPLES + 49 - 50)
                    * PERFORMANCE_SAMPLE_INTERVAL_SECONDS)
        self.assertEqual(history.span_seconds(), expected)
        series = history.series()
        self.assertEqual(len(series["cpu"]), MAX_PERFORMANCE_SAMPLES)


# ---------------------------------------------------------------------------
# Window math: enabling + slicing
# ---------------------------------------------------------------------------

class PerformanceWindowTests(unittest.TestCase):
    def test_windows_constant(self):
        self.assertEqual(PERFORMANCE_WINDOWS_SECONDS, (60, 300, 900))

    def test_enabled_only_when_span_covers_window(self):
        self.assertEqual(performance_enabled_windows(0),
                         {60: False, 300: False, 900: False})
        self.assertEqual(performance_enabled_windows(59.9),
                         {60: False, 300: False, 900: False})
        self.assertEqual(performance_enabled_windows(60),
                         {60: True, 300: False, 900: False})
        self.assertEqual(performance_enabled_windows(300),
                         {60: True, 300: True, 900: False})
        self.assertEqual(performance_enabled_windows(900),
                         {60: True, 300: True, 900: True})
        self.assertEqual(performance_enabled_windows(5000),
                         {60: True, 300: True, 900: True})

    def test_enabled_with_unreadable_span_enables_nothing(self):
        for bad in (None, "soon", float("nan")):
            self.assertEqual(performance_enabled_windows(bad),
                             {60: False, 300: False, 900: False})

    def test_history_enabled_windows_follow_its_span(self):
        history = PerformanceHistory()
        history.push(cpu=1, timestamp=0.0)
        self.assertEqual(history.enabled_windows(),
                         {60: False, 300: False, 900: False})
        history.push(cpu=2, timestamp=300.0)
        self.assertEqual(history.enabled_windows(),
                         {60: True, 300: True, 900: False})

    def _history_0_to_900(self):
        history = PerformanceHistory()
        for index in range(91):  # t = 0, 10, ..., 900
            history.push(cpu=float(index), ram=float(index),
                         timestamp=index * 10.0)
        return history

    def test_window_slice_returns_the_right_tail(self):
        history = self._history_0_to_900()
        sliced = history.window_series(60)
        # Samples at t >= 900 - 60 = 840: 7 samples, boundary kept.
        self.assertEqual(sliced["cpu"],
                         [84.0, 85.0, 86.0, 87.0, 88.0, 89.0, 90.0])
        # Elapsed stays relative to the recording's first sample.
        self.assertEqual(sliced["elapsedSeconds"][0], 840.0)
        self.assertEqual(sliced["elapsedSeconds"][-1], 900.0)

    def test_window_slice_300_and_full_series(self):
        history = self._history_0_to_900()
        sliced = history.window_series(300)
        self.assertEqual(len(sliced["cpu"]), 31)
        self.assertEqual(sliced["cpu"][0], 60.0)
        self.assertEqual(len(history.series()["cpu"]), 91)
        self.assertEqual(len(history.window_series(900)["cpu"]), 91)
        self.assertEqual(len(history.window_series(None)["cpu"]), 91)


# ---------------------------------------------------------------------------
# Payload performance block
# ---------------------------------------------------------------------------

class PerformancePayloadTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeWebApp()

    def test_ephemeral_payload_is_an_honest_one_sample_block(self):
        payload = build_state_payload(self.app, stats=make_stats())
        block = payload["performance"]
        self.assertEqual(block["sampleCount"], 1)
        self.assertEqual(block["historySpanSeconds"], 0.0)
        self.assertEqual(block["cap"], MAX_PERFORMANCE_SAMPLES)
        self.assertEqual(block["series"]["cpu"], [10.0])
        self.assertEqual(block["windows"], [
            {"seconds": 60, "enabled": False},
            {"seconds": 300, "enabled": False},
            {"seconds": 900, "enabled": False},
        ])

    def test_span_grows_and_windows_unlock_with_fake_clock(self):
        history = PerformanceHistory()
        first = build_state_payload(
            self.app, stats=make_stats(cpu=10.0),
            performance_history=history, performance_timestamp=1000.0)
        self.assertEqual(first["performance"]["historySpanSeconds"], 0.0)
        second = build_state_payload(
            self.app, stats=make_stats(cpu=20.0),
            performance_history=history, performance_timestamp=1030.0)
        self.assertEqual(second["performance"]["historySpanSeconds"], 30.0)
        self.assertEqual(second["performance"]["series"]["cpu"], [10.0, 20.0])
        self.assertFalse(second["performance"]["windows"][0]["enabled"])
        third = build_state_payload(
            self.app, stats=make_stats(cpu=30.0),
            performance_history=history, performance_timestamp=1060.0)
        block = third["performance"]
        self.assertEqual(block["historySpanSeconds"], 60.0)
        self.assertEqual(block["sampleCount"], 3)
        self.assertEqual(block["windows"], [
            {"seconds": 60, "enabled": True},
            {"seconds": 300, "enabled": False},
            {"seconds": 900, "enabled": False},
        ])
        self.assertEqual(block["series"]["elapsedSeconds"], [0.0, 30.0, 60.0])

    def test_payload_series_bounded_at_the_cap(self):
        history = PerformanceHistory()
        payload = None
        for index in range(MAX_PERFORMANCE_SAMPLES + 10):
            payload = build_state_payload(
                self.app, stats=make_stats(),
                performance_history=history,
                performance_timestamp=index * 1.5)
        block = payload["performance"]
        self.assertEqual(block["sampleCount"], MAX_PERFORMANCE_SAMPLES)
        for key in PERFORMANCE_SERIES_KEYS:
            self.assertEqual(len(block["series"][key]),
                             MAX_PERFORMANCE_SAMPLES, key)

    def test_block_references_phase1_snapshot_and_own_values(self):
        payload = build_state_payload(self.app, stats=make_stats())
        block = payload["performance"]
        # Same snapshot the stats block carries — referenced, not
        # re-collected.
        self.assertEqual(block["topProcesses"],
                         payload["stats"]["topProcesses"])
        self.assertEqual(block["topProcesses"][0]["name"], "code.exe")
        self.assertEqual(block["topProcesses"][0]["cpuPercent"], 12.0)
        self.assertEqual(block["ownCpuPercent"], 3.0)
        self.assertEqual(block["ownMemoryPercent"], 1.5)
        # GPU / temperature: explicit None on this stack.
        self.assertIsNone(block["gpuPercent"])
        self.assertIsNone(block["temperatureC"])

    def test_unreadable_stats_stay_none_in_series(self):
        stats = make_stats(cpu=None, ram=None, network_upload_bps=None,
                           network_download_bps=None, disk_read_bps=None,
                           disk_write_bps=None, own_cpu_percent=None,
                           own_memory_percent=None)
        payload = build_state_payload(self.app, stats=stats)
        series = payload["performance"]["series"]
        for key in PERFORMANCE_SERIES_KEYS:
            self.assertEqual(series[key], [None], key)

    def test_webview_home_owns_the_persistent_recorder(self):
        home = WebViewHome(self.app)
        self.assertIsInstance(home._performance_history, PerformanceHistory)
        self.assertEqual(len(home._performance_history), 0)


# ---------------------------------------------------------------------------
# Static web checks: Performance page
# ---------------------------------------------------------------------------

class StaticPhase7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_html = (WEB_HOME_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (WEB_HOME_DIR / "app.js").read_text(encoding="utf-8")
        cls.style_css = (WEB_HOME_DIR / "style.css").read_text(encoding="utf-8")

    def test_performance_nav_destination_and_view_exist(self):
        self.assertIn('data-tab="performance"', self.index_html)
        self.assertIn('id="performanceView"', self.index_html)
        self.assertIn("Live Performance", self.index_html)
        self.assertIn('id="performanceBackHome"', self.index_html)
        self.assertIn('tab === "performance"', self.app_js)
        self.assertIn("performanceView", self.app_js)
        self.assertIn("renderPerformance(lastState)", self.app_js)
        self.assertIn("renderPerformance(state)", self.app_js)

    def test_four_charts_render_from_the_payload_series(self):
        for element_id in ("perfCpuChart", "perfRamChart",
                           "perfNetworkChart", "perfDiskChart"):
            self.assertIn(f'id="{element_id}"', self.index_html)
        self.assertIn("state.performance", self.app_js)
        self.assertIn("block.series", self.app_js)
        for key in ("networkDown", "networkUp", "diskRead", "diskWrite"):
            self.assertIn(key, self.app_js)
        self.assertIn("renderPerfChart", self.app_js)
        self.assertIn(".perf-chart-card", self.style_css)
        self.assertIn(".perf-page-chart", self.style_css)

    def test_window_selector_disables_honestly(self):
        for seconds in (60, 300, 900):
            self.assertIn(f'data-window="{seconds}"', self.index_html)
            self.assertIn(f'id="perfWindow{seconds}"', self.index_html)
        self.assertIn('id="perfHistoryNote"', self.index_html)
        self.assertIn("perfWindowEnabled", self.app_js)
        self.assertIn("button.disabled = !enabled", self.app_js)
        self.assertIn("historySpanSeconds", self.app_js)
        self.assertIn("Recording", self.app_js)
        self.assertIn("perfWindowSlice", self.app_js)
        self.assertIn(".perf-window-button:disabled", self.style_css)

    def test_temperature_and_gpu_rows_are_present_and_honest(self):
        self.assertIn('id="perfTemperatureValue"', self.index_html)
        self.assertIn('id="perfGpuValue"', self.index_html)
        # Present in the static page itself, not only after a push.
        self.assertGreaterEqual(self.index_html.count("N/A on Windows"), 2)
        self.assertIn("N/A on Windows", self.app_js)

    def test_processes_table_and_own_usage_line(self):
        self.assertIn('id="perfProcessesTable"', self.index_html)
        self.assertIn('id="perfProcessesBody"', self.index_html)
        self.assertIn('id="perfProcessesEmpty"', self.index_html)
        self.assertIn("<th>CPU %</th>", self.index_html)
        self.assertIn("<th>Memory %</th>", self.index_html)
        self.assertIn("block.topProcesses", self.app_js)
        self.assertIn('id="perfOwnValue"', self.index_html)
        self.assertIn("ownCpuPercent", self.app_js)
        self.assertIn("ownMemoryPercent", self.app_js)
        self.assertIn(".perf-processes-table", self.style_css)

    def test_standalone_not_connected_state(self):
        self.assertIn("renderPerformance(null)", self.app_js)
        self.assertIn("Not connected — desktop bridge required.", self.app_js)

    def test_home_strip_regression(self):
        # The home Live Performance strip keeps its ids and renderer,
        # and now reads the same pushed performance series.
        self.assertIn('id="performanceChart"', self.index_html)
        self.assertIn('id="perfCaption"', self.index_html)
        self.assertIn("function renderPerf()", self.app_js)
        self.assertIn("PERF_BARS", self.app_js)
        self.assertIn("perfSeries", self.app_js)
        self.assertIn("state.performance.series", self.app_js)


class NoBannedWordPhase7Tests(unittest.TestCase):
    def test_phase7_files_have_no_banned_word(self):
        # Fragments are joined at runtime so this guard does not
        # itself contain the banned literals it searches for.
        plain = ["jar" + "vis", "jer" + "vis"]
        dotted = r"\.".join(["j", "a", "r", "v", "i", "s"])
        banned = re.compile("|".join(plain + [dotted]), re.I)
        for rel in ("orvix/home_data.py", "orvix/webview_home.py",
                    "web_home/index.html", "web_home/app.js",
                    "web_home/style.css",
                    "tests/test_orvix_command_center_phase7.py"):
            with self.subTest(file=rel):
                text = (APP_DIR / rel).read_text(encoding="utf-8")
                self.assertIsNone(banned.search(text))


if __name__ == "__main__":
    unittest.main()
