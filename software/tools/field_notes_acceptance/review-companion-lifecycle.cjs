/* Regression from the recorded missing-reveal reproduction; no browser/native runtime. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'source/server/ui/entrance.js'), 'utf8');
function fixture(hidden = false, reduce = false, loading = false) {
  const events = new Map(), timers = new Map();
  let timer = 0;
  const animate = () => ({finished: new Promise(() => {}), cancel() {}});
  const surface = {hidden: true, animate, querySelector: () => ({animate})};
  const document = {hidden, readyState: loading ? 'loading' : 'complete', getElementById: () => surface,
    addEventListener: (name, callback) => events.set(name, callback)};
  const window = {};
  vm.runInNewContext(source, {window, document,
    matchMedia: () => ({matches: reduce, addEventListener() {}}),
    localStorage: {getItem: () => null},
    setTimeout: fn => {timers.set(++timer, fn); return timer;},
    clearTimeout: id => timers.delete(id)}, {filename: 'shipped-entrance.js'});
  return {api: window.FieldEntrance, surface,
    visible() {document.hidden = false; events.get('visibilitychange')?.();},
    loaded() {document.readyState = 'complete'; events.get('DOMContentLoaded')?.();},
    key(key) {events.get('keydown')?.({key});},
    callbacks() {return [...timers.values()];},
    expire() {for (const fn of [...timers.values()]) fn();}};
}
const state = (revision, visible = true, epoch = 'test-window') => ({opening:{epoch,revision,visible,ready:true}});
const hidden = fixture(true);
assert.equal(hidden.api.snapshot().plays, 0);
hidden.api.observe(state(1));
assert.equal(hidden.api.snapshot().plays, 0);
assert.equal(hidden.api.snapshot().pending, true);
hidden.visible();
assert.equal(hidden.api.snapshot().plays, 1);
hidden.api.observe(state(1));
assert.equal(hidden.api.snapshot().plays, 1);
const early = fixture(false, false, true);
assert.equal(early.api.snapshot().plays, 0);
early.api.observe(state(1));
assert.equal(early.api.snapshot().plays, 0);
early.loaded();
assert.equal(early.api.snapshot().plays, 1);
early.expire();
early.api.observe(state(1));
assert.equal(early.api.snapshot().plays, 1);
assert.equal(early.surface.hidden, true);
early.api.observe(state(2));
assert.equal(early.api.snapshot().plays, 2);
assert.equal(early.surface.hidden, false);
const reduced = fixture(false, true);
reduced.api.observe(state(1));
assert.equal(reduced.api.snapshot().plays, 0);
assert.match(reduced.api.snapshot().reason, /Reduced motion/);
const nativeHidden = fixture();
nativeHidden.api.observe(state(1, false));
assert.equal(nativeHidden.api.snapshot().plays, 0);
nativeHidden.api.observe(state(1, true));
assert.equal(nativeHidden.api.snapshot().plays, 1);
const oldTimeout = nativeHidden.callbacks()[0];
nativeHidden.api.observe(state(2));
oldTimeout();
assert.equal(nativeHidden.surface.hidden, false);
nativeHidden.api.observe(state(1));
assert.equal(nativeHidden.api.snapshot().plays, 2);
nativeHidden.api.observe(state(1, true, 'replacement-window'));
assert.equal(nativeHidden.api.snapshot().plays, 3);
nativeHidden.api.observe(0);
nativeHidden.api.observe(state(99, true, 'test-window'));
assert.equal(nativeHidden.api.snapshot().plays, 3);
nativeHidden.key('Escape');
assert.equal(nativeHidden.surface.hidden, true);
nativeHidden.api.observe(state(1, true, 'replacement-window'));
assert.equal(nativeHidden.surface.hidden, true);
const result = {
  date: '2026-11-11', scope: 'Fixed real script with simulated DOM/native observation timing; no native acceptance',
  source_sha256: crypto.createHash('sha256').update(source).digest('hex'),
  repaired_cases: [
    'First native observation while document hidden queues and reveals on visibility',
    'Native observation before DOM ready reveals after load exactly once',
    'Expired initial reveal does not swallow a newer native Open',
    'Native hidden observation waits for actual native visibility without consuming token',
    'Stale timers, revisions and replaced-window epochs cannot dismiss or replace new reveal',
    'Escape dismissal remains final for that token'
  ],
  controls: ['A newer observed revision replays', 'OS reduced motion suppresses intentionally'],
  limitations: ['No native window, browser, device, provider, personal-store or preference access',
    'Earlier companion-recheck-20261111.json preserved as failure evidence']
};
fs.writeFileSync(path.join(__dirname, 'companion-opening-lifecycle-regressions.json'), JSON.stringify(result, null, 2));
console.log('PASS six opening lifecycle regression groups and two behavior controls; no direct play() call.');
