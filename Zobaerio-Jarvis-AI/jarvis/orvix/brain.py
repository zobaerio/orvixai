"""ORVIX Central Brain: planner/decomposer + execution engine.

Pipeline: UNDERSTAND -> THINK -> PLAN -> DELEGATE -> EXECUTE -> VERIFY -> COMPLETE.
Honest reporting: a run is only "completed" when every step succeeded and the
verification step passed. Failures trigger one alternative-approach retry;
persistent failures are reported as exact blockers while all feasible steps
still run. Nothing here fabricates a result.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .router import classify_task, complexity_score

# task_type -> responsible specialised agent (see agents.py)
AGENT_FOR_TASK = {
    "research": "Research Agent",
    "coding": "Coding Agent",
    "document": "File Management Agent",
    "planning": "Chief Intelligence Agent",
    "reasoning": "Chief Intelligence Agent",
    "verification": "Quality Assurance Agent",
    "chat": "Personal Assistant Agent",
}

_BUILD_HINTS = ("build", "create", "make", "develop", "write a", "implement")
_RESEARCH_HINTS = ("research", "investigate", "find out", "compare")
_DOC_HINTS = ("document", "pdf", "file", "summarise", "summarize", "analyse this", "analyze this")


@dataclass
class Step:
    title: str
    task_type: str = "chat"
    agent: str = "Chief Intelligence Agent"
    status: str = "pending"  # pending|running|completed|failed|blocked|skipped
    result: str = ""
    error: str = ""
    attempts: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Plan:
    objective: str
    steps: list
    complexity: float = 0.0

    def to_dict(self) -> dict:
        return {"objective": self.objective, "complexity": self.complexity,
                "steps": [{"title": s.title, "task_type": s.task_type, "agent": s.agent} for s in self.steps]}


class Planner:
    """Deterministic task decomposition. An LLM planner can be layered on top
    by the Chief agent, but this baseline never invents steps it cannot name."""

    def decompose(self, objective: str) -> Plan:
        text = (objective or "").strip()
        low = text.lower()
        steps: list[Step] = []
        # Explicit sequencing in the user's own words wins first.
        parts = [p.strip(" .") for p in re.split(r"\band then\b|;|\n", text) if p.strip()]
        if len(parts) >= 2:
            for part in parts:
                tt = classify_task(part)
                steps.append(Step(title=part[:160], task_type=tt, agent=AGENT_FOR_TASK.get(tt, "Chief Intelligence Agent")))
        elif any(h in low for h in _BUILD_HINTS) and any(w in low for w in ("website", "app", "software", "code", "program", "tool")):
            steps = [
                Step("Analyse requirements and design the approach", "planning", "Software Engineering Agent"),
                Step(f"Implement: {text[:120]}", "coding", "Coding Agent"),
                Step("Test the implementation and fix errors", "coding", "Quality Assurance Agent"),
                Step("Review quality of the final implementation", "verification", "Quality Assurance Agent"),
            ]
        elif any(h in low for h in _RESEARCH_HINTS):
            steps = [
                Step(f"Research: {text[:120]}", "research", "Research Agent"),
                Step("Analyse and compare findings from sources", "reasoning", "Data Analysis Agent"),
                Step("Compile the research report", "document", "Research Agent"),
            ]
        elif any(h in low for h in _DOC_HINTS):
            steps = [
                Step(f"Read and extract the document content: {text[:100]}", "document", "File Management Agent"),
                Step("Analyse the extracted content", "reasoning", "Data Analysis Agent"),
            ]
        else:
            tt = classify_task(text)
            steps = [Step(title=text[:160], task_type=tt, agent=AGENT_FOR_TASK.get(tt, "Chief Intelligence Agent"))]
        if not steps or steps[-1].task_type != "verification":
            steps.append(Step("Verify the results against the original objective", "verification", "Quality Assurance Agent"))
        return Plan(objective=text, steps=steps, complexity=complexity_score(text))


@dataclass
class ExecutionReport:
    objective: str
    status: str  # completed | partial | blocked
    steps: list
    blockers: list = field(default_factory=list)
    plan: Optional[dict] = None

    def summary(self) -> str:
        done = sum(1 for s in self.steps if s.status == "completed")
        return f"{self.status}: {done}/{len(self.steps)} steps completed"


# A step runner returns the step result text, or raises on failure.
StepRunner = Callable[[Step, dict], str]


class ExecutionEngine:
    def __init__(self, runner: StepRunner, max_alternative_retries: int = 1):
        self.runner = runner
        self.max_alternative_retries = max_alternative_retries

    def execute(self, plan: Plan, context: Optional[dict] = None) -> ExecutionReport:
        ctx = context if context is not None else {}
        ctx.setdefault("history", [])
        blockers: list[str] = []
        for step in plan.steps:
            step.status = "running"
            try:
                step.result = self.runner(step, ctx)
                step.attempts += 1
                step.status = "completed"
                ctx["history"].append({"step": step.title, "result": step.result[:500]})
            except Exception as exc:  # try one alternative approach before giving up
                step.attempts += 1
                recovered = None
                for _ in range(self.max_alternative_retries):
                    try:
                        alt_ctx = dict(ctx)
                        alt_ctx["alternative_approach"] = True
                        alt_ctx["previous_error"] = str(exc)
                        recovered = self.runner(step, alt_ctx)
                        step.attempts += 1
                        break
                    except Exception as exc2:
                        exc = exc2
                if recovered is not None:
                    step.result, step.status = recovered, "completed"
                    ctx["history"].append({"step": step.title, "result": step.result[:500]})
                else:
                    step.status = "blocked" if "configured" in str(exc).lower() or "NoProvider" in type(exc).__name__ else "failed"
                    step.error = str(exc)
                    blockers.append(f"{step.title}: {step.error}")
                    # Verification cannot pass if earlier steps failed.
                    if step.task_type != "verification":
                        continue
            # Verification step: deterministic check that real results exist.
            if step.task_type == "verification" and step.status == "completed":
                prior = [s for s in plan.steps if s.task_type != "verification"]
                if any(s.status != "completed" for s in prior) or not any(s.result.strip() for s in prior):
                    step.status = "failed"
                    step.error = "verification failed: not all prior steps produced results"
                    blockers.append(step.error)
        failed = [s for s in plan.steps if s.status in ("failed", "blocked")]
        status = "completed" if not failed else ("blocked" if all(s.status == "blocked" for s in failed) else "partial")
        return ExecutionReport(objective=plan.objective, status=status, steps=plan.steps,
                               blockers=blockers, plan=plan.to_dict())
