"""Autonomous Control Center data layer + standalone dashboard page.

The snapshot is built exclusively from real state: the provider registry
(what is configured), the health monitor, the persistent task queue, the
budget tracker and the audit log. Unconfigured models are shown as
"not configured" — never as fake activity.
"""
from __future__ import annotations

import html
import platform
import time
from typing import Optional

from .agents import AGENT_SPECS
from .providers import PROVIDERS, configured_providers
from .router import BudgetTracker, HealthMonitor
from .taskqueue import TaskQueue


def build_snapshot(*, queue: Optional[TaskQueue] = None, health: Optional[HealthMonitor] = None,
                   budget: Optional[BudgetTracker] = None, audit_log=None, env: Optional[dict] = None) -> dict:
    configured = configured_providers(env)
    health_status = health.status() if health else {}
    models = []
    for name, spec in PROVIDERS.items():
        models.append({"provider": name, "configured": configured.get(name, False),
                       "env_key": spec.env_key or "(local, no key)",
                       "healthy": health_status.get(name, {}).get("healthy") if name in health_status else None,
                       "models": [m.model for m in spec.models]})
    return {
        "generated_at": time.time(),
        "system": {"platform": platform.platform(), "python": platform.python_version()},
        "agents": [{"name": s.name, "responsibility": s.responsibility,
                    "task_types": list(s.task_types), "tools": list(s.tools)} for s in AGENT_SPECS],
        "models": models,
        "tasks": queue.stats() if queue else None,
        "recent_tasks": ([{"id": t["id"][:8], "title": t["title"], "status": t["status"],
                           "attempts": t["attempts"]} for t in queue.list(limit=20)] if queue else []),
        "budget": ({"limit_usd": budget.limit_usd, "spent_usd": round(budget.spent_usd, 4),
                    "calls": budget.calls, "remaining_usd": budget.remaining()} if budget else None),
        "audit_tail": audit_log.tail(20) if audit_log else [],
    }


def render_html(snapshot: dict) -> str:
    esc = html.escape
    agents = "".join(f"<tr><td>{esc(a['name'])}</td><td>{esc(a['responsibility'])}</td>"
                     f"<td>{esc(', '.join(a['task_types']))}</td></tr>" for a in snapshot["agents"])
    models = "".join(
        f"<tr><td>{esc(m['provider'])}</td><td>{'configured' if m['configured'] else 'not configured'}</td>"
        f"<td>{esc(', '.join(m['models']))}</td><td>{esc(m['env_key'])}</td></tr>" for m in snapshot["models"])
    tasks = snapshot.get("tasks") or {}
    task_rows = "".join(f"<tr><td>{esc(t['title'])}</td><td>{esc(t['status'])}</td><td>{t['attempts']}</td></tr>"
                        for t in snapshot.get("recent_tasks", []))
    budget = snapshot.get("budget") or {}
    audit = "".join(f"<li>{esc(str(e.get('event')))} — {esc(str(e.get('actor')))}</li>"
                    for e in snapshot.get("audit_tail", []))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>ORVIX AI — Control Center</title>
<style>body{{background:#050b18;color:#d7e9ff;font-family:system-ui;margin:2rem}}
h1{{color:#38c8ff}}table{{border-collapse:collapse;margin:1rem 0;width:100%}}
td,th{{border:1px solid #1c3a5e;padding:.4rem .6rem;text-align:left}}th{{color:#7fd4ff}}</style></head>
<body><h1>ORVIX AI — Autonomous Control Center</h1>
<p>System: {esc(snapshot['system']['platform'])} · Python {esc(snapshot['system']['python'])}</p>
<h2>AI Models</h2><table><tr><th>Provider</th><th>Status</th><th>Models</th><th>Key (env)</th></tr>{models}</table>
<h2>Agents ({len(snapshot['agents'])})</h2><table><tr><th>Agent</th><th>Responsibility</th><th>Task types</th></tr>{agents}</table>
<h2>Background Tasks</h2><p>{esc(str(tasks))}</p>
<table><tr><th>Task</th><th>Status</th><th>Attempts</th></tr>{task_rows}</table>
<h2>API Budget</h2><p>Spent: ${budget.get('spent_usd', 0)} · Calls: {budget.get('calls', 0)} · Limit: {budget.get('limit_usd')}</p>
<h2>Recent Audit Events</h2><ul>{audit}</ul>
</body></html>"""
