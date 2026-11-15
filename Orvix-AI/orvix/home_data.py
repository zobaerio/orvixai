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


def clamp_percent(value: object) -> Optional[float]:
    """Coerce a raw counter to a 0..100 float, or None when unreadable."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(100.0, number))


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
    platform that does not expose a boot time)."""

    cpu_percent: Optional[float]
    ram_percent: Optional[float]
    storage_percent: Optional[float]
    os_name: str
    os_build: str
    machine: str
    uptime_seconds: Optional[float]

    @property
    def any_gauge(self) -> bool:
        return any(v is not None for v in
                   (self.cpu_percent, self.ram_percent, self.storage_percent))


def collect_system_stats() -> SystemStats:
    """Read the live counters once. Never raises: any source that
    fails simply leaves its field as None / dash."""
    cpu: Optional[float] = None
    ram: Optional[float] = None
    uptime: Optional[float] = None
    try:
        import psutil
        cpu = clamp_percent(psutil.cpu_percent(interval=None))
        ram = clamp_percent(psutil.virtual_memory().percent)
        try:
            uptime = max(0.0, time.time() - float(psutil.boot_time()))
        except Exception:
            uptime = None
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


def quick_action_tiles() -> tuple[QuickTile, ...]:
    """The six Quick Actions tiles (mockup grid, 2 x 3).

    Commands reuse the exact strings the existing quick-action buttons
    already send, so Chrome / File Explorer resolve through the same
    deterministic open-application path as typed commands. There is no
    deterministic 'power menu' handler in the app, so that tile routes
    its text through the normal command pipeline like a typed request.
    """
    return (
        QuickTile("chrome", "Open Chrome", "Chrome kholo"),
        QuickTile("youtube", "YouTube", "YouTube kholo"),
        QuickTile("notepad", "Notepad", "Notepad kholo"),
        QuickTile("calculator", "Calculator", "Calculator kholo"),
        QuickTile("explorer", "File Explorer", "File Explorer kholo"),
        QuickTile("power", "Power Menu", "Power menu"),
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
    "GAUGE_TRACK", "clamp_percent", "gauge_extent_deg", "format_percent",
    "gauge_color", "format_uptime", "format_os_name", "PerfHistory",
    "SystemStats", "collect_system_stats", "ModelStatusLine",
    "model_status_lines", "QuickTile", "quick_action_tiles",
    "performance_caption",
]
