/* Local workflow configuration and owner decisions. No execution actions. */
(() => {
  'use strict';
  const $ = id=>document.getElementById(id);
  const esc = value=>String(value ?? '').replace(/[&<>"']/g,c=>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fact = (label,value)=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const date = value=>{
    if (Number.isFinite(value)) return new Date(value*1000).toLocaleString();
    if (typeof value==='string') {
      const parsed=new Date(value);
      if (!Number.isNaN(parsed.getTime())) return parsed.toLocaleString();
    }
    return 'Not recorded';
  };
  const states={open:'Awaiting owner decision',answered:'Decision saved',ignored:'Ignored locally',expired:'Expired'};
  let data, selected, state='waiting', generation=0, actions, linkGeneration=0;
  const records=()=>data?.rows || [];
  const current=()=>records().find(row=>row.id===selected);
  const buttons=()=>[...$('workflow-list').querySelectorAll('button')];
  function replace(id,html) {
    const target=$(id);
    if (target.dataset.markup===html) return;
    const open=target.querySelector('details')?.open;
    const focused=document.activeElement===target.querySelector('summary');
    target.dataset.markup=html;
    target.innerHTML=html;
    if (open) target.querySelector('details')?.setAttribute('open','');
    if (focused) target.querySelector('summary')?.focus({preventScroll:true});
  }
  function profile() {
    const row=current();
    $('workflow-jump').disabled=!row;
    $('workflow-task').hidden=!(state==='ready' && row?.kind==='decision');
    if (!row) {
      replace('workflow-profile','<div class="fn-eyebrow">AUTOMATION DETAILS</div><h2>Select a record</h2><p class="fn-meta">Local configuration and owner decisions have separate records.</p>');
      return;
    }
    const call=row.call;
    const receipt=call ? Number.isInteger(call.http_status) ? `HTTP ${call.http_status}` :
      'No HTTP receipt recorded' : 'Not recorded';
    const application=typeof call?.application_ok==='boolean' ?
      call.application_ok?'Application reported success':'Application reported failure':'Unknown';
    const facts=row.kind==='definition'?
      fact('Integration setting',row.enabled?'Enabled in local configuration':'Disabled in local configuration')+
      fact('Workflow activation','Not reported')+fact('Current execution','Unknown')+
      fact('Trigger','Remote trigger configuration unavailable')+fact('Supported inputs','No input schema recorded')+
      fact('Last local call observation',call?date(call.at):'Not recorded')+
      fact('HTTP receipt',receipt)+fact('Reported application outcome',application)+
      fact('Confirmed workflow completion','Unverified') :
      fact('Decision state',states[row.state] || 'Unknown')+fact('Requested',date(row.created_at))+
      fact('Resolved',date(row.resolved_at))+fact('Callback receipt','Not retained in this reader')+
      fact('Reported application outcome','Unknown')+fact('Confirmed workflow completion','Unverified');
    replace('workflow-profile',`<div class="fn-eyebrow">${row.kind==='definition'?'WORKFLOW CONFIGURATION':'OWNER DECISION'}</div><h2>${esc(row.name)}</h2>
      ${row.description?`<p class="workflow-text">${esc(row.description)}</p>`:''}
      <dl class="ws-facts">${fact('Source',row.source)}${facts}</dl>
      <p class="fn-meta">${row.kind==='definition'?
        'This reader has no durable execution history or Workspace attempt link. The latest local call observation is session data.':
        'View or answer this exact request in Tasks. Saving a decision does not establish callback delivery or workflow completion.'}</p>
      <details class="ws-diagnostics"><summary>Record details</summary><dl class="ws-facts">
        ${fact('Source-qualified ID',row.id)}${row.correlation_id?fact('Correlation ID',row.correlation_id):''}
        ${fact('External execution ID','Not reported')}${fact('Last local read',date(data.read_at))}</dl></details>`);
  }
  function paint() {
    const definitions=data?.sources?.definitions;
    const live=state==='ready';
    const known=definitions && ['ready','partial'].includes(definitions.state);
    let connection='n8n connection is unobserved';
    if (known && !definitions.enabled) connection='n8n is disabled or unconfigured locally';
    else if (known && definitions.connection!=='unknown') {
      connection=definitions.connection==='connected'?'n8n was reachable at its last check':'n8n was disconnected at its last check';
      if (definitions.connection_stale || !live) connection+=' · stale observation';
    }
    if (!live && definitions?.enabled) connection+=' · showing the last local snapshot';
    $('workflow-connection').textContent=connection;
    const issues=[];
    for (const [key,title] of [['definitions','Workflow configuration'],['decisions','Owner decisions']]) {
      const source=data?.sources?.[key];
      if (!source) continue;
      if (source.state==='partial') issues.push(title+' is partially readable');
      else if (source.state==='error') issues.push(title+' could not be read');
      else if (source.state==='unavailable') issues.push(title+' is unavailable');
      if (source.truncated) issues.push(title+' preview is limited');
    }
    $('workflow-coverage').textContent=issues.join(' · ') || 'Local configuration and decision records. No connection check is made here.';
    $('workflow-status').textContent=state==='waiting'?'Waiting for local records…':state==='loading'?
      'Reading local records…':!live?data?'Reader unavailable · showing the last snapshot':'Local workflow reader unavailable':'';
    const query=$('workflow-search').value.trim().toLowerCase(), filter=$('workflow-filter').value;
    const rows=records().filter(row=>(!filter || filter===row.kind)&&
      [row.name,row.description,row.source,row.id].some(value=>String(value || '').toLowerCase().includes(query)));
    const focused=document.activeElement?.dataset.workflowId;
    const empty=records().length?'No matching records.':
      data && Object.values(data.sources).every(source=>source.state==='ready')?
        'No local workflow configurations or owner decisions.':'No records from the available sources.';
    replace('workflow-list',rows.map(row=>`<button type="button" class="ws-row" data-workflow-id="${esc(row.id)}" aria-pressed="${row.id===selected}">
      <strong>${esc(row.name)}</strong><span class="ws-state">${esc(row.kind==='definition'?'Configuration':states[row.state])}</span>
      <span class="fn-meta">${esc(row.source)}</span></button>`).join('') || `<p class="fn-meta">${empty}</p>`);
    if (focused) buttons().find(node=>node.dataset.workflowId===focused)?.focus({preventScroll:true});
    profile();
  }
  function select(id,focus=false) {
    linkGeneration++;
    selected=id;
    $('workflow-link-status').textContent='';
    paint();
    if (focus) buttons().find(node=>node.dataset.workflowId===id)?.focus();
  }
  window.FieldWorkflows={
    generation:()=>generation,
    init(config) {
      actions=config;
      $('workflow-refresh').onclick=()=>{
        generation++; linkGeneration++; state='loading'; paint(); actions.refresh();
      };
      $('workflow-search').oninput=paint;
      $('workflow-filter').onchange=paint;
      $('workflow-jump').onclick=()=>{
        $('workflow-detail').scrollIntoView({block:'start'}); $('workflow-detail').focus({preventScroll:true});
      };
      $('workflow-list').onclick=event=>{
        const id=event.target.closest('[data-workflow-id]')?.dataset.workflowId;
        if (id) select(id);
      };
      $('workflow-list').onkeydown=event=>{
        if (!['ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
        const nodes=buttons(), index=nodes.indexOf(document.activeElement);
        if (index<0) return;
        event.preventDefault();
        const next={Home:0,End:nodes.length-1,ArrowUp:Math.max(0,index-1),ArrowDown:Math.min(nodes.length-1,index+1)};
        select(nodes[next[event.key]].dataset.workflowId,true);
      };
      $('workflow-task').onclick=async()=>{
        const row=current();
        if (state!=='ready' || row?.kind!=='decision') return;
        const expected=++linkGeneration;
        $('workflow-link-status').textContent='Checking the exact decision…';
        try {
          const result=await actions.call('workflow_request_target',row.id);
          if (expected!==linkGeneration || current()?.id!==row.id || state!=='ready' || !actions.active()) return;
          if (result?.ok===true && result.domain==='integration' && result.correlation_id===row.correlation_id) {
            $('workflow-link-status').textContent='';
            actions.tasks(row.correlation_id);
          } else $('workflow-link-status').textContent='This decision is unavailable. Refresh local records.';
        } catch (_) {
          if (expected===linkGeneration) $('workflow-link-status').textContent='The decision could not be checked.';
        }
      };
    },
    receive(payload,status) {
      if (status==='ok' && payload?.available===true) { data=payload; state='ready'; }
      else { state=status==='waiting'?'waiting':'error'; linkGeneration++; }
      paint();
    }
  };
})();
