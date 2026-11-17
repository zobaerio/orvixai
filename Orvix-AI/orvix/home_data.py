"""Pure data helpers for the redesigned ORVIX home page.

Everything displayable on the home page traces to a real source: live
system counters (psutil / stdlib), the platform module, the app's own
settings and the core state machine. This module owns the formatting
and geometry for those values. It never invents a number: a stat that
cannot be read is ``None`` all the way to the widget, where it renders
as "N/A" / "—" / "Not checked".

No tkinter import — fully unit-testable headless.
"""
from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Sequence

DASH = "—"
NOT_AVAILABLE = "N/A"

# Gauge sweep: a 270-degree dial starting at 135 degrees (Tk Canvas arc
# convention: 0 degrees is at 3 o'clock, positive extent goes
# counter-clockwise, so a clockwise fill uses a negative extent).
GAUGE_START_DEG = 135.0
GAUGE_SWEEP_DEG = 270.0

GAUGE_CYAN = "#22d3ee"
GAUGE_GREEN = "#34d399"
GAUGE_BLUE = "#38bdf8"
GAUGE_ORANGE = "#fb923c"
GAUGE_RED = "#f87171"
GAUGE_TRACK = "#1c2d46"

# Caps for the Command Center Phase 1 metric expansion: the drive
# list and the top-process snapshot stay small enough to sample on
# the state-push cadence without turning the home page into a full
# task manager.
MAX_DRIVES = 10
MAX_TOP_PROCESSES = 5


def clamp_percent(value: object) -> Optional[float]:
    """Coerce a raw counter to a 0..100 float, or None when unreadable."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(100.0, number))


def _optional_float(value: object) -> Optional[float]:
    """Coerce a raw counter to a finite float, or None when unreadable.

    Unlike :func:`clamp_percent` this imposes no 0..100 range: process
    CPU and byte rates legitimately exceed 100."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def format_bytes_per_second(value: object) -> str:
    """'1.5 MB/s' for a real rate, 'N/A' when no rate was measured.

    Rates are bytes per second, scaled by 1024. A first sample (no
    previous counter to diff against) is None and renders 'N/A' —
    never a fabricated zero-rate reading presented as measured."""
    rate = _optional_float(value)
    if rate is None or rate < 0:
        return NOT_AVAILABLE
    if rate < 1024:
        return f"{rate:.0f} B/s"
    if rate < 1024 ** 2:
        return f"{rate / 1024:.1f} KB/s"
    if rate < 1024 ** 3:
        return f"{rate / 1024 ** 2:.1f} MB/s"
    return f"{rate / 1024 ** 3:.1f} GB/s"


def gauge_extent_deg(value: object) -> float:
    """Clockwise arc extent (negative degrees) for a gauge value.

    None / unreadable -> 0.0 (empty dial; the widget draws "N/A").
    """
    percent = clamp_percent(value)
    if percent is None:
        return 0.0
    return -GAUGE_SWEEP_DEG * (percent / 100.0)


def format_percent(value: object) -> str:
    """'42%' for a real value, 'N/A' when the stat was never read."""
    percent = clamp_percent(value)
    if percent is None:
        return NOT_AVAILABLE
    return f"{int(round(percent))}%"


def gauge_color(kind: str, value: object) -> str:
    """Dial colour for a stat: calm tones normally, warm when high.

    Unknown kinds and unreadable values fall back to the neutral track
    colour so an N/A gauge never looks alarmed or 'live'.
    """
    percent = clamp_percent(value)
    if percent is None:
        return GAUGE_TRACK
    if kind == "cpu":
        if percent >= 90:
            return GAUGE_RED
        if percent >= 75:
            return GAUGE_ORANGE
        return GAUGE_CYAN
    if kind == "ram":
        if percent >= 90:
            return GAUGE_RED
        if percent >= 80:
            return GAUGE_ORANGE
        return GAUGE_GREEN
    if kind == "storage":
        if percent >= 95:
            return GAUGE_RED
        if percent >= 85:
            return GAUGE_ORANGE
        return GAUGE_BLUE
    return GAUGE_CYAN


def format_uptime(seconds: object) -> str:
    """Format a duration in seconds as HH:MM:SS (hours may exceed 24).

    None / negative / unreadable -> an em dash: uptime is shown only
    when it was actually measured.
    """
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DASH
    if total < 0:
        return DASH
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_os_name(system: object, release: object, edition: object = None) -> str:
    """Human OS name from platform data, e.g. 'Windows 11 Pro'.

    Falls back gracefully: whatever parts are missing are simply left
    out; a completely unknown system renders as an em dash.
    """
    sys_name = " ".join(str(system or "").split())
    rel = " ".join(str(release or "").split())
    ed = " ".join(str(edition or "").split())
    if not sys_name:
        return DASH
    parts = [sys_name]
    if rel and rel not in sys_name:
        parts.append(rel)
    if ed and ed not in parts:
        parts.append(ed)
    return " ".join(parts)


