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

function addMessage(type, text, speaker, time) {
  const wrap = document.createElement("div");
  wrap.className = type === "user" ? "message user-message" : "message assistant-message";
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
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "copy-button";
  copyButton.textContent = "Copy";
  copyButton.title = "Copy this message";
  copyButton.addEventListener("click", () => copyMessageText(text));
  head.appendChild(copyButton);
  const p = document.createElement("p"); p.textContent = text;
  wrap.append(head, p); $("#chatLog").appendChild(wrap);
  trimChatLog();
  $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
}

// ---------------------------------------------------------------------------
// Desktop bridge detection.
// The desktop app exposes either window.orvixBridge (preload-style) or the
// pywebview bridge at window.pywebview.api. Python pushes live state back
// through window.orvixPushState / window.orvixPushMessages below.
// ---------------------------------------------------------------------------
let bridgeApi = null; let connected = false; let lastState = null;
function detectBridge(){ if (window.orvixBridge) return window.orvixBridge; if (window.pywebview && window.pywebview.api) return window.pywebview.api; return null; }
function tryConnect(){ const api = detectBridge(); if (api && !connected){ bridgeApi = api; connected = true; setConnectedUI(true); } }
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
// checks list: configuration is what is known — the endpoint is
// never probed by the home page, so the pill never claims the model
// itself is connected or reachable.
const MODEL_PILL_TEXT = {
  "configured": "ENDPOINT CONFIGURED",
  "partial": "PARTIALLY CONFIGURED",
  "not-configured": "NOT CONFIGURED",
  "not-checked": "NOT CHECKED"
};
function updateModelPill(state) {
  const pill = $("#modelConnectedPill");
  if (!pill) return;
  if (!connected) { pill.innerHTML = "<i></i> NOT CONNECTED"; return; }
  const model = (state && state.model) || {};
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
let perfCpu = null; let perfRam = null;
function fmtPercent(value) { return value == null ? "—" : Math.round(value) + "%"; }
function renderPerf() {
  const chart = $("#performanceChart");
  if (!chart) return;
  while (chart.children.length < PERF_BARS) chart.appendChild(document.createElement("span"));
  while (chart.children.length > PERF_BARS) chart.removeChild(chart.lastChild);
  [...chart.children].forEach((bar, i) => {
    const value = i < perfData.length ? perfData[i] : null;
    bar.style.height = (value == null ? 0 : Math.max(0, Math.min(100, value))) + "%";
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
// Python -> JS pushes. State shape (all fields may be missing/null):
// { version, stats:{cpu, ram, storage, osName, machine, uptime},
//   model:{name, endpoint, statusLines:[{label, value, state}]},
//   core:{state, mode}, character, wakeEnabled }
// ---------------------------------------------------------------------------
window.orvixPushState = function (state) {
  if (!state || typeof state !== "object") return;
  lastState = state; window.__orvixState = state;
  if (!connected) { connected = true; bridgeApi = bridgeApi || detectBridge(); setConnectedUI(true); }

  const versionEl = $("#appVersion");
  if (versionEl) versionEl.textContent = state.version ? "v" + state.version : "—";

  const stats = state.stats || {};
  setRing("cpu", stats.cpu); setRing("ram", stats.ram); setRing("storage", stats.storage);

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

  const core = state.core || {};
  const coreWrap = $("#coreWrap");
  if (coreWrap && core.state) coreWrap.dataset.state = core.state;
  const coreMode = $("#coreModeLabel");
  if (coreMode) coreMode.textContent = core.mode || "—";

  updateModelPill(state);
  updateChatPill(state);
  updateTypingIndicator(state);
  renderTasks();

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
  renderPerf();
};

// ---------------------------------------------------------------------------
// Notifications: the bell panel lists the app's real SYSTEM messages —
// the same messages the bridge pushes into the chat log. Nothing is
// invented: before the first system message arrives the panel shows an
// honest empty state.
// ---------------------------------------------------------------------------
const notifications = [];
function renderNotifications() {
  const list = $("#notificationsList");
  if (!list) return;
  list.replaceChildren();
  if (!notifications.length) {
    const empty = document.createElement("p");
    empty.className = "notifications-empty";
    empty.textContent = "No notifications yet.";
    list.appendChild(empty);
    return;
  }
  notifications.forEach((text) => {
    const item = document.createElement("p");
    item.className = "notification-item";
    item.textContent = text;
    list.appendChild(item);
  });
}
function isSystemMessage(m) {
  return m.kind === "system" || String(m.speaker || "").toUpperCase() === "SYSTEM";
}

window.orvixPushMessages = function (list) {
  (list || []).forEach((m) => {
    if (!m) return;
    addMessage(m.kind === "user" ? "user" : m.kind === "system" ? "system" : "assistant", m.text, m.speaker, m.time);
    if (isSystemMessage(m) && m.text) {
      notifications.push(String(m.text));
      renderNotifications();
    }
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
$("#notificationsButton").addEventListener("click", () => {
  renderNotifications();
  $("#notificationsPanel").classList.toggle("hidden");
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
$$(".nav-item").forEach(button => button.addEventListener("click", () => {
  const tab = button.dataset.tab;
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
renderTasks();
renderPerf();
tryConnect();
