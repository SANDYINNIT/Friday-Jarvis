/* Actual browser clicks on the installed synthetic world's shipped Town. */
const assert = require('node:assert/strict');
const path = require('node:path');

module.exports = async ({page,rpc,control,text,wait,shot,check,requests}) => {
  const nav = async target => {
    const button = page.locator(`[data-target=${target}]`);
    if (!await button.isVisible()) await page.locator('#nav-toggle').click();
    await button.click();
  };
  const command = async (action, result) => {
    await page.locator(`[data-town-action=${action}]`).click();
    await text('#fn-town-result',result);
  };
  const inspector = page.locator('#lab-inspector');
  const fixture = await control('roster:read');
  const uuid = fixture.ids.primary;
  const revision = fixture.pair.catalog_revision;
  const actor = page.locator('[data-actor="resident:friday"]');
  const name = fixture.pair.roster.characters.find(row=>row.id===uuid).name;
  await nav('view-roster');
  await text('#fn-roster-status',/Catalog revision/);
  await page.locator(`[data-entry="${uuid}"]`).click();
  await page.locator('#fn-find-town').click();
  await text('#fn-town-status',/Catalog revision/);
  await text('#lab-inspector',name);
  await text('#lab-inspector',/Installed resident/);
  assert.equal(await page.locator('#roster-town-handoff').getAttribute('data-character-id'),uuid);
  assert.equal(await page.locator('#roster-town-handoff').getAttribute('data-catalog-revision'),String(revision));
  await inspector.getByRole('button',{name:/View in AI Roster/}).click();
  await text('#fn-profile',name);
  assert.equal(await page.locator('#fn-person-list [aria-pressed=true]').getAttribute('data-entry'),uuid);
  await wait(rev=>document.querySelector('#fn-roster-revision').textContent===`Revision ${rev}`,revision);
  assert.ok(requests.some(item=>item.method==='get_identity_views'&&item.args[0]===uuid&&item.args[1]===revision));
  await page.locator('#fn-find-town').click();
  await text('#lab-inspector',name);
  await actor.waitFor({state:'visible'});
  await shot('12-town-desktop');
  check('Actual installed world, exact UUID/revision Roster–Town–Roster round trip');

  let before = await rpc('get_town_view',[uuid,revision]);
  const walk = new Set(before.walkable.map(cell=>cell.join(',')));
  const geometry = await page.locator('#fn-town-map').evaluate(map=>({
    doors:[...map.querySelectorAll('[data-door]')].map(e=>e.dataset.door),
    walk:[...map.querySelectorAll('[data-walkable]')].map(e=>e.dataset.walkable),
    furniture:[...map.querySelectorAll('[data-furniture]')].map(e=>[e.dataset.furniture,e.dataset.cell,e.dataset.solid]),
  }));
  assert.deepEqual(new Set(geometry.walk),walk);
  const front = before.locations.find(location=>location.id==='front');
  assert.deepEqual(geometry.doors,[`${front.x},${front.y}`]);
  assert.ok(geometry.doors.every(cell=>walk.has(cell)));
  assert.ok(geometry.furniture.every(([,cell,solid])=>walk.has(cell)===(solid==='false')));
  assert.deepEqual(new Set(geometry.furniture.filter(([kind])=>kind==='chair').map(([,cell])=>cell)),
    new Set(before.locations.find(loc=>loc.kind==='kitchen').interaction_cells.map(cell=>cell.join(','))));
  const proposal = before.proposals[0];
  await page.locator(`[data-town-entry="proposal:${proposal.character_id}"]`).click();
  await text('#lab-inspector',/not installed/);
  await page.locator('#fn-town-services button').first().click();
  await text('#lab-inspector',/no independent reasoning/);
  await page.locator('[data-town-entry="resident:research"]').press('Enter');
  await text('#lab-inspector',/Unresolved legacy resident/);
  await actor.click();
  await text('#lab-inspector',name);
  const view = page.locator('#fn-town-map svg');
  const fit = await view.getAttribute('viewBox');
  await page.getByRole('button',{name:'Zoom in'}).click();
  assert.notEqual(await view.getAttribute('viewBox'),fit);
  await page.locator('#fn-town-map').press('ArrowRight');
  await page.getByRole('button',{name:'Follow resident'}).click();
  assert.equal(await page.getByRole('button',{name:'Follow resident'}).getAttribute('aria-pressed'),'true');
  await page.getByRole('button',{name:'Fit',exact:true}).click();
  assert.equal(await view.getAttribute('viewBox'),fit);
  check('Rendered paths, door and furniture match live geometry; proposals, legacy and procedural stations are distinct');

  const movement = [];
  const original = await control('town:state');
  let moved = false;
  let finished = false;
  for (let i=0; i<50; i++) {
    const tick = before.tick+1;
    await page.locator('[data-town-action=step]').click();
    await wait(value=>document.getElementById('fn-town-clock').textContent===`Tick ${value} · Paused`,tick);
    await page.locator('[data-town-action=step]:enabled').waitFor();
    const after = await rpc('get_town_view',[uuid,revision]);
    assert.equal(after.tick,tick);
    const occupied = new Set();
    for (const resident of after.residents) {
      const previous = before.residents.find(row=>row.source_id===resident.source_id);
      const cell = `${resident.x},${resident.y}`;
      assert.ok(walk.has(cell)&&!occupied.has(cell));
      occupied.add(cell);
      assert.ok(Math.abs(resident.x-previous.x)+Math.abs(resident.y-previous.y)<=1);
    }
    const current = after.residents.find(row=>row.source_id==='friday');
    const prior = before.residents.find(row=>row.source_id==='friday');
    assert.equal(await actor.getAttribute('data-x'),String(current.x));
    assert.equal(await actor.getAttribute('data-y'),String(current.y));
    assert.match(await inspector.innerText(),new RegExp(`Cell ${current.x}, ${current.y}`));
    const shown = Number(await inspector.locator('meter[aria-label="energy urgency"]').getAttribute('value'));
    assert.equal(shown,Math.round(current.needs.energy*100)/100);
    const direction = await actor.getAttribute('data-direction');
    if (current.x!==prior.x || current.y!==prior.y) {
      assert.equal(direction,current.x>prior.x?'right':current.x<prior.x?'left':current.y>prior.y?'down':'up');
      if (!moved) await shot('13-town-travel');
      moved = true;
    }
    movement.push({tick,x:current.x,y:current.y,direction,activity:current.activity,energy:shown});
    finished = after.journal.some(event=>event.actor==='friday' && event.kind==='completed' && event.text.includes('rest'));
    before=after;
    if (finished) break;
  }
  assert.ok(moved&&finished);
  assert.ok(movement.at(-1).energy < movement[0].energy);
  assert.ok(movement.some(row=>row.activity==='resting'));
  await text('#fn-town-journal',/FRIDAY completes rest/);
  await shot('14-town-completed-rest');
  const observed = await control('town:state');
  assert.equal(observed.planner_calls,0);
  assert.deepEqual(observed.memories,original.memories);
  check('Manual clicks produce real adjacent travel, rest completion, energy recovery and committed journal entries');

  const keys = async () => page.locator('#fn-town-journal [data-occurrence]').evaluateAll(nodes=>nodes.map(el=>el.dataset.occurrence));
  const saved = await keys();
  assert.deepEqual(saved,before.journal.slice().reverse().map(event=>event.event_key));
  assert.equal(new Set(saved).size,saved.length);
  await command('save',/World saved/);
  await command('load',/Saved world loaded/);
  assert.deepEqual(await keys(),saved);
  await page.reload();
  await nav('view-lab');
  await text('#fn-town-status',/Catalog revision/);
  assert.deepEqual(await keys(),saved);
  const tick = (await rpc('get_town_view')).tick;
  await command('play',/Clock resumed/);
  await wait(previous=>Number(document.getElementById('fn-town-clock').textContent.match(/^Tick (\d+)/)?.[1])>previous,tick);
  await command('pause',/Clock paused/);
  await text('#fn-town-clock',/Paused/);
  await page.setViewportSize({width:760,height:700});
  await page.locator('[data-town-entry="resident:friday"]').click();
  await shot('15-town-compact');
  await inspector.scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(__dirname,'screenshots/16-town-compact-inspector.png')});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  check('Save/load and page reload preserve exact journal occurrences; Play/Pause and compact view work');

  await control('roster:disabled');
  await text('#fn-town-status',/Catalog reader is disabled/);
  await page.locator('[data-town-entry="resident:friday"]').click();
  await text('#lab-inspector',/Legacy simulation resident/);
  assert.equal(await inspector.getByRole('button',{name:/View in AI Roster/}).count(),0);
  await control('town:unloaded');
  await text('#fn-town-status',/No simulation is loaded/);
  assert.ok(await page.locator('[data-town-action=step]').isDisabled());
  assert.ok(!requests.some(item=>['get_lab','get_roster','get_world'].includes(item.method)));
  await shot('17-town-unavailable');
  check('Gate-off loaded legacy and unloaded states are honest, with no lazy world startup calls');
  return movement;
};