@dataclass
class PerfHistory:
    """Rolling buffer of real gauge samples for the Live Performance
    strip. Samples are (cpu, ram) pairs; either side may be None when
    that counter could not be read at sample time."""

    maxlen: int = 48
    _samples: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._samples = deque(maxlen=max(1, int(self.maxlen)))

    def push(self, cpu: object, ram: object) -> None:
        self._samples.append((clamp_percent(cpu), clamp_percent(ram)))

    def samples(self) -> tuple[tuple[Optional[float], Optional[float]], ...]:
        return tuple(self._samples)

    def cpu_series(self) -> tuple[Optional[float], ...]:
        return tuple(cpu for cpu, _ram in self._samples)

    @property
    def has_data(self) -> bool:
        return any(cpu is not None or ram is not None for cpu, ram in self._samples)


@dataclass(frozen=True)
class SystemStats:
    """One real reading of the machine the app is running on.

    Every field is None when its source could not be read in the
    current environment (for example psutil missing, or uptime on a
    platform that does not expose a boot time).

    The Phase 1 fields are all optional with empty/None defaults so
    existing constructions with the original seven fields keep
    working unchanged:

    - ``network_upload_bps`` / ``network_download_bps`` and
      ``disk_read_bps`` / ``disk_write_bps`` are byte-per-second rates
      computed from the delta between successive
      :func:`collect_system_stats` calls; they are None on the first
      call (no previous sample exists yet).
    - ``drives`` is a tuple of ``{"mount", "percent"}`` dicts, one per
      readable mounted drive, capped at :data:`MAX_DRIVES`.
    - ``battery`` is ``{"percent", "plugged"}`` or None when the
      machine has no battery (desktops) — an honest N/A, never a
      fabricated full battery.
    - ``top_processes`` is a tuple of
      ``{"name", "cpu_percent", "memory_percent"}`` dicts for the
      heaviest processes, capped at :data:`MAX_TOP_PROCESSES`.
    - ``own_cpu_percent`` / ``own_memory_percent`` describe this app
      process itself.
    - ``gpu_percent`` / ``temperature_c`` are always None on this
      stack: psutil exposes no GPU counter and no temperature source
      on Windows, so the UI renders them as an explicit "N/A" instead
      of a vendor probe or an invented value.
    """

    cpu_percent: Optional[float]
    ram_percent: Optional[float]
    storage_percent: Optional[float]
    os_name: str
    os_build: str
    machine: str
    uptime_seconds: Optional[float]
    network_upload_bps: Optional[float] = None
    network_download_bps: Optional[float] = None
    disk_read_bps: Optional[float] = None
    disk_write_bps: Optional[float] = None
    drives: tuple = ()
    battery: Optional[dict] = None
    top_processes: tuple = ()
    own_cpu_percent: Optional[float] = None
    own_memory_percent: Optional[float] = None
    gpu_percent: Optional[float] = None
    temperature_c: Optional[float] = None

    @property
    def any_gauge(self) -> bool:
        return any(v is not None for v in
                   (self.cpu_percent, self.ram_percent, self.storage_percent))


# Previous IO counter sample for the two-sample rate computation.
# ``collect_system_stats`` is called repeatedly by the app (the Tk
# gauge refresh and the WebView state push), so the baseline lives in
# module state and each call rates against the previous call.
_prev_io_sample: Optional[dict] = None
_prev_io_lock = threading.Lock()


def _reset_io_baseline() -> None:
    """Drop the stored IO baseline (the next collect reports None
    rates again). Used by tests for isolation; the app never needs
    it because its collect calls run for the process lifetime."""
    global _prev_io_sample
    with _prev_io_lock:
        _prev_io_sample = None


