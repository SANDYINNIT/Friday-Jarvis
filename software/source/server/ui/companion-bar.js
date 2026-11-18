/* Local companion navigation only. No recording, provider or capture actions. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),api=()=>window.pywebview?.api;
  let expanded=false,pinned=false,pending=false,reading=false,revision=0,bee=false;
  function paintPanel(value){
    expanded=value.expanded===true;pinned=value.pinned===true;
    $('companion-launcher').dataset.expanded=String(expanded);
    $('panel-content').hidden=!expanded;
    $('anchor').setAttribute('aria-expanded',String(expanded));
    $('pin').setAttribute('aria-pressed',String(pinned));
    $('pin').textContent=pinned?'Pinned open':'Keep open';
  }
  async function perform(fn){
    if(pending)return;
    if(!api()){$('panel-result').textContent='Local companion unavailable.';return;}
    pending=true;revision++;
    $('companion-launcher').setAttribute('aria-busy','true');
    document.querySelectorAll('button').forEach(button=>button.disabled=true);
    try {const result=await fn(api());
      if(result?.ok!==true){$('panel-result').textContent=result?.message||'That action could not be confirmed.';return result;}
      $('panel-result').textContent='';
      if(typeof result.expanded==='boolean')paintPanel(result);
      return result;
    }catch(_){$('panel-result').textContent='Local companion unavailable. Your preferences are unchanged.';}
    finally{pending=false;$('companion-launcher').setAttribute('aria-busy','false');document.querySelectorAll('button').forEach(button=>button.disabled=false);}
  }
  function setPanel(next,keep=false){return perform(a=>a.set_expanded(next,keep));}
  $('anchor').onclick=async()=>{if(bee){await perform(a=>a.open('home'));return;}await setPanel(true,false);if(expanded)$('collapse').focus();};
  $('collapse').onclick=async()=>{await setPanel(false,false);if(!expanded)$('anchor').focus();};
  $('pin').onclick=()=>setPanel(true,!pinned);
  $('dismiss').onclick=()=>perform(a=>a.dismiss());
  document.querySelectorAll('[data-open]').forEach(button=>button.onclick=async()=>{
    const result=await perform(a=>a.open(button.dataset.open));
    if(result?.ok===true&&expanded&&!pinned)await setPanel(false,false);
  });
  document.addEventListener('keydown',async event=>{if(event.key==='Escape'&&expanded){event.preventDefault();await setPanel(false,false);if(!expanded)$('anchor').focus();}});
  window.addEventListener('blur',()=>{if(expanded&&!pinned&&!pending)setPanel(false,false);});
  document.addEventListener('pointerdown',event=>{if(expanded&&!pinned&&!$('companion-launcher').contains(event.target))setPanel(false,false);});
  async function refresh(){
    if(reading||pending||!api())return;reading=true;const ticket=revision;
    try{const value=await api().get_snapshot();if(ticket!==revision)return;
      bee=value.appearance==='bee';
      $('anchor').querySelector('img').src=bee?'friday-bee.svg':'friday-mark.svg';
      $('anchor').querySelector('.chevron').hidden=bee;
      $('anchor').setAttribute('aria-label',bee?'Open the same FRIDAY window':'Open FRIDAY companion panel');
      $('anchor').setAttribute('aria-expanded',String(!bee&&expanded));
      const activity=value.activity;
      $('activity-label').textContent=typeof activity?.label==='string'?activity.label:'Activity unavailable';
      $('caption').textContent=typeof activity?.caption==='string'&&activity.caption?activity.caption:'Open FRIDAY for a conversation, or revisit a teaching note.';
      const states=['idle','listening','processing','responding','speaking','guiding','error','unknown'];
      $('companion-launcher').dataset.state=states.includes(activity?.state)?activity.state:'unknown';
      const unread=Number.isSafeInteger(value.unread)&&value.unread>=0?value.unread:null;
      $('inbox').textContent=unread===null?'Unread count unavailable':unread?`${unread} unread ${unread===1?'message':'messages'}`:'No unread messages';
      if(typeof value.native?.panel_expanded==='boolean')paintPanel({expanded:value.native.panel_expanded,pinned:value.native.panel_pinned});
      $('panel-result').textContent='';
    }catch(_){if(ticket===revision){$('activity-label').textContent='Local status unavailable';$('companion-launcher').dataset.state='unknown';$('panel-result').textContent='Retained information may be stale.';}}
    finally{reading=false;}
  }
  window.addEventListener('pywebviewready',refresh);setInterval(refresh,1200);refresh();
})();
