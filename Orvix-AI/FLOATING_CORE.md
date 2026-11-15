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

## Audio level — real since the continuous-voice unit

`FloatingCoreWidget.set_level_provider()` is wired in app.py to
`OrvixDesktop._core_audio_level()`, which now returns
`SpeechEngine.current_level()` — real measured audio, not an
animation parameter:

- **LISTENING** — per-chunk RMS (normalised 0..1, smoothed by
  `orvix/audio_levels.LevelMeter`) computed from the *same* frames
  the recogniser consumes: on the VAD capture path the frames are
  read by the engine itself; on the classic path a read tap wraps
  the `sr.Microphone` stream. No second microphone stream is opened.
  The meter goes stale (returns `None`) 0.4 s after the last frame,
  so an idle core never shows a frozen, fabricated level.
- **SPEAKING** — the synthesised MP3 is decoded once before playback
  (`pygame.mixer.Sound` + `pygame.sndarray`) into a per-100 ms
  `PlaybackEnvelope`; the level is the real loudness of the reply at
  the current playback position. pygame has no output meter — this
  is the honest substitute, derived from the actual audio file.

What is still **not** real, deliberately: if the envelope cannot be
decoded (codec/numpy unavailable), SPEAKING falls back to the
state-driven pulse and `current_level()` returns `None` rather than
inventing a level. The pyttsx3 offline fallback voice has no audio
file and no meter at all — its SPEAKING state is state-driven.

## Continuous voice — VAD + barge-in

Settings (Settings dialog, both default ON, both fail-safe —
any failure degrades to the previous behaviour and never breaks the
voice loop): `voice_vad_enabled`, `voice_barge_in`.

- **VAD segmentation** (`orvix/vad.py`): the engine captures through
  its own PyAudio stream; an energy VAD with an adaptive noise floor
  starts an utterance after 90 ms of sustained above-threshold level
  and ends it after 700 ms of silence (12 s cap, 300 ms pre-roll so
  the first syllable is not clipped, <250 ms of voiced audio is
  discarded as a blip). The captured bytes are handed to the same
  Google recogniser as `sr.AudioData`. If the capture itself fails
  before producing an utterance (device busy/missing, stream error),
  `listen_once` automatically falls back to the classic
  `recognizer.listen` path. The wake-word loop uses the same
  `listen_once`, so it gets VAD segmentation too. One library
  boundary remains: recognition itself is still the recogniser's
  blocking network call — VAD controls *capture*, not recognition.
- **Barge-in** (`orvix/barge_in.py`, neural voice only): while
  pygame plays a reply, a monitor thread watches the microphone.
  The first 300 ms are a guard window whose loudest RMS becomes the
  playback-leakage baseline; afterwards the user must sustain a
  level above `max(0.16 RMS, baseline × 1.8)` for 300 ms. On trigger
  the engine stops playback and the app moves SPEAKING → LISTENING
  (`_barge_in_listen`). There is **no echo cancellation**: with loud
  speakers in a small room the baseline can rise until only a close,
  loud interruption registers — headphones make barge-in reliable.
  An interruption that begins inside the guard window also raises
  the baseline; speak after the first fraction of a second. The
  pyttsx3 fallback is a blocking call with no stoppable playback
  handle, so barge-in does not exist on that path.

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

## App → Chief brain wiring

Floating Core + Voice + Autonomous Brain now share ONE backend
intelligence. The bridge lives in `orvix/app_bridge.py` (no tkinter
import, fully unit-tested headless); `app.py` only calls it.

- **Single config source**: `build_app_client` in
  `orvix/providers.py` builds a provider client from the app settings
  (base URL + model) plus the keyring key, injected into the
  Orchestrator as the extra `app` provider model. No env duplication;
  the key is never logged or written. Env-configured registry
  providers remain available alongside it.
- **Opt-in toggle, default OFF**: Settings → "Autonomous Brain
  (multi-agent) for complex commands" (`autonomous_brain` in
  `orvix/settings.py` DEFAULTS, default `False`). Routing rule:
  `route_decision` sends a command to the Brain only when the toggle
  is on AND the existing `should_generate_task_plan` classifier says
  it is complex. Simple commands are unchanged, and voice commands
  route through the same point (`_voice_captured` → `_send_text` →
  `_launch_task` → `_work`).
- **Step surfacing**: Brain steps are reported through the existing
  `_record_task_step` (activity feed + task progress), so the core
  states follow the existing lifecycle (EXECUTING while steps run,
  COMPLETED / ERROR from the mapped Brain status in
  `_finish_response`).
