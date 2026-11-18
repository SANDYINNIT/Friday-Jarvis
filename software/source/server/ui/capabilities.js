/* Read-only FRIDAY capability metadata. No invocation or permission actions. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const kinds = {tool:'Built-in tool',procedure:'Instruction procedure',extension:'Python skill file',
    mcp:'MCP tool',integration:'Configured connection'};
  const registrations = {schema_loaded:'Function schema loaded',stored:'File stored',
    cached:'Discovery metadata cached',configured:'Connection configured'};
  const sourceLabels = {builtin:'Built-in tools',skills:'Saved skills',mcp:'MCP adapter'};
  const sourceStates = {ready:'Read successfully',unsupported:'Safe discovery unsupported',
    unavailable:'Reader unavailable',error:'Could not read metadata'};
  const coverage = {
    builtin:'Only an already loaded Open Interpreter function schema can be reported here.',
    skills:'Saved files can be read from the configured FRIDAY project directory. Loaded kernel functions are not reported.',
    mcp:'Cached tool metadata requires an attached FRIDAY MCP adapter. This page does not establish a connection.'
  };
  const fact = (label,value) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const date = value => Number.isFinite(value) ? new Date(value*1000).toLocaleString() : 'Not reported';
  let data, state = 'waiting', selected, actions, generation = 0;

  function replace(id, html) {
    const target = $(id);
    if (target.dataset.markup === html) return false;
    const disclosure = target.querySelector('details');
    const open = disclosure?.open;
    const focus = document.activeElement === disclosure?.querySelector('summary');
    target.dataset.markup = html;
    target.innerHTML = html;
    if (open) target.querySelector('details')?.setAttribute('open','');
    if (focus) target.querySelector('summary')?.focus({preventScroll:true});
    return true;
  }
  function rows() { return Array.isArray(data?.rows) ? data.rows : []; }
  function badge(row) {
    if (row.metadata_state && row.metadata_state!=='ready') return 'Metadata unavailable';
    if (row.enabled===false) return row.kind==='procedure' || row.kind==='extension' ? 'Auto-import off' : 'Disabled';
    if (row.kind==='mcp' && row.allowlisted===false) return 'Outside allowlist';
    return kinds[row.kind] || 'Metadata entry';
  }
  function paintProfile() {
    const row = rows().find(item=>item.id===selected);
    $('capability-jump').disabled = !row;
    if (!row) {
      replace('capability-profile','<div class="fn-eyebrow">CAPABILITY DETAILS</div><h2>Select an item</h2><p class="fn-meta">Purpose, source and supported inputs appear here.</p>');
      return;
    }
    const fields = row.inputs || {};
    const inputRows = Array.isArray(fields.rows) ? fields.rows : [];
    const inputContent = fields.state==='ready' ? inputRows.length ? `<ul class="capability-inputs">${inputRows.map(input=>
      `<li><strong>${esc(input.name)}</strong> <span class="fn-meta">${esc(input.type)}${input.required===true?' · Required':input.required===false?' · Optional':' · Requirement not reported'}</span>
      ${input.description?`<p class="fn-meta capability-text">${esc(input.description)}</p>`:''}
      ${input.choices?.length?`<p class="fn-meta">Reported choices · ${input.choices.map(esc).join(', ')}</p>`:''}</li>`).join('')}</ul>` :
      '<p class="fn-meta">No parameters reported.</p>' :
      '<p class="fn-meta">Input metadata is unavailable or unsupported.</p>';
    const enabled = typeof row.enabled==='boolean' ? (row.enabled?'Enabled in reported configuration':'Disabled in reported configuration') : 'Not reported';
    const session = typeof row.session_attached==='boolean' ? (row.session_attached?
      'Session object attached · liveness unknown':'No session attached') : 'Not reported';
    let policy = '';
    if (row.kind==='tool') policy = fact('Code confirmation setting',typeof row.automatic==='boolean' ?
      (row.automatic?'Automatic code confirmation':'Code confirmation required') : 'Not reported')+
      fact('Model tool-call format',typeof row.function_calling==='boolean' ?
        row.function_calling?'Function calling supported':'Code blocks can use the separate execution path':'Not reported')+
      fact('Execution guard',row.execution_gate?'Attached · invocation checks still apply':'Attachment not reported');
    if (row.kind==='mcp') policy = fact('Tool allowlist',typeof row.allowlisted==='boolean' ?
      row.allowlisted?'Included in the adapter allowlist':'Outside the adapter allowlist':'Not reported')+
      fact('Adapter confirmation',typeof row.confirmation_required==='boolean' ?
        row.confirmation_required?'Required at invocation':'No confirmation required by reported adapter policy':'Not reported');
    if (row.kind==='integration' || row.kind==='mcp') policy += fact('Connection observation',session);
    if (row.connection_status) policy += fact('Last adapter state',row.connection_status.replaceAll('_',' '));
    if (row.kind==='mcp' && row.schema_supported===false) policy += fact('Model registration','Unsupported schema');
    const technical = fact('Exact item ID',row.id)+fact('Source ID',row.source_id)+
      (row.risk?fact('Adapter risk label',row.risk):'')+
      (row.discovered_at?fact('Metadata discovered',date(row.discovered_at)):'')+
      (row.updated_at?fact('File modified',date(row.updated_at)):'')+
      (row.kind==='procedure' || row.kind==='extension'?fact('Recorded use counter',String(row.uses || 0)):'');
    const stamp = row.discovered_at ? `<p class="fn-meta">Cached discovery from ${esc(date(row.discovered_at))}. Current availability is unverified.</p>` : '';
    const skillNote = row.kind==='procedure' || row.kind==='extension' ?
      '<p class="fn-meta">Stored file metadata does not establish that this skill is loaded in the current kernel.</p>' : '';
    replace('capability-profile',`<div class="fn-eyebrow">${esc(kinds[row.kind] || 'METADATA')}</div><h2>${esc(row.name)}</h2>
      <p class="capability-text">${esc(row.description || 'No purpose description is recorded.')}</p>
      <dl class="ws-facts">${fact('Source',row.source)}${fact('Registration',registrations[row.registration] || 'Not reported')}
      ${fact('Setting',row.kind==='procedure'||row.kind==='extension'?typeof row.enabled==='boolean'?
        row.enabled?'Skill auto-import is enabled':'Skill auto-import is disabled':'Auto-import setting unknown':enabled)}
      ${fact('Permission for this request','Not granted by this view')}${policy}
      ${fact('Current availability','Unverified')}${fact('Last verified success','Not reported')}</dl>${stamp}${skillNote}
      <section class="ws-section"><h3>Supported inputs</h3>${inputContent}${fields.truncated?'<p class="fn-meta">Input preview is incomplete.</p>':''}</section>
      ${row.kind==='procedure' && row.steps?.length?`<section class="ws-section"><h3>Saved instructions</h3><ol class="capability-steps">${row.steps.map(step=>`<li>${esc(step)}</li>`).join('')}</ol></section>`:''}
      ${row.verify?`<section class="ws-section"><h3>Saved verification guidance</h3><p class="capability-text">${esc(row.verify)}</p></section>`:''}
      ${row.metadata_state && row.metadata_state!=='ready'?'<p class="ws-error">This file’s metadata could not be read completely.</p>':''}
      <details class="ws-diagnostics"><summary>Technical details</summary><dl class="ws-facts">${technical}</dl>
      <p>Risk labels and use counters are stored metadata. Action consent, integration decisions and artifact reviews have separate authorization paths.</p></details>`);
  }
  function paint() {
    const sources = Object.entries(data?.sources || {});
    const failures = sources.filter(([,source])=>source.state==='error');
    $('capability-status').textContent = state==='loading' ? 'Reading FRIDAY’s inventory…' :
      state==='ready' ? failures.length ? 'Some inventory sources could not be read.' : data?.truncated ?
        'Inventory preview is incomplete.' : 'FRIDAY metadata · availability and request permissions are checked separately' :
      state==='waiting' ? 'Waiting for FRIDAY’s inventory…' : data ?
        `Reader unavailable · showing the last snapshot from ${date(data.read_at)}` : 'FRIDAY inventory reader is unavailable.';
    replace('capability-sources',sources.map(([name,source])=>`<p><strong>${esc(sourceLabels[name] || 'Source')}</strong> · ${esc(sourceStates[source.state] || 'Unknown state')}${source.truncated?' · Preview limited':''}<br>${esc(coverage[name] || '')}</p>`).join('') ||
      '<p>Inventory coverage has not been reported.</p>');
    const filter = $('capability-filter').value;
    const query = $('capability-search').value.trim().toLowerCase();
    const visible = rows().filter(row=>(!filter || row.kind===filter) &&
      [row.name,row.source,row.description,row.id].some(value=>String(value || '').toLowerCase().includes(query)));
    const focus = document.activeElement?.dataset.capabilityId;
    const empty = rows().length ? 'No matching capabilities.' :
      sources.length && sources.every(([,s])=>s.state==='ready') ? 'No capabilities in the reported inventory.' :
      'No items reported by the available readers. See inventory coverage.';
    if (replace('capability-list',visible.map(row=>`<button type="button" class="ws-row" data-capability-id="${esc(row.id)}" aria-pressed="${row.id===selected}">
      <strong>${esc(row.name)}</strong><span class="ws-state">${esc(badge(row))}</span><span class="fn-meta">${esc(row.source)}</span></button>`).join('') ||
      `<p class="fn-meta">${esc(empty)}</p>`) && focus) {
      buttons().find(button=>button.dataset.capabilityId===focus)?.focus({preventScroll:true});
    }
    paintProfile();
  }
  const buttons = () => [...$('capability-list').querySelectorAll('button')];
  function select(id, focus=false) {
    selected=id;
    paint();
    if (focus) buttons().find(button=>button.dataset.capabilityId===id)?.focus();
  }
  window.FieldCapabilities = {
    generation: () => generation,
    refresh() { generation++; state='loading'; paint(); actions.refresh(); },
    init(config) {
      actions=config;
      $('capability-refresh').onclick=()=>window.FieldCapabilities.refresh();
      $('capability-search').oninput=paint;
      $('capability-filter').onchange=paint;
      $('capability-jump').onclick=()=>{
        $('capability-detail').scrollIntoView({block:'start'});
        $('capability-detail').focus({preventScroll:true});
      };
      $('capability-list').onclick=event=>{
        const id=event.target.closest('[data-capability-id]')?.dataset.capabilityId;
        if (id) select(id);
      };
      $('capability-list').onkeydown=event=>{
        if (!['Home','End','ArrowUp','ArrowDown'].includes(event.key)) return;
        const nodes=buttons(), index=nodes.indexOf(document.activeElement);
        if (index<0) return;
        event.preventDefault();
        const next={Home:0,End:nodes.length-1,ArrowUp:Math.max(0,index-1),ArrowDown:Math.min(nodes.length-1,index+1)};
        select(nodes[next[event.key]].dataset.capabilityId,true);
      };
    },
    receive(payload,status) {
      if (status==='ok' && payload?.available===true) { data=payload; state='ready'; }
      else state=status==='waiting'?'waiting':'unavailable';
      paint();
    }
  };
})();
