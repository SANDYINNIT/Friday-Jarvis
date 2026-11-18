/* Saved tone preferences share the companion authority. No local shadow store. */
(() => {
  'use strict';
  const $ = id => document.getElementById('personality-' + id);
  let call, state, base, draft, dirty = false, pending = false, edit = 0, ticket = 0;
  const report = text => { $('result').textContent = text; };
  function paint() {
    const prefs = state?.preferences, ready = state?.available && prefs?.available;
    if (!dirty && !pending && ready) { draft = {...prefs.values}; base = prefs.revision; }
    for (const [id, key] of [['banter','private_banter'],['suggestions','proactive_suggestions']]) {
      const field = $(id);
      field.disabled = !ready;
      if (draft) { if (field.type === 'checkbox') field.checked = draft[key]; else field.value = draft[key]; }
    }
    $('save').disabled = !ready || !dirty || pending;
    $('cancel').disabled = !dirty || pending;
    $('saved').textContent = ready ? `Saved banter: ${prefs.values.private_banter} · Suggestions: ${prefs.values.proactive_suggestions ? 'on' : 'off'}${dirty ? ' · Unsaved edits' : ''}` : 'Preferences unavailable or stale. Saving is disabled.';
    if (ready && state.suggestions) $('saved').textContent += ' · ' + state.suggestions;
    $('expression').textContent = state?.available ? `Expression: ${state.expression} · ${state.reason}. ${state.warning || ''}` : 'Expression unavailable.';
  }
  async function refresh() {
    const own = ++ticket;
    try {
      const value = await call('get_personality');
      if (own !== ticket) return;
      state = value?.available === true ? value : {available:false};
    } catch (_) { if (own !== ticket) return; state = {available:false}; }
    paint();
  }
  window.FieldPersonality = {init(config) {
    if (!$('form') || call) return;
    call = config.call;
    const change = () => {
      if (!dirty) base = state?.preferences?.revision;
      draft = {private_banter:$('banter').value, proactive_suggestions:$('suggestions').checked};
      dirty = true; edit++; paint(); report('Unsaved choices.');
    };
    $('banter').onchange = $('suggestions').onchange = change;
    $('cancel').onclick = () => { if (pending) return; dirty = false; edit++; paint(); report('Edits cancelled.'); refresh(); };
    $('refresh').onclick = refresh;
    $('form').onsubmit = async event => {
      event.preventDefault();
      if (pending || !dirty || !state?.preferences?.available) return;
      const generation = edit, changes = {...draft};
      pending = true; ticket++; paint(); report('Saving…');
      try {
        const result = await call('set_personality', changes, base);
        if (result?.ok !== true || result.preferences?.available !== true) report(result?.message || 'Save not confirmed. Your edits are kept.');
        else {
          state.preferences = result.preferences; base = result.preferences.revision;
          if (generation === edit) dirty = false;
          report(generation === edit ? 'Saved on this computer. Applies to the next eligible reply; speech stays neutral.' : 'Earlier choices saved. Your newer edits are not saved.');
        }
      } catch (_) { report('Save failed. Your edits are kept.'); }
      finally { pending = false; ticket++; paint(); refresh(); }
    };
    refresh(); // passive; no polling loop, store creation or provider call
  }};
})();
