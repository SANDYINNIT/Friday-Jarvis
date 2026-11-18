/* Living Town presentation over the real simulation and shared identity reader. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const {node,geometry,avatar,cell} = window.FieldTownArt;
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let actions, data, selected, pin, svg, scene, layoutKey, commandPending;
  const actors = new Map();
  const camera = {zoom:1,x:null,y:null,follow:false};
  const rows = () => [
    ...(data?.residents || []).map(row=>({...row,key:'resident:'+row.source_id,kind:'resident'})),
    ...(data?.proposals || []).map(row=>({...row,key:'proposal:'+row.character_id,kind:'proposal'})),
    ...(data?.services || []).map(row=>({...row,key:'service:'+row.id,kind:'service',name:`${row.capability} · ${row.model}`}))
  ];
  const current = () => rows().find(row=>row.key===selected);
  const position = row => row?.kind==='resident' ? [row.x,row.y] : row?.kind==='proposal' ? row.cell : null;
  const statusLabel = row => ({installed:'Installed resident',archived_link:'Archived identity · resident retained',unresolved:'Unresolved legacy resident',legacy:'Legacy simulation resident'}[row.status] || 'Simulation resident');
  function showCamera() {
    const canFit = Boolean(scene);
    const canFollow = current()?.kind === 'resident';
    for (const id of ['fn-town-fit','fn-town-plus','fn-town-minus']) $(id).disabled = !canFit;
    $('fn-town-follow').disabled = !canFit || !canFollow;
    $('fn-town-follow').setAttribute('aria-pressed',String(camera.follow && canFollow));
    if (!scene) return;
    const [x,y,w,h] = scene.bounds, width=w/camera.zoom, height=h/camera.zoom;
    const target = camera.follow && canFollow ? position(current()) : null;
    const cx = target ? (target[0]+.5)*cell : camera.x ?? x+w/2;
    const cy = target ? (target[1]+.5)*cell : camera.y ?? y+h/2;
    const clamp = (value,lo,hi) => Math.min(hi,Math.max(lo,value));
    svg.setAttribute('viewBox',`${clamp(cx-width/2,x,x+w-width)} ${clamp(cy-height/2,y,y+h-height)} ${width} ${height}`);
  }
  function fit() { Object.assign(camera,{zoom:1,x:null,y:null,follow:false}); showCamera(); }
  function zoom(factor) {
    camera.zoom = Math.max(1,Math.min(3,camera.zoom*factor));
    const pos = position(current());
    if (camera.x === null && pos) [camera.x,camera.y] = pos.map(value=>(value+.5)*cell);
    showCamera();
  }
  function choose(row) {
    selected = row.key;
    pin = row.character_id ? {id:row.character_id,revision:data.catalog_revision} : null;
    if (row.kind !== 'resident') camera.follow = false;
    $('roster-town-handoff').hidden = true;
    paint();
    if ($('fn-town-legacy').open) actions.legacy(row.kind === 'resident' ? row.source_id : null);
  }
  function drawMap() {
    const map = $('fn-town-map');
    if (!data?.available || !data.walkable?.length) {
      map.textContent = data?.available ? 'No walkable layout reported.' : 'No loaded simulation to show.';
      scene = null; svg = null; layoutKey = null; actors.clear(); showCamera(); return;
    }
    const key = JSON.stringify([data.walkable,data.fixtures,data.locations.map(loc=>[loc.id,loc.name,loc.kind,loc.rect,loc.x,loc.y,loc.interaction_cells])]);
    if (key !== layoutKey) {
      map.replaceChildren();
      svg = node('svg',{'aria-hidden':'true'},map);
      scene = geometry(svg,data);
      layoutKey = key; actors.clear();
    }
    const moving = rows().filter(position);
    for (const [key,old] of actors) if (!moving.some(row=>row.key===key)) { old.element.remove(); actors.delete(key); }
    for (const row of moving) {
      const [x,y] = position(row);
      const prior = actors.get(row.key);
      const dx = prior ? x-prior.x : 0, dy = prior ? y-prior.y : 0;
      const direction = dx ? dx>0?'right':'left' : dy ? dy>0?'down':'up' : prior?.direction || 'down';
      const element = prior?.element || node('g',{class:'fn-town-actor','data-actor':row.key},scene.actors);
      element.replaceChildren();
      element.setAttribute('data-x',x); element.setAttribute('data-y',y);
      element.setAttribute('data-direction',direction);
      element.setAttribute('transform',`translate(${x*cell+4} ${y*cell+3})`);
      element.onclick = () => choose(row);
      if (row.kind==='resident') {
        if (selected===row.key) node('rect',{class:'fn-town-selection',x:-3,y:-2,width:30,height:29,rx:4},element);
        avatar(element,row.character_id || row.source_id,direction);
        if (row.serving?.length || row.route_failures?.length) {
          node('circle',{cx:23,cy:3,r:3,fill:row.serving?.length?'#376e35':'#9b5425',
            stroke:'#fffdf3','stroke-width':1},element);
        }
      } else node('circle',{class:'fn-town-proposed',cx:12,cy:12,r:10},element);
      const crowded = moving.some(other=>other.key!==row.key && Math.abs(position(other)[0]-x)<=1 && position(other)[1]===y);
      if (selected===row.key || row.kind==='resident' && !crowded) {
        const name = node('text',{x:12,y:32,class:'fn-town-name'},element);
        name.textContent = row.name;
      }
      const title = node('title',{},element); title.textContent = row.name;
      actors.set(row.key,{element,x,y,direction});
    }
    showCamera();
  }
  function list(container, entries) {
    const focus = document.activeElement?.dataset.townEntry;
    const markup = entries.map(row=>{
      const label = row.kind==='proposal' ? ' · proposed' : row.status==='archived_link' ? ' · archived' :
        row.kind==='resident' && row.status!=='installed' ? ' · legacy' : '';
      const duty = row.serving?.length ? ' · serving' : row.route_failures?.some(item=>item.state==='cooldown') ?
        ' · cooldown' : row.route_failures?.length ? ' · attempt failed' : '';
      return `<button type="button" data-town-entry="${esc(row.key)}" aria-pressed="${selected===row.key}">${esc(row.name+label+duty)}</button>`;
    }).join('') || '<span class="fn-meta">No entries reported.</span>';
    if (container.dataset.markup === markup) return;
    container.dataset.markup = markup;
    container.innerHTML = markup;
    for (const button of container.querySelectorAll('button')) {
      if (button.dataset.townEntry === focus) button.focus({preventScroll:true});
    }
  }
  function inspector(row) {
    const el = $('lab-inspector');
    const hadFocus = document.activeElement?.id === 'fn-town-roster';
    let markup = '<div class="fn-eyebrow">TOWN / INSPECTOR</div>';
    if (!row) markup += '<h2>Explore the Town</h2><p class="fn-meta">Choose a resident, placement or service station.</p>';
    else {
      markup += row.kind==='resident' ? `<div class="fn-identity-head"><span class="fn-portrait fn-town-portrait" aria-hidden="true"></span><h2>${esc(row.name)}</h2></div>` : `<h2>${esc(row.name)}</h2>`;
      if (row.kind==='resident') {
        const room = data.locations.find(location=>location.id===row.room);
        markup += `<p class="fn-meta">${esc(statusLabel(row))}</p><p class="fn-callout"><strong>${esc(row.activity)}</strong> · ${esc(room?.name || row.room)}<br>Goal: ${esc(row.goal)} · Cell ${row.x}, ${row.y}</p>`;
        if (row.character_id && row.source_name !== row.name) markup += `<p class="fn-meta">Source resident: ${esc(row.source_name)}</p>`;
        const capabilityName = cap => ({brain:'Brain',vision:'Vision'})[cap];
        if (row.serving?.length) markup += `<p class="fn-callout">Serving now · ${esc(row.serving.map(capabilityName).join(', '))}</p>`;
        for (const failure of row.route_failures || []) {
          markup += `<p class="fn-meta">${esc(capabilityName(failure.capability))} · ${failure.state==='cooldown'?'Route cooling down':'Recent attempt failed'}</p>`;
        }
        if (row.last_served?.length) markup += `<p class="fn-meta">Last successful duty · ${esc(row.last_served.map(capabilityName).join(', '))}</p>`;
        if (row.background_life) {
          const life = row.background_life;
          const labels = {npc:'NPC',ai_eligible:'AI eligible',waiting:'Waiting',budget_exhausted:'Budget exhausted',unavailable:'Unavailable',paused:'Paused'};
          markup += '<p class="fn-meta">Background life: '+(life.mode==='ai'?'AI':'NPC')+
            ' · '+esc(labels[life.state] || 'NPC')+'</p>';
          if (life.reason) markup += '<p class="fn-meta">'+esc(life.reason)+'</p>';
          for (const binding of row.bindings || []) {
            const capability = {brain:'Brain',vision:'Vision'}[binding.capability];
            const status = binding.serving_allowed === true ? 'Allowed by catalog'
              : binding.serving_allowed === false ? 'Blocked by catalog' : 'Permission unavailable';
            if (capability) markup += `<p class="fn-meta">${capability} · ${esc(binding.model)} · ${status}</p>`;
          }
        }
        if (row.blocked_reason) markup += `<p class="fn-meta">${esc(row.blocked_reason)}</p>`;
        markup += '<p class="fn-meta">Needs show urgency. Lower means more recovered.</p><dl>'+Object.entries(row.needs).map(([need,value])=>{
          const amount = Math.round(value*100)/100;
          return `<div class="fn-fact"><dt>${esc(need)}</dt><dd><meter aria-label="${esc(need)} urgency" min="0" max="100" value="${amount}"></meter> ${amount}</dd></div>`;
        }).join('')+'</dl>';
      } else if (row.kind==='proposal') {
        markup += `<p class="fn-callout">Proposed placement. This character is not installed in the simulation.</p><p class="fn-meta">${esc(row.cell ? 'Proposed cell '+row.cell.join(', ') : row.reason || 'No reachable placement.')}</p>`;
      } else markup += `<p class="fn-callout">Procedural station · ${esc(row.status)}. This representation has no independent reasoning or simulated movement.</p><p class="fn-meta">${esc(row.provider)} · ${esc(row.deployment)} · No verified map position reported.</p>`;
      if (row.character_id && data.identity_available) markup += '<button type="button" id="fn-town-roster">View in AI Roster →</button>';
    }
    if (el.dataset.markup === markup) return;
    el.dataset.markup = markup;
    el.innerHTML = markup;
    const portrait = el.querySelector('.fn-town-portrait');
    if (portrait) avatar(node('svg',{viewBox:'0 0 24 27'},portrait),row.character_id || row.source_id,'down');
    if (hadFocus) $('fn-town-roster')?.focus({preventScroll:true});
  }
  function paint() {
    drawMap();
    list($('fn-town-residents'),rows().filter(row=>row.kind!=='service'));
    list($('fn-town-services'),rows().filter(row=>row.kind==='service'));
    inspector(current());
    $('fn-town-legacy').hidden = !data?.available;
    if (!data?.available) $('fn-town-legacy').open = false;
    $('fn-town-clock').textContent = data?.available ? `Tick ${data.tick} · ${data.paused ? 'Paused' : data.ticking ? 'Playing' : 'Clock worker unavailable'}` : 'Simulation unavailable';
    for (const button of document.querySelectorAll('[data-town-action]')) {
      button.disabled = commandPending || !data?.controls_available || button.dataset.townAction==='play' && !data.ticking;
    }
    const journal = $('fn-town-journal');
    const events = data?.journal || [];
    const markup = events.slice().reverse().map(event=>`<li data-occurrence="${esc(event.event_key || '')}"><time>Tick ${esc(event.tick)}</time>${esc(event.text)}</li>`).join('') ||
      `<li>${data?.journal ? 'No committed events yet.' : 'Journal unavailable.'}</li>`;
    if (journal.dataset.markup !== markup) { journal.dataset.markup=markup; journal.innerHTML=markup; }
  }
  function render(payload,status) {
    data = status==='ok' ? payload : null;
    $('fn-town-status').textContent = status==='waiting' ? 'Loading Town…' : status!=='ok' ? 'Town could not be read. The bridge will retry.' :
      !data?.available ? 'No simulation is loaded. Opening this view does not start a store.' :
      data.identity_available ? `Catalog revision ${data.catalog_revision} · installed residents and proposed placements` : data.identity_reason || 'Catalog unavailable. Showing loaded legacy simulation only.';
    if (pin && data?.identity_available && data.catalog_revision===pin.revision) {
      const row = rows().find(row=>row.character_id===pin.id);
      selected = row?.key || null;
    }
    paint();
  }
  function accept(id, revision) {
    pin = {id,revision}; selected = null;
    fit();
  }
  async function transport(action) {
    if (commandPending) return;
    commandPending = true; paint();
    const result = $('fn-town-result');
    try {
      const reply = await actions.call('town_transport',action);
      if (reply?.ok !== true) { result.textContent = reply?.reason || 'Town control unavailable.'; return; }
      await actions.refresh();
      result.textContent = {play:'Clock resumed.',pause:'Clock paused.',step:'Simulation stepped.',save:'World saved.',load:'Saved world loaded.'}[action];
    } catch (_) { result.textContent = 'Town control could not be confirmed.'; }
    finally { commandPending=false; paint(); }
  }
  function init(config) {
    actions = config;
    $('fn-town-fit').onclick = fit;
    $('fn-town-plus').onclick = () => zoom(1.35);
    $('fn-town-minus').onclick = () => zoom(1/1.35);
    $('fn-town-follow').onclick = () => {
      camera.follow = !camera.follow;
      if (camera.follow && camera.zoom===1) camera.zoom=1.75;
      showCamera();
    };
    $('fn-town-map').addEventListener('keydown',event=>{
      const delta = {ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
      if (!delta || !scene || camera.zoom===1) return;
      event.preventDefault();
      const [x,y,w,h] = svg.getAttribute('viewBox').split(' ').map(Number);
      Object.assign(camera,{x:x+w/2+delta[0]*cell,y:y+h/2+delta[1]*cell,follow:false});
      showCamera();
    });
    for (const id of ['fn-town-residents','fn-town-services']) $(id).addEventListener('click',event=>{
      const key = event.target.closest('button')?.dataset.townEntry;
      const row = rows().find(item=>item.key===key);
      if (row) choose(row);
    });
    $('lab-inspector').addEventListener('click',event=>{
      const row = current();
      if (event.target.id==='fn-town-roster' && row?.character_id) actions.roster(row.character_id,data.catalog_revision);
    });
    $('fn-town-legacy').addEventListener('toggle',event=>{
      if (!event.target.open || !data?.available) return;
      const row = current();
      actions.legacy(row?.kind === 'resident' ? row.source_id : null);
    });
    for (const button of document.querySelectorAll('[data-town-action]')) button.onclick = () => transport(button.dataset.townAction);
  }
  window.FieldTown = {init,render,accept,selection:()=>pin ? [pin.id,pin.revision] : []};
})();
