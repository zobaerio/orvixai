"""Bridge between the desktop app and the Chief Intelligence Agent.

This module is the single place where the desktop app's configuration
(settings dict + OS-keyring API key) is adapted into the Super
Intelligence layer (providers / router / agents). It has no tkinter
import so it can be unit-tested headless.

Design notes:

* Single config source: the app endpoint is built from the app's own
  settings + keyring key via :func:`build_app_client`. No environment
  variable duplication and the key is never logged or written.
* Routing is opt-in (``autonomous_brain`` setting, default off) and
  only for commands the caller classifies as complex.
* Desktop steps are executed through the app's existing agent
  (``OrvixAgent.respond``) via a caller-supplied ``desktop_execute``
  callable, so all of that agent's approval gates stay in force.
  Sensitive desktop steps are additionally pre-checked by the
  fail-closed :class:`ConfirmationGate`.
* Take Control (``take_control`` setting, default off): while on,
  :class:`BridgedAgent` — the delegation boundary — forces EVERY
  delegated desktop step through that same gate, so autonomous or
  queued Brain execution cannot start a desktop step without the
  user's explicit approval. Off, nothing changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .agents import AGENT_SPECS, Agent, AgentResult, ChiefIntelligenceAgent
from .providers import APP_PROVIDER_NAME, build_app_client, build_clients
from .router import BudgetTracker, Orchestrator
from .security import TAKE_CONTROL_CATEGORY, AuditLog, ConfirmationGate, ConfirmationRequired
from .settings import is_local_api_endpoint, validate_api_key_transport
from .taskqueue import TaskQueue

BRAIN_SETTING_KEY = "autonomous_brain"

TAKE_CONTROL_SETTING_KEY = "take_control"


def brain_setting_enabled(settings) -> bool:
    return bool(settings and settings.get(BRAIN_SETTING_KEY))


def take_control_enabled(settings) -> bool:
    """Whether Take Control mode is on in ``settings``.

    Take Control is an app-level flag persisted in settings like the
    other toggles (``take_control``, default off). While it is on,
    every delegated desktop step requires explicit approval — see
    :class:`BridgedAgent`, the delegation boundary where the check
    lives. With it off, approval behaviour is exactly as before.
    """
    return bool(settings and settings.get(TAKE_CONTROL_SETTING_KEY))


def _take_control_active(flag) -> bool:
    """Resolve a Take Control flag that may be a bool or a callable.

    A callable is consulted on every step, so turning Take Control
    on or off mid-task takes effect on the very next delegated step
    instead of only on the next task. A callable that raises is
    treated as ON (fail-closed): a broken control check must force
    approval, never silently skip it.
    """
    if callable(flag):
        try:
            return bool(flag())
        except Exception:
            return True
    return bool(flag)


def app_brain_configured(settings, api_key) -> tuple[bool, str]:
    """Mirror the desktop agent's provider-configured checks.

    Returns ``(True, "")`` when the app endpoint can be used, else
    ``(False, reason)``. The API key is never included in any string.
    """
    if not settings:
        return False, "AI model is not configured: settings are missing."
    base_url = str(settings.get("api_base_url", "") or "").strip()
    model = str(settings.get("model", "") or "").strip()
    if not base_url or not model:
        return False, "AI model is not configured: set the API base URL and model in Settings."
    key = api_key or ""
    try:
        validate_api_key_transport(base_url, key)
    except ValueError as exc:
        return False, str(exc)
    try:
        local = is_local_api_endpoint(base_url)
    except ValueError as exc:
        return False, str(exc)
    if not local and not key:
        return False, "AI API key is not configured for the remote endpoint. Add it in Settings or use a local endpoint."
    return True, ""


def route_decision(settings, api_key, text, is_complex) -> str:
    """Return "legacy" | "brain" | "brain_unconfigured" for a command."""
    if not brain_setting_enabled(settings):
        return "legacy"
    if not is_complex(text):
        return "legacy"
    configured, _reason = app_brain_configured(settings, api_key)
    return "brain" if configured else "brain_unconfigured"


def _first_line(text) -> str:
    raw = str(text or "")
    for line in raw.splitlines():
        if line.strip():
            return line.strip()
    return raw.strip()


_TEMPLATE_PREFIXES = (
    "analyse requirements and design the approach",
    "analyze requirements and design the approach",
    "implement:",
    "test the implementation and fix errors",
    "review quality",
    "verify the results against the original objective",
    "research:",
    "analyse and compare findings",
    "analyze and compare findings",
    "compile the research report",
    "read and extract the document content",
)

_DESKTOP_VERBS = ("create", "copy", "move", "rename", "delete", "download")
_DESKTOP_TARGETS = ("file", "folder", "document", "directory", ".txt", ".pdf", ".docx", ".csv", ".md", ".json", ".xlsx")


def classify_desktop_action(text) -> bool:
    """Conservative heuristic: does this step need the desktop executor?

    The check is based on the first line of the step text (the step
    title when the Chief's runner augments the prompt with history),
    so prior-step history cannot trigger a false desktop routing.

    Positive: imperative open/launch/start at the start; Bangla open
    forms; create/copy/move/rename/delete/download plus a file/folder/
    document target; click / press-shortcut / type-into; taking a
    screenshot; searching files.

    Negative (must stay False): the Planner's fixed template steps,
    generic reasoning / research / coding prompts. This heuristic is
    deliberately conservative — a step it misses is answered by the
    assigned agent via the model without desktop tools, which is the
    safe failure mode. It is not a general intent classifier.
    """
    first = _first_line(text)
    if not first:
        return False
    low = " ".join(first.lower().split())
    if any(low.startswith(prefix) for prefix in _TEMPLATE_PREFIXES):
        return False
    # Bangla / romanised open forms (anywhere in the first line).
    if any(form in first for form in ("খোলো", "খুলে দাও", "চালু করো", "চালু করুন")):
        return True
    if any(form in low for form in ("kholo", "khule dao", "chalu koro", "chalu korun")):
        return True
    # Screenshot.
    if "screenshot" in low and ("take" in low or "capture" in low):
        return True
    # Search files.
    if "search files" in low:
        return True
    # Imperative open / launch / start / run at the start.
    if re.match(r"^(open|launch|start|run)\b", low):
        return True
    # Click / press shortcut / type into.
    if low.startswith("click ") or "click on " in low or "click the " in low:
        return True
    if "press" in low and ("shortcut" in low or low.startswith("press ")):
        return True
    if "type into" in low:
        return True
    # Verb + file/folder/document target.
    has_verb = any(re.search(rf"\b{verb}\b", low) for verb in _DESKTOP_VERBS)
    if has_verb and any(target in low for target in _DESKTOP_TARGETS):
        return True
    return False


def sensitive_category(text) -> Optional[str]:
    """Return a ConfirmationGate category for a step, or None.

    Conservative keyword matching over the first line of the step.
    """
    first = _first_line(text)
    low = " ".join(first.lower().split())
    if not low:
        return None
    if "recycle bin" in low or "recycle-bin" in low or re.search(r"\b(delete|remove|erase|wipe|format)\b", low):
        return "destructive"
    if "account settings" in low or ("change" in low and "password" in low):
        return "account_change"
    if re.search(r"\b(pay|payment|purchase|buy|checkout)\b", low):
        return "financial"
    if re.search(r"\bsend\b.*\b(email|message)\b", low) or re.search(r"\bpublish\b", low) or "post publicly" in low:
        return "publishing"
    return None


@dataclass
class DesktopOutcome:
    ok: bool
    text: str


class BridgedAgent(Agent):
    """Agent that routes desktop-action steps to the desktop executor.

    This is the delegation boundary — the single place a Chief step
    becomes a real desktop action — so it is also where Take Control
    is enforced. With Take Control off (the default), only sensitive
    steps (see :func:`sensitive_category`) go through the
    ConfirmationGate, exactly as before. With Take Control on, EVERY
    desktop step goes through the gate: sensitive steps keep their
    own category, the rest are checked under
    ``TAKE_CONTROL_CATEGORY``. The gate stays fail-closed in both
    modes — no gate, or a confirmer that does not approve, means the
    executor is never called. Take Control can only ever ADD
    approvals; it never bypasses or weakens an existing one.
    """

    def __init__(self, spec, orchestrator=None, *, desktop_execute=None, gate=None,
                 take_control=False):
        super().__init__(spec, orchestrator)
        self.desktop_execute = desktop_execute
        self.gate = gate
        # Bool or zero-arg callable returning bool (see
        # _take_control_active); the app passes a callable over its
        # live settings so a mid-task toggle applies to the next step.
        self.take_control = take_control

    def run(self, task: str, context: Optional[dict] = None) -> AgentResult:
        context = context or {}
        if "document_text" in context:
            return super().run(task, context)
        if self.desktop_execute is not None and classify_desktop_action(task):
            category = sensitive_category(task)
            if category is None and _take_control_active(self.take_control):
                category = TAKE_CONTROL_CATEGORY
            if category:
                if self.gate is None:
                    if category == TAKE_CONTROL_CATEGORY:
                        reason = ("desktop step requires explicit approval under "
                                  "Take Control, but no confirmation gate is configured")
                    else:
                        reason = f"sensitive desktop step ({category}) requires a confirmation gate"
                    return AgentResult(self.name, False, "", blocked_reason=reason)
                try:
                    self.gate.check(task, category)
                except ConfirmationRequired as exc:
                    return AgentResult(self.name, False, "", blocked_reason=str(exc))
            try:
                outcome = self.desktop_execute(task)
            except Exception as exc:
                return AgentResult(self.name, False, "", blocked_reason=str(exc))
            if isinstance(outcome, DesktopOutcome):
                if outcome.ok:
                    return AgentResult(self.name, True, outcome.text)
                return AgentResult(self.name, False, "", blocked_reason=outcome.text or "desktop execution failed")
            if isinstance(outcome, str):
                return AgentResult(self.name, True, outcome)
            if outcome is None:
                return AgentResult(self.name, True, "")
            # Unknown return type: treat its text form as a success result.
            return AgentResult(self.name, True, str(outcome))
        return super().run(task, context)


def build_app_agents(orchestrator, *, desktop_execute=None, gate=None, take_control=False) -> dict:
    return {spec.name: BridgedAgent(spec, orchestrator, desktop_execute=desktop_execute, gate=gate,
                                    take_control=take_control)
            for spec in AGENT_SPECS}


def build_app_orchestrator(settings, api_key, *, budget=None, health=None, transport=None, env=None,
                           include_env_providers=True) -> Orchestrator:
    clients = build_clients(env=env, transport=transport) if include_env_providers else {}
    base_url = str((settings or {}).get("api_base_url", "") or "")
    model = str((settings or {}).get("model", "") or "")
    client, info = build_app_client(base_url, model, api_key or None, transport=transport)
    clients[APP_PROVIDER_NAME] = client
    return Orchestrator(clients=clients, budget=budget, health=health, extra_models=(info,))


class AppChiefIntelligenceAgent(ChiefIntelligenceAgent):
    def __init__(self, *args, on_step=None, is_cancelled=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_step = on_step
        self.is_cancelled = is_cancelled

    def _emit(self, step, phase):
        if self.on_step is None:
            return
        try:
            self.on_step(step, phase)
        except Exception:
            pass

    def _runner(self, step, context):
        if self.is_cancelled is not None and self.is_cancelled():
            raise RuntimeError("cancelled by user")
        self._emit(step, "running")
        try:
            result = super()._runner(step, context)
        except Exception:
            self._emit(step, "failed")
            raise
        self._emit(step, "completed")
        return result


def build_app_chief(settings, api_key, *, data_dir=None, queue=None, budget=None, audit_log=None,
                    lesson_log=None, desktop_execute=None, confirmer=None, on_step=None,
                    is_cancelled=None, transport=None, env=None, take_control=False) -> AppChiefIntelligenceAgent:
    if data_dir is not None:
        data_path = Path(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)
        if queue is None:
            queue = TaskQueue(data_path / "task_queue.db")
        if budget is None:
            budget = BudgetTracker(path=data_path / "budget.json")
        if audit_log is None:
            audit_log = AuditLog(data_path / "audit.jsonl")
        if lesson_log is None:
            from .memory import LessonLog
            lesson_log = LessonLog(data_path / "lessons.json")
    orchestrator = build_app_orchestrator(settings, api_key, budget=budget, transport=transport, env=env)
    gate = ConfirmationGate(confirmer=confirmer, audit=audit_log)
    agents = build_app_agents(orchestrator, desktop_execute=desktop_execute, gate=gate,
                              take_control=take_control)
    return AppChiefIntelligenceAgent(orchestrator=orchestrator, agents=agents,
                                     lesson_log=lesson_log, audit_log=audit_log,
                                     on_step=on_step, is_cancelled=is_cancelled)


def format_brain_result(result) -> str:
    status = str(result.get("status", "") if isinstance(result, dict) else "")
    summary = str(result.get("summary", "") if isinstance(result, dict) else "")
    lines = [f"Autonomous Brain — {status}"]
    if summary:
        lines.append(summary)
    steps = result.get("steps", []) if isinstance(result, dict) else []
    for step in steps:
        mark = "✓" if step.get("status") == "completed" else "✗"
        line = f"{mark} [{step.get('agent', '')}] {step.get('title', '')}"
        excerpt = ""
        if step.get("result"):
            excerpt = str(step["result"])[:300]
        elif step.get("error"):
            excerpt = str(step["error"])[:300]
        if excerpt:
            line += f" — {excerpt}"
        lines.append(line)
    blockers = result.get("blockers", []) if isinstance(result, dict) else []
    if blockers:
        lines.append("Blockers:")
        for blocker in blockers:
            lines.append(f"- {blocker}")
    text = "\n".join(lines)
    return text[:8000]


def map_brain_status(status) -> str:
    if status == "completed":
        return "completed"
    if status == "partial":
        return "partial"
    return "failed"


def run_brain_command(chief, command, *, queue=None, is_cancelled=None) -> dict:
    qid = None
    if queue is not None:
        qid = queue.enqueue("[Brain] " + command, payload={"source": "desktop_app"})
        queue.start(qid)
    result = chief.execute_command(command)
    if queue is not None and qid is not None:
        if is_cancelled is not None and is_cancelled():
            queue.cancel(qid)
        elif result.get("status") == "completed":
            queue.finish(qid, "completed", result=format_brain_result(result))
        elif result.get("status") == "partial":
            # The queue STATUSES set has no "partial" — record the run as
            # completed in the queue and carry the true status in the
            # formatted result text.
            queue.finish(qid, "completed", result=format_brain_result(result))
        else:
            blockers = result.get("blockers") or []
            queue.finish(qid, "failed", error="; ".join(blockers) or str(result.get("status", "")))
    return result
