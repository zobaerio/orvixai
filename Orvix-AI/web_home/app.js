// ORVIX WebView home — bridge client.
//
// This page is a thin client of the ORVIX desktop app. All live values
// (system stats, model status, core state, chat messages) arrive from the
// desktop bridge; the page never invents telemetry, model answers or
// action results. Opened standalone (no bridge) it stays in an honest
// not-connected demo state: meters show "—", actions explain that the
// desktop bridge is required, and no fake replies are generated.
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const toast = (message) => {
  const el = $("#toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(() => el.classList.remove("show"), 2800);
};

function updateClock() {
  const now = new Date();
  $("#clockTime").textContent = now.toLocaleTimeString([], {hour12:false});
  $("#clockDate").textContent = now.toLocaleDateString([], {weekday:"short", day:"2-digit", month:"short", year:"numeric"});
}
updateClock(); setInterval(updateClock, 1000);

// Bounded chat growth: the chat DOM keeps at most this many message
// nodes — the same bound as the bridge's message buffer — trimming
// the oldest first. The just-added message is always last, so it is
// never the one trimmed.
const MAX_CHAT_MESSAGES = 200;
function trimChatLog() {
  const log = $("#chatLog");
  if (!log) return;
  let messages = log.querySelectorAll(".message");
  while (messages.length > MAX_CHAT_MESSAGES) {
    messages[0].remove();
    messages = log.querySelectorAll(".message");
  }
}

// Copy a message's text. Uses the async clipboard API when the
// window has access to it; otherwise it says so honestly via a
// toast instead of pretending the copy happened.
function copyMessageText(text) {
  const value = String(text == null ? "" : text);
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    navigator.clipboard.writeText(value).then(
      () => toast("Message copied."),
      () => toast("Copy failed — clipboard unavailable in this window."));
    return;
  }
  toast("Copy unavailable — clipboard is not accessible in this window.");
}

function addMessage(type, text, speaker, time, streamId) {
  const wrap = document.createElement("div");
  wrap.className = type === "user" ? "message user-message" : "message assistant-message";
  if (streamId) wrap.dataset.streamId = String(streamId);
  const head = document.createElement("div");
  head.className = "message-head";
  const label = document.createElement("b");
  label.textContent = speaker || (type === "user" ? "YOU" : type === "system" ? "SYSTEM" : "ORVIX");
  head.appendChild(label);
  // Messages pushed by the bridge carry a "time" field (HH:MM,
  // stamped by the desktop app at creation). Older buffered entries
  // without the field simply render without a timestamp.
  if (time) {
    const stamp = document.createElement("span");
    stamp.className = "message-time";
    stamp.textContent = time;
    head.appendChild(stamp);
  }
  const p = document.createElement("p"); p.textContent = text;
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "copy-button";
  copyButton.textContent = "Copy";
  copyButton.title = "Copy this message";
  // The copy reads the paragraph's CURRENT text at click time, so a
  // streamed message copies its latest (eventually final) text, not
  // the partial text it was created with.
  copyButton.addEventListener("click", () => copyMessageText(p.textContent));
  head.appendChild(copyButton);
  wrap.append(head, p); $("#chatLog").appendChild(wrap);
  trimChatLog();
  $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
  return wrap;
}

// ---------------------------------------------------------------------------
// Streaming messages (Phase 8b): the bridge pushes partial updates for
// ONE in-flight message, identified by a stable id and completed by a
// push with final=true. The existing DOM node is updated in place —
// never a new node per delta — so the message keeps its single
// timestamp and its Copy button ends up copying the final text.
// ---------------------------------------------------------------------------
const streamNodes = new Map();
function upsertStreamMessage(m) {
  const type = m.kind === "user" ? "user" : m.kind === "system" ? "system" : "assistant";
  let wrap = streamNodes.get(m.id);
  if (wrap && wrap.isConnected) {
    const p = wrap.querySelector("p");
    if (p) p.textContent = m.text == null ? "" : String(m.text);
    const log = $("#chatLog");
    if (log) log.scrollTop = log.scrollHeight;
  } else {
    wrap = addMessage(type, m.text, m.speaker, m.time, m.id);
    streamNodes.set(m.id, wrap);
  }
  if (m.final) streamNodes.delete(m.id);
  return wrap;
}

// ---------------------------------------------------------------------------
// Desktop bridge detection.
// The desktop app exposes either window.orvixBridge (preload-style) or the
// pywebview bridge at window.pywebview.api. Python pushes live state back
// through window.orvixPushState / window.orvixPushMessages below.
// ---------------------------------------------------------------------------
let bridgeApi = null; let connected = false; let lastState = null;
function detectBridge(){ if (window.orvixBridge) return window.orvixBridge; if (window.pywebview && window.pywebview.api) return window.pywebview.api; return null; }
function tryConnect(){ const api = detectBridge(); if (api && !connected){ bridgeApi = api; connected = true; setConnectedUI(true); requestPortraits(); } }

// ---------------------------------------------------------------------------
// Character portraits (Phase 11): the bridge hands over the owner's real
// reference images as data URIs (see getCharacterPortraits in the Python
// bridge) because the WebView cannot be relied on to reach the app's
// local asset files. Requested once, after the bridge connects. A card
// whose portrait is absent from the result — or whose image fails to
// load — keeps its styled initial-letter art; nothing is fetched from
// the network and no external artwork is ever used.
// ---------------------------------------------------------------------------
let portraitsRequested = false;
function requestPortraits() {
  if (portraitsRequested || !bridgeApi || typeof bridgeApi.getCharacterPortraits !== "function") return;
  portraitsRequested = true;
  try {
    const result = bridgeApi.getCharacterPortraits();
    if (result && typeof result.then === "function") {
      result.then(applyPortraits).catch(() => {});
    } else {
      applyPortraits(result);
    }
  } catch (_) { /* letter art stays — the honest fallback */ }
}
function applyPortraits(result) {
  const portraits = (result && result.portraits) || {};
  Object.keys(portraits).forEach((key) => {
    const uri = portraits[key];
    if (!uri) return;
    const card = $$(".character").find((item) => item.dataset.character === String(key).toUpperCase());
    if (!card) return;
    const art = card.querySelector(".character-art");
    if (!art || art.querySelector("img")) return;
    const img = document.createElement("img");
    img.className = "character-photo";
    img.alt = (card.dataset.character || key) + " portrait";
    // A portrait that fails to decode removes itself, revealing the
    // letter art underneath instead of a broken-image glyph.
    img.addEventListener("error", () => img.remove());
    img.src = uri;
    art.appendChild(img);
  });
}
window.addEventListener('pywebviewready', tryConnect);
document.addEventListener('DOMContentLoaded', tryConnect);

function callBridge(method, args) {
  // Fire-and-forget bridge call: pywebview methods return promises, so a
  // rejection is surfaced as a toast instead of an unhandled rejection.
  if (!bridgeApi || typeof bridgeApi[method] !== "function") return false;
  try {
    const result = bridgeApi[method](...(args || []));
    if (result && typeof result.catch === "function") {
      result.catch((error) => toast("Bridge error: " + (error && error.message ? error.message : error)));
    }
    return true;
  } catch (error) {
    toast("Bridge error: " + (error && error.message ? error.message : error));
    return false;
  }
}

function setConnectedUI(isConnected) {
  const label = $("#connectionLabel");
  if (label) label.textContent = isConnected ? "Online" : "Not connected";
  const live = $("#livePill");
  if (live) live.innerHTML = isConnected ? "<i></i> LIVE" : "<i></i> DEMO";
  // The model pill is NOT set from the bridge connection alone: a
  // connected bridge says nothing about the AI endpoint. Disconnect
  // is the only state the bridge itself can honestly report here;
  // every connected label comes from the payload's endpoint state
  // via updateModelPill below.
  if (!isConnected) {
    const pill = $("#modelConnectedPill");
    if (pill) pill.innerHTML = "<i></i> NOT CONNECTED";
  } else {
    updateModelPill(lastState);
  }
  updateHeroButtons();
  updateCommandBar(lastState);
}

// Hero controls (Command Center Phase 8, audit Bugs #1): the hero
// caption's three pills are real buttons now — Speak, Command and
// Get Results. Like the Emergency zone's controls, they are bridge
// controls: standalone (no bridge) they are disabled with the
// honest not-connected presentation rather than pretending to act.
function updateHeroButtons() {
  ["#heroSpeakButton", "#heroCommandButton", "#heroResultsButton"].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = !connected;
  });
}

// Model pill wording from the payload's endpoint state, which the
// bridge derives from the same model status lines shown in the
// checks list. The home page itself never probes the endpoint. Since
// Phase 8a the desktop app probes it periodically and the payload
// carries the probe-backed verdict as model.health: a real verdict
// (reachable / unreachable) takes precedence over the configuration-
// derived wording below, because it is live evidence rather than
// configuration alone. Before any probe verdict exists — health is
// "checking" / "not_configured" / absent (older payloads) — the
// configuration-derived behaviour is kept exactly as before.
const MODEL_PILL_TEXT = {
  "configured": "ENDPOINT CONFIGURED",
  "partial": "PARTIALLY CONFIGURED",
  "not-configured": "NOT CONFIGURED",
  "not-checked": "NOT CHECKED"
};
const MODEL_HEALTH_PILL_TEXT = {
  "reachable": "ENDPOINT REACHABLE",
  "unreachable": "ENDPOINT UNREACHABLE"
};
function updateModelPill(state) {
  const pill = $("#modelConnectedPill");
  if (!pill) return;
  if (!connected) { pill.innerHTML = "<i></i> NOT CONNECTED"; return; }
  const model = (state && state.model) || {};
  const healthText = MODEL_HEALTH_PILL_TEXT[model.health];
  if (healthText) {
    const healthDot = document.createElement("i");
    const healthLabel = document.createElement("span");
    healthLabel.textContent = healthText;
    pill.replaceChildren(healthDot, healthLabel);
    return;
  }
  let key = model.endpointState;
  if (!key) {
    // Older payload without endpointState: fall back to the ENDPOINT
    // status line rather than guessing from the bridge connection.
    const lines = Array.isArray(model.statusLines) ? model.statusLines : [];
    const endpointLine = lines.find((line) => line && line.label === "ENDPOINT");
    if (!endpointLine) key = "not-checked";
    else if (endpointLine.state === true) key = "configured";
    else if (endpointLine.state === false) {
      key = String(endpointLine.value || "").indexOf("Partially") === 0 ? "partial" : "not-configured";
    } else key = "not-checked";
  }
  const dot = document.createElement("i");
  const text = document.createElement("span");
  text.textContent = MODEL_PILL_TEXT[key] || MODEL_PILL_TEXT["not-checked"];
  pill.replaceChildren(dot, text);
}

// Hero status line (Phase 11): the human label for the payload's core
// state key, shown under the hero caption. The idle/ready label is
// "READY TO ASSIST"; every other label names the real state the
// desktop app reported. Standalone (no state ever pushed) it says
// NOT CONNECTED rather than pretending the assistant is ready.
const CORE_STATUS_TEXT = {
  idle: "READY TO ASSIST",
  ready: "READY TO ASSIST",
  listening: "LISTENING",
  thinking: "THINKING",
  processing: "PROCESSING",
  executing: "EXECUTING",
  speaking: "SPEAKING",
  completed: "TASK COMPLETED",
  error: "ERROR"
};
function updateCoreStatus(state) {
  const label = $("#coreStatusLabel");
  const wrap = $("#coreStatus");
  if (!label) return;
  if (!connected || !state) {
    label.textContent = "NOT CONNECTED";
    if (wrap) wrap.dataset.state = "idle";
    return;
  }
  const core = state.core || {};
  const key = core.state || "idle";
  label.textContent = CORE_STATUS_TEXT[key] || (core.mode ? String(core.mode) : "READY TO ASSIST");
  if (wrap) wrap.dataset.state = key;
}

