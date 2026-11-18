/* Existing per-field browser preferences and acknowledged registry/session contracts. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fields=[{id:'reduced',key:'friday.ui.reducedMotion',label:'Reduced motion',help:'Limits transitions and existing animated displays. Device accessibility preferences still apply.',fallback:'Device preference'},
    {id:'lowpower',key:'friday.ui.lowPower',label:'Low-power rendering',help:'Limits existing decorative rendering loops. It does not pause Town simulation or assistant work.',fallback:'Off by default'}];
  let actions,startup,startupState='waiting',dirty=false,pending=false,edit=0,readGeneration=0,host,hostState='waiting';
  let voicePending=false;
  const local=new Map();
  const read=key=>{
    try { const raw=localStorage.getItem(key); return {ok:raw===null||raw==='0'||raw==='1',raw}; }
    catch (_) { return {ok:false}; }
  };
  function localPaint(f,force=false) {
    const state=local.get(f.id), input=$('setting-'+f.id), latest=read(f.key);
    if (force || !state.dirty) { state.base=latest; input.value=latest.ok?latest.raw??'default':''; }
    state.readable=latest.ok;
    const stale=state.dirty && latest.raw!==state.base.raw;
    const label=latest.ok?latest.raw===null?f.fallback:latest.raw==='1'?'On':'Off':'Unavailable or invalid';
    const effective=f.id==='reduced'?document.documentElement.dataset.reducedMotion==='on'||matchMedia('(prefers-reduced-motion: reduce)').matches:document.documentElement.dataset.lowPower==='on';
    $('settings-'+f.id+'-saved').textContent=`Saved: ${label} · Effective in this window: ${effective?'on':'off'}${stale?' · Changed elsewhere; Cancel to reload.':''}`;
    input.disabled=!latest.ok;
    $('settings-'+f.id+'-save').disabled=!latest.ok||stale||!state.dirty;
    $('settings-'+f.id+'-cancel').disabled=!state.dirty;
  }
  function paintStartup() {
    const known=startup?.available===true && typeof startup.startup_on==='boolean' && typeof startup.revision==='string';
    const usable=known && startup.editable===true && startupState==='ok';
    const input=$('setting-startup');
    if (!dirty && !pending) input.value=known?startup.startup_on?'on':'off':'';
    input.disabled=!usable;
    const changed=dirty && input.dataset.base!==startup?.revision;
    $('settings-startup-save').disabled=!usable||pending||!dirty||changed||!['on','off'].includes(input.value);
    $('settings-startup-cancel').disabled=pending||!dirty;
    $('startup-state').textContent=(known?`Saved: ${startup.startup_on?'on':'off (no startup entry)'}. Effective at next sign-in; current process unchanged.`:
      startup?.registration==='unrecognized'?'A different startup registration exists. It is preserved and cannot be edited here.':'Saved startup state unavailable.')+
      (startupState!=='ok'&&startup?' Showing stale information; saving is unavailable.':'')+(changed?' Changed elsewhere. Cancel to reload before saving.':'');
  }
  function report(id,message,error=false) { $(id).textContent=message; $(id).dataset.error=String(error); }
  window.FieldSettings={
    generation:()=>readGeneration,
    init(config) {
      actions=config;
      $('settings-preference-forms').innerHTML=fields.map(f=>`<form id="settings-${f.id}-form"><label for="setting-${f.id}">${f.label}</label><select id="setting-${f.id}"><option value="default">${f.fallback}</option><option value="1">On</option><option value="0">Off</option></select><p class="fn-meta">${f.help}</p><p class="fn-meta" id="settings-${f.id}-saved"></p><div class="ws-actions"><button type="submit" class="ws-primary" id="settings-${f.id}-save">Save ${f.label.toLowerCase()}</button><button type="button" id="settings-${f.id}-cancel">Cancel</button></div><p id="settings-${f.id}-result" role="status"></p></form>`).join('');
      for(const f of fields) {
        local.set(f.id,{dirty:false,base:read(f.key)});
        const state=local.get(f.id),input=$('setting-'+f.id);
        input.onchange=()=>{state.dirty=true; report(`settings-${f.id}-result`,'Unsaved change.');localPaint(f);};
        $(`settings-${f.id}-cancel`).onclick=()=>{state.dirty=false;report(`settings-${f.id}-result`,'Change cancelled.');localPaint(f,true);};
        $(`settings-${f.id}-form`).onsubmit=event=>{
          event.preventDefault();const now=read(f.key), value=input.value;
          if (!state.dirty || !now.ok || !state.base.ok || now.raw!==state.base.raw || !['default','0','1'].includes(value)) {
            report(`settings-${f.id}-result`,'Could not save. Check the choice or reload the saved preference.',true);return;
          }
          try {
            if(value==='default') localStorage.removeItem(f.key); else localStorage.setItem(f.key,value);
            const confirmed=read(f.key);
            if(!confirmed.ok || confirmed.raw!==(value==='default'?null:value)) throw Error('unconfirmed');
            state.dirty=false;actions.apply();localPaint(f,true);report(`settings-${f.id}-result`,'Saved in this browser profile. Applied to this window.');
          } catch (_) { report(`settings-${f.id}-result`,'Could not save in this browser. Your edit is kept.',true); }
        };
        localPaint(f,true);
      }
      window.addEventListener('storage',event=>{if(fields.some(f=>f.key===event.key)||event.key===null){actions.apply();for(const f of fields)localPaint(f);}});
      $('setting-startup').onchange=()=>{if(!dirty)$('setting-startup').dataset.base=startup?.revision;dirty=true;edit++;report('settings-startup-result','Unsaved change.');paintStartup();};
      $('settings-startup-cancel').onclick=()=>{dirty=false;edit++;report('settings-startup-result','Change cancelled.');paintStartup();};
      $('settings-startup-form').onsubmit=async event=>{
        event.preventDefault();const input=$('setting-startup'),choice=input.value;
        if(pending||!dirty||startupState!=='ok'||!startup?.editable||input.dataset.base!==startup.revision||!['on','off'].includes(choice))return;
        const expected=edit;pending=true;readGeneration++;paintStartup();report('settings-startup-result','Saving startup…');
        try {
          const result=await actions.call('set_startup',choice==='on',startup.revision);
          if(result?.accepted===true&&result.enabled===(choice==='on')&&typeof result.revision==='string') {
            startup={...startup,startup_on:result.enabled,revision:result.revision};
            input.dataset.base=result.revision;
            if(expected===edit)dirty=false;
            report('settings-startup-result',expected===edit?'Saved. Applies at your next Windows sign-in.':'Earlier choice saved. Your newer edit is not saved.');
          } else {
            startupState='error';report('settings-startup-result',result?.conflict?'Startup changed elsewhere. Your edit is kept; refresh before retrying.':'Could not confirm startup. Your edit is kept; refresh before retrying.',true);
          }
        }catch(_){startupState='error';report('settings-startup-result','Could not save startup. Your edit is kept.',true);}
        pending=false;readGeneration++;paintStartup();actions.refresh();
      };
      $('settings-refresh').onclick=()=>{readGeneration++;actions.apply();for(const f of fields)localPaint(f);actions.refresh();};
      $('settings-status').textContent='Changes are saved separately. Refresh only rereads existing state.';
      paintStartup();
    },
    startup(payload,status) { if(pending)return;if(status==='ok'&&payload?.available===true){startup=payload;startupState='ok';}else startupState='error';paintStartup(); },
    disconnected() { startupState='error';paintStartup();this.host(null,'error'); },
    host(payload,status) {
      if(status==='ok'&&payload&&Object.keys(payload).length){host=payload;hostState='ok';}else hostState='error';
      const labels={'version':'Application version','status UI':'Host UI','current state':'Reported activity','interpreter model setting':'Interpreter model setting (not serving duty)','semantic embeddings':'Semantic retrieval setting'};
      const html=(hostState!=='ok'?'<p class="fn-meta">Runtime information unavailable. Retained information is stale.</p>':'')+(host?'<dl>'+Object.entries(labels).filter(([k])=>Object.hasOwn(host,k)).map(([k,label])=>`<dt>${esc(label)}</dt><dd>${esc(host[k])}</dd>`).join('')+'</dl>':'');
      if($('settings-dump').innerHTML!==html)$('settings-dump').innerHTML=html;
    },
    voice(paused,known,pendingSetting) { voicePending=pendingSetting;$('settings-voice-state').textContent=known?`${paused?'Paused':'Allowed'} for this session${voicePending?' · queuing setting':''}`:'Session voice state unavailable'; },
    voiceReceipt(receipt) {
      const labels = {queued:'Queued for the audio client', sent:'Sent · client application unconfirmed',
        applied:'Audio client applied the software input gate', failed:'Voice setting delivery or application failed',
        disconnected:'Audio client disconnected · application unknown', unconfirmed:'Audio client application unconfirmed'};
      const label = labels[receipt?.state] || 'Audio client status unavailable';
      $('settings-voice-receipt').textContent = receipt?.startup_check ? label + ' · devices disabled for startup check' : label;
    },
    voiceResult(result) { report('settings-voice-result',result.message,!result.accepted); },
    presentation() {for(const f of fields)if(local.has(f.id))localPaint(f);}
  };
})();
