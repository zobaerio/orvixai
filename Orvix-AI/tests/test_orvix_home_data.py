"""Headless tests for the home-page pure data helpers (orvix/home_data).

The home page must never invent a number: these tests pin the N/A
behaviour for unreadable stats, the gauge geometry, the rolling
performance history and the model-status line assembly.
"""
import sys
import time
import types
import unittest
from unittest import mock

from orvix import home_data
from orvix.home_data import (
    GAUGE_SWEEP_DEG, GAUGE_TRACK, MAX_DRIVES, MAX_TOP_PROCESSES,
    PerfHistory, SystemStats, clamp_percent, collect_system_stats,
    format_bytes_per_second, format_os_name, format_percent,
    format_uptime, gauge_color, gauge_extent_deg, model_status_lines,
    performance_caption, quick_action_tiles,
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
    def test_ten_tiles_in_order(self):
        tiles = quick_action_tiles()
        self.assertEqual([t.key for t in tiles],
                         ["chrome", "youtube", "notepad", "calculator", "explorer",
                          "downloads", "settings", "task_manager", "system_info", "power"])
        for tile in tiles:
            self.assertTrue(tile.label)
            self.assertTrue(tile.command)

    def test_four_completion_tiles_use_existing_deterministic_commands(self):
        # Each new tile's command must be the exact string the existing
        # deterministic handlers already resolve (Downloads folder,
        # ms-settings: open, taskmgr alias, system information) — no
        # new command strings that would fall through to the AI model.
        by_key = {t.key: t for t in quick_action_tiles()}
        self.assertEqual(by_key["downloads"].command, "Downloads kholo")
        self.assertEqual(by_key["downloads"].label, "Downloads")
        self.assertEqual(by_key["settings"].command, "Windows Settings kholo")
        self.assertEqual(by_key["settings"].label, "Windows Settings")
        self.assertEqual(by_key["task_manager"].command, "Task Manager kholo")
        self.assertEqual(by_key["task_manager"].label, "Task Manager")
        self.assertEqual(by_key["system_info"].command, "system information")
        self.assertEqual(by_key["system_info"].label, "System Information")

    def test_power_tile_is_deterministic_power_options(self):
        # Bug #11: the power tile must not route free text to the AI
        # model. Its command is the deterministic power-options
        # command and its metadata marks it as the power tile; every
        # other tile stays a plain command tile.
        by_key = {t.key: t for t in quick_action_tiles()}
        power = by_key["power"]
        self.assertEqual(power.label, "Power Options")
        self.assertEqual(power.command, "power options")
        self.assertEqual(power.kind, "power")
        for key, tile in by_key.items():
            if key != "power":
                self.assertEqual(tile.kind, "command")


class FakeNoSuchProcess(Exception):
    pass


class FakeAccessDenied(Exception):
    pass


class FakeZombieProcess(Exception):
    pass


def _ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class FakeProc:
    """Stand-in for a psutil Process snapshot: each accessor returns
    its configured value, or raises it when it is an exception."""

    def __init__(self, name, cpu, memory):
        self._name = name
        self._cpu = cpu
        self._memory = memory

    @staticmethod
    def _resolve(value):
        if isinstance(value, Exception):
            raise value
        return value

    def name(self):
        return self._resolve(self._name)

    def cpu_percent(self):
        return self._resolve(self._cpu)

    def memory_percent(self):
        return self._resolve(self._memory)


class FakePsutil:
    """Fake psutil module with the exact surface home_data calls.

    Counters are mutable attributes so a test can advance them
    between two collect calls and assert the rated delta."""

    NoSuchProcess = FakeNoSuchProcess
    AccessDenied = FakeAccessDenied
    ZombieProcess = FakeZombieProcess

    def __init__(self):
        self.net = _ns(bytes_sent=1000, bytes_recv=5000)
        self.disk_io = _ns(read_bytes=2000, write_bytes=3000)
        self.partitions = [
            _ns(mountpoint="C:\\", device="C:\\"),
            _ns(mountpoint="D:\\", device="D:\\"),
        ]
        self.usage_by_mount = {
            "C:\\": _ns(total=200, used=122, percent=61.0),
            "D:\\": _ns(total=100, used=20, percent=20.0),
        }
        self.battery_reading = _ns(percent=82, power_plugged=True)
        self.processes = [
            FakeProc("alpha", 5.0, 10.0),
            FakeProc("beta", 50.0, 30.0),
        ]
        self.own_process = FakeProc("orvix", 3.5, 1.25)

    def cpu_percent(self, interval=None):
        return 12.0

    def virtual_memory(self):
        return _ns(percent=44.0)

    def boot_time(self):
        return time.time() - 3600

    def net_io_counters(self):
        return self.net

    def disk_io_counters(self):
        return self.disk_io

    def disk_partitions(self):
        return list(self.partitions)

    def disk_usage(self, mount):
        usage = self.usage_by_mount.get(mount)
        if usage is None:
            raise OSError("unreadable drive")
        return usage

    def sensors_battery(self):
        return self.battery_reading

    def process_iter(self):
        return iter(list(self.processes))

    def Process(self):
        return self.own_process


class FakeClock:
    def __init__(self, *times):
        self._times = iter(times)

    def __call__(self):
        return next(self._times)


class Phase1CollectTests(unittest.TestCase):
    """Command Center Phase 1: the expanded collector, driven by a
    fake psutil injected into sys.modules (collect imports psutil
    lazily, exactly as in production)."""

    def setUp(self):
        home_data._reset_io_baseline()
        self.fake = FakePsutil()

    def tearDown(self):
        home_data._reset_io_baseline()

    def _collect(self, clock):
        with mock.patch.dict(sys.modules, {"psutil": self.fake}):
            return collect_system_stats(clock=clock)

    def test_first_call_rates_are_none(self):
        stats = self._collect(FakeClock(100.0))
        # No previous sample exists yet: rates are honestly unknown,
        # never a fabricated zero or a since-boot average.
        self.assertIsNone(stats.network_upload_bps)
        self.assertIsNone(stats.network_download_bps)
        self.assertIsNone(stats.disk_read_bps)
        self.assertIsNone(stats.disk_write_bps)
        # The instantaneous fields are real on the same call.
        self.assertEqual(stats.cpu_percent, 12.0)
        self.assertEqual(stats.ram_percent, 44.0)

    def test_second_call_rates_are_exact_deltas(self):
        clock = FakeClock(100.0, 102.0)  # 2 seconds between samples
        self._collect(clock)
        self.fake.net = _ns(bytes_sent=1000 + 2048, bytes_recv=5000 + 4096)
        self.fake.disk_io = _ns(read_bytes=2000 + 1024,
                                write_bytes=3000 + 3072)
        stats = self._collect(clock)
        self.assertEqual(stats.network_upload_bps, 1024.0)
        self.assertEqual(stats.network_download_bps, 2048.0)
        self.assertEqual(stats.disk_read_bps, 512.0)
        self.assertEqual(stats.disk_write_bps, 1536.0)

    def test_counter_reset_yields_none_for_that_rate_only(self):
        clock = FakeClock(100.0, 101.0)
        self._collect(clock)
        # Sent counter went backwards (reset); recv kept counting.
        self.fake.net = _ns(bytes_sent=10, bytes_recv=5000 + 700)
        stats = self._collect(clock)
        self.assertIsNone(stats.network_upload_bps)
        self.assertEqual(stats.network_download_bps, 700.0)

    def test_missing_io_counters_leave_rates_none(self):
        def boom():
            raise RuntimeError("counters unavailable")
        self.fake.net_io_counters = boom
        self.fake.disk_io_counters = boom
        stats = self._collect(FakeClock(100.0))
        self.assertIsNone(stats.network_upload_bps)
        self.assertIsNone(stats.disk_read_bps)
        # Other metrics are unaffected by the IO failure.
        self.assertEqual(stats.battery, {"percent": 82.0, "plugged": True})

    def test_battery_present(self):
        stats = self._collect(FakeClock(100.0))
        self.assertEqual(stats.battery, {"percent": 82.0, "plugged": True})

    def test_battery_none_on_desktop(self):
        self.fake.battery_reading = None
        stats = self._collect(FakeClock(100.0))
        self.assertIsNone(stats.battery)

    def test_battery_reader_missing_is_none(self):
        self.assertIsNone(home_data._collect_battery(types.SimpleNamespace()))

    def test_drives_skip_unreadable(self):
        self.fake.partitions.append(_ns(mountpoint="E:\\", device="E:\\"))
        stats = self._collect(FakeClock(100.0))
        self.assertEqual(
            stats.drives,
            ({"mount": "C:\\", "percent": 61.0},
             {"mount": "D:\\", "percent": 20.0}))

    def test_drives_are_capped(self):
        fake = FakePsutil()
        fake.partitions = [
            _ns(mountpoint=f"/mnt/{i}", device=f"/mnt/{i}")
            for i in range(MAX_DRIVES + 4)]
        fake.usage_by_mount = {
            f"/mnt/{i}": _ns(total=100, used=10, percent=10.0)
            for i in range(MAX_DRIVES + 4)}
        drives = home_data._collect_drives(fake)
        self.assertEqual(len(drives), MAX_DRIVES)

    def test_drive_percent_computed_when_percent_field_missing(self):
        fake = FakePsutil()
        fake.partitions = [_ns(mountpoint="/data", device="/data")]
        fake.usage_by_mount = {
            "/data": _ns(total=200, used=50, percent=None)}
        self.assertEqual(home_data._collect_drives(fake),
                         ({"mount": "/data", "percent": 25.0},))

    def test_top_processes_sorted_by_memory_and_capped(self):
        self.fake.processes = [
            FakeProc(f"proc{i}", float(i), float(i * 10))
            for i in range(MAX_TOP_PROCESSES + 2)]
        stats = self._collect(FakeClock(100.0))
        self.assertEqual(len(stats.top_processes), MAX_TOP_PROCESSES)
        memories = [p["memory_percent"] for p in stats.top_processes]
        self.assertEqual(memories, sorted(memories, reverse=True))
        self.assertEqual(stats.top_processes[0]["name"],
                         f"proc{MAX_TOP_PROCESSES + 1}")

    def test_top_processes_skip_vanished_and_denied(self):
        self.fake.processes = [
            FakeProc(FakeNoSuchProcess("gone"), 1.0, 1.0),
            FakeProc("denied-cpu", FakeAccessDenied("no"), 99.0),
            FakeProc("denied-mem", 1.0, FakeZombieProcess("zombie")),
            FakeProc("survivor", 7.0, 42.0),
        ]
        stats = self._collect(FakeClock(100.0))
        self.assertEqual(
            stats.top_processes,
            ({"name": "survivor", "cpu_percent": 7.0,
              "memory_percent": 42.0},))

    def test_process_iter_failure_yields_empty(self):
        def boom():
            raise RuntimeError("cannot enumerate")
        self.fake.process_iter = boom
        stats = self._collect(FakeClock(100.0))
        self.assertEqual(stats.top_processes, ())

    def test_own_process_usage(self):
        stats = self._collect(FakeClock(100.0))
        self.assertEqual(stats.own_cpu_percent, 3.5)
        self.assertEqual(stats.own_memory_percent, 1.25)

    def test_own_process_failure_yields_none(self):
        def boom():
            raise RuntimeError("no handle")
        self.fake.Process = boom
        stats = self._collect(FakeClock(100.0))
        self.assertIsNone(stats.own_cpu_percent)
        self.assertIsNone(stats.own_memory_percent)

    def test_gpu_and_temperature_are_honest_none(self):
        stats = self._collect(FakeClock(100.0))
        self.assertIsNone(stats.gpu_percent)
        self.assertIsNone(stats.temperature_c)

    def test_collect_without_psutil_keeps_everything_none(self):
        with mock.patch.dict(sys.modules, {"psutil": None}):
            stats = collect_system_stats(clock=FakeClock(100.0))
        self.assertIsNone(stats.cpu_percent)
        self.assertIsNone(stats.network_upload_bps)
        self.assertIsNone(stats.battery)
        self.assertEqual(stats.drives, ())
        self.assertEqual(stats.top_processes, ())
        self.assertIsNone(stats.own_cpu_percent)
        self.assertIsNone(stats.gpu_percent)
        self.assertIsNone(stats.temperature_c)


class Phase1DefaultsAndFormatTests(unittest.TestCase):
    def test_legacy_positional_construction_defaults(self):
        # The original seven-field construction used across the app
        # and older tests must keep working, with every Phase 1 field
        # at its honest empty default.
        stats = SystemStats(None, None, 25.0, "—", "—", "x86_64", None)
        self.assertIsNone(stats.network_upload_bps)
        self.assertIsNone(stats.network_download_bps)
        self.assertIsNone(stats.disk_read_bps)
        self.assertIsNone(stats.disk_write_bps)
        self.assertEqual(stats.drives, ())
        self.assertIsNone(stats.battery)
        self.assertEqual(stats.top_processes, ())
        self.assertIsNone(stats.own_cpu_percent)
        self.assertIsNone(stats.own_memory_percent)
        self.assertIsNone(stats.gpu_percent)
        self.assertIsNone(stats.temperature_c)

    def test_format_bytes_per_second(self):
        self.assertEqual(format_bytes_per_second(None), "N/A")
        self.assertEqual(format_bytes_per_second("junk"), "N/A")
        self.assertEqual(format_bytes_per_second(-5), "N/A")
        self.assertEqual(format_bytes_per_second(512), "512 B/s")
        self.assertEqual(format_bytes_per_second(2048), "2.0 KB/s")
        self.assertEqual(format_bytes_per_second(3 * 1024 * 1024), "3.0 MB/s")
        self.assertEqual(format_bytes_per_second(2 * 1024 ** 3), "2.0 GB/s")


if __name__ == "__main__":
    unittest.main()
