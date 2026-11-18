/* Local-only workflow settings; remote strings stay inert. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const caps=['chat','gmail.read','calendar.read','docs.read','gmail.send','calendar.write','docs.write','schedule.write','memory.write'];
  const labels={'chat':'Interactive requests','gmail.read':'Read Gmail','calendar.read':'Read Calendar','docs.read':'Read Docs','gmail.send':'Propose sending email','calendar.write':'Propose Calendar changes','docs.write':'Propose Docs changes','schedule.write':'Propose schedule changes','memory.write':'Propose workflow memory changes'};
  let api,snapshot,dirty=false,pending=false,edit=0,base=null,read=0,proposal=null,decision=null,known=false;
  function report(message,error=false){$('n8n-result').textContent=message;$('n8n-result').dataset.error=String(error);}
  function values(){return {enabled:$('n8n-enabled').checked,notifications:$('n8n-notifications').checked,
    capabilities:caps.filter(c=>$('n8n-cap-'+c).checked)};}
  function paint(){
    const config=snapshot?.config;
    const conflict=dirty&&base!==config?.revision;
    $('n8n-state').textContent=!known?'Configuration unavailable or stale. Saving is blocked.':config?.enabled?'Enabled for selected requests; live operation is not established.':'Disabled. No automatic n8n requests.';
    if(!dirty&&!pending&&config){$('n8n-enabled').checked=config.enabled;$('n8n-notifications').checked=config.notifications;for(const c of caps)$('n8n-cap-'+c).checked=config.capabilities.includes(c);base=config.revision;}
    $('n8n-deployment-state').textContent=config?.deployment_configured?'An address is saved. It is not returned to this page.':'No deployment address saved.';
    $('n8n-key-state').textContent=config?.credential_present?'Credential saved (not a connection or permission test).':'No credential saved.';
    for(const el of $('n8n-form').querySelectorAll('input'))el.disabled=!known;
    $('n8n-save').disabled=!known||pending||!dirty||conflict;
    $('n8n-cancel').disabled=pending||!dirty;
    $('n8n-test').disabled=pending||!known||!config?.credential_present||!config?.deployment_configured||dirty;
    $('n8n-pull').disabled=pending||!known||!config?.enabled||!config?.notifications||dirty;
    $('n8n-remove-key').disabled=pending||!known||!config?.credential_present||dirty;
    if(conflict)report('Configuration changed elsewhere. Cancel to reload; your edits are kept.',true);
    const o=snapshot?.observation;
    $('n8n-observation').textContent=o?'Observation: '+o.state+'. '+(o.at?new Date(o.at*1000).toLocaleString():'No live test recorded.')+' '+(o.error||'')+(o.request_id?' Request '+o.request_id+'; outcome '+o.outcome+'.':''):'No connection observation available.';
    proposal=snapshot?.proposal||null;$('n8n-proposal').hidden=!proposal;
    $('n8n-proposal-text').textContent=proposal?proposal.capability+'\n'+proposal.message:'';
    const box=$('n8n-inbox');box.replaceChildren();
    const events=snapshot?.inbox?.events||[];
    if(!snapshot?.inbox?.ok){box.textContent='Event reader unavailable.';}
    else if(!events.length){box.textContent='No locally stored events. This does not establish that the remote outbox is empty.';}
    for(const event of events){
      const row=document.createElement('p');row.textContent=event.type+': '+(event.action||'notice')+' · '+event.state+(event.text?' · '+event.text:'');
      if(event.type==='decision'){
        const button=document.createElement('button');button.type='button';button.textContent='Review exact action';
        button.disabled=pending||!known||!event.current_session||event.state!=='stored'||Date.now()>=event.expires_at*1000;
        button.onclick=async()=>{const answer=await api.call('preview_n8n_decision',event.id);if(answer?.ok){
          decision=answer;$('n8n-decision').hidden=false;$('n8n-decision-text').textContent=JSON.stringify({action:answer.action,arguments:answer.arguments},null,2);
        }else report('That decision is unavailable, stale or expired.',true);};row.append(' ',button);
      }
      box.append(row);
    }
  }
  async function operation(method,...args){
    if(pending)return;pending=true;read++;paint();report('Waiting for acknowledgement…');
    try {const result=await api.call(method,...args);
      report(result?.ok?(result.message||'Confirmed local operation. Reported outcome: '+(result.outcome||'see receipt')+'.'):(result?.message||'Operation not confirmed: '+(result?.error||'unavailable')+'.'),!result?.ok);
      return result;
    }catch(_){report('Could not confirm the operation. Your inputs are kept; check state before retrying.',true);}
    finally{pending=false;read++;paint();api.refresh();}
  }
  window.FieldN8N={
    generation:()=>read,
    init(config){
      api=config;
      for(const c of caps){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.id='n8n-cap-'+c;label.append(input,' '+labels[c]);$('n8n-capabilities').append(label);}
      $('n8n-form').addEventListener('input',()=>{if(!dirty)base=snapshot?.config?.revision;dirty=true;edit++;report('Unsaved changes. No connection or action started.');paint();});
      $('n8n-cancel').onclick=()=>{dirty=false;edit++;$('n8n-secret').value=$('n8n-deployment').value='';report('Edits cancelled.');paint();};
      $('n8n-form').onsubmit=async event=>{
        event.preventDefault();if(pending||!known||base!==snapshot?.config?.revision)return;
        const changed=values(),key=$('n8n-secret').value,url=$('n8n-deployment').value,version=edit;
        if(key)changed.secret=key;if(url)changed.deployment=url;
        const result=await operation('set_n8n',changed,base);
        if(result?.ok){snapshot.config=result.config;base=result.config.revision;if(edit===version){dirty=false;$('n8n-secret').value=$('n8n-deployment').value='';}paint();}
      };
      $('n8n-remove-key').onclick=async()=>{if(confirm('Remove this saved n8n credential and disable this integration?'))await operation('set_n8n',{remove_secret:true},snapshot.config.revision);};
      $('n8n-test').onclick=()=>operation('test_n8n');
      $('n8n-pull').onclick=()=>operation('pull_n8n');
      for(const [id,choice] of [['n8n-send','send'],['n8n-request-cancel','cancel']])$(id).onclick=()=>proposal&&operation('decide_n8n_request',proposal.id,choice);
      for(const [id,choice] of [['n8n-approve','approve'],['n8n-reject','reject']])$(id).onclick=async()=>{
        if(!decision)return;
        const current=decision;decision=null;$('n8n-decision').hidden=true;
        await operation('decide_n8n_event',current.id,current.digest,choice);
      };
      paint();
    },
    receive(data,state){
      if(pending)return;
      if(state==='ok'&&data?.ok){
        if(snapshot?.config&&data.config?.revision<snapshot.config.revision)return;
        if(snapshot?.session_id===data.session_id&&snapshot?.observation?.at&&data.config?.revision===snapshot.config?.revision&&(!data.observation?.at||data.observation.at<snapshot.observation.at))return;
        snapshot=data;known=true;
      }else known=false;
      paint();
    }
  };
})();
