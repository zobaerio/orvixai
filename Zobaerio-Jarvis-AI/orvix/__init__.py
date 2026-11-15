"""ORVIX AI — Autonomous Super Intelligence layer.

Built on top of the existing desktop assistant without replacing it:
multi-model orchestration, the Chief Intelligence Agent + specialised
agents, a persistent background task queue, user-controlled memory and
lessons, document & research intelligence, security gates and a Control
Center snapshot/dashboard. See ORVIX_SUPER_INTELLIGENCE.md at the app root.
"""
from .providers import PROVIDERS, ALL_MODELS, ProviderClient, build_clients, configured_providers
from .router import Orchestrator, BudgetTracker, HealthMonitor, Router, classify_task, complexity_score
from .brain import Planner, ExecutionEngine, Plan, Step, ExecutionReport
from .agents import AGENT_SPECS, Agent, ChiefIntelligenceAgent
from .taskqueue import TaskQueue, Worker
from .memory import MemoryStore, LessonLog
from .security import AuditLog, ConfirmationGate, RateLimiter, redact, get_secret
from .control_center import build_snapshot, render_html

__all__ = [
    "PROVIDERS", "ALL_MODELS", "ProviderClient", "build_clients", "configured_providers",
    "Orchestrator", "BudgetTracker", "HealthMonitor", "Router", "classify_task", "complexity_score",
    "Planner", "ExecutionEngine", "Plan", "Step", "ExecutionReport",
    "AGENT_SPECS", "Agent", "ChiefIntelligenceAgent",
    "TaskQueue", "Worker", "MemoryStore", "LessonLog",
    "AuditLog", "ConfirmationGate", "RateLimiter", "redact", "get_secret",
    "build_snapshot", "render_html",
]
