/* ============================================================
   FRIDAY COMMAND CENTER — app.js
   Plain ES2019+. No build step, no framework, no network.
   Bridge contract: window.pywebview.api (pywebview).
   Every bridge method is called only through call()/has() and
   only from the contract list in the smoke test.
   ============================================================ */
(() => {
  "use strict";

  /* ---------- tiny helpers ---------- */
  const $ = (id) => document.getElementById(id);
  const $$ = (sel) => Array.prototype.slice.call(document.querySelectorAll(sel));
  const DASH = "—";

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const num = (v, d) => (isNum(v) ? v.toFixed(d == null ? 0 : d) : DASH);
  const secs = (v) => (isNum(v) ? v.toFixed(2) + " s" : DASH);
  const pct = (v) => (isNum(v) ? Math.round(v) + "%" : DASH);
  const boolLabel = (v) => (typeof v === "boolean" ? (v ? "Yes" : "No") : DASH);
  function trun(v, n) {
    v = String(v == null ? "" : v);
    return v.length > n ? v.slice(0, n) + "…" : v;
  }
  function fmtUptime(s) {
    if (!isNum(s)) return DASH;
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const p = (x) => String(x).padStart(2, "0");
    return h > 0 ? h + "h " + p(m) + "m " + p(r) + "s" : m > 0 ? m + "m " + p(r) + "s" : r + "s";
  }
  function fmtTime(v) {
    if (v == null || v === "") return DASH;
    let d;
    if (typeof v === "number") {
      if (v > 1e12) d = new Date(v);
      else if (v > 1e9) d = new Date(v * 1000);
      else return String(v);
    } else {
      d = new Date(v);
      if (isNaN(d.getTime())) return String(v);
    }
    if (isNaN(d.getTime())) return String(v);
    try {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (_) {
      return d.toISOString();
    }
  }
  function jsonKey(v) { try { return JSON.stringify(v); } catch (_) { return String(v); } }
  function deepGet(o, path) {
    let cur = o;
    for (const p of path.split(".")) {
      if (cur == null) return undefined;
      cur = cur[p];
    }
    return cur;
  }

  /* ---------- bridge ---------- */
  function api() {
    try { return (window.pywebview && window.pywebview.api) || null; } catch (_) { return null; }
  }
  function has(name) {
    const a = api();
    return !!a && typeof a[name] === "function";
  }
  const inflight = new Set();
  let capabilityRefreshQueued = false;
  let workflowRefreshQueued = false;
  let rosterRefreshQueued = false;
  const settingsRefreshQueued = new Set();
  async function call(name) {
    const a = api();
    const args = Array.prototype.slice.call(arguments, 1);
    if (!a || typeof a[name] !== "function") {
      const err = new Error("missing:" + name);
      err.code = "missing";
      throw err;
    }
    return await a[name].apply(a, args);
  }
  const isMissing = (e) => !!e && (e.code === "missing" || String(e.message || "").indexOf("missing:") === 0);

  /* ---------- display prefs (localStorage) ---------- */
  const PREF_RM = "friday.ui.reducedMotion";
  const PREF_LP = "friday.ui.lowPower";
  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (_) { return undefined; } }
  function osPrefersReduced() {
    try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
    catch (_) { return false; }
  }
  function readPref(key, fallback) {
    const v = lsGet(key);
    if (v === null) return fallback;
    return v === "1" ? true : v === "0" ? false : undefined;
  }
  function motionOK() {
    const de = document.documentElement;
    if (de.dataset.lowPower === "on") return false;
    if (de.dataset.reducedMotion === "on") return false;
    if (osPrefersReduced()) return false;
    return true;
  }
  function applyPrefs() {
    const de = document.documentElement;
    const rm = readPref(PREF_RM, osPrefersReduced()) ?? (de.dataset.reducedMotion ? de.dataset.reducedMotion === "on" : osPrefersReduced());
    const lp = readPref(PREF_LP, false) ?? (de.dataset.lowPower === "on");
    de.dataset.reducedMotion = rm ? "on" : "off";
    de.dataset.lowPower = lp ? "on" : "off";
    window.FieldSettings?.presentation();
    restartLoops();
  }

  /* ---------- toast + live region ---------- */
  let toastTimer = 0;
  function toast(msg) {
    const el = $("toast");
    if (!el) return;
    el.textContent = String(msg == null ? "" : msg);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3600);
  }

  /* ---------- state ---------- */
  const S = {
    status: null, turn: null, chat: null, events: null, core: null,
    agents: null, tasks: null, schedule: null, memory: null, knowledge: null,
    notes: null, tools: null, skills: null, startup: null, settings: null,
    roster: null, world: null, stats: null, focus: null, ear: null, logs: null,
    lab: null
  };
  const ST = {};            // method -> "ok" | "waiting" | "missing" | "error"
  const LAST = {};          // method -> last rendered json key (dedupe)
  const voiceControl = {pending:false, generation:0};
  let speechGeneration = 0;
  let activePane = "view-command";
  let lastServedBrain = "";
  let stopPending = false;

  function setConn(state, text) {
    const el = $("conn");
    if (el) { el.dataset.state = state; el.textContent = text; }
    const sc = $("side-conn");
    if (sc) sc.textContent = text.replace(/^Bridge:\s*/, "");
  }

  /* ---------- state-block helpers ---------- */
  function blockHtml(st, name) {
    if (st === "waiting") {
      return '<div class="empty"><div class="empty-t">Waiting for the pywebview bridge…</div>' +
        '<div class="empty-s">The window is still loading its host connection.</div></div>';
    }
    if (st === "missing") {
      return '<div class="empty"><div class="empty-t">Unavailable — not wired in this build</div>' +
        '<div class="empty-s">Bridge method <code>' + esc(name) + '()</code> is not present in this build.</div></div>';
    }
    return '<div class="empty"><div class="empty-t">Bridge call failed</div>' +
      '<div class="empty-s"><code>' + esc(name) + '()</code> threw — the next poll will retry.</div></div>';
  }
  // Paints `st` into the given element ids. Returns true when the caller
  // should stop (data is not available).

  /* ---------- shared refresh path ---------- */
  const RENDER = {}; // method -> fn(data, st)
  RENDER.get_identity_views = (data, state) => {
    if (window.FieldRoster) window.FieldRoster.render(data, state);
  };
  RENDER.get_town_view = (data, state) => {
    return; // Public Town visual excluded; no asset loading or Town bridge calls.

    if (window.FieldTown) window.FieldTown.render(data, state);
  };

  async function refresh(name) {
    const fn = RENDER[name];
    if (!fn) return;
    const run = async () => {
      const voiceGeneration = voiceControl.generation;
      const speechReadGeneration = speechGeneration;
      const capabilityGeneration = window.FieldCapabilities?.generation();
      const workflowGeneration = window.FieldWorkflows?.generation();
      const settingsGeneration = window.FieldSettings?.generation();
      const n8nGeneration = window.FieldN8N?.generation();
      if (!api()) { ST[name] = "waiting"; fn(null, "waiting"); return; }
      if (!has(name)) { ST[name] = "missing"; fn(null, "missing"); return; }
      try {
        const d = name === "get_identity_views" && window.FieldRoster
          ? await call(name, ...window.FieldRoster.selection()) : name === "get_town_view" && window.FieldTown
            ? await call(name, ...window.FieldTown.selection()) : await call(name);
        if (name === "get_capability_inventory" &&
            capabilityGeneration !== window.FieldCapabilities?.generation()) return;
        if (name === "get_workflow_view" &&
            workflowGeneration !== window.FieldWorkflows?.generation()) return;
        if (["get_startup","get_settings"].includes(name) && settingsGeneration !== window.FieldSettings?.generation()) return;
        if(name==="get_n8n"&&n8nGeneration!==window.FieldN8N?.generation())return;
        if (name === "get_voice_screen" && (voiceControl.pending ||
            voiceGeneration !== voiceControl.generation || speechReadGeneration !== speechGeneration)) return;
        if (name === "get_speech" && speechReadGeneration !== speechGeneration) return;
        if (name === "get_status" && d && (voiceControl.pending || voiceGeneration !== voiceControl.generation)) {
          d.paused = S.status?.paused;
        }
        ST[name] = "ok";
        const key = "ok:" + jsonKey(d);
        // These views also depend on connection state, even when their payload is unchanged.
        if (LAST[name] === key && !["get_routing_view","get_startup","get_settings"].includes(name)) return;
        fn(d, "ok");
        LAST[name] = key;
      } catch (e) {
        if (name === "get_capability_inventory" &&
            capabilityGeneration !== window.FieldCapabilities?.generation()) return;
        if (name === "get_workflow_view" &&
            workflowGeneration !== window.FieldWorkflows?.generation()) return;
        if (["get_startup","get_settings"].includes(name) && settingsGeneration !== window.FieldSettings?.generation()) return;
        if(name==="get_n8n"&&n8nGeneration!==window.FieldN8N?.generation())return;
        ST[name] = isMissing(e) ? "missing" : "error";
        LAST[name] = "";
        fn(null, ST[name]);
      }
    };
    if (inflight.has(name)) return;
    inflight.add(name);
    try { await run(); } catch (_) { /* never break the loop */ } finally {
      inflight.delete(name);
      if (settingsRefreshQueued.delete(name)) refresh(name);
      if (name === "get_identity_views" && rosterRefreshQueued) {
        rosterRefreshQueued = false;
        refresh(name);
      }
      if (name === "get_capability_inventory" && capabilityRefreshQueued) {
        capabilityRefreshQueued = false;
        refresh(name);
      }
      if (name === "get_workflow_view" && workflowRefreshQueued) {
        workflowRefreshQueued = false;
        refresh(name);
      }
    }
  }

  // ONE refresh request at a time. The heavy lab/roster methods (get_roster
  // re-derives the whole cast, ~0.8s) must never overlap each other or the
  // fast set: the shared bridge - and especially the single-threaded HTTP
  // acceptance harness, whose listen backlog is tiny - answers a parallel
  // burst with ERR_CONNECTION_REFUSED. Each chain is fire-and-forget and
  // self-healing (refresh never rejects).
  function refreshChain(names) {
    let p = Promise.resolve();
    for (let i = 0; i < names.length; i++) {
      const name = names[i];
      p = p.then(() => refresh(name)).catch(() => {});
    }
    return p;
  }
  function refreshPane(target) {
    return refreshChain(PANE_METHODS[target] || []);
  }

  /* ============================================================
     GLOBAL CHROME
     ============================================================ */
  // --- clock ---
  function tickClock() {
    const el = $("clock");
    if (!el) return;
    const n = new Date();
    const p = (x) => String(x).padStart(2, "0");
    el.textContent = p(n.getHours()) + ":" + p(n.getMinutes()) + ":" + p(n.getSeconds());
  }

  // --- stop buttons ---
  function syncStopButtons() {
    const active = !!(S.turn && (S.turn.active || S.turn.stop_available));
    const wired = has("stop_turn");
    $$("[data-stop]").forEach((b) => {
      b.disabled = !active || !wired || stopPending;
      b.title = !wired ? "stop_turn() not wired in this build"
        : active ? "Stop the current turn" : "No active turn";
    });
    paintHomeState();
    paintHomeDuty();
  }
  async function doStop() {
    if (stopPending) return;
    if (!has("stop_turn")) { toast("stop_turn() not wired in this build"); return; }
    const btns = $$("[data-stop]");
    stopPending = true;
    paintHomeState();
    btns.forEach((b) => { b.disabled = true; });
    try {
      const r = await call("stop_turn", "user");
      if (r && r.ok === false) {
        toast(r.reason || r.message || "Stop was not accepted.");
      } else if (r && (r.stopped_active_turn === true || r.cancelled_requests > 0)) {
        toast("Stop requested. Waiting for the turn to finish.");
      } else if (r && r.ok === true) {
        toast("No active turn was stopped.");
      } else {
        toast("Stop was not confirmed.");
      }
    } catch (e) {
      toast(isMissing(e) ? "stop_turn() not wired in this build" : "stop_turn() failed.");
    }
    stopPending = false;
    refreshChain(["get_turn", "get_chat", "get_core"]);
    if (activePane === "view-voice") refresh("get_voice_screen");
    syncStopButtons();
  }

  // --- session voice input (set_paused) ---
  function syncMute() {
    const paused = S.status?.paused;
    const known = typeof paused === "boolean" && has("set_paused");
    $$("[data-mute]").forEach((b) => {
      b.textContent = paused ? "Resume voice input" : "Pause voice input";
      b.disabled = !known || voiceControl.pending;
      b.title = known ? "Change voice input for this session" : "Voice input control unavailable";
    });
    window.FieldSettings?.voice(paused,known,voiceControl.pending);
    const ms = $("mute-state");
    if (ms) ms.textContent = known ? (paused ? "Paused for this session" : "Allowed for this session") : "Unavailable";
    const sv = $("side-voice");
    if (sv) sv.textContent = known ? (paused ? "paused" : "allowed") : "n/a";
    const chip = $("status-chip");
    if (chip) chip.dataset.paused = paused ? "true" : "false";
    const cp = $("cc-paused");
    if (cp) cp.textContent = known ? (paused ? "Yes" : "No") : DASH;
    const chip2 = $("cc-paused-chip");
    if (chip2) chip2.hidden = !paused;
  }
  async function changeVoiceInput(next) {
    if (!has("set_paused") || voiceControl.pending) return;
    voiceControl.pending = true;
    voiceControl.generation++;
    syncMute();
    window.FieldVoiceScreen?.setting(true);
    let result = {accepted:false, message:"Voice input change was not confirmed."};
    try {
      const r = await call("set_paused", next);
      if (r?.accepted === true && r.paused === next) {
        if (S.status) S.status = {...S.status, paused:next};
        result = {accepted:true, paused:next, client_receipt:r.client_receipt,
          message:r.message || "Session setting accepted. Audio client application unconfirmed."};
      } else if (r?.accepted === false) {
        result.message = "Voice input change failed. The previous setting was kept.";
      }
    } catch (_) { /* No acknowledgement is not success. */ }
    voiceControl.generation++;
    voiceControl.pending = false;
    window.FieldVoiceScreen?.setting(false,result);
    if (result.accepted) window.FieldSettings?.voiceReceipt(result.client_receipt);
    window.FieldSettings?.voiceResult(result);
    syncMute();
    if (activePane !== "view-settings") toast(result.message);
    LAST.get_status = "";
    LAST.get_voice_screen = "";
    refreshChain(["get_status", "get_voice_screen"]);
  }
  function toggleMute() { if (typeof S.status?.paused === "boolean") changeVoiceInput(!S.status.paused); }

  // --- navigation ---
  const PANE_METHODS = {
    "view-command": ["get_core", "get_sys_stats", "get_focus", "get_ear_status", "get_logs"],
    "view-voice": ["get_voice_screen"],
    "view-core": ["get_routing_view"],
    "view-agents": ["get_routing_view"],
    "view-tasks": ["get_tasks", "get_roster"],
    "view-calendar": ["get_schedule"],
    "view-memory": ["get_memory_library"],
    "view-conversations": ["get_conversation_library"],
    "view-knowledge": ["get_knowledge_library"],
    "view-tools": ["get_capability_inventory"],
    "view-workflows": ["get_workflow_view"],
    "view-settings": ["get_startup", "get_settings", "get_voice_screen", "get_n8n"],
    "view-roster": ["get_identity_views"],
    "view-workspace": ["get_roster"],
    "view-lab": [] // Public artwork and dependent Town visual are excluded pending redistribution rights.
  };

  function setNavigation(open, returnFocus) {
    const hud = document.querySelector(".hud");
    const toggle = $("nav-toggle");
    if (!hud || !toggle) return;
    hud.dataset.navOpen = String(!!open);
    toggle.setAttribute("aria-expanded", String(!!open));
    $("nav-backdrop").hidden = !open;
    if (open) {
      const selected = document.querySelector(".sidebar .nav-item.active");
      if (selected) {
        const details = selected.closest("details");
        if (details) details.open = true;
        selected.focus();
        if (selected.dataset.target === "view-command") $("app-nav").scrollTop = 0;
      }
    } else if (returnFocus) toggle.focus();
  }

  function placeComposer(target) {
    const form = $("chat-form");
    const slot = $(target === "view-command" ? "home-composer-slot" : "global-composer-slot");
    if (form && slot && form.parentElement !== slot) slot.prepend(form);
  }

  function activate(target) {
    if (!target || target === activePane) {
      // still refresh if re-clicked
    }
    const pane = $(target);
    if (!pane) return;
    activePane = target;
    if (target === "view-command") $("app-nav").scrollTop = 0;
    document.querySelector(".hud").dataset.pane = target;
    placeComposer(target);
    $$(".nav-item").forEach((n) => {
      const on = n.getAttribute("data-target") === target;
      n.classList.toggle("active", on);
      if (on) {
        n.setAttribute("aria-current", "page");
        $("page-crumb").textContent = n.querySelector(".nav-label").textContent;
        const details = n.closest("details");
        if (details) details.open = true;
      }
      else n.removeAttribute("aria-current");
    });
    $$(".pane").forEach((p) => p.classList.toggle("active", p.id === target));
    // immediate refresh of this pane's data + fast set (chained: never
    // more than one bridge request in flight)
    refreshPane(target);
    refreshChain(["get_status", "get_turn", "get_chat", "get_events"]);
    restartLoops();
    if (target === "view-roster") layoutRoster();
    const main = $("main");
    setNavigation(false, false);
    if (main) { main.scrollTop = 0; main.focus({ preventScroll: true }); }
  }

  function initNav() {
    placeComposer(activePane);
    $("nav-toggle").addEventListener("click", () => {
      setNavigation($("nav-toggle").getAttribute("aria-expanded") !== "true", true);
    });
    $("nav-close").addEventListener("click", () => setNavigation(false, true));
    $("nav-backdrop").addEventListener("click", () => setNavigation(false, true));
    document.addEventListener("keydown", (event) => {
      if ($("nav-toggle").getAttribute("aria-expanded") !== "true") return;
      if (event.key === "Escape") { event.preventDefault(); setNavigation(false, true); }
      if (event.key === "Tab") {
        const items = $$(".sidebar button, .sidebar summary").filter(el => el.getClientRects().length && !el.disabled);
        const index = items.indexOf(document.activeElement);
        if (event.shiftKey && index <= 0) { event.preventDefault(); items[items.length - 1].focus(); }
        else if (!event.shiftKey && index === items.length - 1) { event.preventDefault(); items[0].focus(); }
      }
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 870) setNavigation(false, false);
    });
    $$(".nav-item").forEach((item) => {
      item.addEventListener("click", () => activate(item.getAttribute("data-target")));
    });
    $$("[data-open-pane]").forEach(item => item.addEventListener("click", () => activate(item.dataset.openPane)));
  }

  /* ============================================================
     POLLING — one shared loop, in-flight guard per method
     1s: status / turn / chat / events
     4s: heavier methods, only for the ACTIVE pane
     ============================================================ */
  // --- connection / status state (defined BEFORE the polling slice: the
  //     polling test harness supplies its own setOnline/applyStatus and must
  //     not have these declarations replace them) ---
  let online = false;

  function setOnline(ok) {
    online = !!ok;
    // The visible chip itself is driven by RENDER.get_status -> setConn, so
    // this only publishes a machine-readable state for CSS and the tests.
    try { document.documentElement.dataset.bridge = online ? "online" : "offline"; }
    catch (_) { /* indicator only; never break polling */ }
  }

  function applyStatus(status) {
    // Hand the fresh status straight to the existing renderer without
    // refetching (the poll already paid for it).
    try { if (RENDER && typeof RENDER.get_status === "function") RENDER.get_status(status, "ok"); }
    catch (_) {}
  }

  // --- Polling ---
  let statusInFlight = false;
  let pollStarted = false;
  let pollTimer = 0;
  // pollCount / lastChatKey / lastLogKey must be DECLARED, never assigned
  // implicitly: this file can be evaluated in strict contexts, where an
  // undeclared assignment throws ReferenceError and kills the whole app. A bare
  // `var` also preserves any value a host environment already set for them.
  var pollCount;
  var lastChatKey;
  var lastLogKey;
  if (pollCount === undefined) pollCount = 0;
  if (lastChatKey === undefined) lastChatKey = "";
  if (lastLogKey === undefined) lastLogKey = "";

  function scheduleNext(ms) {
    // Cancel any armed timer first: several entry points (boot, the ready
    // listener, the 400ms fallback, a visibility change) can each ask for the
    // next tick, and stacking timers would make polling run at 2x, then 4x.
    try { if (pollTimer) clearTimeout(pollTimer); } catch (_) {}
    try { pollTimer = setTimeout(pollOnce, ms); } catch (_) { pollTimer = 0; }
  }

  async function pollOnce() {
    // Single-flight: a status request must never overlap another one, and both
    // startup triggers (pywebviewready + the 400ms fallback) must share one.
    if (statusInFlight) return;
    if (typeof document !== "undefined" && document.hidden) { scheduleNext(1000); return; }
    statusInFlight = true;
    try {
      const bridge = api();
      if (!bridge || typeof bridge.get_status !== "function") {
        setOnline(false);
        // Let the renderer paint its own waiting/unavailable state instead of
        // leaving a blank label behind (guarded: absent from the poll harness).
        try { refresh("get_status"); } catch (_) {}
        return;
      }
      let status = null;
      const generation = typeof voiceControl === "undefined" ? null : voiceControl.generation;
      try { status = await bridge.get_status(); setOnline(true); }
      catch (_) {
        setOnline(false);
        try { refresh("get_status"); } catch (_) {}
        return;                                         // bridge down -> offline
      }
      try {
        if (status && generation !== null &&
            (voiceControl.pending || voiceControl.generation !== generation)) status.paused = S.status?.paused;
        if (status) applyStatus(status);
      } catch (_) {} // render failure isolated
      // Fast set: the turn panel, the event rail and the Stop button state all
      // depend on these refreshing every tick, not only when their pane opens.
      try {
        if (typeof refreshChain === "function") {
          // chained (one bridge request at a time - see refreshChain)
          refreshChain(["get_turn", "get_events", "get_speech",
                        "get_chat", "get_logs"]);
        }
      } catch (_) {}
      pollCount += 1;
      try {
        if (typeof PANE_METHODS === "object" && typeof activePane === "string"
            && pollCount % 4 === 0 && typeof refreshPane === "function") {
          refreshPane(activePane);
        }
      } catch (_) {}
    } finally {
      statusInFlight = false;
      scheduleNext(500);   // always re-arm: a failure must be retryable
    }
  }

  function startPolling() {
    if (pollStarted) return;   // both startup triggers share ONE loop
    pollStarted = true;
    pollOnce();
  }
  function stopPolling() {
    try { clearTimeout(pollTimer); } catch (_) {}
    pollTimer = 0;
    pollStarted = false;
  }
  try { window.addEventListener("pywebviewready", () => startPolling()); } catch (_) {}
  try { setTimeout(() => startPolling(), 400); } catch (_) {}  // fallback signal
  try {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { clearTimeout(pollTimer); pollTimer = 0; }
      else { try { restartLoops(); } catch (_) {} pollOnce(); }
    });
  } catch (_) {}

  // --- Settings load ---
  // Settings are fetched on demand when their pane opens (PANE_METHODS); this
  // single boot attempt just means the pane is never a blank placeholder.
  try { refresh("get_settings"); refresh("get_startup"); } catch (_) {}

  /* ============================================================
     GLOBE HERO (command center)
     ============================================================ */
  // Pinned so the low-power/reduced-motion budget stays explicit (and so
  // test_status_ui can assert the declaration instead of a stale literal).
  const PARTICLE_COUNT = 240;
  const globe = {
    cv: null, ctx: null, pts: [], angle: 0.6, pulse: 0,
    raf: 0, running: false, color: "#59d1ff"
  };
  function initGlobe() {
    globe.cv = $("globe");
    if (!globe.cv || !globe.cv.getContext) return;
    globe.ctx = globe.cv.getContext("2d");
    // fibonacci sphere
    const N = PARTICLE_COUNT;
    globe.pts = [];
    for (let i = 0; i < N; i++) {
      const y = 1 - (i / (N - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const th = i * 2.399963229728653; // golden angle
      globe.pts.push({ x: Math.cos(th) * r, y: y, z: Math.sin(th) * r });
    }
    resizeGlobe();
    drawGlobe();
    restartLoops();
  }
  function resizeGlobe() {
    const cv = globe.cv;
    if (!cv || !cv.parentElement) return;
    const rect = cv.parentElement.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.max(1, Math.round(rect.width * dpr));
    cv.height = Math.max(1, Math.round(rect.height * dpr));
    if (globe.ctx) globe.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawGlobe();
  }
  function validColor(c) {
    if (typeof c !== "string") return null;
    if (/^#[0-9a-f]{3,8}$/i.test(c)) return c;
    if (/^rgba?\(/i.test(c)) return c;
    return null;
  }
  function drawGlobe() {
    const ctx = globe.ctx, cv = globe.cv;
    if (!ctx || !cv) return;
    const w = cv.clientWidth || cv.width;
    const h = cv.clientHeight || cv.height;
    if (!w || !h) return;
    const cx = w / 2, cy = h / 2;
    const base = Math.min(w, h) * 0.36;
    const col = globe.color;
    const pulse = globe.pulse;

    ctx.clearRect(0, 0, w, h);

    // outer glow
    const glow = ctx.createRadialGradient(cx, cy, base * 0.2, cx, cy, base * (2.1 + pulse * 0.5));
    glow.addColorStop(0, hexA(col, 0.30 + pulse * 0.35));
    glow.addColorStop(0.5, hexA(col, 0.08));
    glow.addColorStop(1, hexA(col, 0));
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, base * (2.1 + pulse * 0.5), 0, Math.PI * 2);
    ctx.fill();

    // limb ring
    ctx.strokeStyle = hexA(col, 0.55);
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.arc(cx, cy, base * (1 + pulse * 0.05), 0, Math.PI * 2);
    ctx.stroke();

    // latitude ellipse
    ctx.strokeStyle = hexA(col, 0.22);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.ellipse(cx, cy, base, base * 0.32, 0, 0, Math.PI * 2);
    ctx.stroke();

    // points
    const ca = Math.cos(globe.angle), sa = Math.sin(globe.angle);
    for (const p of globe.pts) {
      const x = p.x * ca - p.z * sa;
      const z = p.x * sa + p.z * ca;
      const sx = cx + x * base;
      const sy = cy + p.y * base;
      const depth = (z + 1) / 2; // 0 back .. 1 front
      const a = 0.12 + depth * 0.75;
      const size = (0.7 + depth * 1.5) * (1 + pulse * 0.7);
      ctx.fillStyle = hexA(col, a);
      ctx.beginPath();
      ctx.arc(sx, sy, size, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  function hexA(color, a) {
    if (color.indexOf("#") === 0) {
      let hex = color.slice(1);
      if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
      if (hex.length === 8) hex = hex.slice(0, 6);
      if (hex.length !== 6) return "rgba(89,209,255," + a.toFixed(2) + ")";
      const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
      return "rgba(" + r + "," + g + "," + b + "," + a.toFixed(2) + ")";
    }
    if (color.indexOf("rgba(") === 0 || color.indexOf("rgb(") === 0) return color;
    return "rgba(89,209,255," + a.toFixed(2) + ")";
  }
  function globeLoop(ts) {
    globe.raf = 0;
    if (!motionOK() || document.hidden || activePane !== "view-command") {
      globe.running = false;
      drawGlobe(); // static frame
      return;
    }
    globe.angle += 0.0055;
    globe.pulse *= 0.94;
    if (globe.pulse < 0.004) globe.pulse = 0;
    drawGlobe();
    globe.raf = requestAnimationFrame(globeLoop);
  }
  function startGlobe() {
    if (!globe.ctx || globe.running) return;
    if (!motionOK() || document.hidden || activePane !== "view-command") { drawGlobe(); return; }
    globe.running = true;
    globe.raf = requestAnimationFrame(globeLoop);
  }
  function stopGlobe() {
    if (globe.raf) cancelAnimationFrame(globe.raf);
    globe.raf = 0;
    globe.running = false;
    drawGlobe();
  }
  function restartLoops() { stopLoops(); startLoops(); }
  function stopLoops() { stopGlobe(); stopWorldPulse(); stopLabLoop(); }
  function startLoops() { startGlobe(); startWorldPulse(); startLabLoop(); }

  /* ============================================================
     FRIDAY_EVENT — external event hook (guarded)
     ============================================================ */
  function setCaption(text) {
    const el = $("globe-caption");
    if (el) el.textContent = text == null ? "" : String(text);
  }
  function pulseGlobe(amount) {
    if (!motionOK() || document.hidden) return;
    globe.pulse = Math.min(1, globe.pulse + amount);
    if (activePane === "view-command" && !globe.running) startGlobe();
  }
  function setWaveform(on) {
    const w = $("voice-wave");
    if (w) w.dataset.live = on ? "on" : "off";
  }

  function invalidateSpeechRead() {
    speechGeneration++;
    LAST.get_voice_screen = "";
    LAST.get_speech = "";
    window.FieldVoiceScreen?.playbackPending();
  }

  window.FRIDAY_EVENT = function (ev) {
    try {
      if (!ev || typeof ev !== "object") return;
      const type = String(ev.type || "");
      const data = ev.data;
      switch (type) {
        case "audio_start":
          setWaveform(true);
          setCaption("AUDIO ▸ live");
          pulseGlobe(0.55);
          break;
        case "audio_chunk":
          pulseGlobe(0.3);
          break;
        case "audio_end":
          setWaveform(false);
          setCaption("");
          pulseGlobe(0.1);
          break;
        case "turn_start":
        case "turn_end":
          refresh("get_turn");
          break;
        case "stop":
          if (!data?.utterance || !speech.utterance || data.utterance === speech.utterance) {
            invalidateSpeechRead();
            stopLoops();
            setCaption("");
            setWaveform(false);
            clearSpeechCaption();
            speech.lastBytes = 0;
          }
          syncStopButtons();
          refresh("get_turn");
          refresh("get_speech");
          if (activePane === "view-voice") refresh("get_voice_screen");
          break;
        default:
          break; // unknown events are ignored, never thrown
      }
    } catch (_) {
      /* an odd event must never break the page */
    }
  };

  /* ============================================================
     RENDER: get_status
     ============================================================ */
  RENDER.get_status = function (d, st) {
    if (st === "missing") setConn("missing", "Bridge: unavailable");
    else if (st === "error") setConn("error", "Bridge: error");
    else if (st === "waiting") setConn("wait", "Bridge: waiting");
    else setConn("ok", "Bridge: connected");

    const label = $("sys-state"), dot = $("status-dot");
    if (st === "ok" && d) {
      if (d.sequence < S.status?.sequence) return;
      S.status = d;
      const lbl = d.label || DASH;
      if (label) label.textContent = lbl;
      const c = validColor(d.color) || "var(--cyan)";
      if (dot) dot.style.background = c;
      if (label) label.style.color = c;
      const cl = $("cc-label"), cd = $("cc-detail");
      if (cl) cl.textContent = lbl;
      if (cd) cd.textContent = d.detail == null ? DASH : String(d.detail);
      const sq = $("cc-seq");
      if (sq) sq.textContent = isNum(d.sequence) ? String(d.sequence) : DASH;
      globe.color = validColor(d.color) || "#59d1ff";
      drawGlobe();
    } else {
      S.status = null;
      voiceControl.generation++;
      LAST.get_voice_screen = "";
      LAST.get_routing_view = "";
      window.FieldVoiceScreen?.receive(null,st === "waiting" ? "waiting" : "disconnected");
      window.FieldSettings?.voiceReceipt(null);
      window.FieldRouting?.receive(null,st);
      LAST.get_capability_inventory = "";
      window.FieldCapabilities?.receive(null,st);
      LAST.get_workflow_view = "";
      window.FieldWorkflows?.receive(null,st);
      window.FieldSettings?.disconnected();
      if (label) { label.textContent = st === "waiting" ? "CONNECTING" : "NO BRIDGE"; label.style.color = ""; }
      if (dot) dot.style.background = "";
      const cl = $("cc-label"), cd = $("cc-detail"), sq = $("cc-seq");
      if (cl) cl.textContent = DASH;
      if (cd) cd.textContent = blockText(st, "get_status");
      if (sq) sq.textContent = DASH;
    }
    syncMute();
    syncStopButtons();
  };
  RENDER.get_voice_screen = (data, state) => {
    if (document.documentElement.dataset.bridge === "offline") {
      window.FieldVoiceScreen?.receive(null,"disconnected");
      window.FieldSettings?.voiceReceipt(null);
      return;
    }
    window.FieldVoiceScreen?.receive(data,state);
    window.FieldSettings?.voiceReceipt(state === "ok" ? data?.listening?.client_receipt : null);
  };
  RENDER.get_routing_view = (data,state) => {
    if (document.documentElement.dataset.bridge === "offline") window.FieldRouting?.receive(null,"error");
    else window.FieldRouting?.receive(data,state);
  };
  RENDER.get_capability_inventory = (data,state) => {
    if (document.documentElement.dataset.bridge === "offline") window.FieldCapabilities?.receive(null,"error");
    else window.FieldCapabilities?.receive(data,state);
  };
  RENDER.get_workflow_view = (data,state) => {
    if (document.documentElement.dataset.bridge === "offline") window.FieldWorkflows?.receive(null,"error");
    else window.FieldWorkflows?.receive(data,state);
  };
  function blockText(st, name) {
    if (st === "waiting") return "Waiting for the pywebview bridge…";
    if (st === "missing") return "Unavailable — " + name + "() not wired in this build";
    return "Bridge call failed — will retry.";
  }

  /* ============================================================
     RENDER: get_turn
     ============================================================ */
  RENDER.get_turn = function (d, st) {
    const state = $("turn-state"), lab = $("turn-label"), kv = $("turn-kv"), bar = $("turn-progress");
    if($("home-n8n-review")) $("home-n8n-review").hidden=!(st==="ok"&&d?.n8n_request_pending);
    const approval = $("home-mcp-approval");
    if (approval) {
      const proposal = st === "ok" ? d?.mcp_proposal : null;
      approval.hidden = !proposal;
      if (proposal && approval.dataset.proposal !== proposal.id) {
        approval.dataset.proposal = proposal.id;
        $("home-mcp-tool").textContent = proposal.tool;
        $("home-mcp-args").textContent = proposal.arguments;
        $("home-mcp-status").textContent = "";
        $("home-mcp-allow").disabled = $("home-mcp-deny").disabled = !has("decide_mcp_proposal");
      }
    }
    if (st !== "ok") {
      S.turn = null;
      if (state) { state.textContent = st === "waiting" ? "CONNECTING" : "UNAVAILABLE"; state.dataset.active = "false"; }
      if (lab) lab.textContent = blockText(st, "get_turn");
      if (kv) kv.innerHTML = '<div><dt>State</dt><dd>' + esc(blockText(st, "get_turn")) + "</dd></div>";
      if (bar) bar.dataset.active = "off";
      syncStopButtons();
      return;
    }
    S.turn = d || null;
    const active = !!(d && d.active);
    if (state) {
      state.textContent = active ? "ACTIVE TURN" : "IDLE";
      state.dataset.active = active ? "true" : "false";
    }
    if (lab) lab.textContent = d && d.label != null && d.label !== "" ? String(d.label) : (active ? "Working…" : "No turn in flight");
    if (bar) bar.dataset.active = active ? "on" : "off";
    if (kv) {
      const rows = [
        ["Source", d && d.source != null ? String(d.source) : DASH],
        ["Elapsed", d && isNum(d.elapsed_seconds) ? d.elapsed_seconds.toFixed(1) + " s" : DASH],
        ["Queued", d && typeof d.queued === "number" ? String(d.queued) : (d && d.queued ? "Yes" : "No")],
        ["Utterance", d && d.utterance_id != null ? String(d.utterance_id) : DASH],
        ["Stop available", d && typeof d.stop_available === "boolean" ? (d.stop_available ? "Yes" : "No") : DASH]
      ];
      kv.innerHTML = rows.map((r) => "<div><dt>" + esc(r[0]) + "</dt><dd>" + esc(r[1]) + "</dd></div>").join("");
    }
    syncStopButtons();
  };

  /* ============================================================
     RENDER: get_chat  (+ conversations + transcript)
     ============================================================ */
  function roleMeta(role) {
    const r = String(role || "unknown");
    if (r === "user") return { who: "YOU", cls: "user" };
    if (r === "assistant") return { who: "FRIDAY", cls: "assistant" };
    if (r === "tool") return { who: "TOOL", cls: "tool" };
    return { who: r.toUpperCase(), cls: "" };
  }
  function msgHtml(m) {
    const meta = roleMeta(m && m.role);
    const t = m && m.timestamp ? fmtTime(m.timestamp) : DASH;
    const status = m && m.status ? '<span class="chip chip-mute">' + esc(m.status) + "</span>" : "";
    const kb = m && m.knowledge;
    const refs = Array.isArray(kb?.references) ? kb.references : [];
    const evidence = (kb?.warning ? '<p class="fn-meta">'+esc(kb.warning)+'</p>' : '') +
      (refs.length ? '<details><summary>Local passages supplied</summary><ul>'+refs.map(row=>
        '<li>'+esc(row.title)+' · lines '+esc(row.first_line)+'–'+esc(row.last_line)+
        '<div class="fn-meta">Source '+esc(row.source_id)+' · version '+esc(row.version_id)+'</div></li>').join('')+'</ul></details>' : '');
    return '<div class="msg ' + meta.cls + '">' +
      '<div class="msg-meta"><span class="who">' + esc(meta.who) + "</span><span>" + esc(t) + "</span>" + status + "</div>" +
      "<div>" + esc(m && m.content != null ? m.content : "") + "</div>"+evidence+"</div>";
  }
  RENDER.get_chat = function (d, st) {
    const notice = $("home-chat-update");
    if (st !== "ok") {
      if (notice) { notice.hidden = false; notice.textContent = "Conversation updates are unavailable. Retrying; earlier messages are kept."; }
      const html = blockHtml(st, "get_chat");
      const cc = $("cc-chat");
      if (cc && !Array.isArray(S.chat)) cc.innerHTML = html;
      paintTranscript();
      return;
    }
    if (notice) { notice.hidden = true; notice.textContent = ""; }
    S.chat = Array.isArray(d) ? d : [];
    const html = S.chat.length
      ? S.chat.slice(-60).map(msgHtml).join("")
      : '<div class="empty"><div class="empty-t">No messages yet</div><div class="empty-s">The chat stream is empty. Type below to start a conversation.</div></div>';
    const cc = $("cc-chat");
    if (cc) {
      const top = cc.scrollTop;
      const follow = cc.scrollHeight - cc.clientHeight - top < 60;
      cc.innerHTML = html;
      cc.scrollTop = follow ? cc.scrollHeight : top;
    }
    paintTranscript();
  };

  /* ============================================================
     RENDER: get_events (+ live rail + transcript merge)
     ============================================================ */
  function eventDataText(data) {
    if (data == null) return "";
    if (typeof data === "string") return data;
    if (typeof data === "object") {
      const t = data.text || data.message || data.label || data.detail || data.reason;
      if (t != null) return String(t);
      try { return trun(JSON.stringify(data), 120); } catch (_) { return ""; }
    }
    return String(data);
  }
  // ROUND ZB-C2/C3: caption + globe bounce driven by what the OUTPUT CLIENT
  // acknowledged, never by a TTS request. The server runs muted, so only the
  // client's bytes_played/first_audio tell us anything is actually audible.
  const speech = { lastBytes: 0, utterance: "" };

  function clearSpeechCaption() {
    const el = $("speech-caption");
    if (el) { el.hidden = true; el.textContent = ""; }
  }

  RENDER.get_speech = function (d, st) {
    const el = $("speech-caption");
    if (st !== "ok" || !d || !el) {
      if (st !== "ok") { speech.lastBytes = 0; clearSpeechCaption(); }
      return;
    }
    const stale = !!d.stale;
    const drained = !!d.drained;
    const frames = Array.isArray(d.frames) ? d.frames : [];
    speech.utterance = d.utterance || "";
    const phrase = (stale || drained || !d.playback_recent || !frames.length)
      ? "" : String(frames[frames.length - 1].text || "");
    el.hidden = !phrase;
    el.textContent = phrase;          // visible words == the fed phrase

    const played = Number(d.bytes_played) || 0;
    const previous = speech.lastBytes;
    if (stale || drained) {
      speech.lastBytes = 0;
      pulseGlobe(0);                   // stop the bounce with the audio
      clearSpeechCaption();
      return;
    }
    speech.lastBytes = played;
    // Progress from the device (acknowledged bytes) -> soft bounce.
    if (played > previous && played > 0) pulseGlobe(0.5);
  };

  RENDER.get_events = function (d, st) {
    if (st !== "ok") {
      S.events = null;
      const el = $("cc-events");
      if (el) el.innerHTML = blockHtml(st, "get_events");
      paintTranscript();
      return;
    }
    const list = d && Array.isArray(d.events) ? d.events : (Array.isArray(d) ? d : []);
    S.events = list;
    const el = $("cc-events");
    if (el) {
      if (!list.length) {
        el.innerHTML = '<div class="empty"><div class="empty-t">No events yet</div>' +
          '<div class="empty-s">Live events appear here as they occur.</div></div>';
      } else {
        el.innerHTML = list.slice(0, 80).map((e) => {
          const type = String(e && e.type || "event");
          const warn = /error|fail|stop|warn/i.test(type) ? " warn" : "";
          const txt = eventDataText(e.data);
          return '<div class="ev-item' + warn + '">' +
            '<span class="ev-type">' + esc(type) + "</span>" +
            '<span class="ev-time">' + esc(fmtTime(e.at)) + "</span>" +
            '<span class="ev-text">' + esc(txt || DASH) + "</span></div>";
        }).join("");
      }
    }
    paintTranscript();
  };

  // transcript rail: chat text merged with audio/turn markers by time
  function paintTranscript() {
    const el = $("voice-transcript");
    if (!el) return;
    if (ST.get_chat === undefined && ST.get_events === undefined) {
      el.innerHTML = blockHtml("waiting", "get_chat");
      return;
    }
    const entries = [];
    (S.chat || []).forEach((m) => {
      entries.push({
        at: m && m.timestamp ? new Date(m.timestamp).getTime() : NaN,
        html: msgHtml(m), kind: "chat"
      });
    });
    (S.events || []).forEach((e) => {
      const t = String(e && e.type || "");
      if (!/^(audio_start|audio_end|turn_start|turn_end|stop)$/.test(t)) return;
      const stamp = typeof e.at === "number" ? (e.at > 1e12 ? e.at : e.at > 1e9 ? e.at * 1000 : NaN)
        : (e.at ? new Date(e.at).getTime() : NaN);
      entries.push({
        at: stamp,
        html: '<div class="ev-item"><span class="ev-type">' + esc(t) + "</span>" +
          '<span class="ev-time">' + esc(fmtTime(e.at)) + "</span>" +
          '<span class="ev-text">' + esc(eventDataText(e.data) || DASH) + "</span></div>",
        kind: "ev"
      });
    });
    if (!entries.length) {
      const st = ST.get_chat === "ok" || ST.get_events === "ok" ? "ok" : (ST.get_chat || ST.get_events || "waiting");
      el.innerHTML = st === "ok"
        ? '<div class="empty"><div class="empty-t">Transcript is empty</div>' +
          '<div class="empty-s">Text and audio markers appear here as turns run.</div></div>'
        : blockHtml(st, "get_chat");
      return;
    }
    // stable order: known timestamps first (asc), unknown last in arrival order
    const known = entries.filter((e) => !isNaN(e.at)).sort((a, b) => a.at - b.at);
    const unknown = entries.filter((e) => isNaN(e.at));
    const all = known.concat(unknown).slice(-70);
    el.innerHTML = all.map((e) => e.html).join("");
    el.scrollTop = el.scrollHeight;
  }
  /* ============================================================
     RENDER: get_core  (AI core screen + voice timings + stages)
     ============================================================ */
  const TTS_STAGE_LABEL = { gemini: "Cloud", local: "Local neural", sapi: "Offline", none: "Silent" };
  const CORE_IDS = [
    "cc-timings", "stage-timeline", "core-chain",
    "core-brain-pool", "core-brain-model", "core-vision-model", "core-vision-avail",
    "core-tts-stage", "core-tts-url", "core-tts-degraded", "core-tts-reason", "core-tts-retry",
    "core-voice-turns", "core-voice-first-token", "core-voice-render", "core-uptime",
    "core-pr-available", "core-pr-epoch", "core-pr-hits", "core-pr-misses", "core-pr-last",
    "core-quota", "core-providers", "core-pr-recent"
  ];
  function kvRow(k, v) {
    return "<div><dt>" + esc(k) + "</dt><dd>" + esc(v == null || v === "" ? DASH : v) + "</dd></div>";
  }
  function setText(id, v) {
    const el = $(id);
    if (el) el.textContent = v == null || v === "" ? DASH : String(v);
  }

  function paintHomeState() {
    const el = $("home-turn-state");
    if (!el) return;
    const active = !!(S.turn && S.turn.active);
    el.dataset.active = String(active);
    el.textContent = !S.status ? "Assistant connection unavailable" :
      stopPending ? "Requesting Stop…" : active ? "Working on the current turn…" :
      S.turn && S.turn.queued > 0 ? S.turn.queued + " message(s) queued" : "Write a message to FRIDAY.";
    const conversation = document.querySelector(".home-chat");
    if (conversation) conversation.setAttribute("aria-busy", String(active));
  }

  function paintHomeDuty() {
    const c = S.core && S.core.available !== false ? S.core : null;
    const serving = c && c.authority && c.authority.serving;
    const known = serving && typeof serving.active === "boolean";
    const idle = S.turn && S.turn.active === false;
    if (known && serving.active === false && serving.duty === "brain" && serving.served_by) {
      lastServedBrain = String(serving.served_by);
    }
    let current = !c ? "Runtime report unavailable" : "No current brain duty reported";
    if (known && serving.active && serving.duty === "brain" && !idle) current = serving.served_by || "Model not reported";
    if (!known && c && c.brain && c.brain.pool && c.brain.model && S.turn && S.turn.active) {
      current = c.brain.pool + "/" + c.brain.model;
    }
    if (!S.status) current = "Runtime connection unavailable";
    setText("home-brain-current", current);
    const fallback = c && c.brain && c.brain.last_model;
    setText("home-history-label", lastServedBrain ? "Last served" : fallback ? "Last selected · success not reported" : "Last served");
    setText("home-brain-history", lastServedBrain || (fallback ? [c.brain.last_pool, fallback].filter(Boolean).join("/") : "Not reported yet"));
    setText("home-vision-current", S.status && known && serving.active && serving.duty === "vision" && !idle
      ? serving.served_by || "Model not reported" : "Not reported by this bridge");
  }

  RENDER.get_core = function (d, st) {
    if (st !== "ok") {
      S.core = null;
      paintHomeDuty();
      const html = blockHtml(st, "get_core");
      ["cc-timings", "stage-timeline", "core-chain", "core-quota", "core-providers", "core-pr-recent"]
        .forEach((id) => { const el = $(id); if (el) el.innerHTML = html; });
      ["core-brain-pool", "core-brain-model", "core-vision-model", "core-vision-avail",
        "core-tts-stage", "core-tts-url", "core-tts-degraded", "core-tts-reason", "core-tts-retry",
        "core-voice-turns", "core-voice-first-token", "core-voice-render", "core-uptime",
        "core-pr-available", "core-pr-epoch", "core-pr-hits", "core-pr-misses", "core-pr-last"
      ].forEach((id) => setText(id, DASH));
      paintStages(null);
      paintTimings(null);
      return;
    }
    S.core = d || {};
    paintHomeDuty();
    const c = S.core;

    // --- AI Core screen ---
    const brain = c.brain || {};
    const brainDuty = (c.authority || {}).serving;
    if (brainDuty) {
      const binding = brainDuty.active === true && brainDuty.duty === "brain"
        ? String(brainDuty.served_by || "") : "";
      const separator = binding.indexOf("/");
      setText("core-brain-pool", separator < 0 ? null : binding.slice(0, separator));
      setText("core-brain-model", separator < 0 ? null : binding.slice(separator + 1));
    } else {
      setText("core-brain-pool", brain.pool);
      setText("core-brain-model", brain.model);
    }
    const vis = c.vision || {};
    setText("core-vision-model", vis.model);
    setText("core-vision-avail", typeof vis.available === "boolean" ? (vis.available ? "Yes" : "No") : DASH);

    const tts = c.tts || {};
    const stageKey = tts.stage == null ? null : String(tts.stage);
    const stageLabel = stageKey == null ? DASH
      : (TTS_STAGE_LABEL[stageKey] || stageKey); // unknown raw value shown honestly
    setText("core-tts-stage", stageLabel);
    setText("core-tts-url", tts.url);
    setText("core-tts-degraded", typeof tts.degraded === "boolean" ? (tts.degraded ? "Yes" : "No") : DASH);
    setText("core-tts-reason", tts.reason);
    setText("core-tts-retry", isNum(tts.retry_in_seconds) ? tts.retry_in_seconds.toFixed(0) + " s" : DASH);

    const voice = c.voice || {};
    setText("core-voice-turns", isNum(voice.turns) ? String(voice.turns) : DASH);
    setText("core-voice-first-token", secs(voice.first_token_seconds));
    setText("core-voice-render", secs(voice.render_seconds));
    setText("core-uptime", fmtUptime(c.uptime_seconds));

    const pr = c.prompt_render || {};
    setText("core-pr-available", typeof pr.available === "boolean" ? (pr.available ? "Yes" : "No") : DASH);
    setText("core-pr-epoch", isNum(pr.epoch) ? String(pr.epoch) : DASH);
    setText("core-pr-hits", isNum(pr.hits) ? String(pr.hits) : DASH);
    setText("core-pr-misses", isNum(pr.misses) ? String(pr.misses) : DASH);
    setText("core-pr-last", secs(pr.last_render_seconds));

    const recent = Array.isArray(pr.recent) ? pr.recent : [];
    const prBody = $("core-pr-recent");
    if (prBody) {
      const tb = prBody.querySelector("tbody");
      if (tb) {
        tb.innerHTML = recent.length
          ? recent.slice(0, 10).map((r) =>
              "<tr><td class='mono'>" + esc(isNum(r && r.epoch) ? String(r.epoch) : DASH) + "</td>" +
              "<td class='mono'>" + esc(secs(r && r.seconds)) + "</td>" +
              "<td class='mono'>" + esc(fmtTime(r && r.at)) + "</td></tr>").join("")
          : '<tr><td colspan="3" class="tbl-empty">No recent renders recorded.</td></tr>';
      }
    }

    // quota (shape unknown → generic key/value, never invented)
    const quotaEl = $("core-quota");
    if (quotaEl) {
      const q = c.quota;
      if (q && typeof q === "object" && Object.keys(q).length) {
        quotaEl.innerHTML = Object.keys(q).map((k) => {
          const v = q[k];
          const s = (v && typeof v === "object") ? jsonKey(v) : String(v);
          return kvRow(k, s);
        }).join("");
      } else {
        quotaEl.innerHTML = kvRow("Quota", "No quota data reported.");
      }
    }

    const provBody = $("core-providers");
    if (provBody) {
      const tb = provBody.querySelector("tbody");
      const providers = Array.isArray(c.providers) ? c.providers : [];
      if (tb) {
        tb.innerHTML = providers.length
          ? providers.map((p) => {
              const avail = typeof (p && p.available) === "boolean"
                ? (p.available ? '<span class="chip chip-ok">yes</span>' : '<span class="chip chip-mute">no</span>')
                : esc(DASH);
              return "<tr><td>" + esc(p && p.name != null ? p.name : DASH) + "</td><td>" + avail +
                "</td><td class='mono'>" + esc(isNum(p && p.total) ? String(p.total) : DASH) + "</td></tr>";
            }).join("")
          : '<tr><td colspan="3" class="tbl-empty">No providers reported.</td></tr>';
      }
    }

    // chain with active step marked
    const chainEl = $("core-chain");
    if (chainEl) {
      const chain = Array.isArray(c.chain) ? c.chain : [];
      if (!chain.length) {
        chainEl.innerHTML = '<li class="chain-item">' + esc(blockText(st, "core.chain")) + "</li>";
      } else {
        chainEl.innerHTML = chain.map((step, i) => {
          let name, sub = "", active = false;
          if (step && typeof step === "object") {
            name = step.name || step.label || step.model || step.provider || step.id || jsonKey(step);
            sub = [step.model, step.provider].filter((x) => x && x !== name).join(" · ");
            active = step.active === true || step.current === true || step.selected === true ||
              step.in_use === true || step.is_active === true || step.state === "active";
          } else {
            name = String(step);
          }
          const cls = "chain-item" + (active ? " active" : "");
          return '<li class="' + cls + '"><span class="chain-idx">' + (i + 1) + "</span>" +
            '<span class="chain-name">' + esc(name) + (sub ? ' <span class="chain-sub">' + esc(sub) + "</span>" : "") + "</span>" +
            (active ? '<span class="chip chip-info">active</span>' : "") + "</li>";
        }).join("");
      }
    }

    paintTimings(c);
    paintStages(c);
  };

  function paintTimings(c) {
    const el = $("cc-timings");
    if (!el) return;
    if (!c) { el.innerHTML = blockHtml(ST.get_core || "waiting", "get_core"); return; }
    const voice = c.voice || {};
    const pr = c.prompt_render || {};
    const rows = [
      ["Turns", isNum(voice.turns) ? String(voice.turns) : DASH],
      ["First token", secs(voice.first_token_seconds)],
      ["Voice render", secs(voice.render_seconds)],
      ["Prompt render (last)", secs(pr.last_render_seconds)],
      ["Prompt hits / misses", (isNum(pr.hits) ? pr.hits : DASH) + " / " + (isNum(pr.misses) ? pr.misses : DASH)],
      ["Uptime", fmtUptime(c.uptime_seconds)]
    ];
    el.innerHTML = rows.map((r) => kvRow(r[0], r[1])).join("");
  }

  // Stage timeline: wake → understand → render → first token → first audio.
  // Values come from measured fields only; anything absent is "—".
  function paintStages(c) {
    const el = $("stage-timeline");
    if (!el) return;
    const voice = (c && c.voice) || {};
    const pr = (c && c.prompt_render) || {};
    const rows = [
      { name: "Wake", val: DASH, src: null },
      { name: "Understand", val: DASH, src: null },
      { name: "Render", val: secs(pr.last_render_seconds), src: "prompt_render.last_render_seconds" },
      { name: "First token", val: secs(voice.first_token_seconds), src: "voice.first_token_seconds" },
      { name: "First audio", val: secs(voice.render_seconds), src: "voice.render_seconds" }
    ];
    el.innerHTML = rows.map((r) => {
      const measured = r.val !== DASH;
      const title = r.src ? ' title="' + esc(r.src) + '"' : "";
      return '<li class="stage" data-state="' + (measured ? "measured" : "missing") + '"' + title + ">" +
        '<span class="stage-dot" aria-hidden="true"></span>' +
        '<span class="stage-name">' + esc(r.name) + "</span>" +
        '<span class="stage-val">' + esc(r.val) + "</span></li>";
    }).join("");
    const note = $("stage-note");
    if (note) {
      note.innerHTML = c
        ? "Measured values come from <code>get_core()</code>. Stages marked <b>" + DASH +
          "</b> have no measured field in this build."
        : "No measurements available — <code>get_core()</code> " + esc(blockText(ST.get_core || "waiting", "get_core").toLowerCase()) + ".";
    }
  }

  /* ============================================================
     RENDER: get_sys_stats / get_focus / get_ear_status / get_logs
     ============================================================ */
  RENDER.get_sys_stats = function (d, st) {
    const cpu = $("stat-cpu"), ram = $("stat-ram");
    if (st !== "ok" || !d) {
      if (cpu) cpu.textContent = DASH;
      if (ram) ram.textContent = DASH;
      return;
    }
    if (cpu) cpu.textContent = isNum(d.cpu) ? Math.round(d.cpu) + "%" : DASH;
    if (ram) {
      const rp = d.ram && typeof d.ram === "object" ? d.ram.percent : d.ram;
      ram.textContent = isNum(rp) ? Math.round(rp) + "%" : DASH;
    }
  };
  RENDER.get_focus = function (d, st) {
    const el = $("focus-pill");
    if (!el) return;
    if (st !== "ok" || !d) { el.textContent = DASH; el.title = ""; return; }
    const title = d.title || d.exe || "";
    if (!title) { el.textContent = DASH; el.title = ""; return; }
    el.textContent = trun(title, 30);
    el.title = String(title);
  };
  RENDER.get_ear_status = function (d, st) {
    const el = $("ear-pill");
    if (!el) return;
    if (st !== "ok" || !d) { el.textContent = DASH; return; }
    if (d.enabled === false) el.textContent = "Off";
    else if (d.error) el.textContent = "Error";
    else if (d.social_call_active) el.textContent = "Muted · call";
    else if (d.capturing) el.textContent = "Capturing";
    else el.textContent = "Standby";
  };
  RENDER.get_logs = function (d, st) {
    const el = $("cc-logs");
    if (!el) return;
    if (st !== "ok") { el.innerHTML = blockHtml(st, "get_logs"); return; }
    let lines = [];
    if (Array.isArray(d)) lines = d.map(String);
    else if (typeof d === "string") lines = d.split("\n");
    else if (d && Array.isArray(d.lines)) lines = d.lines.map(String);
    lines = lines.filter((l) => l && l.trim());
    if (!lines.length) {
      el.innerHTML = '<div class="empty"><div class="empty-t">No log lines yet</div>' +
        '<div class="empty-s">The log tail is empty.</div></div>';
      return;
    }
    el.innerHTML = lines.slice(-70).map((l) => {
      const warn = /error|fail|warn|crash|watchdog/i.test(l) ? " warn" : "";
      return '<div class="log-line' + warn + '">' + esc(l) + "</div>";
    }).join("");
    el.scrollTop = el.scrollHeight;
  };

  /* ============================================================
     RENDER: get_ai_agents
     ============================================================ */
  function agentStatus(status) {
    switch (status) {
      case "in-use": return { cls: "chip-info", text: "In use", live: true };
      case "available": return { cls: "chip-ok", text: "Available" };
      case "standby": return { cls: "chip-mute", text: "Configured" };
      case "exhausted": return { cls: "chip-err", text: "Exhausted" };
      case "no-keys": return { cls: "chip-warn", text: "Unconfigured" };
      default:
        if (status == null || status === "") return { cls: "chip-warn", text: "Unconfigured" };
        return { cls: "chip-mute", text: String(status) }; // raw value, never mislabelled
    }
  }
  RENDER.get_ai_agents = function (d, st) {
    if (st !== "ok") {
      const html = blockHtml(st, "get_ai_agents");
      ["agents-summary", "agents-grid"].forEach((id) => { const el = $(id); if (el) el.innerHTML = html; });
      return;
    }
    const agents = d && Array.isArray(d.agents) ? d.agents : [];
    const summary = (d && d.summary) || {};
    const sumEl = $("agents-summary");
    if (sumEl) {
      const pills = [];
      const add = (k, v) => { if (v != null && v !== "") pills.push('<span class="summary-pill"><span class="k">' + esc(k) + '</span><span class="v">' + esc(v) + "</span></span>"); };
      add("Agents", agents.length);
      if (summary.total != null) add("Reported total", summary.total);
      if (summary.active_turn != null) add("Turn", summary.active_turn ? "in progress" : "idle");
      add("Model", summary.active_model);
      add("Provider", summary.active_provider);
      // ROUND LAB-2B: the badge comes from the ONE authority record, not from
      // an independent summary re-derivation. Empty string renders as "none".
      const duty = summary.serving || {};
      add("Current duty", duty.active === true
        ? (duty.duty || "unknown") + " · " + (duty.served_by || "unknown") : "idle");
      if (duty.active !== true && duty.served_by) add("Last served by", duty.served_by);
      add("Auth rev", summary.authority_rev);
      // Chain depth is reported with its own id so the HUD (and its test) can
      // address it directly. Read from the live router, never a fixed tier.
      pills.push('<span class="summary-pill" id="agents-summary-chain"><span class="k">Chain</span><span class="v">'
        + esc(summary.chain_position || "\u2014") + "</span></span>");
      add("Last switch", summary.last_switch);
      sumEl.innerHTML = pills.length ? pills.join("")
        : '<span class="summary-pill"><span class="v">No summary reported.</span></span>';
    }
    const grid = $("agents-grid");
    if (!grid) return;
    if (!agents.length) {
      grid.innerHTML = '<div class="empty"><div class="empty-t">No agents reported</div>' +
        '<div class="empty-s"><code>get_ai_agents()</code> returned an empty agent list. Nothing is shown because none were provided.</div></div>';
      return;
    }
    grid.innerHTML = agents.map((a) => {
      const st2 = agentStatus(a && a.status);
      const cls = "agent-card" + (st2.live ? " is-live" : (a && (a.status === "no-keys" || !a.status) ? " is-off" : ""));
      const specs = [
        ["Provider", a && a.provider, ""],
        ["Model", a && a.model, "model"],
        ["Pool health", a && a.health_desc, ""],
        ["Status", st2.text, ""]
      ].filter((s) => s[1] != null && s[1] !== "");
      return '<div class="' + cls + '">' +
        '<div class="agent-head"><div><div class="agent-name">' + esc(a && a.name != null ? a.name : DASH) + "</div>" +
        '<div class="agent-role">' + esc(a && a.role != null ? a.role : DASH) + "</div></div>" +
        '<span class="chip ' + st2.cls + '">' + esc(st2.text) + "</span></div>" +
        '<div class="spec-grid">' + specs.map((s) =>
          '<div class="spec"><span class="spec-k">' + esc(s[0]) + '</span><span class="spec-v ' + s[2] + '">' +
          esc(s[1]) + "</span></div>").join("") + "</div>" +
        (a && a.task ? '<div class="agent-task">' + esc(a.task) + "</div>" : "") +
        (a && a.description ? '<div class="agent-desc">' + esc(a.description) + "</div>" : "") +
        "</div>";
    }).join("");
  };

  /* ============================================================
     RENDER: get_tasks  (+ approval queue → review_task)
     ============================================================ */
  RENDER.get_tasks = (data, state) => {
    if (window.FieldPlanner) window.FieldPlanner.tasks(data, state);
  };
  async function doReview(taskId, verdict, attempt, btn) {
    if (!has("review_task")) return;
    btn.disabled = true;
    try {
      const result = await call("review_task", taskId, verdict, attempt);
      toast(result && result.ok && result.state
        ? "Task " + result.state + "."
        : ((result && (result.reason || result.error)) || "Review was not saved."));
    } catch (error) {
      toast("Review could not be confirmed.");
    } finally {
      btn.disabled = false;
      LAST.get_roster = "";
      refresh("get_roster");
    }
  }

  /* ============================================================
     RENDER: get_schedule
     ============================================================ */
  RENDER.get_schedule = (data, state) => {
    if (window.FieldPlanner) window.FieldPlanner.calendar(data, state);
  };
  RENDER.get_memory_library = (data, state) => window.FieldLibrary?.receive("memory", data, state);
  RENDER.get_knowledge_library = (data, state) => window.FieldLibrary?.receive("knowledge", data, state);
  RENDER.get_conversation_library = (data, state) => window.FieldLibrary?.receive("conversations", data, state);
  /* ============================================================
     RENDER: get_memory (+ add / update / delete)
     ============================================================ */
  RENDER.get_memory = function (d, st) {
    if (st !== "ok") {
      S.memory = null;
      const rows = $("memory-rows");
      if (rows) rows.innerHTML = '<tr><td colspan="3" class="tbl-empty">' +
        esc(blockText(st, "get_memory")) + "</td></tr>";
      const orbs = $("memory-orbs");
      if (orbs) orbs.innerHTML = blockHtml(st, "get_memory");
      const badge = $("memory-count");
      if (badge) badge.hidden = true;
      return;
    }
    const records = Array.isArray(d) ? d : [];
    S.memory = records;
    const badge = $("memory-count");
    if (badge) { badge.hidden = false; badge.textContent = String(records.length); }

    const orbs = $("memory-orbs");
    if (orbs) {
      if (!records.length) {
        orbs.innerHTML = '<span class="note">No memories stored yet.</span>';
      } else {
        orbs.innerHTML = records.map((r) =>
          '<button type="button" class="orb" data-key="' + esc(r.key) + '" data-content="' +
          esc(r.content) + '" aria-label="Memory: ' + esc(r.key) + '"></button>').join("");
        const tip = $("memory-tooltip");
        orbs.querySelectorAll(".orb").forEach((b) => {
          const show = () => { if (tip) tip.textContent = "[" + b.dataset.key + "] " + b.dataset.content; };
          const hide = () => { if (tip) tip.textContent = "Hover a node to preview it."; };
          b.addEventListener("mouseenter", show);
          b.addEventListener("mouseleave", hide);
          b.addEventListener("focus", show);
          b.addEventListener("blur", hide);
        });
      }
    }

    const rows = $("memory-rows");
    if (rows) {
      if (!records.length) {
        rows.innerHTML = '<tr><td colspan="3" class="tbl-empty">No memories stored.</td></tr>';
      } else {
        rows.innerHTML = records.map((r) =>
          '<tr data-id="' + esc(r.ref || ('mem-' + r.id)) + '">' +
          '<td class="mono" data-field="key" data-full="' + esc(r.key) + '">' + esc(r.key) + "</td>" +
          '<td data-field="content" data-full="' + esc(r.content) + '">' + esc(trun(r.content, 140)) + "</td>" +
          '<td class="mem-actions"><button type="button" class="tiny-btn" data-act="edit">Edit</button> ' +
          '<button type="button" class="tiny-btn del" data-act="del">Delete</button></td></tr>').join("");
        rows.querySelectorAll("[data-act]").forEach((b) => {
          b.addEventListener("click", () => {
            const tr = b.closest("tr");
            if (b.getAttribute("data-act") === "edit") startMemEdit(tr);
            else deleteMem(tr.getAttribute("data-id"));
          });
        });
      }
    }
  };
  function startMemEdit(tr) {
    if (!tr) return;
    const id = tr.getAttribute("data-id");
    const keyTd = tr.querySelector('[data-field="key"]');
    const conTd = tr.querySelector('[data-field="content"]');
    const actTd = tr.querySelector(".mem-actions");
    if (!keyTd || !conTd || !actTd) return;
    keyTd.innerHTML = '<input class="mem-edit" data-f="key" value="' + esc(keyTd.dataset.full || "") + '">';
    conTd.innerHTML = '<input class="mem-edit" data-f="content" value="' + esc(conTd.dataset.full || "") + '">';
    actTd.innerHTML = '<button type="button" class="tiny-btn save" data-act="save">Save</button> ' +
      '<button type="button" class="tiny-btn" data-act="cancel">Cancel</button>';
    const input = keyTd.querySelector("input");
    if (input) input.focus();
    actTd.querySelectorAll("[data-act]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (b.getAttribute("data-act") === "cancel") { LAST.get_memory = ""; refresh("get_memory"); return; }
        const k = (keyTd.querySelector("input") || {}).value;
        const c = (conTd.querySelector("input") || {}).value;
        if (!k || !c) { toast("Both key and content are required."); return; }
        if (!has("update_memory")) { toast("update_memory() not wired in this build"); return; }
        b.disabled = true;
        try {
          const result = await call("update_memory", id, k, c);
          if (!result || result.ok !== true) {
            toast((result && result.reason) || "Memory update failed.");
            return;
          }
          toast("Memory updated.");
          LAST.get_memory = "";
          refresh("get_memory");
        } catch (e) {
          toast(isMissing(e) ? "update_memory() not wired in this build" : "Memory update failed.");
        } finally {
          b.disabled = false;
        }
      });
    });
  }
  async function deleteMem(id) {
    if (!has("delete_memory")) { toast("delete_memory() not wired in this build"); return; }
    try {
      const result = await call("delete_memory", id);
      if (result && result.ok === true) toast("Memory deleted.");
      else toast((result && result.reason) || "Memory deletion failed.");
    } catch (e) {
      toast(isMissing(e) ? "delete_memory() not wired in this build" : "Memory deletion failed.");
    }
    LAST.get_memory = "";
    refresh("get_memory");
  }
  async function addMemory() {
    const k = $("memory-new-key"), c = $("memory-new-content"), st = $("memory-add-status");
    const key = k && k.value.trim(), content = c && c.value.trim();
    if (!key || !content) {
      if (st) { st.textContent = "Both key and content are required."; st.classList.add("err"); }
      return;
    }
    if (!has("add_memory")) {
      if (st) { st.textContent = "add_memory() not wired in this build"; st.classList.add("err"); }
      return;
    }
    if (st) { st.textContent = "Saving…"; st.classList.remove("err"); }
    try {
      const result = await call("add_memory", key, content);
      if (!result || result.ok !== true) {
        if (st) {
          st.textContent = (result && result.reason) || "Memory could not be stored.";
          st.classList.add("err");
        }
        return;
      }
      if (k) k.value = "";
      if (c) c.value = "";
      if (st) { st.textContent = "Memory stored."; st.classList.remove("err"); }
      LAST.get_memory = "";
      refresh("get_memory");
    } catch (e) {
      if (st) {
        st.textContent = isMissing(e) ? "add_memory() not wired in this build" : "add_memory() failed.";
        st.classList.add("err");
      }
    }
  }

  /* ============================================================
     RENDER: get_knowledge / get_project_notes
     ============================================================ */
  function secLabel(key) {
    return String(key).replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }
  function renderSections(obj) {
    const entries = Object.entries(obj).filter(([, v]) =>
      v != null && (Array.isArray(v) ? v.length > 0 : String(v).trim() !== ""));
    if (!entries.length) {
      return '<div class="empty"><div class="empty-t">Empty</div><div class="empty-s">No sections were provided.</div></div>';
    }
    return entries.map(([k, v]) => {
      let body;
      if (Array.isArray(v)) {
        body = v.map((item) => {
          if (item && typeof item === "object") {
            const name = item.name || item.title || item.path || jsonKey(item);
            const kind = item.kind || item.type;
            return '<div class="row-item">' + (kind ? '<span class="chip chip-ok">' + esc(kind) + "</span>" : "") +
              esc(name) + "</div>";
          }
          return '<div class="row-item">' + esc(item) + "</div>";
        }).join("");
      } else if (v && typeof v === "object") {
        body = Object.keys(v).map((kk) =>
          '<div class="row-item"><b>' + esc(secLabel(kk)) + ":</b> " +
          esc(typeof v[kk] === "object" ? jsonKey(v[kk]) : v[kk]) + "</div>").join("");
      } else {
        body = '<div class="row-item">' + esc(v) + "</div>";
      }
      return '<div class="section-card"><div class="section-title">' + esc(secLabel(k)) + "</div>" + body + "</div>";
    }).join("");
  }
  RENDER.get_knowledge = function (d, st) {
    const el = $("kb-sections");
    if (!el) return;
    if (st !== "ok") { el.innerHTML = blockHtml(st, "get_knowledge"); return; }
    if (!d || typeof d !== "object" || (Array.isArray(d) && !d.length)) {
      el.innerHTML = '<div class="empty"><div class="empty-t">Knowledge base is empty</div>' +
        '<div class="empty-s">Nothing has been stored yet.</div></div>';
      return;
    }
    el.innerHTML = renderSections(d);
  };
  RENDER.get_project_notes = function (d, st) {
    const el = $("kb-notes");
    if (!el) return;
    if (st !== "ok") { el.innerHTML = blockHtml(st, "get_project_notes"); return; }
    const text = typeof d === "string" ? d : (d && typeof d.text === "string" ? d.text : null);
    if (text == null || !text.trim()) {
      el.innerHTML = '<span class="note">No project notes recorded.</span>';
      return;
    }
    el.textContent = text;
  };

  /* ============================================================
     RENDER: get_tools / get_skills
     ============================================================ */
  RENDER.get_tools = function (d, st) {
    const el = $("tools-sections");
    if (!el) return;
    if (st !== "ok") { el.innerHTML = blockHtml(st, "get_tools"); return; }
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const keys = Object.keys(d);
      if (keys.length) { el.innerHTML = renderSections(d); return; }
    }
    if (Array.isArray(d) && d.length) {
      el.innerHTML = '<div class="section-card"><div class="section-title">Tools</div>' +
        '<div class="chip-row">' + d.map((t) => '<span class="chip-plain">' +
          esc(typeof t === "string" ? t : (t.name || jsonKey(t))) + "</span>").join("") + "</div></div>";
      return;
    }
    el.innerHTML = '<div class="empty"><div class="empty-t">No tools discovered</div>' +
      '<div class="empty-s">The host reported an empty tool set.</div></div>';
  };
  RENDER.get_skills = function (d, st) {
    const el = $("skills-sections");
    if (!el) return;
    if (st !== "ok") { el.innerHTML = blockHtml(st, "get_skills"); return; }
    const list = Array.isArray(d) ? d : (d && Array.isArray(d.skills) ? d.skills : null);
    if (!list || !list.length) {
      el.innerHTML = '<div class="empty"><div class="empty-t">No skills discovered</div>' +
        '<div class="empty-s">The host reported an empty skill set.</div></div>';
      return;
    }
    el.innerHTML = '<div class="section-card"><div class="section-title">Skills</div><div class="chip-row">' +
      list.map((s) => '<span class="chip-plain">' +
        esc(typeof s === "string" ? s : (s.name || jsonKey(s))) + "</span>").join("") + "</div></div>";
  };

  /* ============================================================
     RENDER: get_startup / get_settings
     ============================================================ */
  RENDER.get_n8n = (d,st) => window.FieldN8N?.receive(d,document.documentElement.dataset.bridge === "offline" ? "error" : st);
  RENDER.get_startup = (d,st) => window.FieldSettings?.startup(d,document.documentElement.dataset.bridge === "offline" ? "error" : st);
  RENDER.get_settings = (d,st) => window.FieldSettings?.host(d,document.documentElement.dataset.bridge === "offline" ? "error" : st);
  /* ============================================================
     RENDER: get_roster  (FUTURE LAB — graph + inspector)
     ============================================================ */
  let rosterSel = null;
  RENDER.get_roster = function (d, st) {
    if (typeof window !== "undefined") {
      if (window.FieldWorkspace) window.FieldWorkspace.render(d, st);
      if (window.FieldPlanner) window.FieldPlanner.workspace(d, st);
    }
    if (st !== "ok") {
      S.roster = null;
      ["roster-stage", "roster-inspector", "roster-artifacts", "roster-approvals",
        "roster-people", "roster-task-status"].forEach((id) => {
        const el = $(id);
        if (el) el.innerHTML = blockHtml(st, "get_roster");
      });
      const sel = $("assign-agent");
      if (sel) sel.innerHTML = '<option value="">—</option>';
      return;
    }
    S.roster = d || {};
    renderRosterChain(d);
    const agents = Array.isArray(d.agents) ? d.agents : [];
    // keep selection if still present, else first agent
    if (!rosterSel || !agents.some((a) => a && a.id === rosterSel)) {
      rosterSel = agents.length && agents[0] ? String(agents[0].id) : null;
    }
    layoutRoster();
    paintRosterInspector();
    renderRosterPeople(d);   // AFTER the selection default (aria-pressed)
    // LAB-3R6: meaningful titles instead of raw ids - map task ids to their
    // goal text and give artifacts an inspectable title + expandable body.
    const taskById = {};
    (Array.isArray(d.tasks) ? d.tasks : []).forEach((t) => {
      if (t && t.id != null) taskById[String(t.id)] = t;
    });
    const shortGoal = (t) => {
      const g = t && t.goal ? String(t.goal) : "";
      return g ? trun(g, 48) : null;
    };
    const shelf = Array.isArray(d.artifacts) ? d.artifacts : [];
    paintRosterList("roster-artifacts", shelf, (a) => {
      const status = a.file_state || "historical";
      const chips = ['<span class="chip chip-mute">' + esc(status === "ok" ? "file present" : status) + "</span>"];
      if (a.ok === false) chips.push('<span class="chip chip-err">failed</span>');
      if (a.error) chips.push('<span class="list-error">' + esc(a.error) + "</span>");
      if (a.file_backed && status === "ok" && has("read_artifact")) {
        chips.push('<button type="button" class="tiny-btn" data-art-toggle="' + esc(a.id) + '">View</button>');
      }
      return chips.join("");
    }, d.artifact_error || "No artifacts recorded.", (a) => {
      const goal = shortGoal(taskById[String(a.task)]);
      return a.label || (goal ? "Result · " + goal : String(a.id || "Artifact"));
    });
    const ra = $("roster-artifacts");
    if (ra) {
      if (shelf.length && d.artifact_error) {
        const warning = document.createElement("div");
        warning.className = "list-error";
        warning.textContent = d.artifact_error;
        ra.prepend(warning);
      }
      ra.querySelectorAll("[data-art-toggle]").forEach((button) => {
        button.addEventListener("click", async () => {
          const holder = button.closest(".list-item");
          if (!holder) return;
          const existing = holder.querySelector(".art-body");
          if (existing) { existing.remove(); button.textContent = "View"; return; }
          button.disabled = true;
          const body = document.createElement("pre");
          body.className = "art-body";
          try {
            const result = await call("read_artifact", button.getAttribute("data-art-toggle"));
            body.textContent = result && result.ok && typeof result.text === "string"
              ? result.text : ((result && (result.reason || result.error)) || "Artifact unavailable.");
          } catch (error) {
            body.textContent = "Artifact could not be read.";
          } finally {
            holder.appendChild(body);
            button.textContent = "Hide";
            button.disabled = false;
          }
        });
      });
    }
    const tasks = (Array.isArray(d.tasks) ? d.tasks : []).slice()
      .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
    paintRosterList("roster-task-status", tasks, (task) => {
      const state = String(task.state || "unknown");
      const retryable = state === "paused" || state === "failed";
      const reason = task.error || task.paused_by;
      const status = '<span class="chip chip-mute">' + esc(state) + "</span>";
      const error = reason ? '<span class="list-error">' + esc(reason) + "</span>" : "";
      const action = has("world_command") && (retryable || state === "assigned") && task.id
        ? '<button type="button" class="tiny-btn" data-run-task="' + esc(task.id) +
          '" data-retry="' + (retryable ? "yes" : "no") + '">' + (retryable ? "Retry" : "Run") + "</button>"
        : "";
      return status + error + action;
    }, "No tasks recorded.", (task) => String(task.goal || task.id));
    const statuses = $("roster-task-status");
    if (statuses) statuses.querySelectorAll("[data-run-task]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await call("world_command", "execute", {
            task_id: button.getAttribute("data-run-task"),
            retry: button.getAttribute("data-retry") === "yes"
          });
          toast(result && result.ok ? "Task admitted." :
            ((result && (result.reason || result.error)) || "Task was not admitted."));
        } catch (error) {
          toast("Task admission could not be confirmed.");
        } finally {
          button.disabled = false;
          LAST.get_roster = "";
          refresh("get_roster");
        }
      });
    });
    paintRosterList("roster-approvals", Array.isArray(d.approvals) ? d.approvals : [], (a) => {
      const chips = [];
      if (a.owner) chips.push('<span class="chip chip-mute">' + esc(a.owner) + "</span>");
      if (a.state) chips.push('<span class="chip chip-warn">' + esc(a.state) + "</span>");
      if (a.state === "awaiting_review" && a.id != null && has("review_task")) {
        chips.push('<button type="button" class="tiny-btn save" data-roster-review="' + esc(a.id) + '" data-attempt="' + esc(a.attempt || "") + '" data-verdict="approve">Approve</button>');
        chips.push('<button type="button" class="tiny-btn del" data-roster-review="' + esc(a.id) + '" data-attempt="' + esc(a.attempt || "") + '" data-verdict="reject">Reject</button>');
      } else if (a.id == null) {
        chips.push('<span class="list-meta">no task id — cannot review</span>');
      }
      return chips.join("");
    }, "No approvals pending.", (a) => {
      const g = shortGoal(a);
      return g || ("approval " + trun(String(a.id != null ? a.id : "?"), 14));
    });
    const ra2 = $("roster-approvals");
    if (ra2) {
      ra2.querySelectorAll("[data-roster-review]").forEach((b) => {
        b.addEventListener("click", () => doReview(b.getAttribute("data-roster-review"), b.getAttribute("data-verdict"), b.getAttribute("data-attempt"), b));
      });
    }
    // assignment select
    const sel = $("assign-agent");
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = agents.length
        ? agents.map((a) => '<option value="' + esc(a.id) + '">' + esc(a.name || a.id) + "</option>").join("")
        : '<option value="">— no agents —</option>';
      if (prev && agents.some((a) => String(a.id) === prev)) sel.value = prev;
      sel.disabled = !agents.length || !has("assign_task");
    }
  };
  function paintRosterList(id, items, chipFn, emptyText, titleFn) {
    const el = $(id);
    if (!el) return;
    if (!items.length) {
      el.innerHTML = '<div class="empty"><div class="empty-t">' + esc(emptyText) + "</div></div>";
      return;
    }
    el.innerHTML = items.map((it) => {
      const label = titleFn ? titleFn(it)
        : (it.label || it.summary || it.title || it.id || jsonKey(it));
      return '<div class="list-item"><span class="grow"><span class="list-title">' + esc(label) + "</span></span>" +
        chipFn(it) + "</div>";
    }).join("");
  }

  /* ============================================================
     RESIDENT IDENTITY — ONE mapping rule shared by all three pages.
     Personal identity (name/role/capabilities) comes from the roster;
     provider/lane facts come from the ONE authority's cast; a resident's
     own life comes from authority.life[<resident id>] (local === null,
     merged by get_lab - the CAST lane's life is a different axis and is
     never borrowed); duty comes from the serving record. Nothing is
     invented: a resident without a dedicated lane says so.
     ============================================================ */
  function brainLanes(auth) {
    const cast = (auth && auth.cast) || {};
    const members = Array.isArray(cast.cast) ? cast.cast : [];
    return members.filter((m) =>
      m && (m.lane === "main_brain" || m.lane === "fallback_brain"));
  }

  function identityFor(agent, auth) {
    const id = String((agent && agent.id) || "");
    const name = String((agent && agent.name) || id);
    if (!auth) {
      auth = (S.lab && S.lab.authority) ||
             (S.roster && S.roster.authority) || null;
    }
    const brain = brainLanes(auth);
    // A resident name or generic ID is not a verified model binding.
    const lane = brain.find((m) => String(m.char_id) === id) || null;
    const castId = lane ? String(lane.char_id) : null;
    const life = (auth && auth.life) || {};
    // a resident's OWN life record rides on get_lab's projection (local null);
    // the roster's authority carries only cast lanes - check both, same source
    const labLife = (S.lab && S.lab.authority && S.lab.authority.life) || {};
    const residentLife = (id && (life[id] || labLife[id])) || null;
    const serving = (auth && auth.serving) || {};
    const onDuty = serving.active === true && !!castId &&
      String(serving.char_id) === castId;
    const duty = onDuty
      ? "serving as FRIDAY now · " + (serving.duty || "capability unknown") +
        (serving.served_by ? " · via " + serving.served_by : "")
      : "not serving";
    const caps = Array.isArray(agent && agent.permissions)
      ? agent.permissions
      : (agent && agent.permissions && typeof agent.permissions === "object"
        ? Object.keys(agent.permissions) : []);
    const laneBinding = lane
      ? ((lane.pool || "?") + ":" + (lane.model || "?")) : "";
    // Without any configured lanes the authority is simply unavailable -
    // say that instead of implying this resident is chain-shared.
    const noAuthority = brain.length === 0;
    return {
      id: id, name: name, role: String((agent && agent.role) || ""),
      caps: caps, castId: castId, lane: lane, life: residentLife,
      duty: duty, onDuty: onDuty,
      // health exists ONLY where a dedicated lane exists
      health: lane ? String(lane.availability || "unknown") : null,
      eligibility: lane
        ? (lane.lane === "main_brain" ? "primary lane" : "fallback lane") +
          " · " + laneBinding
        : (noAuthority ? "authority unavailable" : "shared chain — not dedicated"),
      model: lane ? laneBinding
        : (noAuthority ? "authority unavailable" : "per request from the model chain"),
      lanes: brain.length
    };
  }

  // many-to-many, stated once so the page never reads as three systems
  function chainShareText(d) {
    const agents = (d && Array.isArray(d.agents)) ? d.agents : [];
    const lanes = brainLanes((d && d.authority) || {}).length;
    return "chain shared by " + agents.length + " residents (" +
      lanes + " lanes)";
  }

  function capsHtml(caps) {
    if (!caps || !caps.length) return '<span class="chip chip-mute">none reported</span>';
    return caps.map((p) => '<span class="chip-plain">' + esc(p) + "</span>").join("");
  }

  /* ---- Residents (simulation): compact identity cards (population #1) ---- */
  function renderRosterPeople(d) {
    const el = $("roster-people");
    if (!el) return;
    const agents = (d && Array.isArray(d.agents)) ? d.agents : [];
    if (!agents.length) {
      el.innerHTML = '<div class="empty"><div class="empty-t">No residents yet</div>' +
        '<div class="empty-s">The workforce appears here once agents exist.</div></div>';
      return;
    }
    el.innerHTML = agents.map((a) => {
      const idn = identityFor(a, d.authority);
      const sel = String(a.id) === String(rosterSel);
      const lifeTxt = idn.life
        ? String(idn.life.mode) + (idn.life.source ? " · " + idn.life.source : "")
        : "not reported";
      const dutyShort = !idn.onDuty ? "" :
        '<span class="chip chip-ok">FRIDAY duty</span>';
      const eligCls = idn.lane ? "chip-info" : "chip-mute";
      return '<button type="button" class="person-card' + (sel ? " is-sel" : "") +
        '" data-agent="' + esc(a.id) + '" aria-pressed="' + (sel ? "true" : "false") + '">' +
        '<span class="person-top"><span class="person-name">' + esc(idn.name) + "</span>" +
        '<span class="person-role">' + esc(idn.role) + "</span></span>" +
        '<span class="chip-row person-caps">' + capsHtml(idn.caps) + "</span>" +
        '<span class="person-line">life <b>' + esc(lifeTxt) + "</b>" +
        (idn.health ? " · lane health <b>" + esc(idn.health) + "</b>" : "") + "</span>" +
        '<span class="chip-row"><span class="chip ' + eligCls + '">' +
        esc(idn.eligibility) + "</span>" + dutyShort + "</span>" +
        "</button>";
    }).join("");
    el.querySelectorAll("[data-agent]").forEach((b) => {
      b.addEventListener("click", () => {
        rosterSel = b.getAttribute("data-agent");
        layoutRoster();
        paintRosterInspector();
        renderRosterPeople(S.roster);
      });
    });
  }

  /* ============================================================
     LAB-3R6: the CONFIGURED MODEL CHAIN section on the roster page.
     This is population #2 (provider lanes from the ONE authority) shown
     NEXT TO the workforce graph (population #1: roster agents) so the two
     are never conflated. All values authoritative: pool/model/lane/
     availability/local/life come from derive_cast + snapshot(); nothing
     here invents an "active" status.
     ============================================================ */
  function renderRosterChain(d) {
    const el = $("roster-chain");
    if (!el) return;
    const auth = d && d.authority;
    if (!auth || auth.ok === false) {
      el.innerHTML = '<div class="empty-s">Model chain unavailable — ' +
        esc((auth && (auth.reason || auth.error)) || "authority not configured") +
        "</div>";
      return;
    }
    const cast = auth.cast || {};
    const members = Array.isArray(cast.cast) ? cast.cast : [];
    const life = auth.life || {};
    const serving = auth.serving || {};
    const brain = members.filter((m) =>
      m && (m.lane === "main_brain" || m.lane === "fallback_brain"));
    if (!brain.length) {
      el.innerHTML = '<div class="empty-s">No configured brain lanes ' +
        "(add a provider key; the chain is built from real configuration)" +
        "</div>";
      return;
    }
    const rows = brain.map((m) => {
      const cid = String(m.char_id || "");
      const lm = life[cid] || { mode: m.life_mode || "npc", source: "default",
                                local: !!m.local };
      const isServing = serving.active === true && String(serving.char_id) === cid;
      const avail = String(m.availability || "unknown");
      const chips = [
        '<span class="chip ' + (m.lane === "main_brain" ? "chip-info" : "chip-mute") +
          '">' + (m.lane === "main_brain" ? "primary" : "fallback") + "</span>",
        '<span class="chip ' + (avail === "up" ? "chip-ok"
          : avail === "banned" ? "chip-warn" : "chip-mute") + '">health ' +
          esc(avail) + "</span>",
        '<span class="chip ' + (lm.mode === "ai" ? "chip-ok" : "chip-mute") +
          '">life ' + esc(String(lm.mode)) + "</span>",
      ];
      if (lm.local === true) chips.push('<span class="chip chip-warn">local · never bulk</span>');
      if (isServing) chips.push('<span class="chip chip-ok">SERVING AS FRIDAY</span>');
      const title = (m.pool || "?") + ":" + (m.model || "?") +
        (m.binding ? "" : "");
      return '<div class="list-item"><span class="grow">' +
        '<span class="list-title">' + esc(m.display_name || cid) + "</span>" +
        '<span class="list-meta"> ' + esc(title) + "</span></span>" +
        chips.join("") + "</div>";
    }).join("");
    el.innerHTML = '<div class="field-label">Model chain (providers) — ' +
      chainShareText(d) + "; what answers when a resident needs a model</div>" + rows;
  }

  function layoutRoster() {
    const stage = $("roster-stage");
    if (!stage) return;
    const d = S.roster;
    if (!d) { stage.innerHTML = blockHtml(ST.get_roster || "waiting", "get_roster"); return; }
    const agents = Array.isArray(d.agents) ? d.agents : [];
    const edges = Array.isArray(d.edges) ? d.edges : [];
    if (!agents.length) {
      stage.innerHTML = '<div class="empty" style="position:absolute;inset:12px;"><div class="empty-t">No roster agents</div>' +
        '<div class="empty-s"><code>get_roster().agents</code> is empty.</div></div>';
      return;
    }
    const w = stage.clientWidth || 640;
    const h = stage.clientHeight || 330;
    const cx = w / 2, cy = h / 2;
    const R = Math.max(70, Math.min(w, h) * 0.34);
    const pos = {};
    agents.forEach((a, i) => {
      const ang = -Math.PI / 2 + (i * 2 * Math.PI) / agents.length;
      pos[String(a.id)] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang) };
    });
    // LAB-3R6: the roster WRITES real agent ids now, but legacy rows (the
    // live store had 79 of them) carry display labels ("FRIDAY" = the name,
    // not the id) or the "world" sentinel. Resolve endpoints here so old
    // data draws too: exact id -> case-insensitive NAME match -> the
    // virtual "world" node -> unresolvable (counted honestly below).
    const nameToId = {};
    agents.forEach((a) => {
      const nm = String(a.name || "").toLowerCase();
      if (nm && nameToId[nm] === undefined) nameToId[nm] = String(a.id);
    });
    const resolveEnd = (raw) => {
      const key = String(raw == null ? "" : raw);
      if (!key) return { id: null, noOwner: true };
      if (pos[key]) return { id: key };
      if (key === "world") {
        if (!pos.world) {
          pos.world = { x: cx, y: cy + R + 42, virtual: true };
        }
        return { id: "world" };
      }
      const byName = nameToId[key.toLowerCase()];
      if (byName) return { id: byName };
      return { id: null, noOwner: key === "?" };
    };

    // edges: ONLY from the edges array; unresolved endpoints are counted, never faked
    const lines = [];
    let shown = 0, unknown = 0, noOwner = 0;
    edges.forEach((e) => {
      if (!e) return;
      const ea = resolveEnd(e.from), eb = resolveEnd(e.to);
      if (!ea.id || !eb.id) {
        if (ea.noOwner || eb.noOwner) noOwner += 1;
        else unknown += 1;
        return;
      }
      const a = pos[ea.id], b = pos[eb.id];
      if (!a || !b) { unknown += 1; return; }
      shown += 1;
      const s = String(e.state || e.kind || "other").toLowerCase();
      const cls = /active|running|live/.test(s) ? "active"
        : /pending|queued|waiting/.test(s) ? "pending"
        : /blocked|failed|error/.test(s) ? "blocked" : "other";
      const title = [e.kind, e.state, e.message, e.at ? fmtTime(e.at) : ""].filter(Boolean).join(" · ");
      lines.push('<line class="edge-line ' + cls + '" x1="' + a.x.toFixed(1) + '" y1="' + a.y.toFixed(1) +
        '" x2="' + b.x.toFixed(1) + '" y2="' + b.y.toFixed(1) + '"><title>' + esc(title || "handoff") + "</title></line>");
    });

    const nodes = agents.map((a) => {
      const p = pos[String(a.id)];
      const state = String(a.state || "").toLowerCase();
      const sKey = /blocked|error|fail/.test(state) ? "blocked"
        : /active|busy|working|running/.test(state) ? "active"
        : /pending|queued/.test(state) ? "pending" : "";
      const sel = String(a.id) === rosterSel;
      return '<button type="button" class="roster-node" data-agent="' + esc(a.id) +
        '" aria-pressed="' + (sel ? "true" : "false") + '" style="left:' + p.x.toFixed(1) + "px;top:" + p.y.toFixed(1) + 'px">' +
        "<span>" + esc(a.name || a.id) + "</span>" +
        '<span class="n-state" data-s="' + sKey + '">' + esc(a.state || DASH) + "</span></button>";
    }).join("");
    // the virtual World node (pause/resume handoffs) - drawn, not skipped
    let worldNode = "";
    if (pos.world) {
      worldNode = '<span class="roster-node roster-node-world" style="left:' +
        pos.world.x.toFixed(1) + "px;top:" + pos.world.y.toFixed(1) + 'px">' +
        "<span>World</span><span class=\"n-state\">rest cycle</span></span>";
    }

    const skipped = unknown + noOwner;
    const noteParts = [];
    if (edges.length === 0) {
      noteParts.push('<div class="roster-note">No handoffs recorded yet</div>');
    } else if (skipped > 0) {
      const bits = [];
      if (unknown) bits.push(unknown + " reference unknown agents");
      if (noOwner) bits.push(noOwner + " have no owner recorded");
      noteParts.push('<div class="roster-note">' + shown + " of " + edges.length +
        " handoffs drawn (" + bits.join(", ") + ")</div>");
    }

    stage.innerHTML = '<svg class="roster-svg" width="' + w + '" height="' + h +
      '" aria-hidden="true">' + lines.join("") + "</svg>" + nodes + worldNode +
      noteParts.join("");
    stage.querySelectorAll(".roster-node").forEach((b) => {
      b.addEventListener("click", () => {
        rosterSel = b.getAttribute("data-agent");
        layoutRoster();
        paintRosterInspector();
        renderRosterPeople(S.roster);   // keep the people cards in step
      });
    });
  }
  function paintRosterInspector() {
    const el = $("roster-inspector");
    if (!el) return;
    const d = S.roster;
    if (!d) { el.innerHTML = blockHtml(ST.get_roster || "waiting", "get_roster"); return; }
    const agents = Array.isArray(d.agents) ? d.agents : [];
    const a = agents.find((x) => String(x.id) === String(rosterSel));
    if (!a) {
      el.innerHTML = '<div class="empty"><div class="empty-t">No resident selected</div>' +
        '<div class="empty-s">Pick a resident card or a node in the workforce graph.</div></div>';
      return;
    }
    const budget = a.budget || {};
    // LAB-3R6: "runtime selected" was a seed PLACEHOLDER, not a model.
    // Say what actually happens (the provider is chosen per request from
    // the configured chain) instead of showing a lie/label - unless the
    // authority maps this resident to a REAL dedicated lane.
    const idn = identityFor(a, d.authority);
    const rawModel = String(a.model || "");
    const modelText = idn.lane ? idn.model
      : ((!rawModel || rawModel === "runtime selected")
        ? "per request from the model chain (see chain below)"
        : rawModel);
    // duty is a separate axis from this worker's personal identity
    let dutyText = idn.duty;
    if (idn.duty.indexOf("serving as FRIDAY") === 0) {
      dutyText = "SERVING AS FRIDAY now" +
        idn.duty.slice("serving as FRIDAY now".length);
    }
    let html = '<div class="id-head"><span class="id-name">' + esc(idn.name) +
      '</span><span class="id-role">' + esc(idn.role) + "</span></div>";
    html += '<dl class="kv">';
    html += kvRow("Model", modelText);
    html += kvRow("State", a.state);
    html += kvRow("Current task", a.task);
    html += kvRow("Life", idn.life
      ? String(idn.life.mode) +
        (idn.life.source ? " · " + idn.life.source : "") +
        (idn.life.local === true ? " · local" : "")
      : "not reported");
    html += kvRow("Health", idn.health || "no dedicated lane");
    html += kvRow("FRIDAY duty", dutyText);
    html += kvRow("Eligibility", idn.eligibility);
    html += kvRow("Max steps", budget.max_steps);
    html += kvRow("Max model calls", budget.max_model_calls);
    html += "</dl>";
    html += '<div class="field-label">Capabilities</div><div class="chip-row" style="margin-top:5px;">' +
      capsHtml(idn.caps) + "</div>";
    if (a.last_error) {
      html += '<div class="list-error" style="margin-top:8px;">Last error: ' + esc(a.last_error) + "</div>";
    }
    el.innerHTML = html;
  }
  async function onAssignSubmit(e) {
    e.preventDefault();
    const st = $("assign-status");
    const agentId = $("assign-agent") && $("assign-agent").value;
    const goal = $("assign-goal") && $("assign-goal").value.trim();
    const budgetRaw = $("assign-budget") && $("assign-budget").value;
    if (!agentId) { if (st) { st.textContent = "Select an agent."; st.classList.add("err"); } return; }
    if (!goal) { if (st) { st.textContent = "Enter a goal."; st.classList.add("err"); } return; }
    if (!has("assign_task")) {
      if (st) { st.textContent = "assign_task() not wired in this build"; st.classList.add("err"); }
      return;
    }
    const budget = isNum(Number(budgetRaw)) && Number(budgetRaw) > 0 ? Number(budgetRaw) : 20;
    if (st) { st.textContent = "Assigning…"; st.classList.remove("err"); }
    const submit = e.currentTarget.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      const result = await call("assign_task", agentId, goal, budget);
      const saved = result && result.ok && result.task && result.task.id;
      const execution = saved && result.execution;
      const refused = execution && execution.ok !== true;
      const state = saved && result.task.state;
      const message = !saved
        ? ((result && (result.reason || result.error)) || "Assignment was not confirmed.")
        : refused
          ? "Task saved. " + (execution.reason || execution.error || "Execution was not admitted.")
          : (state ? "Task saved · " + state + "." : "Task saved.");
      if (st) {
        st.textContent = message;
        st.classList.toggle("err", !saved || !!refused);
      }
      if (saved) {
        const input = $("assign-goal");
        if (input) input.value = "";
        toast(message);
      }
    } catch (err) {
      if (st) {
        st.textContent = "Assignment could not be confirmed.";
        st.classList.add("err");
      }
    } finally {
      if (submit) submit.disabled = false;
    }
    LAST.get_roster = "";
    refresh("get_roster");
  }

  /* ============================================================
     RENDER: get_world  (FUTURE LAB — tile map)
     ============================================================ */
  const world = { cv: null, ctx: null, sel: null, raf: 0, running: false, pulse: 0,
                  layout: null,
                  // ONE world, two views: the workspace keeps its OWN camera so
                  // panning the operations view never moves the town view.
                  cam: { x: 0, y: 0, zoom: 2 }, fitted: false, drag: null };
  const NEED_KEYS = ["energy", "nourishment", "social", "curiosity"];

  RENDER.get_world = function (d, st) {
    if (st !== "ok") {
      S.world = null;
      // the map canvas is NOT clobbered here: it is now owned by the shared
      // scene renderer (get_lab), which draws pixels or its own honest
      // message - blanking it on a get_world failure would flicker the map
      // every poll on mixed-version backends.
      ["world-inspector", "world-journal"].forEach((id) => {
        const el = $(id);
        if (el) el.innerHTML = blockHtml(st, "get_world");
      });
      ["world-clock", "world-tick", "world-speed", "world-paused",
        "world-stats-ticks", "world-stats-planner", "world-stats-budget"].forEach((id) => setText(id, DASH));
      const save = $("world-save");
      if (save) save.textContent = blockText(st, "get_world");
      const chips = $("world-chars");
      if (chips) chips.innerHTML = "";
      return;
    }
    S.world = d || {};
    const w = S.world;

    setText("world-clock", w.clock);
    setText("world-tick", isNum(w.tick) ? String(w.tick) : DASH);
    setText("world-speed", isNum(w.speed) ? String(w.speed) + "×" : DASH);
    setText("world-paused", typeof w.paused === "boolean" ? (w.paused ? "PAUSED" : "RUNNING") : DASH);
    const stats = w.stats || {};
    setText("world-stats-ticks", isNum(stats.ticks) ? String(stats.ticks) : DASH);
    setText("world-stats-planner", isNum(stats.planner_calls) ? String(stats.planner_calls) : DASH);
    setText("world-stats-budget", isNum(stats.model_budget_left) ? String(stats.model_budget_left) : DASH);

    const saveEl = $("world-save");
    if (saveEl) {
      const s = w.save;
      if (s && typeof s === "object" && Object.keys(s).length) {
        saveEl.innerHTML = Object.keys(s).map((k) =>
          "<b>" + esc(secLabel(k)) + ":</b> " + esc(typeof s[k] === "object" ? jsonKey(s[k]) : s[k])).join(" · ");
      } else {
        saveEl.textContent = "No save data reported.";
      }
    }

    const chars = Array.isArray(w.characters) ? w.characters : [];
    if (!world.sel || !chars.some((c) => String(c.id) === String(world.sel))) {
      world.sel = chars.length && chars[0] ? String(chars[0].id) : null;
    }

    // character chips (keyboard-accessible selection)
      const chips = $("world-chars");
      if (chips) {
      chips.innerHTML = chars.length ? chars.map((c) => {
        const s = String(c.state || "").toLowerCase();
        const key = /block|error|fail/.test(s) || c.blocked_reason ? "blocked"
          : /active|work|busy|move/.test(s) ? "active" : "idle";
        return '<button type="button" class="char-chip" data-char="' + esc(c.id) +
          '" aria-pressed="' + (String(c.id) === String(world.sel) ? "true" : "false") + '">' +
          '<span class="dot" data-s="' + key + '"></span>' + esc(c.name || c.id) + "</button>";
      }).join("") : '<span class="note">No characters reported.</span>';
      chips.querySelectorAll("[data-char]").forEach((b) => {
        b.addEventListener("click", () => {
          world.sel = b.getAttribute("data-char");
          RENDER.get_world(S.world, "ok");
        });
      });
    }

    // LAB-3R6: the render tail is isolated from refresh()'s catch on
    // purpose - a canvas/inspector fault must never rewrite a HEALTHY
    // payload into ST="error" (that turned into "Bridge call failed" and
    // blanked the transport pills). The payload pills above are already
    // painted from real data; a draw failure now degrades to a drawn-nothing
    // canvas + one console line instead of a fake transport failure.
    try {
      paintWorldInspector();
      paintWorldJournal();
      // canvas lives inside world-canvas-wrap — re-attach if state message replaced it
      ensureCanvas();
      drawWorld();
      restartLoops();
    } catch (renderError) {
      console.error("workspace render failed:", renderError);
    }
  };

  function ensureCanvas() {
    const wrap = $("world-canvas-wrap");
    if (!wrap) return;
    let cv = $("world-canvas");
    if (!cv) {
      wrap.innerHTML = '<canvas id="world-canvas" aria-label="Top-down tile map of the agent world"></canvas>';
      cv = $("world-canvas");
    }
    world.cv = cv;
    world.ctx = cv && cv.getContext ? cv.getContext("2d") : null;
    if (cv && !cv.dataset.bound) {
      cv.dataset.bound = "1";
      // same interaction model as the town canvas: drag to pan, wheel to
      // zoom, tap/click to inspect (selection on pointer-up, <5px = click)
      cv.addEventListener("pointerdown", worldPointerDown);
      cv.addEventListener("pointermove", worldPointerMove);
      cv.addEventListener("pointerup", worldPointerUp);
      cv.addEventListener("pointercancel", worldPointerUp);
      cv.addEventListener("wheel", worldWheel, { passive: false });
    }
  }

  function roomColor(kind) {
    const k = String(kind || "").toLowerCase();
    if (/office|work|studio/.test(k)) return { fill: "#0e2237", edge: "#2b6f96" };
    if (/collab|meeting|conference/.test(k)) return { fill: "#132038", edge: "#5a4fa8" };
    if (/library|archive|study/.test(k)) return { fill: "#12261f", edge: "#2f7d5f" };
    if (/kitchen|cafe|caf|dining|break/.test(k)) return { fill: "#2a2113", edge: "#9a7b3a" };
    if (/bed|sleep|rest|dorm/.test(k)) return { fill: "#1d1730", edge: "#6a54a8" };
    if (/park|garden|green/.test(k)) return { fill: "#12281a", edge: "#3c8a55" };
    if (/street|road|exterior|path/.test(k)) return { fill: "#151a20", edge: "#3a4654" };
    return { fill: "#101d2e", edge: "#2c5f80" };
  }

  function computeLayout() {
    const w = S.world || {};
    const locs = Array.isArray(w.locations) ? w.locations : [];
    const cols = Math.max(1, Math.min(4, Math.ceil(Math.sqrt(locs.length || 1))));
    const rows = Math.max(1, Math.ceil((locs.length || 1) / cols));
    return { locs, cols, rows };
  }

  function drawWorld() {
    ensureCanvas();
    const cv = world.cv, ctx = world.ctx;
    if (!cv || !ctx) return;
    // ONE world, three views: when the lab map exists the workspace draws
    // the SAME pixel scene as Living Town (same art, same positions, same
    // label policy). The schematic room grid below is only the honest
    // fallback for a backend that has no get_lab().
    if (labMap()) {
      drawWorldScene(cv, ctx);
      return;
    }
    // No usable lab map: say so ONLY once we actually know (never while
    // both sources are still waiting for their first answer).
    const labSt = ST.get_lab;
    const note = $("world-scene-note");
    const known = labSt === "missing" || labSt === "error" ||
      (labSt === "ok" && !(S.lab && S.lab.map && S.lab.map.ok));
    if (note) {
      note.hidden = !known;
      if (known) {
        note.textContent = S.world
          ? "Live map unavailable — showing the basic room layout."
          : "Live map unavailable.";
      }
    }
    if (!S.world) return;
    const w = S.world;
    const wrap = cv.parentElement;
    const cssW = (wrap && wrap.clientWidth) || 700;
    const cssH = cssW < 700 ? 320 : 400; // matches the <=900px media query
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
      cv.width = Math.round(cssW * dpr);
      cv.height = Math.round(cssH * dpr);
      cv.style.height = cssH + "px";
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = cssW, H = cssH;
    ctx.clearRect(0, 0, W, H);

    // exterior ground + street + park (map chrome; characters/rooms come from data)
    ctx.fillStyle = "#08111c";
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#0c1420"; // street band bottom
    ctx.fillRect(0, H - 46, W, 46);
    ctx.strokeStyle = "rgba(255,207,107,0.35)";
    ctx.lineWidth = 2;
    ctx.setLineDash([14, 12]);
    ctx.beginPath();
    ctx.moveTo(0, H - 23);
    ctx.lineTo(W, H - 23);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,207,107,0.75)";
    ctx.font = "10px Consolas, monospace";
    ctx.fillText("STREET", 10, H - 8);

    const { locs, cols, rows } = computeLayout();
    const padX = 16, padTop = 16, padBottom = 58;
    const gridW = W - padX * 2, gridH = H - padTop - padBottom;
    const cw = gridW / cols, ch = gridH / rows;
    const rooms = [];

    // paths between room doors (exterior paths)
    ctx.strokeStyle = "rgba(120,188,222,0.25)";
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    for (let i = 0; i < locs.length - 1; i++) {
      const a = roomRect(i, padX, padTop, cw, ch, cols);
      const b = roomRect(i + 1, padX, padTop, cw, ch, cols);
      ctx.beginPath();
      ctx.moveTo(a.x + a.w / 2, a.y + a.h);
      ctx.lineTo(a.x + a.w / 2, a.y + a.h + 14);
      ctx.lineTo(b.x + b.w / 2, a.y + a.h + 14);
      ctx.lineTo(b.x + b.w / 2, b.y + b.h);
      ctx.stroke();
    }
    // park patch top-right when there is room
    if (locs.length && W > 480) {
      ctx.fillStyle = "rgba(60,138,85,0.16)";
      ctx.strokeStyle = "rgba(60,138,85,0.5)";
      ctx.setLineDash([5, 4]);
      ctx.strokeRect(W - 118, padTop, 102, 54);
      ctx.fillRect(W - 118, padTop, 102, 54);
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(140,220,170,0.85)";
      ctx.font = "10px Consolas, monospace";
      ctx.fillText("PARK", W - 108, padTop + 32);
    }

    locs.forEach((loc, i) => {
      const r = roomRect(i, padX, padTop, cw, ch, cols);
      rooms.push({ loc, rect: r });
      const col = roomColor(loc && loc.kind);
      // tile with bevel
      ctx.fillStyle = "rgba(0,0,0,0.35)";
      roundRect(ctx, r.x + 3, r.y + 4, r.w - 8, r.h - 8, 8);
      ctx.fill();
      ctx.fillStyle = col.fill;
      ctx.strokeStyle = col.edge;
      ctx.lineWidth = 1.5;
      roundRect(ctx, r.x, r.y, r.w - 8, r.h - 8, 8);
      ctx.fill();
      ctx.stroke();
      // header band
      ctx.fillStyle = col.edge;
      roundRect(ctx, r.x, r.y, r.w - 8, 22, 8);
      ctx.fill();
      ctx.fillStyle = "#f4f8fb";
      ctx.font = "bold 11px Segoe UI, Arial, sans-serif";
      const name = loc && loc.name ? String(loc.name) : "room " + (i + 1);
      ctx.fillText(trun(name, Math.max(6, Math.floor((r.w - 20) / 6.4))), r.x + 8, r.y + 15);
      // body
      ctx.fillStyle = "rgba(185,200,214,0.9)";
      ctx.font = "10px Consolas, monospace";
      const kind = loc && loc.kind ? String(loc.kind) : DASH;
      ctx.fillText(trun(kind, Math.max(6, Math.floor((r.w - 20) / 5.6))), r.x + 8, r.y + 38);
      if (loc && isNum(loc.occupied) && isNum(loc.capacity)) {
        ctx.fillStyle = loc.occupied >= loc.capacity ? "#ffcf6b" : "#66dfb1";
        ctx.fillText("occupied " + loc.occupied + "/" + loc.capacity, r.x + 8, r.y + 53);
      }
      r.center = { x: r.x + (r.w - 8) / 2, y: r.y + (r.h - 8) / 2 };
      r.loc = loc;
    });

    // characters
    const chars = Array.isArray(w.characters) ? w.characters : [];
    const t = performance.now ? performance.now() : Date.now();
    chars.forEach((c) => {
      let room = null;
      const key = String(c.room != null ? c.room : "");
      room = rooms.find((rr) =>
        rr.loc && (String(rr.loc.id) === key || String(rr.loc.name).toLowerCase() === key.toLowerCase()));
      let x, y;
      if (room) {
        const n = chars.filter((cc) => {
          const k2 = String(cc.room != null ? cc.room : "");
          return room.loc && (String(room.loc.id) === k2 || String(room.loc.name).toLowerCase() === k2.toLowerCase());
        }).length || 1;
        const idx = chars.filter((cc) => cc === c).length; // offset by index below
        const same = chars.filter((cc) => String(cc.room) === String(c.room));
        const j = Math.max(0, same.indexOf(c));
        const spread = Math.min(60, (n - 1) * 18);
        // LAB-3R6 fix: rooms[] entries are {loc, rect} wrappers and .center
        // is set on the RECT (L2047) - reading room.center raised TypeError
        // ("Cannot read properties of undefined (reading 'x')"), the render
        // threw, and refresh() flipped ST to "error": the whole workspace
        // showed "Bridge call failed" and blanked every transport pill
        // while the header (fed by a different path) said "connected".
        const rct = room.rect || room;
        x = rct.center.x - spread / 2 + (n > 1 ? (j * spread) / Math.max(1, n - 1) : 0);
        y = rct.center.y + 14;
        void idx;
      } else {
        x = 26 + (chars.indexOf(c) % 6) * 22;
        y = H - 66;
      }
      c._x = x; c._y = y;
      const selected = String(c.id) === String(world.sel);
      const s = String(c.state || "").toLowerCase();
      const blocked = !!c.blocked_reason || /block|error|fail/.test(s);
      const active = /active|work|busy|move|run/.test(s);
      const fill = blocked ? "#ff6b86" : active ? "#66dfb1" : "#7d93a8";
      if (selected && motionOK() && !document.hidden && activePane === "view-workspace") {
        const pr = 1 + 0.25 * Math.sin(t / 300);
        ctx.strokeStyle = "rgba(89,209,255,0.8)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x, y, 16 * pr, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.fillStyle = fill;
      ctx.beginPath();
      ctx.arc(x, y, 11, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = selected ? "#f4f8fb" : "rgba(5,11,22,0.9)";
      ctx.lineWidth = selected ? 3 : 1.5;
      ctx.stroke();
      ctx.fillStyle = "#04121d";
      ctx.font = "bold 10px Segoe UI, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(trun(String(c.name || c.id || "?"), 1).toUpperCase(), x, y + 3.5);
      ctx.textAlign = "left";
      if (selected) {
        ctx.fillStyle = "#f4f8fb";
        ctx.font = "10px Segoe UI, Arial, sans-serif";
        ctx.fillText(trun(String(c.name || c.id), 18), x + 15, y - 12);
      }
    });

    world.layout = { rooms, chars };
    world.cssW = W;
    world.cssH = H;
  }
  function roomRect(i, padX, padTop, cw, ch, cols) {
    const col = i % cols, row = Math.floor(i / cols);
    return { x: padX + col * cw, y: padTop + row * ch, w: cw, h: ch };
  }
  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }
  /* ---- Agent Workspace view: the SAME pixel scene, its own camera -----
     drawWorldScene sizes the canvas from its CSS box (the schematic
     fallback may have pinned an inline height), then hands the paint to
     the shared scene renderer so both canvases show the identical
     building, positions and label policy. */
  function drawWorldScene(cv, ctx) {
    const note = $("world-scene-note");
    if (note) note.hidden = true;
    if (cv.style.height) cv.style.height = "";   // fluid clamp owns the box
    sceneRender({
      cv: cv, ctx: ctx, cam: world.cam, kind: "world",
      sel: world.sel != null
        ? { kind: "character", id: String(world.sel) } : null
    });
  }

  function worldIsScene() {
    return !!labMap() && labArt.state === "ok";
  }

  function worldSelectAt(sx, sy, ev) {
    if (worldIsScene()) {
      const hit = labPick(sx, sy, world.cam, worldSize());
      if (hit && hit.kind === "character") {
        world.sel = hit.id;
        if (S.world) RENDER.get_world(S.world, "ok");
        else { paintWorldInspector(); drawWorld(); }
      }
      return;
    }
    if (ev) onWorldClick(ev);            // schematic fallback keeps its pick
  }

  function worldPointerDown(e) {
    if (!world.cv) return;
    world.drag = { x: e.clientX, y: e.clientY, moved: 0 };
    try { world.cv.setPointerCapture(e.pointerId); } catch (_) { /* optional */ }
  }
  function worldPointerMove(e) {
    if (!world.drag) return;
    const dx = e.clientX - world.drag.x;
    const dy = e.clientY - world.drag.y;
    world.drag.x = e.clientX;
    world.drag.y = e.clientY;
    world.drag.moved += Math.abs(dx) + Math.abs(dy);
    if (worldIsScene()) { worldPan(dx, dy); drawWorld(); }
    if (e.cancelable) e.preventDefault();
  }
  function worldPointerUp(e) {
    const drag = world.drag;
    world.drag = null;
    try { if (world.cv) world.cv.releasePointerCapture(e.pointerId); } catch (_) { /* optional */ }
    if (drag && drag.moved < 5 && world.cv) {
      const rect = world.cv.getBoundingClientRect();
      worldSelectAt(e.clientX - rect.left, e.clientY - rect.top, e);
    } else {
      drawWorld();
    }
  }
  function worldWheel(e) {
    if (!world.cv || !worldIsScene()) return;   // no camera in the fallback
    e.preventDefault();
    const rect = world.cv.getBoundingClientRect();
    worldZoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15,
                e.clientX - rect.left, e.clientY - rect.top);
    drawWorld();
  }

  function onWorldClick(ev) {
    const cv = world.cv;
    if (!cv || !world.layout) return;
    const rect = cv.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    let best = null, bestD = 22;
    (world.layout.chars || []).forEach((c) => {
      const d = Math.hypot(c._x - mx, c._y - my);
      if (d < bestD) { bestD = d; best = c; }
    });
    if (best) {
      world.sel = String(best.id);
      RENDER.get_world(S.world, "ok");
    }
  }

  function paintWorldInspector() {
    const el = $("world-inspector");
    if (!el) return;
    if (!S.world) { el.innerHTML = blockHtml(ST.get_world || "waiting", "get_world"); return; }
    const chars = Array.isArray(S.world.characters) ? S.world.characters : [];
    const c = chars.find((x) => String(x.id) === String(world.sel));
    if (!c) {
      el.innerHTML = '<div class="empty"><div class="empty-t">No character selected</div>' +
        '<div class="empty-s">Pick a marker on the map or a chip below it.</div></div>';
      return;
    }
    const needs = c.needs || {};
    let html = '<dl class="kv">';
    html += kvRow("Name", c.name);
    html += kvRow("Room", c.room);
    html += kvRow("State", c.state);
    html += kvRow("Goal", c.goal);
    html += kvRow("Task ref", c.task_ref);
    html += "</dl>";
    if (c.blocked_reason) {
      html += '<div class="list-error" style="margin-top:8px;">Blocked: ' + esc(c.blocked_reason) + "</div>";
    }
    html += '<div class="field-label" style="margin-top:10px;">Needs — urgency, fuller bar means more urgent</div>' +
      '<div class="needs" style="margin-top:7px;">';
    let anyNeed = false;
    NEED_KEYS.forEach((k) => {
      if (!isNum(needs[k])) return;
      anyNeed = true;
      const v = Math.max(0, Math.min(100, needs[k]));
      html += '<div class="need-row"><span class="need-name">' + esc(k) + "</span>" +
        '<div class="need-bar"><i style="width:' + v.toFixed(0) + '%"></i></div>' +
        '<span class="need-val">' + v.toFixed(0) + "</span></div>";
    });
    html += "</div>";
    if (!anyNeed) html += '<p class="note" style="margin-top:6px;">No need values reported.</p>';
    const mem = Array.isArray(c.memory) ? c.memory : [];
    html += '<div class="field-label" style="margin-top:12px;">Memories</div>';
    html += mem.length
      ? '<ul class="list" style="margin-top:6px;list-style:none;">' +
        mem.slice(0, 8).map((m) => '<li class="row-item">' + esc(typeof m === "string" ? m : jsonKey(m)) + "</li>").join("") +
        "</ul>"
      : '<p class="note" style="margin-top:6px;">No memories recorded.</p>';
    el.innerHTML = html;
  }

  function srcBadge(ev) {
    const src = ev && (ev.source || ev.origin || ev.driver || ev.by);
    const kind = ev && ev.kind;
    const raw = src != null ? String(src) : (kind != null ? String(kind) : null);
    if (raw == null) return "";
    const cls = /model|llm|ai|agent/i.test(raw) ? "model"
      : /procedural|rule|sim|tick|script|engine/i.test(raw) ? "procedural" : "";
    return '<span class="src-badge ' + cls + '">' + esc(raw) + "</span>";
  }
  function paintWorldJournal() {
    const el = $("world-journal");
    if (!el) return;
    if (!S.world) { el.innerHTML = blockHtml(ST.get_world || "waiting", "get_world"); return; }
    const recent = Array.isArray(S.world.last_events) ? S.world.last_events : [];
    const journal = Array.isArray(S.world.journal) ? S.world.journal : [];
    const renderEv = (e, showTick) => {
      const text = e && (e.text || e.message || e.event || e.description);
      const actor = e && e.actor;
      const tick = e && e.tick;
      return '<div class="journal-item">' +
        (showTick && tick != null ? '<span class="j-tick">t' + esc(tick) + "</span>" : "") +
        srcBadge(e) +
        (actor ? '<span class="src-badge">' + esc(actor) + "</span>" : "") +
        '<span class="j-text">' + esc(text != null ? text : jsonKey(e)) + "</span></div>";
    };
    if (!recent.length && !journal.length) {
      el.innerHTML = '<div class="empty"><div class="empty-t">Journal is empty</div>' +
        '<div class="empty-s">World events will stream here.</div></div>';
      return;
    }
    let html = "";
    if (recent.length) {
      html += '<div class="section-title" style="margin-bottom:6px;">Recent events</div>' +
        recent.slice(-30).reverse().map((e) => renderEv(e, true)).join("");
    }
    if (journal.length) {
      html += '<div class="section-title" style="margin:10px 0 6px;">Journal</div>' +
        journal.slice(-50).map((e) => renderEv(e, true)).join("");
    }
    const scrollTop = el.scrollTop;
    el.innerHTML = html;
    // Recent events are newest-first. Keep the reader's position instead of
    // pushing a fresh journal to its oldest visible rows.
    el.scrollTop = scrollTop;
  }

  // transport
  async function worldCommand(cmd) {
    if (!has("world_command")) { toast("world_command() not wired in this build"); return; }
    let args = {};
    if (cmd === "speed") {
      const cur = S.world && isNum(S.world.speed) ? S.world.speed : 1;
      const order = [1, 2, 4, 0.5];
      const idx = order.indexOf(cur);
      args = { speed: order[(idx + 1) % order.length] };
    }
    try {
      const r = await call("world_command", cmd, args);
      if (r && r.ok === false) {
        toast(r.message || "world_command(" + cmd + ") not accepted.");
      } else {
        if (r && r.world) {
          S.world = r.world;
          LAST.get_world = "";
          RENDER.get_world(r.world, "ok");
        } else {
          LAST.get_world = "";
          refresh("get_world");
        }
        toast("world: " + cmd + (args.speed ? " → " + args.speed + "×" : ""));
      }
    } catch (e) {
      toast(isMissing(e) ? "world_command() not wired in this build" : "world_command(" + cmd + ") failed.");
    }
  }
  function startWorldPulse() {
    if (!$("world-canvas") || !motionOK() || document.hidden || activePane !== "view-workspace") return;
    if (world.running) return;
    world.running = true;
    const step = () => {
      world.raf = 0;
      if (!motionOK() || document.hidden || activePane !== "view-workspace" ||
          (!S.world && !labMap())) {
        world.running = false;
        drawWorld();
        return;
      }
      drawWorld();
      world.raf = requestAnimationFrame(step);
    };
    world.raf = requestAnimationFrame(step);
  }
  function stopWorldPulse() {
    if (world.raf) cancelAnimationFrame(world.raf);
    world.raf = 0;
    world.running = false;
  }

  /* ============================================================
     LIVING TOWN (view-lab) — pixel-art renderer for get_lab().map
     Draw order: ground -> object -> overhead -> characters -> bubbles.
     The camera lives in MAP-PIXEL space and zoom is quantised to whole
     pixels, so nearest-neighbour scaling never leaves seams between tiles.
     ============================================================ */
  const lab = {
    cv: null, ctx: null, raf: 0, running: false, bound: false, wired: false,
    cam: { x: 0, y: 0, zoom: 2 },
    follow: false, sel: null, fitted: false, drag: null
  };
  const LAB_ZOOM_MIN = 1, LAB_ZOOM_MAX = 6;
  const LAB_WALK_MS = 700;          // one interpolation step between polls
  const labArt = { tiles: null, chars: null, sprite: null, state: "idle", error: "" };
  const labAnim = {};               // char id -> target/drawn positions + facing
  let labDrawFailed = false;

  function labMap() {
    const m = S.lab && S.lab.map;
    return m && m.ok ? m : null;
  }

  function labAsset(rel) {
    // Art resolves relative to the UI entry point: index.html sits beside
    // app.js in source/server/ui/, exactly like style.css and app.js do.
    try { return new URL(String(rel), document.baseURI).toString(); }
    catch (_) { return String(rel); }
  }

  function labOverlay(text, kind) {
    const el = $("lab-overlay");
    if (!el) return;
    const span = el.querySelector(".lab-overlay-t");
    if (span) span.textContent = text == null ? "" : String(text);
    el.dataset.kind = text ? (kind || "show") : "hide";
  }

  function labCenterText(ctx, v, text) {
    ctx.fillStyle = "#7895ab";
    ctx.font = "12px Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillText(String(text || ""), v.cx, v.cy);
    ctx.textAlign = "left";
  }

  /* ---------- art loading ---------- */
  function labLoadArt() {
    if (labArt.state === "loading" || labArt.state === "ok") return;
    labArt.state = "loading";
    labOverlay("Loading town…", "show");
    const map = (S.lab && S.lab.map) || {};
    const paths = map.art || {};
    const loadImage = (rel) => new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve({ ok: true, img: img, rel: String(rel) });
      img.onerror = () => resolve({ ok: false, img: null, rel: String(rel) });
      img.src = labAsset(rel);
    });
    const loadManifest = () => new Promise((resolve) => {
      // file:// pages cannot fetch() their own JSON; get_lab() embeds the
      // same manifest, so that is the fallback, not a guess.
      const fallback = paths.sprite || null;
      try {
        fetch(labAsset(paths.manifest || "art/tiles.json"))
          .then((r) => (r && r.ok ? r.json() : Promise.reject(new Error("http " + (r ? r.status : "?")))))
          .then((data) => resolve(data))
          .catch(() => resolve(fallback));
      } catch (_) {
        resolve(fallback);
      }
    });
    Promise.all([
      loadImage(paths.tiles || "art/tiles.png"),
      loadImage(paths.characters || "art/characters.png"),
      loadManifest(),
    ]).then((res) => {
      const broken = [];
      if (res[0].ok) labArt.tiles = res[0].img; else broken.push(res[0].rel);
      if (res[1].ok) labArt.chars = res[1].img; else broken.push(res[1].rel);
      labArt.sprite = res[2] && Array.isArray(res[2].characters) ? res[2] : null;
      if (broken.length) {
        labArt.state = "error";
        labArt.error = "Town art failed to load: " + broken.join(", ");
      } else if (!labArt.sprite) {
        labArt.state = "error";
        labArt.error = "tiles.json unavailable — character sprites cannot be placed.";
      } else {
        labArt.state = "ok";
        labArt.error = "";
      }
      labOverlay(labArt.state === "ok" ? "" : labArt.error, labArt.state === "ok" ? "hide" : "err");
      labPaintToolbar();
      labDraw();
      restartLoops();
    }).catch((err) => {
      labArt.state = "error";
      labArt.error = "Town art failed: " + (err && err.message ? err.message : String(err));
      labOverlay(labArt.error, "err");
      labDraw();
    });
  }

  /* ---------- camera (ONE scene maths, two view cameras) ---------- */
  function sceneSize(cv) {
    const w = (cv && cv.clientWidth) || 0;
    const h = (cv && cv.clientHeight) || 0;
    return { w: w, h: h, cx: w / 2, cy: h / 2 };
  }
  function labSize() { return sceneSize(lab.cv); }
  function worldSize() { return sceneSize(world.cv); }

  // Both canvases use IDENTICAL tile/sprite scaling: same layout maths,
  // same quantised step, only the camera and viewport differ.
  function sceneLayout(cam, v) {
    const map = labMap();
    const ts = map && isNum(map.tile_size) ? Math.max(1, Math.round(map.tile_size)) : 16;
    const zoom = Math.max(LAB_ZOOM_MIN, Math.min(LAB_ZOOM_MAX, cam.zoom));
    const step = Math.max(1, Math.round(ts * zoom));
    const s = step / ts;
    return {
      ts: ts, step: step, s: s, zoom: zoom, v: v,
      ox: Math.round(v.cx - cam.x * s),
      oy: Math.round(v.cy - cam.y * s)
    };
  }
  function labLayout() { return sceneLayout(lab.cam, labSize()); }
  function worldLayout() { return sceneLayout(world.cam, worldSize()); }

  function labToWorld(sx, sy, L) {
    return { x: (sx - L.ox) / L.s, y: (sy - L.oy) / L.s };
  }

  function sceneFit(cam, v) {
    const map = labMap();
    if (!map) return false;
    if (!v.w || !v.h) return false;
    const ts = Math.max(1, Math.round(map.tile_size || 16));
    const mw = Math.max(1, map.width * ts), mh = Math.max(1, map.height * ts);
    const z = Math.min(v.w / mw, v.h / mh);
    cam.zoom = Math.max(LAB_ZOOM_MIN, Math.min(LAB_ZOOM_MAX, z));
    cam.x = mw / 2;
    cam.y = mh / 2;
    return true;
  }
  function labFit() { if (sceneFit(lab.cam, labSize())) lab.fitted = true; }
  function worldFit() { if (sceneFit(world.cam, worldSize())) world.fitted = true; }

  function sceneZoomAt(cam, factor, sx, sy, v) {
    const before = sceneLayout(cam, v);
    const pt = labToWorld(sx, sy, before);
    cam.zoom = Math.max(LAB_ZOOM_MIN, Math.min(LAB_ZOOM_MAX, before.zoom * factor));
    const after = sceneLayout(cam, v);
    // keep the world point that was under the cursor exactly under it
    cam.x = pt.x + (after.v.cx - sx) / after.s;
    cam.y = pt.y + (after.v.cy - sy) / after.s;
  }
  function labZoomAt(factor, sx, sy) {
    sceneZoomAt(lab.cam, factor, sx, sy, labSize());
    labPaintToolbar();
  }
  function worldZoomAt(factor, sx, sy) {
    sceneZoomAt(world.cam, factor, sx, sy, worldSize());
  }

  function scenePan(cam, dxScreen, dyScreen, L) {
    cam.x -= dxScreen / L.s;
    cam.y -= dyScreen / L.s;
  }
  function labPan(dxScreen, dyScreen) {
    scenePan(lab.cam, dxScreen, dyScreen, labLayout());
  }
  function worldPan(dxScreen, dyScreen) {
    scenePan(world.cam, dxScreen, dyScreen, worldLayout());
  }

  /* ---------- data helpers ---------- */
  function labChar(id) {
    const map = labMap();
    if (!map || !Array.isArray(map.characters)) return null;
    id = String(id);
    for (let i = 0; i < map.characters.length; i++) {
      if (String(map.characters[i].id) === id) return map.characters[i];
    }
    return null;
  }

  function labFind(list, id) {
    if (!Array.isArray(list)) return null;
    id = String(id);
    for (let i = 0; i < list.length; i++) {
      if (String(list[i].id) === id) return list[i];
    }
    return null;
  }

  function labBlockRow(char, index) {
    const list = (labArt.sprite && labArt.sprite.characters) || [];
    const id = String(char.id == null ? "" : char.id).toLowerCase();
    const name = String(char.name == null ? "" : char.name).toLowerCase();
    for (let i = 0; i < list.length; i++) {
      const entry = list[i] || {};
      const eid = String(entry.id == null ? "" : entry.id).toLowerCase();
      const ename = String(entry.name == null ? "" : entry.name).toLowerCase();
      if ((eid && eid === id) || (ename && (ename === name || ename === id))) {
        return isNum(entry.block_row) ? entry.block_row : i;
      }
    }
    const byIndex = list[index];
    if (byIndex && isNum(byIndex.block_row)) return byIndex.block_row;
    return 0;
  }

  function labDirIndex(dir) {
    const dirs = (labArt.sprite && labArt.sprite.directions) || ["down", "left", "right", "up"];
    const at = dirs.indexOf(String(dir));
    return at >= 0 ? at : 0;
  }

  /* ---------- character interpolation ---------- */
  function labSyncAnim(chars, ts, origin, now) {
    const seen = {};
    const dur = motionOK() ? LAB_WALK_MS : 0;
    for (let i = 0; i < chars.length; i++) {
      const c = chars[i];
      if (!c || !isNum(c.x) || !isNum(c.y)) continue;
      const id = String(c.id);
      seen[id] = true;
      const tx = (c.x - origin.x) * ts;
      const ty = (c.y - origin.y) * ts;
      const rec = labAnim[id];
      if (!rec) {
        labAnim[id] = { x: tx, y: ty, fx: tx, fy: ty, fromx: tx, fromy: ty,
                        t0: 0, dur: 0, dir: "down", moving: false };
        continue;
      }
      if (rec.x !== tx || rec.y !== ty) {
        rec.fromx = rec.fx;                 // start from where it is DRAWN now
        rec.fromy = rec.fy;
        rec.x = tx; rec.y = ty;
        rec.t0 = now;
        // A poll can skip cells. Only animate a known adjacent move,
        // since drawing the chord of a skipped route can cross walls.
        const adjacent = Math.abs(tx - rec.fromx) + Math.abs(ty - rec.fromy) === ts;
        rec.dur = adjacent ? dur : 0;
        rec.moving = rec.dur > 0;
        const dx = tx - rec.fromx, dy = ty - rec.fromy;
        if (Math.abs(dx) >= Math.abs(dy)) {
          if (dx) rec.dir = dx > 0 ? "right" : "left";
        } else if (dy) {
          rec.dir = dy > 0 ? "down" : "up";
        }
      }
    }
    Object.keys(labAnim).forEach((id) => { if (!seen[id]) delete labAnim[id]; });
  }

  function labCharPos(rec, now) {
    if (!rec.moving || !rec.dur || rec.t0 <= 0) {
      rec.fx = rec.x; rec.fy = rec.y;
      return { x: rec.x, y: rec.y, moving: false };
    }
    const p = Math.min(1, (now - rec.t0) / rec.dur);
    if (p >= 1) {
      rec.moving = false;
      rec.fx = rec.x; rec.fy = rec.y;
      return { x: rec.x, y: rec.y, moving: false };
    }
    const x = rec.fromx + (rec.x - rec.fromx) * p;
    const y = rec.fromy + (rec.y - rec.fromy) * p;
    rec.fx = x; rec.fy = y;
    return { x: x, y: y, moving: true };
  }

  /* ---------- canvas + draw ---------- */
  function labWire() {
    const fit = $("lab-fit");
    if (fit && !fit.dataset.bound) {
      fit.dataset.bound = "1";
      fit.addEventListener("click", () => {
        labFit();
        labPaintToolbar();
        labDraw();
      });
    }
    const follow = $("lab-follow");
    if (follow && !follow.dataset.bound) {
      follow.dataset.bound = "1";
      follow.addEventListener("click", () => {
        lab.follow = !lab.follow;
        follow.setAttribute("aria-pressed", lab.follow ? "true" : "false");
        follow.textContent = lab.follow ? "Follow on" : "Follow off";
        if (lab.follow && (!lab.sel || lab.sel.kind !== "character")) {
          const map = labMap();
          const chars = map && Array.isArray(map.characters) ? map.characters : [];
          if (chars.length) lab.sel = { kind: "character", id: String(chars[0].id) };
          paintLabInspector();
        }
        labDraw();
      });
    }
    // LAB-3R5: bulk "AI powered" - enables eligible NONLOCAL residents
    // only (locals are skipped by the authority; see lab_set_global_ai).
    [["lab-ai-on", true], ["lab-ai-off", false]].forEach(([id, on]) => {
      const btn = $(id);
      if (!btn || btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", async () => {
        let res;
        try {
          res = await call("lab_set_global_ai", on);
        } catch (e) {
          toast("AI switch failed: request error");
          return;
        }
        if (!res || res.ok === false) {
          toast("AI switch failed: " +
            ((res && (res.reason || res.error)) || "unknown error"));
          return;
        }
        const changed = (res.changed || []).length;
        const skipped = (res.skipped_local || []).length;
        toast((on ? "AI on" : "AI off") + ": " + changed + " changed" +
          (skipped ? " · " + skipped + " local skipped" : ""));
        refresh("get_lab");   // read-back from the ONE authority
      });
    });
  }

  function labEnsureCanvas() {
    const cv = $("lab-canvas");
    if (!cv || !cv.getContext) return;
    lab.cv = cv;
    lab.ctx = cv.getContext("2d");
    labWire();
    if (lab.bound) return;
    lab.bound = true;
    cv.addEventListener("pointerdown", labPointerDown);
    cv.addEventListener("pointermove", labPointerMove);
    cv.addEventListener("pointerup", labPointerUp);
    cv.addEventListener("pointercancel", labPointerUp);
    cv.addEventListener("wheel", labWheel, { passive: false });
  }

  function sceneResize(cv) {
    const v = sceneSize(cv);
    if (!cv || !v.w || !v.h) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(v.w * dpr), h = Math.round(v.h * dpr);
    if (cv.width !== w || cv.height !== h) {
      cv.width = w;
      cv.height = h;
    }
  }
  function labResize() { sceneResize(lab.cv); }

  function labDraw() {
    // One log only: a renderer fault must not spam the console every frame.
    try {
      labDrawBody();
      labDrawFailed = false;
    } catch (err) {
      if (!labDrawFailed) {
        labDrawFailed = true;
        try { console.error("[lab] render failed:", err); } catch (_) { /* noop */ }
      }
    }
  }

  /* ---------- ONE scene, drawn into any view canvas --------------------
     view = {cv, ctx, cam, sel, kind}. The Living Town canvas and the
     Agent Workspace canvas both paint through here: identical tiles,
     sprites, positions, scaling and label policy - only the camera and
     viewport differ. Draw order: ground -> object -> overhead -> labels
     -> characters -> bubbles -> name tags. */
  function sceneRender(view) {
    const cv = view.cv, ctx = view.ctx;
    if (!cv || !ctx) return;
    sceneResize(cv);
    const v = sceneSize(cv);
    if (!v.w || !v.h) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, v.w, v.h);
    ctx.fillStyle = "#050b16";                       // never a white canvas
    ctx.fillRect(0, 0, v.w, v.h);

    const map = labMap();
    if (!map) {
      const msg = (S.lab && S.lab.map && S.lab.map.error)
        ? "Town map unavailable: " + S.lab.map.error : "No town data yet.";
      labCenterText(ctx, v, msg);
      return;
    }
    if (view.kind === "lab") { if (!lab.fitted) labFit(); }
    else if (!world.fitted) worldFit();
    if (labArt.state === "error") {
      labCenterText(ctx, v, labArt.error || "Town art failed to load.");
      return;
    }
    if (labArt.state !== "ok" || !labArt.tiles || !labArt.chars) {
      labCenterText(ctx, v, "Loading town…");
      return;
    }

    const ts = Math.max(1, Math.round(map.tile_size || 16));
    const origin = map.origin || { x: 0, y: 0 };
    const chars = Array.isArray(map.characters) ? map.characters : [];
    const now = Date.now();

    labSyncAnim(chars, ts, origin, now);
    if (view.kind === "lab" && lab.follow && lab.sel && lab.sel.kind === "character") {
      const rec = labAnim[String(lab.sel.id)];
      if (rec) { lab.cam.x = rec.fx + ts / 2; lab.cam.y = rec.fy + ts / 2; }
    }

    const L = sceneLayout(view.cam, v);
    const cols = Math.max(1, Math.round(map.columns || 8));
    const topLeft = labToWorld(0, 0, L);
    const bottomRight = labToWorld(v.w, v.h, L);
    const gx0 = Math.max(0, Math.floor(topLeft.x / ts));
    const gy0 = Math.max(0, Math.floor(topLeft.y / ts));
    const gx1 = Math.min(map.width - 1, Math.floor(bottomRight.x / ts));
    const gy1 = Math.min(map.height - 1, Math.floor(bottomRight.y / ts));

    const drawLayer = (name) => {
      const layer = map.layers && map.layers[name];
      if (!Array.isArray(layer)) return;
      for (let gy = gy0; gy <= gy1; gy++) {
        const base = gy * map.width;
        const dy = L.oy + gy * L.step;
        for (let gx = gx0; gx <= gx1; gx++) {
          const idx = layer[base + gx];
          if (!isNum(idx) || idx < 0) continue;
          const sx = (idx % cols) * ts;
          const sy = Math.floor(idx / cols) * ts;
          ctx.drawImage(labArt.tiles, sx, sy, ts, ts,
                        L.ox + gx * L.step, dy, L.step, L.step);
        }
      }
    };
    drawLayer("ground");
    drawLayer("object");
    drawLayer("overhead");

    // --- room labels: REAL rooms only (a rect, not a 1-tile anchor), -----
    // centred INSIDE the rect, word-boundary fitted, dropped when the text
    // cannot fit. Point anchors (desks/beds/parked cars) carry no
    // persistent label at all - fewer, clearer labels beat many, and a
    // selected object/room still shows its name in the inspector + pick
    // outline (labPaintPick), so no text ever floats over a wrong tile.
    // Drawn boxes seed the placer below: no bubble or name tag may ever
    // cover a room label.
    const labelBoxes = [];
    if (L.step >= 13) {
      const roomList = Array.isArray(map.rooms) ? map.rooms : [];
      ctx.font = "9px Segoe UI, Arial, sans-serif";
      ctx.textAlign = "center";
      for (let i = 0; i < roomList.length; i++) {
        const room = roomList[i];
        if (!room || !isNum(room.x) || !isNum(room.y)) continue;
        const rw = Math.max(1, Math.round(room.w || 1));
        const rh = Math.max(1, Math.round(room.h || 1));
        if (rw <= 1 && rh <= 1) continue;          // 1-tile anchor: no label
        const full = String(room.name || room.id || "");
        if (!full) continue;
        const label = fitSceneLabel(ctx, full, rw * L.step - 8);
        if (!label) continue;                      // cannot fit confidently
        const lx = Math.round(L.ox + (room.x - origin.x + rw / 2) * L.step);
        let ly = Math.round(L.oy + (room.y - origin.y) * L.step + 11);
        if (rh === 1) {
          ly = Math.round(L.oy + (room.y - origin.y + 0.5) * L.step + 3);
        }
        if (lx < -80 || ly < -14 || lx > v.w + 80 || ly > v.h + 14) continue;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = "rgba(5, 11, 22, 0.55)";
        ctx.fillRect(Math.round(lx - tw / 2 - 3), Math.round(ly - 9),
                     Math.round(tw + 6), 12);
        ctx.fillStyle = room.tag === "outdoor" ? "rgba(180,230,190,0.9)"
          : "rgba(214,228,240,0.92)";
        ctx.fillText(label, lx, ly);
        labelBoxes.push({ x: Math.round(lx - tw / 2 - 3), y: Math.round(ly - 9),
                          w: Math.round(tw + 6), h: 12 });
      }
      ctx.textAlign = "start";
    }

    // --- characters (sprites first, overlays after, per draw order) -------
    const fw = 16, fh = 24; // frame_size is [16, 24] in tiles.json
    const cw = Math.max(1, Math.round(fw * L.s));
    const ch = Math.max(1, Math.round(fh * L.s));
    const drawn = [];
    for (let i = 0; i < chars.length; i++) {
      const c = chars[i];
      const id = String(c.id);
      const rec = labAnim[id];
      if (!rec) continue;
      const pos = labCharPos(rec, now);
      const row = labBlockRow(c, i);
      const frame = pos.moving ? Math.floor((now - rec.t0) / 150) % 4 : 0;
      const dx = Math.round(L.ox + pos.x * L.s + (L.step - cw) / 2);
      const dy = Math.round(L.oy + pos.y * L.s + L.step - ch);
      ctx.drawImage(labArt.chars, frame * fw, row * fh * 4 + labDirIndex(rec.dir) * fh,
                    fw, fh, dx, dy, cw, ch);
      drawn.push({ c: c, rec: rec, pos: pos, dx: dx, dy: dy, row: row });
    }

    // --- one placer for every overlay box (labels seed it) ---------------
    const boxes = labelBoxes.slice();
    const placeBox = (nx, ny, nw, nh) => {
      let py = Math.round(ny);
      for (let guard = 0; guard < 8; guard++) {
        let hit = null;
        for (let k = 0; k < boxes.length; k++) {
          const b = boxes[k];
          if (Math.abs(b.x - nx) < Math.max(b.w, nw) + 3 &&
              Math.abs(b.y - py) < Math.max(b.h, nh) + 2) { hit = b; break; }
        }
        if (!hit) break;
        py = hit.y - (nh + 2);                    // stack upward, never overlap
        if (py < -16) py = Math.round(ny) + (nh + 3) * (guard + 1);
      }
      const box = { x: Math.round(nx), y: py, w: nw, h: nh };
      boxes.push(box);
      return box;
    };
    const drawTag = (text, cx, cy, isSel, chip) => {
      ctx.font = (chip ? "bold " : "") + "9px Segoe UI, Arial, sans-serif";
      const nw = Math.round(ctx.measureText(text).width + (chip ? 8 : 6));
      const box = placeBox(cx - nw / 2, cy, nw, 12);
      ctx.fillStyle = chip ? "rgba(5, 11, 22, 0.85)" : "rgba(5, 11, 22, 0.72)";
      ctx.fillRect(box.x, box.y, box.w, box.h);
      if (chip) {
        ctx.strokeStyle = "rgba(255, 207, 107, 0.65)";
        ctx.lineWidth = 1;
        ctx.strokeRect(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1);
        ctx.fillStyle = "#ffcf6b";
      } else {
        ctx.fillStyle = isSel ? "#9fe8ff" : "#c6d6e6";
      }
      ctx.fillText(text, box.x + (chip ? 4 : 3), box.y + 9);
    };

    // --- selection + activity bubbles ------------------------------------
    for (let i = 0; i < drawn.length; i++) {
      const d = drawn[i];
      const selected = view.sel && view.sel.kind === "character" &&
        String(view.sel.id) === String(d.c.id);
      if (selected) {
        ctx.strokeStyle = "#42d6ff";
        ctx.lineWidth = 2;
        ctx.strokeRect(Math.round(L.ox + d.pos.x * L.s), Math.round(L.oy + d.pos.y * L.s),
                       L.step, L.step);
      }
      // restrained bubbles: the selected resident always speaks; others
      // only when actually blocked (visible trouble), so four bubbles
      // never crowd the scene.
      const showBubble = selected || !!d.c.blocked_reason;
      let bubble = showBubble ? labBubbleText(d.c) : "";
      if (bubble) {
        ctx.font = "10px Consolas, monospace";
        // the TEXT is trimmed to the box (never the box to the text), so a
        // long action can never spill out of its bubble onto other tiles
        const maxW = Math.max(64, L.step * 4);
        if (ctx.measureText(bubble).width + 12 > maxW) {
          let cut = bubble;
          while (cut.length > 4 &&
                 ctx.measureText(cut + "…").width + 12 > maxW) {
            cut = cut.slice(0, -1);
          }
          bubble = cut + "…";
        }
        const tw = Math.round(ctx.measureText(bubble).width + 12);
        const bx0 = Math.round(d.dx + cw / 2 - tw / 2);
        const by0 = Math.round(d.dy - 20);
        // placer: dodges room labels and other overlay boxes deterministically
        const box = placeBox(bx0, by0, tw, 16);
        ctx.fillStyle = "rgba(5, 11, 22, 0.9)";
        ctx.strokeStyle = "rgba(89, 209, 255, 0.55)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(box.x, box.y, box.w, box.h, 8);
        else ctx.rect(box.x, box.y, box.w, box.h);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#dceeff";
        ctx.fillText(bubble, box.x + 6, box.y + 11);
      }
    }

    // --- name tags: cluster rule -----------------------------------------
    // Residents within 1.5 tiles (ai-town's CONVERSATION_DISTANCE) read as
    // ONE group: only the selected resident keeps a name tag, everyone
    // else collapses into a compact "+N" chip. Every box then goes through
    // one deterministic non-overlap pass, so text never collides.
    const TAG_CLUSTER = 1.5;
    const comps = [];
    const compOf = {};
    for (let i = 0; i < drawn.length; i++) {
      if (compOf[i] !== undefined) continue;
      const ci = comps.length;
      const members = [];
      comps.push(members);
      compOf[i] = ci;
      const stack = [i];
      while (stack.length) {
        const a = stack.pop();
        members.push(a);
        for (let b = 0; b < drawn.length; b++) {
          if (compOf[b] !== undefined) continue;
          const dx = drawn[a].pos.x - drawn[b].pos.x;
          const dy = drawn[a].pos.y - drawn[b].pos.y;
          if (Math.sqrt(dx * dx + dy * dy) <= TAG_CLUSTER * ts) {
            compOf[b] = ci;
            stack.push(b);
          }
        }
      }
    }
    const anchors = comps.map((members) => {
      let miny = Infinity, minx = Infinity, maxb = -Infinity, sx = 0;
      members.forEach((i) => {
        const d = drawn[i];
        miny = Math.min(miny, d.dy);
        minx = Math.min(minx, d.dx);
        maxb = Math.max(maxb, d.dy + ch);
        sx += d.dx + cw / 2;
      });
      return { miny: miny, minx: minx, maxb: maxb, cx: sx / members.length };
    });
    const compOrder = comps.map((_, i) => i).sort((a, b) =>
      (anchors[a].miny - anchors[b].miny) || (anchors[a].minx - anchors[b].minx));
    compOrder.forEach((ci) => {
      const members = comps[ci];
      const a = anchors[ci];
      const selIdx = members.find((i) => view.sel && view.sel.kind === "character" &&
        String(view.sel.id) === String(drawn[i].c.id));
      if (selIdx !== undefined) {
        const d = drawn[selIdx];
        const nm = trun(String(d.c.name || d.c.id || ""), 12);
        if (nm) drawTag(nm, d.dx + cw / 2, d.dy + ch + 2, true, false);
      }
      if (members.length > 1) {
        // one group chip for the whole cluster (selected's tag + "+others")
        const others = selIdx !== undefined ? members.length - 1 : members.length;
        drawTag("+" + others, a.cx, a.maxb + 2, false, true);
      } else if (selIdx === undefined) {
        const d = drawn[members[0]];
        const nm = trun(String(d.c.name || d.c.id || ""), 12);
        if (nm) drawTag(nm, d.dx + cw / 2, d.dy + ch + 2, false, false);
      }
    });

    if (view.kind === "lab") { labPaintPick(L, ts, origin); labPaintToolbar(); }
  }

  // Longest word-boundary prefix of `text` that fits maxW; "" when even the
  // first word cannot - dropping the label beats an unreadable overflow.
  function fitSceneLabel(ctx, text, maxW) {
    const words = String(text).split(/\s+/).filter(Boolean);
    let out = "";
    for (let i = 0; i < words.length; i++) {
      const candidate = out ? out + " " + words[i] : words[i];
      if (ctx.measureText(candidate).width > maxW) break;
      out = candidate;
    }
    return out;
  }

  function labDrawBody() {
    labEnsureCanvas();
    if (!lab.cv || !lab.ctx) return;
    sceneRender({ cv: lab.cv, ctx: lab.ctx, cam: lab.cam, sel: lab.sel,
                  kind: "lab" });
  }

  function labBubbleText(c) {
    const state = c.state == null ? "" : String(c.state).trim();
    if (!state || state.toLowerCase() === "idle") return "";
    const goal = c.goal == null ? "" : String(c.goal).trim();
    return trun(goal ? state + " · " + goal : state, 40);
  }

  function labPaintPick(L, ts, origin) {
    const sel = lab.sel;
    if (!sel) return;
    const map = labMap();
    if (!map) return;
    let gx = null, gy = null, gw = 1, gh = 1, dashed = false;
    if (sel.kind === "character") {
      const rec = labAnim[String(sel.id)];
      if (!rec) return;
      gx = rec.fx / ts; gy = rec.fy / ts;
    } else if (sel.kind === "object") {
      const o = labFind(map.objects, sel.id);
      if (!o) return;
      gx = o.x - origin.x; gy = o.y - origin.y;
    } else if (sel.kind === "room") {
      const r = labFind(map.rooms, sel.id);
      if (!r) return;
      gx = r.x - origin.x; gy = r.y - origin.y;
      gw = Math.max(1, r.w); gh = Math.max(1, r.h);
      dashed = true;
    }
    if (gx == null || gy == null) return;
    ctxStrokePick(L, gx, gy, gw, gh, dashed);
    // A selected object/room is the ONE place a point anchor (desk, bed,
    // parked car) gets a name: centred over its own rect, so label text
    // only ever sits on the thing it names.
    if (sel.kind !== "character") {
      const target = sel.kind === "object"
        ? labFind(map.objects, sel.id) : labFind(map.rooms, sel.id);
      const name = target && (target.name || target.id);
      if (!name) return;
      const ctx = lab.ctx;
      const label = trun(String(name), 18);
      ctx.font = "9px Segoe UI, Arial, sans-serif";
      ctx.textAlign = "center";
      const tw = ctx.measureText(label).width;
      const lx = Math.round(L.ox + (gx + gw / 2) * L.step);
      const ly = Math.round(L.oy + gy * L.step) - 5;
      ctx.fillStyle = "rgba(5, 11, 22, 0.85)";
      ctx.fillRect(Math.round(lx - tw / 2 - 3), Math.round(ly - 9),
                   Math.round(tw + 6), 12);
      ctx.fillStyle = "#9fe8ff";
      ctx.fillText(label, lx, ly);
      ctx.textAlign = "start";
    }
  }

  function ctxStrokePick(L, gx, gy, gw, gh, dashed) {
    const ctx = lab.ctx;
    if (!ctx) return;
    ctx.save();
    ctx.strokeStyle = "#42d6ff";
    ctx.lineWidth = 2;
    if (dashed) ctx.setLineDash([6, 4]);
    ctx.strokeRect(Math.round(L.ox + gx * L.step), Math.round(L.oy + gy * L.step),
                   Math.round(gw * L.step), Math.round(gh * L.step));
    ctx.restore();
  }

  function labPaintToolbar() {
    const map = labMap();
    setText("lab-zoom", map ? (Math.round(lab.cam.zoom * 100) / 100) + "×" : DASH);
    setText("lab-size", map ? map.width + "×" + map.height : DASH);
    const chars = map && Array.isArray(map.characters) ? map.characters.length : null;
    setText("lab-count", chars == null ? DASH : String(chars));
  }

  /* ---------- pointer / wheel / keyboard ---------- */
  function labPointerDown(e) {
    if (!lab.cv) return;
    lab.drag = { x: e.clientX, y: e.clientY, moved: 0 };
    try { lab.cv.setPointerCapture(e.pointerId); } catch (_) { /* optional */ }
  }

  function labPointerMove(e) {
    if (!lab.drag) return;
    const dx = e.clientX - lab.drag.x;
    const dy = e.clientY - lab.drag.y;
    lab.drag.x = e.clientX;
    lab.drag.y = e.clientY;
    lab.drag.moved += Math.abs(dx) + Math.abs(dy);
    labPan(dx, dy);
    labDraw();
    if (e.cancelable) e.preventDefault();
  }

  function labPointerUp(e) {
    const drag = lab.drag;
    lab.drag = null;
    try { if (lab.cv) lab.cv.releasePointerCapture(e.pointerId); } catch (_) { /* optional */ }
    if (drag && drag.moved < 5 && lab.cv) {
      const rect = lab.cv.getBoundingClientRect();
      labSelect(e.clientX - rect.left, e.clientY - rect.top);
    } else {
      labDraw();
    }
  }

  function labWheel(e) {
    if (!lab.cv) return;
    e.preventDefault();
    const rect = lab.cv.getBoundingClientRect();
    labZoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15,
              e.clientX - rect.left, e.clientY - rect.top);
    labDraw();
  }

  function labKeyPan(e) {
    if (activePane !== "view-lab") return;
    const t = e.target;
    if (t && /^(input|textarea|select)$/i.test(t.tagName || "")) return;
    const k = String(e.key || "").toLowerCase();
    let dx = 0, dy = 0;
    if (k === "arrowleft" || k === "a") dx = -1;
    else if (k === "arrowright" || k === "d") dx = 1;
    else if (k === "arrowup" || k === "w") dy = -1;
    else if (k === "arrowdown" || k === "s") dy = 1;
    else return;
    e.preventDefault();
    const L = labLayout();
    const stepPx = 48 / L.s;
    lab.cam.x += dx * stepPx;
    lab.cam.y += dy * stepPx;
    labDraw();
  }
  try { document.addEventListener("keydown", labKeyPan); } catch (_) { /* noop */ }
  try { window.addEventListener("resize", () => {
  // LAB-3R6: a fit taken at another window size used to be latched forever
  // (lab.fitted never cleared; only labDraw ran) -> a 760-wide fit zoomed
  // to 1.05 left a 374x323 map floating in a much larger canvas after
  // resizing ("tiny map in a big empty viewport"). Re-fit on resize; a
  // manual pan/zoom made BEFORE the resize is intentionally re-based (the
  // user is changing the stage, not the camera target).
  lab.fitted = false;
  labFit();
  labDraw();
}); } catch (_) { /* noop */ }

  /* ---------- picking + inspector ---------- */
  function labPick(sx, sy, cam, v) {
    const map = labMap();
    if (!map) return null;
    const L = sceneLayout(cam || lab.cam, v || labSize());
    const w = labToWorld(sx, sy, L);
    const ts = L.ts;
    const origin = map.origin || { x: 0, y: 0 };
    const wx = Math.floor(w.x / ts) + origin.x;
    const wy = Math.floor(w.y / ts) + origin.y;

    // 1) topmost character under the pointer
    const chars = Array.isArray(map.characters) ? map.characters : [];
    let best = null, bestD = Infinity;
    for (let i = 0; i < chars.length; i++) {
      const c = chars[i];
      if (!isNum(c.x) || !isNum(c.y)) continue;
      const rec = labAnim[String(c.id)];
      const px = (rec ? rec.fx : (c.x - origin.x) * ts) + ts / 2;
      const py = (rec ? rec.fy : (c.y - origin.y) * ts) + ts / 2;
      const d = Math.sqrt((w.x - px) * (w.x - px) + (w.y - py) * (w.y - py));
      if (d <= bestD) { bestD = d; best = c; }
    }
    if (best && bestD <= ts * 0.9) return { kind: "character", id: String(best.id) };

    // 2) object on that cell
    const objects = Array.isArray(map.objects) ? map.objects : [];
    for (let i = 0; i < objects.length; i++) {
      const o = objects[i];
      if (Number(o.x) === wx && Number(o.y) === wy) return { kind: "object", id: String(o.id) };
    }

    // 3) room containing the cell
    const rooms = Array.isArray(map.rooms) ? map.rooms : [];
    for (let i = 0; i < rooms.length; i++) {
      const r = rooms[i];
      if (wx >= r.x && wx < r.x + r.w && wy >= r.y && wy < r.y + r.h) {
        return { kind: "room", id: String(r.id) };
      }
    }
    return null;
  }

  function labSelect(sx, sy) {
    lab.sel = labPick(sx, sy);
    paintLabInspector();
    labDraw();
  }

  // ROUND LAB-3 ROUND-2: character memory inspector — the REAL bridge round trip.
  // ---- character memory inspector (LAB-3R5) ------------------------------
  // Typed refs (exp-/mem-/evt-) are the ONLY mutation keys: three tables
  // share bare integer ids, so row.id is never sent to a mutation call.
  // window._labEditing = {charId, ref, value} is the editor draft; it
  // survives the 4s inspector repaint, character switches (restored when
  // the owner character is shown again) and slow typing. window._labMemGen
  // discards stale async responses. Every ok:false is surfaced via toast.
  window._labEditing = null;
  window._labMemGen = 0;                     // MUST start numeric (NaN guard)
  window._labRowTexts = {};
  window._labRenderedChar = null;
  async function loadLabCharMemory(charId) {
    const target = document.getElementById("lab-char-mem-l");
    if (!target) return;
    window._labMemChar = charId;
    const gen = ++window._labMemGen;
    let result;
    try {
      result = await call("get_character_memory", charId, 12);
    } catch (e) {
      if (gen !== window._labMemGen) return;   // superseded by a newer load
      target.innerHTML = '<div class="list-error">Memory load failed: ' +
        esc(String(e)) + "</div>";
      return;
    }
    // stale guard: a newer load started, or the selection changed mid-flight
    if (gen !== window._labMemGen || window._labMemChar !== charId) return;
    if (!result || result.ok === false) {
      target.innerHTML = '<div class="empty-s">' +
        esc((result && result.reason) || "memory unavailable") + "</div>";
      return;
    }
    const sb = result.scrapbook || {};
    const experiences = Array.isArray(sb.experiences) ? sb.experiences :
      (Array.isArray(result.experiences) ? result.experiences : []);
    const rows = experiences.concat(Array.isArray(sb.events) ? sb.events : []);
    if (!rows.length) {
      target.innerHTML = '<div class="empty-s">No memories recorded.</div>';
      return;
    }
    const editing = (window._labEditing &&
                     window._labEditing.charId === charId)
      ? window._labEditing : null;
    let html = '<div class="list">';
    window._labRowTexts = {};               // ref -> original text
    rows.forEach(function (row) {
      const ref = String(row.ref || "");    // typed or empty (never bare id)
      const kind = esc(row.kind || "entry");
      const raw = String(row.text || row.content || "");
      const text = esc(raw.substring(0, 200));
      const at = row.at ? new Date(row.at * 1000).toLocaleTimeString() :
        (row.created_at || "");
      const pinChip = row.pinned
        ? ' <span class="chip">pinned</span>' : "";
      let controls = "";
      if (ref.indexOf("exp-") === 0) {
        window._labRowTexts[ref] = raw;
        const open = !!(editing && editing.ref === ref);
        const pinBtn = row.pinned
          ? '<button type="button" class="btn" style="font-size:9px;padding:2px 6px;" onclick="window._labUnpinMem(\'' + esc(ref) + '\')">unpin</button> '
          : '<button type="button" class="btn" style="font-size:9px;padding:2px 6px;" onclick="window._labPinMem(\'' + esc(ref) + '\')">pin</button> ';
        controls = '<div style="margin-top:4px;">' + pinBtn +
          '<button type="button" class="btn" style="font-size:9px;padding:2px 6px;" onclick="window._labForgetMem(\'' + esc(ref) + '\')">forget</button> ' +
          '<button type="button" class="btn" style="font-size:9px;padding:2px 6px;" onclick="window._labCorrectMem(\'' + esc(ref) + '\')">correct</button>' +
          '</div>' +
          '<div id="lab-cor-' + esc(ref) + '" style="display:' +
          (open ? "block" : "none") + ';margin-top:4px;">' +
          '<input type="text" id="lab-cortext-' + esc(ref) +
          '" style="width:95%;font-size:10px;background:rgba(20,30,45,0.8);color:#cde;border:1px solid rgba(80,110,140,0.5);padding:3px;" />' +
          '<div style="margin-top:3px;">' +
          '<button type="button" class="btn" style="font-size:9px;padding:2px 6px;" onclick="window._labCorrectSave(\'' + esc(ref) + '\')">save</button> ' +
          '<button type="button" class="btn" style="font-size:9px;padding:2px 6px;" onclick="window._labCorrectCancel(\'' + esc(ref) + '\')">cancel</button>' +
          '</div></div>';
      } else if (ref.indexOf("evt-") === 0) {
        controls = '<div style="margin-top:4px;"><span class="chip">journal · read-only</span></div>';
      } else if (ref.indexOf("mem-") === 0) {
        controls = '<div style="margin-top:4px;"><span class="chip">shared · read-only</span></div>';
      }
      // rows without a typed ref render read-only (never mutate bare ids)
      html += '<div class="list-item" style="margin:6px 0;border-bottom:1px solid rgba(60,80,100,0.3);padding-bottom:6px;">' +
        '<span class="chip">' + kind + '</span>' + pinChip +
        ' <span style="font-size:10px;color:#8aa;">' + esc(at) + '</span>' +
        '<div style="margin-top:3px;">' + text + '</div>' +
        controls + '</div>';
    });
    html += "</div>";
    target.innerHTML = html;
    if (editing) {
      // restore the draft + focus after any legitimate re-render
      const input = document.getElementById("lab-cortext-" + editing.ref);
      if (input) {
        input.value = editing.value;
        // live mirror: keep the draft current so a character switch (which
        // MUST repaint) restores typed-but-unsaved text, not the prefill
        input.oninput = function () {
          if (window._labEditing &&
              window._labEditing.ref === editing.ref) {
            window._labEditing.value = input.value;
          }
        };
        input.focus();
        try { input.setSelectionRange(input.value.length, input.value.length); }
        catch (e) { /* older engines: caret position not critical */ }
      }
    }
  }
  function labActionChar() {
    return labSel() && labSel().kind === "character" ? labSel().id
      : (window._labMemChar || "");
  }
  async function labMemoryAction(name, ref, extra) {
    // Shared mutation caller: visible feedback on ok:false AND exceptions;
    // reload only AFTER the bridge reports storage success.
    const c = labActionChar();
    let res;
    try {
      res = await call.apply(null, [name, c, ref].concat(extra || []));
    } catch (e) {
      toast(name + " failed: request error");
      return false;
    }
    if (!res || res.ok !== true) {
      toast(name + " failed: " +
        ((res && res.reason) || "unknown error"));
      return false;
    }
    if (window._labMemChar === c) await loadLabCharMemory(c);
    return true;
  }
  window._labPinMem = function (ref) {
    return labMemoryAction("character_pin", ref, [true]);
  };
  window._labUnpinMem = function (ref) {
    return labMemoryAction("character_pin", ref, [false]);
  };
  window._labForgetMem = function (ref) {
    return labMemoryAction("character_forget", ref, []);
  };
  window._labCorrectMem = function (ref) {
    const box = document.getElementById("lab-cor-" + ref);
    const input = document.getElementById("lab-cortext-" + ref);
    if (!box || !input) return;
    window._labEditing = {
      charId: labActionChar(),
      ref: String(ref),
      value: (window._labRowTexts || {})[ref] || "",
    };
    input.value = window._labEditing.value;
    input.oninput = function () {              // live draft mirror
      if (window._labEditing && window._labEditing.ref === ref) {
        window._labEditing.value = input.value;
      }
    };
    box.style.display = "block";
    input.focus();
  };
  window._labCorrectCancel = function (ref) {
    if (window._labEditing && window._labEditing.ref === ref) {
      window._labEditing = null;             // draft dropped by owner action
    }
    const box = document.getElementById("lab-cor-" + ref);
    if (box) box.style.display = "none";
  };
  window._labCorrectSave = async function (ref) {
    const input = document.getElementById("lab-cortext-" + ref);
    const value = input ? input.value.trim() : "";
    if (!value) {
      toast("Correction is empty - nothing saved.");   // visible, was silent
      return;
    }
    const c = labActionChar();
    let res;
    try {
      res = await call("character_correct", c, ref, value,
                       "corrected by owner via inspector");
    } catch (e) {
      toast("Correct failed: request error");           // draft kept
      return;
    }
    if (!res || res.ok !== true) {
      toast("Correct failed: " + ((res && res.reason) || "unknown error"));
      return;                                            // draft kept
    }
    window._labEditing = null;                // storage succeeded first
    if (window._labMemChar === c) await loadLabCharMemory(c);
    toast("Correction saved.");
  };
  window._labForgetChar = async function (charId) {
    // "Forget last": soft-delete the NEWEST live experience of this
    // character via its typed ref (typed ref only, honest feedback).
    let listed;
    try {
      listed = await call("get_character_memory", charId, 50);
    } catch (e) {
      toast("Forget last failed: request error");
      return;
    }
    if (!listed || listed.ok !== true) {
      toast("Forget last failed: " + ((listed && listed.reason) || "could not read experiences"));
      return;
    }
    const exps = listed && listed.scrapbook &&
      Array.isArray(listed.scrapbook.experiences)
      ? listed.scrapbook.experiences : [];
    if (!exps.length) {
      toast("No experience to forget.");
      return;
    }
    const newest = exps[exps.length - 1];     // ordered tick ASC
    const ref = String(newest.ref || "");
    if (ref.indexOf("exp-") !== 0) {
      toast("No forgettable experience.");
      return;
    }
    const ok = await labMemoryAction("character_forget", ref, []);
    if (ok) toast("Forgot the latest experience.");
  };
  window._labRefreshChar = async function (charId) { loadLabCharMemory(charId); };
  window._labLifeToggle = async function (charId, currentMode) {
    // LAB-3R5 individual opt-in/opt-out; local residents may only change
    // HERE (the bulk switch skips them by design).
    const next = currentMode === "ai" ? "npc" : "ai";
    let res;
    try {
      res = await call("lab_set_life_mode", charId, next);
    } catch (e) {
      toast("Life switch failed: request error");
      return;
    }
    if (!res || res.ok === false) {
      toast("Life switch failed: " +
        ((res && (res.reason || res.error)) || "unknown error"));
      return;
    }
    toast(charId + " → " + next);
    refresh("get_lab");       // badges + inspector read back the truth
  };

  function labSel() {
    return lab && lab.sel;
  }

  // ↓ the ORIGINAL paintLabInspector body resumes here;
  //   the function header was lost in the LAB-3 insert above.
  function paintLabInspector() {
    const el = $("fn-town-legacy-inspector");
    if (!el) return;
    // LAB-3R5: while the owner character's editor draft is open, the 4s
    // repaint must not rebuild the inspector (it would destroy the draft,
    // focus and the open box). Needs bars freeze for the draft's lifetime;
    // everything else repaints normally. The guard also yields when the
    // world/selection disappeared so a stale inspector cannot stick.
    if (window._labEditing && S.lab && lab && lab.sel &&
        lab.sel.kind === "character" &&
        window._labRenderedChar === lab.sel.id &&
        window._labEditing.charId === lab.sel.id &&
        labChar(lab.sel.id) &&
        document.getElementById("lab-cor-" + window._labEditing.ref)) {
      return;   // the open editor box still exists in the DOM
    }
    window._labRenderedChar = null;
    if (!S.lab) {
      el.innerHTML = blockHtml(ST.get_lab || "waiting", "get_lab");
      return;
    }
    const sel = lab.sel;
    if (!sel) {
      el.innerHTML = '<div class="empty"><div class="empty-t">Nothing selected</div>' +
        '<div class="empty-s">Click a resident, a piece of furniture or a room on the map.</div></div>';
      return;
    }
    const map = S.lab.map || {};
    if (sel.kind === "character") {
      const c = labChar(sel.id);
      if (!c) {
        lab.sel = null;
        el.innerHTML = '<div class="empty"><div class="empty-t">Resident left the map</div>' +
          '<div class="empty-s">Pick someone else on the town view.</div></div>';
        return;
      }
      // --- identity block: personal identity vs provider/lane vs life vs
      // health vs duty vs eligibility - each axis its own row, from the
      // ONE authority + the roster. Never invented: a resident without a
      // dedicated lane says "shared chain — not dedicated" instead of a
      // fabricated model.
      const rosterAgents = (S.roster && Array.isArray(S.roster.agents))
        ? S.roster.agents : [];
      const agent = rosterAgents.find((x) => String(x.id) === String(c.id)) || null;
      const idn = identityFor(agent || { id: c.id, name: c.name },
                              (S.lab && S.lab.authority) || null);
      const lifeEntry = idn.life ||
        { mode: "npc", source: "resident default", local: null };
      let html = '<div class="id-head"><span class="id-name">' + esc(idn.name) +
        '</span><span class="id-role">' + esc(idn.role || "") + "</span></div>";
      html += '<dl class="kv">';
      html += kvRow("State", c.state);
      html += kvRow("Goal", c.goal);
      html += kvRow("Room", c.room);
      html += kvRow("Task ref", c.task_ref);
      // LAB-3R5: life mode from the ONE authority (auth.life). Residents
      // have no per-resident provider classification yet - the source
      // says so honestly instead of guessing.
      html += kvRow("Life", lifeEntry.mode +
        (lifeEntry.source ? " · " + lifeEntry.source : "") +
        (lifeEntry.local === true ? " · local" : ""));
      html += kvRow("Health", idn.health || "no dedicated lane");
      // LAB-3R6: serving-as-FRIDAY is a DUTY axis, shown separately from
      // this resident's personal identity/life (mapped only where the
      // authority's boss_char_id makes the mapping real).
      html += kvRow("FRIDAY duty", idn.duty);
      html += kvRow("Eligibility", idn.eligibility);
      html += "</dl>";
      html += '<div class="field-label" style="margin-top:8px;">Capabilities</div>' +
        '<div class="chip-row" style="margin-top:5px;">' + capsHtml(idn.caps) + "</div>";
      if (c.id) {
        html += '<div style="margin-top:8px;">' +
          '<button type="button" class="btn" onclick="window._labLifeToggle(\'' +
          esc(c.id) + '\',\'' + esc(lifeEntry.mode) + '\')">' +
          (lifeEntry.mode === "ai" ? "set NPC" : "enable AI") +
          "</button></div>";
      }
      if (c.blocked_reason) {
        html += '<div class="list-error" style="margin-top:8px;">Blocked: ' + esc(c.blocked_reason) + "</div>";
      }
      const needs = c.needs;
      if (needs && typeof needs === "object") {
        let rows = "";
        let any = false;
        NEED_KEYS.forEach((k) => {
          if (!isNum(needs[k])) return;
          any = true;
          const v = Math.max(0, Math.min(100, needs[k]));
          rows += '<div class="need-row"><span class="need-name">' + esc(k) + "</span>" +
            '<div class="need-bar"><i style="width:' + v.toFixed(0) + '%"></i></div>' +
            '<span class="need-val">' + v.toFixed(0) + "</span></div>";
        });
        if (any) {
          html += '<div class="field-label" style="margin-top:10px;">Needs — urgency, fuller bar means more urgent</div>' +
            '<div class="needs" style="margin-top:7px;">' + rows + "</div>";
        }
      }
      // ROUND LAB-3: character identity + memory inspector panel
      html += '<div class="field-label" style="margin-top:12px;">Memory</div>';
      html += '<div id="lab-char-mem-l"><div class="empty-s">Loading memory…</div></div>';
      if (c.id) {
        html += '<div style="margin-top:8px;">' +
          '<button type="button" class="btn" onclick="window._labForgetChar(\'' + esc(c.id) + '\')">Forget last</button> ' +
          '<button type="button" class="btn" onclick="window._labRefreshChar(\'' + esc(c.id) + '\')">Refresh</button>' +
          "</div>";
      }
      el.innerHTML = html;
      window._labRenderedChar = c.id;        // LAB-3R5 draft-guard anchor
      // async: fetch character memory from the real bridge
      loadLabCharMemory(c.id);
      return;
    }
    if (sel.kind === "object") {
      const o = labFind(map.objects, sel.id);
      if (!o) { el.innerHTML = blockHtml("missing", "get_lab"); return; }
      el.innerHTML = '<dl class="kv">' +
        kvRow("Name", o.name) +
        kvRow("Purpose", o.purpose) +
        kvRow("Solid", boolLabel(o.solid)) +
        "</dl>";
      return;
    }
    if (sel.kind === "room") {
      const r = labFind(map.rooms, sel.id);
      if (!r) { el.innerHTML = blockHtml("missing", "get_lab"); return; }
      el.innerHTML = '<dl class="kv">' +
        kvRow("Name", r.name) +
        kvRow("Kind", r.kind) +
        kvRow("Capacity", r.capacity) +
        kvRow("Occupied", r.occupied) +
        "</dl>";
    }
  }

  /* ---------- rAF loop (only while this pane is visible) ---------- */
  function startLabLoop() {
    if (lab.running) return;
    if (activePane !== "view-lab" || document.hidden || !motionOK()) {
      labDraw();
      return;
    }
    lab.running = true;
    const step = () => {
      lab.raf = 0;
      if (activePane !== "view-lab" || document.hidden || !motionOK()) {
        lab.running = false;
        labDraw();
        return;
      }
      labDraw();
      lab.raf = requestAnimationFrame(step);
    };
    lab.raf = requestAnimationFrame(step);
  }

  function stopLabLoop() {
    if (lab.raf) cancelAnimationFrame(lab.raf);
    lab.raf = 0;
    lab.running = false;
  }

  // ROUND LAB-2B: the lab toolbar shows the ONE authoritative projection
  // (rev / served-by / life-state). Real fields only - never invented.
  function renderLabAuthority(auth) {
    const served = $("lab-served");
    const rev = $("lab-rev");
    const ai = $("lab-ai");
    if (!served && !rev && !ai) return;
    if (!auth || auth.ok === false) {
      if (served) served.textContent = "unavailable";
      if (rev) rev.textContent = "";
      if (ai) ai.textContent = "unavailable";
      return;
    }
    const serving = (auth.serving || {});
    // Duty readout: WHO is serving as FRIDAY right now (pool/model), kept
    // short enough for one pill; the per-entry life modes live in the AI pill.
    if (served) served.textContent = serving.active === true
      ? (serving.duty || "unknown") + " · " + trun(serving.served_by || "unknown", 30)
      : "idle";
    if (rev) rev.textContent = String(auth.rev || 0);
    // LAB-3R6: ONE authoritative life map, TWO clearly-labelled populations.
    // residents (local === null: no per-resident provider classification
    // exists - honest) vs the configured model chain (cast, local boolean).
    // The old single "0 ai / 16 npc (1 local)" lumped 4 residents + 12
    // providers together and contradicted the "4 residents" count pill.
    if (ai) {
      const life = auth.life || {};
      const ids = Object.keys(life);
      if (!ids.length) {
        ai.textContent = "?";
      } else {
        let resN = 0, resAi = 0, chN = 0, chAi = 0, localN = 0;
        ids.forEach((id) => {
          const entry = life[id];
          if (!entry) return;
          if (entry.local === null || entry.local === undefined) {
            resN += 1;
            if (entry.mode === "ai") resAi += 1;
          } else {
            chN += 1;
            if (entry.mode === "ai") chAi += 1;
            if (entry.local === true) localN += 1;
          }
        });
        // Plain wording (the user's chrome rule): counts kept, abbreviations
        // dropped - "Residents 0/4 AI · Providers 11/12 AI (1 local)".
        const provPart = chN
          ? "Providers " + chAi + "/" + chN + " AI" +
            (localN ? " (" + localN + " local)" : "")
          : "Providers none configured";
        ai.textContent = "Residents " + resAi + "/" + resN + " AI · " + provPart;
        ai.title = "Residents = town simulation characters (free, zero-token " +
          "by default). Providers = configured lanes in the model chain; " +
          "local models are never bulk-enabled.";
      }
    }
  }

  // ROUND LAB-2C fix: the actual budget strip (Real calls, not decorative).
  // Every button is an atomic bridge action; the read-back is authoritative.
  function renderLabBudget(d, st) {
    const el = (id) => $(id);
    if (!el("lab-budget-enabled")) return;
    if (st !== "ok" || !d || d.ok === false) {
      el("lab-budget-enabled").textContent = "unavailable";
      el("lab-budget-requests").textContent = "";
      el("lab-budget-tokens").textContent = "";
      el("lab-budget-attempts").textContent = "";
      el("lab-budget-presence").textContent = "";
      return;
    }
    el("lab-budget-enabled").textContent =
      d.enabled ? "on" : "off";
    const remaining = d.remaining || {};
    const reqs = remaining.requests != null ? remaining.requests :
      (d.usage && d.usage.attempts != null ? String(d.usage.attempts) + " used" : "?");
    el("lab-budget-requests").textContent = reqs;
    el("lab-budget-tokens").textContent =
      remaining.tokens != null ? remaining.tokens :
      (d.usage && d.usage.tokens_total != null ? String(d.usage.tokens_total) : "?");
    el("lab-budget-attempts").textContent =
      d.usage ? String(d.usage.attempts) : "?";
    el("lab-budget-presence").textContent = d.presence || "?";
  }

  async function labBudgetAction(action, args) {
    const payload = await call(action, args || {});
    refresh("get_budgets");
    return payload;
  }
  function refreshBudget() {
    refresh("get_budgets");
  }

  // ROUND LAB-2C: the RENDER hook for the honest budget strip.
  RENDER.get_budgets = renderLabBudget;

  RENDER.get_lab = function (d, st) {
    if (st !== "ok" || !d) {
      S.lab = null;
      lab.sel = null;
      labOverlay(st === "waiting" ? "Waiting for the pywebview bridge…"
        : st === "missing" ? "get_lab() not wired in this build"
        : "Bridge call failed — will retry.", "err");
      paintLabInspector();
      labDraw();
      // ONE world: with no lab payload the workspace falls back honestly
      // (schematic or empty) instead of keeping a stale frame.
      try { drawWorld(); }
      catch (renderError) { console.error("workspace render failed:", renderError); }
      return;
    }
    S.lab = d;
    renderLabAuthority(d.authority || { ok: false });
    labEnsureCanvas();
    if (labArt.state === "idle") labLoadArt();
    const map = d.map;
    if (!map || !map.ok) {
      labOverlay("Town map unavailable: " + ((map && map.error) || "no data"), "err");
    } else if (labArt.state === "error") {
      labOverlay(labArt.error, "err");
    } else if (labArt.state === "ok") {
      labOverlay("", "hide");
    } else {
      labOverlay("Loading town…", "show");
    }
    if (!lab.fitted) labFit();
    labPaintToolbar();
    paintLabInspector();
    labDraw();
    // keep the workspace canvas on the SAME payload (same positions)
    try { drawWorld(); }
    catch (renderError) { console.error("workspace render failed:", renderError); }
    restartLoops();
  };

  /* ============================================================
     CHAT FORM + QUICK ACTIONS + WIRING
     ============================================================ */
  function wireChat() {
    const form = $("chat-form");
    if (!form) return;
    let sending = false;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (sending) return;
      const input = $("chat-input");
      const status = $("chat-status");
      const draft = input ? input.value : "";
      const text = draft.trim();
      if (!text) return;
      if (!has("send_text")) {
        if (status) { status.textContent = "Message sending is unavailable. Your draft is kept."; status.dataset.error = "true"; }
        return;
      }
      const btn = form.querySelector('button[type="submit"]');
      sending = true;
      form.setAttribute("aria-busy", "true");
      if (btn) btn.disabled = true;
      if (status) { status.textContent = "Sending…"; status.dataset.error = "false"; }
      try {
        const r = await call("send_text", text);
        if (!r || r.accepted !== true) {
          if (status) { status.textContent = (r && r.message) || "Message was not accepted. Your draft is kept."; status.dataset.error = "true"; }
        } else {
          if (input && input.value === draft) input.value = "";
          if (status) status.textContent = r.message || (r.status === "queued" ? "Message queued." : "Message accepted.");
          LAST.get_chat = "";
          refreshChain(["get_chat", "get_turn", "get_core"]);
        }
      } catch (err) {
        if (status) { status.textContent = "Could not confirm the message. Your draft is kept; check the conversation before retrying."; status.dataset.error = "true"; }
      }
      sending = false;
      form.setAttribute("aria-busy", "false");
      if (btn) btn.disabled = false;
      if (input && (document.activeElement === input || document.activeElement === btn)) input.focus();
    });
  }

  function wireActions() {
    $("home-n8n-settings")?.addEventListener("click",()=>activate("view-settings"));
    for (const [id, allow] of [["home-mcp-allow", true], ["home-mcp-deny", false]]) {
      $(id)?.addEventListener("click", async () => {
        const proposal = S.turn?.mcp_proposal?.id;
        if (!proposal || !has("decide_mcp_proposal")) return;
        $("home-mcp-allow").disabled = $("home-mcp-deny").disabled = true;
        let result;
        try { result = await call("decide_mcp_proposal", proposal, allow); } catch (_) {}
        if ($("home-mcp-approval").dataset.proposal !== proposal) return;
        $("home-mcp-status").textContent = result?.ok ? "Decision recorded." : "Decision could not be confirmed.";
        LAST.get_turn = "";
        refresh("get_turn");
      });
    }
    window.FieldN8N?.init({call,refresh:()=>{LAST.get_n8n="";if(inflight.has("get_n8n"))settingsRefreshQueued.add("get_n8n");else refresh("get_n8n");}});
    window.FieldCompanion?.init({call});
    window.FieldPersonality?.init({call});
    window.FieldSettings?.init({call,apply:applyPrefs,refresh:()=>{
      for(const name of ["get_startup","get_settings","get_n8n"]) {
        LAST[name]="";
        if(inflight.has(name)) settingsRefreshQueued.add(name);else refresh(name);
      }
    }});
    window.FieldWorkflows?.init({
      call,
      active: () => activePane === "view-workflows",
      refresh: () => {
        LAST.get_workflow_view = "";
        if (inflight.has("get_workflow_view")) workflowRefreshQueued = true;
        else refresh("get_workflow_view");
      },
      tasks: correlationId => {
        if (!window.FieldPlanner?.fromWorkflow(correlationId)) return;
        LAST.get_tasks = "";
        activate("view-tasks");
      }
    });
    window.FieldCapabilities?.init({
      refresh: () => {
        LAST.get_capability_inventory = "";
        if (inflight.has("get_capability_inventory")) capabilityRefreshQueued = true;
        else refresh("get_capability_inventory");
      }
    });
    window.FieldRouting?.init({
      open: activate,
      readiness: () => call("get_rollout_readiness"),
      refresh: () => { LAST.get_routing_view = ""; return refresh("get_routing_view"); },
      roster: (id,revision) => {
        window.FieldRoster.fromTown(id,revision);
        LAST.get_identity_views = "";
        activate("view-roster");
      }
    });
    window.FieldVoiceScreen?.init(changeVoiceInput);
    if (window.FieldLibrary) window.FieldLibrary.init({
      call, has,
      refreshConversations: () => { LAST.get_conversation_library="";return refresh("get_conversation_library"); },
      refreshMemory: () => { LAST.get_memory_library = ""; return refresh("get_memory_library"); },
      refreshKnowledge: () => { LAST.get_knowledge_library = ""; return refresh("get_knowledge_library"); },
      openChat: () => { activate("view-command"); $("chat-input").focus(); }
    });
    if (window.FieldPlanner) window.FieldPlanner.init({
      call, has,
      refreshTasks: () => { LAST.get_tasks = ""; return refresh("get_tasks"); },
      openWorkspace: (id, attempt) => {
        window.FieldWorkspace.fromTasks(id, attempt);
        LAST.get_roster = "";
        activate("view-workspace");
      },
      openChat: () => { activate("view-command"); $("chat-input").focus(); }
    });
    window.FieldPersonalPlanner?.init({call,has,refresh:()=>{
      LAST.get_tasks='';LAST.get_schedule='';return Promise.all([refresh('get_tasks'),refresh('get_schedule')]);
    }});

    if (window.FieldWorkspace) window.FieldWorkspace.init({
      call, has, refresh: () => { LAST.get_roster = ""; return refresh("get_roster"); },
      roster: (id, revision) => {
        window.FieldRoster.fromTown(id, revision);
        LAST.get_identity_views = "";
        if (inflight.has("get_identity_views")) rosterRefreshQueued = true;
        activate("view-roster");
      }
    });
    if (window.FieldTown) window.FieldTown.init({
      call,
      refresh: () => refresh("get_town_view"),
      legacy: sourceId => {
        lab.sel = sourceId ? {kind:"character",id:sourceId} : null;
        LAST.get_lab = "";
        refreshChain(["get_lab", "get_budgets"]);
      },
      roster: (characterId, revision) => {
        window.FieldRoster.fromTown(characterId, revision);
        LAST.get_identity_views = "";
        activate("view-roster");
      }
    });
    window.FieldTownInstall?.init(call, () => refresh("get_town_view"));
    if (window.FieldRoster) window.FieldRoster.init({
      call,
      canSave: () => has("set_catalog_serving"),
      canSaveLife: () => has("set_catalog_background_life") && has("set_catalog_life_budget"),
      refresh: () => {
        LAST.get_identity_views = "";
        if (inflight.has("get_identity_views")) rosterRefreshQueued = true;
        else refresh("get_identity_views");
      },
      town: (sourceId, characterId, catalogRevision) => {
        window.FieldTown.accept(characterId, catalogRevision);
        LAST.get_town_view = "";
        activate("view-lab");
      }
    });
    const workforce = $("roster-workforce");
    if (workforce) workforce.addEventListener("toggle", () => {
      if (workforce.open) refreshChain(["get_roster", "get_lab"]);
    });
    labWire();
    $$("[data-stop]").forEach((b) => b.addEventListener("click", doStop));
    $$("[data-mute]").forEach((b) => b.addEventListener("click", toggleMute));
    const add = $("memory-add-btn");
    if (add) add.addEventListener("click", addMemory);
    const mnc = $("memory-new-content");
    if (mnc) mnc.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addMemory(); }
    });
    const af = $("assign-form");
    if (af) af.addEventListener("submit", onAssignSubmit);
    const qf = $("qa-focus-chat");
    if (qf) qf.addEventListener("click", () => {
      const i = $("chat-input");
      if (i) { i.focus(); i.select(); }
    });
    const qv = $("qa-voice");
    if (qv) qv.addEventListener("click", () => activate("view-voice"));
    const gc = $("cc-goto-conversations");
    if (gc) gc.addEventListener("click", () => activate("view-conversations"));
    $$("[data-world-cmd]").forEach((b) => {
      b.addEventListener("click", () => worldCommand(b.getAttribute("data-world-cmd")));
    });
    window.addEventListener("resize", () => {
      resizeGlobe();
      layoutRoster();
      // re-fit ONLY on window resize (never mid-session); same rule as the
      // town canvas's existing lab.fitted = false handler
      world.fitted = false;
      drawWorld();
    });
  }

  /* ============================================================
     BOOT
     ============================================================ */
  function paintInitialStates() {
    // give every data container an honest waiting state before the bridge answers
    ["get_core", "get_tasks", "get_schedule", "get_ai_agents", "get_memory",
      "get_knowledge", "get_project_notes", "get_tools", "get_skills",
      "get_memory_library", "get_knowledge_library", "get_conversation_library", "get_voice_screen", "get_routing_view", "get_capability_inventory", "get_workflow_view",
      "get_roster", "get_identity_views", "get_world", "get_lab", "get_town_view", "get_chat", "get_events", "get_logs",
      "get_sys_stats", "get_focus", "get_ear_status", "get_startup", "get_settings", "get_n8n"
    ].forEach((m) => { ST[m] = "waiting"; if (RENDER[m]) RENDER[m](null, "waiting"); });
    syncStopButtons();
    syncMute();
  }

  // ROUND LAB-2C: real budget+lifecycle buttons wired to bridge methods.
  // Every click performs ONE bridge action with read-back; nothing is
  // decorative or stored client-side.
  function wireBudget() {
    const bind = (id, action) => {
      const button = $(id);
      if (!button) return;
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        labBudgetAction(action);
      });
    };
    bind("lab-budget-toggle", "lab_budget_enable");
    bind("lab-budget-off", "lab_budget_disable");
    bind("lab-budget-pause", "lab_budget_pause");
    bind("lab-budget-open", "lab_budget_open");
  }

  function boot() {
    initNav();
    wireChat();
    wireActions();
    wireBudget();
    applyPrefs();
    paintInitialStates();
    tickClock();
    setInterval(tickClock, 1000);
    initGlobe();
    ensureCanvas();
    startPolling();

    window.addEventListener("pywebviewready", () => {
      // bridge appeared — clear dedupe cache and repaint everything
      Object.keys(LAST).forEach((k) => { delete LAST[k]; });
      setConn("wait", "Bridge: connecting");
      pollOnce();
      refreshPane(activePane);
      syncStopButtons();
      syncMute();
    });

    // fallback: if pywebviewready never fires, poll anyway (loop already started)
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
