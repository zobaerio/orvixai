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

import base64
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
    MAX_PERFORMANCE_SAMPLES,
    PERFORMANCE_SAMPLE_INTERVAL_SECONDS,
    PERFORMANCE_WINDOWS_SECONDS,
    PerformanceHistory,
    collect_system_stats,
    format_uptime,
    model_status_lines,
    quick_action_tiles,
)
from .settings import ENDPOINT_HEALTH_STATES
from .providers import PROVIDERS, configured_providers
from .hud_data import build_hud_panels, build_task_entries, task_current_step
from .memory import CATEGORIES as MEMORY_CATEGORIES
from .ui_formatters import format_message_time
from .version import APP_NAME, APP_VERSION

ENV_VAR = "ORVIX_WEBVIEW_HOME"

# Canonical characters, mirroring the desktop app's definitions. Used
# only when the app object does not expose its own ``characters`` dict
# (for example a minimal embedding), so the web page still lists the
# real three profiles — never an invented one.
_FALLBACK_CHARACTERS = {
    "luna": {"name": "LUNA", "role": "Calm strategy companion",
             "image": "luna.jpg"},
    "ruby": {"name": "RUBY", "role": "Confident action companion",
             "image": "ruby.jpg"},
    "maya": {"name": "MAYA", "role": "Warm creative companion",
             "image": "maya.jpg"},
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


def _portrait_roots(base_dir=None) -> list:
    """Directories that may hold the character reference portraits.

    Mirrors the desktop app's own discovery in
    ``OrvixAssistant._prepare_avatar_images``: the app folder's
    ``assets/characters/reference`` first, then any sibling app
    folder's — the reference JPGs may still live in a sibling folder
    whose move into this folder is a separate owner step, so the
    sibling is discovered by glob, never named or assumed.
    """
    if base_dir is None:
        base = Path(__file__).resolve().parent.parent
    else:
        base = Path(base_dir)
    roots = [base / "assets" / "characters" / "reference"]
    try:
        siblings = sorted(base.parent.glob("*/assets/characters/reference"))
    except Exception:
        siblings = []
    for sibling in siblings:
        if sibling not in roots:
            roots.append(sibling)
    return roots


def character_portrait_data_uris(characters=None, base_dir=None) -> dict:
    """``{key: "data:image/jpeg;base64,..."}`` for the real portraits.

    The web page cannot be relied on to reach the app's local asset
    files (the WebView may serve the page away from the asset tree),
    so the bridge injects the owner's own reference images as data
    URIs on request. A character whose file is missing — or cannot
    be read — is simply absent from the result: the page then keeps
    its styled initial-letter art for that card. Nothing is invented
    and no external URL is ever produced. Never raises.
    """
    if not isinstance(characters, dict) or not characters:
        characters = _FALLBACK_CHARACTERS
    roots = _portrait_roots(base_dir)
    result = {}
    for key, value in characters.items():
        filename = value.get("image") if isinstance(value, dict) else None
        if not filename:
            continue
        path = next(
            (root / filename for root in roots
             if (root / filename).is_file()),
            None)
        if path is None:
            continue
        try:
            data = path.read_bytes()
        except Exception:
            continue
        if not data:
            continue
        result[str(key)] = (
            "data:image/jpeg;base64,"
            + base64.b64encode(data).decode("ascii"))
    return result


# How many recent task records the payload carries. The web Tasks
# view is a recent-history glance, not the full Task History window
# (which stays the place for search/filter over all 100 records).
MAX_PAYLOAD_TASKS = 10

# How many recent activity entries the payload carries (Command
# Center Phase 2). The desktop feed itself is the source; the
# payload only bounds what one push sends to the page.
MAX_PAYLOAD_ACTIVITY = 10

# How many notification records the payload carries (Command Center
# Phase 3). The store itself keeps up to 100; one push sends only
# the latest 20 to the page, plus the store's TOTAL unread count
# (which may exceed the number of unread items in those 20).
MAX_PAYLOAD_NOTIFICATIONS = 20

# How many memory entries the payload carries (Command Center
# Phase 5). The store itself holds up to 500 entries; one push
# sends only the latest 100 (newest first, grouped by category),
# plus the TRUE per-category counts and total, which cover every
# stored entry — so the page can honestly say "latest 100 of N"
# instead of pretending the capped list is everything.
MAX_PAYLOAD_MEMORY_ENTRIES = 100

# Provider health states for the Model Management Center (Command
# Center Phase 6). A provider's health is "not_checked" until a
# real ProviderClient.health() check has actually run for it in
# this process — configuration alone never produces a verdict, and
# no state here ever claims a provider is "connected".
PROVIDER_HEALTH_NOT_CHECKED = "not_checked"
PROVIDER_HEALTH_CHECKING = "checking"
PROVIDER_HEALTH_REACHABLE = "reachable"
PROVIDER_HEALTH_UNREACHABLE = "unreachable"

PROVIDER_HEALTH_STATES: tuple = (
    PROVIDER_HEALTH_NOT_CHECKED,
    PROVIDER_HEALTH_CHECKING,
    PROVIDER_HEALTH_REACHABLE,
    PROVIDER_HEALTH_UNREACHABLE,
)

# The Tk activity panel's placeholder line before the first real
# command: furniture, not an activity event, so it never reaches
# the web activity panel (which shows its honest empty state
# instead).
_ACTIVITY_PLACEHOLDER = "Waiting for your first command"


def _control_center_payload(app, app_tasks):
    """Control Center / diagnostics block for the payload, or None.

    The data is the SAME data the Tk HUD home shows: a
    :func:`orvix.control_center.build_snapshot` snapshot turned into
    display lines by :func:`orvix.hud_data.build_hud_panels`. It is
    obtained from the app object defensively, in the same style the
    bridge already uses for optional app capabilities:

    1. an app-provided panels accessor (``_web_hud_panels`` — the
       real desktop app supplies one next to its Tk HUD refresh, so
       memory/lesson counts match the Tk lines exactly);
    2. an app-provided snapshot accessor
       (``_hud_control_center_snapshot``, the method the Tk HUD
       refresh itself calls), with the panels assembled here;
    3. otherwise the capability is missing and the block is None —
       the page then renders its honest empty state. A snapshot
       accessor that exists but returns None still counts as the
       capability being present: the panels are assembled with a
       None snapshot, exactly like the Tk HUD does, so the task
       counts from ``app_tasks`` stay real and the rest render as
       dashes. Nothing here ever fabricates a line, and no failure
       raises into the payload.
    """
    accessor = getattr(app, "_web_hud_panels", None)
    if callable(accessor):
        try:
            panels = accessor()
        except Exception:
            panels = None
        if panels is not None:
            return _hud_panels_payload(panels)
    snapshot_fn = getattr(app, "_hud_control_center_snapshot", None)
    if not callable(snapshot_fn):
        return None
    try:
        snapshot = snapshot_fn()
    except Exception:
        snapshot = None
    memory_count, lesson_count = _hud_memory_counts(app)
    try:
        panels = build_hud_panels(
            snapshot, app_tasks,
            memory_count=memory_count, lesson_count=lesson_count)
    except Exception:
        return None
    return _hud_panels_payload(panels)


def _hud_panels_payload(panels) -> dict:
    """JSON shape for a HudPanelData: the six Control Center lines
    plus the snapshot's recent-task and execution-log lines."""
    return {
        "agents": str(getattr(panels, "agents", "")),
        "models": str(getattr(panels, "models", "")),
        "health": str(getattr(panels, "health", "")),
        "queue": str(getattr(panels, "queue", "")),
        "budget": str(getattr(panels, "budget", "")),
        "memory": str(getattr(panels, "memory", "")),
        "recentTasks": [str(line) for line in
                        (getattr(panels, "recent_tasks", None) or ())],
        "logLines": [str(line) for line in
                     (getattr(panels, "log_lines", None) or ())],
    }


def _hud_memory_counts(app):
    """(memory_count, lesson_count) from the app's shared ORVIX
    state objects, mirroring the Tk HUD refresh: the objects tuple
    is (queue, budget, audit, memory, lessons). Either count stays
    None when it cannot be read — the memory line then shows a
    dash for that half instead of an invented number."""
    objects = getattr(app, "_hud_cc_objects", None)
    if objects is None:
        return (None, None)
    memory_count = lesson_count = None
    try:
        memory_count = len(objects[3].list())
    except Exception:
        pass
    try:
        lesson_count = len(objects[4].list())
    except Exception:
        pass
    return (memory_count, lesson_count)


def _activity_payload(app) -> list:
    """Recent-activity block: the desktop app's activity feed,
    newest first, bounded to ``MAX_PAYLOAD_ACTIVITY`` entries.

    Sources, in order, all read defensively: an app accessor
    (``_web_activity_entries`` — the real app supplies one over its
    timestamped feed), a timestamped feed attribute
    (``_activity_feed`` / ``activity_feed``), then the legacy
    ``recent_activity`` string list the Tk panel shows. Dict entries
    keep their recorded ``time`` (a string) — a time is shown only
    when one was actually recorded; plain-string entries carry
    ``time: None``. The Tk placeholder line is furniture, not an
    event, and is skipped. Missing feed → empty list (the page
    renders "No recent activity"); nothing is invented and no
    failure raises.
    """
    raw = None
    accessor = getattr(app, "_web_activity_entries", None)
    if callable(accessor):
        try:
            raw = accessor()
        except Exception:
            raw = None
    if not isinstance(raw, list):
        raw = getattr(app, "_activity_feed", None)
    if not isinstance(raw, list):
        raw = getattr(app, "activity_feed", None)
    if not isinstance(raw, list):
        raw = getattr(app, "recent_activity", None)
    if not isinstance(raw, list):
        return []
    entries: list[dict] = []
    for item in raw:
        if len(entries) >= MAX_PAYLOAD_ACTIVITY:
            break
        if isinstance(item, dict):
            text = " ".join(str(item.get("text") or "").split())
            stamp = item.get("time")
            stamp = stamp if isinstance(stamp, str) and stamp.strip() else None
        elif isinstance(item, str):
            text = " ".join(item.split())
            stamp = None
        else:
            continue
        if not text or text == _ACTIVITY_PLACEHOLDER:
            continue
        entries.append({"text": text, "time": stamp})
    return entries


def _current_operation_payload(records) -> dict | None:
    """The running task's current operation, or None when idle.

    The first (newest-first) record still running / cancelling is
    the current operation: its text is the record's current step
    (running plan-step title, else latest progress event) when one
    exists, else the task's own command text — the work actually in
    flight. No running record → None, and the page shows its honest
    idle label instead of inventing an operation.
    """
    if not records:
        return None
    for row in records:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in ("running", "cancelling"):
            continue
        step = task_current_step(row)
        command = " ".join(str(row.get("command", "")).split())
        text = step or command
        if not text:
            continue
        return {"text": text, "status": status, "command": command}
    return None


def _notifications_payload(app):
    """Notification Center block for the payload, or None.

    The source is the app's persistent NotificationStore
    (``app.notification_store``, Command Center Phase 3): the
    latest ``MAX_PAYLOAD_NOTIFICATIONS`` records, newest first,
    each with its category, title, text, epoch ``timestamp``, an
    HH:MM ``time`` formatted from that timestamp when it is a real
    number (None otherwise — no time is invented), and its read
    flag; plus ``unreadCount``, the store's total unread count.

    An app with no store yields None — the block is absent and the
    page renders its honest empty state. A store that exists but
    cannot be read also yields None rather than raising or
    fabricating items. Nothing here ever invents a notification.
    """
    store = getattr(app, "notification_store", None)
    if store is None:
        return None
    list_fn = getattr(store, "list", None)
    if not callable(list_fn):
        return None
    try:
        try:
            records = list_fn(limit=MAX_PAYLOAD_NOTIFICATIONS)
        except TypeError:
            records = list_fn()
    except Exception:
        return None
    if not isinstance(records, list):
        return None
    items: list[dict] = []
    for row in records[:MAX_PAYLOAD_NOTIFICATIONS]:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            timestamp = None
        time_text = None
        if timestamp is not None:
            try:
                time_text = format_message_time(
                    datetime.fromtimestamp(timestamp))
            except Exception:
                time_text = None
        items.append({
            "id": str(row.get("id") or ""),
            "category": str(row.get("category") or "system"),
            "title": str(row.get("title") or ""),
            "text": str(row.get("text") or ""),
            "timestamp": timestamp,
            "time": time_text,
            "read": bool(row.get("read", False)),
        })
    unread = None
    unread_fn = getattr(store, "unread_count", None)
    if callable(unread_fn):
        try:
            value = unread_fn()
        except Exception:
            value = None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            unread = value
    if unread is None:
        unread = sum(1 for item in items if not item["read"])
    return {"items": items, "unreadCount": unread}


def _resolve_memory_store(app):
    """The app's MemoryStore for the payload / bridge, or None.

    Sources, in order, all read defensively in the same style the
    bridge already uses for optional app capabilities:

    1. an app-provided accessor (``_web_memory_store`` — the real
       desktop app supplies one over its shared ORVIX state
       objects, so the store is the ONE instance the Brain/Chief
       path and the Tk HUD counts use, never a second instance
       pointed at the same file);
    2. a plain ``memory_store`` attribute (minimal embeddings);
    3. element 3 of the app's cached shared-state objects tuple
       (``_hud_cc_objects``: queue, budget, audit, memory,
       lessons — the same slot ``_hud_memory_counts`` reads).

    A missing or unreadable store yields None: the payload block
    is then null and the page renders its honest empty state.
    Nothing here ever fabricates an entry, and no failure raises.
    """
    accessor = getattr(app, "_web_memory_store", None)
    if callable(accessor):
        try:
            store = accessor()
        except Exception:
            store = None
        if store is not None:
            return store
    store = getattr(app, "memory_store", None)
    if store is not None:
        return store
    objects = getattr(app, "_hud_cc_objects", None)
    if objects is not None:
        try:
            store = objects[3]
        except Exception:
            store = None
        if store is not None:
            return store
    return None


def _memory_entry_payload(entry) -> dict:
    """JSON shape for one MemoryStore entry.

    The store records ``id``, ``key``, ``value``, ``category`` and
    ``updated_at`` (an epoch float — the store records no separate
    created timestamp, so the field is honestly named
    ``updatedAt``). ``text`` carries the entry's value: the text
    that was remembered. A missing/invalid timestamp stays None —
    no time is invented.
    """
    updated = entry.get("updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        updated = None
    return {
        "id": str(entry.get("id") or ""),
        "key": str(entry.get("key") or ""),
        "text": str(entry.get("value") or ""),
        "category": str(entry.get("category") or "note"),
        "updatedAt": updated,
    }


def _memory_payload(app, query=""):
    """Memory Center block for the payload, or None.

    Entries come from the app's MemoryStore (see
    :func:`_resolve_memory_store`), grouped by category under
    ``byCategory`` (the store's real categories — preference /
    fact / strategy / note — always present, in that order, plus
    any further category a stored entry actually carries), newest
    first within each category, capped at
    ``MAX_PAYLOAD_MEMORY_ENTRIES`` entries in total. ``counts``
    and ``total`` are computed over the FULL (possibly
    search-filtered) set before the cap, so they stay true even
    when the entry lists are truncated.

    When ``query`` is non-empty the block is the store's own
    ``search()`` result for it (matching key or value) and the
    query is echoed in the block, so the page renders exactly the
    filter the bridge applied. An app with no readable store
    yields None — never an exception, never an invented entry.
    """
    store = _resolve_memory_store(app)
    if store is None:
        return None
    query = str(query or "").strip()
    try:
        if query:
            raw = store.search(query)
        else:
            raw = store.list()
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    entries = [_memory_entry_payload(row) for row in raw
               if isinstance(row, dict)]
    counts: dict = {category: 0 for category in MEMORY_CATEGORIES}
    for entry in entries:
        counts[entry["category"]] = counts.get(entry["category"], 0) + 1
    total = len(entries)
    # Newest first, capped: the store lists oldest first, so the
    # latest entries are the tail of the full list.
    latest = list(reversed(entries))[:MAX_PAYLOAD_MEMORY_ENTRIES]
    by_category: dict = {category: [] for category in MEMORY_CATEGORIES}
    for entry in latest:
        by_category.setdefault(entry["category"], []).append(entry)
    return {
        "byCategory": by_category,
        "counts": counts,
        "total": total,
        "shown": len(latest),
        "truncated": total > len(latest),
        "cap": MAX_PAYLOAD_MEMORY_ENTRIES,
        "query": query,
    }


def _provider_health_map(app, explicit=None) -> dict:
    """Last-check provider health results as {provider_id: state}.

    Sources, in order, all read defensively in the same style the
    bridge already uses for optional app capabilities:

    1. an explicit mapping (tests, or a caller that already holds
       the results);
    2. an app-provided accessor (``_web_provider_health`` — the
       real desktop app supplies one over the results its
       ``_test_provider_from_web`` handler recorded when the
       bridge's ``testProvider`` method enqueued a real
       ``ProviderClient.health()`` check);
    3. a plain app attribute (``provider_health`` /
       ``_provider_health_results`` — minimal embeddings);
    4. the Control Center snapshot's per-model ``healthy`` flags
       (the snapshot / HealthMonitor pattern: True/False from a
       check that ran, None — absent here — when none has).

    Values may be state strings ("reachable" / "unreachable" /
    "checking") or booleans from a health check (True → reachable,
    False → unreachable). Anything else is ignored. An empty result
    means no check has run: every provider then reports
    "not_checked", never a guessed verdict. No failure raises.
    """
    merged: dict = {}

    def absorb(candidate):
        if not isinstance(candidate, dict):
            return
        for key, value in candidate.items():
            provider_id = str(key or "").strip().lower()
            if provider_id not in PROVIDERS:
                continue
            if value is True:
                merged[provider_id] = PROVIDER_HEALTH_REACHABLE
            elif value is False:
                merged[provider_id] = PROVIDER_HEALTH_UNREACHABLE
            elif isinstance(value, str) and value in PROVIDER_HEALTH_STATES:
                merged[provider_id] = value

    # Lowest precedence first: the snapshot's recorded flags, then
    # a plain attribute, then the app's last-check accessor, then
    # an explicit mapping — later (more specific) sources win.
    snapshot_fn = getattr(app, "_hud_control_center_snapshot", None)
    if callable(snapshot_fn):
        try:
            snapshot = snapshot_fn()
        except Exception:
            snapshot = None
        models = snapshot.get("models") if isinstance(snapshot, dict) else None
        if isinstance(models, list):
            absorb({str(row.get("provider") or ""): row.get("healthy")
                    for row in models
                    if isinstance(row, dict) and row.get("healthy") is not None})
    for attr in ("provider_health", "_provider_health_results"):
        absorb(getattr(app, attr, None))
    accessor = getattr(app, "_web_provider_health", None)
    if callable(accessor):
        try:
            absorb(accessor())
        except Exception:
            pass
    absorb(explicit)
    return merged


def _providers_payload(app, provider_env=None, provider_health=None) -> list:
    """Model Management Center block for the payload.

    One entry per Brain registry provider (orvix.providers
    PROVIDERS), in registry order: its id and registry name, a
    ``configured`` boolean (the provider's environment key is
    present — for the keyless local provider, configured means
    "no key needed"; the key itself NEVER appears, only the
    boolean), a ``keyless`` flag, its models with their real
    capability sets from the registry, and its health state —
    "not_checked" until a real health check has run (see
    :func:`_provider_health_map`). ``provider_env`` is the
    environment mapping the configured flags are read from
    (os.environ in production; a fake mapping in tests, exactly
    like configured_providers itself accepts).
    """
    try:
        configured = configured_providers(provider_env)
    except Exception:
        configured = {}
    health = _provider_health_map(app, explicit=provider_health)
    entries: list = []
    for provider_id, spec in PROVIDERS.items():
        models = []
        for info in spec.models:
            models.append({
                "name": str(info.model),
                "capabilities": sorted(str(cap) for cap in info.capabilities),
                "contextTokens": int(info.context_tokens),
            })
        entries.append({
            "id": str(provider_id),
            "name": str(spec.name),
            "configured": bool(configured.get(provider_id, False)),
            "keyless": spec.env_key is None,
            "health": health.get(provider_id, PROVIDER_HEALTH_NOT_CHECKED),
            "models": models,
        })
    return entries


def _recent_task_records(app) -> list:
    """Newest-first TaskStore records from the app, or [] when the app
    has no readable task store. Any failure yields an empty list: the
    page then shows its honest empty state instead of stale or
    invented tasks."""
    records = _panel_task_records(app)
    return records if records is not None else []


def _panel_task_records(app):
    """TaskStore records for the HUD panel assembly, or None when
    the app has no readable task store. The None case matters: it is
    what the Tk HUD passes to build_hud_panels when its own store
    read fails, so the queue line falls back to the snapshot's
    background-queue stats instead of reporting invented zeros for
    a store that was never read."""
    store = getattr(app, "task_store", None)
    if store is None:
        return None
    try:
        records = store.list_tasks(limit=MAX_PAYLOAD_TASKS)
    except Exception:
        return None
    return records if isinstance(records, list) else None


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


def _drives_payload(stats) -> list:
    """Per-drive usage for the payload: ``[{"mount", "percent"}]``.

    Read defensively with ``getattr`` so a stats object from before
    the Phase 1 fields existed (or a minimal stand-in) yields an
    empty list instead of raising; malformed entries are skipped."""
    drives = getattr(stats, "drives", None) or ()
    result = []
    for entry in drives:
        if not isinstance(entry, dict):
            continue
        mount = entry.get("mount")
        if not mount:
            continue
        result.append({"mount": str(mount), "percent": entry.get("percent")})
    return result


def _battery_payload(stats):
    """Battery for the payload: ``{"percent", "plugged"}`` or None.

    None is the honest desktop case (no battery) and passes through
    untouched so the page renders "N/A"."""
    battery = getattr(stats, "battery", None)
    if not isinstance(battery, dict):
        return None
    return {"percent": battery.get("percent"),
            "plugged": battery.get("plugged")}


def _top_processes_payload(stats) -> list:
    """Top-process snapshot for the payload, with camelCase keys:
    ``[{"name", "cpuPercent", "memoryPercent"}]``."""
    processes = getattr(stats, "top_processes", None) or ()
    result = []
    for entry in processes:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        result.append({
            "name": str(name),
            "cpuPercent": entry.get("cpu_percent"),
            "memoryPercent": entry.get("memory_percent"),
        })
    return result


def _performance_payload(stats, history=None, timestamp=None,
                           top_processes=None) -> dict:
    """Live Performance Center block for the payload (Phase 7).

    The series come from ONE recorder — a
    :class:`orvix.home_data.PerformanceHistory` owned by the
    WebView push loop in production — fed from the SAME stats
    reading this payload's stats block carries, at the same ~1.5 s
    push cadence: no second collector, no parallel polling loop.
    Each payload build records exactly one new sample. A caller
    without a persistent recorder gets an ephemeral one-sample
    block (span 0, no window enabled) rather than a fabricated
    history. ``historySpanSeconds`` is how much history actually
    exists; a window in ``windows`` is enabled only when the span
    covers it, so the page can disable 5-/15-minute views honestly
    until enough history has accrued. Series are bounded at
    :data:`MAX_PERFORMANCE_SAMPLES` (600 ≈ 15 min at the cadence).

    ``topProcesses`` / the own-process values / GPU / temperature
    are the Phase 1 stats values themselves, referenced here for
    the Performance page — never re-collected.
    """
    recorder = history
    if recorder is None or not (
            hasattr(recorder, "push_stats")
            and hasattr(recorder, "series")
            and hasattr(recorder, "span_seconds")):
        recorder = PerformanceHistory()
    recorder.push_stats(stats, timestamp=timestamp)
    span = float(recorder.span_seconds())
    enabled = {window: span >= window
               for window in PERFORMANCE_WINDOWS_SECONDS}
    return {
        "series": recorder.series(),
        "historySpanSeconds": span,
        "sampleCount": len(recorder),
        "cap": MAX_PERFORMANCE_SAMPLES,
        "sampleIntervalSeconds": PERFORMANCE_SAMPLE_INTERVAL_SECONDS,
        "windows": [
            {"seconds": window, "enabled": bool(enabled[window])}
            for window in PERFORMANCE_WINDOWS_SECONDS
        ],
        "topProcesses": (
            top_processes if top_processes is not None
            else _top_processes_payload(stats)),
        "ownCpuPercent": getattr(stats, "own_cpu_percent", None),
        "ownMemoryPercent": getattr(stats, "own_memory_percent", None),
        "gpuPercent": getattr(stats, "gpu_percent", None),
        "temperatureC": getattr(stats, "temperature_c", None),
    }


def build_state_payload(app, stats=None, memory_query=None,
                        provider_env=None, provider_health=None,
                        performance_history=None,
                        performance_timestamp=None) -> dict:
    """JSON-serializable snapshot of real app state for the web page.

    Attributes are read defensively (``getattr`` with defaults) so a
    minimal app-like object works. Stats come from
    :func:`orvix.home_data.collect_system_stats` unless a reading is
    supplied; unreadable stats stay ``None`` all the way to the page.

    ``memory_query`` (Command Center Phase 5) is the Memory Center
    search filter the page last requested through the bridge's
    ``memorySearch`` method: when it is a non-empty string the
    payload's memory block carries the store's search results for
    it instead of the full latest entries. It is a view filter
    only — it never changes what the store holds.

    ``provider_env`` / ``provider_health`` (Command Center Phase 6)
    are the environment mapping and last-check health results the
    providers block is built from; both default to the real
    sources (os.environ, and the app's recorded check results via
    :func:`_provider_health_map`).

    ``performance_history`` (Command Center Phase 7) is the push
    loop's persistent :class:`PerformanceHistory` recorder; each
    build records the current stats reading into it (at
    ``performance_timestamp`` when a caller supplies one — tests
    with fake clocks) and the payload's performance block carries
    its bounded series plus the recorded span. Without a recorder
    the block is an honest one-sample snapshot.
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
    # None (not []) when the app has no readable task store, so the
    # Control Center assembly below falls back to the snapshot's
    # queue stats exactly like the Tk HUD; the tasks entries and the
    # current operation treat None as an empty history.
    task_records = _panel_task_records(app)
    top_processes = _top_processes_payload(stats)
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
            task_records, limit=MAX_PAYLOAD_TASKS),
        # Command Center Phase 2 (additive — every key above is
        # unchanged): the Control Center diagnostics block (None
        # when the app exposes no snapshot capability), the recent
        # activity feed (empty when the app records none), and the
        # current operation (None when no task is running).
        "controlCenter": _control_center_payload(app, task_records),
        "activity": _activity_payload(app),
        "currentOperation": _current_operation_payload(task_records),
        # Command Center Phase 3 (additive): the Notification
        # Center block — latest records + total unread count from
        # the app's persistent store, or None when the app has no
        # readable store (the page then shows its honest empty
        # state instead of inventing notifications).
        "notifications": _notifications_payload(app),
        # Command Center Phase 5 (additive): the Memory Center
        # block — entries grouped by category (latest
        # MAX_PAYLOAD_MEMORY_ENTRIES, newest first) with true
        # per-category counts and total from the app's one
        # MemoryStore, filtered by the bridge's current search
        # query when one is set; None when the app has no readable
        # store (the page then shows its honest empty state).
        "memory": _memory_payload(app, query=memory_query or ""),
        # Command Center Phase 6 (additive): the Model Management
        # Center block — one entry per Brain registry provider
        # (id, name, configured boolean only — never any key
        # material — keyless flag, models with capability sets,
        # and health: "not_checked" until a real health() check
        # has run, then "reachable" / "unreachable").
        "providers": _providers_payload(
            app, provider_env=provider_env, provider_health=provider_health),
        # Command Center Phase 7 (additive): the Live Performance
        # Center block — bounded recent series for cpu / ram /
        # network (up+down) / disk (read+write) / own-process
        # (cpu/ram) recorded by the push loop's PerformanceHistory
        # from this same stats reading, the recorded span, and the
        # honestly-enabled 1/5/15-minute windows; plus the Phase 1
        # top-processes snapshot and own-process values referenced
        # from stats (see _performance_payload).
        "performance": _performance_payload(
            stats, history=performance_history,
            timestamp=performance_timestamp,
            top_processes=top_processes),
        "stats": {
            "cpu": stats.cpu_percent,
            "ram": stats.ram_percent,
            "storage": stats.storage_percent,
            "osName": stats.os_name,
            "osBuild": stats.os_build,
            "machine": stats.machine,
            "uptimeSeconds": stats.uptime_seconds,
            "uptime": format_uptime(stats.uptime_seconds),
            # Command Center Phase 1 (additive — every key above is
            # unchanged): IO rates are bytes/sec from the collector's
            # two-sample delta (None on the first sample), battery is
            # {"percent", "plugged"} or None on machines without one,
            # and gpu / temperature are explicit None on this stack
            # so the page renders an honest "N/A" for them.
            "networkUploadBps": getattr(stats, "network_upload_bps", None),
            "networkDownloadBps": getattr(stats, "network_download_bps", None),
            "diskReadBps": getattr(stats, "disk_read_bps", None),
            "diskWriteBps": getattr(stats, "disk_write_bps", None),
            "drives": _drives_payload(stats),
            "battery": _battery_payload(stats),
            "topProcesses": top_processes,
            "ownCpuPercent": getattr(stats, "own_cpu_percent", None),
            "ownMemoryPercent": getattr(stats, "own_memory_percent", None),
            "gpuPercent": getattr(stats, "gpu_percent", None),
            "temperatureC": getattr(stats, "temperature_c", None),
        },
        "core": {"mode": core_mode, "state": core_state_key(core_mode)},
        "character": str(selected) if selected is not None else "luna",
        "characters": _character_entries(app),
        "wakeEnabled": wake_enabled,
        # Command Center Phase 4 (additive): Take Control mode, read
        # from the persisted settings flag the app owns — the page
        # renders its toggle from this value on every push and never
        # keeps a local-only copy of the state. "running" mirrors
        # the busy flag below under the name the Emergency zone
        # cares about: whether anything is currently running.
        "takeControl": bool(settings.get("take_control", False)),
        "running": bool(getattr(app, "_busy", False)),
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
        # Memory Center search filter (Command Center Phase 5):
        # the query the page last asked for via memorySearch. It
        # lives on the API object — the same bridge-local state
        # pattern as the portrait cache in getCharacterPortraits —
        # and the push loop passes it to build_state_payload, so
        # the NEXT state payload carries the filtered memory block
        # and the page renders the search exactly like every other
        # piece of live state: from the payload, never from a
        # local-only copy.
        self._memory_query = ""

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

    def getCharacterPortraits(self):
        """One-shot portrait hand-off for the character cards.

        Returns ``{"ok": True, "portraits": {key: data URI}}`` built
        by :func:`character_portrait_data_uris` from the app's real
        character definitions. The page calls this once after the
        bridge connects and swaps each card's letter art for the
        owner's actual reference image; keys absent from the result
        keep the letter art. The lookup is cached on the API object —
        the files do not change while the app runs. This is a pure
        read of local files: nothing is enqueued and no Tk widget is
        touched.
        """
        try:
            portraits = getattr(self, "_portraits_cache", None)
            if portraits is None:
                characters = getattr(self.app, "characters", None)
                portraits = character_portrait_data_uris(characters)
                self._portraits_cache = portraits
            return {"ok": True, "portraits": portraits}
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

    def markNotificationsRead(self):
        try:
            # A write, so it follows the same enqueue discipline as
            # every other mutating bridge method: the store is only
            # touched on the app's Tk/event thread, never here. The
            # page's badge and unread styling refresh from the next
            # state payload, which reflects the store's real state.
            self._enqueue("_mark_notifications_read_from_web", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def clearNotifications(self):
        try:
            # The web page has already asked the user to confirm in
            # its own UI (the app's usual confirmation pattern), so
            # the enqueued app method clears without a second
            # desktop prompt. Only the notification history is
            # cleared — task history and settings are never touched.
            self._enqueue("_clear_notifications_from_web", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def stopAll(self):
        try:
            # Emergency Stop (Command Center Phase 4). The web page
            # has already run its in-page confirm step, so the
            # enqueued app method — OrvixDesktop._stop_all, the
            # composition of the existing cancel / stop-speaking /
            # queue-disarm / Brain-queue-cancel machinery — runs
            # without a second desktop prompt. A write, so it
            # follows the same enqueue discipline as every other
            # mutating bridge method: nothing is stopped from this
            # bridge thread.
            self._enqueue("_stop_all", ())
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def setTakeControl(self, enabled=False):
        try:
            # Take Control (Command Center Phase 4): a write, so it
            # follows the same enqueue discipline — the app method
            # persists the settings flag and writes the audit entry
            # on the Tk/event thread. The page's toggle renders from
            # the payload's takeControl value on the next state push,
            # never from a local-only copy of this call.
            self._enqueue("_set_take_control_from_web", (bool(enabled),))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def memorySearch(self, query=None):
        try:
            # Memory Center search (Command Center Phase 5). This
            # follows the codebase's established pattern for live
            # state: the search is a FILTER the page requests, and
            # the next state payload reflects it (the push loop
            # passes this query to build_state_payload, whose
            # memory block then carries the store's own search()
            # results). The one return-value read the bridge has —
            # getCharacterPortraits — is a one-shot hand-off of
            # static local files, not live state; memory IS live
            # state (the Brain path can add entries while the page
            # is open), so it stays on the payload path like the
            # notifications, tasks and Take Control state. Nothing
            # is enqueued and the store is never touched here: a
            # filter is a view concern, not an app write.
            self._memory_query = str(query or "").strip()
            return {"ok": True, "query": self._memory_query}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def memoryForget(self, entry_id=None):
        try:
            # Delete ONE memory entry (Command Center Phase 5): a
            # write, so it follows the same enqueue discipline as
            # every other mutating bridge method — the store is
            # only touched on the app's Tk/event thread by
            # _forget_memory_from_web, which resolves the entry id
            # the page sends to the key MemoryStore.forget() is
            # keyed by. The page's list refreshes from the next
            # state payload, which reflects the store's real state.
            ident = str(entry_id or "").strip()
            if not ident:
                return {"ok": False, "error": "missing entry id"}
            self._enqueue("_forget_memory_from_web", (ident,))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def testProvider(self, providerId=None):
        try:
            # Model Management Center (Command Center Phase 6): run
            # ONE provider's real ProviderClient.health() check.
            # The check itself is network work, so it follows the
            # same enqueue discipline as every other bridge action:
            # nothing is probed from this bridge thread — the
            # enqueued app method (_test_provider_from_web) builds
            # the registry client, runs health() (for ollama that
            # is the keyless local /models check), and records the
            # result the app's _web_provider_health accessor then
            # supplies to the next state payload. An unknown
            # provider id is rejected here and enqueues nothing.
            provider_id = str(providerId or "").strip().lower()
            if provider_id not in PROVIDERS:
                return {"ok": False, "error": "unknown provider"}
            self._enqueue("_test_provider_from_web", (provider_id,))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def memoryClear(self):
        try:
            # Clear ALL memory entries (Command Center Phase 5).
            # The web page has already asked the user to confirm in
            # its own UI (the in-page confirm step), so the
            # enqueued app method clears without a second desktop
            # prompt. Only the memory entries are cleared — task
            # history, lessons and settings are never touched. A
            # write, so it is enqueued like every other mutating
            # bridge method; nothing is cleared from this thread.
            self._enqueue("_clear_memory_from_web", ())
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
        self._stream_entry = None
        self._stream_counter = 0
        self._cleared_pending = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._push_thread = None
        self.start_error = None
        self._last_state_json = None
        self._push_counter = 0
        # Live Performance Center history (Phase 7): the ONE
        # recorder for the performance series, fed one sample per
        # state build in _push_state_once — the existing ~1.5 s
        # cadence, no extra polling. Bounded at
        # MAX_PERFORMANCE_SAMPLES (600 ≈ 15 minutes).
        self._performance_history = PerformanceHistory()

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

    def push_stream_message(self, speaker, text, kind, final=False):
        """Push one streaming update for the single in-flight message.

        The first call creates ONE buffer entry carrying a stable
        ``id`` and ``final: False``; later calls update that same
        entry's text in place — never one entry per delta — and the
        call with ``final=True`` completes it (same id, ``final:
        True``) and releases it, so the next stream starts a fresh
        entry. The entry is stamped once, at creation, exactly like
        ``push_message``. If the push loop has already drained the
        entry, an update re-queues that same single entry so the page
        receives the new text (and updates its DOM node in place by
        id) — the buffer still never holds more than one entry for
        the streamed message. Returns a copy of the entry.
        """
        with self._lock:
            entry = self._stream_entry
            if entry is None:
                self._stream_counter += 1
                entry = {"speaker": str(speaker), "text": str(text),
                         "kind": str(kind),
                         "time": format_message_time(datetime.now()),
                         "id": f"stream-{self._stream_counter}",
                         "final": False}
                self._stream_entry = entry
                self._messages.append(entry)
            else:
                entry["text"] = str(text)
                if not any(item is entry for item in self._messages):
                    self._messages.append(entry)
            if final:
                entry["final"] = True
                self._stream_entry = None
            return dict(entry)

    def clear_messages(self):
        """Empty the message buffer and flag the web chat as cleared.

        Only the buffer is touched — task history and settings live
        elsewhere and are never cleared here. The push loop forwards
        the cleared signal to the page on its next iteration.
        """
        with self._lock:
            self._messages.clear()
            self._stream_entry = None
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
            # The Memory Center search filter the page last set via
            # the bridge (Phase 5) rides along, so a search request
            # is reflected in the very next payload the page renders
            # from — and a forget/clear applied on the app thread is
            # reflected the same way, still filtered.
            payload = build_state_payload(
                self.app,
                memory_query=getattr(self.api, "_memory_query", ""),
                performance_history=self._performance_history)
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
    "MAX_PAYLOAD_ACTIVITY",
    "MAX_PAYLOAD_MEMORY_ENTRIES",
    "MAX_PAYLOAD_NOTIFICATIONS",
    "MAX_PAYLOAD_TASKS",
    "PROVIDER_HEALTH_STATES",
    "OrvixWebHomeApi",
    "WebViewHome",
    "build_state_payload",
    "character_portrait_data_uris",
    "core_state_key",
    "webview_available",
    "webview_home_enabled",
]
