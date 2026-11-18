/* Field Notes Tasks and Calendar. Each decision domain keeps its own contract. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fact = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const civilDate = value => new Date(`${value}T00:00:00Z`);
  const dayLabel = value => civilDate(value).toLocaleDateString(undefined, {timeZone:'UTC',weekday:'long',day:'numeric',month:'long',year:'numeric'});
  function timeLabel(value) {
    if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/i.test(value)) return value || 'Not recorded';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(undefined,{timeZone:'Europe/Madrid',dateStyle:'medium',timeStyle:'short'}) + ' · Europe/Madrid';
  }
  function replace(element, html) {
    if (element.dataset.markup === html) return false;
    element.dataset.markup = html;
    element.innerHTML = html;
    return true;
  }
  function paintRows(id, records, selected, empty) {
    const list = $(id);
    const focused = document.activeElement?.dataset.plannerKey;
    const markup = records.map(record => `<button type="button" class="ws-row" data-planner-key="${esc(record.key)}" aria-pressed="${selected===record.key}">
      <strong>${esc(record.title)}</strong><span class="ws-state" data-tone="${record.tone || ''}">${esc(record.badge)}</span>
      <span class="fn-meta">${esc(record.subtitle)}</span></button>`).join('');
    if (replace(list,markup || `<p class="fn-meta">${esc(empty)}</p>`) && focused) {
      [...list.querySelectorAll('button')].find(button=>button.dataset.plannerKey===focused)?.focus({preventScroll:true});
    }
  }
  function wireRows(id, select) {
    const list = $(id);
    list.onclick = event => {
      const key = event.target.closest('button')?.dataset.plannerKey;
      if (key) select(key);
    };
    list.onkeydown = event => {
      const buttons = [...list.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement);
      if (index < 0 || !['ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = {Home:0,End:buttons.length-1,ArrowUp:Math.max(0,index-1),ArrowDown:Math.min(buttons.length-1,index+1)}[event.key];
      const key = buttons[next].dataset.plannerKey;
      select(key);
      [...list.querySelectorAll('button')].find(button=>button.dataset.plannerKey===key)?.focus();
    };
  }
  function jump(id, target) {
    $(id).onclick = () => { $(target).scrollIntoView({block:'start'}); $(target).focus({preventScroll:true}); };
  }

  let actions;
  let personal, execution, personalStatus = 'waiting', executionStatus = 'waiting', selectedTask;
  let linkedDecision = null;
  let decisionPending = false;
  const drafts = new Map(), receipts = new Map();
  const taskStates = {open:'Open',done:'Done',pending:'Pending',fired:'Fired',cancelled:'Cancelled',answered:'Decision saved',ignored:'Ignored',expired:'Expired',
    assigned:'Assigned',queued:'Queued',running:'Running',awaiting_review:'Awaiting artifact review',failed:'Failed',paused:'Paused',rejected:'Rejected'};
  function taskRecords() {
    const records = [];
    const push = (domain,id,title,badge,source,row,tone) => {
      if (id == null) return;
      records.push({domain,id,key:`${domain}:${id}`,title:title || 'Untitled task',badge,subtitle:source,row,tone});
    };
    for (const row of personal?.pending || []) {
      const state = row.state==='open' && !row.actionable ? 'Response window closed' : row.state==='open' ? 'Awaiting decision' : taskStates[row.state];
      push('integration',row.correlation_id,row.question,state || 'Unknown','n8n · Owner decision',row,row.actionable?'review':'');
    }
    for (const row of personal?.tasks || []) push('personal',row.id,row.title,taskStates[row.status] || 'Unknown','Personal task',row);
    for (const row of personal?.reminders || []) push('reminder',row.id,row.title,taskStates[row.status] || 'Unknown','FRIDAY reminder',row);
    for (const row of execution?.tasks || []) {
      const owner = (execution.agents || []).find(agent=>agent.id===row.owner);
      const state = row.state==='approved' ? row.approval_required?'Approved':'Result saved' : taskStates[row.state];
      push('workspace',row.id,row.goal,state || 'Unknown',`Workspace · ${owner?.name || 'Worker unknown'}`,row,row.state==='awaiting_review'?'review':'');
    }
    return records;
  }
  function filteredTasks() {
    const type = $('planner-domain').value;
    const search = $('planner-search').value.trim().toLowerCase();
    return taskRecords().filter(record=>(!type || type===record.domain) &&
      [record.title,record.id,record.subtitle,record.badge].join(' ').toLowerCase().includes(search));
  }
  const currentTask = () => taskRecords().find(record=>record.key===selectedTask);
  function canDecide(record) {
    return record?.domain==='integration' && record.row.actionable===true && !receipts.get(record.key)?.ok &&
      personal?.sources?.pending==='ready' && personal.decision_available && actions.has('decide_pending_request');
  }
  function taskDetail(record) {
    if (!record) return linkedDecision ?
      '<div class="fn-eyebrow">LINKED OWNER DECISION</div><h2>Decision unavailable</h2><p class="fn-meta">The exact linked request is not in the current Tasks snapshot.</p>' :
      '<div class="fn-eyebrow">TASK DETAILS</div><h2>Select a record</h2><p class="fn-meta">Dates and available actions will appear here.</p>';
    const {row,id,domain} = record;
    let details = fact('Source',record.subtitle)+fact('State',record.badge);
    let help = '';
    if (domain==='personal') {
      const due = row.due_at ? `${row.due_at} · timezone not recorded` : row.due_label || 'No due date';
      details += fact('Due',due)+fact('Priority',row.priority || 'Not recorded');
      if (Number.isFinite(row.created_at)) details += fact('Created',timeLabel(new Date(row.created_at*1000).toISOString()));
      if (row.notes) help += `<p>${esc(row.notes)}</p>`;
      help += '<p class="fn-meta">Edit this personal task below. Integration decisions and Workspace reviews remain separate.</p>';
    } else if (domain==='reminder') {
      details += fact('Remind at',timeLabel(row.due_at))+fact('Created',timeLabel(row.created_at))+fact('Repeat',row.recurrence || 'Once');
      if (row.last_error) help += `<p class="ws-error">${esc(row.last_error)}</p>`;
      help += '<p class="fn-meta">Set reminders through FRIDAY chat. Reminder editing and cancellation are unavailable on this page.</p>';
    } else if (domain==='workspace') {
      if (Number.isFinite(row.updated_at)) details += fact('Updated',timeLabel(new Date(row.updated_at*1000).toISOString()));
      if (Number.isFinite(row.created_at)) details += fact('Created',timeLabel(new Date(row.created_at*1000).toISOString()));
      details += fact('Review policy',row.approval_required?'Manual review requested':'Automatic after save');
      if (row.error) help += `<p class="ws-error">${esc(row.error)}</p>`;
      help += `<div class="ws-actions"><button type="button" class="ws-primary" id="planner-workspace">${row.attempt?'Open exact attempt':'Open task in Workspace'} →</button></div>`;
    } else {
      details += fact('Correlation ID',id)+fact('Requested',timeLabel(row.created_at));
      if (row.resolved_at) details += fact('Resolved',timeLabel(row.resolved_at));
      if (row.answer) help += `<p><strong>Saved answer</strong><br>${esc(row.answer)}</p>`;
      if (row.actionable && !canDecide(record) && !receipts.get(record.key)?.ok) {
        help += `<p class="fn-meta">Direct decisions are unavailable here. Use this exact command in FRIDAY chat.</p><code class="planner-command">n8n decision ${esc(id)}: &lt;answer or ignore&gt;</code>`;
      } else if (canDecide(record)) {
        help += '<p class="fn-meta">Record an answer for this request. Type “ignore” to close it locally without sending a callback.</p>';
      }
      if (row.state==='answered' && !receipts.has(record.key)) {
        help += '<p class="fn-meta">The decision is saved. Callback receipt and application outcome are unavailable after reload. Workflow completion is unverified.</p>';
      }
    }
    return `<div class="fn-eyebrow">${domain==='integration'?'OWNER DECISION':'SELECTED RECORD'}</div><h2>${esc(record.title)}</h2><dl class="ws-facts">${details}</dl>${help}`;
  }
  function paintTasks() {
    const records = filteredTasks();
    const record = currentTask();
    const waiting = personalStatus==='waiting' || executionStatus==='waiting';
    $('planner-task-count').textContent = `${records.length} ${records.length===1?'record':'records'}`;
    paintRows('planner-task-list',records,selectedTask,waiting?'Loading records…':'No available records match these filters.');
    replace($('planner-task-summary'),taskDetail(record));
    $('planner-task-jump').disabled = !record;
    const form = $('planner-decision');
    form.hidden = !canDecide(record);
    $('planner-submit').disabled = decisionPending;
    if (form.dataset.key !== (record?.key || '')) {
      form.dataset.key = record?.key || '';
      $('planner-answer').value = drafts.get(record?.key) || '';
    }
    const feedback = receipts.get(record?.key);
    $('planner-decision-status').textContent = feedback?.message || '';
    $('planner-decision-status').dataset.error = String(feedback?.ok===false);

    const unavailable = [];
    for (const [name,label] of [['tasks','Personal tasks'],['reminders','Reminders'],['pending','n8n decisions']]) {
      const status = personal?.sources?.[name];
      if (status!=='ready' && personalStatus!=='waiting') unavailable.push(`${label} ${status==='error'?'could not be read':'unavailable'}`);
    }
    if (executionStatus==='error') unavailable.push('Workspace tasks could not be read');
    $('planner-task-status').textContent = unavailable.length ? unavailable.join(' · ') : waiting ? 'Loading task sources…' : 'Tasks and decisions from your local stores';
  }
  function chooseTask(key) { linkedDecision = null; selectedTask = key; paintTasks(); }
  function refreshSelection() {
    const rows = filteredTasks();
    if (linkedDecision) selectedTask = linkedDecision;
    else if (!rows.some(record=>record.key===selectedTask)) selectedTask = rows[0]?.key;
    paintTasks();
  }
  async function submitDecision(event) {
    event.preventDefault();
    const record = currentTask();
    if (decisionPending || !canDecide(record)) return;
    const input = $('planner-answer');
    const answer = input.value;
    if (!answer.trim()) {
      receipts.set(record.key,{ok:false,message:'Enter a decision or type ignore.'});
      paintTasks(); return;
    }
    decisionPending = true;
    receipts.set(record.key,{message:'Saving the decision…'});
    paintTasks();
    try {
      const response = await actions.call('decide_pending_request',record.id,answer);
      const saved = response?.ok===true && response.request?.correlation_id===record.id;
      receipts.set(record.key,{ok:saved,message:response?.message || 'The decision could not be confirmed.'});
      if (saved && drafts.get(record.key)===answer) {
        drafts.delete(record.key);
        if (currentTask()?.key===record.key) input.value='';
      }
      await actions.refreshTasks();
    } catch (_) {
      receipts.set(record.key,{ok:false,message:'The decision could not be confirmed. Your answer is kept. Check the request before retrying.'});
    } finally { decisionPending=false; paintTasks(); }
  }

  let agenda, calendarStatus = 'waiting', selectedEvent, day, allDates = false;
  function agendaRecords() {
    return (agenda?.events || []).map(row=>({key:row.id,title:row.title,row,
      badge:row.kind==='all_day'?'All day':row.kind==='timed'?row.start_label:'Unplaced',
      subtitle:row.kind==='unplaced' ? row.reason : allDates ? dayLabel(row.date)+' · '+row.source : row.source}));
  }
  function visibleEvents(records) {
    return records.filter(({row})=>row.kind!=='unplaced' && (allDates || row.date===day ||
      row.kind==='all_day' && row.date<day && day<row.end_date_exclusive));
  }
  function eventDetail(record) {
    if (!record) return '<div class="fn-eyebrow">EVENT DETAILS</div><h2>Select an event</h2><p class="fn-meta">Select a saved event, or deliberately create a local event above. No remote calendar is connected.</p>';
    const row = record.row;
    let facts = fact('Source',`Local schedule · ${row.source}`);
    if (row.kind==='timed') {
      facts += fact('Date',dayLabel(row.date))+fact('Starts',row.start_label)+fact('Ends',row.end_label + (row.end_date===row.date?'':' · '+dayLabel(row.end_date)))+fact('Display timezone',agenda.timezone);
    } else if (row.kind==='all_day') {
      facts += fact('Date',dayLabel(row.date))+fact('Time','All day');
      const end = civilDate(row.end_date_exclusive);
      end.setUTCDate(end.getUTCDate()-1);
      const last = end.toISOString().slice(0,10);
      if (last!==row.date) facts += fact('Through',dayLabel(last));
    } else facts += fact('Time status',row.reason);
    return `<div class="fn-eyebrow">SAVED EVENT</div><h2>${esc(row.title)}</h2><dl class="ws-facts">${facts}</dl>
      <p class="fn-meta">${row.editable?'Use the local event controls above. Changes affect only FRIDAY’s saved schedule.':'This source or ambiguous saved identity is read-only. No remote calendar is changed.'}</p><details class="ws-section ws-diagnostics"><summary>Saved timestamp</summary><p>${esc(row.stored_start || 'Not recorded')}</p></details>`;
  }
  function paintAgenda() {
    const records = agendaRecords(), visible = visibleEvents(records);
    const unplaced = records.filter(record=>record.row.kind==='unplaced');
    const selected = [...visible,...unplaced].find(record=>record.key===selectedEvent);
    $('agenda-heading').textContent = allDates ? 'All dates' : day ? dayLabel(day) : 'Agenda';
    $('agenda-timezone').textContent = agenda?.timezone || 'Europe/Madrid';
    const stale = Boolean(agenda) && calendarStatus!=='ok';
    $('agenda-status').dataset.stale = String(stale);
    const status = {waiting:'Loading calendar…',disconnected:'Local schedule is disconnected.',error:'The local schedule could not be read.'}[calendarStatus];
    $('agenda-status').textContent = stale ? `${status} Showing the last loaded agenda · ${timeLabel(agenda.read_at)}` : status || 'Events recorded in the local schedule';
    for (const id of ['agenda-previous','agenda-next','agenda-today','agenda-date','agenda-all']) $(id).disabled = !agenda;
    $('agenda-all').setAttribute('aria-pressed',String(allDates));
    if (day && $('agenda-date').value!==day) $('agenda-date').value = day;
    $('agenda-jump').disabled = !selected;
    $('agenda-unplaced').hidden = !unplaced.length;
    paintRows('agenda-list',visible,selectedEvent,agenda?'No scheduled events for this view.':'No schedule data is available.');
    paintRows('agenda-unplaced-list',unplaced,selectedEvent,'No unplaced events.');
    replace($('agenda-detail'),eventDetail(selected));
  }
  function setDay(value) { if (!value || Number.isNaN(civilDate(value).getTime())) return; day=value; allDates=false; paintAgenda(); }
  function shiftDay(amount) {
    const next = civilDate(day);
    next.setUTCDate(next.getUTCDate()+amount);
    setDay(next.toISOString().slice(0,10));
  }
  function init(config) {
    actions = config;
    wireRows('planner-task-list',chooseTask);
    const changeTaskFilter = () => { linkedDecision = null; refreshSelection(); };
    $('planner-search').oninput = changeTaskFilter;
    $('planner-domain').onchange = changeTaskFilter;
    $('planner-decision').onsubmit = submitDecision;
    $('planner-answer').oninput = event=>{ if (selectedTask) drafts.set(selectedTask,event.target.value); };
    $('planner-task-summary').onclick = event=>{
      const record = currentTask();
      if (event.target.id==='planner-workspace' && record?.domain==='workspace') actions.openWorkspace(record.id,record.row.attempt);
    };
    jump('planner-task-jump','planner-task-detail');
    jump('agenda-jump','agenda-detail');
    for (const id of ['agenda-list','agenda-unplaced-list']) wireRows(id,key=>{selectedEvent=key; paintAgenda();});
    $('agenda-date').onchange = event=>setDay(event.target.value);
    $('agenda-previous').onclick = ()=>shiftDay(-1);
    $('agenda-next').onclick = ()=>shiftDay(1);
    $('agenda-today').onclick = ()=>setDay(agenda?.today);
    $('agenda-all').onclick = ()=>{allDates=!allDates;paintAgenda();};
    $('agenda-chat').onclick = config.openChat;
  }
  window.FieldPlanner = {
    init,
    personalSelection() { const record=currentTask(); return record?.domain==='personal'?JSON.parse(JSON.stringify(record.row)):null; },
    calendarSelection() { const row=agendaRecords().find(r=>r.key===selectedEvent)?.row;return row?JSON.parse(JSON.stringify(row)):null; },
    calendarState() { return calendarStatus==='ok' && agenda?.editable ? {revision:agenda.revision}:null; },
    fromWorkflow(correlationId) {
      if (typeof correlationId!=='string' || !/^pr-[0-9a-f]{8}$/.test(correlationId)) return false;
      linkedDecision = 'integration:'+correlationId;
      selectedTask = linkedDecision;
      $('planner-domain').value='integration';
      $('planner-search').value='';
      paintTasks();
      return true;
    },
    tasks(payload,status) { personal=status==='ok'?payload:null; personalStatus=status; refreshSelection(); },
    workspace(payload,status) { execution=status==='ok' && !payload?.error ? payload:null; executionStatus=execution?'ok':status==='waiting'?'waiting':'error'; refreshSelection(); },
    calendar(payload,status) {
      if (status==='ok' && payload?.available===true) {
        agenda=payload; calendarStatus='ok';
        if (!day) day=payload.today;
      } else calendarStatus=status==='waiting'?'waiting':status==='missing' || payload?.status==='disconnected'?'disconnected':'error';
      paintAgenda();
    }
  };
})();
