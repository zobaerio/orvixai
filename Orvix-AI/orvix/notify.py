"""Best-effort native notifications for ORVIX (Premium Phase 6).

A small, guarded layer over the system-tray icon's ``notify``
capability (pystray ``Icon.notify(message, title)``). Notifications
are decoration on top of the existing in-app messages: the SYSTEM
lines in the conversation remain the record, and a notification is
only ever an extra signal — never a replacement that could lose
information.

Rules enforced here:

- A notification is attempted only when the per-event setting toggle
  is enabled AND a tray icon with a callable ``notify`` exists.
- Every failure — no tray icon, a non-callable ``notify``, an
  exception raised by the OS/tray layer, an unknown event kind —
  is swallowed. This module never raises, never blocks, and its
  return value never claims a delivery that did not happen: it
  returns True only when the tray call completed without error.

The settings-gating decision (:func:`notification_enabled`) and the
title/message mapping (:func:`notification_content`) are pure and
separately testable from the OS call (:func:`notify_event`).

There is no polling or scheduling in this module. The
``endpoint_health`` kind is ready for real health-result points
(the Settings connection-test result is wired in app.py); periodic
endpoint polling is a later phase and is not invented here.

Command Center Phase 3 adds a persistent Notification Center store
(:class:`NotificationStore`) alongside the toaster. The store
records ONLY the same real events this module already signals —
the four event kinds above, mapped onto the centre's categories —
in a bounded JSON file (``MAX_NOTIFICATIONS`` records) next to the
other app data. It invents no event sources: the ``system``,
``ai`` and ``security`` categories exist in the category set for
completeness but no code path creates records in them today,
because no real event source in the app maps onto them yet.
Recording follows the same per-kind Settings toggles as the
toasts (see :func:`record_event`): a kind the user switched off is
neither toasted nor recorded. Unlike a toast, a record does not
depend on a tray icon existing — the centre is the durable record
the toasts never were.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .settings import APP_DIR

# Event kinds.
TASK_COMPLETED = "task_completed"
TASK_FAILED = "task_failed"
ENDPOINT_HEALTH = "endpoint_health"
WAKE_ACTIVATION = "wake_activation"

EVENT_KINDS: tuple[str, ...] = (
    TASK_COMPLETED,
    TASK_FAILED,
    ENDPOINT_HEALTH,
    WAKE_ACTIVATION,
)

#: Event kind -> settings key that gates it. Every key defaults to
#: True in ``orvix.settings.DEFAULTS``: native notifications are a
#: new Phase 6 feature, on by default and individually switchable.
EVENT_SETTING_KEYS: dict[str, str] = {
    TASK_COMPLETED: "notify_task_completed",
    TASK_FAILED: "notify_task_failed",
    ENDPOINT_HEALTH: "notify_endpoint_health",
    WAKE_ACTIVATION: "notify_wake_activation",
}

#: Event kind -> (title, default message). The detail supplied by
#: the caller, when present, replaces the default message.
_EVENT_CONTENT: dict[str, tuple[str, str]] = {
    TASK_COMPLETED: ("ORVIX — Task completed", "A task finished successfully."),
    TASK_FAILED: (
        "ORVIX — Task failed",
        "A task finished with an error. Open ORVIX for details.",
    ),
    ENDPOINT_HEALTH: (
        "ORVIX — Endpoint health",
        "An endpoint health check finished. Open ORVIX for details.",
    ),
    WAKE_ACTIVATION: ("ORVIX — Listening", "Wake phrase detected. ORVIX is listening."),
}

#: Toast text stays short: a long detail is cut rather than dumped.
_MAX_DETAIL_CHARS = 200


def notification_enabled(settings: Any, kind: str) -> bool:
    """Pure gating decision: is ``kind`` enabled in ``settings``?

    Unknown kinds are never enabled. A missing key falls back to
    True, matching the DEFAULTS value for every notification key,
    so a settings dict that predates Phase 6 behaves as documented.
    A non-dict ``settings`` disables everything (fail closed on the
    decision, while :func:`notify_event` still never raises).
    """
    key = EVENT_SETTING_KEYS.get(kind)
    if key is None:
        return False
    if not isinstance(settings, dict):
        return False
    return bool(settings.get(key, True))


def notification_content(kind: str, detail: str = "") -> tuple[str, str] | None:
    """Pure mapping: (title, message) for ``kind``, or None if unknown.

    A non-empty ``detail`` becomes the message: its first line only,
    whitespace-trimmed and length-capped, so callers may pass a task
    command or a full result string without flooding the toast.
    """
    content = _EVENT_CONTENT.get(kind)
    if content is None:
        return None
    title, default_message = content
    text = str(detail or "").strip()
    if text:
        first_line = text.splitlines()[0].strip()
        if first_line:
            if len(first_line) > _MAX_DETAIL_CHARS:
                first_line = first_line[: _MAX_DETAIL_CHARS - 1].rstrip() + "…"
            return title, first_line
    return title, default_message


def notify_event(
    settings: Any,
    tray_icon: Any,
    kind: str,
    *,
    detail: str = "",
) -> bool:
    """Attempt one native notification; return True iff it was sent.

    "Sent" means only that the tray icon's ``notify`` callable was
    invoked with (message, title) and returned without raising —
    the OS gives no delivery receipt, and none is claimed. Any
    gating failure, missing/non-callable tray notify, or exception
    from the tray layer returns False. This function never raises.
    """
    try:
        if not notification_enabled(settings, kind):
            return False
        if tray_icon is None:
            return False
        notify = getattr(tray_icon, "notify", None)
        if not callable(notify):
            return False
        content = notification_content(kind, detail)
        if content is None:
            return False
        title, message = content
        notify(message, title)
        return True
    except Exception:
        return False


class Notifier:
    """Convenience holder binding settings + tray icon for callers.

    Thin wrapper over the module functions; the functions remain the
    canonical, separately testable API. ``settings`` is read live on
    each call, so a Settings save that replaces the dict should hand
    the app a fresh Notifier (or call the functions directly, as
    app.py does).
    """

    def __init__(self, settings: Any, tray_icon: Any = None) -> None:
        self._settings = settings
        self._tray_icon = tray_icon

    def enabled(self, kind: str) -> bool:
        return notification_enabled(self._settings, kind)

    def notify(self, kind: str, *, detail: str = "") -> bool:
        return notify_event(self._settings, self._tray_icon, kind, detail=detail)

    # Named helpers for the four event kinds.
    def task_completed(self, detail: str = "") -> bool:
        return self.notify(TASK_COMPLETED, detail=detail)

    def task_failed(self, detail: str = "") -> bool:
        return self.notify(TASK_FAILED, detail=detail)

    def endpoint_health(self, detail: str = "") -> bool:
        return self.notify(ENDPOINT_HEALTH, detail=detail)

    def wake_activation(self, detail: str = "") -> bool:
        return self.notify(WAKE_ACTIVATION, detail=detail)


# ---------------------------------------------------------------------------
# Notification Center store (Command Center Phase 3)
# ---------------------------------------------------------------------------

#: The centre's category set. Only ``task``, ``connection`` and
#: ``voice`` have real event sources today (see KIND_CATEGORIES);
#: ``system`` / ``ai`` / ``security`` are valid categories a record
#: may carry, but nothing creates them yet — no source is invented.
CATEGORIES: tuple[str, ...] = (
    "task",
    "connection",
    "voice",
    "system",
    "ai",
    "security",
)

#: Event kind -> Notification Center category. The two task kinds
#: share the ``task`` category (the record's title still says
#: completed vs failed); endpoint health is the connection signal;
#: a wake activation is a voice event.
KIND_CATEGORIES: dict[str, str] = {
    TASK_COMPLETED: "task",
    TASK_FAILED: "task",
    ENDPOINT_HEALTH: "connection",
    WAKE_ACTIVATION: "voice",
}

#: Hard bound on stored records: the centre keeps the newest 100
#: and drops the oldest beyond that, so the file can never grow
#: without limit (the in-page array it replaces was unbounded).
MAX_NOTIFICATIONS = 100

#: Title/text bounds for a single record, so one event can never
#: flood the store file or the centre panel.
_MAX_TITLE_CHARS = 200
_MAX_TEXT_CHARS = 1000

#: The store file lives next to the other app data (settings.json,
#: task_history.json) under the same per-user app directory.
DEFAULT_NOTIFICATIONS_FILE = APP_DIR / "notifications.json"


def category_for_kind(kind: str) -> str | None:
    """Centre category for an event kind, or None if unmapped."""
    return KIND_CATEGORIES.get(kind)


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON atomically (same pattern as the
    memory/task stores: temp file in the same directory, fsync,
    then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=1))
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _clean_record(item: Any) -> dict | None:
    """Validate one loaded record; None when it is not usable.

    A record needs a non-empty string id and a known category.
    Title/text are coerced to bounded strings, the timestamp to a
    float (0.0 when unusable) and the read flag to a bool, so a
    hand-edited or partially damaged file degrades record by
    record instead of breaking the whole store.
    """
    if not isinstance(item, dict):
        return None
    record_id = item.get("id")
    category = item.get("category")
    if not isinstance(record_id, str) or not record_id:
        return None
    if category not in CATEGORIES:
        return None
    timestamp = item.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        timestamp = 0.0
    return {
        "id": record_id,
        "category": category,
        "title": str(item.get("title") or "")[:_MAX_TITLE_CHARS],
        "text": str(item.get("text") or "")[:_MAX_TEXT_CHARS],
        "timestamp": float(timestamp),
        "read": bool(item.get("read", False)),
    }


class NotificationStore:
    """Persistent, bounded Notification Center history.

    Records are kept newest-first in memory and on disk. Every
    mutation rewrites the JSON file atomically; a missing or
    corrupt file simply starts the store empty (history is a
    convenience record, never worth breaking startup over), and a
    failed save is swallowed for the same reason — the in-memory
    records still serve the running session. All writes in the
    real app are serialised by the app's own event/Tk thread (the
    toaster call sites and the bridge's enqueued mark/clear
    handlers all run there), so no extra locking lives here.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_NOTIFICATIONS_FILE
        self._records: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = None
            if isinstance(data, dict):
                data = data.get("notifications")
            if isinstance(data, list):
                cleaned = [record for record in
                           (_clean_record(item) for item in data)
                           if record is not None]
                self._records = cleaned[:MAX_NOTIFICATIONS]

    def _save(self) -> None:
        try:
            _atomic_write_json(self.path, self._records)
        except OSError:
            pass

    def add(self, category: str, title: str, text: str = "",
            *, timestamp: float | None = None) -> dict:
        """Append one record (newest first) and persist it.

        Raises ValueError for an unknown category or a record with
        neither title nor text — callers recording real events use
        :meth:`add_event`, which always supplies both from the
        event's own content mapping.
        """
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        clean_title = " ".join(str(title or "").split())[:_MAX_TITLE_CHARS]
        clean_text = " ".join(str(text or "").split())[:_MAX_TEXT_CHARS]
        if not clean_title and not clean_text:
            raise ValueError("a notification needs a title or text")
        record = {
            "id": uuid.uuid4().hex[:12],
            "category": category,
            "title": clean_title,
            "text": clean_text,
            "timestamp": float(timestamp if timestamp is not None
                               else time.time()),
            "read": False,
        }
        self._records.insert(0, record)
        del self._records[MAX_NOTIFICATIONS:]
        self._save()
        return dict(record)

    def add_event(self, kind: str, detail: str = "") -> dict | None:
        """Record one of the module's real event kinds.

        The title/text come from the same :func:`notification_content`
        mapping the toast shows, and the category from
        :data:`KIND_CATEGORIES`. An unknown kind records nothing
        and returns None.
        """
        category = KIND_CATEGORIES.get(kind)
        content = notification_content(kind, detail)
        if category is None or content is None:
            return None
        title, message = content
        return self.add(category, title, message)

    def list(self, limit: int | None = None) -> list[dict]:
        """Newest-first copies of the stored records."""
        if limit is None:
            return [dict(record) for record in self._records]
        safe_limit = max(0, min(int(limit), MAX_NOTIFICATIONS))
        return [dict(record) for record in self._records[:safe_limit]]

    def unread_count(self) -> int:
        return sum(1 for record in self._records if not record["read"])

    def mark_all_read(self) -> int:
        """Mark every record read; returns how many changed."""
        changed = 0
        for record in self._records:
            if not record["read"]:
                record["read"] = True
                changed += 1
        if changed:
            self._save()
        return changed

    def mark_read(self, ids) -> int:
        """Mark the records with the given ids read; returns how
        many changed. Unknown ids are ignored."""
        wanted = {str(item) for item in (ids or ())}
        if not wanted:
            return 0
        changed = 0
        for record in self._records:
            if record["id"] in wanted and not record["read"]:
                record["read"] = True
                changed += 1
        if changed:
            self._save()
        return changed

    def clear(self) -> None:
        """Remove every record and persist the empty history."""
        if self._records:
            self._records = []
            self._save()


def record_event(store: Any, settings: Any, kind: str,
                 *, detail: str = "") -> dict | None:
    """Record ``kind`` in ``store`` under the toast toggle rules.

    This is the Notification Center counterpart of
    :func:`notify_event` and shares its gating decision exactly:
    a kind whose Settings toggle is off is NOT recorded, and an
    unknown kind never is. It differs from the toast in one honest
    way — no tray icon is involved, so a record is kept even in
    foreground mode where no toast could fire. A missing store, a
    store without the event API, or any store failure returns
    None; this function never raises.
    """
    try:
        if store is None:
            return None
        if not notification_enabled(settings, kind):
            return None
        add_event = getattr(store, "add_event", None)
        if not callable(add_event):
            return None
        return add_event(kind, detail=detail)
    except Exception:
        return None
