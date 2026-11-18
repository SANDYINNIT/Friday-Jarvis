/* Native HUD controls only. Ordinary browser views keep their own chrome. */
(() => {
  "use strict";
  const group = document.getElementById("window-controls");
  if (!group) return;
  const buttons = [...group.querySelectorAll("button")];
  const maximize = document.getElementById("window-maximize");
  let ready = false, busy = false, reading = false, revision = 0, errorTimer;
  let openingReceipt, openingPending;
  const api = () => window.pywebview?.api;
  function error() {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = "Window action unavailable. Your current work is unchanged.";
    toast.classList.add("show");
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => toast.classList.remove("show"), 3600);
  }
  function paint(state) {
    window.FieldEntrance?.observe(state?.opening ? state : state?.opening_revision);
    const observed=window.FieldEntrance?.snapshot(), opening=state?.opening;
    const outcomes={'Playing':'playing','Complete':'complete','Dismissed':'dismissed',
      'Reduced motion is enabled':'reduced_motion','Low-power rendering is enabled':'low_power',
      'Motion preferences are unavailable':'preferences_unavailable','Opening animation is unavailable':'animation_unavailable'};
    const outcome=outcomes[observed?.reason];
    const key=opening&&`${opening.epoch}:${opening.revision}:${outcome}`;
    if(outcome&&opening?.revision>0&&observed?.epoch===opening.epoch&&
        key!==openingReceipt&&key!==openingPending&&typeof api()?.report_window_opening==='function'){
      openingPending=key;
      Promise.resolve(api().report_window_opening(opening.epoch,opening.revision,outcome))
        .then(value=>{if(value?.ok===true)openingReceipt=key;}).catch(()=>{})
        .finally(()=>{if(openingPending===key)openingPending=null;});
    }
    ready = state?.available === true && state.integrated === true;
    group.hidden = !ready;
    document.documentElement.dataset.nativeWindow = String(ready);
    const maximized = state?.maximized === true;
    group.dataset.maximized = String(maximized);
    const label = typeof state?.maximized !== "boolean" ? "Maximize or restore window"
      : maximized ? "Restore window" : "Maximize window";
    maximize.setAttribute("aria-label", label);
    maximize.title = label;
  }
  async function refresh() {
    if (reading || busy) return;
    if (typeof api()?.get_window_state !== "function") { paint(null); return; }
    reading = true;
    const version = revision;
    try {
      const state = await api().get_window_state();
      if (version === revision) paint(state);
    } catch (_) {
      // Retain usable controls after a transient status failure. Actions
      // still require the owning native window on the backend.
    } finally { reading = false; }
  }
  async function act(action) {
    if (!ready || busy || typeof api()?.window_action !== "function") return;
    busy = true;
    revision++;
    buttons.forEach(button => { button.setAttribute("aria-disabled", "true"); });
    try {
      const result = await api().window_action(action);
      if (result?.ok !== true) error();
    } catch (_) { error(); }
    finally {
      busy = false;
      buttons.forEach(button => { button.setAttribute("aria-disabled", "false"); });
      refresh();
    }
  }
  group.addEventListener("click", event => {
    const button = event.target.closest("button[data-window-action]");
    if (button && group.contains(button)) act(button.dataset.windowAction);
  });
  // Only these noninteractive, explicitly designated header surfaces drag.
  // Neither the sidebar nor a container around Stop/buttons is draggable.
  for (const area of document.querySelectorAll(".topbar-drag, .page-crumb")) {
    area.addEventListener("mousedown", down => {
      if (down.button !== 0 || down.detail > 1 || !ready) return;
      down.preventDefault();
      function clear() {
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", clear);
        window.removeEventListener("blur", clear);
      }
      function move(event) {
        if (!(event.buttons & 1)) { clear(); return; }
        if (Math.abs(event.screenX - down.screenX) + Math.abs(event.screenY - down.screenY) < 4) return;
        clear();
        act("drag");
      }
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", clear);
      window.addEventListener("blur", clear);
    });
    area.addEventListener("dblclick", event => {
      if (event.button === 0 && ready) { event.preventDefault(); act("toggle_maximize"); }
    });
  }
  window.addEventListener("pywebviewready", refresh);
  window.addEventListener("focus", refresh);
  setInterval(refresh, 1000);
  refresh();
})();
