/* Shipped panel interaction against a bounded fake native bridge, never a real desktop. */
const assert=require('node:assert/strict');
const path=require('node:path');
module.exports=async({context,origin,captures,check,errors})=>{
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(8000);
  try {
    await page.setViewportSize({width:184,height:52});
    await page.addInitScript(()=>{
      const state=window.__panelFixture={expanded:false,pinned:false,calls:[],fail:false,holdRead:false};
      const snapshot=()=>({appearance:state.appearance,unread:2,native:{panel_expanded:state.expanded,panel_pinned:state.pinned},
        activity:{state:'responding',label:'Responding · isolated fixture',caption:'A bounded synthetic response. <img src=x onerror=alert(1)>'}});
      window.pywebview={api:{
        get_snapshot(){const value=snapshot();if(state.holdRead){state.holdRead=false;return new Promise(resolve=>{state.releaseRead=()=>resolve(value);});}return Promise.resolve(value);},
        set_expanded(expanded,pinned){state.calls.push(['expand',expanded,pinned]);if(state.fail)return Promise.resolve({ok:false,message:'Synthetic native refusal'});state.expanded=expanded;state.pinned=expanded&&pinned;return Promise.resolve({ok:true,expanded:state.expanded,pinned:state.pinned});},
        open(tab){state.calls.push(['open',tab]);return Promise.resolve({ok:true});},
        dismiss(){state.calls.push(['dismiss']);return Promise.resolve({ok:true});}
      }};
    });
    await page.goto(origin+'/companion-bar.html');
    await page.getByRole('button',{name:'Open FRIDAY companion panel',exact:true}).waitFor();
    assert.equal(await page.locator('#panel-content').isVisible(),false);
    assert.deepEqual(await page.evaluate(()=>window.__panelFixture.calls),[]);
    await page.screenshot({path:path.join(captures,'companion-anchor.png')});
    await page.getByRole('button',{name:'Open FRIDAY companion panel',exact:true}).focus();
    await page.keyboard.press('Enter');
    await page.locator('#panel-content').waitFor({state:'visible'});
    await page.setViewportSize({width:360,height:480});
    await page.waitForFunction(()=>document.getElementById('activity-label').textContent.includes('isolated fixture'));
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#caption img').count(),0);
    assert.ok(await page.evaluate(()=>document.getElementById('panel-content').getBoundingClientRect().bottom<=innerHeight),'Expanded panel content must fit the native fixture bounds');
    await page.screenshot({path:path.join(captures,'companion-expanded.png')});
    for(const [label,tab] of [['Teaching notes','lessons'],['Notifications','inbox'],['Companion settings','settings'],['Open FRIDAY','home']]){
      if(!await page.locator('#panel-content').isVisible())await page.locator('#anchor').click();
      await page.getByRole('button',{name:new RegExp(label)}).click();
      assert.ok(await page.evaluate(t=>window.__panelFixture.calls.some(c=>c[0]==='open'&&c[1]===t),tab));
      await page.locator('#panel-content').waitFor({state:'hidden'});
    }
    await page.locator('#anchor').click();
    await page.locator('#pin').click();
    await page.waitForFunction(()=>document.getElementById('pin').getAttribute('aria-pressed')==='true');
    await page.evaluate(()=>window.dispatchEvent(new Event('blur')));
    assert.equal(await page.locator('#panel-content').isVisible(),true);
    await page.keyboard.press('Escape');
    await page.locator('#panel-content').waitFor({state:'hidden'});
    assert.equal(await page.evaluate(()=>window.__panelFixture.calls.some(c=>c[0]==='dismiss')),false);
    await page.evaluate(()=>{window.__panelFixture.holdRead=true;});
    await page.waitForFunction(()=>typeof window.__panelFixture.releaseRead==='function');
    await page.locator('#anchor').click();
    await page.locator('#panel-content').waitFor({state:'visible'});
    await page.evaluate(()=>window.__panelFixture.releaseRead());
    assert.equal(await page.locator('#panel-content').isVisible(),true);
    await page.evaluate(()=>window.__panelFixture.fail=true);
    await page.locator('#pin').click();
    await page.getByText('Synthetic native refusal',{exact:true}).waitFor();
    assert.equal(await page.locator('#pin').getAttribute('aria-pressed'),'false');
    await page.evaluate(()=>window.__panelFixture.fail=false);
    await page.evaluate(()=>window.dispatchEvent(new Event('blur')));
    await page.locator('#panel-content').waitFor({state:'hidden'});
    assert.equal(await page.evaluate(()=>window.__panelFixture.calls.some(c=>c[0]==='dismiss')),false);
    await page.evaluate(()=>{window.__panelFixture.appearance='bee';window.__panelFixture.calls=[];});
    await page.getByRole('button',{name:'Open the same FRIDAY window',exact:true}).waitFor();
    await page.setViewportSize({width:184,height:52});
    await page.screenshot({path:path.join(captures,'completion-bee-launcher.png')});
    await page.getByRole('button',{name:'Open the same FRIDAY window',exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>window.__panelFixture.calls),[['open','home']]);
    assert.equal(await page.locator('#panel-content').isVisible(),false);
    check('Optional bee launcher opens the same owned window once without expanding or starting work');
    check('Shipped compact panel expands by keyboard, navigates exact destinations, pins/collapses without disabling and rejects stale/refused receipts');
  } finally {await page.close();}
};
