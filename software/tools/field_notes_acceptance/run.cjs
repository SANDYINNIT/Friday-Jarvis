/* Acceptance against shipped assets and the real Python HUD chat bridge. */
const {chromium} = require('../../../.opencode/extensions/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '../..');
const ui = process.env.FRIDAY_ACCEPTANCE_UI ? path.resolve(process.env.FRIDAY_ACCEPTANCE_UI) : path.join(root, 'source/server/ui');
const area = path.join(root, '.venv/field-notes-ui-check');
const origin = 'http://field-notes-check.invalid';
const n8nMode=process.argv.includes('--n8n');
const completionRecordsMode=process.argv.includes('--completion-records');
const rosterMode = process.argv.includes('--roster');
const rosterControlsMode = process.argv.includes('--roster-controls');
const townMode = process.argv.includes('--town');
const townInstallMode = process.argv.includes('--town-install');
const townLifeMode = process.argv.includes('--town-life');
const workspaceMode = process.argv.includes('--workspace');
const workerMode = process.argv.includes('--worker-provenance');
const plannerMode = process.argv.includes('--planner');
const libraryMode = process.argv.includes('--library');
const voiceMode = process.argv.includes('--voice');
const routingMode = process.argv.includes('--routing');
const readinessMode = process.argv.includes('--rollout-readiness');
const capabilityMode = process.argv.includes('--capabilities');
const workflowMode = process.argv.includes('--workflows');
const settingsMode = process.argv.includes('--settings');
const windowMode = process.argv.includes('--window-polish');
const archiveMode = process.argv.includes('--archive');
const knowledgeIngestMode = process.argv.includes('--knowledge-ingest');
const mcpApprovalMode = process.argv.includes('--mcp-approval');
const companionMode = process.argv.includes('--companion');
const entranceMode = process.argv.includes('--entrance');
const mode = n8nMode ? 'n8n' : completionRecordsMode ? 'completion-records' : townInstallMode ? 'town-install' : readinessMode ? 'rollout-readiness' : mcpApprovalMode ? 'mcp-approval' : townLifeMode ? 'town-life' : knowledgeIngestMode ? 'knowledge-ingest' : workerMode ? 'worker-provenance' : archiveMode ? 'archive' : rosterControlsMode ? 'roster-controls' : windowMode ? 'window-polish' : settingsMode ? 'settings' : workflowMode ? 'workflows' : capabilityMode ? 'capabilities' : routingMode ? 'routing' : voiceMode ? 'voice' : libraryMode ? 'library' : plannerMode ? 'planner' : workspaceMode ? 'workspace' : townMode ? 'town' : rosterMode ? 'roster' : 'home';
const stamp = `run-${entranceMode ? 'entrance' : companionMode ? 'companion' : mode}-${Date.now()}-${process.pid}`;
const captures = process.argv.includes('--scratch-captures')
  ? path.join(area,stamp+'-captures') : path.join(__dirname,'screenshots');
fs.mkdirSync(area, {recursive:true});
fs.mkdirSync(captures, {recursive:true});
const methods = ['get_status','get_chat','send_text','stop_turn','get_turn','get_core','set_paused',
  'get_speech','get_events','get_logs','get_sys_stats','get_focus','get_ear_status','get_settings',
  'get_tasks','get_memory','get_knowledge','get_project_notes','get_tools','get_skills','get_conversation_library','get_voice_screen','get_routing_view','get_capability_inventory','get_workflow_view','workflow_request_target',
  'get_startup','set_startup','get_schedule','get_ai_agents','get_roster','get_world','get_lab','get_budgets','get_identity_views','get_character_memory','get_town_view','town_transport'];
if(n8nMode)methods.push('get_n8n','set_n8n','test_n8n','pull_n8n','decide_n8n_request','preview_n8n_decision','decide_n8n_event');
if (workspaceMode || plannerMode) methods.push('assign_task','world_command','review_task','read_artifact');
if (plannerMode) methods.push('decide_pending_request');
if (windowMode || entranceMode) methods.push('get_window_state', 'window_action');
if (rosterControlsMode) methods.push('set_catalog_serving');
if (townLifeMode) methods.push('set_catalog_background_life','set_catalog_life_budget');
if (townInstallMode) methods.push('get_town_install_preview','confirm_town_installation','rollback_town_installation');
if(completionRecordsMode)methods.push('mutate_personal_task','mutate_calendar','get_conversation_transcript','export_conversation','delete_conversation');
if (archiveMode) methods.push('get_conversation_transcript');
if (mcpApprovalMode) methods.push('decide_mcp_proposal');
if (companionMode) methods.push('get_companion','set_companion','acknowledge_companion','hide_companion_surface');
if (readinessMode) methods.push('get_rollout_readiness');
if (knowledgeIngestMode) methods.push('get_knowledge_library','import_knowledge','search_knowledge','get_knowledge_source');
if (libraryMode) methods.push('get_memory_library','get_knowledge_library',
  'get_conversation_transcript','add_memory','update_memory','delete_memory','character_correct','character_pin','character_forget');
let child, context, seq = 0, releaseAck, deferSend = false;
const pending = new Map(), errors = [], external = [], checks = [], requests = [];
const deferred = new Map();
function hold(method) {
  let started, release;
  const received = new Promise(resolve=>{started=resolve;});
  const blocked = new Promise(resolve=>{release=resolve;});
  deferred.set(method,{started,blocked});
  return {received,release};
}
function rpc(method, args = []) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error('Fixture timeout ' + method)); }, 12000);
    pending.set(id, result => { clearTimeout(timer); resolve(result); });
    child.stdin.write(JSON.stringify({id, method, args}) + '\n');
  });
}
const control = (action, data = {}) => rpc('_fixture', [{action, ...data}]);
async function fixture() {
  child = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-u', path.join(__dirname, 'bridge_fixture.py'), stamp, companionMode ? 'companion' : mode],
    {cwd:root, windowsHide:true, stdio:['pipe','pipe','inherit']});
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('Fixture did not start')), knowledgeIngestMode || townLifeMode ? 30000 : 12000);
    child.once('error', reject);
    child.once('exit', code => { clearTimeout(timer); reject(Error('Fixture exit ' + code)); });
    readline.createInterface({input:child.stdout}).on('line', line => {
      if (line === 'READY') { clearTimeout(timer); resolve(); }
      if (line.startsWith('REPLY ')) {
        const [id, result] = JSON.parse(line.slice(6));
        pending.get(id)?.(result); pending.delete(id);
      }
    });
  });
}
async function run() {
  const env = {...process.env, TEMP:area, TMP:area};
  delete env.SSLKEYLOGFILE;
  context = await chromium.launchPersistentContext(path.join(area, stamp + '-profile'), {
    executablePath:path.join(root, '.venv/town-playwright/chromium_headless_shell-1252/chrome-headless-shell-win64/chrome-headless-shell.exe'),
    headless:true, chromiumSandbox:true, offline:true, env,
    ...(settingsMode ? {reducedMotion:'no-preference'} : {}),
    viewport:{width:1440,height:930}, timeout:12000,
  });
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(8000);
  await page.goto('about:blank');
  assert.equal(page.url(), 'about:blank');
  await fixture();
  const fakeWindow = {available:true, integrated:true, maximized:false,opening_revision:1,
    opening:{epoch:'fixture-window',revision:1,ready:true,visible:true}};
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { external.push(url.origin); return route.abort(); }
    if (url.pathname === '/bridge-bootstrap.js') {
      return route.fulfill({contentType:'application/javascript', body:
        'window.pywebview={api:Object.fromEntries(' + JSON.stringify(methods) + '.map(name=>[name,(...args)=>' +
        'fetch("/bridge/"+name,{method:"POST",body:JSON.stringify(args)}).then(r=>r.json()).then(d=>{if(d.fixture_error)throw Error(d.fixture_error);return d;})]))};'});
    }
    if (url.pathname.startsWith('/bridge/')) {
      const method = url.pathname.slice(8);
      if (!methods.includes(method) || request.method() !== 'POST') return route.abort();
      const args = request.postDataJSON();
      requests.push({method,args});
      if ((windowMode || entranceMode) && method === 'get_window_state') return route.fulfill({json:fakeWindow});
      if ((windowMode || entranceMode) && method === 'window_action') {
        if (args[0] === 'toggle_maximize') fakeWindow.maximized = !fakeWindow.maximized;
        return route.fulfill({json:{ok:true}});
      }
      const result = await rpc(method,args);
      const delay = deferred.get(method);
      if (delay) {
        deferred.delete(method);
        delay.started(result);
        await delay.blocked;
      }
      if (method === 'send_text' && deferSend) await new Promise(resolve => { releaseAck = resolve; });
      return route.fulfill({json:result});
    }
    const filename = path.resolve(ui, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!filename.startsWith(ui + path.sep) || !fs.existsSync(filename)) return route.fulfill({status:404,body:''});
    return route.fulfill({path:filename});
  });
  page.on('pageerror', error => errors.push(error.message));
  const text = (selector, pattern) => page.locator(selector).filter({hasText:pattern}).waitFor({state:'visible'});
  const wait = (fn, arg) => page.waitForFunction(fn, arg);
  const shot = async (name, resetScroll = true) => {
    if (resetScroll) await page.locator('#main').evaluate(el => { el.scrollTop = 0; });
    await page.screenshot({path:path.join(captures,name+'.png')});
  };
  const check = name => { checks.push(name); console.log('PASS ' + name); };
  const input = page.getByRole('textbox',{name:'Message FRIDAY'});
  if(process.env.FRIDAY_ACCEPTANCE_UI){
    await context.addInitScript('window.pywebview={api:Object.fromEntries(' + JSON.stringify(methods) +
      '.map(name=>[name,(...args)=>fetch("/bridge/"+name,{method:"POST",body:JSON.stringify(args)}).then(r=>r.json()).then(d=>{if(d.fixture_error)throw Error(d.fixture_error);return d;})]))};');
  }
  await page.goto(origin + '/');
  if(n8nMode){
    await require('./n8n-checks.cjs')({page,rpc,control,text,wait,shot,check,requests,hold});
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'n8n-browser.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('n8n evidence: '+captures);return;
  }
  if(process.argv.includes('--completion-bee')){
    await require('./companion-panel-checks.cjs')({context,origin,captures,check,errors});
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'completion-bee-browser.json'),JSON.stringify({checks,errors,external},null,2));return;
  }
  if(completionRecordsMode){
    try {await require('./completion-records-checks.cjs')({page,rpc,text,shot,check,hold});}
    catch(error){await shot('completion-records-failure',false);fs.writeFileSync(path.join(captures,'completion-records-browser-failure.json'),JSON.stringify({error:String(error),errors,external,checks},null,2));console.error(errors);throw error;}
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'completion-records-browser.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Local record evidence: '+captures);return;
  }
  if (entranceMode) {
    await require('./entrance-checks.cjs')({page,input,shot,check,checks,control,requests,errors,external,captures,fakeWindow});
    return;
  }
  if (companionMode) {
    await require('./companion-checks.cjs')({page,context,input,shot,check,checks,control,rpc,text,hold,requests,errors,external,origin,methods,captures});
    return;
  }
  if (townInstallMode) {
    await input.fill('Keep this draft');
    await page.locator('[data-target=view-lab]').click();
    await page.getByText('Catalog resident installation',{exact:true}).click();
    const preview = page.getByRole('button',{name:'Preview installation'});
    const consent = page.getByRole('checkbox',{name:/reviewed this exact preview/});
    const apply = page.getByRole('button',{name:'Back up and install'});
    await preview.click();
    await text('#town-install-status', /Preview ready/);
    await text('#town-install-evidence', /Proposed NPC residents \(3\)/);
    assert.equal(await apply.isDisabled(),true);
    assert.ok(!requests.some(row=>row.method==='confirm_town_installation'));
    await control('roster:archive');
    await consent.check();
    await apply.click();
    await text('#town-install-status', /preview is stale or installation is blocked/);
    await preview.click();
    await text('#town-install-evidence', /Proposed NPC residents \(2\)/);
    assert.equal(await consent.isChecked(),false);
    await consent.check();
    await apply.click();
    await text('#town-install-status', /Installed 2 NPC residents\. Batch [0-9a-f]{64}, backup [0-9a-f]{32}/);
    await preview.click();
    await text('#town-install-evidence', /Installation receipt/);
    await shot('town-install-receipt',false);
    await page.getByRole('checkbox',{name:/Restore only the installation batch/}).check();
    await page.getByRole('button',{name:'Roll back installation',exact:true}).click();
    await text('#town-install-status', /Installation rolled back/);
    assert.equal(await input.inputValue(),'Keep this draft');
    assert.equal(requests.filter(row=>row.method==='confirm_town_installation').length,2);
    assert.equal(requests.filter(row=>row.method==='rollback_town_installation').length,1);
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    check('Town preview, stale confirmation, backed installation and guarded rollback preserve the draft');
    fs.writeFileSync(path.join(captures,'town-install-browser.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('Town installation evidence: ' + captures);
    return;
  }
  if (mcpApprovalMode) {
    const approval = page.locator('#home-mcp-approval');
    await text('#home-mcp-tool', /studio:read/);
    await text('#home-mcp-args', /<img src=x/);
    assert.equal(await approval.locator('img').count(),0);
    await approval.getByRole('button',{name:'Allow once'}).click();
    await approval.waitFor({state:'hidden'});
    assert.deepEqual(requests.filter(row=>row.method==='decide_mcp_proposal').map(row=>row.args),
      [['fixture-proposal',true]]);
    assert.ok(!requests.some(row=>row.method==='send_text'));
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    check('HUD showed literal tool arguments and sent exactly one approval for the displayed proposal');
    fs.writeFileSync(path.join(captures,'mcp-approval-browser.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('MCP approval evidence: ' + captures);
    return;
  }
  if (townLifeMode) {
    const fixture = await control('roster:read');
    const id = fixture.ids.primary;
    await page.locator('[data-target=view-roster]').click();
    await page.locator('[data-entry="'+id+'"]').click();
    await text('#fn-profile', /Saved: NPC/);
    await page.getByRole('button',{name:'Enable AI · fixture-primary',exact:true}).click();
    await text('#fn-profile', /Saved: AI · Paused/);
    const saved = await rpc('get_identity_views',[id]);
    const expected = saved.roster.characters.find(row=>row.id===id).background_life;
    assert.equal(expected.mode,'ai');
    assert.equal(expected.state,'paused');
    await page.reload();
    await page.locator('[data-target=view-roster]').click();
    await page.locator('[data-entry="'+id+'"]').click();
    await text('#fn-profile', /Saved: AI · Paused/);
    await page.getByRole('button',{name:'Find in Town'}).click();
    await text('#lab-inspector', /Background life: AI · Paused/);
    const town = await rpc('get_town_view',[id]);
    assert.deepEqual(town.residents.find(row=>row.character_id===id).background_life,expected);
    const world = await control('town:state');
    assert.equal(world.planner_calls,0);
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    check('Saved exact AI preference survives reload and agrees across paused Roster/Town without dispatch');
    fs.writeFileSync(path.join(captures,'town-life-browser.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('Town life evidence: ' + captures);
    return;
  }
  if (knowledgeIngestMode) {
    await input.fill('Composer draft stays intact');
    const nav = page.locator('[data-target=view-knowledge]');
    await nav.evaluate(element=>{const group=element.closest('details'); if(group) group.open=true;});
    await nav.click();
    await page.getByText('Add a local source',{exact:true}).click();
    await page.getByLabel('Source title').fill('Fixture field guide');
    await page.getByLabel('Or select a text file').setInputFiles({
      name:'field-guide.md',mimeType:'text/markdown',
      buffer:Buffer.from("Harlequin sightings\n<img src=x onerror='window.ingestExecuted=true'>")
    });
    await page.getByRole('button',{name:'Save and index source'}).click();
    await text('#knowledge-import-outcome',/saved and indexed/);
    await page.getByLabel('Find a source or entry').fill('Harlequin');
    await page.getByRole('button',{name:/Fixture field guide/}).click();
    const details = page.locator('#knowledge-library-record');
    await text('#knowledge-library-record',/field-guide.md/);
    assert.match(await details.innerText(),/Lines 1–2/);
    assert.match(await details.innerText(),/<img src=x/);
    assert.equal(await details.locator('img').count(),0);
    assert.equal(await page.evaluate(()=>window.ingestExecuted),undefined);
    await control('knowledge:fail-write');
    await page.getByRole('button',{name:'Update this source'}).click();
    await page.getByLabel('Paste source text').fill('Unsaved replacement draft');
    await page.getByRole('button',{name:'Save and index source'}).click();
    await text('#knowledge-import-outcome',/Existing versions were kept/);
    assert.equal(await page.getByLabel('Paste source text').inputValue(),'Unsaved replacement draft');
    assert.equal(await input.inputValue(),'Composer draft stays intact');
    assert.equal((await rpc('search_knowledge',['Harlequin'])).rows.length,1);
    assert.equal((await rpc('search_knowledge',['Unsaved replacement'])).rows.length,0);
    assert.ok(!requests.some(row=>['send_text','stop_turn'].includes(row.method)));
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    check('Imported selected Markdown, found the passage, rendered markup inertly and kept the previous version and both drafts after a failed update');
    fs.writeFileSync(path.join(captures,'knowledge-ingest-browser.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('Knowledge ingestion evidence: ' + captures);
    return;
  }
  if (archiveMode) {
    await input.fill('Keep this draft while I read');
    const nav = page.locator('[data-target=view-conversations]');
    await nav.evaluate(element => { const details = element.closest('details'); if (details) details.open = true; });
    await nav.click();
    await page.getByRole('button',{name:/Review the archived fixture/}).click();
    const record = page.locator('#conversations-library-record');
    await text('#conversations-library-record', /Interrupted · partial response/);
    assert.match(await record.innerText(),/Desktop chat/);
    assert.match(await record.innerText(),/Saved fragment <img src=x/);
    assert.equal(await record.locator('img').count(),0);
    assert.equal(await page.evaluate(() => window.archiveRan),undefined);
    assert.equal(await input.inputValue(),'Keep this draft while I read');
    assert.ok(requests.some(row => row.method === 'get_conversation_transcript' &&
      /^light:[a-f0-9]{32}$/.test(row.args[0])));
    assert.ok(!requests.some(row => ['send_text','stop_turn'].includes(row.method)));
    await shot('light-archive-reopened');
    check('Reopened Light archive displays source and interrupted partial text without executing markup, dispatching a turn or changing the draft');
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'light-archive-verification.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('Light archive evidence: ' + captures);
    return;
  }
  if (rosterControlsMode) {
    const fixture = await control('roster:read');
    const id = fixture.ids.primary;
    const revision = fixture.pair.catalog_revision;
    await page.locator('[data-target=view-roster]').click();
    await page.locator('#fn-person-list [data-entry="'+id+'"]').click();
    await text('#fn-profile', /NPC · procedural routines/);
    await text('#fn-profile', /Background AI is unavailable/);
    const profile = page.locator('#fn-profile');
    const change = profile.getByRole('button',{name:'Disable serving for Brain · fixture-primary'});
    const save = hold('set_catalog_serving');
    await change.click();
    const ack = await save.received;
    try {
      assert.equal(ack.ok,true);
      assert.ok(ack.catalog_revision > revision);
      await text('#fn-profile', /Default preference · Allowed by catalog/);
    } finally { save.release(); }
    await text('#fn-profile', /Saved: disabled · Blocked by catalog/);
    await text('#fn-profile', /Brain preference saved/);
    assert.equal(await profile.getByRole('button',{name:'Disable serving for Vision · fixture-primary'}).count(),1);
    const pair = await rpc('get_identity_views',[id]);
    const character = pair.roster.characters.find(row => row.id === id);
    assert.equal(character.bindings.find(b => b.capability === 'brain').saved_serving,false);
    assert.equal(character.bindings.find(b => b.capability === 'vision').saved_serving,null);
    await profile.screenshot({path:path.join(captures,'roster-serving-profile.png')});
    await profile.getByRole('button',{name:'Find in Town →'}).click();
    await text('#lab-inspector', /Brain · fixture-primary · Blocked by catalog/);
    await text('#lab-inspector', /Vision · fixture-primary · Allowed by catalog/);
    await page.locator('#fn-town-roster').click();
    await text('#fn-profile', /Saved: disabled · Blocked by catalog/);
    check('NPC remains visible, exact Brain write waits for acknowledgement, Vision stays independent, and Town agrees');
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'roster-controls-verification.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('Roster controls evidence: ' + captures);
    return;
  }
  if (windowMode) {
    const windowActions = () => requests.filter(row => row.method === 'window_action').map(row => row.args[0]);
    const controls = page.getByRole('group', {name:'Window controls'});
    await controls.waitFor();
    await input.fill('Unsent window polish draft');
    for (const [width,height] of [[1440,930],[760,560],[390,844]]) {
      await page.setViewportSize({width,height});
      assert.ok(await page.locator('.topbar').evaluate(header => {
        const stop = header.querySelector('#btn-stop').getBoundingClientRect();
        const group = header.querySelector('#window-controls').getBoundingClientRect();
        const drag = header.querySelector('.topbar-drag').getBoundingClientRect();
        return stop.right <= group.left && group.right <= innerWidth && drag.width >= 22 &&
          stop.top < group.bottom && group.top < stop.bottom;
      }));
      const header = page.locator('.topbar');
      await header.screenshot({path:path.join(captures,`window-header-${width}.png`)});
    }
    check('One header row fits Stop, window buttons and a dedicated drag area at 1440, 760 and 390 px');
    const minimize = controls.getByRole('button',{name:'Minimize window'});
    await minimize.focus();
    await page.keyboard.press('Tab');
    const maximize = controls.getByRole('button',{name:'Maximize window'});
    assert.ok(await maximize.evaluate(button => button === document.activeElement && button.matches(':focus-visible')));
    await page.keyboard.press('Enter');
    await controls.getByRole('button',{name:'Restore window'}).click();
    await maximize.waitFor();
    await minimize.click();
    await controls.getByRole('button',{name:'Hide FRIDAY to tray'}).click();
    await wait(() => document.querySelector('[data-window-action="close"]').getAttribute('aria-disabled') === 'false');
    assert.deepEqual(windowActions(), ['toggle_maximize','toggle_maximize','minimize','close']);
    assert.equal(await input.inputValue(), 'Unsent window polish draft');
    assert.equal(requests.filter(row => ['send_text','stop_turn'].includes(row.method)).length, 0);
    check('Keyboard and mouse dispatch only window actions, preserve focus and keep the composer draft');
    const drag = await page.locator('.topbar-drag').boundingBox();
    await page.mouse.move(drag.x + 6, drag.y + 12);
    await page.mouse.down();
    await page.mouse.move(drag.x + 17, drag.y + 12);
    await page.mouse.up();
    await wait(() => document.querySelector('[data-window-action="close"]').getAttribute('aria-disabled') === 'false');
    assert.deepEqual(windowActions(), ['toggle_maximize','toggle_maximize','minimize','close','drag']);
    fakeWindow.available = false;
    await controls.waitFor({state:'hidden'});
    check('Only the header drag target sends a move request, and unavailable native controls stay hidden');
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'window-polish-verification.json'),JSON.stringify({checks,errors,external,actions:windowActions()},null,2));
    console.log('Window header evidence: ' + captures);
    return;
  }
  if (settingsMode) {
    await require('./settings-checks.cjs')({page,rpc,control,text,wait,shot,check,requests,hold});
    assert.deepEqual(errors,[]); assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'settings-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Settings acceptance passed.');
    return;
  }
  if (workflowMode) {
    await require('./workflow-checks.cjs')({page,rpc,control,text,wait,shot,check,requests,hold});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'workflow-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Workflows acceptance passed.');
    return;
  }
  if (capabilityMode) {
    await require('./capability-checks.cjs')({page,rpc,control,text,wait,shot,check,requests,hold});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'capability-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Tools & Skills acceptance passed.');
    return;
  }
  if (routingMode) {
    await require('./routing-checks.cjs')({page,rpc,control,text,wait,shot,check,requests});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'routing-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes AI Core and Agents acceptance passed.');
    return;
  }
  if (readinessMode) {
    const previous = await control('routing:gate',{value:false});
    await input.fill('Draft remains here');
    await page.locator('#nav-more summary').click();
    await page.locator('[data-target=view-core]').click();
    await text('#routing-core-status', /Read-only routing snapshot/);
    assert.ok(!requests.some(row=>row.method==='get_rollout_readiness'));
    await page.getByText('Rollout readiness',{exact:true}).click();
    await text('#rollout-status', /Local evidence refreshed/);
    await text('#rollout-evidence', /Identity integration\s*disabled/i);
    await control('routing:integration-gate',{value:true});
    await page.getByRole('button',{name:'Refresh local evidence'}).click();
    await text('#rollout-status', /Local evidence refreshed/);
    await text('#rollout-evidence', /Identity integration\s*enabled/i);
    await page.getByText(/Route enrollment \(/).click();
    await text('#rollout-evidence', /Live eligibility and provider health are unverified/);
    await text('#rollout-evidence ol li', /studio-main\s+Runtime: Configured\s+·\s+verified/i);
    await text('#rollout-evidence', /unmatched/i);
    assert.equal(await input.inputValue(),'Draft remains here');
    assert.equal(requests.filter(row=>row.method==='get_rollout_readiness').length,2);
    const final = await control('routing:gate',{value:false});
    assert.deepEqual(final.calls,[]);
    assert.deepEqual(final.stores,previous.stores);
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    await shot('rollout-readiness-desktop',false);
    check('AI Core disclosure refreshed passive readiness evidence and preserved the composer draft');
    fs.writeFileSync(path.join(captures,'rollout-readiness-browser.json'),
      JSON.stringify({checks,errors,external},null,2));
    console.log('Readiness evidence: ' + captures);
    return;
  }
  if (voiceMode) {
    await require('./voice-checks.cjs')({page,rpc,control,text,wait,shot,check,requests,hold});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'voice-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Voice & Screen acceptance passed.');
    return;
  }
  if (libraryMode) {
    if(process.argv.includes('--review-editor-interleaving')){
      try{await require('./library-race-checks.cjs')({page,rpc,text,hold,shot,check,requests});}
      catch(error){await shot('second-pass-memory-failure',false);fs.writeFileSync(path.join(captures,'second-pass-memory-failure.json'),JSON.stringify({error:String(error),checks,errors,external},null,2));throw error;}
    } else await require('./library-checks.cjs')({page,rpc,control,text,wait,shot,check,requests});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'library-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes library acceptance passed.');
    return;
  }
  if (plannerMode) {
    await require('./planner-checks.cjs')({page,rpc,control,text,wait,shot,check,requests});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'planner-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Tasks and Calendar acceptance passed.');
    return;
  }
  if (workerMode) {
    const ledger = await rpc('get_roster');
    const row = ledger.tasks.find(task=>task.goal==='Review the recorded executor');
    const recorded = row.attempts.at(-1).result_executor;
    const head = await rpc('get_identity_views');
    assert.ok(head.catalog_revision > recorded.catalog_revision);
    await page.locator('[data-target=view-workspace]').click();
    await page.getByRole('button',{name:/Review the recorded executor/}).click();
    await text('#ws-summary',/Assigned worker/);
    await text('#ws-summary',/modal \/ workspace-fixture \/ vendor\/fixture/);
    await page.getByRole('button',{name:'View in AI Roster'}).click();
    const historical = await rpc('get_identity_views',[recorded.character_id,recorded.catalog_revision]);
    const character = historical.roster.characters.find(person=>person.id===recorded.character_id);
    await text('#fn-profile',new RegExp(character.name));
    assert.ok(requests.some(row=>row.method==='get_identity_views' &&
      row.args[0]===recorded.character_id && row.args[1]===recorded.catalog_revision));
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    assert.ok(!requests.some(row=>['send_text','assign_task','world_command','review_task'].includes(row.method)));
    check('Workspace opened the recorded executor UUID at its historical revision after retirement');
    fs.writeFileSync(path.join(captures,'worker-provenance-browser.json'),JSON.stringify({checks,errors,external},null,2));
    console.log('Worker provenance evidence: ' + captures);
    return;
  }
  if (workspaceMode) {
    await require('./workspace-checks.cjs')({page,rpc,control,text,wait,shot,check,requests});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'workspace-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Workspace acceptance passed.');
    return;
  }
  if (townMode) {
    const movement = await require('./town-checks.cjs')({page,rpc,control,text,wait,shot,check,requests});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'town-verification.json'),JSON.stringify({checks,errors,external,requests,movement},null,2));
    console.log('Shipped Field Notes Town acceptance passed.');
    return;
  }
  if (rosterMode) {
    await require('./roster-checks.cjs')({page,context,rpc,control,text,wait,shot,check,requests});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(captures,'roster-verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
    console.log('Shipped Field Notes Roster acceptance passed.');
    return;
  }
  await text('#cc-chat', /Help me review the plan/);
  await text('#home-brain-history', /fixture-previous-model/);
  await text('#home-brain-current', /No current/);
  await text('#home-vision-current', /Not reported/);
  assert.ok(!(await page.locator('#home-brain-current').innerText()).includes('fixture-selected-model'));
  assert.equal(await page.locator('#home-composer-slot #chat-form').count(),1);
  await shot('01-desktop-home');
  check('Shipped Home, real bridge history and separate current/history/unsupported vision');

  await control('mode',{value:'reject'});
  await input.fill('  Keep this rejected draft  ');
  await input.press('Enter');
  await text('#chat-status', /admission refused/);
  assert.equal(await input.inputValue(),'  Keep this rejected draft  ');
  await control('mode',{value:'throw'});
  await page.locator('#chat-send').click();
  await text('#chat-status', /Could not confirm/);
  assert.equal(await input.inputValue(),'  Keep this rejected draft  ');
  await control('mode',{value:'unconfirmed'});
  await input.press('Enter');
  await text('#chat-status', /not accepted/);
  assert.equal(await input.inputValue(),'  Keep this rejected draft  ');
  await shot('02-draft-preserved');
  check('Rejected, thrown and unconfirmed sends preserve the exact draft');

  await control('mode',{value:'accept'});
  deferSend = true;
  const before = requests.filter(r => r.method === 'send_text').length;
  await input.fill('First accepted request');
  await input.press('Enter');
  await wait(() => document.querySelector('#chat-status').textContent === 'Sending…');
  await input.press('Enter');
  await input.fill('A newer draft typed while waiting');
  await wait(() => document.querySelector('#chat-send').disabled);
  while (!releaseAck) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(requests.filter(r => r.method === 'send_text').length,before+1);
  deferSend = false;
  releaseAck(); releaseAck = null;
  await text('#chat-status', /Message accepted/);
  assert.equal(await input.inputValue(),'A newer draft typed while waiting');
  await control('chunk',{text:'A streamed response'});
  await text('#cc-chat', /A streamed response/);
  await control('chunk',{text:' with its next chunk.'});
  await text('#cc-chat', /A streamed response with its next chunk/);
  await text('#home-brain-current', /fixture-thinking-model/);
  assert.equal(await page.locator('#cc-chat .msg.assistant').count(),2);
  assert.ok(await input.evaluate(el => el === document.activeElement));
  await shot('03-streaming');
  await control('cancel_mode',{value:'throw'});
  await page.locator('#btn-stop').click();
  await text('#toast', /cancellation failed.*not confirmed/);
  assert.equal((await rpc('get_turn')).active, true);
  await control('cancel_mode',{value:'accept'});
  await page.locator('#btn-stop').click();
  await text('#toast', /Stop requested/);
  await wait(() => document.querySelector('#btn-stop').disabled);
  await text('#home-brain-current', /No current/);
  const stopped = await control('read');
  assert.equal(stopped.calls.filter(c => c[0] === 'cancel_owned_turn').length,2);
  await control('chunk',{text:'Late obsolete output'});
  assert.ok(!(await rpc('get_chat')).some(m => m.content.includes('Late obsolete output')));
  check('One pending send, newer draft, streams, failed Stop reported truthfully and successful Stop reaches owner');

  await input.fill('Next admitted request');
  await page.locator('#chat-send').click();
  await text('#chat-status', /Message accepted/);
  assert.equal(await input.inputValue(),'');
  await control('chunk',{text:'The next response completes normally.'});
  await control('finish');
  await text('#cc-chat', /The next response completes normally/);
  await text('#home-brain-history', /fixture-thinking-model/);
  await control('chat_error',{value:true});
  await text('#home-chat-update', /earlier messages are kept/);
  await text('#cc-chat', /The next response completes normally/);
  await control('chat_error',{value:false});
  await wait(() => document.querySelector('#home-chat-update').hidden);
  check('Subsequent request, last served and cached history recover after a read failure');

  await page.locator('#qa-mute').click();
  await text('#qa-mute', /Resume voice input/);
  const muted = await control('read');
  assert.equal(muted.calls.filter(c => c[0] === 'set_paused').length,1);
  assert.equal(muted.paused,true);
  await page.locator('#qa-voice').click();
  await page.locator('#view-voice.active').waitFor();
  assert.equal(await page.locator('#global-composer-slot #chat-form').count(),1);
  await input.fill('Draft across navigation');
  const targets = await page.locator('.nav-item').evaluateAll(items => items.map(el => el.dataset.target));
  assert.equal(targets.length,15);
  for (const target of targets) {
    await page.locator('#nav-more').evaluate(el => { el.open = true; });
    await page.locator('[data-target="'+target+'"]').click();
    assert.equal(await page.locator('.pane.active').getAttribute('id'),target);
    assert.equal(await input.inputValue(),'Draft across navigation');
  }
  await page.locator('[data-target=view-command]').click();
  assert.equal(await page.locator('#chat-form').count(),1);
  deferSend = true;
  await input.fill('Pending navigation request');
  await input.press('Enter');
  await text('#chat-status', /Sending/);
  await input.fill('Draft across navigation');
  await page.locator('#qa-voice').click();
  while (!releaseAck) await new Promise(resolve => setTimeout(resolve, 10));
  deferSend = false;
  releaseAck(); releaseAck = null;
  await text('#chat-status', /Message accepted/);
  assert.equal(await page.evaluate(() => document.activeElement.id),'main');
  assert.equal(await input.inputValue(),'Draft across navigation');
  await page.locator('#btn-stop').click();
  await wait(() => document.querySelector('#btn-stop').disabled);
  await page.locator('[data-target=view-command]').click();
  await input.focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id),'chat-send');
  check('Voice input pause acts once; all 15 pages and one composer preserve the draft');

  await page.setViewportSize({width:760,height:560});
  await shot('04-compact-home');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  const composerBox = await page.locator('#chat-input').boundingBox();
  assert.ok(composerBox.y + composerBox.height <= 560);
  await page.locator('#nav-toggle').click();
  const menuGeometry = await page.locator('#app-nav').evaluate(el => ({
    direction:getComputedStyle(el).flexDirection, width:el.clientWidth, scroll:el.scrollWidth,
    brandTop:el.querySelector('.brand').getBoundingClientRect().top,
  }));
  assert.equal(menuGeometry.direction,'column');
  assert.ok(menuGeometry.scroll<=menuGeometry.width&&menuGeometry.brandTop>=0);
  assert.equal(await page.locator('#nav-toggle').getAttribute('aria-expanded'),'true');
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => document.activeElement.id),'nav-toggle');
  await page.locator('#nav-toggle').click();
  await shot('05-compact-navigation');
  await page.locator('[data-target=view-conversations]').evaluate(el => { el.closest('details').open = true; });
  await page.locator('[data-target=view-conversations]').click();
  await page.locator('#view-conversations.active').waitFor();
  assert.equal(await page.locator('#nav-toggle').getAttribute('aria-expanded'),'false');
  assert.equal(await page.evaluate(() => document.activeElement.id),'main');
  await text('#conversations-library-source', /No saved archive is available/);
  assert.equal(await page.locator('#conversations-library-record').getByText('The next response completes normally').count(),0);
  await page.locator('#nav-toggle').click();
  await page.locator('[data-target=view-command]').click();
  await text('#cc-chat', /The next response completes normally/);
  assert.equal(await input.inputValue(),'Draft across navigation');
  check('Compact layout, visible composer, drawer Escape/focus and conversation navigation');

  await control('selected_only');
  await page.reload();
  await text('#home-history-label', /Last selected.*success not reported/);
  await text('#home-brain-current', /No current/);
  await text('#home-vision-current', /Not reported/);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await shot('06-narrow-home');
  await control('history');
  await text('#cc-chat', /Earlier fixture message 34/);
  await page.locator('#cc-chat').evaluate(el => { el.scrollTop = 0; });
  const literal = '<script>window.unexpectedFixtureScript=true</script>';
  await control('append',{text:literal});
  await text('#cc-chat', /unexpectedFixtureScript/);
  assert.ok(await page.locator('#cc-chat').evaluate(el => el.scrollTop < 2));
  assert.equal(await page.evaluate(() => window.unexpectedFixtureScript),undefined);
  check('History reading position and escaped response text survive refresh');
  assert.deepEqual(errors,[]);
  assert.deepEqual(external,[]);
  fs.writeFileSync(path.join(captures,'verification.json'),JSON.stringify({checks,errors,external,requests},null,2));
  console.log('Shipped Field Notes acceptance passed.');
}
run().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  releaseAck?.();
  try { await context?.close(); } finally { child?.stdin.end(); }
});
