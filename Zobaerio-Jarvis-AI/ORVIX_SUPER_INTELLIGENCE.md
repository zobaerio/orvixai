# ORVIX AI — Autonomous Super Intelligence Layer

Added 2026-11-15 on top of the existing desktop assistant (nothing replaced;
all pre-existing features and their 314 tests are unchanged). New code lives
in `jarvis/orvix/` and uses only the Python standard library.

## Architecture (Brain Architecture from the spec)

USER COMMAND → Chief Intelligence Agent → Planner (decompose) → Router
(selects provider/model per step) → Specialised Agents → Execution Engine
(one alternative-approach retry per failed step) → Verification step →
Final report (completed / partial / blocked with exact blockers — never a
false "100% complete").

## Modules

| Module | What it really does |
|---|---|
| `providers.py` | Real API adapters for OpenAI, Anthropic, Google Gemini, DeepSeek, xAI, Mistral, OpenRouter and local Ollama. Keys come from environment variables only (see `.env.example`). Unconfigured providers raise an honest error naming the variable to set. Extensible registry: add a `ProviderSpec` + models, no redesign. |
| `router.py` | Task classification + complexity scoring, capability/cost/latency/health-aware model ranking, persistent budget tracker (hard cap via `ORVIX_BUDGET_USD`, never silently exceeded), provider health monitor, Orchestrator with retry + cross-provider fallback + cross-model verification for complex tasks + collaboration reviews. |
| `brain.py` | Deterministic planner/decomposer and the execution engine with verification and honest blocker reporting. |
| `agents.py` | Chief Intelligence Agent + 15 specialised agents (Research, Coding, Software Engineering, Browser Automation, Computer Use, File Management, Data Analysis, Business Intelligence, Productivity, Communication, Security, QA, System Administration, Workflow Automation, Personal Assistant), each with defined responsibilities/tools, all reporting to the Chief. |
| `taskqueue.py` | Persistent SQLite background queue: priorities, checkpoints, pause/resume/cancel/retry, crash recovery (interrupted tasks return to pending with checkpoints), threaded worker. |
| `memory.py` | User-controlled persistent memory (list/delete/clear at any time, bounded, atomic writes) + post-task lesson log that records outcomes and reuses successful strategies. Lessons never modify code, security policy or permissions. |
| `documents.py` | Real text extraction for TXT/MD/code/JSON, CSV (+ numeric column stats), XLSX and DOCX (ZIP+XML), and PDFs with a text layer; extractive summarisation, sentence-retrieval QA, document comparison. Scanned PDFs honestly report that OCR is unavailable. |
| `research.py` | Bounded web fetching + DuckDuckGo HTML search; findings are labelled `corroborated` (>= 2 sources) or `single-source / uncertain`. |
| `security.py` | Append-only redacted audit log, fail-closed confirmation gate for destructive/financial/publishing/account actions, sliding-window rate limiter, env-only secret access. |
| `control_center.py` | Real-state snapshot (agents, models + configured/health, queue stats, budget, audit tail) and a standalone dark dashboard HTML page. Unconfigured items are shown as "not configured", never as fake activity. |

## Usage

```
python -m jarvis.orvix status
python -m jarvis.orvix dashboard control_center.html
python -m jarvis.orvix ask "Research vector databases and compare the top options"
```

Set at least one provider key first (`.env.example` lists them), or run a
local Ollama server. Without any provider, commands report `blocked` with
the exact missing configuration instead of pretending to work.

## Honest limitations

- Live provider calls cannot be verified without real API keys; adapters are
  unit-tested at the HTTP boundary with clearly-labelled test doubles and
  follow each provider's documented public API shape.
- Voice (continuous/VAD/barge-in), a Tkinter Control Center panel, and
  signed installers remain future units; the Control Center ships as a
  data layer + standalone HTML dashboard generated from real state.
- Computer-use / browser agents delegate to the app's existing
  approval-gated automation tools; they do not bypass any consent dialog.
