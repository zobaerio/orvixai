"""ORVIX AI — the unified ORVIX package.

The desktop assistant plus its Autonomous Super Intelligence layer:
multi-model orchestration, the Chief Intelligence Agent + specialised
agents, a persistent background task queue, user-controlled memory and
lessons, document & research intelligence, security gates and a Control
Center snapshot/dashboard. See ORVIX_SUPER_INTELLIGENCE.md at the app root.
"""
from .providers import PROVIDERS, ALL_MODELS, APP_PROVIDER_NAME, ProviderClient, build_app_client, build_clients, configured_providers
from .router import Orchestrator, BudgetTracker, HealthMonitor, Router, classify_task, complexity_score
from .brain import Planner, ExecutionEngine, Plan, Step, ExecutionReport
from .agents import AGENT_SPECS, Agent, ChiefIntelligenceAgent
from .app_bridge import (
    BRAIN_SETTING_KEY, AppChiefIntelligenceAgent, BridgedAgent, DesktopOutcome,
    app_brain_configured, brain_setting_enabled, build_app_agents, build_app_chief,
    build_app_orchestrator, classify_desktop_action, format_brain_result,
    map_brain_status, route_decision, run_brain_command, sensitive_category,
)
from .taskqueue import TaskQueue, Worker
from .memory import MemoryStore, LessonLog
from .security import AuditLog, ConfirmationGate, RateLimiter, redact, get_secret
from .control_center import build_snapshot, render_html
from .version import APP_VERSION

__version__ = APP_VERSION

__all__ = [
    "PROVIDERS", "ALL_MODELS", "APP_PROVIDER_NAME", "ProviderClient", "build_app_client", "build_clients", "configured_providers",
    "Orchestrator", "BudgetTracker", "HealthMonitor", "Router", "classify_task", "complexity_score",
    "Planner", "ExecutionEngine", "Plan", "Step", "ExecutionReport",
    "AGENT_SPECS", "Agent", "ChiefIntelligenceAgent",
    "BRAIN_SETTING_KEY", "AppChiefIntelligenceAgent", "BridgedAgent", "DesktopOutcome",
    "app_brain_configured", "brain_setting_enabled", "build_app_agents", "build_app_chief",
    "build_app_orchestrator", "classify_desktop_action", "format_brain_result",
    "map_brain_status", "route_decision", "run_brain_command", "sensitive_category",
    "TaskQueue", "Worker", "MemoryStore", "LessonLog",
    "AuditLog", "ConfirmationGate", "RateLimiter", "redact", "get_secret",
    "build_snapshot", "render_html",
]
