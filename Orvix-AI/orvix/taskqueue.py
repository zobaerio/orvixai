"""Persistent background task queue (SQLite, stdlib only).

Tasks survive process restarts: on startup, tasks left in ``running`` by a
crashed process are returned to ``pending`` with their checkpoint intact.
Supports priority ordering, pause / resume / cancel / retry, checkpoints and
a threaded worker.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

STATUSES = ("pending", "running", "paused", "completed", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', priority INTEGER NOT NULL DEFAULT 5,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  checkpoint TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority, created_at);
"""


class TaskQueue:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.recover()

    def recover(self) -> int:
        """Return interrupted 'running' tasks to 'pending' (restart survival)."""
        with self._lock:
            cur = self._db.execute("UPDATE tasks SET status='pending', updated_at=? WHERE status='running'",
                                   (time.time(),))
            self._db.commit()
            return cur.rowcount

    def enqueue(self, title: str, payload: Optional[dict] = None, priority: int = 5,
                max_attempts: int = 3) -> str:
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (task_id, title[:300], json.dumps(payload or {}), "pending", priority,
                              0, max_attempts, "{}", "", "", now, now))
            self._db.commit()
        return task_id

    def _row(self, task_id: str) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"] or "{}")
        d["checkpoint"] = json.loads(d["checkpoint"] or "{}")
        return d

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._row(task_id)

    def list(self, status: Optional[str] = None, limit: int = 100) -> list:
        with self._lock:
            if status:
                rows = self._db.execute("SELECT * FROM tasks WHERE status=? ORDER BY priority, created_at LIMIT ?",
                                        (status, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._row(r["id"]) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status").fetchall()
            return {s: 0 for s in STATUSES} | {r["status"]: r["c"] for r in rows}

    def _set(self, task_id: str, **fields):
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
            self._db.commit()

    def claim_next(self) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY priority ASC, created_at ASC LIMIT 1").fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                             (time.time(), row["id"]))
            self._db.commit()
            return self._row(row["id"])

    def complete(self, task_id: str, result: str = ""):
        self._set(task_id, status="completed", result=result[:20000], error="")

    def fail(self, task_id: str, error: str):
        task = self.get(task_id)
        if task and task["attempts"] < task["max_attempts"]:
            self._set(task_id, status="pending", error=error[:2000])
        else:
            self._set(task_id, status="failed", error=error[:2000])

    def start(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task and task["status"] == "pending":
            self._set(task_id, status="running", attempts=task["attempts"] + 1)
            return True
        return False

    def finish(self, task_id: str, status: str, result: str = "", error: str = "") -> bool:
        if status not in STATUSES or status in ("pending", "running"):
            raise ValueError(f"finish status must be a terminal status, got {status!r}")
        task = self.get(task_id)
        if not task:
            return False
        self._set(task_id, status=status, result=result[:20000], error=error[:2000])
        return True

    def pause(self, task_id: str):
        task = self.get(task_id)
        if task and task["status"] in ("pending", "running"):
            self._set(task_id, status="paused")

    def resume(self, task_id: str):
        task = self.get(task_id)
        if task and task["status"] == "paused":
            self._set(task_id, status="pending")

    def cancel(self, task_id: str):
        task = self.get(task_id)
        if task and task["status"] in ("pending", "paused", "running"):
            self._set(task_id, status="cancelled")

    def retry(self, task_id: str):
        task = self.get(task_id)
        if task and task["status"] in ("failed", "cancelled"):
            self._set(task_id, status="pending", attempts=0, error="")

    def save_checkpoint(self, task_id: str, checkpoint: dict):
        self._set(task_id, checkpoint=json.dumps(checkpoint))

    def close(self):
        with self._lock:
            self._db.close()


class Worker:
    """Threaded worker draining a TaskQueue with a user handler.

    Handler signature: handler(task_dict, save_checkpoint, is_cancelled) -> str result
    """

    def __init__(self, queue: TaskQueue, handler: Callable, poll_seconds: float = 0.25):
        self.queue, self.handler, self.poll_seconds = queue, handler, poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> bool:
        task = self.queue.claim_next()
        if task is None:
            return False

        def save_checkpoint(cp: dict):
            self.queue.save_checkpoint(task["id"], cp)

        def is_cancelled() -> bool:
            current = self.queue.get(task["id"])
            return not current or current["status"] == "cancelled"

        try:
            result = self.handler(task, save_checkpoint, is_cancelled)
            if is_cancelled():
                return True
            self.queue.complete(task["id"], str(result or ""))
        except Exception as exc:
            self.queue.fail(task["id"], str(exc))
        return True

    def _loop(self):
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_seconds)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="orvix-worker")
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
