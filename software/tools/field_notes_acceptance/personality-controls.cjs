/* Focused shipped Settings controller checks; no browser/native acceptance claim. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const nodes = new Map();
for (const id of ['form','banter','suggestions','save','cancel','refresh','result','saved','expression'])
  nodes.set('personality-'+id, {disabled:false, value:'off', checked:false,
    type:id==='suggestions'?'checkbox':'text', textContent:''});
const get=id=>nodes.get('personality-'+id);
const copy=v=>JSON.parse(JSON.stringify(v));
let state={available:true,expression:'focused',reason:'Helping',preferences:{available:true,revision:'r1',values:{private_banter:'off',proactive_suggestions:false}}};
let resolveSave, failSave=false, calls=[];
const call=async(method,...args)=>{
  calls.push([method,...args]);
  if(method==='get_personality')return copy(state);
  assert.equal(method,'set_personality');
  if(failSave)return {ok:false,message:'Save failed. Your edits are kept.'};
  return new Promise(resolve=>{resolveSave=value=>{
    if(value.ok)state.preferences=value.preferences;
    resolve(copy(value));
  };});
};
const context={window:{},document:{getElementById:id=>nodes.get(id)},console};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../../source/server/ui/personality.js'),'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  context.window.FieldPersonality.init({call});await flush();
  assert.equal(calls.length,1);assert.equal(get('save').disabled,true);
  get('banter').value='spicy';get('banter').onchange();
  get('refresh').onclick();await flush();
  assert.equal(get('banter').value,'spicy'); // read never erases draft
  failSave=true;await get('form').onsubmit({preventDefault(){}});await flush();
  assert.equal(get('banter').value,'spicy');assert.equal(get('save').disabled,false);
  assert.match(get('result').textContent,/failed/);
  failSave=false;const saving=get('form').onsubmit({preventDefault(){}});await flush();
  const before=calls.filter(x=>x[0]==='set_personality').length;
  await get('form').onsubmit({preventDefault(){}});
  assert.equal(calls.filter(x=>x[0]==='set_personality').length,before);
  get('banter').value='light';get('banter').onchange();
  resolveSave({ok:true,preferences:{available:true,revision:'r2',values:{private_banter:'spicy',proactive_suggestions:false}}});
  await saving;await flush();
  assert.equal(get('banter').value,'light');assert.equal(get('save').disabled,false);
  assert.match(get('result').textContent,/newer edits/);
  get('cancel').onclick();await flush();assert.equal(get('banter').value,'spicy');
  state={available:false};await get('refresh').onclick();await flush();
  assert.equal(get('save').disabled,true);assert.equal(get('banter').disabled,true);
  console.log('PASS: passive read/draft preservation; failed save; duplicate submit; late acknowledgement/newer edit; cancel; unavailable state (6 groups).');
})().catch(error=>{console.error(error);process.exitCode=1;});
