/* Task ledger over existing Lab attempt and artifact contracts. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cancelReason = 'Cancelled from Workspace';
  const states = {pending:'Pending',assigned:'Assigned',queued:'Queued',running:'Running',
    awaiting_review:'Awaiting review',approved:'Result saved',rejected:'Rejected',failed:'Failed',paused:'Paused'};
  let actions, data, selected, artifactId, artifactToken = 0, pending = false;
  let reviewed = null, snapshotStatus = 'waiting';
  let linkedAttempt = null;
  const tasks = () => data?.tasks || [];
  const agents = () => data?.agents || [];
  const task = () => tasks().find(row=>row.id===selected);
  const worker = id => agents().find(row=>row.id===id);
  const ownerName = id => worker(id)?.name || 'Unknown worker';
  const attemptFor = row => (row?.attempts || []).find(attempt=>attempt.id===row.attempt);
  const available = name => snapshotStatus==='ok' && actions.has(name);
  function stateLabel(state, manual, reason) {
    if (state==='approved' && manual) return 'Approved';
    if (state==='paused' && reason===cancelReason) return 'Cancelled';
    return states[state] || 'Unknown state';
  }
  function badge(state, manual, reason) {
    const tone = state==='awaiting_review' ? 'review' : ['failed','rejected'].includes(state) ? 'error' : '';
    return `<span class="ws-state" data-tone="${tone}">${esc(stateLabel(state,manual,reason))}</span>`;
  }
  function time(value) {
    return Number.isFinite(value) && value>0 ? new Date(value*1000).toLocaleString() : 'Time not recorded';
  }
  function notify(text, error = false) {
    $('ws-status').textContent = text;
    $('ws-status').dataset.error = String(error);
  }
  function filtered() {
    const query = $('ws-search').value.trim().toLowerCase();
    const state = $('ws-filter').value;
    return tasks().filter(row=>(!state || row.state===state) &&
      [row.goal,ownerName(row.owner),worker(row.owner)?.role].join(' ').toLowerCase().includes(query))
      .sort((a,b)=>Number(b.updated_at || 0)-Number(a.updated_at || 0));
  }
  function replace(el, markup) {
    if (el.dataset.markup===markup) return false;
    el.dataset.markup = markup;
    el.innerHTML = markup;
    return true;
  }
  function list() {
    const rows = filtered();
    const hold = document.activeElement?.dataset.wsTask;
    $('ws-count').textContent = `${rows.length} ${rows.length===1?'task':'tasks'}`;
    const content = rows.map(row=>`<button type="button" class="ws-row" data-ws-task="${esc(row.id)}" aria-pressed="${row.id===selected}">
      <strong>${esc(row.goal || 'Untitled task')}</strong>${badge(row.state,row.approval_required,attemptFor(row)?.reason)}
      <span class="fn-meta">${esc(ownerName(row.owner))}</span></button>`).join('');
    const empty = snapshotStatus==='waiting' ? 'Loading tasks…' : snapshotStatus!=='ok' ? 'Task ledger unavailable.' :
      tasks().length ? 'No tasks match these filters.' : 'No tasks recorded. Assign work when you have a request.';
    if (replace($('ws-list'),content || `<p class="fn-meta">${empty}</p>`) && hold) {
      [...$('ws-list').querySelectorAll('button')].find(button=>button.dataset.wsTask===hold)?.focus({preventScroll:true});
    }
  }
  function clearArtifact() {
    artifactToken++;
    artifactId = null;
    $('ws-artifact-content').hidden = true;
    $('ws-artifact-content').textContent = '';
    $('ws-read-status').textContent = '';
  }
  function select(id, focus = false) {
    linkedAttempt = null;
    if (selected!==id) clearArtifact();
    selected = id;
    paint();
    if (focus) [...$('ws-list').querySelectorAll('button')].find(button=>button.dataset.wsTask===id)?.focus();
  }
  function filteredSelection() {
    const rows = filtered();
    if (!rows.some(row=>row.id===selected)) select(rows[0]?.id);
    else list();
  }
  function fact(name,value) { return `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`; }
  function provenance(attempt) {
    if (attempt?.result_executor) {
      const route = attempt.result_executor;
      return [route.provider || route.pool, route.deployment, route.model, route.capability].filter(Boolean).join(' / ');
    }
    return attempt?.provider || attempt?.model ? `${attempt.provider || 'Unknown provider'} / ${attempt.model || 'Unknown model'}` : 'Executor not recorded';
  }
  function verified(attempt) {
    const route = attempt?.result_executor;
    return route?.resolution==='verified' && route.character_id && route.binding_id &&
      Number.isInteger(route.catalog_revision) && route.catalog_revision>0;
  }
  function executorDetails(attempt) {
    const result = attempt.result_executor;
    const observations = (attempt.execution_calls || []).flatMap(call=>call.candidates.map(candidate=>
      `<li>Call ${esc(call.call)} · ${esc([candidate.provider || candidate.pool,candidate.deployment,candidate.model].filter(Boolean).join(' / '))}
      · ${candidate.outcome==='returned_result'?'Returned a response':'Failed'}${candidate.http_status?' · HTTP '+esc(candidate.http_status):''}
      ${candidate.redirected?' · Redirected, executor unverified':''}</li>`));
    return (verified(attempt) ? `<p class="fn-meta">Verified at catalog revision ${esc(result.catalog_revision)}</p>
      <button type="button" data-ws-executor="${esc(attempt.id)}">View in AI Roster</button>` :
      result || attempt.provider || attempt.model ? '<p class="fn-meta">Catalog character unresolved. No identity was inferred.</p>' : '') +
      (observations.length ? `<details><summary>Provider observations</summary><ol>${observations.join('')}</ol></details>` : '');
  }
  function summary(row) {
    const attempt = attemptFor(row);
    const assigned = attempt?.worker || worker(row.owner);
    const description = {pending:'This request has no admitted attempt.',assigned:'Assigned to a worker. Execution has not been admitted.',
      queued:'An attempt is waiting for an execution slot.',running:'An attempt is executing. Review opens after a result is saved.',
      awaiting_review:'The result is saved and waiting for your decision.',approved:row.approval_required ?
        'You approved this result.' : 'A result was saved under the automatic policy. No manual decision was recorded.',
      rejected:'You rejected this result.',failed:'The attempt ended in an error.',paused:attempt?.reason===cancelReason ?
        'The attempt was cancelled. Its history is retained.' : 'This attempt is paused.'}[row.state] || 'No outcome was reported.';
    const exactReview = row.state==='awaiting_review' && row.approval_required===true && attempt?.state==='awaiting_review' && Boolean(attempt.id);
    const changedLink = Boolean(linkedAttempt?.id===row.id && linkedAttempt.attempt && linkedAttempt.attempt!==row.attempt);
    const alreadyReviewed = reviewed?.task===row.id && reviewed.attempt===attempt?.id;
    const review = exactReview && available('review_task') && !alreadyReviewed && !changedLink;
    const cancel = ['queued','running'].includes(row.state) && Boolean(attempt?.id);
    const retry = ['failed','paused'].includes(row.state);
    const run = row.state==='assigned';
    const buttons = [];
    if (review) buttons.push('<button type="button" class="ws-primary" data-ws-action="approve">Approve result</button>',
      '<button type="button" data-ws-action="reject">Reject result</button>');
    if ((cancel || retry || run) && available('world_command') && !changedLink) {
      const action = cancel?'cancel':retry?'retry':'run';
      buttons.push(`<button type="button" data-ws-action="${action}">${cancel?'Cancel attempt':retry?'Retry task':'Run assigned task'}</button>`);
    }
    let unavailable = '';
    if (changedLink) unavailable = 'This link points to an older attempt. Select the task in the ledger to act on its current attempt.';
    else if (row.state==='awaiting_review' && !review) unavailable = alreadyReviewed ? 'Decision recorded. Refreshing the ledger…' :
      'Review is unavailable without the exact current attempt and a review connection.';
    else if (['queued','running'].includes(row.state) && (!cancel || !available('world_command'))) unavailable = 'Cancellation is unavailable without the exact current attempt and a control connection.';
    else if ((retry || run) && !available('world_command')) unavailable = 'Execution controls are unavailable in this view.';
    const attemptLabel = attempt ? `Attempt ${row.attempts.indexOf(attempt)+1} · ${stateLabel(attempt.state,row.approval_required,attempt.reason)}` :
      row.attempt ? 'Current attempt details unavailable' : 'No admitted attempt';
    return `<div class="fn-eyebrow">SELECTED REQUEST</div><h2>${esc(row.goal || 'Untitled task')}</h2><p>${badge(row.state,row.approval_required,attempt?.reason)}</p>
      <p>${esc(description)}</p><dl class="ws-facts">${fact('Original task owner',row.original_owner || 'Not recorded')}${fact('Assigned worker',assigned?.name || assigned?.id || 'Not recorded')}${fact('Worker role',assigned?.role || 'Role not recorded')}
      ${fact('Current attempt',attemptLabel)}${fact('Actual executor',provenance(attempt))}${fact('Review policy',row.approval_required?'Manual review requested':'Automatic after save')}</dl>
      ${row.error ? `<p class="ws-error">${esc(row.error)}</p>` : ''}<div class="ws-actions">${buttons.join('')}</div>
      ${unavailable ? `<p class="fn-meta">${esc(unavailable)}</p>` : ''}`;
  }
  function history(row) {
    const attempts = row.attempts || [];
    const content = attempts.map((attempt,index)=>`<li><strong>Attempt ${index+1}</strong> ${badge(attempt.state,row.approval_required,attempt.reason)}
      ${linkedAttempt?.id===row.id && linkedAttempt.attempt===attempt.id ? '<span class="fn-meta">Linked from Tasks</span>' : ''}
      <div class="fn-meta">${esc(provenance(attempt))} · ${esc(time(attempt.updated_at || attempt.created_at))}</div>
      ${attempt.worker ? `<div class="fn-meta">Assigned worker · ${esc(attempt.worker.name || attempt.worker.id || 'Not recorded')}</div>` : ''}
      ${executorDetails(attempt)}
      ${attempt.error || attempt.reason ? `<div class="fn-meta">${esc(attempt.error || attempt.reason)}</div>` : ''}</li>`).reverse().join('');
    return `<section class="ws-section"><h3>Attempt history</h3>${content ? `<ol class="ws-history">${content}</ol>` : '<p class="fn-meta">No attempts recorded.</p>'}</section>`;
  }
  function artifactState(item) {
    if (item.revoked || item.file_state==='revoked') return 'Revoked';
    if (item.ok===false || item.error) return 'Artifact error';
    return {missing:'File missing',ok:'File available',historical:'Historical reference',unavailable:'File unavailable'}[item.file_state] || 'File status unknown';
  }
  const artifacts = row => (data?.artifacts || []).filter(item=>item.task===row.id);
  const readable = item => artifactState(item)==='File available';
  function shelf(row) {
    const items = artifacts(row);
    if (artifactId && !items.some(item=>item.id===artifactId && readable(item))) clearArtifact();
    const content = items.map(item=>{
      const origin = item.source_task ? tasks().find(task=>task.id===item.source_task) : row;
      const index = (origin?.attempts || []).findIndex(attempt=>attempt.id===item.attempt);
      const attempt = index<0 ? 'Attempt not recorded' : `Attempt ${index+1}`;
      const canRead = readable(item) && available('read_artifact');
      return `<li><strong>${esc(artifactState(item))}</strong> <span class="fn-meta">${esc(attempt)}</span>
        ${item.source_task ? `<span class="fn-meta">Linked subtask · ${esc(origin?.goal || 'Source request unavailable')}</span>` : ''}
        ${item.error && artifactState(item)!=='Revoked' ? `<p class="ws-error">${esc(item.error)}</p>` : ''}
        ${canRead ? `<button type="button" data-ws-artifact="${esc(item.id)}" aria-pressed="${artifactId===item.id}">Read result</button>` :
          readable(item) ? '<p class="fn-meta">Artifact reading is unavailable in this view.</p>' : ''}</li>`;
    }).join('');
    return `<section class="ws-section"><h3>Results &amp; artifacts</h3><p class="fn-meta">Saved output is separate from review and verification.</p>
      ${data.artifact_error ? '<p class="ws-error">The artifact shelf could not be read.</p>' : ''}
      ${content ? `<ul class="ws-artifact-list">${content}</ul>` : '<p class="fn-meta">No artifacts reported for this task.</p>'}</section>`;
  }
  function paint() {
    list();
    const row = task();
    $('ws-detail-jump').disabled = !row;
    const detail = $('ws-detail');
    const focused = document.activeElement;
    const focusAction = detail.contains(focused) ? focused.dataset.wsAction : null;
    const focusArtifact = detail.contains(focused) ? focused.dataset.wsArtifact : null;
    replace($('ws-summary'),row ? summary(row) : linkedAttempt ?
      '<div class="fn-eyebrow">REQUEST DETAILS</div><h2>Linked task unavailable</h2><p class="fn-meta">Choose a task from the ledger to continue.</p>' :
      '<div class="fn-eyebrow">REQUEST DETAILS</div><h2>Select a task</h2><p class="fn-meta">The worker, attempts and results will appear here.</p>');
    replace($('ws-history'),row ? history(row) : '');
    replace($('ws-artifacts'),row ? shelf(row) : '');
    const ids = row ? `<p>Task ID ${esc(row.id)}</p>${row.attempt ? `<p>Attempt ID ${esc(row.attempt)}</p>` : ''}` : '';
    replace($('ws-diagnostics'),row ? `<details class="ws-section ws-diagnostics"><summary>Ownership &amp; diagnostics</summary>
      <p>Worker links to AI Roster and Town are unavailable because this task has no explicit catalog mapping.</p>${ids}</details>` : '');
    const focusMatch = [...detail.querySelectorAll('button')].find(button=>focusAction && button.dataset.wsAction===focusAction ||
      focusArtifact && button.dataset.wsArtifact===focusArtifact);
    if (focusMatch && document.activeElement!==focused) focusMatch.focus({preventScroll:true});
    const owner = $('ws-owner');
    const prior = owner.value;
    if (replace(owner,'<option value="">Choose a worker</option>'+agents().map(agent=>`<option value="${esc(agent.id)}">${esc(agent.name)} · ${esc(agent.role)}</option>`).join(''))) owner.value = prior;
    $('ws-assign-submit').disabled = pending || !available('assign_task') || !agents().length;
    $('ws-new').disabled = !available('assign_task') || !agents().length;
    for (const button of detail.querySelectorAll('[data-ws-action]')) button.disabled = pending;
  }
  function render(payload,status) {
    snapshotStatus = status==='ok' && payload && !payload.error ? 'ok' : status==='waiting' ? 'waiting' : 'error';
    data = snapshotStatus==='ok' ? payload : null;
    if (snapshotStatus!=='ok') {
      clearArtifact();
      notify(snapshotStatus==='waiting' ? 'Loading the task ledger…' : 'The task ledger could not be read. The connection will retry.',snapshotStatus==='error');
    } else if ($('ws-status').textContent.startsWith('Loading') || $('ws-status').textContent.startsWith('The task ledger could not')) {
      notify(!actions.has('assign_task') ? 'Assignment is unavailable through this connection.' : !agents().length ? 'No task workers are available.' : '');
    }
    const filter = $('ws-filter');
    const prior = filter.value;
    const labels = [...new Set(tasks().map(row=>row.state))].sort().map(state=>`<option value="${esc(state)}">${esc(state==='paused'?'Paused or cancelled':state==='approved'?'Result saved or approved':states[state] || state)}</option>`).join('');
    if (replace(filter,'<option value="">All states</option>'+labels) && [...filter.options].some(option=>option.value===prior)) filter.value=prior;
    if (snapshotStatus==='ok' && !linkedAttempt && !tasks().some(row=>row.id===selected)) {
      select(filtered()[0]?.id);
    } else paint();
  }
  async function assign(event) {
    event.preventDefault();
    if (pending || !available('assign_task')) return;
    const goal = $('ws-goal');
    const draft = goal.value;
    if (!draft.trim()) { notify('Enter a request for the worker.',true); return; }
    pending = true; paint();
    try {
      const result = await actions.call('assign_task',$('ws-owner').value,draft.trim(),null,$('ws-policy').value==='manual');
      if (result?.ok !== true || !result.task?.id) { notify(result?.reason || 'Assignment could not be confirmed.',true); return; }
      if (goal.value===draft) goal.value = '';
      selected = result.task.id;
      linkedAttempt = null;
      clearArtifact();
      $('ws-search').value = '';
      $('ws-filter').value = '';
      if (!goal.value) closeForm();
      notify(result.execution?.ok===false ? `Assignment saved. Execution was not admitted or failed. ${result.execution.reason || ''}` : 'Assignment saved. Follow its attempt in the ledger.',result.execution?.ok===false);
      await actions.refresh();
    } catch (_) { notify('Assignment could not be confirmed. Your request is kept.',true); }
    finally { pending = false; paint(); }
  }
  async function act(action) {
    const row = task();
    const attempt = attemptFor(row);
    if (pending || !row) return;
    pending = true; paint();
    try {
      const isReview = action==='approve' || action==='reject';
      const isCancel = action==='cancel';
      const result = isReview ? await actions.call('review_task',row.id,action,attempt.id) :
        await actions.call('world_command',isCancel?'cancel_execution':'execute',isCancel ?
          {task_id:row.id,attempt:attempt.id,reason:cancelReason} : {task_id:row.id,retry:action==='retry'});
      const confirmed = result?.ok===true && (!isCancel || result.cancelled===true);
      if (confirmed && isReview) reviewed={task:row.id,attempt:attempt.id};
      notify(confirmed ? isReview ? 'Review decision saved.' : isCancel ? 'Attempt cancelled.' : 'Execution admitted. Follow its outcome in the ledger.' :
        result?.reason || 'The action could not be confirmed.',!confirmed);
      await actions.refresh();
    } catch (_) { notify('The action could not be confirmed.',true); }
    finally { pending=false; paint(); }
  }
  async function read(id) {
    const row = task();
    if (!row || !available('read_artifact') || !artifacts(row).some(item=>item.id===id && readable(item))) return;
    clearArtifact();
    artifactId = id;
    const token = artifactToken;
    paint();
    $('ws-read-status').textContent = 'Reading saved artifact…';
    try {
      const response = await actions.call('read_artifact',id);
      if (token!==artifactToken || selected!==row.id) return;
      if (response?.ok===true && response.id===id && typeof response.text==='string') {
        $('ws-artifact-content').textContent = response.text;
        $('ws-artifact-content').hidden = false;
        $('ws-read-status').textContent = 'Saved artifact content';
      } else $('ws-read-status').textContent = response?.reason || 'Artifact could not be read.';
    } catch (_) { if (token===artifactToken) $('ws-read-status').textContent = 'Artifact could not be read.'; }
  }
  function closeForm() {
    $('ws-assignment').hidden = true;
    $('ws-new').setAttribute('aria-expanded','false');
    if ($('ws-assignment').contains(document.activeElement)) $('ws-new').focus();
  }
  function init(config) {
    actions=config;
    $('ws-new').onclick=()=>{
      if (!$('ws-assignment').hidden) { closeForm(); return; }
      $('ws-assignment').hidden=false;
      $('ws-new').setAttribute('aria-expanded','true');
      $('ws-goal').focus();
    };
    $('ws-assignment').onsubmit=assign;
    $('ws-detail-jump').onclick=()=>{
      $('ws-detail').scrollIntoView({block:'start'});
      $('ws-detail').focus({preventScroll:true});
    };
    $('ws-search').oninput=filteredSelection;
    $('ws-filter').onchange=filteredSelection;
    $('ws-list').addEventListener('click',event=>{
      const button=event.target.closest('[data-ws-task]');
      if (button) select(button.dataset.wsTask,true);
    });
    $('ws-list').addEventListener('keydown',event=>{
      const buttons=[...$('ws-list').querySelectorAll('button')];
      const index=buttons.indexOf(document.activeElement);
      if (index<0 || !['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:
        Math.max(0,Math.min(buttons.length-1,index+(event.key==='ArrowDown'?1:-1)));
      select(buttons[next].dataset.wsTask,true);
    });
    $('ws-detail').addEventListener('click',event=>{
      const button=event.target.closest('button');
      if (button?.dataset.wsAction) act(button.dataset.wsAction);
      if (button?.dataset.wsArtifact) read(button.dataset.wsArtifact);
      if (button?.dataset.wsExecutor) {
        const attempt = task()?.attempts?.find(item=>item.id===button.dataset.wsExecutor);
        if (verified(attempt)) actions.roster(attempt.result_executor.character_id,attempt.result_executor.catalog_revision);
      }
    });
  }
  function fromTasks(id, attempt) {
    clearArtifact();
    selected = id;
    linkedAttempt = {id,attempt};
    $('ws-search').value = '';
    $('ws-filter').value = '';
    paint();
  }
  window.FieldWorkspace={init,render,fromTasks};
})();
