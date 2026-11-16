# ORVIX Floating AI Core

The animated living energy core — ORVIX AI's visual identity — plus the
8-state machine that drives it from **real application events**. Built
for the existing Python/Tkinter desktop app (Part 2 of the Floating
Core spec). No static image is used as the core; every frame is drawn
procedurally on a Tkinter Canvas.

## Files

| File | Role |
|---|---|
| `orvix/core_states.py` | Pure state machine: 8 states, per-state animation parameters, audio-level smoothing, FPS throttle rules. No tkinter import — fully unit-testable headless. |
| `orvix/floating_core.py` | `CoreModel` (pure geometry → draw primitives) + `FloatingCoreWidget` (thin Canvas shell, capped `after()` loop, redraws only its own tagged items). |
| `tests/test_orvix_floating_core.py` | 24 headless tests: transitions, COMPLETED auto-return, sticky ERROR, level math, throttle, geometry bounds, widget behaviour against a fake canvas. |
| `app.py` | Integration: hero core + floating-HUD core, event bridge (`_set_core_state`). |

## Visual design (from the owner's references)

Concentric rotating rings (4 layers, alternating directions) in
electric blue / cyan / turquoise with subtle purple accents, glow
ladders (dark→bright concentric strokes — Tkinter has no alpha, so
volumetric glow is simulated with colour steps), 26 orbiting
particles, a fluid wave ring whose radius is modulated by travelling
sines, an amber inner arc and a dark transparent centre (from the
reference video's teal-ring/amber-centre core), on near-black
(`#050b17` / hero `#061020` / HUD `#07111f`).

## The 8 states

| State | Look | Trigger (real event, app.py) |
|---|---|---|
| IDLE | Slow breathing (12 FPS), subtle particles | App ready; `_set_busy(False, "● READY")` via `_core_state_for_status` (app.py:2178) |
| LISTENING | Cyan, level-reactive pulse | `_listen()` → `_set_busy(True, "● LISTENING…")` |
| THINKING | Fast counter-rotating rings, purple accent | `_launch_task()` → `_set_busy(True, "● THINKING")` |
| PROCESSING | Bright, fast pulse | Plan-step progress, cancellation (`_cancel_current_task`, app.py:1131), voice received |
| EXECUTING | Turquoise, full particle field, strongest wave | `_show_task_progress()` — real tool/plan-step callbacks from the task executor (app.py:1973) |
| SPEAKING | Cyan, level-reactive | `_speak_safely()` when playback actually starts (app.py:2129); returns to IDLE via `_core_after_speech` when playback ends |
| COMPLETED | Bright turquoise burst, then auto-returns to IDLE after 2.2 s | `_finish_response()` with task status `completed`/`partial` (app.py:2114) |
| ERROR | Red/amber warning pulse, sticky until the next interaction | `_finish_response()` with status `failed` (app.py:2112); `_voice_failed()` (app.py:2169) |

Both the hero core (app.py:743, placed over the hero panel; the old
static avatar-rig circle drawing was removed from `_render_core`) and
the floating-HUD core (app.py:152, on the HUD's existing canvas) are
driven by the same bridge, so the floating window and the main window
always show the same backend state — the same intelligence the chat,
planner and task executor run on.

## Audio level — honest status

`FloatingCoreWidget.set_level_provider()` is wired in app.py to
`OrvixDesktop._core_audio_level()`. The existing `SpeechEngine`
exposes **no live level**: `speech_recognition` reports no microphone
RMS during capture and pygame reports no output level during
playback. The provider therefore returns `None` today, and
LISTENING/SPEAKING use their state-driven pulse — an animation
parameter, **not** fabricated audio data. If a level-capable engine
(RMS tap on the capture stream / playback meter) is added later, it
only has to be returned from `_core_audio_level()`; the smoothing and
modulation math is already in place and tested.

## Floating window behaviour

Unchanged from the existing HUD, which the core now lives inside:
frameless (`overrideredirect`), always-on-top toggle, drag-to-move,
mini/expanded modes, resize grip, opacity, right-click menu, and
position/size/opacity persisted through the existing settings store
(`floating_hud_*` keys in `orvix/settings.py`). Hotkey Ctrl+Shift+J
shows/hides it. The main window keeps its existing navigation and
features; the mic control remains under the hero core in the composer
(TALK button), and the HUD keeps its MIC / TALK button.

## Performance

- State-capped FPS: IDLE 12, ERROR 15, THINKING/COMPLETED 24,
  active states 30 (`frame_interval_ms` in `core_states.py`).
- The widget pauses entirely while its canvas is not viewable or its
  window is minimised/iconified (600 ms re-check, no drawing).
- Redraws delete/recreate only items tagged `orvix_core` (the HUD
  status dot and other canvas items are untouched), geometry is
  precomputed per size, and frame `dt` is clamped so a stalled UI
  cannot make the animation jump.

## App → Chief brain wiring: deliberately NOT done in this unit

Routing home-screen commands through the Super Intelligence Chief
(`orvix/agents.py`) was assessed and skipped, for a concrete reason:
the desktop app's provider configuration lives in its own settings +
OS keyring and speaks to one OpenAI-compatible endpoint
(`orvix/agent.py`), while the Chief's `Orchestrator` resolves
providers from environment variables against its own registry
(`orvix/router.py`). There is no supported way to inject the app's
configured endpoint/key into the Orchestrator, and the Chief's agents
do not have the desktop toolset (28 automation tools, planner,
approval gates) that the app agent has. Bolting them together here
would either duplicate provider configuration or silently change
chat behaviour. The safe bridge is a small follow-up: construct the
Orchestrator from the app settings (base URL + keyring key) behind
the existing "provider configured" check, with fallback to the app
agent when unconfigured. The core's states already reflect whichever
backend runs the task, because they are driven by the task/voice
lifecycle, not by a specific engine.

## Small overlay orb (mini mode)

The floating window's **mini mode is the small overlay orb**: a
104×128 px frameless, always-on-top bubble that can be dragged to any
position on the screen (position persists through the existing
`floating_hud_*` settings). It renders the *same* `FloatingCoreWidget`
at 88 px with the *same* 8 states — no duplicated animation logic
(`orvix/overlay.py` holds only the pure geometry/interaction math).

Interactions:

- **Single click** (a press/release that barely moves — drags are
  discriminated by travel distance) toggles a compact **command
  palette** under the orb: a text entry, a ➤ submit button and a 🎙
  mic button. The window grows downward to fit it, shifting up near
  the screen bottom so the palette stays visible. `Esc` closes it.
- **Submitting** a palette command routes through
  `OrvixDesktop._send_text` — the exact entry point of the main
  window's SEND button — so commands execute with the app's existing
  tools, task queueing and approval gates. The workspace is then
  raised, matching the floating MIC flow, so progress and any
  approval prompts are visible.
- **Double-click** opens/raises the main window (it cancels the
  pending single-click toggle first).
- **Right-click menu**: Open ORVIX, Command Palette, Switch
  Mini/Expanded, Pin on top, HUD Opacity, Hide Floating HUD,
  Quit Overlay. `Ctrl+Shift+J` summons/hides the overlay as before.

Scope honesty: the overlay floats over other **desktop** applications
— that is what an always-on-top desktop window does. It does not
embed inside other applications or websites, and nothing in the UI or
docs claims that. Web/app control happens because palette commands
run through the app's existing browser/executor tools.

Default behaviour follows the existing floating window: the HUD is
shown at startup, and mini (overlay) mode is entered via the MINI
button / context menu and then persisted (`floating_hud_mini`). The
overlay is not forced on users who have not chosen it.

## HUD home page

The home view follows the HUD layout language of the owner's
reference (top status bar, data-panel columns flanking the central
core, bottom status strip) in the app's own dark navy/cyan style:

- **Top bar** (existing): ORVIX wordmark, live clock/date, model
  status, wake-word and floating-HUD controls.
- **Left column**: System overview (CPU/memory from psutil, with an
  honest "MONITOR N/A" fallback) and the new **Control Center**
  panel — agents registered, providers configured, provider health,
  task counts, budget and memory/lessons counts, plus an execution
  log tail. Every value comes from the real Control Center snapshot
  (`orvix/control_center.build_snapshot` over the shared `~/.orvix`
  state — the same task queue/budget/audit files the CLI and Chief
  use) combined with the desktop app's own `TaskStore` records,
  assembled by the pure module `orvix/hud_data.py`. Anything missing
  or never measured renders as a dash or an explicit "NOT CHECKED /
  NO AUDIT EVENTS RECORDED" line — never an invented number.
- **Centre**: the hero Floating AI Core, conversation and composer
  (unchanged).
- **Right column**: quick actions, active tasks (now including the
  most recent task outcomes from the real task records), AI model
  status and permissions (all pre-existing).
- **Bottom strip**: the footer now reflects the live core mode
  (READY / THINKING / EXECUTING / …).

Panels refresh on a 2 s throttled timer that pauses while the main
window is minimised or not viewable, and immediately whenever the
core state changes (via the `_set_core_state` bridge).

## What remains / honest limits

- The full app *was* run under Xvfb on Linux for the overlay/HUD
  unit (with a capture-only cursor-name shim — the app targets
  Windows Tk cursor names) and the home view, the overlay orb in
  EXECUTING (palette open) and LISTENING states were captured and
  inspected. Still outstanding: one visual check on the owner's real
  Windows desktop, where fonts, emoji buttons and true always-on-top
  stacking behave natively. Under bare Xvfb there is no window
  manager, so `-topmost` stacking above the main window could not be
  exercised — the overlay had to be parked beside the main window
  for its captures; on Windows the topmost flag is honoured by the
  OS window manager.
- Continuous listening / VAD / barge-in (spec Part 8) is a separate
  unit; the existing one-shot + wake-word voice flow is what drives
  LISTENING/SPEAKING today.
- A WebGL/Three.js core would require a web shell; the desktop main
  window has none, so Canvas rendering is the efficient correct
  choice here. A browser build could not stay above other
  applications anyway — always-on-top is a desktop-only capability
  and is provided by the HUD.