// Chat heading pill: mirrors the live core state from the payload —
// the active core mode while the app is busy, READY when it is idle.
// Before the first state push (standalone demo) it says NOT
// CONNECTED instead of pretending to be ready.
function updateChatPill(state) {
  const label = $("#chatStateLabel");
  if (!label) return;
  if (!connected || !state) { label.textContent = "NOT CONNECTED"; return; }
  const core = state.core || {};
  if (state.busy) label.textContent = core.mode ? String(core.mode) : "BUSY";
  else label.textContent = "READY";
}

// Typing/working indicator: shown exactly while the payload's busy
// flag is true, hidden otherwise — the same flag that drives the
// chat pill above, so the two can never disagree. Nothing is shown
// for a standalone (unconnected) page.
function updateTypingIndicator(state) {
  const el = $("#typingIndicator");
  if (!el) return;
  const busy = !!(connected && state && state.busy);
  el.classList.toggle("hidden", !busy);
  el.setAttribute("aria-hidden", busy ? "false" : "true");
}

// ---------------------------------------------------------------------------
// Persistent command bar (Command Center Phase 9): the ONE composer
// (mic / input / send) lives in the bottom bar OUTSIDE every view
// container, so it is visible and functional on every view. Its
// status line mirrors the same busy/core state as the chat pill —
// the live core mode while the app is busy, READY when idle, NOT
// CONNECTED standalone. The bar's Stop control calls the Phase 4
// stopAll bridge method and is shown/enabled ONLY while the
// payload says something is actually running (running/busy);
// otherwise it is hidden and disabled — standalone included, where
// the not-connected state is the honest presentation.
// ---------------------------------------------------------------------------
function updateCommandBar(state) {
  const status = $("#commandBarStatus");
  if (status) {
    if (!connected || !state) status.textContent = "NOT CONNECTED";
    else if (state.busy) {
      const core = state.core || {};
      status.textContent = core.mode ? String(core.mode) : "BUSY";
    } else status.textContent = "READY";
  }
  const stop = $("#commandStopButton");
  if (stop) {
    const running = !!(connected && state && (state.running || state.busy));
    stop.classList.toggle("hidden", !running);
    stop.disabled = !running;
  }
}

// ---------------------------------------------------------------------------
// Live performance strip: bars are real CPU samples pushed by the bridge.
// Before the first sample every bar stays at 0 height — nothing is faked.
// ---------------------------------------------------------------------------
const PERF_BARS = 38;
const perfData = [];
const perfRamData = [];
let perfCpu = null; let perfRam = null;
function fmtPercent(value) { return value == null ? "—" : Math.round(value) + "%"; }
function perfBarHeight(value) { return (value == null ? 0 : Math.max(0, Math.min(100, value))) + "%"; }
function renderPerf() {
  const chart = $("#performanceChart");
  if (!chart) return;
  // Equalizer-style columns (Phase 11): each column carries the real
  // CPU sample in front and the real RAM sample behind it, both from
  // the same pushed state. A missing sample stays at 0 height — the
  // strip is never decorated with invented motion.
  while (chart.children.length < PERF_BARS) {
    const col = document.createElement("span");
    const ramBar = document.createElement("i"); ramBar.className = "ram-bar";
    const cpuBar = document.createElement("i"); cpuBar.className = "cpu-bar";
    col.append(ramBar, cpuBar);
    chart.appendChild(col);
  }
  while (chart.children.length > PERF_BARS) chart.removeChild(chart.lastChild);
  [...chart.children].forEach((col, i) => {
    const cpuBar = col.querySelector(".cpu-bar");
    const ramBar = col.querySelector(".ram-bar");
    if (cpuBar) cpuBar.style.height = perfBarHeight(i < perfData.length ? perfData[i] : null);
    if (ramBar) ramBar.style.height = perfBarHeight(i < perfRamData.length ? perfRamData[i] : null);
  });
  const caption = $("#perfCaption");
  if (caption) caption.textContent = `CPU ${fmtPercent(perfCpu)} · RAM ${fmtPercent(perfRam)}`;
}

// ---------------------------------------------------------------------------
// Live Performance Center (Command Center Phase 7): the Performance
// nav destination renders ONLY from the payload's performance block
// — the bounded series the bridge records from the same stats
// readings at its normal push cadence (cpu / ram / network up+down /
// disk read+write / own-process cpu+ram), the recorded span, the
// top-processes snapshot and the own-process values. The page keeps
// no performance collection of its own: the home strip above and
// this page read the SAME pushed series. A 1/5/15-minute window is
// selectable only once that much history has actually been
// recorded; before that its button is disabled and the note says
// recording is still in progress — a shorter recording is never
// stretched over a longer label. Temperature / GPU have no source
// on Windows with this stack, so their rows render "N/A on
// Windows" explicitly instead of disappearing. Standalone (no
// bridge) the page shows the honest not-connected state.
// ---------------------------------------------------------------------------
const PERF_WINDOW_SECONDS = [60, 300, 900];
const PERF_MAX_BARS = 60;
let perfWindowSeconds = 60;
function perfWindowEnabled(windowSeconds, spanSeconds) {
  return typeof spanSeconds === "number" && isFinite(spanSeconds)
    && spanSeconds >= windowSeconds;
}
function perfWindowSlice(series, windowSeconds) {
  if (!series || typeof series !== "object") return {};
  const elapsed = Array.isArray(series.elapsedSeconds) ? series.elapsedSeconds : [];
  const reference = Array.isArray(series.cpu) ? series.cpu : elapsed;
  const count = reference.length;
  if (!count) return series;
  let start = 0;
  if (windowSeconds && windowSeconds > 0) {
    if (elapsed.length === count) {
      const cutoff = elapsed[elapsed.length - 1] - windowSeconds;
      start = elapsed.findIndex((value) => typeof value === "number" && value >= cutoff);
      if (start < 0) start = count;
    } else {
      // No recorded timestamps: fall back to the bridge's nominal
      // ~1.5 s push cadence for the point count.
      start = Math.max(0, count - Math.ceil(windowSeconds / 1.5));
    }
  }
  const out = {};
  Object.keys(series).forEach((key) => {
    out[key] = Array.isArray(series[key]) ? series[key].slice(start) : series[key];
  });
  return out;
}
function perfLatest(values) {
  if (!Array.isArray(values)) return null;
  for (let i = values.length - 1; i >= 0; i--) {
    if (values[i] != null) return values[i];
  }
  return null;
}
function perfBarScale(front, back) {
  let max = 0;
  [front, back].forEach((values) => {
    (values || []).forEach((value) => {
      if (typeof value === "number" && isFinite(value) && value > max) max = value;
    });
  });
  return max;
}
function renderPerfChart(el, front, back, isRate) {
  if (!el) return;
  const frontValues = Array.isArray(front) ? front : [];
  const backValues = Array.isArray(back) ? back : [];
  const count = Math.max(frontValues.length, backValues.length);
  const scale = isRate ? perfBarScale(frontValues, backValues) : 100;
  const heightFor = (value) => {
    if (value == null || typeof value !== "number" || !isFinite(value)) return "0%";
    if (!scale || scale <= 0) return "0%";
    return Math.max(0, Math.min(100, (value / scale) * 100)) + "%";
  };
  // At most PERF_MAX_BARS columns, sampled evenly and always
  // including the latest sample — the same equalizer-column
  // rendering as the home strip, just over a longer series.
  const indices = [];
  if (count <= PERF_MAX_BARS) {
    for (let i = 0; i < count; i++) indices.push(i);
  } else {
    const step = (count - 1) / (PERF_MAX_BARS - 1);
    for (let i = 0; i < PERF_MAX_BARS; i++) indices.push(Math.round(i * step));
  }
  el.replaceChildren();
  indices.forEach((index) => {
    const col = document.createElement("span");
    const backBar = document.createElement("i"); backBar.className = "ram-bar";
    backBar.style.height = heightFor(backValues[index]);
    const frontBar = document.createElement("i"); frontBar.className = "cpu-bar";
    frontBar.style.height = heightFor(frontValues[index]);
    col.append(backBar, frontBar);
    el.appendChild(col);
  });
}
function renderPerformance(state) {
  const block = connected && state && state.performance ? state.performance : null;
  const span = block && typeof block.historySpanSeconds === "number"
    ? block.historySpanSeconds : 0;
  const note = $("#perfHistoryNote");
  $$(".perf-window-button").forEach((button) => {
    const windowSeconds = Number(button.dataset.window);
    const enabled = perfWindowEnabled(windowSeconds, span);
    button.disabled = !enabled;
    button.classList.toggle("active", windowSeconds === perfWindowSeconds);
    button.title = enabled ? "" : "Recording… — this window needs "
      + windowSeconds + "s of recorded history";
  });
  if (note) {
    if (!connected) note.textContent = "Not connected — desktop bridge required.";
    else if (!block || !span) note.textContent = "Recording… — windows unlock as history accrues (1 min after 60s recorded).";
    else note.textContent = "Recorded " + Math.floor(span) + "s of history"
      + (PERF_WINDOW_SECONDS.some((w) => !perfWindowEnabled(w, span))
        ? " — longer windows unlock as recording continues." : ".");
  }
  const series = block && block.series ? block.series : {};
  const sliced = perfWindowSlice(
    series, perfWindowEnabled(perfWindowSeconds, span) ? perfWindowSeconds : null);
  renderPerfChart($("#perfCpuChart"), sliced.cpu, null, false);
  renderPerfChart($("#perfRamChart"), sliced.ram, null, false);
  renderPerfChart($("#perfNetworkChart"), sliced.networkDown, sliced.networkUp, true);
  renderPerfChart($("#perfDiskChart"), sliced.diskRead, sliced.diskWrite, true);
  setDetailText("perfCpuCaption", fmtPercent(perfLatest(sliced.cpu)));
  setDetailText("perfRamCaption", fmtPercent(perfLatest(sliced.ram)));
  setDetailText("perfNetworkCaption",
    "↓ " + fmtRate(perfLatest(sliced.networkDown)) + " · ↑ " + fmtRate(perfLatest(sliced.networkUp)));
  setDetailText("perfDiskCaption",
    "R " + fmtRate(perfLatest(sliced.diskRead)) + " · W " + fmtRate(perfLatest(sliced.diskWrite)));
  const ownCpu = block ? block.ownCpuPercent : null;
  const ownRam = block ? block.ownMemoryPercent : null;
  if (ownCpu == null && ownRam == null) {
    setDetailText("perfOwnValue", "N/A");
  } else {
    const ownPct = (v) => (v == null ? "N/A" : Math.round(v) + "%");
    setDetailText("perfOwnValue", "CPU " + ownPct(ownCpu) + " · RAM " + ownPct(ownRam));
  }
  // Temperature / GPU: no source on Windows with this stack — the
  // payload carries explicit nulls, so the rows say so plainly.
  setDetailText("perfTemperatureValue",
    block && block.temperatureC != null ? Math.round(block.temperatureC) + " °C" : "N/A on Windows");
  setDetailText("perfGpuValue",
    block && block.gpuPercent != null ? Math.round(block.gpuPercent) + "%" : "N/A on Windows");
  const body = $("#perfProcessesBody");
  const empty = $("#perfProcessesEmpty");
  if (body && empty) {
    body.replaceChildren();
    const processes = block && Array.isArray(block.topProcesses) ? block.topProcesses : [];
    if (!processes.length) {
      empty.textContent = connected
        ? "No process data yet"
        : "Not connected — desktop bridge required.";
      empty.classList.remove("hidden");
    } else {
      empty.classList.add("hidden");
      processes.forEach((proc) => {
        if (!proc || !proc.name) return;
        const row = document.createElement("tr");
        const name = document.createElement("td");
        name.textContent = String(proc.name);
        const cpu = document.createElement("td");
        cpu.textContent = proc.cpuPercent == null ? "N/A" : Math.round(proc.cpuPercent) + "%";
        const memory = document.createElement("td");
        memory.textContent = proc.memoryPercent == null ? "N/A" : Math.round(proc.memoryPercent) + "%";
        row.append(name, cpu, memory);
        body.appendChild(row);
      });
    }
  }
}
$$(".perf-window-button").forEach((button) => button.addEventListener("click", () => {
  const windowSeconds = Number(button.dataset.window);
  if (PERF_WINDOW_SECONDS.indexOf(windowSeconds) !== -1) perfWindowSeconds = windowSeconds;
  renderPerformance(lastState);
}));

