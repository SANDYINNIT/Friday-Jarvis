(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const canvas = $("orb");
  const ctx = canvas.getContext("2d");

  const COLORS = {
    idle: "#4b7895",
    listening: "#42d6ff",
    thinking: "#b28cff",
    speaking: "#52f2b1",
    error: "#ff4d6a",
  };

  const PARTICLE_COUNT = 600;
  const particles = Array.from({ length: PARTICLE_COUNT }, (_, i) => ({
    angle: (i / PARTICLE_COUNT) * Math.PI * 2,
    radius: 0.15 + Math.random() * 0.85,
    speed: (Math.random() - 0.5) * 0.002,
    phase: Math.random() * Math.PI * 2,
    size: 0.3 + Math.random() * 1.6,
    layer: Math.floor(Math.random() * 3),
  }));

  let current = { state: "idle", label: "STANDBY", detail: "Ready", sequence: -1, color: COLORS.idle };
  let intensity = 0;
  let analyser = null;
  let audioData = null;
  let lastChatKey = "";
  let lastLogKey = "";
  let pollCount = 0;

  // --- Canvas ---
  function resize() {
    const r = Math.min(devicePixelRatio || 1, 2);
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * r;
    canvas.height = rect.height * r;
    ctx.setTransform(r, 0, 0, r, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  // --- Bridge ---
  function api() {
    return window.pywebview?.api || {};
  }

  // --- Utilities ---
  function esc(v) {
    return String(v).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function fmtTime(iso) {
    if (!iso) return "---";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function trun(v, n) {
    v = String(v ?? "");
    return v.length > n ? v.slice(0, n) + "…" : v;
  }

  // --- Clock ---
  function updateClock() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, "0");
    const m = String(now.getMinutes()).padStart(2, "0");
    const s = String(now.getSeconds()).padStart(2, "0");
    const el = $("clock");
    if (el) el.textContent = h + ":" + m + ":" + s;
  }
  setInterval(updateClock, 1000);
  updateClock();

  // --- Sidebar tab navigation ---
  function initTabs() {
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.addEventListener("click", () => {
        const target = item.getAttribute("data-target");
        if (!target) return;
        document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
        document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
        item.classList.add("active");
        const pane = $(target);
        if (pane) pane.classList.add("active");
        if (target === "view-agents") loadAgents();
        if (target === "view-memory") loadMemory();
        if (target === "view-knowledge-base") loadKnowledgeBase();
        if (target === "view-tools") loadSkills();
      });
    });
  }
  initTabs();

  // --- Status ---
  function applyStatus(s) {
    if (!s) return;
    if (typeof s.paused === "boolean") renderMicButton(s.paused);
    if (s.sequence <= current.sequence) return;
    current = s;
    $("state").textContent = s.label || "STANDBY";
    $("detail").textContent = s.detail || "Ready";
    $("sys-state").textContent = s.label || "STANDBY";
    $("sys-state").style.color = s.color || COLORS.idle;
  }

  // --- Connection ---
  function setOnline(ok) {
    const el = $("connection");
    if (!el) return;
    el.textContent = ok ? "ONLINE" : "OFFLINE";
    el.className = "conn " + (ok ? "online" : "offline");
  }

  // --- Polling ---
  async function poll() {
    try {
      if (api().get_status) {
        applyStatus(await api().get_status());
        setOnline(true);
      }
      if (api().get_chat) {
        const chat = await api().get_chat();
        const key = JSON.stringify(chat);
        if (key !== lastChatKey) {
          renderChat(chat);
          renderConversations(chat);
          lastChatKey = key;
        }
      }
      if (api().get_logs) {
        const logs = await api().get_logs();
        const lkey = logs.slice(0, 400);
        if (lkey !== lastLogKey) {
          renderLogs(logs);
          lastLogKey = lkey;
        }
      }
      pollCount += 1;
      const agentsActive = $("view-agents")?.classList.contains("active");
      if (agentsActive || pollCount % 4 === 0) {
        if (api().get_ai_agents) {
          loadAgents();
        }
      }
      if (pollCount % 4 === 0) {
        if (api().get_sys_stats) {
          const stats = await api().get_sys_stats();
          renderStats(stats);
        }
        if (api().get_ear_status) {
          renderEar(await api().get_ear_status());
        }
        if (api().get_focus) {
          renderFocus(await api().get_focus());
        }
        if (api().get_memory) {
          loadMemory(false);
        }
      }
      const calendarActive = $("view-calendar")?.classList.contains("active");
      if (calendarActive || pollCount % 4 === 0) {
        refreshSchedule();
      }
      const settingsActive = $("view-settings")?.classList.contains("active");
      if (settingsActive || pollCount % 8 === 0) {
        refreshStartupSetting();
        bindStartupToggle();
      }
    } catch (_) {
      setOnline(false);
    }
    setTimeout(poll, 500);
  }
  window.addEventListener("pywebviewready", poll);
  setTimeout(poll, 400);

  // --- Settings load ---
  async function loadSettings() {
    try {
      if (!api().get_settings) return;
      const s = await api().get_settings();
      $("sys-model").textContent = s.model || "---";
      $("sys-vision").textContent = s["vision model"] || "---";
      $("sys-tts").textContent = s["TTS URL"] || "---";
      if (api().get_version) {
        const v = await api().get_version();
        $("sys-version").textContent = v || "---";
        const bv = $("bottom-version");
        if (bv) bv.textContent = v ? "v" + v : "v0.1";
      }
    } catch (_) {}
  }
  window.addEventListener("pywebviewready", loadSettings);

  // --- Logs (LIVE INTELLIGENCE FEED) ---
  function logLevelClass(line) {
    if (!line) return "info";
    const l = String(line).toLowerCase();
    if (l.includes("[redacted]") || l.includes("error") || l.includes("watchdog reset") || l.includes("failed") || l.includes("crash")) return "warn";
    if (l.includes("assistant") || l.includes("accepted voice") || l.includes("heard") || l.includes("typed")) return "info";
    return "info";
  }

  function renderLogs(text) {
    const pre = $("log-output");
    if (!pre) return;
    const raw = String(text || "");
    if (!raw.trim()) {
      pre.innerHTML = '<div class="intel-item info">No intelligence activity recorded yet.</div>';
      return;
    }
    const lines = raw.split("\n").filter(Boolean);
    const last = lines.slice(-60);
    pre.innerHTML = last.map((line) => {
      const level = logLevelClass(line);
      return '<div class="intel-item ' + level + '">' + esc(line) + "</div>";
    }).join("");
    pre.scrollTop = pre.scrollHeight;
  }

  // --- System stats ---
  function renderStats(stats) {
    if (!stats) return;
    const cpuEl = $("stat-cpu");
    if (cpuEl && typeof stats.cpu === "number") cpuEl.textContent = Math.round(stats.cpu) + "%";
    const ramEl = $("stat-ram");
    if (ramEl && stats.ram && typeof stats.ram.percent === "number") ramEl.textContent = Math.round(stats.ram.percent) + "%";
  }

  // --- Focus & Ear status pills ---
  function renderFocus(focus) {
    const el = $("focus-pill");
    if (!el) return;
    const title = (focus && focus.title) || "";
    const exe = (focus && focus.exe) || "";
    if (focus && focus.editor) {
      el.textContent = "Editor · " + trun(title || exe, 26);
      el.style.color = "var(--accent-green)";
    } else if (title) {
      el.textContent = trun(title, 26);
      el.style.color = "var(--text-bright)";
    } else {
      el.textContent = "--";
      el.style.color = "";
    }
  }

  function renderEar(ear) {
    const el = $("ear-pill");
    if (!el) return;
    if (!ear || !ear.enabled) {
      el.textContent = "Off";
      el.style.color = "#5a738c";
    } else if (ear.social_call_active) {
      el.textContent = "Muted · Call";
      el.style.color = "#ff637d";
    } else if (ear.capturing) {
      el.textContent = "Capturing 2m";
      el.style.color = "var(--accent-cyan)";
    } else if (ear.error) {
      el.textContent = "Error";
      el.style.color = "#ff637d";
    } else {
      el.textContent = "Standby";
      el.style.color = "var(--accent-green)";
    }
  }

  // --- Memory Orbs Neural Net + Table ---
  async function loadMemory(force) {
    try {
      if (!api().get_memory) return;
      const records = await api().get_memory();
      const container = $("memory-orbs-container");
      const tooltip = $("memory-tooltip");
      const countEl = $("memory-count");
      const statEl = $("mem-stored-stat");
      if (countEl) countEl.textContent = records.length ? String(records.length) : "0";
      if (statEl) statEl.textContent = records.length ? String(records.length) : "0";

      if (container) {
        if (!records.length) {
          container.innerHTML = '<div class="mem-empty">No neural memory nodes stored yet.</div>';
        } else {
          container.innerHTML = records.map((r) =>
            '<div class="memory-orb" data-key="' + esc(r.key) + '" data-content="' + esc(r.content) + '" title="' + esc(r.key) + '"></div>'
          ).join("");
          container.querySelectorAll(".memory-orb").forEach((orb) => {
            orb.addEventListener("mouseenter", () => {
              if (tooltip) {
                tooltip.textContent = "[" + orb.getAttribute("data-key") + "]: " + orb.getAttribute("data-content");
                tooltip.style.color = "var(--accent-cyan)";
              }
            });
            orb.addEventListener("mouseleave", () => {
              if (tooltip) {
                tooltip.textContent = "Hover over a memory node above to inspect its neural weight and content.";
                tooltip.style.color = "var(--text-bright)";
              }
            });
          });
        }
      }

      const tbody = $("memory-table");
      if (tbody) {
        if (!records.length) {
          tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-dim);font:11px var(--font-mono);padding:8px;">No persistent memories stored.</td></tr>';
        } else {
          tbody.innerHTML = records.map((r) =>
            '<tr data-id="' + r.id + '">' +
              '<td class="mem-key-cell" data-full="' + esc(r.key) + '">' + esc(r.key) + "</td>" +
              '<td class="mem-content-cell" data-full="' + esc(r.content) + '">' + esc(trun(r.content, 120)) + "</td>" +
              '<td class="mem-actions">' +
                '<button class="tiny-btn edit" data-id="' + r.id + '">Edit</button> ' +
                '<button class="tiny-btn" data-id="' + r.id + '">Forget</button>' +
              "</td>" +
            "</tr>"
          ).join("");
          tbody.querySelectorAll("button.edit").forEach((btn) => {
            btn.addEventListener("click", () => startEditRow(btn.closest("tr")));
          });
          tbody.querySelectorAll("button:not(.edit)").forEach((btn) => {
            btn.addEventListener("click", async () => {
              try {
                if (api().delete_memory) {
                  await api().delete_memory(btn.getAttribute("data-id"));
                }
                loadMemory(true);
              } catch (_) {}
            });
          });
        }
      }
    } catch (_) {}
  }

  function startEditRow(tr) {
    if (!tr) return;
    const id = tr.getAttribute("data-id");
    const keyEl = tr.querySelector(".mem-key-cell");
    const contentEl = tr.querySelector(".mem-content-cell");
    const actionsEl = tr.querySelector(".mem-actions");
    if (!keyEl || !contentEl || !actionsEl) return;
    keyEl.innerHTML = '<input class="mem-edit-input" data-field="key" value="' + esc(keyEl.getAttribute("data-full") || keyEl.textContent) + '" />';
    contentEl.innerHTML = '<input class="mem-edit-input wide" data-field="content" value="' + esc(contentEl.getAttribute("data-full") || contentEl.textContent) + '" />';
    actionsEl.innerHTML = '<button class="tiny-btn save">Save</button> <button class="tiny-btn cancel">Cancel</button>';
    actionsEl.querySelector(".save").addEventListener("click", async () => {
      const key = tr.querySelector('input[data-field="key"]').value.trim();
      const content = tr.querySelector('input[data-field="content"]').value.trim();
      try {
        if (key && content && api().update_memory) {
          await api().update_memory(id, key, content);
        }
      } catch (_) {}
      loadMemory(true);
    });
    actionsEl.querySelector(".cancel").addEventListener("click", () => loadMemory(true));
  }

  async function addMemory() {
    const keyEl = $("memory-new-key");
    const contentEl = $("memory-new-content");
    const statusEl = $("memory-add-status");
    if (!keyEl || !contentEl) return;
    const key = keyEl.value.trim();
    const content = contentEl.value.trim();
    if (!key || !content) {
      if (statusEl) statusEl.textContent = "Both key and content are required.";
      return;
    }
    try {
      if (api().add_memory) {
        await api().add_memory(key, content);
        keyEl.value = "";
        contentEl.value = "";
        if (statusEl) statusEl.textContent = "Memory stored.";
      } else {
        if (statusEl) statusEl.textContent = "Add-memory bridge unavailable.";
      }
    } catch (_) {
      if (statusEl) statusEl.textContent = "Failed to store memory.";
    }
    loadMemory(true);
  }
  $("memory-add-btn")?.addEventListener("click", addMemory);
  $("memory-new-content")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addMemory();
    }
  });
  window.addEventListener("pywebviewready", () => loadMemory(true));

  // --- Knowledge Base (sections + project notes) ---
  function secLabel(key) {
    return String(key).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function escItem(item) {
    if (item && typeof item === "object" && item.name !== undefined) {
      return '<div class="kbase-row kbase-project">' +
        '<span class="project-kind">' + esc(String(item.kind || "misc")) + "</span>" +
        esc(item.name) + "</div>";
    }
    return '<div class="kbase-row">' + esc(item) + "</div>";
  }

  async function loadKnowledgeBase() {
    try {
      const sectionsEl = $("knowledge-sections");
      if (sectionsEl) {
        let sections = null;
        if (api().get_knowledge) sections = await api().get_knowledge();
        if (sections && typeof sections === "object") {
          const entries = Object.entries(sections).filter(([, v]) => v && (Array.isArray(v) ? v.length : true));
          if (entries.length) {
            sectionsEl.innerHTML = entries.map(([key, items]) =>
              '<div class="kbase-section">' +
                '<div class="kbase-section-title">' + esc(secLabel(key)) + "</div>" +
                (Array.isArray(items) ? items.map(escItem).join("") : esc(items)) +
              "</div>"
            ).join("");
          } else {
            sectionsEl.innerHTML = '<div class="chat-empty">Knowledge base is empty.</div>';
          }
        } else {
          sectionsEl.innerHTML = '<div class="chat-empty">Knowledge base unavailable.</div>';
        }
      }
      const notesEl = $("knowledge-base-notes");
      if (notesEl && api().get_project_notes) {
        notesEl.innerHTML = '<pre class="notes-pre">' + esc(await api().get_project_notes()) + "</pre>";
      }
    } catch (_) {}
  }
  window.addEventListener("pywebviewready", loadKnowledgeBase);

  // --- Tools & Skills (sections of tools) ---
  async function loadSkills() {
    try {
      const el = $("skills-list-view");
      if (!el) return;
      let payload = null;
      if (api().get_tools) payload = await api().get_tools();
      if (payload && typeof payload === "object") {
        const entries = Object.entries(payload).filter(([, v]) => v && (Array.isArray(v) ? v.length : true));
        if (entries.length) {
          el.innerHTML = entries.map(([key, items]) =>
            '<div class="kbase-section">' +
              '<div class="kbase-section-title">' + esc(secLabel(key)) + "</div>" +
              '<div class="skill-chip-row">' +
                (Array.isArray(items) ? items.map((item) => '<span class="skill-chip">' + esc(item) + "</span>").join("") : esc(items)) +
              "</div>" +
            "</div>"
          ).join("");
          return;
        }
      }
      if (api().get_skills) {
        const skills = await api().get_skills();
        if (skills && skills.length) {
          el.innerHTML = '<div class="kbase-section"><div class="kbase-section-title">Core Modules</div>' +
            '<div class="skill-chip-row">' + skills.map((name) => '<span class="skill-chip">' + esc(name) + "</span>").join("") + "</div></div>";
          return;
        }
      }
      el.innerHTML = '<div class="chat-empty">No tools discovered.</div>';
    } catch (_) {}
  }
  window.addEventListener("pywebviewready", loadSkills);

  // --- AI Orchestrator & Agents Matrix ---
  let lastAgentsKey = "";
  async function loadAgents() {
    try {
      if (!api().get_ai_agents) return;
      const data = await api().get_ai_agents();
      if (!data) return;
      const key = JSON.stringify(data);
      if (key === lastAgentsKey) return;
      lastAgentsKey = key;
      renderAgents(data);
    } catch (_) {}
  }
  window.addEventListener("pywebviewready", loadAgents);

  // --- Settings (startup toggle) ---
  async function refreshStartupSetting() {
    try {
      if (!api().get_startup) return;
      const data = await api().get_startup();
      const toggle = $("setting-startup");
      if (!toggle) return;
      toggle.checked = !!data.startup_on;
      const slider = $("setting-startup-slider");
      if (slider) {
        slider.style.background = toggle.checked ? "var(--accent-green)" : "#33506b";
        if (toggle.checked) {
          slider.style.transform = "translateX(22px)";
        } else {
          slider.style.transform = "none";
        }
      }
      const trayEl = $("setting-tray-status");
      if (trayEl) {
        trayEl.textContent = data.tray_available
          ? "System tray icon running."
          : "System tray unavailable (pystray not installed).";
      }
    } catch (error) {
      /* settings tab is cosmetic; never break the HUD */
    }
  }

  async function bindStartupToggle() {
    const toggle = $("setting-startup");
    if (!toggle || toggle.dataset.bound) return;
    toggle.dataset.bound = "1";
    toggle.addEventListener("change", async () => {
      const checked = !!toggle.checked;
      try {
        if (api().set_startup) {
          const result = await api().set_startup(checked);
          const slider = $("setting-startup-slider");
          if (slider) {
            const ok = result && (result.accepted || result.enabled);
            slider.style.background = ok && checked ? "var(--accent-green)" : "#33506b";
            slider.style.transform = ok && checked ? "translateX(22px)" : "none";
          }
        } else {
          toggle.checked = false;
          return;
        }
        if (!checked) {
          const slider = $("setting-startup-slider");
          if (slider) {
            slider.style.background = "#33506b";
            slider.style.transform = "none";
          }
        }
      } catch (error) {
        toggle.checked = !checked; // revert on failure
      }
    });
  }

  function renderAgents(data) {
    const summary = data.summary || {};
    const agents = data.agents || [];

    const totalEl = $("agents-summary-total");
    const activeEl = $("agents-summary-active");
    const modelEl = $("agents-summary-model");
    const providerEl = $("agents-summary-provider");
    const tierEl = $("agents-summary-tier");

    if (totalEl) totalEl.textContent = summary.total != null ? String(summary.total) : String(agents.length);
    if (activeEl) {
      if (summary.active_turn) {
        activeEl.textContent = "IN-PROGRESS ENGAGEMENT";
        activeEl.style.color = "var(--accent-cyan)";
      } else {
        activeEl.textContent = "STANDBY (READY)";
        activeEl.style.color = "var(--accent-green)";
      }
    }
    if (modelEl) modelEl.textContent = summary.active_model || "---";
    if (providerEl) providerEl.textContent = summary.active_provider || "---";
    if (tierEl) {
      const t = (summary.active_tier || "NORMAL").toUpperCase();
      tierEl.textContent = t;
      tierEl.style.color = t === "DEEP" ? "#ff0055" : (t === "HARD" ? "#ffb86c" : "var(--accent-cyan)");
    }
    const switchEl = $("agents-switch-line");
    if (switchEl) switchEl.textContent = summary.last_switch || "No provider switches yet.";

    const container = $("agents-container");
    if (!container) return;

    if (!agents.length) {
      container.innerHTML = '<div class="agent-loading-state">No active AI agents detected in configuration.</div>';
      return;
    }

    container.innerHTML = agents.map((agent) => {
      const statusClass = agent.status === "in-use" ? "in-use" : (agent.status === "available" ? "available" : (agent.status === "exhausted" ? "exhausted" : "no-keys"));
      const statusLabel = agent.status === "in-use" ? "⚡ IN-USE" : (agent.status === "available" ? "✓ AVAILABLE" : (agent.status === "exhausted" ? "⚠ EXHAUSTED" : "UNCONFIGURED"));
      const isInUse = agent.status === "in-use";

      return (
        '<div class="agent-card ' + statusClass + '" id="' + esc(agent.id) + '">' +
          '<div class="agent-card-header">' +
            '<div>' +
              '<div class="agent-card-title">' + esc(agent.name) + '</div>' +
              '<div class="agent-card-role">' + esc(agent.role) + '</div>' +
            '</div>' +
            '<div class="agent-status-tag ' + statusClass + '">' + esc(statusLabel) + '</div>' +
          '</div>' +

          '<div class="agent-tech-specs">' +
            '<div class="spec-item">' +
              '<span class="spec-label">Provider</span>' +
              '<span class="spec-val provider-val">' + esc(agent.provider) + '</span>' +
            '</div>' +
            '<div class="spec-item">' +
              '<span class="spec-label">Model Engine</span>' +
              '<span class="spec-val model-val">' + esc(agent.model) + '</span>' +
            '</div>' +
          '</div>' +

          '<div class="agent-task-banner ' + (isInUse ? "in-use" : "") + '">' +
            '<span class="task-label">Current State:</span>' +
            '<span class="task-val">' + esc(agent.task) + '</span>' +
          '</div>' +

          '<div class="agent-desc">' + esc(agent.description) + '</div>' +

          '<div class="agent-health-footer">' +
            '<span>Pool Health:</span>' +
            '<span class="quota-info">' + esc(agent.health_desc) + '</span>' +
          '</div>' +
        '</div>'
      );
    }).join("");
  }

  // --- Chat ---
  function chatMessageHtml(m) {
    const who = m.role === "user" ? "YOU" : (m.role === "tool" ? "TOOL" : "FRIDAY");
    const cls = m.role === "user" ? "user" : (m.role === "tool" ? "tool" : "assistant");
    return '<div class="chat-msg ' + cls + '">' +
      '<div class="chat-meta">' + who + "</div>" +
      "<div>" + esc(m.content) + "</div>" +
    "</div>";
  }

  function renderChat(messages) {
    const list = $("chat-list");
    if (!list) return;
    if (!messages || !messages.length) {
      list.innerHTML = '<div class="chat-empty">Waiting for FRIDAY interaction...</div>';
      return;
    }
    list.innerHTML = messages.map(chatMessageHtml).join("");
    list.scrollTop = list.scrollHeight;
  }

  function renderConversations(messages) {
    const list = $("conversation-history");
    if (!list) return;
    if (!messages || !messages.length) {
      list.innerHTML = '<div class="chat-empty">No conversation history yet.</div>';
      return;
    }
    list.innerHTML = messages.map(chatMessageHtml).join("");
    list.scrollTop = list.scrollHeight;
  }

  $("chat-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("chat-input");
    const sendBtn = e.target.querySelector('button[type="submit"]');
    const text = input.value.trim();
    if (!text) return;
    if (sendBtn) sendBtn.disabled = true;
    try {
      const result = api().send_text ? await api().send_text(text) : { accepted: false, message: "Server not ready." };
      if (result && result.accepted) {
        input.value = "";
      }
    } catch (_) {}
    if (sendBtn) sendBtn.disabled = false;
    input.focus();
  });

  $("chat-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("chat-form")?.requestSubmit();
    }
  });

  // --- Mic: Mute / Unmute toggle for the server voice channel ---
  function renderMicButton(paused) {
    const btn = $("mic");
    if (!btn) return;
    const activated = !!paused;
    if (btn.dataset.paused === (activated ? "true" : "false")) return;
    btn.dataset.paused = activated ? "true" : "false";
    btn.textContent = activated ? "Unmute" : "Mute";
    const st = $("mic-status");
    if (st) st.textContent = activated ? "Voice mode muted" : "Voice active — tap to mute";
  }

  $("mic")?.addEventListener("click", async () => {
    const btn = $("mic");
    if (btn) btn.disabled = true;
    const paused = !(btn && btn.dataset.paused === "true");
    try {
      if (!api().set_paused) {
        renderMicButton(paused);
        return;
      }
      const res = await api().set_paused(paused);
      renderMicButton(res && typeof res.paused === "boolean" ? res.paused : paused);
      const st = $("mic-status");
      if (st && res && res.message) st.textContent = res.message;
    } catch (_) {
      const st = $("mic-status");
      if (st) st.textContent = "Mute control unavailable";
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // --- Orb drawing ---
  function draw(now) {
    const w = canvas.parentElement.clientWidth;
    const h = canvas.parentElement.clientHeight;
    const cx = w / 2;
    const cy = h / 2;
    const base = Math.min(w, h) * 0.28;
    const color = current.color || COLORS.idle;

    if (analyser && audioData) {
      analyser.getByteTimeDomainData(audioData);
      let sum = 0;
      for (const v of audioData) sum += Math.abs(v - 128);
      intensity += (sum / audioData.length / 128 - intensity) * 0.2;
    }

    const activity = Math.min(1, intensity * 2.5 + (current.state === "idle" ? 0.03 : 0.12));

    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = "lighter";

    const glow = ctx.createRadialGradient(cx, cy, base * 0.1, cx, cy, base * 2);
    glow.addColorStop(0, color + "44");
    glow.addColorStop(0.4, color + "11");
    glow.addColorStop(1, color + "00");
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, base * 2, 0, Math.PI * 2);
    ctx.fill();

    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, base * 0.35);
    core.addColorStop(0, color + "aa");
    core.addColorStop(0.6, color + "33");
    core.addColorStop(1, color + "00");
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(cx, cy, base * 0.35, 0, Math.PI * 2);
    ctx.fill();

    for (const p of particles) {
      p.angle += p.speed * (1 + activity * 10);
      const wave = Math.sin(now * 0.001 + p.phase + p.angle * 3) * (0.02 + activity * 0.08);
      const r = base * (p.radius + wave);
      const x = cx + Math.cos(p.angle) * r;
      const y = cy + Math.sin(p.angle) * r * 0.88;
      const alpha = p.layer === 0 ? "cc" : p.layer === 1 ? "88" : "55";
      ctx.fillStyle = color + alpha;
      ctx.beginPath();
      ctx.arc(x, y, p.size * (1 + activity * 0.8), 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.globalCompositeOperation = "source-over";
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(draw);
})();