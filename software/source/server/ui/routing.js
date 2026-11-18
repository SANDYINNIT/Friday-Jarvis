/* Field Notes read-only route and duty views. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const caps = {brain:'Brain', vision:'Vision', listen:'Listening', voice_out:'Voice', workflow:'Workflows'};
  const providers = {modal:'Modal', groq:'Groq', gemini:'Gemini', openrouter:'OpenRouter',
    ollama:'Ollama', deepgram:'Deepgram', 'faster-whisper':'Faster Whisper', 'edge-tts':'Edge TTS',
    'windows-sapi':'Windows SAPI', n8n:'n8n'};
  const provider = route => Object.hasOwn(providers,route.provider) ? providers[route.provider] : route.provider || 'Provider unavailable';
  const label = route => [provider(route), route.model].filter(Boolean).join(' · ');
  const config = {configured:'Configured', unconfigured:'Configuration unavailable', unknown:'Configuration unknown'};
  const catalog = {configured:'Configured',disabled:'Disabled',retired:'Retired'};
  const cooldown = {pool_cooldown:'Pool cooling down', model_cooldown:'Model cooling down'};
  function role(row) {
    if (!Number.isInteger(row.position)) return 'Resolver role unavailable';
    return row.position===1 ? 'First resolver candidate' : 'Fallback candidate';
  }
  const stamp = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : 'Not reported';
  const fact = (title,value) => `<div><dt>${escape(title)}</dt><dd>${escape(value)}</dd></div>`;
  const stateLabel = value => String(value ?? 'Unavailable').replaceAll('-', ' ');
  let data, selected = null, status = 'waiting', lastRead, actions;
  let readinessRead = 0;

  function replace(id, markup) {
    const el = $(id);
    if (el.dataset.markup === markup) return false;
    el.dataset.markup = markup;
    el.innerHTML = markup;
    return true;
  }

  function observation(item) {
    if (!item) return 'Not reported';
    const context = item.resolution === 'unbound' ? ' · Unbound' :
      item.resolution === 'session' ? ' · This session' : '';
    return label(item) + (item.deployment ? ' · ' + item.deployment : '') + context +
      (item.at ? ' · ' + stamp(item.at) : '');
  }

  async function refreshReadiness() {
    const current = ++readinessRead;
    const message = $('rollout-status');
    message.textContent = 'Reading local evidence…';
    try {
      const result = await actions.readiness();
      if (current !== readinessRead) return;
      if (result?.available !== true) throw new Error();
      const {identity, town, life, mcp, runtime, serving_history: history} = result;
      const policy = row => row.eligible === false ? 'Reported restriction' :
        row.eligible === true ? 'Reported policy allows' : 'Policy unknown';
      const saved = row => row.saved_serving === true ? 'Allow saved' :
        row.saved_serving === false ? 'Deny saved' : 'No explicit choice reported';
      const routes = result.routes.map(row =>
        `<li><strong>${escape([caps[row.capability], label(row), row.deployment].filter(Boolean).join(' · '))}</strong>
        <span class="fn-meta">${escape([row.kind === 'catalog' ? 'Catalog only' : 'Runtime: ' + config[row.configuration],
          stateLabel(row.enrollment || 'Service route'), row.catalog_status ? 'Catalog: ' + row.catalog_status : '',
          saved(row), policy(row)]
          .filter(Boolean).join(' · '))}</span></li>`).join('');
      const servers = mcp.servers.map(row => `<li>${escape(row.label || 'Unnamed server')} ·
        ${escape(row.enabled ? 'Enabled' : 'Disabled')} · ${escape(stateLabel(row.observed_connection))}
        · ${escape(stateLabel(row.cache))} discovery</li>`).join('');
      replace('rollout-evidence', `<dl class="ws-facts">
        ${fact('Identity integration', stateLabel(identity.integration_gate))}
        ${fact('Roster and Town reader', stateLabel(identity.reader_gate))}
        ${fact('Catalog', identity.revision ? 'Revision ' + identity.revision : stateLabel(identity.state))}
        ${fact('Unresolved catalog aliases', identity.unresolved_aliases ?? 'Unavailable')}
        ${fact('Town', town.state === 'loaded' ? `${town.verified_links} verified links · ${town.unlinked_residents} unlinked residents` : stateLabel(town.state))}
        ${fact('Background life', life.saved_ai == null ? stateLabel(life.state) + ' · preferences unavailable' :
          `${life.saved_ai} saved AI · ${life.saved_npc} saved NPC · ${stateLabel(life.state)}`)}
        ${fact('Life budget', stateLabel(life.budget))}
        ${fact('MCP configuration', stateLabel(mcp.state))}
        ${fact('Normal runtime readiness', stateLabel(runtime.normal_readiness))}
        ${fact('Local audio transport', stateLabel(runtime.audio_transport))}
        ${fact('Last service evidence', `Brain ${stateLabel(history.brain)} · Vision ${stateLabel(history.vision)}`)}
      </dl>${servers ? '<ul>' + servers + '</ul>' : ''}
      <details class="ws-diagnostics"><summary>Route enrollment (${result.route_count})</summary>
      <p class="fn-meta">Live eligibility and provider health are unverified.${result.route_count > result.routes.length ? ' Showing the first 100 rows.' : ''}</p>
      ${routes ? '<ol>' + routes + '</ol>' : '<p>No route data available.</p>'}</details>`);
      message.textContent = result.route_inventory === 'available' ? 'Local evidence refreshed.' : 'Route inventory unavailable.';
    } catch (_) {
      if (current === readinessRead) message.textContent = $('rollout-evidence').childElementCount
        ? 'Read failed. Showing earlier evidence.' : 'Readiness information is unavailable.';
    }
  }

  function renderCore() {
    replace('routing-duty',data ? Object.entries(caps).map(([key,name]) => {
      const item = data.capabilities[key];
      return `<article><h3>${name}</h3><dl class="ws-facts">${fact(status==='ok'?'Current duty':'Duty at last read',item.current ?
        observation(item.current) : item.duty_observable ? 'No active observation' : 'Unavailable')}${fact('Last successful service',observation(item.last_success))}</dl></article>`;
    }).join('') : '<p class="fn-meta">No routing observations available.</p>');
    const order = data?.routes.filter(row => Number.isInteger(row.position)) || [];
    replace('routing-order',order.length ? order.map(row => `<li value="${row.position}"><strong>${escape(label(row))}</strong>
      <span class="fn-meta">${escape([role(row),
        config[row.configuration],row.eligible===false?'Disabled':'',cooldown[row.cooldown]].filter(Boolean).join(' · '))}</span></li>`).join('') :
      '<li class="fn-meta">Resolver order is unavailable.</li>');
  }

  function badges(row) {
    if (row.serving) return status==='ok' ? 'Serving' : 'Was serving';
    if (row.catalog_status==='retired') return 'Retired';
    if (row.catalog_status==='disabled' || row.eligible===false) return 'Disabled';
    if (row.cooldown) return 'Cooling down';
    if (row.kind==='observation') return 'Unbound';
    if (row.kind==='catalog') return 'Catalog only';
    return config[row.configuration] || 'Unknown';
  }

  function renderDirectory() {
    const all = data?.routes || [];
    const query = $('routing-search').value.trim().toLowerCase();
    const cap = $('routing-filter').value;
    const results = all.filter(row => (!cap || cap===row.capability) &&
      [label(row), row.deployment, caps[row.capability], badges(row)].some(value=>String(value || '').toLowerCase().includes(query)));
    const focused = document.activeElement?.dataset.routeId;
    const markup = results.map(row=>`<button type="button" class="ws-row" data-route-id="${escape(row.id)}" aria-pressed="${row.id===selected}">
      <strong>${escape(row.model || provider(row))}</strong><span class="ws-state">${escape(badges(row))}</span>
      <span class="fn-meta">${escape([caps[row.capability],provider(row),row.deployment].filter(Boolean).join(' · '))}</span></button>`).join('');
    if (replace('routing-list',markup || `<p class="fn-meta">${data?'No matching routes.':'Route directory is unavailable.'}</p>`) && focused) {
      buttons().find(node => node.dataset.routeId === focused)?.focus({preventScroll:true});
    }
    const row = all.find(item=>item.id===selected);
    const link = $('routing-character');
    link.hidden = !(status==='ok' && row?.character_id && row.binding_id && data.catalog_revision);
    $('routing-detail-jump').disabled = !row;
    if (!row) {
      replace('routing-profile','<div class="fn-eyebrow">ROUTE PROFILE</div><h2>Select a route</h2><p class="fn-meta">Provider, capability and observation details appear here.</p>');
      return;
    }
    const policy = typeof row.eligible==='boolean' ? (row.eligible?'Allowed by reported policy':'Disabled by reported policy') : 'Policy unavailable';
    const duty = row.serving ? (status==='ok' ? 'Currently serving' : 'Serving at last read') :
      data.capabilities[row.capability].duty_observable ?
        (status==='ok' ? 'No current duty reported for this route' : 'No duty reported at last read') : 'Duty unavailable';
    const identity = row.character_id ? 'Exact catalog binding' :
      ['listen','voice_out','workflow'].includes(row.capability) ? 'Procedural service route' :
      data.identity==='disabled' ? 'Catalog reader disabled' : 'No verified character binding';
    replace('routing-profile',`<div class="fn-eyebrow">${escape(caps[row.capability])} ROUTE</div><h2>${escape(row.model || provider(row))}</h2>
      <dl class="ws-facts">${fact('Provider',provider(row))}${fact('Exact model',row.model || 'Unavailable or not applicable')}
      ${fact('Runtime configuration',config[row.configuration] || 'Unknown')}${fact('Routing role',role(row))}${fact('Eligibility',policy)}
      ${fact('Catalog state',catalog[row.catalog_status] || 'No exact catalog state')}${fact('Cooldown',cooldown[row.cooldown] || 'No cooldown reported')}
      ${fact('Current duty',duty)}${fact('Last successful observation',stamp(row.success_at))}
      ${fact('Last reported failure',stamp(row.failure_at))}${fact('Provider health','Unknown')}
      ${fact('Identity',identity)}</dl>
      <details class="ws-diagnostics"><summary>Route details</summary><dl class="ws-facts">
      ${fact('Public deployment',row.deployment || 'Unavailable')}${fact('Resolver position',row.position || 'Unavailable')}
      ${fact('Source',row.kind==='catalog'?'Catalog declaration only':row.kind==='observation'?'Unbound runtime observation':'Runtime configuration')}</dl></details>`);
  }

  function paint() {
    const text = status==='ok' ? 'Read-only routing snapshot' :
      data ? 'Reader unavailable · showing the last snapshot' :
      status==='waiting' ? 'Waiting for routing data…' : 'Routing data unavailable';
    const detail = data?.identity==='error' || data?.identity==='unavailable' ? ' · Catalog observations unavailable' : '';
    for (const name of ['core','agents']) {
      const element = $('routing-'+name+'-status');
      const message = text + detail + (status!=='ok' && lastRead ? ' · Last read ' + lastRead : '');
      if (element.textContent!==message) element.textContent=message;
    }
    renderCore();
    renderDirectory();
  }

  const buttons = () => [...$('routing-list').querySelectorAll('[data-route-id]')];
  function choose(id, focus = false) {
    selected = id;
    renderDirectory();
    if (focus) buttons().find(node=>node.dataset.routeId===id)?.focus();
  }

  window.FieldRouting = {
    init(config) {
      actions = config;
      $('rollout-readiness').ontoggle = event => {
        if (event.target.open) refreshReadiness();
      };
      $('rollout-refresh').onclick = refreshReadiness;
      $('core-open-agents').onclick = () => actions.open('view-agents');
      $('agents-open-core').onclick = () => actions.open('view-core');
      document.querySelectorAll('.routing-refresh').forEach(button => {
        button.onclick = () => actions.refresh();
      });
      $('routing-search').oninput = renderDirectory;
      $('routing-filter').onchange = renderDirectory;
      $('routing-list').onclick = event => {
        const id = event.target.closest('[data-route-id]')?.dataset.routeId;
        if (id) choose(id);
      };
      $('routing-list').onkeydown = event => {
        if (!['Home','End','ArrowUp','ArrowDown'].includes(event.key)) return;
        const list = buttons();
        const index = list.indexOf(document.activeElement);
        if (index<0) return;
        event.preventDefault();
        const next = {Home:0,End:list.length-1,ArrowUp:Math.max(0,index-1),ArrowDown:Math.min(list.length-1,index+1)};
        choose(list[next[event.key]].dataset.routeId,true);
      };
      $('routing-detail-jump').onclick = () => {
        $('routing-detail').scrollIntoView({block:'start'});
        $('routing-detail').focus({preventScroll:true});
      };
      $('routing-character').onclick = () => {
        const row = data?.routes.find(item=>item.id===selected);
        if (status==='ok' && row?.character_id && row.binding_id && data.catalog_revision) {
          actions.roster(row.character_id,data.catalog_revision);
        }
      };
    },
    receive(payload, transport) {
      status = transport==='ok' && payload?.available===true ? 'ok' :
        transport==='waiting' ? 'waiting' : 'unavailable';
      if (status==='ok') {
        data = payload;
        lastRead = new Date().toLocaleTimeString();
      }
      paint();
    }
  };
})();
