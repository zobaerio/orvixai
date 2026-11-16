"""WebView (pywebview) home page bridge for ORVIX AI.

This module is the Python side of the HTML home page that lives in
``Orvix-AI/web_home/``. It owns three things:

- feature gating (``ORVIX_WEBVIEW_HOME`` env var / ``webview_home``
  setting) and availability reporting for the optional pywebview
  dependency;
- :func:`build_state_payload` — a JSON-serializable snapshot of real
  app state (system stats, model status, characters, core mode) built
  from :mod:`orvix.home_data`, so the web page never has to invent a
  number;
- :class:`OrvixWebHomeApi` — the object exposed to JavaScript. Every
  method enqueues work onto ``app.events`` (the queue the Tk thread
  drains every 100 ms) and never touches a Tk widget directly, because
  pywebview calls the API from its own threads.

No tkinter import — fully unit-testable headless. pywebview itself is
imported guarded: the desktop app must keep working (Tk console only)
on machines where pywebview is not installed.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import webview as _webview
except Exception:  # pragma: no cover - depends on optional install
    _webview = None

from .home_data import (
    collect_system_stats,
    format_uptime,
    model_status_lines,
    quick_action_tiles,
)
from .settings import ENDPOINT_HEALTH_STATES
from .hud_data import build_task_entries
from .ui_formatters import format_message_time
from .version import APP_NAME, APP_VERSION

ENV_VAR = "ORVIX_WEBVIEW_HOME"

# Canonical characters, mirroring the desktop app's definitions. Used
# only when the app object does not expose its own ``characters`` dict
# (for example a minimal embedding), so the web page still lists the
# real three profiles — never an invented one.
_FALLBACK_CHARACTERS = {
    "luna": {"name": "LUNA", "role": "Calm strategy companion"},
    "ruby": {"name": "RUBY", "role": "Confident action companion"},
    "maya": {"name": "MAYA", "role": "Warm creative companion"},
}


def webview_available() -> tuple[bool, str]:
    """(available, reason) for the optional pywebview dependency."""
    if _webview is None:
        return (False, "pywebview is not installed")
    return (True, "")


def webview_home_enabled(settings, environ=None) -> bool:
    """Whether the WebView home page should be used.

    The ``ORVIX_WEBVIEW_HOME`` environment variable wins over the
    setting: ``"1"`` forces it on, ``"0"`` forces it off. Any other
    env value falls through to ``settings["webview_home"]``, which
    defaults to True when absent.
    """
    if environ is None:
        environ = os.environ
    env_value = str(environ.get(ENV_VAR, "") or "").strip()
    if env_value == "0":
        return False
    if env_value == "1":
        return True
    return bool((settings or {}).get("webview_home", True))


def core_state_key(core_mode) -> str:
    """Map a free-form core mode string to a stable web state key.

    The desktop app reports modes such as ``"READY"``, ``"EXECUTING"``,
    ``"WAKE WORD ACTIVE"`` and ``"CANCELLATION REQUESTED"`` (sometimes
    still carrying decoration that other layers strip). Matching is by
    substring on the uppercased mode, checked in this order:

    listening (LISTEN) → thinking (THINK) → executing (EXECUT) →
    speaking (SPEAK) → error (ERROR/FAIL) → completed (COMPLET/DONE) →
    listening (WAKE — the wake-word listener is a listening state) →
    processing (CANCEL/PROCESS) → ready (READY/IDLE).

    ERROR is checked before COMPLETED so a failure mode can never be
    reported as a success. An empty or unrecognised mode is ``"idle"``.
    """
    mode = str(core_mode or "").upper()
    if not mode.strip():
        return "idle"
    if "LISTEN" in mode:
        return "listening"
    if "THINK" in mode:
        return "thinking"
    if "EXECUT" in mode:
        return "executing"
    if "SPEAK" in mode:
        return "speaking"
    if "ERROR" in mode or "FAIL" in mode:
        return "error"
    if "COMPLET" in mode or "DONE" in mode:
        return "completed"
    if "WAKE" in mode:
        return "listening"
    if "CANCEL" in mode or "PROCESS" in mode:
        return "processing"
    if "READY" in mode or "IDLE" in mode:
        return "ready"
    return "idle"


def _build_action_commands() -> dict:
    commands = {tile.key: tile.command for tile in quick_action_tiles()}
    explorer_command = commands.get("explorer")
    if explorer_command:
        # The web grid labels this tile "Files"; keep it an alias of
        # the exact File Explorer command, not a second string.
        commands["files"] = explorer_command
    return commands


ACTION_COMMANDS = _build_action_commands()


def _character_entries(app) -> list:
    characters = getattr(app, "characters", None)
    if not isinstance(characters, dict) or not characters:
        characters = _FALLBACK_CHARACTERS
    entries = []
    for key, value in characters.items():
        if isinstance(value, dict):
            name = value.get("name", str(key).upper())
            role = value.get("role", "")
        else:
            name = str(key).upper()
            role = ""
        entries.append({"key": str(key), "name": str(name), "role": str(role)})
    return entries


# How many recent task records the payload carries. The web Tasks
# view is a recent-history glance, not the full Task History window
# (which stays the place for search/filter over all 100 records).
MAX_PAYLOAD_TASKS = 10


def _recent_task_records(app) -> list:
    """Newest-first TaskStore records from the app, or [] when the app
    has no readable task store. Any failure yields an empty list: the
    page then shows its honest empty state instead of stale or
    invented tasks."""
    store = getattr(app, "task_store", None)
    if store is None:
        return []
    try:
        records = store.list_tasks(limit=MAX_PAYLOAD_TASKS)
    except Exception:
        return []
    return records if isinstance(records, list) else []


def _probe_health(app):
    """Probe-backed endpoint health for the payload, or None.

    Reads the desktop app's ``endpoint_health`` attribute, which its
    periodic probe maintains in the orvix.settings health states
    (Premium Phase 8a). Only a known state passes through; a minimal
    app with no probe — or an unrecognised value — yields None, and
    the page then keeps the configuration-derived ``endpointState``
    behaviour exactly as before. A probe result, when present, takes
    precedence over that configuration-derived state on the page.
    """
    value = getattr(app, "endpoint_health", None)
    if value in ENDPOINT_HEALTH_STATES:
        return value
    return None


def _endpoint_state(lines) -> str:
    """One-word endpoint state for the model pill, taken from the
    ENDPOINT status line the payload already carries: 'configured'
    when that line reports a configured endpoint, 'partial' when it
    reports a partial configuration, 'not-configured' otherwise, and
    'not-checked' when no endpoint line exists at all. This never
    claims the endpoint was reached — configuration is all the status
    lines actually know."""
    for line in lines:
        if line.label != "ENDPOINT":
            continue
        if line.state is True:
            return "configured"
        if line.state is False:
            return "partial" if "Partially" in line.value else "not-configured"
        return "not-checked"
    return "not-checked"


def build_state_payload(app, stats=None) -> dict:
    """JSON-serializable snapshot of real app state for the web page.

    Attributes are read defensively (``getattr`` with defaults) so a
    minimal app-like object works. Stats come from
    :func:`orvix.home_data.collect_system_stats` unless a reading is
    supplied; unreadable stats stay ``None`` all the way to the page.
    """
    if stats is None:
        stats = collect_system_stats()
    settings = getattr(app, "settings", None) or {}
    if not isinstance(settings, dict):
        settings = {}
    raw_mode = getattr(app, "core_mode", "READY")
    core_mode = str(raw_mode) if raw_mode is not None else ""
    wake_enabled = bool(getattr(app, "wake_enabled", False))
    selected = getattr(app, "selected_character", "luna")
    lines = model_status_lines(
        settings, core_mode=core_mode, wake_enabled=wake_enabled)
    return {
        "connected": True,
        "appName": APP_NAME,
        "version": APP_VERSION,
        "model": {
            "name": str(settings.get("model") or ""),
            "endpoint": str(settings.get("api_base_url") or ""),
            "endpointState": _endpoint_state(lines),
            # Probe-backed health (Phase 8a): one of the settings
            # health states, or None before the app's probe has a
            # state to report. The page prefers a probe verdict
            # (reachable / unreachable) over endpointState.
            "health": _probe_health(app),
            "statusLines": [
                {"label": line.label, "value": line.value, "state": line.state}
                for line in lines
            ],
        },
        "tasks": build_task_entries(
            _recent_task_records(app), limit=MAX_PAYLOAD_TASKS),
        "stats": {
            "cpu": stats.cpu_percent,
            "ram": stats.ram_percent,
            "storage": stats.storage_percent,
            "osName": stats.os_name,
            "osBuild": stats.os_build,
            "machine": stats.machine,
            "uptimeSeconds": stats.uptime_seconds,
            "uptime": format_uptime(stats.uptime_seconds),
        },
        "core": {"mode": core_mode, "state": core_state_key(core_mode)},
        "character": str(selected) if selected is not None else "luna",
        "characters": _character_entries(app),
        "wakeEnabled": wake_enabled,
        # The configured wake phrase, so the page's wake pill displays
        # the exact phrase the wake-word matcher listens for.
        "wakePhrase": str(settings.get("wake_phrase") or "hey orvix"),
        # Appearance (Settings > Appearance): whether the page's CSS
        # animations/transitions run. Defaults to True (the previous
        # always-animated behaviour) when the key is absent.
        "webHomeAnimations": bool(settings.get("web_home_animations", True)),
        "busy": bool(getattr(app, "_busy", False)),
    }


def _raise_root(app) -> None:
    """Bring the Tk console back to the front. Runs on the Tk thread
    (it is only ever enqueued onto ``app.events``). Each call is
    guarded: a half-torn-down root must not raise here."""
    root = getattr(app, "root", None)
    if root is None:
        return
    for method_name in ("deiconify", "lift", "focus_force"):
        try:
            method = getattr(root, method_name, None)
            if callable(method):
                method()
        except Exception:
            pass


class OrvixWebHomeApi:
    """Object exposed to JavaScript via pywebview's ``js_api``.

    Method names are the exact names the page calls, so they are
    camelCase by design. Every method returns ``{"ok": True}`` or
    ``{"ok": False, "error": str}`` and never raises: a JS call must
    not crash the bridge. Work is handed to the Tk thread through
    ``app.events``; callables are resolved at call time via
    ``getattr`` so tests (and the app) can substitute them freely.
    """

    def __init__(self, app):
        self.app = app

    def _enqueue(self, method_name, args=()):
        callback = getattr(self.app, method_name)
        self.app.events.put((callback, tuple(args)))

    def sendCommand(self, text=None):
        try:
            command = str(text or "").strip()
            if not command:
                return {"ok": False, "error": "empty command"}
            self._enqueue("_send_text", (command,))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def runAction(self, action=None):
        try:
            key = str(action or "").strip().lower()
            command = ACTION_COMMANDS.get(key)
            if command is None:
                return {"ok": False, "error": "unknown action"}
            self._enqueue("_send_text", (command,))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def selectCharacter(self, name=None):
        try:
            key = str(name or "").strip().lower()
            characters = getattr(self.app, "characters", None) or {}
            valid = set(characters.keys()) or {"luna", "ruby", "maya"}
            if key not in valid:
                return {"ok": False, "error": "unknown character"}
            self._enqueue("_select_character", (key,))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def startListening(self):
        try:
            self._enqueue("_listen", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def stopSpeaking(self):
        try:
            self._enqueue("_stop_speaking", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def setWakeWord(self, enabled=False):
        try:
            want = bool(enabled)
            if want == bool(getattr(self.app, "wake_enabled", False)):
                return {"ok": True}
            self._enqueue("_toggle_wake_word", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def openConsole(self):
        try:
            self.app.events.put((_raise_root, (self.app,)))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def clearConversation(self):
        try:
            # The web page has already asked the user to confirm in
            # its own UI, so the enqueued app method clears without a
            # second desktop prompt. It clears ONLY the conversation
            # views and this bridge's message buffer — task history
            # (TaskStore) and settings are never touched.
            self._enqueue("_clear_conversation_from_web", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}


# Upper bound on buffered chat messages awaiting push to the web
# page. The web chat DOM trims to the same bound (see app.js), so a
# long session cannot grow either side without limit.
MAX_MESSAGE_BUFFER = 200


class WebViewHome:
    """Owns the pywebview window and the state/message push loops."""

    def __init__(self, app):
        self.app = app
        self.api = OrvixWebHomeApi(app)
        self.window = None
        self._messages = collections.deque(maxlen=MAX_MESSAGE_BUFFER)
        self._cleared_pending = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._push_thread = None
        self.start_error = None
        self._last_state_json = None
        self._push_counter = 0

    @staticmethod
    def index_path() -> Path:
        return Path(__file__).resolve().parent.parent / "web_home" / "index.html"

    # -- message buffer -------------------------------------------------

    def push_message(self, speaker, text, kind):
        # Every message is stamped once, here at creation, with the
        # shared HH:MM formatter so the web page and the Tk console
        # show the same time for the same message.
        with self._lock:
            self._messages.append(
                {"speaker": str(speaker), "text": str(text),
                 "kind": str(kind),
                 "time": format_message_time(datetime.now())})

    def clear_messages(self):
        """Empty the message buffer and flag the web chat as cleared.

        Only the buffer is touched — task history and settings live
        elsewhere and are never cleared here. The push loop forwards
        the cleared signal to the page on its next iteration.
        """
        with self._lock:
            self._messages.clear()
            self._cleared_pending = True

    def drain_messages(self) -> list:
        with self._lock:
            items = list(self._messages)
            self._messages.clear()
            return items

    def _requeue_front(self, messages):
        with self._lock:
            self._messages.extendleft(reversed(messages))

    # -- lifecycle ------------------------------------------------------

    def start(self) -> bool:
        """Create the window and run pywebview on a daemon thread.

        Returns True when the GUI loop started without an error. Any
        failure (pywebview missing, page missing, GUI error) is stored
        on ``start_error`` and reported as False so the caller can fall
        back to the Tk console.
        """
        if _webview is None:
            self.start_error = "pywebview is not installed"
            return False
        index = self.index_path()
        if not index.is_file():
            self.start_error = f"web home page not found: {index}"
            return False
        self.start_error = None
        self._stop.clear()
        try:
            self.window = _webview.create_window(
                "ORVIX", url=str(index), js_api=self.api,
                width=1500, height=920)
        except Exception as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            return False
        try:
            self.window.events.closed += self._on_closed
        except Exception:
            pass
        shown = threading.Event()
        shown_registered = False
        try:
            self.window.events.shown += shown.set
            shown_registered = True
        except Exception:
            shown_registered = False

        self._push_thread = threading.Thread(
            target=self._push_loop, name="orvix-webhome-push", daemon=True)
        self._push_thread.start()

        def _run_gui():
            try:
                _webview.start()
            except Exception as exc:
                self.start_error = f"{type(exc).__name__}: {exc}"

        self._thread = threading.Thread(
            target=_run_gui, name="orvix-webhome-gui", daemon=True)
        self._thread.start()

        if shown_registered:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if self.start_error is not None or shown.is_set():
                    break
                shown.wait(0.05)
        else:
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                if self.start_error is not None:
                    break
                time.sleep(0.05)
        return self.start_error is None

    def stop(self):
        self._stop.set()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def _on_closed(self):
        self._stop.set()
        try:
            self.app.events.put((_raise_root, (self.app,)))
        except Exception:
            pass

    # -- push loop --------------------------------------------------------

    def _push_loop(self):
        while not self._stop.is_set():
            # A pending cleared signal goes out BEFORE any messages
            # drained below: those messages arrived after the clear
            # (clearing empties the buffer), so the page must wipe
            # first and only then append them.
            with self._lock:
                cleared = self._cleared_pending
                self._cleared_pending = False
            if cleared and self.window is not None:
                try:
                    self.window.evaluate_js(
                        "window.orvixChatCleared && window.orvixChatCleared()")
                except Exception:
                    pass
            messages = self.drain_messages()
            if messages and self.window is not None:
                try:
                    self.window.evaluate_js(
                        "window.orvixPushMessages && window.orvixPushMessages("
                        + json.dumps(messages) + ")")
                except Exception:
                    if self._stop.is_set():
                        return
                    self._requeue_front(messages)
            self._push_counter += 1
            # State is rebuilt every 3rd loop (~1.5 s) so the gauges do
            # not hammer psutil, and pushed only when it changed.
            if (self._push_counter - 1) % 3 == 0:
                self._push_state_once()
            self._stop.wait(0.5)

    def _push_state_once(self):
        if self.window is None:
            return
        try:
            payload = build_state_payload(self.app)
            payload_json = json.dumps(payload)
        except Exception:
            return
        if payload_json == self._last_state_json:
            return
        try:
            self.window.evaluate_js(
                "window.orvixPushState && window.orvixPushState("
                + payload_json + ")")
            self._last_state_json = payload_json
        except Exception:
            # Window already closed etc.: the loop head re-checks
            # _stop and exits on the next iteration.
            pass


__all__ = [
    "ENV_VAR",
    "ACTION_COMMANDS",
    "MAX_MESSAGE_BUFFER",
    "OrvixWebHomeApi",
    "WebViewHome",
    "build_state_payload",
    "core_state_key",
    "webview_available",
    "webview_home_enabled",
]
