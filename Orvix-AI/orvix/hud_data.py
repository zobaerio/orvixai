"""Pure data assembly for the HUD home page panels.

The home view's Control Center panel shows REAL data only: the
provider/agent registry, the persistent background-task queue, the
budget tracker and the audit log (via
:func:`orvix.control_center.build_snapshot`), plus the desktop app's
own task records (``TaskStore`` rows). This module turns those raw
structures into display lines. It never invents a number: anything
that is missing or was never measured renders as an em dash or an
explicit "not checked / none recorded" line.

No tkinter import — fully unit-testable headless with fake snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

DASH = "—"

_STATUS_MARKS = {
    "completed": "✓",
    "failed": "✗",
    "running": "●",
    "queued": "◌",
    "pending": "◌",
    "paused": "❚❚",
    "cancelled": "✗",
}


@dataclass(frozen=True)
class HudPanelData:
    """Display-ready lines for the HUD home panels."""

    agents: str
    models: str
    health: str
    queue: str
    budget: str
    memory: str
    recent_tasks: tuple[str, ...]
    log_lines: tuple[str, ...]


def _clip(text: object, limit: int) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: max(0, limit - 1)] + "…"


def _agents_line(snapshot: Optional[dict]) -> str:
    agents = (snapshot or {}).get("agents") or []
    if not agents:
        return f"AGENTS  {DASH}"
    return f"AGENTS  {len(agents)} REGISTERED"


def _models_line(snapshot: Optional[dict]) -> str:
    models = (snapshot or {}).get("models") or []
    if not models:
        return f"PROVIDERS  {DASH}"
    configured = sum(1 for m in models if m.get("configured"))
    return f"PROVIDERS  {configured}/{len(models)} CONFIGURED"


def _health_line(snapshot: Optional[dict]) -> str:
    models = (snapshot or {}).get("models") or []
    known = [m.get("healthy") for m in models if m.get("healthy") is not None]
    if not known:
        # No provider health check has run in this process; say so
        # instead of guessing a status.
        return f"PROVIDER HEALTH  {DASH}  NOT CHECKED"
    up = sum(1 for h in known if h)
    return f"PROVIDER HEALTH  {up}/{len(known)} UP"


def _queue_line(snapshot: Optional[dict], app_tasks: Optional[Sequence[dict]]) -> str:
    """Queue stats from the desktop app's own task records when present,
    falling back to the background queue stats from the snapshot."""
    if app_tasks is not None:
        counts: dict[str, int] = {}
        for row in app_tasks:
            status = str(row.get("status", "")).lower()
            if status:
                counts[status] = counts.get(status, 0) + 1
        waiting = counts.get("queued", 0) + counts.get("pending", 0)
        running = counts.get("running", 0)
        done = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        return f"TASKS  {waiting} QUEUED · {running} RUNNING · {done} DONE · {failed} FAILED"
    stats = (snapshot or {}).get("tasks")
    if stats:
        waiting = int(stats.get("pending", 0))
        running = int(stats.get("running", 0))
        done = int(stats.get("completed", 0))
        failed = int(stats.get("failed", 0))
        return f"TASKS  {waiting} QUEUED · {running} RUNNING · {done} DONE · {failed} FAILED"
    return f"TASKS  {DASH}"


def _budget_line(snapshot: Optional[dict]) -> str:
    budget = (snapshot or {}).get("budget")
    if not budget:
        return f"BUDGET  {DASH}"
    spent = budget.get("spent_usd")
    limit = budget.get("limit_usd")
    calls = budget.get("calls")
    if spent is None:
        return f"BUDGET  {DASH}"
    line = f"BUDGET  ${float(spent):.2f} SPENT"
    if limit:
        line += f" / ${float(limit):.2f} CAP"
    if calls is not None:
        line += f" · {int(calls)} CALLS"
    return line


def _memory_line(memory_count: Optional[int], lesson_count: Optional[int]) -> str:
    if memory_count is None and lesson_count is None:
        return f"MEMORY  {DASH}"
    memories = f"{int(memory_count)} ITEMS" if memory_count is not None else DASH
    lessons = f"{int(lesson_count)} LESSONS" if lesson_count is not None else DASH
    return f"MEMORY  {memories} · {lessons}"


def _recent_task_lines(app_tasks: Optional[Sequence[dict]],
                       snapshot: Optional[dict]) -> tuple[str, ...]:
    """Most recent finished/active tasks, newest first (max 3)."""
    rows: list[dict] = []
    if app_tasks:
        rows = [dict(r) for r in app_tasks[:3]]
        lines = []
        for row in rows:
            status = str(row.get("status", "")).lower()
            mark = _STATUS_MARKS.get(status, "·")
            lines.append(f"{mark} {_clip(row.get('command', ''), 34)}")
        return tuple(lines)
    recent = (snapshot or {}).get("recent_tasks") or []
    if recent:
        lines = []
        for row in recent[:3]:
            status = str(row.get("status", "")).lower()
            mark = _STATUS_MARKS.get(status, "·")
            lines.append(f"{mark} {_clip(row.get('title', ''), 34)}")
        return tuple(lines)
    return ("NO TASKS RECORDED YET",)


def _log_lines(snapshot: Optional[dict]) -> tuple[str, ...]:
    """Tail of the audit log (execution log), newest last (max 3 shown
    newest-first). Empty log -> an explicit 'none recorded' line."""
    tail = (snapshot or {}).get("audit_tail") or []
    if not tail:
        return ("NO AUDIT EVENTS RECORDED",)
    lines = []
    for entry in reversed(tail[-3:]):
        if isinstance(entry, dict):
            event = _clip(entry.get("event", ""), 22)
            actor = _clip(entry.get("actor", ""), 12)
            lines.append(f"{event} · {actor}" if actor else event)
        else:
            lines.append(_clip(entry, 36))
    return tuple(lines)


def format_task_timestamp(value: object) -> str:
    """Format an ISO task timestamp as 'YYYY-MM-DD HH:MM'.

    The wall-clock time is taken as recorded (TaskStore stamps local
    time with its offset), so no timezone conversion is applied.
    Missing or unparseable values render as an em dash — a time is
    shown only when one was actually recorded.
    """
    if not isinstance(value, str) or not value.strip():
        return DASH
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return DASH
    return parsed.strftime("%Y-%m-%d %H:%M")


def _plan_steps(record: dict) -> list[dict]:
    """The record's plan steps (``TaskStore.set_task_plan`` rows).

    Only dict rows count — that is the shape the store writes. A
    record without a plan (most simple commands never get one) has
    no plan steps at all, which is different from a plan whose steps
    are all still pending."""
    plan = record.get("plan")
    if not isinstance(plan, list):
        return []
    return [step for step in plan if isinstance(step, dict)]


def task_progress_fraction(record: object) -> Optional[float]:
    """Completed plan steps / total plan steps for one task record.

    Returns None when the record carries no plan steps: with no plan
    there is no honest percentage to report (a UI must show the
    status only, never a fabricated 0%). A plan that exists but has
    no completed step yet honestly reports 0.0.
    """
    if not isinstance(record, dict):
        return None
    steps = _plan_steps(record)
    if not steps:
        return None
    completed = sum(
        1 for step in steps
        if str(step.get("status") or "").strip().lower() == "completed")
    return completed / len(steps)


def task_current_step(record: object) -> Optional[str]:
    """The record's current step text, or None when there is none.

    The running plan step's title wins (that is the step executing
    right now); otherwise the latest recorded progress event in the
    record's ``steps`` list is the most recent thing the task was
    seen doing. A record with neither has no current step — callers
    must not substitute the command text and call it a step.
    """
    if not isinstance(record, dict):
        return None
    for step in _plan_steps(record):
        if str(step.get("status") or "").strip().lower() == "running":
            title = " ".join(str(step.get("title") or "").split())
            if title:
                return title
    events = record.get("steps")
    if isinstance(events, list):
        for event in reversed(events):
            if isinstance(event, dict):
                text = " ".join(str(event.get("text") or "").split())
                if text:
                    return text
    return None


def build_task_entries(records: Optional[Sequence[dict]],
                       limit: int = 10) -> list[dict]:
    """Display-ready entries for a newest-first TaskStore record list.

    Each entry carries only what the record itself holds: its status
    (plus the HUD status mark for that status), its command text with
    whitespace collapsed, and its created/finished times formatted by
    :func:`format_task_timestamp`. ``finishedAt`` stays None while a
    task has not finished — it is never invented. Command Center
    Phase 2 adds ``progress`` (the plan-step fraction from
    :func:`task_progress_fraction`; None when the record has no plan
    steps) and ``currentStep`` (from :func:`task_current_step`; None
    when the record records no step). Non-dict rows are skipped and
    the result is bounded to ``limit`` entries; an empty or missing
    history yields an empty list.
    """
    if not records:
        return []
    try:
        bound = max(0, int(limit))
    except (TypeError, ValueError):
        bound = 10
    entries: list[dict] = []
    for row in records:
        if len(entries) >= bound:
            break
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        finished_raw = row.get("finished_at")
        entries.append({
            "status": status,
            "mark": _STATUS_MARKS.get(status, "·"),
            "command": " ".join(str(row.get("command", "")).split()),
            "createdAt": format_task_timestamp(row.get("created_at")),
            "finishedAt": (format_task_timestamp(finished_raw)
                           if finished_raw else None),
            "progress": task_progress_fraction(row),
            "currentStep": task_current_step(row),
        })
    return entries


def build_hud_panels(snapshot: Optional[dict], app_tasks: Optional[Sequence[dict]],
                     *, memory_count: Optional[int] = None,
                     lesson_count: Optional[int] = None) -> HudPanelData:
    """Assemble every HUD panel line from real state only.

    ``snapshot`` is a :func:`orvix.control_center.build_snapshot` dict
    (or None when the snapshot could not be built); ``app_tasks`` is a
    newest-first list of desktop ``TaskStore`` records (or None).
    """
    return HudPanelData(
        agents=_agents_line(snapshot),
        models=_models_line(snapshot),
        health=_health_line(snapshot),
        queue=_queue_line(snapshot, app_tasks),
        budget=_budget_line(snapshot),
        memory=_memory_line(memory_count, lesson_count),
        recent_tasks=_recent_task_lines(app_tasks, snapshot),
        log_lines=_log_lines(snapshot),
    )
