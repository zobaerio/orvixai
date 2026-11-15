"""Intelligent model routing, budget control, health monitoring and the
multi-model Orchestrator (fallback / retry / cross-model verification)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .providers import ALL_MODELS, ModelInfo, ProviderClient, ProviderError, build_clients

TASK_TYPES = ("chat", "reasoning", "coding", "research", "document", "planning", "verification")

_KEYWORDS = {
    "coding": ("code", "bug", "function", "python", "javascript", "compile", "debug", "refactor", "script", "program"),
    "research": ("research", "compare", "find out", "investigate", "sources", "what is the latest", "survey"),
    "document": ("document", "pdf", "docx", "xlsx", "csv", "summarise", "summarize", "extract from", "report file"),
    "reasoning": ("why", "explain", "analyse", "analyze", "prove", "plan", "strategy", "decide", "complex"),
    "planning": ("plan", "roadmap", "steps to", "project plan", "schedule"),
}


def classify_task(text: str) -> str:
    low = (text or "").lower()
    best, hits = "chat", 0
    for task_type, words in _KEYWORDS.items():
        n = sum(1 for w in words if w in low)
        if n > hits:
            best, hits = task_type, n
    return best


def complexity_score(text: str) -> float:
    """Deterministic 0..1 heuristic: length, sub-task markers, question count."""
    t = text or ""
    score = min(len(t) / 1200.0, 0.4)
    markers = (" and then ", "step", "first", "finally", ";", "\n-", "1.", "2.")
    score += 0.08 * sum(1 for m in markers if m in t.lower())
    score += 0.05 * t.count("?")
    return round(min(score, 1.0), 3)


class BudgetExceeded(RuntimeError):
    pass


class BudgetTracker:
    """Persistent spend tracker. Never silently exceeds the configured cap."""

    def __init__(self, path: Optional[Path] = None, limit_usd: Optional[float] = None):
        self.path = Path(path) if path else None
        if limit_usd is None:
            raw = os.environ.get("ORVIX_BUDGET_USD")
            limit_usd = float(raw) if raw else None
        self.limit_usd = limit_usd
        self._lock = threading.Lock()
        self.spent_usd = 0.0
        self.calls = 0
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.spent_usd, self.calls = float(data.get("spent_usd", 0)), int(data.get("calls", 0))
            except (ValueError, OSError):
                pass

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"spent_usd": self.spent_usd, "calls": self.calls}), encoding="utf-8")
            tmp.replace(self.path)

    def remaining(self) -> Optional[float]:
        return None if self.limit_usd is None else max(self.limit_usd - self.spent_usd, 0.0)

    def check(self, estimated: float):
        if self.limit_usd is not None and self.spent_usd + estimated > self.limit_usd:
            raise BudgetExceeded(
                f"budget limit ${self.limit_usd:.2f} would be exceeded "
                f"(spent ${self.spent_usd:.4f}, next call est. ${estimated:.4f})")

    def record(self, cost: float):
        with self._lock:
            self.spent_usd += cost
            self.calls += 1
            self._save()


class HealthMonitor:
    def __init__(self, failure_threshold: int = 3):
        self._state: dict[str, dict] = {}
        self._threshold = failure_threshold
        self._lock = threading.Lock()

    def record(self, provider: str, ok: bool):
        with self._lock:
            s = self._state.setdefault(provider, {"success": 0, "failure": 0, "consecutive_failures": 0, "last_ok": None})
            if ok:
                s["success"] += 1
                s["consecutive_failures"] = 0
                s["last_ok"] = time.time()
            else:
                s["failure"] += 1
                s["consecutive_failures"] += 1

    def healthy(self, provider: str) -> bool:
        s = self._state.get(provider)
        return not s or s["consecutive_failures"] < self._threshold

    def status(self) -> dict:
        return {p: {**s, "healthy": self.healthy(p)} for p, s in self._state.items()}

    def check_providers(self, clients: dict[str, ProviderClient]) -> dict:
        out = {}
        for name, client in clients.items():
            ok = client.health()
            self.record(name, ok)
            out[name] = ok
        return out


class Router:
    def rank(self, task_type: str, complexity: float, *, configured: dict[str, bool],
             health: HealthMonitor, budget_remaining: Optional[float], models=None) -> list[ModelInfo]:
        scored = []
        for info in (ALL_MODELS if models is None else models):
            if not configured.get(info.provider, False):
                continue
            if not health.healthy(info.provider):
                continue
            cap = 1.0 if task_type in info.capabilities else 0.25
            cost_penalty = min(info.cost_per_1k_tokens * 40, 0.5) if budget_remaining is not None else min(info.cost_per_1k_tokens * 10, 0.2)
            if budget_remaining is not None and info.cost_per_1k_tokens > budget_remaining * 2 and info.cost_per_1k_tokens > 0:
                continue  # single 1k-token block alone would blow the remaining budget
            quality = cap * (0.6 + 0.4 * complexity) if info.cost_per_1k_tokens > 0.002 else cap * 0.7
            score = quality - cost_penalty - 0.03 * info.latency_rank
            scored.append((score, info))
        scored.sort(key=lambda x: -x[0])
        return [info for _s, info in scored]


class NoProviderConfigured(RuntimeError):
    pass


@dataclass
class OrchestrationResult:
    text: str
    provider: str
    model: str
    task_type: str
    attempts: list = field(default_factory=list)
    cost_usd: float = 0.0
    verified: bool = False
    verifier: Optional[str] = None
    collaborators: list = field(default_factory=list)


class Orchestrator:
    """Central multi-model orchestrator: route -> call -> fallback -> verify."""

    def __init__(self, clients: Optional[dict[str, ProviderClient]] = None,
                 budget: Optional[BudgetTracker] = None, health: Optional[HealthMonitor] = None,
                 max_retries: int = 2, env: Optional[dict] = None, extra_models: tuple = ()):
        self.clients = clients or build_clients(env=env)
        self.budget = budget or BudgetTracker()
        self.health = health or HealthMonitor()
        self.router = Router()
        self.max_retries = max_retries
        self.models = tuple(ALL_MODELS) + tuple(extra_models)

    def _configured(self) -> dict[str, bool]:
        return {name: c.configured for name, c in self.clients.items()}

    def complete(self, prompt: str, *, task_type: Optional[str] = None, system: str = "",
                 verify: bool = False, max_tokens: int = 1024) -> OrchestrationResult:
        task_type = task_type or classify_task(prompt)
        complexity = complexity_score(prompt)
        candidates = self.router.rank(task_type, complexity, configured=self._configured(),
                                      health=self.health, budget_remaining=self.budget.remaining(), models=self.models)
        if not candidates:
            needed = ", ".join(sorted({s for s in
                                        ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                                         "XAI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY")}))
            raise NoProviderConfigured(
                f"no AI provider is configured or healthy. Set one of: {needed} "
                f"(or run a local Ollama server). Task type: {task_type}.")
        attempts: list[dict] = []
        last_error: Optional[Exception] = None
        for info in candidates:
            client = self.clients[info.provider]
            est = (max_tokens / 1000.0) * info.cost_per_1k_tokens
            try:
                self.budget.check(est)
            except BudgetExceeded as exc:
                attempts.append({"provider": info.provider, "model": info.model, "error": str(exc)})
                continue
            for attempt in range(self.max_retries):
                try:
                    messages = ([{"role": "system", "content": system}] if system else []) + \
                               [{"role": "user", "content": prompt}]
                    res = client.chat(messages, info.model, max_tokens=max_tokens)
                    cost = res.estimated_cost(info)
                    self.budget.record(cost)
                    self.health.record(info.provider, True)
                    attempts.append({"provider": info.provider, "model": info.model, "ok": True})
                    result = OrchestrationResult(res.text, info.provider, info.model, task_type,
                                                 attempts=attempts, cost_usd=cost)
                    if verify and complexity >= 0.35:
                        self._verify(result, prompt)
                    return result
                except ProviderError as exc:
                    last_error = exc
                    self.health.record(info.provider, False)
                    attempts.append({"provider": info.provider, "model": info.model,
                                     "error": str(exc), "attempt": attempt + 1})
                    if not exc.transient:
                        break
                    time.sleep(min(0.2 * (attempt + 1), 1.0))
        raise ProviderError("orchestrator", f"all candidate models failed; last error: {last_error}")

    def _verify(self, result: OrchestrationResult, original_prompt: str):
        """Cross-model verification: a *different* provider checks the answer."""
        others = [c for c in self.router.rank("verification", 0.5, configured=self._configured(),
                                              health=self.health, budget_remaining=self.budget.remaining(), models=self.models)
                  if c.provider != result.provider]
        if not others:
            return
        info = others[0]
        try:
            check = self.clients[info.provider].chat(
                [{"role": "user", "content":
                  f"Original request:\n{original_prompt}\n\nProposed answer:\n{result.text}\n\n"
                  "Reply with exactly 'VERIFIED' if the answer correctly and completely addresses the request, "
                  "otherwise reply 'ISSUES:' followed by a one-line description."}],
                info.model, max_tokens=256)
            self.budget.record(check.estimated_cost(info))
            result.verified = check.text.strip().upper().startswith("VERIFIED")
            result.verifier = f"{info.provider}/{info.model}"
        except ProviderError:
            result.verified = False
            result.verifier = f"{info.provider}/{info.model} (verification call failed)"

    def collaborate(self, prompt: str, *, max_models: int = 2) -> OrchestrationResult:
        """Run the same complex task through up to ``max_models`` different
        providers and return the primary answer annotated with collaborators.
        Simple tasks should use :meth:`complete` instead (no wasted calls)."""
        primary = self.complete(prompt)
        primary.collaborators = [f"{primary.provider}/{primary.model}"]
        if complexity_score(prompt) < 0.35:
            return primary
        configured = self._configured()
        others = [c for c in self.router.rank(classify_task(prompt), 0.7, configured=configured,
                                              health=self.health, budget_remaining=self.budget.remaining(), models=self.models)
                  if c.provider != primary.provider][: max_models - 1]
        reviews = []
        for info in others:
            try:
                r = self.clients[info.provider].chat(
                    [{"role": "user", "content": f"Review this answer for errors or gaps. "
                     f"Original request: {prompt}\nAnswer: {primary.text}\nReply briefly."}],
                    info.model, max_tokens=400)
                self.budget.record(r.estimated_cost(info))
                reviews.append(f"[{info.provider}/{info.model}] {r.text.strip()}")
                primary.collaborators.append(f"{info.provider}/{info.model}")
            except ProviderError:
                continue
        if reviews:
            primary.text += "\n\n--- Cross-model review ---\n" + "\n".join(reviews)
        return primary
