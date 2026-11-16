"""The ORVIX agent ecosystem: a Chief Intelligence Agent coordinating the
15 specialised agents from the Super Intelligence spec.

Agents are real classes with defined responsibilities, task types and tools.
An agent either performs local work (file / research / data tools) or
delegates generation to the multi-model Orchestrator. With no provider
configured, an agent returns an honest "blocked" result naming the missing
configuration instead of inventing output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .brain import ExecutionEngine, Planner
from .router import Orchestrator, NoProviderConfigured


@dataclass
class AgentResult:
    agent: str
    ok: bool
    text: str
    blocked_reason: str = ""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    responsibility: str
    task_types: tuple
    tools: tuple


AGENT_SPECS: tuple = (
    AgentSpec("Chief Intelligence Agent", "Central decision-making: receives commands, plans, delegates, verifies, reports", ("planning", "reasoning"), ("planner", "orchestrator", "task_queue")),
    AgentSpec("Research Agent", "Web research, source gathering and verification", ("research",), ("web_fetch", "web_search", "documents")),
    AgentSpec("Coding Agent", "Writes and edits code to a specification", ("coding",), ("orchestrator",)),
    AgentSpec("Software Engineering Agent", "Architecture, code analysis, debugging, PR preparation", ("coding", "planning"), ("orchestrator", "documents")),
    AgentSpec("Browser Automation Agent", "Supervised website navigation tasks via authorised integrations", ("research",), ("web_fetch",)),
    AgentSpec("Computer Use Agent", "Permission-gated desktop actions through the existing automation tools", ("chat",), ("desktop_automation",)),
    AgentSpec("File Management Agent", "Reads, extracts and organises files and documents", ("document",), ("documents", "file_extract")),
    AgentSpec("Data Analysis Agent", "Structured data analysis over CSV/XLSX/text", ("document", "reasoning"), ("documents", "csv_stats")),
    AgentSpec("Business Intelligence Agent", "Summaries, comparisons and decision briefs", ("reasoning", "research"), ("orchestrator", "documents")),
    AgentSpec("Productivity Agent", "Task lists, reminders and workflow organisation (local)", ("planning", "chat"), ("task_queue", "memory")),
    AgentSpec("Communication Agent", "Drafts messages and documents; never sends without confirmation", ("chat", "document"), ("orchestrator",)),
    AgentSpec("Security Agent", "Security review, redaction checks and audit inspection", ("verification",), ("audit_log", "redaction")),
    AgentSpec("Quality Assurance Agent", "Verifies results against objectives and runs checks", ("verification",), ("orchestrator", "verification")),
    AgentSpec("System Administration Agent", "System health, configuration and diagnostics (read-only by default)", ("chat",), ("health", "audit_log")),
    AgentSpec("Workflow Automation Agent", "Builds and runs reusable multi-step workflows on the task queue", ("planning",), ("task_queue", "planner")),
    AgentSpec("Personal Assistant Agent", "Everyday conversation, preferences and memory-aware help", ("chat",), ("orchestrator", "memory")),
)


class Agent:
    def __init__(self, spec: AgentSpec, orchestrator: Optional[Orchestrator] = None):
        self.spec = spec
        self.orchestrator = orchestrator

    @property
    def name(self) -> str:
        return self.spec.name

    def run(self, task: str, context: Optional[dict] = None) -> AgentResult:
        context = context or {}
        # Local, no-model work first where the agent owns a real tool.
        if "document_text" in context and self.spec.name in ("File Management Agent", "Data Analysis Agent"):
            from .documents import answer_question, summarize_extractive
            if context.get("question"):
                return AgentResult(self.name, True, answer_question(context["document_text"], context["question"]))
            return AgentResult(self.name, True, summarize_extractive(context["document_text"]))
        if self.orchestrator is None:
            return AgentResult(self.name, False, "",
                               blocked_reason="no AI provider configured for this agent "
                                              "(set a provider API key or run local Ollama)")
        try:
            result = self.orchestrator.complete(
                task, task_type=self.spec.task_types[0],
                system=f"You are the {self.spec.name} of ORVIX AI. Responsibility: {self.spec.responsibility}. "
                       "Be accurate, concise and honest about limitations.")
            return AgentResult(self.name, True, result.text)
        except NoProviderConfigured as exc:
            return AgentResult(self.name, False, "", blocked_reason=str(exc))
        except Exception as exc:
            return AgentResult(self.name, False, "", blocked_reason=str(exc))


class ChiefIntelligenceAgent:
    """Central Brain entry point: command -> plan -> delegate -> verify -> report."""

    def __init__(self, orchestrator: Optional[Orchestrator] = None, agents: Optional[dict] = None,
                 lesson_log=None, audit_log=None):
        self.orchestrator = orchestrator
        self.agents = agents or {spec.name: Agent(spec, orchestrator) for spec in AGENT_SPECS}
        self.planner = Planner()
        self.lesson_log = lesson_log
        self.audit_log = audit_log

    def _runner(self, step, context):
        agent = self.agents.get(step.agent) or self.agents["Chief Intelligence Agent"]
        prompt = step.title
        if context.get("alternative_approach"):
            prompt += (f"\n\nA previous attempt failed with: {context.get('previous_error', '')}. "
                       "Use a different, simpler approach.")
        if context.get("history"):
            prompt += "\n\nResults so far:\n" + "\n".join(
                f"- {h['step']}: {h['result'][:200]}" for h in context["history"])
        result = agent.run(prompt, context)
        if not result.ok:
            raise RuntimeError(result.blocked_reason or f"{agent.name} failed")
        return result.text

    def execute_command(self, command: str, context: Optional[dict] = None) -> dict:
        plan = self.planner.decompose(command)
        engine = ExecutionEngine(self._runner)
        report = engine.execute(plan, dict(context or {}))
        if self.audit_log is not None:
            self.audit_log.log("command_executed", actor="chief",
                               details={"status": report.status, "steps": len(report.steps),
                                        "blockers": report.blockers})
        if self.lesson_log is not None:
            from .memory import evaluate_execution
            self.lesson_log.record(task_type=plan.steps[0].task_type if plan.steps else "chat",
                                   strategy="; ".join(s.title for s in plan.steps),
                                   outcome=report.status,
                                   lesson=evaluate_execution(report))
        return {"objective": report.objective, "status": report.status,
                "summary": report.summary(), "blockers": report.blockers,
                "steps": [{"title": s.title, "agent": s.agent, "status": s.status,
                           "result": s.result, "error": s.error} for s in report.steps],
                "plan": report.plan}