def _update_io_rates(net_counters, disk_counters, now: float):
    """Rate the IO counters against the previous sample.

    Returns ``(upload_bps, download_bps, disk_read_bps,
    disk_write_bps)`` — each None when it cannot be rated (first
    sample, missing counter, non-positive elapsed time, or a counter
    that went backwards, which means it reset). The current counters
    become the new baseline either way, so a reset costs exactly one
    unreadable interval instead of a negative or absurd rate."""
    global _prev_io_sample
    current = {
        "t": _optional_float(now),
        "net_sent": _optional_float(
            getattr(net_counters, "bytes_sent", None)),
        "net_recv": _optional_float(
            getattr(net_counters, "bytes_recv", None)),
        "disk_read": _optional_float(
            getattr(disk_counters, "read_bytes", None)),
        "disk_write": _optional_float(
            getattr(disk_counters, "write_bytes", None)),
    }
    with _prev_io_lock:
        previous = _prev_io_sample
        _prev_io_sample = current
    if previous is None:
        return (None, None, None, None)
    elapsed = None
    if current["t"] is not None and previous.get("t") is not None:
        elapsed = current["t"] - previous["t"]
    if elapsed is None or elapsed <= 0:
        return (None, None, None, None)

    def _rate(key: str) -> Optional[float]:
        cur = current.get(key)
        prev = previous.get(key)
        if cur is None or prev is None or cur < prev:
            return None
        return (cur - prev) / elapsed

    return (_rate("net_sent"), _rate("net_recv"),
            _rate("disk_read"), _rate("disk_write"))


def _collect_drives(psutil_module) -> tuple:
    """Per-drive usage from ``disk_partitions`` + ``disk_usage``.

    A drive whose usage cannot be read is skipped (never reported
    with an invented percent); the list is capped at
    :data:`MAX_DRIVES`."""
    try:
        partitions = psutil_module.disk_partitions()
    except Exception:
        return ()
    drives = []
    for partition in partitions or []:
        if len(drives) >= MAX_DRIVES:
            break
        mount = (getattr(partition, "mountpoint", None)
                 or getattr(partition, "device", None))
        if not mount:
            continue
        try:
            usage = psutil_module.disk_usage(mount)
        except Exception:
            continue
        percent = clamp_percent(getattr(usage, "percent", None))
        if percent is None:
            total = _optional_float(getattr(usage, "total", None))
            used = _optional_float(getattr(usage, "used", None))
            if total and used is not None:
                percent = clamp_percent(used * 100.0 / total)
        if percent is None:
            continue
        drives.append({"mount": str(mount), "percent": percent})
    return tuple(drives)


def _collect_battery(psutil_module) -> Optional[dict]:
    """Battery reading as ``{"percent", "plugged"}``, or None.

    ``sensors_battery`` returns None on machines without a battery
    (desktops) and does not exist at all on some platforms — both are
    an honest None here, which the UI renders as "N/A"."""
    reader = getattr(psutil_module, "sensors_battery", None)
    if not callable(reader):
        return None
    try:
        reading = reader()
    except Exception:
        return None
    if reading is None:
        return None
    percent = clamp_percent(getattr(reading, "percent", None))
    if percent is None:
        return None
    plugged_raw = getattr(reading, "power_plugged", None)
    plugged = bool(plugged_raw) if plugged_raw is not None else None
    return {"percent": percent, "plugged": plugged}


def _process_field(proc, info, name: str):
    """One field of a process snapshot, from its ``info`` dict when
    ``process_iter`` supplied one, else from the process method."""
    if isinstance(info, dict) and name in info:
        return info[name]
    member = getattr(proc, name, None)
    return member() if callable(member) else member


def _collect_top_processes(psutil_module) -> tuple:
    """Top processes by memory: ``{"name", "cpu_percent",
    "memory_percent"}`` dicts, heaviest first, capped at
    :data:`MAX_TOP_PROCESSES`.

    Each process is read inside its own guard: a process that exits
    or denies access mid-scan (NoSuchProcess / AccessDenied /
    ZombieProcess — all Exception subclasses from psutil) is skipped,
    never fatal and never reported with invented numbers."""
    try:
        iterator = psutil_module.process_iter()
    except Exception:
        return ()
    entries = []
    for proc in iterator or []:
        try:
            info = getattr(proc, "info", None)
            name = _process_field(proc, info, "name")
            if not name:
                continue
            cpu = _optional_float(_process_field(proc, info, "cpu_percent"))
            if cpu is not None:
                cpu = max(0.0, cpu)
            memory = clamp_percent(
                _process_field(proc, info, "memory_percent"))
            entries.append({
                "name": str(name),
                "cpu_percent": cpu,
                "memory_percent": memory,
            })
        except Exception:
            continue
    entries.sort(
        key=lambda entry: (
            entry["memory_percent"] if entry["memory_percent"] is not None
            else -1.0,
            entry["cpu_percent"] if entry["cpu_percent"] is not None
            else -1.0),
        reverse=True)
    return tuple(entries[:MAX_TOP_PROCESSES])


