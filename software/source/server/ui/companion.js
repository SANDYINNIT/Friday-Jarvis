/* Inert local teaching scenes. No eval, markup injection, remote URLs or input injection. */
(() => {
  'use strict';
  const $ = id => document.getElementById('companion-' + id);
  let actions, snapshot, tab = 'lessons', selected, dirty = false, pending = false;
  let base, edit = 0, read = 0, reading = false, navigation, returnFocus, draft;
  let yaw = .6, pitch = -.4, zoom = 1, sceneKey, drag;
  const fieldIds = ['cursor', 'style', 'top-panel', 'reduced-motion', 'teaching'];
  const preferenceForms = () => [...document.querySelectorAll('[data-companion-preferences]')];
  const formElement = (form, id) => form.querySelector(`[data-preference="${id}"]`);
  const report = (id, text, error = false) => {
    if (id === 'save-result') {
      for (const form of preferenceForms()) { const target=formElement(form,'result');target.textContent=text;target.dataset.error=String(error); }
    } else if ($(id)) { $(id).textContent = text; $(id).dataset.error = String(error); }
  };
  function button(text, click) { const b = document.createElement('button'); b.type = 'button'; b.textContent = text; b.onclick = click; return b; }
  function close() {
    if (actions.close) { actions.close(); return; }
    $('panel').hidden = true;
    document.querySelectorAll('.companion-trigger').forEach(b => b.setAttribute('aria-expanded', 'false'));
    if (returnFocus?.isConnected) returnFocus.focus();
  }
  function open(next, focus = true) {
    if (!['lessons', 'inbox', 'settings'].includes(next)) return;
    if ($('panel').hidden) returnFocus = document.activeElement;
    tab = next; $('panel').hidden = false;
    if($('heading-title'))$('heading-title').textContent=tab==='lessons'?'Teaching notes':tab==='inbox'?'Notifications':'Companion preferences';
    document.querySelectorAll('[data-companion-tab]').forEach(b => {
      if (b.closest('.companion-tabs')) b.setAttribute('aria-pressed', String(b.dataset.companionTab === tab));
      else b.setAttribute('aria-expanded', String(b.dataset.companionTab === tab));
    });
    $('records').hidden = tab === 'settings'; $('preferences-wrap').hidden = tab !== 'settings';
    paint(); if (focus) $('close').focus();
  }
  function values(form) {
    const field = id => formElement(form,id);
    return {cursor: field('cursor').value, style: field('style').value, top_panel: field('top-panel').checked,
      reduced_motion: field('reduced-motion').checked, teaching: field('teaching').checked};
  }
  function paintPrefs() {
    const prefs = snapshot?.preferences;
    if (!dirty && !pending && prefs?.available) {base = prefs.revision;draft={...prefs.values};}
    const native = snapshot?.native;
    const nativeFailures={preferences_unavailable:'Saved companion preferences could not be read. Saving is unavailable until they can be read again.',
      presentation_unavailable:'Windows could not show the companion. Capture protection remains required; an unprotected cursor is not enabled.',
      not_started:'The native companion is not running.',stopped:'The native companion is stopped.'};
    const nativeText = native?.available ?
      `Native companion attached. Cursor ${native.cursor_requested ? 'requested' : 'not requested'}. Top panel ${native.top_panel_visible ? 'visible' : 'hidden'}.` :
      (nativeFailures[native?.state] || 'Native companion unavailable. Saved preferences do not prove it is running.');
    for (const form of preferenceForms()) {
      formElement(form,'save').disabled=pending||!dirty||prefs?.available!==true;
      formElement(form,'cancel').disabled=pending||!dirty;
      for (const id of fieldIds) {
        const input=formElement(form,id),value=draft?.[id.replaceAll('-','_')];
        input.disabled=prefs?.available!==true;
        if(value!==undefined){if(input.type==='checkbox')input.checked=value;else input.value=value;}
      }
      formElement(form,'native').textContent=nativeText;
      const saved=formElement(form,'saved');
      if(saved) saved.textContent=prefs?.available ? `Saved cursor: ${prefs.values.cursor==='always'?'Always':prefs.values.cursor==='when_asked'?'When asked':'Off'} · ${dirty?'Unsaved edits':'Up to date'}. Applies on this computer; no recording permission is changed.` : 'Preferences unavailable or stale. Saving is disabled.';
      const entrance=formElement(form,'entrance');
      if(entrance) entrance.textContent=`Opening animation: ${window.FieldEntrance?.snapshot().reason || 'state unavailable in this surface'}.`;
    }
  }
  function current() { return snapshot?.rows?.find(r => r.id === selected); }
  function paint() {
    paintPrefs();
    const all = Array.isArray(snapshot?.rows) ? snapshot.rows : [];
    const unread = all.filter(r => r.content.kind === 'notification' && !r.seen).length;
    $('inbox-trigger').textContent = unread ? 'Inbox · ' + unread : 'Inbox';
    if (tab === 'settings') return;
    const rows = all.filter(r => r.content.kind === (tab === 'inbox' ? 'notification' : 'lesson'));
    if (!rows.some(r => r.id === selected)) selected = rows.at(-1)?.id;
    $('scope').textContent = tab === 'inbox' ?
      'Local messages from FRIDAY. A notification is not an executed task, approval request or confirmed workflow outcome.' :
      'Explanations and teaching scenes from this session. Diagrams and models are illustrations, not actions or verified facts.';
    const signature = JSON.stringify(rows.map(r => [r.id, r.content.title, r.seen, r.id === selected]));
    if ($('list').dataset.signature !== signature) {
      const fragment = document.createDocumentFragment();
      for (const row of rows) {
        const b = button(row.content.title + (row.seen ? '' : tab === 'inbox' ? ' · unread' : ''), () => { selected = row.id; paint(); });
        b.setAttribute('aria-pressed', String(row.id === selected));
        b.dataset.cardId = row.id;
        b.onkeydown = event => {
          if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
            event.preventDefault();
            const list = Array.from($('list').children), offset = event.key === 'ArrowRight' ? 1 : -1;
            const next = list[(list.indexOf(b) + offset + list.length) % list.length];
            if (next) { next.click(); $('list').querySelector('[aria-pressed=true]')?.focus(); }
          }
        };
        fragment.append(b);
      }
      $('list').replaceChildren(fragment); $('list').dataset.signature = signature;
    }
    const row = current();
    if($('meta'))$('meta').textContent=row ? `${tab==='inbox'?'Notification':row.content.scene?.type==='mesh'?'3D teaching box':row.content.scene?'Diagram teaching box':'Teaching note'} · this session · ${Math.floor(row.age_seconds/60)} min ago` : 'This session';
    $('title').textContent = row?.content.title || (tab === 'inbox' ? 'Nothing waiting here' : 'A little space to work things out');
    $('text').textContent = row?.content.text || (tab === 'inbox' ? 'No local notifications in this session.' : 'Ask FRIDAY for an explanation, diagram or 3D teaching model. No example activity is generated by opening this panel.');
    $('mark-read').hidden = !row || tab !== 'inbox' || row.seen;
    $('scene').hidden = !row?.content.scene;
    const mesh = row?.content.scene?.type === 'mesh';
    $('scene-controls').hidden = !mesh;
    $('scene-help').textContent = mesh ? '3D illustration · drag or use arrow keys to rotate; + / − to zoom. No model file or code is executed.' : row?.content.scene ? 'Diagram illustration · text equivalent above. No desktop controls are clicked.' : '';
    if (row?.content.scene) {
      const key = row.id + JSON.stringify(row.content.scene);
      if (sceneKey !== key) { sceneKey = key; yaw = .6; pitch = -.4; zoom = 1; draw(); }
    } else sceneKey = null;
  }
  function draw() {
    const scene = current()?.content.scene;
    if (!scene) return;
    const canvas = $('scene'), c = canvas.getContext('2d');
    c.clearRect(0, 0, 640, 480); c.fillStyle = '#edf0e3'; c.fillRect(0, 0, 640, 480);
    c.lineWidth = 2; c.strokeStyle = '#173a31'; c.fillStyle = '#173a31'; c.font = '24px system-ui';
    if (scene.type === 'mesh') {
      const vertices = scene.vertices;
      const mins = [0,1,2].map(i => Math.min(...vertices.map(v => v[i]))), maxs = [0,1,2].map(i => Math.max(...vertices.map(v => v[i])));
      const size = Math.max(1, ...maxs.map((v,i) => v-mins[i]));
      const transformed = vertices.map(v => {
        const [x,y,z] = v.map((n,i) => (n-(mins[i]+maxs[i])/2)/size);
        const xx = x*Math.cos(yaw) + z*Math.sin(yaw), zz = -x*Math.sin(yaw) + z*Math.cos(yaw);
        return [320+xx*300*zoom,240+(y*Math.cos(pitch)-zz*Math.sin(pitch))*300*zoom,y*Math.sin(pitch)+zz*Math.cos(pitch)];
      });
      const faces = scene.faces.map(f => ({f,z:f.reduce((s,i) => s+transformed[i][2],0)/f.length})).sort((a,b) => a.z-b.z);
      for (const {f,z} of faces) {
        c.beginPath(); f.forEach((i,n) => n ? c.lineTo(...transformed[i].slice(0,2)) : c.moveTo(...transformed[i].slice(0,2))); c.closePath();
        c.fillStyle = `hsl(145 21% ${Math.max(33,Math.min(78,58+z*30))}%)`; c.fill(); c.stroke();
      }
    } else if (scene.type === 'diagram') {
      const shapes = scene.shapes;
      const xs = shapes.flatMap(s => [s.x || 0,(s.x||0)+(s.w||0)]), ys = shapes.flatMap(s => [s.y||0,(s.y||0)+(s.h||0)]);
      const left = Math.min(0,...xs), top = Math.min(0,...ys), scale = Math.min(1,580/Math.max(1,...xs.map(x => x-left)),420/Math.max(1,...ys.map(y => y-top)));
      c.save(); c.translate(30-left*scale,30-top*scale); c.scale(scale,scale);
      for (const s of shapes) {
        const x=s.x||0,y=s.y||0,w=s.w||0,h=s.h||0;
        if (s.kind === 'text') c.fillText(s.text||'',x,y);
        else if (s.kind === 'rect') { c.strokeRect(x,y,w,h); if(s.text)c.fillText(s.text,x+8,y+24); }
        else if (s.kind === 'ellipse') { c.beginPath();c.ellipse(x+w/2,y+h/2,Math.abs(w/2),Math.abs(h/2),0,0,Math.PI*2);c.stroke(); }
        else {
          c.beginPath();c.moveTo(x,y);c.lineTo(x+w,y+h);c.stroke();
          if (s.kind === 'arrow') { const a=Math.atan2(h,w);c.beginPath();c.moveTo(x+w,y+h);c.lineTo(x+w-12*Math.cos(a-.5),y+h-12*Math.sin(a-.5));c.lineTo(x+w-12*Math.cos(a+.5),y+h-12*Math.sin(a+.5));c.closePath();c.fill(); }
        }
      }
      c.restore();
    }
  }
  async function refresh() {
    if (reading) return;
    reading = true; const ticket = ++read;
    try {
      const value = await actions.call('get_companion');
      if (ticket !== read) return;
      if (!value?.available) throw Error('unavailable');
      snapshot = value;
      report('status', 'Local session · refresh does not capture your screen or start work.');
      paint();
      if (value.navigation && value.navigation.id !== navigation) {
        navigation = value.navigation.id;
        if (value.navigation.tab !== 'home') open(value.navigation.tab, false);
      }
    } catch (_) {
      if (ticket !== read) return;
      if (snapshot) snapshot.preferences = {...snapshot.preferences, available:false};
      report('status', snapshot ? 'Local runtime unavailable · retained cards may be stale. Preferences cannot be saved.' : 'Local companion unavailable. No screen or provider work was started.', true);
      paintPrefs();
    } finally { reading = false; }
  }
  window.FieldCompanion = {init(config) {
    actions = config;
    document.querySelectorAll('[data-companion-tab]').forEach(b => b.addEventListener('click', () => open(b.dataset.companionTab)));
    $('close').onclick = close;
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('panel').hidden) { close(); e.preventDefault(); } });
    for (const form of preferenceForms()) {
      for (const id of fieldIds) formElement(form,id).onchange = () => {
        if (!dirty) base = snapshot?.preferences?.revision;
        draft=values(form);dirty = true; edit++; report('save-result','Unsaved preferences.'); paintPrefs();
      };
      formElement(form,'cancel').onclick=()=>{if(pending)return;dirty=false;edit++;paintPrefs();report('save-result','Edits cancelled.');};
      form.onsubmit = async event => {
      event.preventDefault(); if(pending || !dirty || snapshot?.preferences?.available !== true)return;
      const revision=base, changes={...draft}, generation=edit;
      pending=true;read++;paintPrefs();report('save-result','Saving…');
      try {
        const result=await actions.call('set_companion',changes,revision);
        if (result?.ok !== true || result.preferences?.available !== true) {
          report('save-result', result?.message || 'Save not confirmed. Your edit is kept.', true);
        } else {
          snapshot.preferences=result.preferences;base=result.preferences.revision;
          if(generation===edit)dirty=false;
          report('save-result',generation===edit ? 'Saved on this computer. Native activity is reported separately.' : 'Earlier preferences saved. Your newer edit is not saved.');
        }
      } catch (_) { report('save-result','Could not save. Your edit is kept.',true); }
      finally {pending=false;read++;paintPrefs();refresh();}
      };
    }
    $('mark-read').onclick = async () => {
      const id=selected; $('mark-read').disabled=true;
      try {const result=await actions.call('acknowledge_companion',id);if(!result?.ok)report('status','Notification was not acknowledged.',true);await refresh();}
      catch(_){report('status','Could not acknowledge the notification.',true);}
      finally{$('mark-read').disabled=false;}
    };
    const canvas=$('scene');
    canvas.onpointerdown=e=>{if(current()?.content.scene?.type==='mesh'){drag={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId);}};
    canvas.onpointermove=e=>{if(drag){yaw+=(e.clientX-drag.x)/100;pitch+=(e.clientY-drag.y)/100;drag={x:e.clientX,y:e.clientY};draw();}};
    canvas.onpointerup=canvas.onpointercancel=()=>{drag=null;};
    canvas.onkeydown=e=>{if(current()?.content.scene?.type!=='mesh')return;const k=e.key;if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','-','='].includes(k))return;e.preventDefault();if(k==='ArrowLeft')yaw-=.2;if(k==='ArrowRight')yaw+=.2;if(k==='ArrowUp')pitch-=.2;if(k==='ArrowDown')pitch+=.2;if(k==='+'||k==='=')zoom=Math.min(2,zoom+.1);if(k==='-')zoom=Math.max(.4,zoom-.1);draw();};
    $('rotate').onclick=()=>{yaw+=.3;draw();};$('zoom-in').onclick=()=>{zoom=Math.min(2,zoom+.1);draw();};$('reset').onclick=()=>{yaw=.6;pitch=-.4;zoom=1;draw();};
    refresh();setInterval(()=>{if(!document.hidden)refresh();},1500);
    if (config.startTab) open(config.startTab, false);
  }};
})();
