"""User-controlled persistent memory + self-improvement lesson log.

Memory is explicit: entries are only stored when code/user asks to remember
something. The user can list, delete or clear everything at any time. Storage
is a bounded JSON file written atomically (same pattern as task_store.py).
ORVIX never modifies its own code, security policy or permissions from
lessons — lessons only inform future planning choices.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

MAX_ENTRIES = 500
MAX_VALUE_LENGTH = 2000
CATEGORIES = ("preference", "fact", "strategy", "note")


def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(tmp, "ab") as fh:
        os.fsync(fh.fileno())
    tmp.replace(path)


class MemoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._entries = [e for e in data if isinstance(e, dict)][:MAX_ENTRIES]
            except (ValueError, OSError):
                self._entries = []

    def remember(self, key: str, value: str, category: str = "fact") -> dict:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        key, value = key.strip()[:200], value.strip()[:MAX_VALUE_LENGTH]
        if not key or not value:
            raise ValueError("key and value are required")
        with self._lock:
            self._entries = [e for e in self._entries if e["key"] != key]
            entry = {"id": uuid.uuid4().hex[:12], "key": key, "value": value,
                     "category": category, "updated_at": time.time()}
            self._entries.append(entry)
            self._entries = self._entries[-MAX_ENTRIES:]
            _atomic_write(self.path, self._entries)
            return entry

    def recall(self, key: str) -> Optional[str]:
        for e in reversed(self._entries):
            if e["key"] == key:
                return e["value"]
        return None

    def search(self, query: str) -> list[dict]:
        q = query.lower()
        return [e for e in self._entries if q in e["key"].lower() or q in e["value"].lower()]

    def list(self, category: Optional[str] = None) -> list[dict]:
        return [e for e in self._entries if category is None or e["category"] == category]

    def forget(self, key: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e["key"] != key]
            if len(self._entries) != before:
                _atomic_write(self.path, self._entries)
                return True
            return False

    def clear(self):
        with self._lock:
            self._entries = []
            _atomic_write(self.path, [])

    def prompt_summary(self, max_chars: int = 1200) -> str:
        """Bounded memory block for inclusion in a model prompt."""
        lines = [f"- ({e['category']}) {e['key']}: {e['value']}" for e in self._entries]
        out = ""
        for line in lines:
            if len(out) + len(line) > max_chars:
                break
            out += line + "\n"
        return out.strip()


class LessonLog:
    """Post-task evaluation log: what strategy was used, outcome, lesson."""

    def __init__(self, path: Path | str, max_lessons: int = 300):
        self.path = Path(path)
        self.max_lessons = max_lessons
        self._lock = threading.Lock()
        self._lessons: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._lessons = [x for x in data if isinstance(x, dict)][-max_lessons:]
            except (ValueError, OSError):
                self._lessons = []

    def record(self, *, task_type: str, strategy: str, outcome: str, lesson: str) -> dict:
        entry = {"id": uuid.uuid4().hex[:12], "task_type": task_type,
                 "strategy_hash": hashlib.sha256(strategy.encode()).hexdigest()[:12],
                 "strategy": strategy[:1000], "outcome": outcome,
                 "lesson": lesson[:1000], "at": time.time()}
        with self._lock:
            self._lessons.append(entry)
            self._lessons = self._lessons[-self.max_lessons:]
            _atomic_write(self.path, self._lessons)
        return entry

    def successful_strategies(self, task_type: str) -> list[dict]:
        return [x for x in self._lessons if x["task_type"] == task_type and x["outcome"] == "completed"]

    def list(self) -> list[dict]:
        return list(self._lessons)


def evaluate_execution(report) -> str:
    """Deterministic post-task evaluation (no model call needed)."""
    total = len(report.steps)
    done = sum(1 for s in report.steps if s.status == "completed")
    if report.status == "completed":
        return f"Strategy worked: all {total} steps completed. Reuse this decomposition for similar objectives."
    if report.blockers:
        return (f"Partial/blocked: {done}/{total} steps completed. First blocker: {report.blockers[0][:300]}. "
                "Next time, check this precondition before starting dependent steps.")
    return f"Outcome {report.status}: {done}/{total} steps completed."
