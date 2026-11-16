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
"""
from __future__ import annotations

from typing import Any

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