def _collect_own_process(psutil_module):
    """This app process's own ``(cpu_percent, memory_percent)``.

    Each counter is guarded separately so one unreadable counter does
    not discard the other; both are None when the process handle
    itself cannot be obtained."""
    try:
        proc = psutil_module.Process()
    except Exception:
        return (None, None)
    cpu: Optional[float] = None
    memory: Optional[float] = None
    try:
        cpu = _optional_float(proc.cpu_percent())
        if cpu is not None:
            cpu = max(0.0, cpu)
    except Exception:
        cpu = None
    try:
        memory = clamp_percent(proc.memory_percent())
    except Exception:
        memory = None
    return (cpu, memory)


def collect_system_stats(clock=None) -> SystemStats:
    """Read the live counters once. Never raises: any source that
    fails simply leaves its field as None / dash.

    ``clock`` is the monotonic time source for the IO rate deltas
    (defaults to ``time.monotonic``); tests inject a fake clock so a
    two-sample rate can be asserted exactly."""
    cpu: Optional[float] = None
    ram: Optional[float] = None
    uptime: Optional[float] = None
    network_upload: Optional[float] = None
    network_download: Optional[float] = None
    disk_read: Optional[float] = None
    disk_write: Optional[float] = None
    drives: tuple = ()
    battery: Optional[dict] = None
    top_processes: tuple = ()
    own_cpu: Optional[float] = None
    own_memory: Optional[float] = None
    try:
        import psutil
        cpu = clamp_percent(psutil.cpu_percent(interval=None))
        ram = clamp_percent(psutil.virtual_memory().percent)
        try:
            uptime = max(0.0, time.time() - float(psutil.boot_time()))
        except Exception:
            uptime = None
        # IO rates: two-sample delta against the previous collect.
        try:
            net_counters = psutil.net_io_counters()
        except Exception:
            net_counters = None
        try:
            disk_counters = psutil.disk_io_counters()
        except Exception:
            disk_counters = None
        now = (clock() if callable(clock) else time.monotonic())
        (network_upload, network_download,
         disk_read, disk_write) = _update_io_rates(
             net_counters, disk_counters, now)
        drives = _collect_drives(psutil)
        battery = _collect_battery(psutil)
        top_processes = _collect_top_processes(psutil)
        own_cpu, own_memory = _collect_own_process(psutil)
    except Exception:
        cpu = ram = None

    storage: Optional[float] = None
    try:
        anchor = os.environ.get("SystemDrive") or Path_home_anchor()
        usage = shutil.disk_usage(anchor)
        if usage.total > 0:
            storage = clamp_percent(usage.used * 100.0 / usage.total)
    except Exception:
        storage = None

    edition = None
    try:
        win32_edition = getattr(platform, "win32_edition", None)
        if callable(win32_edition):
            edition = win32_edition()
    except Exception:
        edition = None

    return SystemStats(
        cpu_percent=cpu,
        ram_percent=ram,
        storage_percent=storage,
        os_name=format_os_name(platform.system(), platform.release(), edition),
        os_build=" ".join(str(platform.version() or "").split()) or DASH,
        machine=" ".join(str(platform.machine() or "").split()) or DASH,
        uptime_seconds=uptime,
        network_upload_bps=network_upload,
        network_download_bps=network_download,
        disk_read_bps=disk_read,
        disk_write_bps=disk_write,
        drives=drives,
        battery=battery,
        top_processes=top_processes,
        own_cpu_percent=own_cpu,
        own_memory_percent=own_memory,
        # No honest source on this stack (see SystemStats docstring):
        # explicit None so the UI renders "N/A".
        gpu_percent=None,
        temperature_c=None,
    )


def Path_home_anchor() -> str:
    """Filesystem anchor of the user's home (drive root on Windows)."""
    from pathlib import Path
    return str(Path.home().anchor or "/")


@dataclass(frozen=True)
class ModelStatusLine:
    label: str
    value: str
    state: Optional[bool]  # True ok, False problem, None unknown/neutral


def _tts_ready(module_available) -> bool:
    """TTS is usable when either the cloud (edge-tts) or the local
    (pyttsx3) engine module is installed — the same engines
    SpeechEngine.speak actually tries, in the same order."""
    try:
        return bool(module_available("edge_tts") or module_available("pyttsx3"))
    except Exception:
        return False


