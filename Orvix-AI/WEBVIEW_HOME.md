# ORVIX WebView Home

## What it is

The WebView Home is the owner-supplied HTML homepage (`web_home/`) hosted in a
[pywebview](https://pywebview.flowrl.com/) window as the app's home screen at
startup.

The Tk window remains the engine — voice, wake word, floating HUD / overlay,
executor, and the Chief (Autonomous Brain) all live there exactly as before.
While the web home is open the Tk root window is withdrawn (`root.withdraw()`),
not destroyed. Closing the web home returns to the Tk console, which has kept
running underneath the whole time.

This is a presentation layer only. No command, tool, approval gate, or brain
routing changes because the HTML home is open.

Integration points in `app.py`:

- `OrvixDesktop._maybe_start_webview_home()` — called from `run()` before
  `mainloop()`; starts the web home only when enabled and available.
- `OrvixDesktop._write_message()` — the single funnel for all transcript
  output also mirrors every message to the web home via
  `WebViewHome.push_message(speaker, text, kind)`.
- `OrvixDesktop._exit_application()` — stops the web home
  (`self._web_home.stop()`) before `root.destroy()`.
- Module: `orvix/webview_home.py` exposes `WebViewHome`,
  `webview_available()`, and `webview_home_enabled(settings)`.

## Bridge contract

### JavaScript → Python — `window.pywebview.api`

| JS call | Python dispatch | Notes |
| --- | --- | --- |
| `sendCommand(text)` | `app._send_text` via the thread-safe `app.events` queue | Same pipeline as the Tk composer entry. |
| `runAction(action)` | Same quick-action command strings as the Tk tiles, via `_send_text` | No separate action implementation in the page. |
| `selectCharacter(name)` | `app._select_character` | Same character profiles as the Tk home. |
| `startListening()` | `app._listen` | Same mic / speech path as the Tk mic button. |
| `setWakeWord(bool)` | Wake-word toggle | Same wake-word engine as the Tk top bar. |
| `openConsole()` | Raise the Tk window | Brings the withdrawn Tk console back to the front. |
| `stopAll()` / `setTakeControl(bool)` | Emergency Stop / Take Control (Phase 4) | Stop composes the app's existing cancel + stop-speaking + queue-disarm paths; Take Control forces approval on delegated steps. |
| `markNotificationsRead()` / `clearNotifications()` | Notification Center store (Phase 3) | Read/clear act on the app's persistent notification store. |
| `memorySearch(q)` / `memoryForget(id)` / `memoryClear()` | Memory Center (Phase 5) | Search is a view filter; forget/clear go through the same enqueue discipline as every other write. |
| `testProvider(id)` | Models page (Phase 6) | Runs that provider's real connection check in the desktop app. |
| `cancelTask(id)` | Tasks view (Phase 9) | Routes to the app's existing queued/running cancel paths only. |

### Python → JavaScript

| JS function | Cadence / trigger | Payload |
| --- | --- | --- |
| `window.orvixPushState(json)` | ~every 1.5 s, only when changed | Gauges / OS / uptime from `home_data.collect_system_stats`, model + status lines from settings / `home_data.model_status_lines`, core mode / state, active character, wake state, app version. The Command Center phases extended the payload additively: Phase 1 stats (network/disk rates, drives, battery, top processes), Phase 2 Control Center / activity / current-operation blocks, Phase 3 notifications, Phase 5 memory, Phase 6 providers, Phase 7 performance history, Phase 8 approvals + character accent, and Phase 10 `accentPreset` (the Settings > Appearance preset key; the page sets the root `data-accent` attribute from it, separately from the Phase 8 `data-character` ambient tint). Since Phase 10 the model panel's INTERNET status line carries the endpoint probe's verdict (`model.health`) instead of a permanent "Not checked". |
| `window.orvixPushMessages(list)` | On every console message | Mirrors every message that passes through the `_write_message` funnel (`speaker`, `text`, `kind`). |

## Views

The top navigation opens: **Home** (the command-center dashboard: system overview, character cards, voice control, hero reactor, diagnostics, quick actions, active tasks, recent activity, model status, live performance strip), **Tools** and **About** (secondary card views), **Tasks** (task records with progress and cancel), **AI Core** (the eight core states with the live one highlighted, current operation, pending approvals, activity), **Memory** (the Memory Center over the app's MemoryStore), **Models** (the Model Management Center: registry providers, capabilities, per-provider connection tests, desktop-endpoint summary), **Performance** (the Live Performance Center: recorded CPU/RAM/network/disk/own-process series with honest time windows, top processes, temperature/GPU as N/A on Windows), **Voice** (focuses the home Voice Control panel) and **Settings** (opens the desktop Settings dialog via the console). The **Notification Center** opens from the bell (unread badge, categories, mark-all-read, clear). A **persistent command bar** sits at the bottom of the shell on every view — the one composer, its status line, and the Stop control while work is running (Phase 9).

## Honesty rules

- The page never invents data. Every gauge, model name, status line, and
  message shown in the web home comes from the real app state pushed over the
  bridge above.
- Standalone (opened without the bridge — e.g. the HTML file opened directly
  in a browser) the page shows a clearly-labelled **not-connected** demo with
  gauges at `—`. It does not present demo values as live system data.
- If a stat is unavailable it is shown as `—` / not checked, never fabricated.

## Fallback & disabling

- **Setting:** `webview_home` in settings (Settings dialog checkbox
  “WebView Home — open the HTML home window at startup”), default **on**.
  Takes effect next launch.
- **Environment override:** `ORVIX_WEBVIEW_HOME=0` forces the Tk console;
  `ORVIX_WEBVIEW_HOME=1` forces the web home on. The env var wins over the
  setting (`webview_home_enabled(settings)`).
- **pywebview missing or window failure:** `webview_available()` returns
  `(False, reason)` or `WebViewHome.start()` returns `False`, a SYSTEM message
  explains the fallback, and the Tk home opens unchanged — exactly as before
  this feature existed. Any exception in the start path is caught and also
  leaves the Tk home intact.
- The mirror in `_write_message` is wrapped so a web-home failure can never
  break the console transcript.

## Platform notes

- **Windows (target):** pywebview uses the preinstalled Microsoft Edge
  WebView2 runtime — no extra browser install for the user.
  Dependency: `pywebview>=4.4.1` in `requirements.txt`.
- **Threading arrangement:** pywebview's GUI loop runs on a dedicated thread
  in this integration. On Windows this is the standard pywebview + Tkinter
  arrangement (each toolkit keeps its own loop; they communicate only through
  the bridge described below).
- **Linux / GTK:** webview was not the target platform and may require running
  on the main thread. This is a documented limitation — the fallback above
  covers it: if the window cannot start, the Tk home is used.

## Threading rule

- Bridge API calls arrive **off the Tk thread**. They are dispatched **only**
  through `app.events` — the queue that `_drain_events` already polls on the
  Tk thread. No Tk widget is touched from the bridge thread.
- JavaScript evaluation (`orvixPushState` / `orvixPushMessages`) happens only
  on the bridge push thread, never from arbitrary app threads.
