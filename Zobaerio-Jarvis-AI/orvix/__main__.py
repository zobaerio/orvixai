"""CLI for the ORVIX Super Intelligence layer.

Usage:
  python -m orvix status              # JSON snapshot of agents/models/tasks/budget
  python -m orvix dashboard out.html  # write the Control Center HTML page
  python -m orvix ask "your command"  # run a command through the Chief Intelligence Agent

Without a configured provider, `ask` honestly reports blocked steps and the
exact configuration that is missing. Data files live under ~/.orvix
unless ORVIX_DATA_DIR is set.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _data_dir() -> Path:
    raw = os.environ.get("ORVIX_DATA_DIR")
    if raw:
        return Path(raw)
    return Path.home() / ".orvix"


def main(argv: list[str]) -> int:
    from .control_center import build_snapshot, render_html
    from .memory import LessonLog
    from .router import BudgetTracker, Orchestrator
    from .security import AuditLog
    from .taskqueue import TaskQueue

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    queue = TaskQueue(data / "task_queue.db")
    budget = BudgetTracker(path=data / "budget.json")
    audit = AuditLog(data / "audit.jsonl")
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        snap = build_snapshot(queue=queue, budget=budget, audit_log=audit)
        print(json.dumps(snap, indent=1, default=str))
        return 0
    if cmd == "dashboard":
        out = Path(argv[2]) if len(argv) > 2 else data / "control_center.html"
        out.write_text(render_html(build_snapshot(queue=queue, budget=budget, audit_log=audit)), encoding="utf-8")
        print(f"Control Center written to {out}")
        return 0
    if cmd == "ask":
        if len(argv) < 3:
            print('usage: python -m orvix ask "your command"')
            return 2
        from .agents import ChiefIntelligenceAgent
        chief = ChiefIntelligenceAgent(orchestrator=Orchestrator(budget=budget),
                                       lesson_log=LessonLog(data / "lessons.json"), audit_log=audit)
        result = chief.execute_command(" ".join(argv[2:]))
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0 if result["status"] == "completed" else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