function setRing(stat, value) {
  const ring = document.querySelector(`.ring[data-stat="${stat}"]`);
  if (!ring) return;
  const span = ring.querySelector("span");
  if (value == null) {
    ring.style.setProperty("--value", 0);
    if (span) span.textContent = "—";
  } else {
    ring.style.setProperty("--value", value);
    if (span) span.textContent = Math.round(value) + "%";
  }
}

// ---------------------------------------------------------------------------
// System Overview detail rows (Command Center Phase 1): network and
// disk-activity rates, battery, per-drive usage, this app's own usage,
// and the GPU / temperature rows. Every value comes from the payload's
// stats block; a null (unreadable, no battery, no rate sample yet, or
// no source on this stack) renders as "N/A" — never an invented
// number. Standalone (no bridge) the rows keep the same "N/A"
// presentation as the rest of the panel.
// ---------------------------------------------------------------------------
function fmtRate(value) {
  if (value == null || typeof value !== "number" || !isFinite(value) || value < 0) return "N/A";
  if (value < 1024) return Math.round(value) + " B/s";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB/s";
  if (value < 1024 * 1024 * 1024) return (value / (1024 * 1024)).toFixed(1) + " MB/s";
  return (value / (1024 * 1024 * 1024)).toFixed(1) + " GB/s";
}
function fmtBattery(battery) {
  if (!battery || battery.percent == null) return "N/A";
  const base = Math.round(battery.percent) + "%";
  if (battery.plugged === true) return base + " · Plugged in";
  if (battery.plugged === false) return base + " · On battery";
  return base;
}
function setDetailText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
function renderDrives(drives) {
  const list = $("#driveList");
  if (!list) return;
  list.replaceChildren();
  const rows = Array.isArray(drives) ? drives.filter((d) => d && d.mount) : [];
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "drive-empty";
    empty.textContent = "N/A";
    list.appendChild(empty);
    return;
  }
  rows.forEach((drive) => {
    const row = document.createElement("div");
    row.className = "drive-row";
    const mount = document.createElement("span");
    mount.textContent = String(drive.mount);
    const percent = document.createElement("b");
    percent.textContent = drive.percent == null ? "N/A" : Math.round(drive.percent) + "%";
    row.append(mount, percent);
    list.appendChild(row);
  });
}
function updateSystemDetails(stats) {
  const s = stats || {};
  setDetailText("networkDown", fmtRate(s.networkDownloadBps));
  setDetailText("networkUp", fmtRate(s.networkUploadBps));
  setDetailText("diskRead", fmtRate(s.diskReadBps));
  setDetailText("diskWrite", fmtRate(s.diskWriteBps));
  setDetailText("batteryValue", fmtBattery(s.battery));
  // GPU and temperature have no source on this stack: the payload
  // carries explicit nulls for them, so these rows stay "N/A" unless
  // a real value ever arrives.
  setDetailText("gpuValue", s.gpuPercent == null ? "N/A" : Math.round(s.gpuPercent) + "%");
  setDetailText("temperatureValue", s.temperatureC == null ? "N/A" : Math.round(s.temperatureC) + " °C");
  if (s.ownCpuPercent == null && s.ownMemoryPercent == null) {
    setDetailText("ownProcessValue", "N/A");
  } else {
    const ownPct = (v) => (v == null ? "N/A" : Math.round(v) + "%");
    setDetailText("ownProcessValue",
      "CPU " + ownPct(s.ownCpuPercent) + " · RAM " + ownPct(s.ownMemoryPercent));
  }
  renderDrives(s.drives);
}

