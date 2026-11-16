"""Security & trust utilities: audit logging, secret redaction, confirmation
gates for sensitive actions, and a sliding-window rate limiter.

Secrets are only ever read from the environment / OS keyring by callers;
this module never stores secret values — the audit log redacts them.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Optional

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
]


def redact(text: str) -> str:
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) + "***") if m.lastindex else "***", out)
    return out


def get_secret(name: str) -> Optional[str]:
    """Read a secret from the environment only. Never logs the value."""
    value = os.environ.get(name)
    return value if value else None


class AuditLog:
    """Append-only JSONL audit log with redaction. Stores actions, not secrets."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event: str, *, actor: str = "system", details: Optional[dict] = None) -> dict:
        entry = {"at": time.time(), "event": event, "actor": actor,
                 "details": json.loads(redact(json.dumps(details or {}, default=str)))}
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, encoding="utf-8") as fh:
            return sum(1 for _ in fh)


SENSITIVE_CATEGORIES = ("destructive", "financial", "publishing", "account_change")


class ConfirmationRequired(RuntimeError):
    pass


class ConfirmationGate:
    """Sensitive actions require an explicit confirmer callback.

    Default posture is deny: with no confirmer configured, sensitive actions
    are refused (fail-closed), matching the existing app's approval dialogs.
    """

    def __init__(self, confirmer: Optional[Callable[[str, str], bool]] = None, audit: Optional[AuditLog] = None):
        self.confirmer = confirmer
        self.audit = audit

    def check(self, action: str, category: str) -> bool:
        if category not in SENSITIVE_CATEGORIES:
            return True
        approved = bool(self.confirmer and self.confirmer(action, category))
        if self.audit:
            self.audit.log("confirmation_gate", actor="security",
                           details={"action": action[:200], "category": category, "approved": approved})
        if not approved:
            raise ConfirmationRequired(
                f"action '{action}' ({category}) requires explicit user confirmation and was not approved")
        return True


class RateLimiter:
    """Sliding-window limiter, e.g. max provider calls per minute per key."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        self.max_calls, self.window = max_calls, window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str = "default") -> bool:
        now = time.time()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max_calls:
                return False
            dq.append(now)
            return True
