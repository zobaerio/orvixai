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
  updateCoreStatus(state);
  renderTasks();
  renderDiagnostics(state);
  renderHomeTasks(state);
  renderActivity(state);
  renderNotifications(state);

  if (state.character) {
    const key = String(state.character).toUpperCase();
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
  perfData.push(stats.cpu == null ? null : stats.cpu);
  while (perfData.length > PERF_BARS) perfData.shift();
  perfRamData.push(stats.ram == null ? null : stats.ram);
  while (perfRamData.length > PERF_BARS) perfRamData.shift();
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
// times), newest first, read-only. Standalone (no bridge) it shows
// the same not-connected honesty as the rest of the page instead of
// inventing tasks; a connected bridge with no history shows the
// honest empty state.
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
    list.appendChild(row);
  });
}

const tabContent = {
  tools: {title:"ORVIX Tools", desc:"Quick access to your desktop tools and assistant capabilities.", cards:[
    ["Voice Assistant","Speech recognition, wake word and text-to-speech controls."],
    ["File Manager","Search, organize and manage local files (desktop bridge required)."],
    ["Web Assistant","Open websites and perform browser-based tasks."],
    ["AI Chat","Connect your local or cloud AI model for conversational responses."],
    ["System Monitor","Display real CPU, RAM, storage and uptime telemetry."],
    ["Task Manager","Queue, run, cancel and review desktop automation tasks."]
  ]},
  settings: {title:"Settings & Connections", desc:"Configure your ORVIX desktop integrations. Values shown here are interface placeholders.", cards:[
    ["AI Model Endpoint","Connect your local model endpoint and configure model name, API URL and access key."],
    ["Voice Settings","Choose recognition language, microphone, voice output and wake word."],
    ["Appearance","Customize theme, animations, density and assistant character."],
    ["Permissions","Control access to files, applications, browser and system actions."]
  ]},
  about: {title:"About ORVIX", desc:"ORVIX is a desktop AI assistant interface designed for voice, commands and productivity.", cards:[
    ["Version","—"],
    ["Architecture","Frontend dashboard with optional desktop bridge and AI endpoint."],
    ["Privacy","Keep credentials in the desktop main process; do not expose API keys in frontend code."]
  ]}
};
function showTab(tab) {
  const tasksView = $("#tasksView");
  if (tab === "home") {
    $("#homeView").classList.remove("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.add("hidden");
  } else if (tab === "tasks") {
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.add("hidden");
    if (tasksView) tasksView.classList.remove("hidden");
    renderTasks();
  } else {
    const data = tabContent[tab] || tabContent.tools;
    $("#homeView").classList.add("hidden"); $("#secondaryView").classList.remove("hidden");
    if (tasksView) tasksView.classList.add("hidden");
    $("#secondaryTitle").textContent = data.title; $("#secondaryDescription").textContent = data.desc;
    $("#secondaryCards").replaceChildren();
    data.cards.forEach(([title, desc]) => {
      const card = document.createElement("article"); card.className = "secondary-card";
      const h = document.createElement("h3"); h.textContent = title;
      const p = document.createElement("p"); p.textContent = desc;
      card.append(h,p); $("#secondaryCards").append(card);
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
$("#backHome").addEventListener("click", () => showTab("home"));
$("#tasksBackHome").addEventListener("click", () => showTab("home"));

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
renderNotifications(null);
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