// ---------------------------------------------------------------------------
// Command Center Phase 2 panels: diagnostics (centre), active tasks
// with progress + the current operation line, and recent activity
// (right column). Every value comes from the payload blocks the
// bridge builds from real app state (controlCenter / tasks /
// activity / currentOperation). An absent or empty block renders
// the panel's honest empty state; standalone (no bridge) the panels
// say the bridge is required instead of inventing operations,
// activity or diagnostics.
// ---------------------------------------------------------------------------
function taskProgressPercent(task) {
  const value = task && task.progress;
  if (typeof value !== "number" || !isFinite(value)) return null;
  return Math.max(0, Math.min(100, Math.round(value * 100)));
}
function taskProgressElement(task) {
  const percent = taskProgressPercent(task);
  if (percent == null) return null;
  const cell = document.createElement("span");
  cell.className = "task-progress-cell";
  const track = document.createElement("span");
  track.className = "task-progress";
  const fill = document.createElement("i");
  fill.className = "task-progress-fill";
  fill.style.width = percent + "%";
  track.appendChild(fill);
  const label = document.createElement("b");
  label.className = "task-progress-label";
  label.textContent = percent + "%";
  cell.append(track, label);
  return cell;
}
function renderDiagnostics(state) {
  const list = $("#diagnosticsList");
  const empty = $("#diagnosticsEmpty");
  const logWrap = $("#diagnosticsLogWrap");
  const log = $("#diagnosticsLog");
  if (!list || !empty) return;
  list.replaceChildren();
  if (log) log.replaceChildren();
  if (logWrap) logWrap.classList.add("hidden");
  const cc = connected && state && state.controlCenter ? state.controlCenter : null;
  if (!cc) {
    empty.textContent = connected
      ? "No diagnostics available"
      : "Not connected — desktop bridge required.";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  ["agents", "models", "health", "queue", "budget", "memory"].forEach((key) => {
    const line = cc[key];
    if (!line) return;
    const row = document.createElement("div");
    row.className = "diagnostic-row";
    row.textContent = String(line);
    list.appendChild(row);
  });
  const logLines = Array.isArray(cc.logLines) ? cc.logLines : [];
  if (log && logWrap && logLines.length) {
    logLines.forEach((line) => {
      const row = document.createElement("div");
      row.className = "diagnostic-log-row";
      row.textContent = String(line);
      log.appendChild(row);
    });
    logWrap.classList.remove("hidden");
  }
}
const ACTIVE_TASK_STATUSES = ["running", "queued", "cancelling", "pending", "paused"];
function renderHomeTasks(state) {
  const operation = $("#currentOperation");
  if (operation) {
    const current = connected && state && state.currentOperation
      ? state.currentOperation : null;
    operation.textContent = current && current.text
      ? String(current.text)
      : "Idle — no task running";
  }
  const list = $("#homeTasksList");
  const empty = $("#homeTasksEmpty");
  if (!list || !empty) return;
  list.replaceChildren();
  const tasks = connected && state && Array.isArray(state.tasks) ? state.tasks : [];
  const active = tasks.filter(
    (task) => task && ACTIVE_TASK_STATUSES.indexOf(String(task.status || "").toLowerCase()) !== -1);
  if (!active.length) {
    empty.textContent = connected
      ? "No active tasks"
      : "Not connected — desktop bridge required.";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  active.forEach((task) => {
    const row = document.createElement("article");
    row.className = "home-task-row";
    const head = document.createElement("div");
    head.className = "home-task-head";
    const status = document.createElement("span");
    status.className = "task-status";
    status.textContent = (task.mark ? task.mark + " " : "") + String(task.status || "unknown").toUpperCase();
    const command = document.createElement("span");
    command.className = "task-command";
    command.textContent = task.command || "";
    head.append(status, command);
    row.appendChild(head);
    // Progress bar only when the payload carries a real fraction
    // (the task has plan steps); a task without steps shows its
    // status only — no fake percentage is ever rendered.
    const progress = taskProgressElement(task);
    if (progress) row.appendChild(progress);
    list.appendChild(row);
  });
}
function renderActivity(state) {
  const list = $("#activityList");
  const empty = $("#activityEmpty");
  if (!list || !empty) return;
  list.replaceChildren();
  const entries = connected && state && Array.isArray(state.activity) ? state.activity : [];
  if (!entries.length) {
    empty.textContent = connected
      ? "No recent activity"
      : "Not connected — desktop bridge required.";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  entries.forEach((entry) => {
    if (!entry || !entry.text) return;
    const row = document.createElement("div");
    row.className = "activity-row";
    const text = document.createElement("span");
    text.className = "activity-text";
    text.textContent = String(entry.text);
    row.appendChild(text);
    if (entry.time) {
      const time = document.createElement("span");
      time.className = "activity-time";
      time.textContent = String(entry.time);
      row.appendChild(time);
    }
    list.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// AI Core page (Command Center Phase 8): the Core nav destination
// renders ONLY from the same pushed payload as the home panels —
// the core state (state panel), the current operation, the
// activity feed and the approvals block. The state panel lists
// exactly the eight states of orvix/core_states.py; the live one
// is highlighted from the payload's core.state (the bridge's
// "ready" key is the idle state under its bridge name). OFFLINE
// is NOT a ninth core state (audit Bugs #7): it is this page's
// presentation for a disconnected bridge, shown as its own row
// whenever no bridge is connected, with no core state highlighted.
// ---------------------------------------------------------------------------
const CORE_STATE_KEYS = ["idle", "listening", "thinking", "processing",
  "executing", "speaking", "completed", "error"];
function corePanelStateKey(state) {
  const core = state && state.core ? state.core : {};
  const key = String(core.state || "idle").toLowerCase();
  if (key === "ready") return "idle";
  return CORE_STATE_KEYS.indexOf(key) !== -1 ? key : "idle";
}
function renderCore(state) {
  const rows = $$("#coreStateList .core-state-row");
  if (rows.length) {
    const liveKey = connected && state ? corePanelStateKey(state) : null;
    rows.forEach((row) => {
      const key = row.dataset.coreState;
      row.classList.toggle("live", connected ? key === liveKey : key === "offline");
    });
  }
  const modeLine = $("#coreModeLine");
  if (modeLine) {
    if (!connected || !state) modeLine.textContent = "Core mode: — (bridge disconnected)";
    else {
      const core = state.core || {};
      modeLine.textContent = "Core mode: " + (core.mode || "—");
    }
  }
  const operation = $("#coreCurrentOperation");
  if (operation) {
    const current = connected && state && state.currentOperation
      ? state.currentOperation : null;
    operation.textContent = current && current.text
      ? String(current.text)
      : "Idle — no task running";
  }
  renderCoreApprovals(state);
  const list = $("#coreActivityList");
  const empty = $("#coreActivityEmpty");
  if (list && empty) {
    list.replaceChildren();
    const entries = connected && state && Array.isArray(state.activity)
      ? state.activity : [];
    if (!entries.length) {
      empty.textContent = connected
        ? "No recent activity"
        : "Not connected — desktop bridge required.";
      empty.classList.remove("hidden");
    } else {
      empty.classList.add("hidden");
      entries.forEach((entry) => {
        if (!entry || !entry.text) return;
        const row = document.createElement("div");
        row.className = "activity-row";
        const text = document.createElement("span");
        text.className = "activity-text";
        text.textContent = String(entry.text);
        row.appendChild(text);
        if (entry.time) {
          const time = document.createElement("span");
          time.className = "activity-time";
          time.textContent = String(entry.time);
          row.appendChild(time);
        }
        list.appendChild(row);
      });
    }
  }
}

// Pending approvals (Phase 8): the desktop app has no approval
// queue — an approval request is ONE modal dialog in the desktop
// app that blocks until answered. The payload's approvals block
// therefore reports only whether that dialog is open right now
// and, when the app recorded it, the waiting request's title and
// message. This panel is read-only by design: answering happens
// in the desktop dialog, never here.
function renderCoreApprovals(state) {
  const box = $("#coreApprovals");
  if (!box) return;
  box.replaceChildren();
  const note = document.createElement("p");
  note.className = "core-note";
  if (!connected) {
    note.textContent = "Not connected — desktop bridge required.";
    box.appendChild(note);
    return;
  }
  const approvals = state && state.approvals ? state.approvals : null;
  if (approvals && approvals.dialogOpen) {
    const head = document.createElement("b");
    head.className = "core-approval-waiting";
    head.textContent = "⏳ Approval requested — waiting in the desktop app";
    box.appendChild(head);
    if (approvals.title) {
      const title = document.createElement("p");
      title.className = "core-approval-title";
      title.textContent = String(approvals.title);
      box.appendChild(title);
    }
    if (approvals.message) {
      const message = document.createElement("p");
      message.className = "core-approval-message";
      message.textContent = String(approvals.message);
      box.appendChild(message);
    }
    const hint = document.createElement("p");
    hint.className = "core-note";
    hint.textContent = "Answer it in the desktop dialog — approvals cannot be answered from this page.";
    box.appendChild(hint);
    return;
  }
  note.textContent = "No pending approvals — approval requests appear as dialogs in the desktop app.";
  box.appendChild(note);
}

// ---------------------------------------------------------------------------
// Python -> JS pushes. State shape (all fields may be missing/null):
// { version, stats:{cpu, ram, storage, osName, machine, uptime},
//   model:{name, endpoint, statusLines:[{label, value, state}]},
//   core:{state, mode}, character, wakeEnabled }
// ---------------------------------------------------------------------------
window.orvixPushState = function (state) {
  if (!state || typeof state !== "object") return;
  lastState = state; window.__orvixState = state;
  if (!connected) { connected = true; bridgeApi = bridgeApi || detectBridge(); setConnectedUI(true); requestPortraits(); }

  const versionEl = $("#appVersion");
  if (versionEl) versionEl.textContent = state.version ? "v" + state.version : "—";

  const stats = state.stats || {};
  setRing("cpu", stats.cpu); setRing("ram", stats.ram); setRing("storage", stats.storage);
  updateSystemDetails(stats);

  const osInfo = $("#osInfo");
  if (osInfo) {
    if (!stats.osName || stats.osName === "—") osInfo.textContent = "—";
    else osInfo.textContent = stats.osName + (stats.machine ? " · " + stats.machine : "");
  }
  const uptime = $("#uptimeValue");
  if (uptime) uptime.textContent = stats.uptime || "—";

  const model = state.model || {};
  const modelName = $("#modelName");
  if (modelName) modelName.textContent = model.name || "—";
  const modelEndpoint = $("#modelEndpoint");
  if (modelEndpoint) modelEndpoint.textContent = model.endpoint || "Not configured";

  const checks = $("#modelChecks");
  if (checks && Array.isArray(model.statusLines)) {
    const lines = model.statusLines.length
      ? model.statusLines
      : [{ label: "Internet", value: "Not checked", state: null }];
    checks.replaceChildren();
    lines.forEach((line) => {
      const row = document.createElement("div");
      const icon = document.createElement("i");
      icon.textContent = line.state === true ? "✓" : line.state === false ? "✕" : "•";
      const bold = document.createElement("b");
      bold.textContent = line.value == null ? "" : String(line.value);
      row.append(icon, document.createTextNode((line.label || "") + ": "), bold);
      checks.appendChild(row);
    });
  }

  // Appearance: the payload carries the Settings > Appearance
  // web-home animations toggle. When it is explicitly off the page
  // adds a class that disables its CSS animations/transitions (see
  // style.css); anything else keeps the animated default.
  document.body.classList.toggle("no-animations", state.webHomeAnimations === false);

  const core = state.core || {};
  const coreWrap = $("#coreWrap");
  if (coreWrap && core.state) coreWrap.dataset.state = core.state;
  const coreMode = $("#coreModeLabel");
  if (coreMode) coreMode.textContent = core.mode || "—";

  updateModelPill(state);
  updateChatPill(state);
  updateTypingIndicator(state);
  updateCommandBar(state);
  updateCoreStatus(state);
  renderTasks();
  renderDiagnostics(state);
  renderHomeTasks(state);
  renderActivity(state);
  renderCore(state);
  renderNotifications(state);
  renderEmergency(state);
  renderMemory(state);
  renderModels(state);
  renderPerformance(state);

  if (state.character) {
    const key = String(state.character).toUpperCase();
    // Personality theme (Phase 8): the payload's character drives
    // the page root's data-character attribute; the stylesheet's
    // per-character rules switch ONLY the ambient accent variables
    // (aurora, panel edge tint, character-card accents). Core
    // state colours are keyed to data-state, never to this.
    document.documentElement.dataset.character = key;
    const match = $$(".character").find((item) => item.dataset.character === key);
    if (match) {
      $$(".character").forEach((item) => item.classList.toggle("selected", item === match));
      const active = $("#activeCharacter");
      if (active) active.textContent = match.dataset.character;
    }
  }

  const toggle = $("#voiceToggle");
  if (toggle && state.wakeEnabled !== undefined) toggle.checked = !!state.wakeEnabled;

  // The wake pill shows the CONFIGURED wake phrase from the payload —
  // the exact phrase the desktop matcher listens for — never a
  // hard-coded string, so display and matcher cannot diverge.
  const wakeButton = $("#wakeWordButton");
  if (wakeButton && state.wakePhrase) wakeButton.textContent = state.wakePhrase;

  perfCpu = stats.cpu == null ? null : stats.cpu;
  perfRam = stats.ram == null ? null : stats.ram;
  // The home strip and the Performance page share ONE history: the
  // payload's performance series (Phase 7). When it is present the
  // strip renders its CPU/RAM tail; only an older payload without
  // the block falls back to accumulating the pushed samples here,
  // exactly as the strip always did.
  const perfSeries = state.performance && state.performance.series
    ? state.performance.series : null;
  if (perfSeries && Array.isArray(perfSeries.cpu)) {
    perfData.length = 0;
    perfSeries.cpu.slice(-PERF_BARS).forEach((value) => perfData.push(value == null ? null : value));
    perfRamData.length = 0;
    (Array.isArray(perfSeries.ram) ? perfSeries.ram : []).slice(-PERF_BARS)
      .forEach((value) => perfRamData.push(value == null ? null : value));
  } else {
    perfData.push(stats.cpu == null ? null : stats.cpu);
    while (perfData.length > PERF_BARS) perfData.shift();
    perfRamData.push(stats.ram == null ? null : stats.ram);
    while (perfRamData.length > PERF_BARS) perfRamData.shift();
  }
  renderPerf();
};

// ---------------------------------------------------------------------------
// Notification Center (Command Center Phase 3): the bell panel is
// rendered ONLY from the payload's notifications block — the latest
// records of the desktop app's persistent, bounded store, pushed by
// the bridge with the state (see orvix/webview_home.py). The page
// keeps NO notification collection of its own: the previous
// page-lifetime array that accumulated every SYSTEM chat message
// without a cap (audit Bugs #3) is gone, chat messages no longer
// feed this panel at all, and every render replaces the list from
// the payload. The badge shows the payload's real unreadCount and
// is hidden at 0. Standalone (no bridge / no block) the panel shows
// the honest not-connected empty state and the badge stays hidden.
// ---------------------------------------------------------------------------
function updateNotificationBadge(unreadCount) {
  const badge = $("#notificationBadge");
  if (!badge) return;
  const count = typeof unreadCount === "number" && isFinite(unreadCount)
    ? Math.max(0, Math.floor(unreadCount)) : 0;
  badge.textContent = count > 99 ? "99+" : String(count);
  badge.classList.toggle("hidden", count === 0);
}
function renderNotifications(state) {
  const list = $("#notificationsList");
  if (!list) return;
  const block = connected && state && state.notifications
    ? state.notifications : null;
  const items = block && Array.isArray(block.items) ? block.items : [];
  updateNotificationBadge(block ? block.unreadCount : 0);
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "notifications-empty";
    empty.textContent = connected
      ? "No notifications yet."
      : "Not connected — desktop bridge required.";
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    if (!item) return;
    const row = document.createElement("div");
    row.className = item.read
      ? "notification-item" : "notification-item unread";
    const head = document.createElement("div");
    head.className = "notification-head";
    const category = document.createElement("span");
    category.className = "notification-category";
    category.textContent = String(item.category || "system").toUpperCase();
    head.appendChild(category);
    if (item.time) {
      const time = document.createElement("span");
      time.className = "notification-time";
      time.textContent = String(item.time);
      head.appendChild(time);
    }
    row.appendChild(head);
    if (item.title) {
      const title = document.createElement("b");
      title.className = "notification-title";
      title.textContent = String(item.title);
      row.appendChild(title);
    }
    if (item.text) {
      const text = document.createElement("p");
      text.className = "notification-text";
      text.textContent = String(item.text);
      row.appendChild(text);
    }
    list.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// Memory Center (Command Center Phase 5): the Memory nav destination
// renders ONLY from the payload's memory block — the desktop app's
// one MemoryStore, grouped by its real categories with true counts,
// pushed by the bridge with the state (see orvix/webview_home.py).
// Search is a filter the page requests through the bridge
// (memorySearch); the next payload then carries the store's own
// search results and the page re-renders from it — the page keeps
// no memory collection of its own and never filters or deletes
// locally. Entry keys and texts are rendered with textContent only,
// so a remembered string can never become markup. Standalone (no
// bridge / no store) the view shows the honest not-connected /
// unavailable presentation instead of inventing entries.
// ---------------------------------------------------------------------------
const MEMORY_CATEGORIES = [
  ["preference", "Preferences"],
  ["fact", "Facts"],
  ["strategy", "Strategies"],
  ["note", "Notes"]
];
function memoryEntryTime(entry) {
  const ts = entry && entry.updatedAt;
  if (typeof ts !== "number" || !isFinite(ts) || ts <= 0) return "";
  try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return ""; }
}
function renderMemory(state) {
  const list = $("#memoryList");
  const empty = $("#memoryEmpty");
  const summary = $("#memorySummary");
  const clearButton = $("#memoryClearButton");
  const confirmBox = $("#memoryClearConfirm");
  if (!list || !empty) return;
  list.replaceChildren();
  const block = connected && state && state.memory ? state.memory : null;
  if (clearButton) clearButton.disabled = !connected || !block || !block.total;
  if (!connected && confirmBox) confirmBox.classList.add("hidden");
  if (!block) {
    if (summary) summary.textContent = "";
    empty.textContent = connected
      ? "Memory is unavailable — the desktop app has no memory store connected."
      : "Not connected — desktop bridge required.";
    empty.classList.remove("hidden");
    return;
  }
  const query = String(block.query || "");
  const total = typeof block.total === "number" ? block.total : 0;
  const shown = typeof block.shown === "number" ? block.shown : 0;
  if (summary) {
    if (query) {
      summary.textContent = total
        ? total + (total === 1 ? " memory matches " : " memories match ") + "“" + query + "”"
        : "";
    } else if (total) {
      summary.textContent = block.truncated
        ? "Showing latest " + shown + " of " + total + " memories"
        : total + (total === 1 ? " memory saved" : " memories saved");
    } else {
      summary.textContent = "";
    }
  }
  if (!total) {
    empty.textContent = query
      ? "No memories match “" + query + "”."
      : "No memories saved yet";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  const byCategory = block.byCategory || {};
  const counts = block.counts || {};
  const knownKeys = MEMORY_CATEGORIES.map(([key]) => key);
  const order = MEMORY_CATEGORIES.concat(
    Object.keys(byCategory)
      .filter((key) => knownKeys.indexOf(key) === -1)
      .map((key) => [key, key]));
  order.forEach(([key, label]) => {
    const entries = Array.isArray(byCategory[key]) ? byCategory[key] : [];
    const count = typeof counts[key] === "number" ? counts[key] : entries.length;
    const section = document.createElement("section");
    section.className = "memory-category";
    const heading = document.createElement("h3");
    heading.className = "memory-category-heading";
    heading.textContent = label + " · " + count;
    section.appendChild(heading);
    if (!entries.length) {
      const none = document.createElement("p");
      none.className = "memory-category-empty";
      if (count > 0) none.textContent = "More entries in this category exist beyond the latest shown.";
      else if (query) none.textContent = "No " + label.toLowerCase() + " match your search.";
      else none.textContent = "No " + label.toLowerCase() + " saved yet.";
      section.appendChild(none);
    }
    entries.forEach((entry) => {
      if (!entry) return;
      const row = document.createElement("article");
      row.className = "memory-entry";
      const head = document.createElement("div");
      head.className = "memory-entry-head";
      const keyEl = document.createElement("b");
      keyEl.className = "memory-entry-key";
      keyEl.textContent = String(entry.key || "");
      head.appendChild(keyEl);
      const stamp = memoryEntryTime(entry);
      if (stamp) {
        const time = document.createElement("span");
        time.className = "memory-entry-time";
        time.textContent = stamp;
        head.appendChild(time);
      }
      const del = document.createElement("button");
      del.type = "button";
      del.className = "memory-delete-button";
      del.textContent = "Delete";
      del.dataset.entryId = String(entry.id || "");
      del.dataset.entryKey = String(entry.key || "");
      del.disabled = !connected;
      head.appendChild(del);
      row.appendChild(head);
      if (entry.text) {
        const text = document.createElement("p");
        text.className = "memory-entry-text";
        text.textContent = String(entry.text);
        row.appendChild(text);
      }
      section.appendChild(row);
    });
    list.appendChild(section);
  });
}

// ---------------------------------------------------------------------------
// Model Management Center (Command Center Phase 6): the Models nav
// destination renders ONLY from the payload — the providers block
// (the Brain registry: id, name, a configured BOOLEAN, models with
// capability sets, and health) plus the model block for the desktop
// endpoint section. Health is "not_checked" until a real health
// check has run in the desktop app; the page never upgrades it from
// configuration alone and never sees any key material (the payload
// carries booleans only). A Test button click sends testProvider
// through the bridge, which enqueues the real check on the app
// side; the card then shows "checking" — a check the user just
// requested IS in flight — until the next payload carries the
// app's recorded verdict. Standalone (no bridge) the view shows
// the honest not-connected presentation and Test buttons are
// disabled.
// ---------------------------------------------------------------------------
const PROVIDER_DISPLAY_NAMES = {
  openai: "OpenAI", anthropic: "Anthropic", gemini: "Gemini",
  deepseek: "DeepSeek", xai: "xAI", mistral: "Mistral",
  openrouter: "OpenRouter", ollama: "Ollama", groq: "Groq"
};
const PROVIDER_HEALTH_TEXT = {
  "not_checked": "Not checked",
  "checking": "Checking…",
  "reachable": "Reachable",
  "unreachable": "Unreachable"
};
const providerTestsPending = new Set();
function providerHealthKey(provider) {
  if (provider && providerTestsPending.has(String(provider.id || ""))) return "checking";
  const health = provider && provider.health;
  return PROVIDER_HEALTH_TEXT[health] ? health : "not_checked";
}
function renderModels(state) {
  const grid = $("#providerGrid");
  const empty = $("#providersEmpty");
  if (!grid || !empty) return;
  grid.replaceChildren();
  // Desktop endpoint section: the payload's model block (the one
  // configured endpoint + model, with its probe-backed health when
  // a probe verdict exists — otherwise the honest not-checked /
  // configuration wording, never a claim of reachability).
  const model = connected && state && state.model ? state.model : null;
  const endpointUrl = $("#modelsEndpointUrl");
  const endpointModel = $("#modelsEndpointModel");
  const endpointHealth = $("#modelsEndpointHealth");
  if (endpointUrl) endpointUrl.textContent = model && model.endpoint ? String(model.endpoint) : "—";
  if (endpointModel) endpointModel.textContent = model && model.name ? String(model.name) : "—";
  if (endpointHealth) {
    if (!connected) endpointHealth.textContent = "Not connected";
    else if (model && MODEL_HEALTH_PILL_TEXT[model.health]) endpointHealth.textContent = PROVIDER_HEALTH_TEXT[model.health] || "Not checked";
    else if (model && model.health === "checking") endpointHealth.textContent = "Checking…";
    else if (model && model.health === "not_configured") endpointHealth.textContent = "Not configured";
    else endpointHealth.textContent = "Not checked";
  }
  const providers = connected && state && Array.isArray(state.providers) ? state.providers : [];
  // A pending test whose verdict has arrived is no longer pending.
  providers.forEach((provider) => {
    if (provider && provider.health && provider.health !== "not_checked") {
      providerTestsPending.delete(String(provider.id || ""));
    }
  });
  if (!providers.length) {
    empty.textContent = connected
      ? "No providers available"
      : "Not connected — desktop bridge required.";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  providers.forEach((provider) => {
    if (!provider || !provider.id) return;
    const card = document.createElement("article");
    card.className = "provider-card";
    const head = document.createElement("div");
    head.className = "provider-card-head";
    const name = document.createElement("b");
    name.className = "provider-name";
    name.textContent = PROVIDER_DISPLAY_NAMES[provider.id] || String(provider.name || provider.id);
    const configured = document.createElement("span");
    configured.className = provider.configured ? "provider-configured" : "provider-not-configured";
    configured.textContent = provider.keyless
      ? (provider.configured ? "Local — no key needed" : "Not configured")
      : (provider.configured ? "Configured" : "Not configured");
    head.append(name, configured);
    card.appendChild(head);
    const healthKey = providerHealthKey(provider);
    const health = document.createElement("p");
    health.className = "provider-health provider-health-" + healthKey;
    health.textContent = "Health: " + PROVIDER_HEALTH_TEXT[healthKey];
    card.appendChild(health);
    const models = Array.isArray(provider.models) ? provider.models : [];
    if (!models.length) {
      const none = document.createElement("p");
      none.className = "provider-models-empty";
      none.textContent = "No models registered.";
      card.appendChild(none);
    }
    models.forEach((entry) => {
      if (!entry || !entry.name) return;
      const row = document.createElement("div");
      row.className = "provider-model";
      const modelName = document.createElement("span");
      modelName.className = "provider-model-name";
      modelName.textContent = String(entry.name);
      row.appendChild(modelName);
      const caps = Array.isArray(entry.capabilities) ? entry.capabilities : [];
      caps.forEach((cap) => {
        const chip = document.createElement("span");
        chip.className = "capability-chip";
        chip.textContent = String(cap);
        row.appendChild(chip);
      });
      card.appendChild(row);
    });
    const test = document.createElement("button");
    test.type = "button";
    test.className = "provider-test-button";
    test.textContent = healthKey === "checking" ? "Checking…" : "Test";
    test.dataset.providerId = String(provider.id);
    test.disabled = !connected || healthKey === "checking";
    card.appendChild(test);
    grid.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// Emergency zone (Command Center Phase 4): the far-right panel with
// the EMERGENCY STOP button and the TAKE CONTROL toggle. Both
// controls render ONLY from the payload / bridge connection state:
// the toggle's checked state always comes from the payload's
// takeControl value on a state push (never from a local click), and
// standalone (no bridge) both controls are disabled with the honest
// not-connected presentation. The stop button never fires on the
// first click — it only reveals the in-page confirm step; the
// bridge stopAll call happens exclusively from the confirm button.
// ---------------------------------------------------------------------------
function renderEmergency(state) {
  const stopButton = $("#emergencyStopButton");
  const confirmButton = $("#emergencyStopConfirm");
  const toggle = $("#takeControlToggle");
  const stateLine = $("#takeControlState");
  const confirmBox = $("#emergencyConfirm");
  const on = !!(connected && state && state.takeControl);
  if (stopButton) stopButton.disabled = !connected;
  if (confirmButton) confirmButton.disabled = !connected;
  if (toggle) {
    toggle.disabled = !connected;
    toggle.checked = on;
  }
  if (stateLine) {
    if (!connected) stateLine.textContent = "Not connected — desktop bridge required.";
    else stateLine.textContent = on
      ? "Take Control: ON — delegated steps need your approval"
      : "Take Control: OFF";
  }
  if (!connected && confirmBox) confirmBox.classList.add("hidden");
}

window.orvixPushMessages = function (list) {
  (list || []).forEach((m) => {
    if (!m) return;
    if (m.id) {
      // Streamed message: partial pushes update the one in-flight
      // node in place; the final push completes it.
      upsertStreamMessage(m);
    } else {
      addMessage(m.kind === "user" ? "user" : m.kind === "system" ? "system" : "assistant", m.text, m.speaker, m.time);
    }
    // Chat messages never feed the Notification Center: it renders
    // only from the state payload's notifications block (Phase 3).
  });
};

// Pushed by the bridge after a Clear Conversation: the desktop
// console view and the bridge buffer are already empty, so the web
// chat view empties too — both UIs stay in sync. Only message nodes
// are removed; the static system note is page furniture, not a
// conversation message. Idempotent: the Clear button below also
// calls it directly for immediate feedback.
window.orvixChatCleared = function () {
  const log = $("#chatLog");
  if (log) log.querySelectorAll(".message").forEach((node) => node.remove());
  streamNodes.clear();
};

// ---------------------------------------------------------------------------
// User actions -> bridge (live) or honest demo behaviour (standalone).
// ---------------------------------------------------------------------------
function runAction(action) {
  if (connected && callBridge("runAction", [action])) {
    toast("Request sent: " + action);
    return;
  }
  toast("Not connected — desktop bridge required.");
  addMessage("system", `"${action}" needs the ORVIX desktop bridge.`);
}
$$(".quick-action").forEach(button => button.addEventListener("click", () => runAction(button.dataset.action)));

function sendCommand() {
  const input = $("#commandInput");
  const command = input.value.trim();
  if (!command) return;
  input.value = "";
  if (connected && bridgeApi && typeof bridgeApi.sendCommand === "function") {
    // Nothing is echoed locally: the app pushes the real exchange back
    // through orvixPushMessages once it has actually run the command.
    callBridge("sendCommand", [command]);
    return;
  }
  addMessage("user", command);
  addMessage("system", "Not connected — open ORVIX via the desktop app (WebView home) to run commands. Standalone page is a demo.");
}
$("#sendButton").addEventListener("click", sendCommand);
$("#commandInput").addEventListener("keydown", e => { if (e.key === "Enter") sendCommand(); });

// Clear Conversation: asks first (the confirm text states that task
// history is NOT deleted), then — with a live bridge — routes
// through the bridge so the desktop view and the message buffer
// clear too; the bridge pushes orvixChatCleared back to confirm.
// Standalone it clears only this page's view and says so.
$("#clearChatButton").addEventListener("click", () => {
  const note = "Clear the conversation view? Task history and settings are NOT deleted.";
  if (!window.confirm(note)) return;
  if (connected && bridgeApi && typeof bridgeApi.clearConversation === "function") {
    callBridge("clearConversation", []);
    window.orvixChatCleared();
    toast("Conversation cleared.");
    return;
  }
  window.orvixChatCleared();
  toast("Conversation cleared (demo — this page only).");
});

$$(".character").forEach(button => button.addEventListener("click", () => {
  $$(".character").forEach(item => item.classList.remove("selected"));
  button.classList.add("selected");
  const name = button.dataset.character;
  $("#activeCharacter").textContent = name;
  // Local ambient tint follows the local selection immediately,
  // exactly like the .selected card styling above; the bridge
  // push re-confirms it from the app's persisted selection.
  document.documentElement.dataset.character = String(name || "").toUpperCase();
  if (connected) {
    // Live mode: the app confirms with its own pushed system message.
    callBridge("selectCharacter", [name]);
  } else {
    toast(`${name} character profile selected (demo — not saved).`);
  }
}));

const voiceToggle = $("#voiceToggle");
voiceToggle.addEventListener("change", () => {
  if (connected) { callBridge("setWakeWord", [voiceToggle.checked]); return; }
  toast(voiceToggle.checked ? "Voice control enabled (demo — not saved)." : "Voice control disabled (demo — not saved).");
});
$("#wakeWordButton").addEventListener("click", () => {
  voiceToggle.checked = !voiceToggle.checked;
  voiceToggle.dispatchEvent(new Event("change"));
});

// Stop Speaking: routes through the bridge to the desktop app's
// SpeechEngine.stop_speaking(). Safe to press while ORVIX is silent
// (a no-op then); standalone it explains the bridge is required.
$("#stopSpeakingButton").addEventListener("click", () => {
  if (connected && callBridge("stopSpeaking", [])) {
    toast("Stop speaking requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Command-bar Stop (Phase 9): wired to the SAME Phase 4 stopAll
// bridge method as the Emergency panel's confirmed stop — it
// enqueues the desktop app's _stop_all composition (cancel current
// task + stop speaking + disarm queued auto-run + cancel the
// running Brain queue task). The button is only visible while the
// payload reports something running (see updateCommandBar), so a
// click here is always a stop of work actually in flight; the
// result and the cannot-undo limit are reported back by the app as
// a SYSTEM message, exactly as with the Emergency panel.
$("#commandStopButton").addEventListener("click", () => {
  if (connected && callBridge("stopAll", [])) {
    toast("Stop requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Emergency Stop: the first click only opens the in-page confirm
// step — an accidental click must not stop anything. Only the
// confirm button calls the bridge stopAll method, which enqueues
// the desktop app's _stop_all composition (cancel current task +
// stop speaking + disarm queued auto-run + cancel the running Brain
// queue task). The result and the cannot-undo limit are reported
// back by the app as a SYSTEM message; the button itself claims
// nothing beyond the request.
$("#emergencyStopButton").addEventListener("click", () => {
  if (!connected) { toast("Not connected — desktop bridge required."); return; }
  $("#emergencyConfirm").classList.remove("hidden");
});
$("#emergencyStopCancel").addEventListener("click", () => {
  $("#emergencyConfirm").classList.add("hidden");
});
$("#emergencyStopConfirm").addEventListener("click", () => {
  $("#emergencyConfirm").classList.add("hidden");
  if (connected && callBridge("stopAll", [])) {
    toast("Emergency Stop requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Take Control toggle: a change only SENDS the request through the
// bridge (setTakeControl enqueues the app-side settings write). The
// toggle's displayed state is then re-rendered from the last
// payload — it snaps back to the payload value until the next state
// push confirms the app's real, persisted flag, so the page can
// never show a Take Control state the app does not actually have.
$("#takeControlToggle").addEventListener("change", (event) => {
  const want = !!event.target.checked;
  if (connected && callBridge("setTakeControl", [want])) {
    toast(want ? "Take Control requested: ON" : "Take Control requested: OFF");
  } else {
    toast("Not connected — desktop bridge required.");
  }
  renderEmergency(lastState);
});

// Memory Center actions (Phase 5). Search: the button / Enter only
// SENDS the filter through the bridge (memorySearch) — the list
// re-renders when the next state payload arrives carrying the
// store's search results for that filter, exactly like the Take
// Control toggle above; nothing is filtered locally. Clear search
// sends the empty filter the same way.
function runMemorySearch() {
  const input = $("#memorySearchInput");
  const query = input ? input.value.trim() : "";
  if (connected && callBridge("memorySearch", [query])) {
    toast(query ? "Searching memory…" : "Memory search cleared.");
    return;
  }
  toast("Not connected — desktop bridge required.");
}
$("#memorySearchButton").addEventListener("click", runMemorySearch);
$("#memorySearchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") runMemorySearch(); });
$("#memorySearchClear").addEventListener("click", () => {
  const input = $("#memorySearchInput");
  if (input) input.value = "";
  if (connected && callBridge("memorySearch", [""])) {
    toast("Memory search cleared.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Per-entry delete: one delegated listener for the whole list. The
// button carries the entry id the payload sent (falling back to the
// entry key for entries stored before ids existed); the bridge
// enqueues the deletion on the app side and the list refreshes from
// the next payload — the entry is never removed locally first, so
// the page cannot show a deletion the store did not apply.
$("#memoryList").addEventListener("click", (event) => {
  const target = event.target;
  const button = target && typeof target.closest === "function"
    ? target.closest(".memory-delete-button") : null;
  if (!button) return;
  const ident = button.dataset.entryId || button.dataset.entryKey || "";
  if (!ident) { toast("This entry cannot be deleted from here."); return; }
  if (connected && callBridge("memoryForget", [ident])) {
    toast("Memory delete requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Clear all: the first click only opens the in-page confirm step —
// an accidental click must not clear anything. Only the confirm
// button calls the bridge memoryClear method, which enqueues the
// store clear on the app side; the list and counts refresh from the
// next payload.
$("#memoryClearButton").addEventListener("click", () => {
  if (!connected) { toast("Not connected — desktop bridge required."); return; }
  $("#memoryClearConfirm").classList.remove("hidden");
});
$("#memoryClearCancel").addEventListener("click", () => {
  $("#memoryClearConfirm").classList.add("hidden");
});
$("#memoryClearConfirmButton").addEventListener("click", () => {
  $("#memoryClearConfirm").classList.add("hidden");
  if (connected && callBridge("memoryClear", [])) {
    toast("Memory clear requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Model Management actions (Phase 6). One delegated listener for
// the provider grid: a Test button carries the provider id the
// payload sent; the bridge testProvider method enqueues the real
// health check on the app side, and the card's verdict refreshes
// from the next payload — the result is never shown locally first,
// so the page cannot claim a reachability the check did not find.
// Until that payload arrives the provider is marked pending, which
// renderModels shows as "Checking…" (the requested check really
// is in flight).
$("#providerGrid").addEventListener("click", (event) => {
  const target = event.target;
  const button = target && typeof target.closest === "function"
    ? target.closest(".provider-test-button") : null;
  if (!button) return;
  const providerId = button.dataset.providerId || "";
  if (!providerId) { toast("This provider cannot be tested from here."); return; }
  if (connected && callBridge("testProvider", [providerId])) {
    providerTestsPending.add(providerId);
    renderModels(lastState);
    toast("Provider test requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Desktop-endpoint section: endpoint / key editing lives in
// Settings, opened through the same console path as the home model
// panel's Configure button; standalone it shows the Settings view.
$("#modelsSettingsButton").addEventListener("click", () => {
  if (connected && bridgeApi && typeof bridgeApi.openConsole === "function") { callBridge("openConsole", []); return; }
  showTab("settings");
});

// Standalone fallback only: browser speech recognition fills the composer.
// In the desktop app the mic routes to the app's own speech engine instead.
let recognition = null;
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SpeechRecognition) {
  recognition = new SpeechRecognition();
  recognition.lang = "bn-BD"; recognition.interimResults = false; recognition.continuous = false;
  recognition.onstart = () => { $("#micButton").classList.add("listening"); toast("Listening… Speak now."); };
  recognition.onend = () => $("#micButton").classList.remove("listening");
  recognition.onerror = () => toast("Voice recognition could not start. Check microphone permission.");
  recognition.onresult = e => { $("#commandInput").value = e.results[0][0].transcript; sendCommand(); };
} else {
  $("#micButton").title = "Speech recognition is not supported in this browser";
}
$("#micButton").addEventListener("click", () => {
  if (connected && bridgeApi && typeof bridgeApi.startListening === "function") {
    callBridge("startListening", []);
    toast("Listening…");
    return;
  }
  if (!voiceToggle.checked) return toast("Enable Voice Control first.");
  if (!recognition) return toast("এই ব্রাউজারে Voice Recognition সমর্থিত নয়।");
  try { recognition.start(); } catch (_) { toast("Voice input is already active."); }
});

// Hero controls (Command Center Phase 8, audit Bugs #1) — the three
// hero pills are real controls with the pill styling kept:
// - Speak runs the composer's own mic path (the exact handler
//   above, bridge startListening included) by clicking the mic
//   button, so there is only one listen path to maintain.
// - Command focuses the composer input on the home view.
// - Get Results goes to where results actually live: the latest
//   assistant message in the chat (replies and action outcomes are
//   presented there; the Tasks view holds records, not results).
//   It scrolls that message into view and flashes it; when no
//   assistant message exists yet it says so honestly.
// All three are disabled standalone by updateHeroButtons above.
$("#heroSpeakButton").addEventListener("click", () => {
  const mic = $("#micButton");
  if (mic) mic.click();
});
$("#heroCommandButton").addEventListener("click", () => {
  showTab("home");
  const input = $("#commandInput");
  if (!input) return;
  input.scrollIntoView({ behavior: "smooth", block: "center" });
  input.focus();
});
$("#heroResultsButton").addEventListener("click", () => {
  showTab("home");
  const log = $("#chatLog");
  const messages = log ? [...log.querySelectorAll(".assistant-message")] : [];
  const latest = messages.length ? messages[messages.length - 1] : null;
  if (!latest) { toast("No results yet — run a command first."); return; }
  latest.scrollIntoView({ behavior: "smooth", block: "center" });
  latest.classList.remove("result-flash");
  void latest.offsetWidth;
  latest.classList.add("result-flash");
  setTimeout(() => latest.classList.remove("result-flash"), 2600);
});

$("#modelSettingsButton").addEventListener("click", () => {
  if (connected && bridgeApi && typeof bridgeApi.openConsole === "function") { callBridge("openConsole", []); return; }
  showTab("settings");
});
// Notification Center actions. Mark-read behaviour (Phase 3):
// opening the panel marks everything read through the bridge, and
// the explicit Mark-all-read button does the same on demand. The
// page does NOT update the badge or the unread styling locally —
// both refresh from the next state payload, which reflects the
// store's real state after the app applies the enqueued write, so
// the badge can never claim a read state the store does not have.
$("#notificationsButton").addEventListener("click", () => {
  renderNotifications(lastState);
  const panel = $("#notificationsPanel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden") && connected) {
    callBridge("markNotificationsRead", []);
  }
});
$("#notificationsMarkRead").addEventListener("click", () => {
  if (connected && callBridge("markNotificationsRead", [])) {
    toast("Notifications marked as read.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});
// Clear: the app's usual confirmation pattern (same as Clear
// Conversation) — the confirm states exactly what is removed, then
// the bridge enqueues the store clear on the app side; the panel
// and badge refresh from the next payload.
$("#notificationsClear").addEventListener("click", () => {
  const note = "Clear all notifications? This removes the notification history. Task history and settings are NOT deleted.";
  if (!window.confirm(note)) return;
  if (connected && callBridge("clearNotifications", [])) {
    toast("Notifications cleared.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});
$("#notificationsClose").addEventListener("click", () => {
  $("#notificationsPanel").classList.add("hidden");
});
$("#profileButton").addEventListener("click", () => {
  // Same behaviour as the Settings nav / model settings button: with a
  // live bridge the profile opens the ORVIX console window; standalone
  // it falls back to the About view instead of doing nothing.
  if (connected && bridgeApi && typeof bridgeApi.openConsole === "function") { callBridge("openConsole", []); return; }
  showTab("about");
});

// ---------------------------------------------------------------------------
// Tasks view: renders the payload's tasks block — real TaskStore
// records pushed by the bridge (status, command, created/finished
// times), newest first. Command Center Phase 9 completes the
// centre: each row also shows the record's progress bar (Phase 2),
// its recorded result and its safe-diagnostics summary when the
// payload carries them, and a Cancel button ONLY on cancellable
// records (the payload's cancellable flag — queued / running per
// the record's real status machine). Cancel goes through the
// bridge cancelTask method, which enqueues the app's existing
// cancel paths; the row is never updated locally first, so the
// view cannot show a cancellation the app did not apply.
// Standalone (no bridge) it shows the same not-connected honesty
// as the rest of the page instead of inventing tasks; a connected
// bridge with no history shows the honest empty state.
// ---------------------------------------------------------------------------
function renderTasks() {
  const list = $("#tasksList");
  const empty = $("#tasksEmpty");
  if (!list || !empty) return;
  list.replaceChildren();
  if (!connected) {
    empty.textContent = "Not connected — desktop bridge required. Tasks recorded by the ORVIX desktop app appear here when the bridge is connected.";
    empty.classList.remove("hidden");
    return;
  }
  const tasks = lastState && Array.isArray(lastState.tasks) ? lastState.tasks : [];
  if (!tasks.length) {
    empty.textContent = "No tasks yet";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  tasks.forEach((task) => {
    if (!task) return;
    const row = document.createElement("article");
    row.className = "task-row";
    const status = document.createElement("span");
    status.className = "task-status";
    status.textContent = (task.mark ? task.mark + " " : "") + String(task.status || "unknown").toUpperCase();
    const command = document.createElement("span");
    command.className = "task-command";
    command.textContent = task.command || "";
    const times = document.createElement("span");
    times.className = "task-times";
    times.textContent = "Started " + (task.createdAt || "—") + " · Finished " + (task.finishedAt || "Not finished");
    row.append(status, command, times);
    const progress = taskProgressElement(task);
    if (progress) row.appendChild(progress);
    // Result / diagnostics lines (Phase 9): rendered only when the
    // payload carries them — the record's own recorded result text
    // (for a failed task, its recorded failure description) and the
    // safe-diagnostics counts summary. Neither is ever invented.
    if (task.result) {
      const result = document.createElement("span");
      result.className = "task-result";
      result.textContent = String(task.result);
      row.appendChild(result);
    }
    if (task.diagnostics) {
      const diagnostics = document.createElement("span");
      diagnostics.className = "task-diagnostics";
      diagnostics.textContent = "Diagnostics: " + String(task.diagnostics);
      row.appendChild(diagnostics);
    }
    // Cancel (Phase 9): only on cancellable records, addressed by
    // the record id the payload carries. The click is handled by
    // the delegated listener below — the row itself claims nothing.
    if (task.cancellable && task.id) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "task-cancel-button";
      cancel.textContent = "Cancel";
      cancel.dataset.taskId = String(task.id);
      cancel.disabled = !connected;
      row.appendChild(cancel);
    }
    list.appendChild(row);
  });
}

// Tasks view Cancel (Phase 9): one delegated listener for the
// whole list. The button carries the record id the payload sent;
// the bridge cancelTask method enqueues the cancellation on the
// app side (queued -> TaskStore.cancel_queued_task, the running
// active task -> _cancel_current_task) and the list refreshes from
// the next payload — a task that already finished is an honest
// no-op in the app and its row simply loses the button then.
$("#tasksList").addEventListener("click", (event) => {
  const target = event.target;
  const button = target && typeof target.closest === "function"
    ? target.closest(".task-cancel-button") : null;
  if (!button) return;
  const taskId = button.dataset.taskId || "";
  if (!taskId) { toast("This task cannot be cancelled from here."); return; }
  if (connected && callBridge("cancelTask", [taskId])) {
    toast("Task cancel requested.");
    return;
  }
  toast("Not connected — desktop bridge required.");
});

// Secondary-view cards (Tools / Settings / About). Command Center
// Phase 9 honesty pass (audit Bugs #2): every card is a REAL entry
// point — its Open button navigates to the destination that
// actually exists (a centre view, the Voice panel, the Notification
// Center, or the desktop console's Settings dialog via the bridge)
// — and its text describes only what the app actually does. No
// card may promise a capability the app lacks. A card's target is
// {tab: "<view>"} for an in-page destination or {action: "..."}
// for the voice / notifications / settings actions handled by the
// delegated listener below.
const tabContent = {
  tools: {title:"ORVIX Tools", desc:"Quick access to the centres and panels that exist in this app — every card opens the real thing.", cards:[
    ["Voice Assistant","Speech recognition, wake word and text-to-speech controls — the Voice Control panel on the Home view.",{action:"voice"}],
    ["AI Models","The Brain provider registry: configured status, model capabilities and real connection tests.",{tab:"models"}],
    ["Memory Center","What ORVIX was explicitly asked to remember — search it, delete individual entries, or clear it all.",{tab:"memory"}],
    ["AI Core","The core state machine, live: current state, current operation, recent activity and pending approvals.",{tab:"core"}],
    ["Tasks","Recent tasks with progress, recorded results and cancel for queued or running tasks.",{tab:"tasks"}],
    ["Live Performance","Recorded CPU, RAM, network and disk history, top processes and this app's own usage.",{tab:"performance"}],
    ["Notifications","The Notification Center: recorded notifications with categories, read state and clear.",{action:"notifications"}],
    ["Settings","The desktop console's Settings dialog (10 tabs): model endpoint, voice, character, appearance, startup, permissions, notifications and about.",{action:"settings"}]
  ]},
  settings: {title:"Settings & Connections", desc:"ORVIX settings live in the desktop console's Settings dialog (10 tabs). Each card opens that dialog; the card names the tab where the setting lives.", cards:[
    ["AI Model Endpoint","Endpoint URL, model name, temperature and response limits — Settings > AI Model tab. The access key is stored in Windows Credential Manager via keyring, never in settings.json.",{action:"settings"}],
    ["Voice Settings","Recognition language, voice, speech rate and volume, and the wake phrase — Settings > Voice tab.",{action:"settings"}],
    ["Appearance","The animations toggle for this home page — Settings > Appearance tab. Assistant character selection lives on the Home view. ORVIX ships this one dark theme; there is no light theme or density setting.",{action:"settings"}],
    ["Permissions","A read-only summary of the app's permission posture — Settings > Permissions tab. Sensitive actions still ask for your approval when they run.",{action:"settings"}]
  ]},
  about: {title:"About ORVIX", desc:"ORVIX is a local desktop AI assistant for voice, commands and productivity.", cards:[
    ["Version","—"],
    ["Architecture","A local Windows desktop app: a Python/Tkinter console plus this WebView home, connected by an enqueue-only local bridge. Replies come from the configured AI endpoint or the Brain providers."],
    ["Privacy","Everything is stored locally on this PC — settings, task history and memory live in the app's local data folder. The desktop endpoint's API key is kept in Windows Credential Manager via keyring — never in settings.json, this page or the logs; Brain provider keys come from environment variables only. Audit logs redact secrets, and safe-diagnostics exports carry counts and timings only — no commands, results or provider payloads."]
  ]}
};
function showTab(tab) {
  // AI Core view (Phase 8): toggled here for every tab so each
  // branch below only has to handle its own view, exactly like the
  // branches treat each other's views.
  const coreViewEl = $("#coreView");
  if (coreViewEl) coreViewEl.classList.toggle("hidden", tab !== "core");
  if (tab === "core") {
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.add("hidden");
    const tv = $("#tasksView"); if (tv) tv.classList.add("hidden");
    const mv = $("#memoryView"); if (mv) mv.classList.add("hidden");
    const mov = $("#modelsView"); if (mov) mov.classList.add("hidden");
    const pv = $("#performanceView"); if (pv) pv.classList.add("hidden");
    renderCore(lastState);
    $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.tab === tab));
    return;
  }
  const tasksView = $("#tasksView");
  const memoryView = $("#memoryView");
  const modelsView = $("#modelsView");
  const performanceView = $("#performanceView");
  if (tab === "home") {
    $("#homeView").classList.remove("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.add("hidden");
    if (memoryView) memoryView.classList.add("hidden");
    if (modelsView) modelsView.classList.add("hidden");
    if (performanceView) performanceView.classList.add("hidden");
  } else if (tab === "tasks") {
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.remove("hidden");
    if (memoryView) memoryView.classList.add("hidden");
    if (modelsView) modelsView.classList.add("hidden");
    if (performanceView) performanceView.classList.add("hidden");
    renderTasks();
  } else if (tab === "memory") {
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.add("hidden");
    if (memoryView) memoryView.classList.remove("hidden");
    if (modelsView) modelsView.classList.add("hidden");
    if (performanceView) performanceView.classList.add("hidden");
    renderMemory(lastState);
  } else if (tab === "models") {
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.add("hidden");
    if (memoryView) memoryView.classList.add("hidden");
    if (modelsView) modelsView.classList.remove("hidden");
    if (performanceView) performanceView.classList.add("hidden");
    renderModels(lastState);
  } else if (tab === "performance") {
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.add("hidden");
    if (memoryView) memoryView.classList.add("hidden");
    if (modelsView) modelsView.classList.add("hidden");
    if (performanceView) performanceView.classList.remove("hidden");
    renderPerformance(lastState);
  } else {
    const data = tabContent[tab] || tabContent.tools;
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.remove("hidden");
    if (tasksView) tasksView.classList.add("hidden");
    if (memoryView) memoryView.classList.add("hidden");
    if (modelsView) modelsView.classList.add("hidden");
    if (performanceView) performanceView.classList.add("hidden");
    $("#secondaryTitle").textContent = data.title; $("#secondaryDescription").textContent = data.desc;
    $("#secondaryCards").replaceChildren();
    data.cards.forEach(([title, desc, target]) => {
      const card = document.createElement("article"); card.className = "secondary-card";
      const h = document.createElement("h3"); h.textContent = title;
      const p = document.createElement("p"); p.textContent = desc;
      card.append(h,p);
      // A card with a target gets a real Open button (Phase 9):
      // the delegated listener below routes it to the existing
      // view or action the card names. A card without one (About's
      // informational cards) stays text-only and claims no action.
      if (target) {
        const open = document.createElement("button");
        open.type = "button";
        open.className = "secondary-card-action";
        open.textContent = "Open →";
        if (target.tab) open.dataset.gotoTab = target.tab;
        if (target.action) open.dataset.gotoAction = target.action;
        card.appendChild(open);
      }
      $("#secondaryCards").append(card);
    });
    if (tab === "about") {
      const version = (window.__orvixState && window.__orvixState.version) || (lastState && lastState.version);
      if (version) {
        $$("#secondaryCards .secondary-card").forEach((card) => {
          const h = card.querySelector("h3"); const p = card.querySelector("p");
          if (h && p && h.textContent === "Version") p.textContent = `ORVIX AI Agent Assistant · v${version}`;
        });
      }
    }
  }
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.tab === tab));
}
// Voice nav entry (Phase 11): Voice is a panel on the home view, not a
// separate page — the nav item brings the user to it: the home view is
// shown, the Voice Control panel is scrolled into view and flashed so
// the eye lands on it, and Voice stays the active nav item. Every
// control inside the panel keeps its existing wiring (wake toggle,
// wake phrase pill, Stop Speaking).
function focusVoicePanel() {
  showTab("home");
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.tab === "voice"));
  const panel = $(".voice-panel");
  if (!panel) return;
  panel.scrollIntoView({ behavior: "smooth", block: "center" });
  panel.classList.remove("focus-flash");
  void panel.offsetWidth;
  panel.classList.add("focus-flash");
  setTimeout(() => panel.classList.remove("focus-flash"), 2600);
}
$$(".nav-item").forEach(button => button.addEventListener("click", () => {
  const tab = button.dataset.tab;
  if (tab === "voice") { focusVoicePanel(); return; }
  if (tab === "settings" && connected && bridgeApi && typeof bridgeApi.openConsole === "function") {
    callBridge("openConsole", []);
    toast("Settings open in the ORVIX console window.");
    return;
  }
  showTab(tab);
}));
$$("[data-tab='tools']").forEach(button => button.addEventListener("click", () => showTab("tools")));
// Secondary-card Open buttons (Phase 9): one delegated listener
// routes every card to the destination it names — an existing
// centre view (showTab), the Home Voice panel, the Notification
// Center (through the bell's own handler), or the desktop
// console's Settings dialog through the bridge, exactly like the
// Settings nav item. Standalone, the Settings action says the
// bridge is required instead of pretending to open anything.
$("#secondaryCards").addEventListener("click", (event) => {
  const target = event.target;
  const button = target && typeof target.closest === "function"
    ? target.closest(".secondary-card-action") : null;
  if (!button) return;
  if (button.dataset.gotoTab) { showTab(button.dataset.gotoTab); return; }
  const action = button.dataset.gotoAction || "";
  if (action === "voice") { focusVoicePanel(); return; }
  if (action === "notifications") {
    const bell = $("#notificationsButton");
    if (bell) bell.click();
    return;
  }
  if (action === "settings") {
    if (connected && bridgeApi && typeof bridgeApi.openConsole === "function") {
      callBridge("openConsole", []);
      toast("Settings open in the ORVIX console window.");
      return;
    }
    toast("Not connected — desktop bridge required.");
  }
});
$("#backHome").addEventListener("click", () => showTab("home"));
$("#coreBackHome").addEventListener("click", () => showTab("home"));
$("#tasksBackHome").addEventListener("click", () => showTab("home"));
$("#memoryBackHome").addEventListener("click", () => showTab("home"));
$("#modelsBackHome").addEventListener("click", () => showTab("home"));
$("#performanceBackHome").addEventListener("click", () => showTab("home"));

// Initial state: not connected until the bridge proves otherwise, and an
// empty performance strip (all bars at 0 height, caption shows dashes).
setConnectedUI(false);
updateChatPill(null);
updateTypingIndicator(null);
updateCoreStatus(null);
updateSystemDetails(null);
renderTasks();
renderDiagnostics(null);
renderHomeTasks(null);
renderActivity(null);
renderCore(null);
renderNotifications(null);
renderEmergency(null);
renderMemory(null);
renderModels(null);
renderPerformance(null);
renderPerf();
tryConnect();

// ---------------------------------------------------------------------------
// Ambient FX (Phase 11, denser reactor field in Phase 12): two
// lightweight canvas particle layers — a slow drift behind the whole
// page and a denser field of motes inside the hero reactor, rising
// slowly and twinkling. Pure decoration: no data is read or invented.
// One shared requestAnimationFrame loop drives both; it pauses while
// the document is hidden and stops entirely while the Settings >
// Appearance animations toggle is off (body.no-animations) or the OS
// asks for reduced motion, matching the CSS animations. A slow
// watchdog restarts it if the toggle is switched back on.
// ---------------------------------------------------------------------------
const FX_STATE_COLORS = {
  idle: "#00dfff", ready: "#00dfff", listening: "#00e88f",
  thinking: "#a78bfa", processing: "#ffb020", executing: "#38bdf8",
  speaking: "#2dd4bf", completed: "#00e88f", error: "#ff4d5e"
};
const fx = { running: false, raf: 0, bg: [], core: [], w: 0, h: 0, cw: 0, ch: 0 };
function fxSeed() {
  const bgCanvas = $("#bgCanvas");
  const coreCanvas = $("#reactorParticles");
  if (bgCanvas) {
    fx.w = bgCanvas.width = window.innerWidth;
    fx.h = bgCanvas.height = window.innerHeight;
    if (!fx.bg.length) {
      for (let i = 0; i < 55; i++) {
        fx.bg.push({ x: Math.random() * fx.w, y: Math.random() * fx.h,
          r: Math.random() * 1.8 + 0.4, s: Math.random() * 0.16 + 0.04,
          drift: Math.random() * 0.1 - 0.05 });
      }
    }
  }
  if (coreCanvas) {
    fx.cw = coreCanvas.width;
    fx.ch = coreCanvas.height;
    if (!fx.core.length) {
      for (let i = 0; i < 46; i++) {
        const a = Math.random() * Math.PI * 2;
        const rr = Math.sqrt(Math.random()) * 0.92;
        fx.core.push({ x: Math.cos(a) * rr, y: Math.sin(a) * rr,
          vy: Math.random() * 0.3 + 0.15,
          sway: Math.random() * Math.PI * 2,
          tw: Math.random() * Math.PI * 2,
          ts: Math.random() * 1.6 + 0.6,
          r: Math.random() * 1.7 + 0.5 });
      }
    }
  }
}
function fxDraw() {
  const bgCanvas = $("#bgCanvas");
  if (bgCanvas && fx.w) {
    const ctx = bgCanvas.getContext("2d");
    ctx.clearRect(0, 0, fx.w, fx.h);
    ctx.fillStyle = "rgba(0, 223, 255, 0.5)";
    fx.bg.forEach((p) => {
      p.y -= p.s; p.x += p.drift;
      if (p.y < -4) { p.y = fx.h + 4; p.x = Math.random() * fx.w; }
      if (p.x < -4) p.x = fx.w + 4; else if (p.x > fx.w + 4) p.x = -4;
      ctx.globalAlpha = 0.14 + (p.r / 2.2) * 0.3;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalAlpha = 1;
  }
  const coreCanvas = $("#reactorParticles");
  const wrap = $("#coreWrap");
  if (coreCanvas && wrap && fx.cw) {
    const ctx = coreCanvas.getContext("2d");
    const cx = fx.cw / 2; const cy = fx.ch / 2;
    const maxOrbit = Math.min(cx, cy) * 0.96;
    ctx.clearRect(0, 0, fx.cw, fx.ch);
    ctx.fillStyle = FX_STATE_COLORS[wrap.dataset.state] || FX_STATE_COLORS.idle;
    fx.core.forEach((p) => {
      // Slow upward drift through the reactor disc; a mote that
      // leaves the top respawns along the bottom chord. Brightness
      // twinkles on its own phase, and a gentle sideways sway is
      // applied at draw time so it never accumulates.
      p.y -= p.vy * 0.016;
      if (p.y < -0.95) { p.y = 0.9; p.x = (Math.random() * 2 - 1) * 0.8; }
      p.tw += p.ts * 0.016;
      const x = cx + (p.x + Math.sin(p.tw * 0.7 + p.sway) * 0.012) * maxOrbit;
      const y = cy + p.y * maxOrbit;
      ctx.globalAlpha = 0.2 + 0.5 * (0.5 + 0.5 * Math.sin(p.tw));
      ctx.beginPath(); ctx.arc(x, y, p.r, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalAlpha = 1;
  }
}
function fxStep() {
  if (!fx.running) return;
  fxDraw();
  fx.raf = requestAnimationFrame(fxStep);
}
// Motion is blocked by the Phase 7 Settings > Appearance toggle
// (body.no-animations) or by the OS reduced-motion preference; the
// CSS side applies the same pair of conditions (Phase 12).
function fxMotionBlocked() {
  return document.body.classList.contains("no-animations")
    || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
function fxStart() {
  if (fx.running || document.hidden) return;
  if (fxMotionBlocked()) return;
  fxSeed();
  fx.running = true;
  fx.raf = requestAnimationFrame(fxStep);
}
function fxStop() {
  fx.running = false;
  cancelAnimationFrame(fx.raf);
  ["#bgCanvas", "#reactorParticles"].forEach((selector) => {
    const canvas = $(selector);
    if (canvas) canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  });
}
document.addEventListener("visibilitychange", () => { if (document.hidden) fxStop(); else fxStart(); });
window.addEventListener("resize", () => { if (fx.running) fxSeed(); });
setInterval(() => {
  if (fxMotionBlocked()) fxStop();
  else fxStart();
}, 1600);
fxStart();