def model_status_lines(settings: dict, *, core_mode: str,
                       wake_enabled: bool, module_available=None) -> tuple[ModelStatusLine, ...]:
    """Status lines for the AI Model Status panel, from real state.

    - INTERNET: the home page never probes the network, so this is
      honestly 'Not checked' — the endpoint line below carries what is
      actually known (local vs remote configuration).
    - AI CORE: the live core mode string from the app's state bridge.
    - VOICE: the real wake-word flag.
    - TTS: module presence for the engines SpeechEngine really uses.
    """
    if module_available is None:
        import importlib.util

        def module_available(name: str) -> bool:  # type: ignore[no-redef]
            try:
                return importlib.util.find_spec(name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                return False

    endpoint = str((settings or {}).get("api_base_url", "") or "").strip()
    model = str((settings or {}).get("model", "") or "").strip()
    lowered = endpoint.lower()
    is_local = any(host in lowered for host in ("localhost", "127.0.0.1", "::1"))
    if endpoint and model:
        endpoint_value = "Local endpoint configured" if is_local else "Remote endpoint configured"
        endpoint_state: Optional[bool] = True
    elif endpoint or model:
        endpoint_value = "Partially configured"
        endpoint_state = False
    else:
        endpoint_value = "Not configured"
        endpoint_state = False

    mode = " ".join(str(core_mode or "").split()).upper() or "READY"
    tts_ok = _tts_ready(module_available)
    return (
        ModelStatusLine("ENDPOINT", endpoint_value, endpoint_state),
        ModelStatusLine("INTERNET", "Not checked", None),
        ModelStatusLine("AI CORE", mode, True),
        ModelStatusLine("VOICE", f"Wake word {'ON' if wake_enabled else 'OFF'}",
                        True if wake_enabled else None),
        ModelStatusLine("TTS", "Engine ready" if tts_ok else "Engine not installed",
                        True if tts_ok else False),
    )


@dataclass(frozen=True)
class QuickTile:
    key: str
    label: str
    command: str  # text routed through the app's normal _send_text pipeline
    # Tile category: "command" tiles send their command text through
    # the normal pipeline; the "power" tile is the power-options entry
    # point — its command is the deterministic power-options command,
    # handled locally (never free text for the AI model), and the
    # desktop app opens its power-options dialog for it.
    kind: str = "command"


def quick_action_tiles() -> tuple[QuickTile, ...]:
    """The ten Quick Actions tiles (the original six plus the four
    completions: Downloads, Windows Settings, Task Manager and System
    Information).

    Commands reuse the exact strings the existing quick-action buttons
    already send, so Chrome / File Explorer / Downloads / System
    Information resolve through the same deterministic paths as typed
    commands. Windows Settings resolves through the deterministic
    ms-settings: open and Task Manager through the taskmgr app alias.

    The power tile no longer routes free text to the AI model: its
    command is the deterministic "power options" command, which the
    automation layer handles locally (Lock runs immediately; Restart
    and Shut down require approval plus a cancellable countdown).
    """
    return (
        QuickTile("chrome", "Open Chrome", "Chrome kholo"),
        QuickTile("youtube", "YouTube", "YouTube kholo"),
        QuickTile("notepad", "Notepad", "Notepad kholo"),
        QuickTile("calculator", "Calculator", "Calculator kholo"),
        QuickTile("explorer", "File Explorer", "File Explorer kholo"),
        QuickTile("downloads", "Downloads", "Downloads kholo"),
        QuickTile("settings", "Windows Settings", "Windows Settings kholo"),
        QuickTile("task_manager", "Task Manager", "Task Manager kholo"),
        QuickTile("system_info", "System Information", "system information"),
        QuickTile("power", "Power Options", "power options", kind="power"),
    )


def performance_caption(history: PerfHistory) -> str:
    """One-line caption under the Live Performance bars: the latest
    real sample, or 'N/A' before any readable sample exists."""
    for cpu, ram in reversed(history.samples()):
        if cpu is not None or ram is not None:
            return f"CPU {format_percent(cpu)} · RAM {format_percent(ram)}"
    return NOT_AVAILABLE


__all__ = [
    "DASH", "NOT_AVAILABLE", "GAUGE_START_DEG", "GAUGE_SWEEP_DEG",
    "GAUGE_CYAN", "GAUGE_GREEN", "GAUGE_BLUE", "GAUGE_ORANGE", "GAUGE_RED",
    "GAUGE_TRACK", "MAX_DRIVES", "MAX_TOP_PROCESSES",
    "clamp_percent", "gauge_extent_deg", "format_percent",
    "format_bytes_per_second",
    "gauge_color", "format_uptime", "format_os_name", "PerfHistory",
    "SystemStats", "collect_system_stats", "ModelStatusLine",
    "model_status_lines", "QuickTile", "quick_action_tiles",
    "performance_caption",
]
