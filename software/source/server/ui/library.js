/* Read-only source views and explicit, typed memory actions. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names = ['memory','knowledge','conversations'];
  const state = Object.fromEntries(names.map(name => [name, {status:'waiting', selected:null}]));
  const rows = page => page?.rows || [];
  const known = value => value ?? 'Not recorded';
  const date = value => {
    if (typeof value === 'number') return new Date(value * 1000).toLocaleString();
    if (typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/i.test(value)) {
      const parsed = new Date(value);
      if (!Number.isNaN(parsed.getTime())) return parsed.toLocaleString();
    }
    return value ? String(value) + ' · timezone not recorded' : 'Not recorded';
  };
  const fact = (label,value) => `<div><dt>${esc(label)}</dt><dd>${esc(known(value))}</dd></div>`;
  const diagnostics = body => `<details class="ws-diagnostics"><summary>Record details</summary>${body}</details>`;
  const body = value => `<p class="library-text">${esc(value)}</p>`;
  const section = (eyebrow,title) => `<div class="fn-eyebrow">${esc(eyebrow)}</div><h2>${esc(title)}</h2>`;
  const button = (action,label) => `<button type="button" data-memory-action="${action}">${esc(label)}</button>`;
  let bridge, editor, confirmation, writing = false, transcriptGeneration = 0;
  let knowledgeSearchTimer, searchGeneration = 0, sourceGeneration = 0, importTarget = null, importing = false;
  const drafts = new Map();
  let draftContinuation=null;

  function replace(element, html) {
    if (element.dataset.markup === html) return false;
    element.dataset.markup = html;
    element.innerHTML = html;
    return true;
  }

  function memoryRecords(data) {
    const {canonical, experiences, events} = data;
    const facts = (page, retired) => rows(page).map(row => ({
      key:row.ref, type:retired?'history':'fact', domain:'fact', row, title:row.key, text:row.content,
      badge:row.deleted?'Forgotten':row.superseded_by?'Superseded':retired?'Expired':'Current fact',
      source:row.source || 'Shared canonical memory', live:!retired && canonical.state==='ready'
    }));
    const experiencesOf = (page, retired) => rows(page).map(row => ({
      key:row.ref, type:retired?'history':row.owner_resolved?'experience':'legacy', domain:'experience', row,
      title:row.text.split(/\r?\n/,1)[0].slice(0,86), text:row.text,
      badge:retired?'Forgotten':!row.owner_resolved?'Unresolved owner':row.pinned?'Pinned':'Experience',
      source:row.owner_name || 'Original ownership retained',
      live:!retired && row.owner_resolved && experiences.state==='ready' && events?.state==='ready'
    }));
    return [...facts(canonical?.current,false), ...experiencesOf(experiences?.current,false),
      ...rows(events).map(row=>({key:row.ref,type:'event',domain:'event',row,
        title:row.text?.split(/\r?\n/,1)[0].slice(0,86) || row.kind || 'World event',
        text:row.text,badge:'World event',source:row.owner_name || row.actor || 'Town journal'})),
      ...facts(canonical?.history,true), ...experiencesOf(experiences?.history,true)];
  }

  function knowledgeRecords(data) {
    const records = [];
    const imported = new Map(rows(data.imports).map(row=>[row.source_id,row]));
    const hits = state.knowledge.hits || [];
    for (const hit of hits) if (!imported.has(hit.source_id)) imported.set(hit.source_id,hit);
    for (const row of imported.values()) records.push({
      key:'import:'+row.source_id,type:'source',title:row.title,badge:'Indexed source',
      source:row.filename || 'Local import',updated:row.imported_at,row,
      hit:hits.find(hit=>hit.source_id===row.source_id && hit.version_id===row.version_id)
    });
    if (data.notes?.text) records.push({
      key:'notes',type:'source',title:'Project notes',text:data.notes.text,badge:'Source material',
      source:'Project notes document',updated:data.notes.updated_at,truncated:data.notes.truncated,
      retrieval:'Read directly from the document. No index status is available.'
    });
    for (const row of rows(data.canonical?.facts)) records.push({
      key:`fact:${row.id}`,type:'fact',title:row.key,text:row.content,badge:'Saved fact',
      source:row.source || 'Shared canonical memory',updated:row.updated_at,
      retrieval:'Current in the memory store. Independent validation is not recorded.',
      reference:`mem-${row.id}`,epistemic:row.epistemic
    });
    for (const [name, source] of [['Shared memory',data.canonical],['Lab memory',data.lab]]) {
      for (const row of rows(source?.summaries)) records.push({
        key:`${name}:summary:${row.id}`,type:'summary',title:row.memory_key || 'Summary of unavailable source',
        text:row.summary,badge:row.current?'Generated summary':'Stale summary',source:name,
        updated:row.updated_at,reference:`Summary ${row.id} · source mem-${row.memory_id}`,
        retrieval:row.current?'Current summary of a saved memory. Independent validation is not recorded.':
          'Historical summary. Its source was retired, is missing, or the summary was marked stale.',
        provenance:row.memory_source
      });
    }
    return records;
  }

  function archiveRecords(data) {
    const channel = {hud:'Desktop chat',voice:'Voice',telegram:'Telegram',http:'HTTP chat',n8n:'n8n'};
    return rows(data).map(row=>({key:row.id,
      title:row.title || row.id.slice(0,-5).replace(/_/g,' ').trim() || 'Saved conversation',
      source:date(row.updated_at), badge:channel[row.source] || 'Legacy transcript', version:row.updated_at}));
  }

  const builders = {memory:memoryRecords,knowledge:knowledgeRecords,conversations:archiveRecords};
  const recordsFor = name => state[name].data ? builders[name](state[name].data) : [];
  const selection = name => recordsFor(name).find(record => record.key === state[name].selected);

  function visibleRecords(name) {
    const query = $(name+'-library-search').value.trim().toLowerCase();
    const filter = $(name+'-library-filter')?.value;
    return recordsFor(name).filter(record => (!filter || record.type === filter) &&
      (record.hit || [record.title,record.text,record.source,record.badge,record.key].some(value=>String(value ?? '').toLowerCase().includes(query))));
  }

  function sourceStatus(name) {
    const {data,status} = state[name];
    if (status === 'waiting') return 'Waiting for the reader…';
    if (status !== 'ok') return data ? 'Reader failed. Showing the last loaded directory.' : 'Reader unavailable.';
    if (name === 'conversations') {
      if (data.state === 'unavailable') return data.warning || 'No saved archive is available.';
      if (data.state === 'error') return data.warning || 'Could not read the saved archive.';
      return [data.warning, data.truncated ? 'Showing the 100 most recently modified archives.' : ''].filter(Boolean).join(' ');
    }
    const sources = name === 'memory' ? [['Shared facts',data.canonical],['Experiences',data.experiences],['World events',data.events]] :
      [['Local index',data.imports],['Project notes',data.notes],['Shared facts',data.canonical],['Lab summaries',data.lab],
       ...(data.canonical?.summaries?.state==='unavailable' && data.lab?.summaries?.state!=='ready'?[['Stored summaries',data.canonical.summaries]]:[])];
    const messages = sources.filter(([,source])=>source && source.state!=='ready')
      .map(([label,source])=>label + (source.state==='error'?' could not be read.':' unavailable.'));
    if (sources.some(([,s])=>s?.truncated || s?.current?.truncated || s?.history?.truncated || s?.facts?.truncated || s?.summaries?.truncated)) {
      messages.push('Some sources show a limited preview.');
    }
    if (name==='knowledge' && state.knowledge.searchStatus) messages.push(state.knowledge.searchStatus);
    return messages.join(' ');
  }

  function paint(name) {
    const panel = state[name];
    const list = $(name+'-library-list');
    const focused = document.activeElement?.dataset.libraryKey;
    const status = sourceStatus(name);
    const indicator = $(name+'-library-source');
    if (indicator.textContent !== status) indicator.textContent = status;
    const items = visibleRecords(name);
    const markup = items.map(record=>`<button type="button" class="ws-row" data-library-key="${esc(record.key)}" aria-pressed="${record.key===panel.selected}">
      <strong>${esc(record.title)}</strong><span class="ws-state">${esc(record.badge)}</span><span class="fn-meta">${esc(record.source)}</span></button>`).join('');
    if (replace(list,markup || `<p class="fn-meta">${status ? '' : recordsFor(name).length ? 'No matching records.' : 'No records yet.'}</p>`) && focused) {
      [...list.querySelectorAll('button')].find(node=>node.dataset.libraryKey===focused)?.focus({preventScroll:true});
    }
    const record = selection(name);
    if (name === 'memory') paintMemory(record);
    if (name === 'knowledge') paintKnowledge(record);
    if (name === 'conversations') paintArchive(record);
  }

  function paintMemory(record) {
    const target = $('memory-library-record');
    $('memory-library-add').disabled = !bridge.has('add_memory');
    if (!record) {
      replace(target,section('MEMORY RECORD','Select a memory')+body('Choose a record to see what was saved and where it came from. Experiences are editable only when their original owner can be identified.'));
      return;
    }
    const {row,domain,live} = record;
    const ready = live && state.memory.status==='ok';
    let facts = fact('Source',record.source)+fact('Recorded',date(row.created_at));
    if (domain==='experience' || domain==='event') facts += fact('Owner',row.owner_name || (row.actor ? row.actor : 'Unresolved'));
    if (domain==='fact') facts += fact('Last updated',date(row.updated_at));
    if (row.correction_reason) facts += fact('Correction reason',row.correction_reason);
    const edit = ready && bridge.has(domain==='fact'?'update_memory':'character_correct');
    const forget = ready && bridge.has(domain==='fact'?'delete_memory':'character_forget');
    const pin = ready && domain==='experience' && bridge.has('character_pin') && Object.hasOwn(row,'pinned');
    const actions = (edit?button('edit',drafts.has(record.key)?'Resume correction':'Correct'):'')+
      (pin?button('pin',row.pinned?'Unpin':'Pin'):'')+(forget?button('forget','Forget'):'');
    const links = (row.previous_id ? fact('Previous version',`mem-${row.previous_id}`) : '')+
      (row.superseded_by ? fact('Replaced by',`mem-${row.superseded_by}`) : '');
    const focus = document.activeElement?.dataset.memoryAction;
    if (replace(target,section(record.badge,record.title)+body(record.text)+
      `<dl class="ws-facts">${facts}</dl><div class="ws-actions">${actions}</div>`+
      (!actions?'<p class="fn-meta">This record is read-only here.</p>':'')+
      diagnostics(`<dl class="ws-facts">${fact('Reference',record.key)}${links}${row.char_id?fact('Original owner ID',row.char_id):''}${row.source_event_id?fact('Source event',row.source_event_id):''}${row.tick != null?fact('World tick',row.tick):''}</dl>`)) && focus) {
      target.querySelector(`[data-memory-action="${focus}"]`)?.focus({preventScroll:true});
    }
  }

  function paintKnowledge(record) {
    const target = $('knowledge-library-record');
    if (record?.key.startsWith('import:')) {
      const detail = state.knowledge.detail;
      const result = detail?.version === record.row.version_id ? detail.result : null;
      const source = result?.source;
      const passages = source ? [record.hit,...source.passages].filter(Boolean)
        .filter((passage,index,all)=>all.findIndex(item=>item.start_offset===passage.start_offset)===index) : [];
      replace(target, section('INDEXED SOURCE',record.title)+
        (source ? `<dl class="ws-facts">${fact('Provenance',source.filename || 'Pasted text')}${fact('Imported',date(source.imported_at))}${fact('Version',source.version_id)}</dl>
        <p class="fn-meta">Active in local text search. Independent validation is not recorded.</p>
        <button type="button" data-knowledge-update>Update this source</button>
        <h3>Passage previews</h3><ol class="ws-history">${passages.map(p=>`<li><strong>Lines ${esc(p.first_line)}–${esc(p.last_line)}</strong>${body(p.text)}</li>`).join('')}</ol>
        ${source.truncated?'<p class="fn-meta">Showing a bounded preview.</p>':''}
        ${diagnostics('<dl class="ws-facts">'+fact('Source ID',source.source_id)+fact('Content hash',source.content_hash)+'</dl>'+
          (source.history?.length?`<h4>Retired versions</h4><ol>${source.history.map(item=>`<li>${esc(item.title)} · ${esc(date(item.imported_at))} · ${esc(item.version_id)}</li>`).join('')}</ol>`:'')+
          (source.history_truncated?'<p>Showing the ten most recent retired versions.</p>':''))}` :
          `<p role="status">${esc(result?.reason || 'Loading source details…')}</p>${result?'<button type="button" data-knowledge-retry>Retry source details</button>':''}`));
      return;
    }
    replace(target, record ? section(record.badge,record.title)+
      `<dl class="ws-facts">${fact('Source',record.source)}${fact('Last updated',date(record.updated))}</dl>`+
      `<p class="fn-meta">${esc(record.retrieval)}</p>`+body(record.text)+
      (record.truncated?'<p class="fn-meta">This document preview is incomplete.</p>':'')+
      (record.reference?diagnostics(`<dl class="ws-facts">${fact('Reference',record.reference)}${fact('Source attribution',record.provenance)}${fact('Evidence label',record.epistemic)}</dl>`):'')
      : section('SOURCE DETAILS','Select an entry')+body('Browse imported sources, project notes, saved facts and summaries.'));
  }

  async function loadKnowledge(record, force = false) {
    if (!record?.key.startsWith('import:') || !bridge.has('get_knowledge_source')) return;
    const panel = state.knowledge;
    const {source_id,version_id} = record.row;
    if (!force && (panel.detail?.version===version_id || panel.detailRequest===version_id)) return;
    const generation = ++sourceGeneration;
    panel.detailRequest = version_id;
    let result;
    try { result = await bridge.call('get_knowledge_source',source_id,version_id); }
    catch (_) { result = {ok:false,reason:'The source details could not be read.'}; }
    if (generation!==sourceGeneration) return;
    panel.detailRequest = null;
    if (selection('knowledge')?.row?.version_id!==version_id) return;
    panel.detail = {version:version_id,result:result || {ok:false,reason:'Source details are unavailable.'}};
    paintKnowledge(selection('knowledge'));
  }

  function scheduleKnowledgeSearch() {
    const panel = state.knowledge;
    const query = $('knowledge-library-search').value.trim();
    const generation = ++searchGeneration;
    clearTimeout(knowledgeSearchTimer);
    panel.hits = [];
    panel.searchStatus = '';
    paint('knowledge');
    if (!query || !bridge.has('search_knowledge')) return;
    knowledgeSearchTimer = setTimeout(async () => {
      let result;
      try { result = await bridge.call('search_knowledge',query.slice(0,600)); }
      catch (_) { result = {ok:false}; }
      if (generation!==searchGeneration) return;
      panel.hits = result?.ok ? result.rows : [];
      panel.searchStatus = result?.ok ? (result.rows.length ? '' : 'No indexed passages matched.') : 'The local index could not be searched.';
      paint('knowledge');
      loadKnowledge(selection('knowledge'));
    },180);
  }

  function paintArchive(record) {
    const target = $('conversations-library-record');
    const archive = state.conversations.transcript;
    if (!record) {
      replace(target,section('READ-ONLY ARCHIVE','Select a conversation')+body('Saved records appear here when the archive is available. Open Home for the active conversation.'));
      return;
    }
    let content = '<p role="status">Loading saved messages…</p>';
    if (archive?.key === record.key && archive.version === record.version) {
      const result = archive.result;
      if (result.state !== 'ready') content = '<p role="status">Could not read this transcript. It may be unavailable or damaged.</p><button type="button" id="library-transcript-retry">Try again</button>';
      else {
        const outcomes = {in_progress:'In progress at last save',completed:'Completed',interrupted:'Interrupted',
          cancelled:'Cancelled',failed:'Failed'};
        const history = rows(result).map(message=>`<li><strong>${esc(message.speaker || 'Speaker not recorded')}</strong>
          <span class="fn-meta">${esc(date(message.timestamp))}${message.source?' · '+esc(message.source):''}</span>
          ${message.outcome ? `<p class="fn-meta">${esc(outcomes[message.outcome] || 'Outcome unavailable')}${message.state === 'partial' ? ' · partial response' : ''}</p>` : ''}
          ${body(message.text ?? 'Non-text or unsupported content omitted.')}${message.truncated?'<p class="fn-meta">Message preview truncated.</p>':''}
          ${diagnostics(`<p>Message ${esc(message.position)} · ID ${esc(known(message.id))} · ${esc(message.kind)}</p>`)}</li>`).join('');
        content = (result.oversize?'<p>This file exceeds the transcript preview limit.</p>':
          history?`<ol class="ws-history">${history}</ol>`:'<p>No saved messages in this transcript.</p>')+
          (result.incomplete || result.truncated?'<p class="fn-meta">This transcript preview is incomplete.</p>':'')+
          (result.truncated && record.key.startsWith('light:')?'<p class="fn-meta">Showing the first 200 saved messages.</p>':'');
      }
    }
    if(archive?.key===record.key && archive.result?.state==='ready') {
      const result=archive.result;
      content += '<div class="ws-actions">'+(bridge.has('export_conversation')?'<button type="button" data-archive-action="export">Export displayed transcript</button>':'')+
        (record.key.startsWith('light:') && result.revision && bridge.has('delete_conversation')?'<button type="button" data-archive-action="delete">Delete saved conversation</button>':'')+'</div><p id="archive-action-result" role="status"></p>';
    }
    replace(target,section('SAVED TRANSCRIPT',record.title)+
      `<p class="fn-meta">${esc(record.badge)} · last saved ${esc(record.source)}. Historical transcript; opening it cannot resume work.</p>`+
      diagnostics(`<p>Archive ID ${esc(record.key)}</p>`)+content);
  }

  async function loadTranscript(record, force = false) {
    const panel = state.conversations;
    if (!record || panel.status !== 'ok' || panel.data?.state !== 'ready') return;
    const {key,version} = record;
    if (!force && (panel.transcript?.key === key && panel.transcript.version === version ||
        panel.request?.key === key && panel.request.version === version)) return;
    const generation = ++transcriptGeneration;
    panel.request = {key,version};
    panel.transcript = null;
    paintArchive(record);
    let result;
    try { result = await bridge.call('get_conversation_transcript',key); }
    catch (_) { result = {state:'error'}; }
    if (generation !== transcriptGeneration) return;
    panel.request = null;
    if (panel.selected !== key) return;
    panel.transcript = {key,version,result:result || {state:'error'}};
    paintArchive(selection('conversations'));
  }

  function outcome(message, error = false) {
    const target = $('memory-library-outcome');
    target.textContent = message;
    target.classList.toggle('ws-error',error);
  }
  function closeEditor() {
    $('memory-library-editor').hidden = true;
    editor = null;
    draftContinuation=null;
    if($('memory-continue-draft'))$('memory-continue-draft').hidden=true;
  }
  function cancelForget() {
    $('memory-library-confirm').hidden = true;
    confirmation = null;
  }
  function openEditor(record) {
    draftContinuation=null;
    if($('memory-continue-draft'))$('memory-continue-draft').hidden=true;
    cancelForget();
    const id = record?.key || 'new';
    editor = drafts.get(id) || {
      id, record, key:record?.domain==='fact'?record.row.key:'',
      content:record?.text || '',reason:''
    };
    drafts.set(id,editor);
    const experience = editor.record?.domain==='experience';
    $('memory-library-editor-title').textContent = record ? 'Correct memory' : 'Add shared fact';
    $('memory-library-key-label').hidden = experience;
    $('memory-library-reason-label').hidden = !experience;
    for (const field of ['key','content','reason']) {
      const input = $('memory-library-'+field);
      input.value = editor[field];
      input.required = field==='content' || (field==='key' && !experience) || (field==='reason' && experience);
    }
    $('memory-library-editor').hidden = false;
    $('memory-library-editor').querySelector('[type=submit]').disabled=writing;
    $(experience?'memory-library-content':'memory-library-key').focus();
  }

  async function mutate(method,args,onSuccess) {
    if (writing || !bridge.has(method)) return;
    writing = true;
    outcome('Saving…');
    const submit = $('memory-library-editor').querySelector('[type=submit]');
    submit.disabled = true;
    try {
      const result = await bridge.call(method,...args);
      if (result?.ok !== true) {
        outcome(result?.reason || 'Storage could not confirm this change. Your draft is kept.',true);
        return;
      }
      const followup=onSuccess(result);
      outcome(followup || 'Saved to memory.');
      await bridge.refreshMemory();
    } catch (_) { outcome('Storage could not confirm this change. Your draft is kept.',true); }
    finally { writing = false; submit.disabled = draftContinuation?.draft===editor; }
  }

  function select(name,key) {
    if (name==='memory' && key !== state.memory.selected) {
      closeEditor();
      cancelForget();
      if (!writing) outcome('');
    }
    state[name].selected = key;
    paint(name);
    if (name==='conversations') loadTranscript(selection(name));
    if (name==='knowledge') loadKnowledge(selection(name));
  }

  function wire(name) {
    const list = $(name+'-library-list');
    $(name+'-library-search').oninput = name==='knowledge' ? scheduleKnowledgeSearch : () => paint(name);
    const filter = $(name+'-library-filter');
    if (filter) filter.onchange = () => paint(name);
    $(name+'-library-jump').onclick = () => $(name+'-library-detail').focus();
    list.onclick = event => {
      const key = event.target.closest('button')?.dataset.libraryKey;
      if (key) select(name,key);
    };
    list.onkeydown = event => {
      const buttons = [...list.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement);
      if (index < 0 || !['ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = {Home:0,End:buttons.length-1,ArrowUp:Math.max(0,index-1),ArrowDown:Math.min(buttons.length-1,index+1)}[event.key];
      const key = buttons[next].dataset.libraryKey;
      select(name,key);
      [...list.querySelectorAll('button')].find(node=>node.dataset.libraryKey===key)?.focus();
    };
  }

  function wireKnowledge() {
    const form = $('knowledge-import-form');
    const title = $('knowledge-import-title');
    const text = $('knowledge-import-text');
    const file = $('knowledge-import-file');
    const status = $('knowledge-import-outcome');
    const save = $('knowledge-import-save');
    const target = $('knowledge-import-target');
    const reset = $('knowledge-import-new');
    function mode(record) {
      importTarget = record?.row || null;
      target.textContent = record ? 'Updating '+record.title+'. Its current version stays usable until the new one is indexed.' : '';
      reset.hidden = !record;
    }
    reset.onclick = () => { if (!importing) mode(null); };
    $('knowledge-import').ontoggle = () => {
      if (!$('knowledge-import').open) return;
      save.disabled = importing || !bridge.has('import_knowledge');
      if (!bridge.has('import_knowledge')) status.textContent = 'Local import is unavailable in this view.';
    };
    $('knowledge-library-record').onclick = event => {
      const record = selection('knowledge');
      if (event.target.closest('[data-knowledge-retry]')) loadKnowledge(record,true);
      if (event.target.closest('[data-knowledge-update]') && record && !importing) {
        mode(record);
        if (!title.value) title.value = record.title;
        $('knowledge-import').open = true;
        title.focus();
      }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      if (importing || !bridge.has('import_knowledge')) return;
      const picked = file.files[0];
      const draft = {title:title.value,text:text.value,target:importTarget};
      importing = true;
      for (const input of [title,text,file,save,reset]) input.disabled = true;
      status.classList.remove('ws-error');
      status.textContent = 'Saving and indexing…';
      try {
        let content = draft.text;
        if (picked) {
          if (content.trim()) throw Error('Choose a file or paste text.');
          if (!/\.(txt|md)$/i.test(picked.name) || picked.size > 262144) throw Error('Select a UTF-8 .txt or .md file up to 256 KiB.');
          try { content = new TextDecoder('utf-8',{fatal:true}).decode(await picked.arrayBuffer()); }
          catch (_) { throw Error('This file could not be read as UTF-8.'); }
        }
        let result;
        try {
          result = await bridge.call('import_knowledge',draft.title,content,picked?.name || null,
            draft.target?.source_id || null,draft.target?.version_id || null);
        } catch (_) { throw Error('Could not confirm the import. Your draft was kept.'); }
        if (result?.ok!==true) throw Error(result?.reason || 'The source could not be saved.');
        form.reset();
        mode(null);
        status.textContent = result.unchanged ? 'This text is already indexed.' : 'Source saved and indexed.';
        $('knowledge-library-search').value = '';
        $('knowledge-library-filter').value = '';
        state.knowledge.selected = 'import:'+result.source_id;
        state.knowledge.detail = null;
        scheduleKnowledgeSearch();
        bridge.refreshKnowledge();
      } catch (error) {
        status.classList.add('ws-error');
        status.textContent = error.message || 'The source could not be saved.';
      } finally {
        importing = false;
        for (const input of [title,text,file,save,reset]) input.disabled = false;
      }
    };
  }

  function wireMemory() {
    const continueDraft=document.createElement('button');
    continueDraft.id='memory-continue-draft';continueDraft.type='button';continueDraft.hidden=true;
    continueDraft.textContent='Continue editing saved version';
    $('memory-library-editor').querySelector('.ws-actions').append(continueDraft);
    continueDraft.onclick=async()=>{
      const pending=draftContinuation;
      if(writing||!pending||editor!==pending.draft)return;
      continueDraft.disabled=true;
      try {
        await bridge.refreshMemory();
        if(draftContinuation!==pending||editor!==pending.draft)return;
        const target=recordsFor('memory').find(row=>row.key===pending.ref && row.live && row.domain===pending.domain &&
          (row.domain!=='experience'||row.row.char_id===pending.owner));
        if(state.memory.status!=='ok'||!target){outcome('The confirmed version is no longer available. Your draft is kept; no memory was changed.',true);return;}
        if(drafts.has(target.key)&&drafts.get(target.key)!==editor){outcome('A separate draft already exists for that version. Copy this text before switching.',true);return;}
        if(drafts.get(editor.id)===editor)drafts.delete(editor.id);
        editor.id=target.key;editor.record=target;drafts.set(target.key,editor);
        state.memory.selected=target.key;draftContinuation=null;continueDraft.hidden=true;
        $('memory-library-editor').querySelector('[type=submit]').disabled=false;
        paint('memory');outcome('Newer draft kept. Now editing the confirmed current version; Save to persist it.');
      }catch(_){outcome('Could not confirm the current version. Your draft is kept.',true);}
      finally{continueDraft.disabled=false;}
    };
    $('memory-library-add').onclick = () => openEditor(null);
    $('memory-library-close').onclick = () => {
      closeEditor();
      ($('memory-library-record').querySelector('[data-memory-action="edit"]') || $('memory-library-add')).focus();
    };
    for (const field of ['key','content','reason']) $('memory-library-'+field).oninput = event => {
      if (editor) editor[field] = event.target.value;
    };
    $('memory-library-editor').onsubmit = event => {
      event.preventDefault();
      if (!editor) return;
      if(draftContinuation?.draft===editor){outcome('Continue editing the saved version before saving this newer draft.');return;}
      const {id,record,key,content,reason} = editor;
      const method = !record?'add_memory':record.domain==='fact'?'update_memory':'character_correct';
      const args = !record?[key,content]:record.domain==='fact'?
        [id,key,content,[record.row.key,record.row.content]]:[record.row.char_id,id,content,reason,record.text];
      mutate(method,args,result=>{
        const current=drafts.get(id);
        const unchanged=current && current.key===key && current.content===content && current.reason===reason;
        if(unchanged)drafts.delete(id);
        if (unchanged && editor===current) {
          closeEditor();
          select('memory',result.ref || id);
          $('memory-library-detail').focus({preventScroll:true});
        }
        if(current && !unchanged){
          const domain=record?.domain||'fact';
          const validRef=typeof result.ref==='string' && (domain==='fact'?/^mem-[1-9][0-9]*$/:/^exp-[1-9][0-9]*$/).test(result.ref);
          if(editor===current && validRef){
            draftContinuation={draft:current,ref:result.ref,domain,owner:record?.row.char_id};continueDraft.hidden=false;
            return 'Submitted memory saved. Your newer draft is kept; continue on the confirmed saved version when ready.';
          }
          return 'Submitted memory saved. Your newer draft is kept here; copy it before opening the current record.';
        }
        if(editor && editor!==current)return 'Submitted memory saved. The currently open draft is unchanged.';
      });
    };
    $('memory-library-record').onclick = event => {
      const action = event.target.closest('button')?.dataset.memoryAction;
      const record = selection('memory');
      if (!action || !record?.live || writing || state.memory.status!=='ok') return;
      if (action==='edit') openEditor(record);
      if (action==='pin') mutate('character_pin',[record.row.char_id,record.key,!record.row.pinned],()=>{});
      if (action==='forget') {
        confirmation = record;
        $('memory-library-consequence').textContent = `Forget “${record.title}”? It will leave active retrieval and remain in read-only history.`;
        $('memory-library-confirm').hidden = false;
        $('memory-library-forget').focus();
      }
    };
    $('memory-library-cancel-forget').onclick = () => {
      cancelForget();
      $('memory-library-record').querySelector('[data-memory-action="forget"]')?.focus();
    };
    $('memory-library-forget').onclick = () => {
      if (!confirmation || confirmation.key!==state.memory.selected) return;
      const {key,domain,row} = confirmation;
      mutate(domain==='fact'?'delete_memory':'character_forget',domain==='fact'?[key]:[row.char_id,key],()=>{
        cancelForget();
        if (editor?.id===key) closeEditor();
        $('memory-library-detail').focus({preventScroll:true});
      });
    };
  }

  window.FieldLibrary = {
    init(actions) {
      bridge = actions;
      for (const name of names) wire(name);
      wireMemory();
      wireKnowledge();
      $('conversations-library-home').onclick = actions.openChat;
      let archiveWriting=false;
      $('conversations-library-record').onclick = async event => {
        if(event.target.id==='library-transcript-retry')return loadTranscript(selection('conversations'),true);
        const action=event.target.closest('button')?.dataset.archiveAction;
        const record=selection('conversations'),saved=state.conversations.transcript;
        if(!action||!record||archiveWriting||saved?.key!==record.key||state.conversations.status!=='ok')return;
        if(action==='delete'&&!confirm('Delete this exact saved Light conversation? This cannot be undone. The active conversation is protected; no memories or tools are changed.'))return;
        archiveWriting=true;event.target.disabled=true;
        try {
          const result=(action==='export'?await bridge.call('export_conversation',record.key):await bridge.call('delete_conversation',record.key,saved.result.revision,true));
          if(result?.ok && action==='export') {
            const url=URL.createObjectURL(new Blob([result.content],{type:'application/json'}));
            const link=document.createElement('a');link.href=url;link.download='friday-conversation.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
          }
          const target=$('archive-action-result');if(state.conversations.selected===record.key&&target)target.textContent=result?.message||(result?.ok?'Exported displayed messages.':'Operation not confirmed.');
          if(result?.ok && action==='delete'){state.conversations.transcript=null;await bridge.refreshConversations();}
        }catch(_){const target=$('archive-action-result');if(target)target.textContent='Operation not confirmed. Reload saved state before retrying.';}
        finally{archiveWriting=false;if(event.target.isConnected)event.target.disabled=false;}
      };
    },
    receive(name,data,status) {
      if (!bridge) return;
      state[name].status = status;
      if (status==='ok' && data) state[name].data = data;
      if (name==='knowledge' && data?.imports?.state==='error') state.knowledge.hits = [];
      paint(name);
      if (name==='conversations') loadTranscript(selection(name));
      if (name==='knowledge') loadKnowledge(selection(name));
    }
  };
})();
