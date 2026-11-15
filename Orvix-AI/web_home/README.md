# ORVIX WebView Home (`web_home/`)

The ORVIX desktop app's home page, rendered inside a WebView window by the
desktop app. Plain HTML, CSS and JavaScript — no build step.

## Provenance

Owner-supplied homepage package ("ORVIX Homepage"), vendored into the repo
and fixed to be an honest bridge client: baked-in demo values (meter
percentages, version string, model status lines, fake chart animation)
were removed, the character set was aligned with the app's real profiles
(LUNA, RUBY, MAYA), and `app.js` was rewritten from a random-data demo
into a client of the desktop bridge described below.

## Bridge contract

JavaScript → Python — the desktop app exposes one of
`window.orvixBridge` or `window.pywebview.api` (pywebview) with:

- `sendCommand(text)` — run a typed/spoken command through the app
- `runAction(action)` — quick actions: `chrome`, `youtube`, `notepad`,
  `calculator`, `files`, `power`
- `selectCharacter(name)` — e.g. `"MAYA"` (the app lowercases it)
- `startListening()` — start the app's speech engine
- `setWakeWord(enabled)` — wake-word toggle
- `openConsole()` — open Settings in the ORVIX console window

Python → JavaScript — the app pushes live state by evaluating:

- `window.orvixPushState(state)` — one JSON object:
  `{ version, stats: { cpu, ram, storage, osName, machine, uptime },
     model: { name, endpoint, statusLines: [{ label, value, state }] },
     core: { state, mode }, character, wakeEnabled }`
  (`state` in a status line is `true` = ok, `false` = problem,
  `null` = not checked; `character` is a lowercase key such as `"maya"`;
  `core.state` drives the reactor tint: idle, listening, thinking,
  processing, executing, speaking, completed, error.)
- `window.orvixPushMessages(list)` — chat history/exchange entries:
  `[{ kind: "user" | "assistant" | "system", text, speaker? }]`

## Honesty rules

- No random or demo data. Meters, the performance strip, model status,
  OS info and uptime render only values pushed by the bridge; anything
  unknown shows `—` / "Not checked" / "Not configured".
- Standalone (opened without the desktop bridge) the page is a
  not-connected demo: it says so, commands and quick actions explain that
  the desktop bridge is required, character/voice choices are labelled
  as not saved, and no assistant answers are invented.
- The page sends nothing anywhere itself and holds no API keys;
  privileged work stays in the desktop app.

## Notes

- `style.css` starts with a Google Fonts `@import`. Inside the WebView
  with no network it simply degrades to the system font stack
  (Inter → Arial/sans-serif, Rajdhani → sans-serif); layout is unaffected.
- Character portraits intentionally use styled initial-letter art
  (L / R / M tiles) — no external image files are referenced.
