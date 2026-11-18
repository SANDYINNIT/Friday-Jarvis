/* Deliberate local record editing; integration decisions and Workspace stay separate. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const operationId=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
  function button(label,action){const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=action;return b;}
  window.FieldPersonalPlanner={init(actions){
    if(!$('planner-task-summary') || $('personal-task-editor'))return;
    function editor(kind,parent){
      const form=document.createElement('form');form.id=`personal-${kind}-editor`;form.className='ws-section library-editor';form.hidden=true;
      form.innerHTML=kind==='task'?`<h3>Edit personal task</h3><label>Title<input name="title" maxlength="300" required></label><label>Notes<textarea name="notes" maxlength="2000" rows="3"></textarea></label><label>Priority<select name="priority"><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option></select></label><label>Due date or label<input name="due" maxlength="120"></label>`:
        `<h3>Edit local event</h3><label>Title<input name="title" maxlength="300" required></label><label>Start<input name="start" placeholder="2026-11-14T10:00 or 2026-11-14" required></label><label><input name="all_day" type="checkbox">All day</label><label>All-day end (exclusive)<input name="end" type="date"></label><label>Duration in minutes<input name="duration_minutes" type="number" value="60" min="1" max="10080"></label><p class="fn-meta">Timed values use Europe/Madrid unless you supply an explicit UTC offset. Ambiguous daylight-saving times are refused. This edits only FRIDAY’s local schedule.</p>`;
      const save=button('Save',()=>form.requestSubmit()),cancel=button('Cancel',()=>{if(!pending){form.hidden=true;base=null;}}),status=document.createElement('p');
      status.setAttribute('role','status');status.className='fn-meta';form.append(save,cancel);parent.append(form,status);
      let base=null,pending=false;
      const field=name=>form.elements.namedItem(name);
      function open(row){
        if(pending)return;
        if(kind==='event'&&row?.editable===false){status.textContent='This source or ambiguous saved identity is read-only. No remote calendar was changed.';return;}
        const state=kind==='event'?window.FieldPlanner.calendarState():null;
        if(kind==='event'&&!state){status.textContent='Calendar reader unavailable; editing is disabled.';form.hidden=false;return;}
        base={row:row?{...row}:null,revision:state?.revision,operation:operationId()};
        form.querySelector('h3').textContent=row?`Edit ${kind==='task'?'personal task':'local event'}: ${row.title}`:
          (kind==='task'?'New personal task':'New local event');
        form.reset();field('title').value=row?.title||'';
        if(kind==='task'){field('notes').value=row?.notes||'';field('priority').value=row?.priority||'normal';field('due').value=row?.due_at||row?.due_label||'';}
        else {field('start').value=row?.stored_start||'';field('all_day').checked=row?.kind==='all_day';field('duration_minutes').value=row?.duration_minutes||60;field('end').value=row?.end_date_exclusive||'';}
        status.textContent=row?'Editing the selected saved record; polling will not reset this draft.':'New local record.';
        form.hidden=false;field('title').focus();
      }
      async function write(action,values,confirm=false,requestBase=base){
        if(pending||!requestBase)return;
        if(kind==='event'&&!window.FieldPlanner.calendarState()){status.textContent='Calendar reader unavailable. Your edit is kept.';return;}
        const savedBase=requestBase;pending=true;for(const input of form.elements)input.disabled=true;status.textContent='Saving…';
        try{
          const result=kind==='task'?await actions.call('mutate_personal_task',savedBase.row?'task-'+savedBase.row.id:null,action,values,savedBase.row?.revision,confirm,savedBase.operation):
            await actions.call('mutate_calendar',savedBase.row?'calendar:'+savedBase.row.id:null,action,values,savedBase.revision,confirm);
          status.textContent=result?.message||'Save not confirmed. Your draft is kept.';
          if(result?.ok===true){
            const title=result.task?.title||result.event?.title||savedBase.row?.title||values.title;
            if(title)status.textContent+=` ${kind==='task'?'Task':'Event'}: ${title}.`;
          }
          if(result?.ok===true){if(base===savedBase){base=null;form.hidden=true;}await actions.refresh();}
        }catch(_){status.textContent='Save failed. Your draft is kept; check current state before retrying.';}
        finally{pending=false;for(const input of form.elements)input.disabled=false;}
      }
      form.onsubmit=event=>{event.preventDefault();if(!base)return;const values={title:field('title').value};
        if(kind==='task')Object.assign(values,{notes:field('notes').value,priority:field('priority').value,due:field('due').value});
        else Object.assign(values,{start:field('start').value,all_day:field('all_day').checked,duration_minutes:Number(field('duration_minutes').value),timezone:'Europe/Madrid',end:field('end').value||undefined});
        write(base.row?'update':'create',values);
      };
      return {open,write,notice(message){status.textContent=message;},apply(row,action,confirm=false){
        if(pending)return;
        if(base){status.textContent='Save or cancel the open edit before changing another record.';return;}
        const request={row:{...row},revision:window.FieldPlanner.calendarState()?.revision,operation:operationId()};
        return write(action,{},confirm,request);
      }};
    }
    const tasks=editor('task',$('planner-task-detail')),events=editor('event',$('view-calendar'));
    const tools=document.createElement('div');tools.className='ws-actions';$('planner-task-detail').append(tools);
    const choose=()=>window.FieldPlanner.personalSelection();
    tools.append(button('Edit personal task',()=>{const row=choose();if(row)tasks.open(row);else tasks.notice('Select a personal task. Integration decisions and Workspace reviews use their own actions.');}),
      button('Complete / reopen',()=>{const row=choose();if(row)tasks.apply(row,row.status==='done'?'reopen':'complete');}),
      button('Delete personal task',()=>{const row=choose();if(row&&confirm(`Delete personal task “${row.title}” and its notes? This cannot be undone.`))tasks.apply(row,'delete',true);}));
    const newTask=button('New personal task',()=>tasks.open(null));newTask.id='personal-task-new';$('view-tasks').querySelector('header').append(newTask);
    const calendarTools=document.createElement('div');calendarTools.className='ws-actions';$('view-calendar').querySelector('header').append(calendarTools);
    const selectedEvent=()=>window.FieldPlanner.calendarSelection();
    calendarTools.append(button('New local event',()=>events.open(null)),button('Edit event',()=>{const row=selectedEvent();if(row)events.open(row);else events.notice('Select a saved local event first.');}),
      button('Delete event',()=>{const row=selectedEvent();if(row&&confirm(`Delete local event “${row.title}”? No external calendar is changed.`))events.apply(row,'delete',true);}));
    for(const b of tools.querySelectorAll('button'))b.disabled=!actions.has('mutate_personal_task');
    newTask.disabled=!actions.has('mutate_personal_task');
    for(const b of calendarTools.querySelectorAll('button'))b.disabled=!actions.has('mutate_calendar');
  }};
})();
