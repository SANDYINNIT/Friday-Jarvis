/* Field Notes directory and exact binding controls over the shared catalog. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let data = null, selected = null, revisionPin = null, filter = 'characters', query = '', actions = null, generation = 0;
  let pending = null, notice = null, minimumRevision = null;
  const caps = row => [...new Set((row.bindings || []).map(binding => binding.capability))].sort();
  function bindings(row) {
    const groups = new Map();
    for (const binding of row.bindings || []) {
      const key = JSON.stringify([binding.provider,binding.deployment,binding.model,binding.status]);
      if (!groups.has(key)) groups.set(key,{...binding,caps:[]});
      groups.get(key).caps.push(binding.capability);
    }
    return [...groups.values()];
  }
  const chip = text => '<span class="fn-chip">' + esc(text) + '</span>';
  const label = cap => ({brain:'Brain',vision:'Vision',listen:'Listening',stt:'Listening',tts:'Voice',voice_out:'Voice',workflow:'Workflow',n8n:'Workflow'}[cap] || cap);
  function portrait(id, service = false) {
    let hash = 2166136261;
    for (const char of String(id)) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
    const tones = ['#dce8d3','#eddfcd','#dfe1ee','#dfebe8'];
    const hair = ['#393f32','#79503c','#4a454c','#b29059'][(hash >>> 2) % 4];
    const shirt = ['#253d32','#695049','#4e506d','#3b6170'][(hash >>> 4) % 4];
    return '<span class="fn-portrait" title="Stable ID portrait" aria-hidden="true" style="background:'+tones[hash % 4]+'">' +
      '<svg viewBox="0 0 24 28" shape-rendering="crispEdges">' + (service
        ? '<path fill="#426a59" d="M4 7h16v14H4zM9 3h6v4H9z"/><path fill="#eff2dc" d="M7 11h3v3H7zm7 0h3v3h-3zM8 18h8v2H8z"/>'
        : '<path fill="'+hair+'" d="M5 3h14v13H5z"/><path fill="#d6a47f" d="M7 7h10v11H7zM9 18h6v3H9z"/><path fill="'+hair+'" d="M7 5h10v4H7z"/><path fill="#253d32" d="M8 11h2v2H8zm6 0h2v2h-2z"/><path fill="'+shirt+'" d="M5 21h14v7H5z"/><path fill="#ece8cf" d="M10 21h4v4h-4z"/>') + '</svg></span>';
  }
  function entries() {
    const roster = data && data.roster || {};
    const characters = (roster.characters || []).map(row => ({...row, kind:'character', key:row.id}));
    const services = (roster.services || []).map(row => ({...row, kind:'service', key:'service:'+row.id,
      name:row.name || label(row.capability) + ' · ' + row.model}));
    const legacy = (roster.legacy_residents || data && data.legacy_residents || [])
      .filter(row => row.resolution === 'unresolved').map(row => ({kind:'history',
        key:'resident:'+row.source_resident_id, name:row.name || row.source_state && row.source_state.name || row.source_resident_id,
        reference:row.source_resident_id, namespace:row.namespace || 'town', reason:'Resident ownership is unresolved.'}));
    const unresolved = (roster.unresolved || []).map((row, index) => ({...row, kind:'history',
      key:'unresolved:'+JSON.stringify([row.kind,row.namespace,row.legacy_id,row.route,row.decision_ids,index]),
      name:row.legacy_id || row.route && row.route.model || 'Unresolved mapping', reference:row.legacy_id,
      namespace:row.namespace || row.kind}));
    return [...characters,...services,...legacy,...unresolved];
  }
  function current(row) {
    return Object.entries(data && data.duty && data.duty.current || {})
      .filter(([, duty]) => duty.character_id === row.id && duty.resolution === 'bound');
  }
  function searchText(row) {
    return [row.name,row.id,row.provider,row.model,row.capability,row.namespace,row.reason,
      ...(row.bindings || []).flatMap(binding => [binding.provider,binding.model,binding.capability,binding.deployment])].join(' ').toLowerCase();
  }
  function visible() {
    return entries().filter(row => (filter === 'characters' ? row.kind === 'character' && row.status !== 'archived'
      : filter === 'services' ? row.kind === 'service'
      : row.kind === 'history' || row.kind === 'character' && row.status === 'archived') && searchText(row).includes(query.toLowerCase()));
  }
  function list(focusKey) {
    const held = focusKey || document.activeElement && document.activeElement.dataset.entry;
    const rows = visible();
    $('fn-roster-count').textContent = rows.length + (rows.length === 1 ? ' entry' : ' entries');
    $('fn-person-list').innerHTML = rows.map(row => '<button type="button" class="fn-person" data-entry="'+esc(row.key)+'" aria-pressed="'+(row.key === selected)+'">' +
      portrait(row.id || row.key,row.kind === 'service') + '<span><span class="fn-person-name">'+esc(row.name)+'</span><span class="fn-meta">'+esc(row.kind === 'character'
        ? caps(row).map(label).join(' · ') || 'No capability binding' : row.kind === 'service' ? 'Procedural service' : 'Unresolved history')+'</span></span><span class="fn-chips">'+
      (row.kind === 'character' ? current(row).map(([cap]) => chip('FRIDAY · '+label(cap))).join('') + (row.status === 'archived' ? chip('Archived') : '')
        : chip(row.kind === 'service' ? row.status || 'Service' : 'Read only')) + '</span></button>').join('') ||
      '<p class="fn-empty">'+(query ? 'No entries match your search.' : filter === 'characters' ? 'No configured characters are available in this view.' : 'No entries in this section.')+'</p>';
    $('fn-person-list').querySelectorAll('[data-entry]').forEach(button => button.addEventListener('click', () => choose(button.dataset.entry, true)));
    if (held) [...$('fn-person-list').querySelectorAll('[data-entry]')].find(el => el.dataset.entry === held)?.focus({preventScroll: !focusKey});
  }
  function choose(key, focus = false) {
    generation += 1;
    selected = key;
    if (revisionPin !== null && data?.selected_character_id !== key) revisionPin = null;
    if (!entries().some(row=>row.key===key && row.kind==='character')) revisionPin = null;
    list(focus ? key : null);
    profile();
  }
  function field(title, value) { return '<div class="fn-fact"><dt>'+esc(title)+'</dt><dd>'+value+'</dd></div>'; }
  function servingControls(row) {
    const life = row.background_life;
    const refreshing = minimumRevision !== null && data.catalog_revision < minimumRevision;
    const rows = (row.bindings || []).filter(binding => ['brain','vision'].includes(binding.capability));
    const writable = revisionPin === null && actions.canSaveLife() && Array.isArray(life?.routes);
    const disabled = pending || refreshing ? ' disabled' : '';
    const labels = {npc:'NPC',ai_eligible:'AI eligible',waiting:'Waiting',budget_exhausted:'Budget exhausted',unavailable:'Unavailable',paused:'Paused'};
    const selectedRoute = life?.routes?.find(route=>route.id===life.binding_id);
    return '<div class="fn-identity-block"><h3>Background life</h3><p class="fn-meta">Saved: ' +
      (life?.mode === 'ai' ? 'AI' : 'NPC') + ' · '+esc(labels[life?.state] || 'NPC')+'</p>' +
      (selectedRoute?'<p class="fn-meta">'+esc(selectedRoute.model+' · '+selectedRoute.deployment)+'</p>':'') +
      '<p class="fn-meta">'+esc(life?.reason || 'Movement and routines stay procedural. Serving the user is controlled separately.')+'</p>' +
      (!writable ? '<p class="fn-meta">Background AI is unavailable in this view.</p>' :
        '<div class="ws-actions">'+
        (life.mode==='ai' ? '<button type="button" data-life="npc"'+disabled+'>Use NPC</button>' : '')+
        (life.routes || []).filter(route=>route.id!==life.binding_id).map(route=>
          '<button type="button" data-life="ai" data-binding="'+esc(route.id)+'"'+disabled+'>Enable AI · '+esc(route.model)+(route.provider==='ollama'?' (local)':'')+'</button>').join('')+
        '<button type="button" data-life="bulk"'+disabled+'>Enable eligible nonlocal AI</button>'+
        (typeof data.life_budget_enabled==='boolean'?'<button type="button" data-life="budget"'+disabled+'>'+
          (data.life_budget_enabled?'Pause Life budget':'Enable Life budget')+'</button>':'')+
        '</div><p class="fn-meta">Bulk enable skips local and ambiguous deployments. Individual local opt-in is explicit.</p>')+'</div>' +
      '<div class="fn-identity-block"><h3>Can serve the user</h3><p class="fn-meta">Applies on the next route selection or retry. Current work stays with its admitted route. Routing still checks readiness and other restrictions.</p>' +
      (revisionPin !== null ? '<p class="fn-meta">Refresh to edit the latest catalog revision.</p>' : '') +
      rows.map(binding => {
        const allowed = binding.serving_allowed;
        const saved = binding.saved_serving;
        const editable = binding.serving_editable && revisionPin === null && actions.canSave();
        const setting = saved === true ? 'Saved: allowed' : saved === false ? 'Saved: disabled' : 'Default preference';
        const button = pending === binding.id ? 'Saving…' : allowed ? 'Disable serving' : 'Allow serving';
        return '<div class="fn-binding"><div><strong>'+esc(label(binding.capability))+'</strong><div class="fn-meta">'+
          esc(binding.model+' · '+binding.provider+' · '+binding.deployment)+'</div><div class="fn-meta">'+
          esc(setting)+' · '+(allowed === true ? 'Allowed by catalog' : allowed === false ? 'Blocked by catalog' : 'Permission unavailable')+'</div></div>'+
          (editable ? '<button type="button" class="fn-control" id="serve-'+esc(binding.id)+'" data-serving="'+esc(binding.id)+
            '" aria-label="'+esc(button+' for '+label(binding.capability)+' · '+binding.model)+'" '+(pending || refreshing ? 'disabled' : '')+'>'+esc(button)+'</button>'
            : '<span class="fn-meta">Read only</span>')+'</div>';
      }).join('') +
      '<p class="fn-meta" role="status">'+esc(notice?.characterId === row.id ? notice.message : '')+'</p></div>';
  }
  async function saveLife(row, button) {
    if (pending || revisionPin !== null || !actions.canSaveLife()) return;
    const operation = button.dataset.life, revision = data.catalog_revision;
    const enabled = !data.life_budget_enabled;
    pending = 'life';
    notice = null;
    profile();
    try {
      const result = operation==='budget' ? await actions.call('set_catalog_life_budget',enabled) :
        await actions.call('set_catalog_background_life',operation==='bulk'?null:row.id,
          button.dataset.binding || null,operation==='npc'?'npc':'ai',revision);
      if (!result?.ok || (operation==='budget' ? result.enabled!==enabled :
          !Number.isInteger(result.catalog_revision) || result.catalog_revision<revision)) {
        notice = {characterId:row.id,message:result?.message || 'The change was not confirmed.'};
        return;
      }
      minimumRevision = result.catalog_revision ?? revision;
      notice = {characterId:row.id,message:operation==='budget'?'Life budget preference saved.':
        operation==='bulk'?String(result.changed)+' nonlocal preferences saved.':'Background life preference saved.'};
    } catch (_) {
      notice = {characterId:row.id,message:'The change was not confirmed.'};
    } finally {
      pending = null;
      profile();
      actions.refresh();
    }
  }
  async function saveServing(row, binding) {
    if (pending || !binding.serving_editable || revisionPin !== null || !actions.canSave()) return;
    const revision = data.catalog_revision, allowed = !binding.serving_allowed;
    pending = binding.id;
    notice = null;
    profile();
    try {
      const result = await actions.call('set_catalog_serving', row.id, binding.id, binding.capability, allowed, revision);
      if (!result?.ok || result.binding_id !== binding.id || result.allowed !== allowed ||
          !Number.isInteger(result.catalog_revision) || result.catalog_revision < revision) {
        notice = {characterId:row.id, message:result?.message || 'The change was not confirmed. Refresh before retrying.'};
        return;
      }
      minimumRevision = result.catalog_revision;
      notice = {characterId:row.id, message:label(binding.capability)+' preference saved. Current work is unchanged.'};
    } catch (_) {
      notice = {characterId:row.id, message:'The change was not confirmed. Refresh before retrying.'};
    } finally {
      pending = null;
      profile();
      if (minimumRevision !== null) actions.refresh();
    }
  }
  function profile() {
    const row = entries().find(entry => entry.key === selected);
    const node = $('fn-profile');
    const held = node.contains(document.activeElement) ? document.activeElement.id : null;
    $('fn-profile-jump').disabled = !row;
    $('fn-profile-jump').textContent = row ? 'Read '+row.name+'’s profile ↓' : 'Choose an entry to read its profile';
    if (!row) { node.innerHTML = '<div class="fn-eyebrow">CHARACTER PROFILE</div><h2>'+(selected ? 'Selection unavailable' : 'Meet your roster')+'</h2><p class="fn-subtle">'+(selected ? 'The selected identity is not in this snapshot. No replacement was guessed.' : 'Choose an entry to explore its identity, capabilities and serving history.')+'</p>'; return; }
    const service = row.kind === 'service', history = row.kind === 'history';
    let html = '<div class="fn-eyebrow">'+(service ? 'PROCEDURAL REPRESENTATION' : history ? 'UNRESOLVED HISTORY' : row.status === 'archived' ? 'ARCHIVED CHARACTER' : 'CHARACTER PROFILE')+'</div>' +
      '<div class="fn-identity-head">'+portrait(row.id || row.key,service)+'<div><h2>'+esc(row.name)+'</h2><p class="fn-meta">'+esc(service ? 'A service, not an independent reasoning character' : history ? row.namespace : 'Persistent catalog identity')+'</p></div></div>';
    if (history) {
      html += '<p class="fn-subtle">'+esc(row.reason || 'Ownership has not been established.')+'</p><dl>'+field('Original reference',esc(row.reference || JSON.stringify(row.route || row.decision_ids || [])))+'</dl><p class="fn-callout">History remains with its original owner. This directory cannot reassign or edit it.</p>';
    } else if (service) {
      html += '<dl>'+field('Provider',esc(row.provider))+field('Model / service',esc(row.model))+field('Capability',esc(label(row.capability)))+field('Configuration',esc(row.status))+field('Service health','Not reported by the shared reader')+'</dl><p class="fn-callout">Procedural representation only. No independent reasoning, background AI or assistant serving permissions are implied.</p>';
    } else {
      const now = current(row);
      const historyRows = Object.entries(data.duty && data.duty.last_success || {}).filter(([, item]) => item.character_id === row.id);
      html += '<div class="fn-chips">'+caps(row).map(cap => chip(label(cap))).join('')+'</div><div class="fn-callout" id="fn-current-duty">'+esc(now.length ? 'Serving as FRIDAY · '+now.map(([cap]) => label(cap)).join(' + ') : 'No current FRIDAY duty reported')+'</div>';
      html += '<div class="fn-identity-block"><h3>Model bindings</h3>'+bindings(row).map(binding => '<div class="fn-binding"><div><strong>'+esc(binding.model)+'</strong><div class="fn-meta">'+esc(binding.provider)+' · '+esc(binding.deployment)+'</div></div><span class="fn-chips">'+binding.caps.sort().map(cap=>chip(label(cap))).join('')+chip(binding.status)+'</span></div>').join('')+'</div>';
      html += '<dl>'+field('Last served',historyRows.length ? historyRows.map(([cap,item]) => esc(label(cap))+' · '+esc(new Date(item.completed_at * 1000).toLocaleString())).join('<br>') : 'No successful serving history reported')+
        field('Provider health','Not reported; configuration and serving history are not health checks')+'</dl>';
      html += servingControls(row);
      const placement = row.placement || {};
      const unavailable = !data.available || row.status === 'archived' || !placement.cell;
      html += '<button type="button" class="fn-primary" id="fn-find-town" '+(unavailable ? 'disabled' : '')+'>Find in Town →</button><p class="fn-meta">'+esc(placement.simulation === 'existing_source_resident' ? 'Verified source resident; original ownership is preserved.' : placement.status === 'proposed' ? 'Placement proposed, not installed in the simulation.' : placement.reason || 'No active Town placement available.')+'</p>';
    }
    html += '<details class="fn-provenance"><summary>Identity & provenance</summary><code>'+esc(row.id || row.reference || row.key)+'</code><p class="fn-meta">Catalog revision '+esc(data.catalog_revision == null ? 'unavailable' : data.catalog_revision)+'</p></details>';
    node.innerHTML = html;
    if (held && $(held)) $(held).focus({preventScroll:true});
    const town = $('fn-find-town');
    if (town) town.addEventListener('click', () => handoff(row, town));
    node.querySelectorAll('[data-serving]').forEach(button => button.addEventListener('click', () => {
      const binding = row.bindings.find(item => item.id === button.dataset.serving);
      if (binding) saveServing(row, binding);
    }));
    node.querySelectorAll('[data-life]').forEach(button => button.addEventListener('click', () => saveLife(row,button)));
  }
  async function handoff(row, button) {
    const revision = data.catalog_revision, token = ++generation;
    button.disabled = true;
    try {
      const pair = await actions.call('get_identity_views', row.id, revision);
      if (token !== generation || selected !== row.id || !$('view-roster').classList.contains('active')) return;
      if (!pair || !pair.available || pair.selected_character_id !== row.id || pair.catalog_revision !== revision ||
          pair.roster.selected_character_id !== row.id || pair.town.selected_character_id !== row.id ||
          pair.roster.catalog_revision !== revision || pair.town.catalog_revision !== revision ||
          pair.roster.read_id !== pair.town.read_id) throw Error('Selection could not be confirmed');
      const chosen = pair.roster.characters.find(char => char.id === row.id);
      const banner = $('roster-town-handoff');
      banner.hidden = false;
      banner.dataset.characterId = row.id;
      banner.dataset.catalogRevision = String(revision);
      banner.textContent = 'Roster handoff: '+row.name + ' · catalog revision '+revision+'. '+(chosen.placement.simulation === 'existing_source_resident' ? 'Verified source resident selected.' : 'Proposed placement only; this character is not installed.');
      actions.town(chosen.placement.source_resident_id || null, row.id, revision);
    } catch (_) {
      if (token === generation) $('fn-roster-status').textContent = 'Town selection could not be confirmed. No other identity was selected. Retry the view.';
    } finally { if (button.isConnected) button.disabled = false; }
  }
  function render(payload, status) {
    if (!$('fn-person-list') || pending) return;
    data = status === 'ok' && payload ? payload : null;
    if (data?.available && minimumRevision !== null && data.catalog_revision >= minimumRevision) minimumRevision = null;
    const message = $('fn-roster-status');
    message.textContent = status === 'waiting' ? 'Loading the shared identity view…' : status !== 'ok' ? 'The identity view could not be loaded. Retry when the bridge is ready.' :
      !data.available ? data.reason || 'Catalog reader unavailable.' : 'Catalog revision '+data.catalog_revision+' · live duty and successful history are separate';
    $('fn-roster-refresh').disabled = status === 'waiting';
    $('fn-roster-revision').textContent = data && data.available ? 'Revision '+data.catalog_revision : 'Catalog unavailable';
    $('fn-roster-status').dataset.state = status !== 'ok' ? status : data.available ? 'ready' : 'unavailable';
    if (data && !data.available && (data.legacy_residents || []).length && !selected) filter = 'history';
    document.querySelectorAll('[data-roster-filter]').forEach(button => button.setAttribute('aria-pressed', String(filter === button.dataset.rosterFilter)));
    list(); profile();
  }
  function init(config) {
    actions = config;
    $('fn-profile-jump').addEventListener('click', () => {
      $('fn-profile').scrollIntoView({block:'start'});
      $('fn-profile').focus({preventScroll:true});
    });
    $('fn-roster-search').addEventListener('input', event => { query = event.target.value; list(); });
    $('fn-roster-refresh').addEventListener('click', () => { revisionPin = null; config.refresh(); });
    document.querySelectorAll('[data-roster-filter]').forEach(button => button.addEventListener('click', () => {
      filter = button.dataset.rosterFilter;
      document.querySelectorAll('[data-roster-filter]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      list();
    }));
    $('fn-person-list').addEventListener('keydown', event => {
      if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
      const buttons = [...$('fn-person-list').querySelectorAll('[data-entry]')];
      if (!buttons.length) return;
      event.preventDefault();
      const index = buttons.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : Math.max(0,Math.min(buttons.length-1,index+(event.key === 'ArrowDown' ? 1 : -1)));
      choose(buttons[next].dataset.entry,true);
    });
  }
  function fromTown(id, revision) {
    generation += 1;
    selected = id;
    revisionPin = revision;
    query = '';
    $('fn-roster-search').value = '';
    filter = entries().some(row=>row.id===id && row.status==='archived') ? 'history' : 'characters';
  }
  window.FieldRoster = {init,render,fromTown,selection:() => revisionPin !== null ? [selected,revisionPin] :
    [entries().some(row => row.key === selected && row.kind === 'character') ? selected : null]};
})();