- **Tool bridge**: `BridgedAgent` routes a step by kind — desktop-
  action steps (conservative `classify_desktop_action` heuristic) go
  to `OrvixAgent.respond` with all its approval gates intact;
  sensitive steps are pre-checked by `ConfirmationGate` fail-closed
  (no gate / no approval = blocked, the desktop executor is never
  called); document steps (`document_text` context) go to the
  layer's documents tools via the base agent; research / chat /
  reasoning / coding / verification steps go to the Orchestrator
  (app endpoint plus any env providers).
- **Honest degradation**: when the Brain is on but not configured
  (`brain_unconfigured` — empty endpoint/model, key-over-plain-HTTP
  to a remote endpoint, or a remote endpoint with no key), `_work`
  posts a SYSTEM note naming the reason and handles the command with
  the normal assistant instead. No fake Brain responses.
- **Budget**: the shared `BudgetTracker` over `~/.orvix/budget.json`
  counts app-endpoint calls at $0 estimated cost — a custom
  endpoint's pricing is unknown, so dollar estimates exist only for
  registry models with published prices. The `ORVIX_BUDGET_USD` cap
  still applies to priced calls.
- **Shared state**: the Brain uses the same TaskQueue / AuditLog /
  LessonLog / Budget files as the CLI and the HUD panel (via the
  extracted `_shared_orvix_state()` in app.py), so
  `python -m orvix status` and the HUD Control Center panel reflect
  app Brain tasks. The queue has no "partial" status, so partial
  runs are recorded as completed in the queue with the true status
  carried in the formatted result text.
- **Honest limits**: (1) No mid-call cancellation of an in-flight
  provider request — cancellation is checked between steps, the
  same cooperative model as the existing agent. (2) The desktop
  classifier is a conservative heuristic — a step it misses is
  answered by the assigned agent via the model without desktop
  tools. (3) The app endpoint is one OpenAI-compatible endpoint —
  non-OpenAI-shaped endpoints are not adapted.

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

## Home page redesign (owner's final mockup)

The home view was rebuilt to the owner's approved mockup layout
(top nav pills, circular gauges, centred core with logotype, quick
action icon grid, bottom command bar). All data stays real, assembled
by the pure module `orvix/home_data.py`:

- **Top bar**: brand + real `APP_VERSION`; nav pills Home / Tools /
  Settings / About wired to existing entry points (composer focus,
  `_quick_actions`, `_open_settings`, an About dialog with the real
  version); endpoint pill reports only the configured endpoint kind;
  live clock (unchanged).
- **System Overview**: three Canvas arc gauges (CPU / RAM / Storage)
  fed by `collect_system_stats` (psutil + `shutil.disk_usage`); the
  LIVE indicator degrades to PARTIAL / N/A with the counters that are
  actually readable. Windows Info strip shows real platform name /
  edition, build and uptime (`psutil.boot_time`), dashes when a source
  is unavailable. The Control Center panel from the HUD unit is
  unchanged below it.
- **Characters**: the three existing profiles as portrait cards
  (name, role, selection dot) calling the existing `_select_character`;
  portraits load defensively — the reference JPGs are found in the
  app folder or in any sibling folder that still holds them, with the
  styled initial-letter card as fallback.
- **Centre**: the hero Floating AI Core is larger and centred, with
  the ORVIX logotype and "Speak • Command • Get Results" chip beneath
  it; the transcript panel stays directly below (the mockup has no
  transcript, but the conversation is core functionality and is not
  removed). Core state wiring is untouched.
- **Quick Actions**: 2×3 icon grid (glyphs drawn with Canvas
  primitives, no icon assets); tiles route the same command strings
  as the old list buttons through `_send_text`. Downloads and System
  Information remain as compact buttons under the grid.
- **AI Model Status**: the configured model/endpoint (unchanged) plus
  status lines from `model_status_lines` — ENDPOINT (configuration),
  INTERNET ("Not checked"; the home page never probes the network),
  AI CORE (live core mode), VOICE (real wake-word flag), TTS
  (installed engine modules).
- **Bottom command bar**: Voice Control (waveform strip, "Hey ORVIX"
  wake-word pill, the existing wake-word toggle), the composer with a
  big circular mic button and an "Ask me anything..." placeholder,
  and Live Performance bars drawn from a rolling history of the real
  CPU samples the gauge poll already takes (N/A until a readable
  sample exists).
- **Footer**: "Built with ❤ for a Smarter Tomorrow", the live core
  mode + "Engineering Precision. Digital Innovation.", and a GitHub
  link to the real repository.

Layout note: the pack order in `_build_ui` is deliberate — top bar,
footer and bottom bar are packed before the expanding body so the
fixed strips always get their height. At the default 900 px window
height the rails are dense: the lowest panels (Available tools on the
left, AI Model Status / Permissions on the right) come fully into
view as the window is maximised, the same packing behaviour the app
has always had.

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
