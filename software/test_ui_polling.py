"""Execute the shipped HUD polling code against a controlled webview bridge."""

import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
APP_JS = Path(__file__).parent / "source" / "server" / "ui" / "app.js"

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('  // --- Polling ---');
const end = source.indexOf('  // --- Settings load ---', start);
assert(start >= 0 && end > start, 'production polling block must exist');
const events = [];
const timers = [];
const listeners = {};
let statusCalls = 0;
const bridge = { get_status: async () => { statusCalls++; return {sequence: 1}; } };
const context = {
  api: () => bridge, applyStatus: () => {}, setOnline: value => events.push(value),
  setTimeout: (fn, ms) => timers.push({fn, ms}),
  window: {addEventListener: (name, fn) => { listeners[name] = fn; }},
  $: () => ({classList: {contains: () => false}}), pollCount: 3,
  lastChatKey: '', lastLogKey: '', JSON,
};
for (const name of ['renderChat', 'renderConversations', 'renderLogs', 'loadAgents',
    'renderStats', 'renderEar', 'renderFocus', 'loadMemory', 'refreshSchedule',
    'refreshStartupSetting', 'bindStartupToggle']) context[name] = () => {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const settle = () => new Promise(resolve => setImmediate(resolve));
const retry = async () => {
  const index = timers.findIndex(item => item.ms === 500);
  assert(index >= 0, 'poll must schedule its next retry');
  timers.splice(index, 1)[0].fn();
  await settle();
};
(async () => {
  const scenario = process.argv[3];
  if (scenario === 'status-recovery') {
    bridge.get_status = async () => { statusCalls++; throw Error('bridge unavailable'); };
    listeners.pywebviewready();
    await settle();
    assert.deepEqual(events, [false]);
    bridge.get_status = async () => { statusCalls++; return {sequence: 2}; };
    await retry();
    assert.deepEqual(events, [false, true]);
  } else if (scenario === 'missing-bridge') {
    delete bridge.get_status;
    timers.find(item => item.ms === 400).fn();
    await settle();
    assert.deepEqual(events, [false]);
    bridge.get_status = async () => { statusCalls++; return {sequence: 2}; };
    await retry();
    assert.deepEqual(events, [false, true]);
  } else if (scenario.startsWith('aux:')) {
    const method = scenario.slice(4);
    bridge[method] = async () => { throw Error('optional panel unavailable'); };
    listeners.pywebviewready();
    await settle();
    assert.deepEqual(events, [true], 'optional failure must not mark status offline');
    bridge[method] = async () => method === 'get_logs' ? '' : [];
    await retry();
    assert.deepEqual(events, [true, true]);
  } else if (scenario === 'render-failure') {
    context.applyStatus = () => { throw Error('render unavailable'); };
    listeners.pywebviewready();
    await settle();
    assert.deepEqual(events, [true]);
    await retry();
    assert.deepEqual(events, [true, true]);
  } else if (scenario.startsWith('single-chain:')) {
    let release;
    let active = 0;
    let peak = 0;
    bridge.get_status = async () => {
      statusCalls++; active++; peak = Math.max(peak, active);
      await new Promise(resolve => { release = resolve; });
      active--; return {sequence: statusCalls};
    };
    const fallback = timers.find(item => item.ms === 400).fn;
    const ready = listeners.pywebviewready;
    const triggers = scenario.endsWith('timer-first') ? [fallback, ready] : [ready, fallback];
    triggers[0](); triggers[1](); ready();
    assert.equal(statusCalls, 1, 'both startup triggers must share one in-flight poll');
    assert.equal(timers.filter(item => item.ms === 500).length, 0);
    release(); await settle();
    assert.equal(timers.filter(item => item.ms === 500).length, 1);
    timers.find(item => item.ms === 500).fn();
    assert.equal(statusCalls, 2);
    assert.equal(peak, 1, 'status calls must never overlap');
    release(); await settle();
  } else {
    throw Error('unknown test scenario');
  }
  console.log('PASS ' + scenario);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "status-recovery",
        "missing-bridge",
        "render-failure",
        "single-chain:ready-first",
        "single-chain:timer-first",
        *[f"aux:{method}" for method in (
            "get_chat", "get_logs", "get_sys_stats", "get_ear_status", "get_focus"
        )],
    ],
)
def test_production_polling(scenario):
    if not NODE:
        pytest.skip("Node.js is required to execute the production HUD JavaScript")
    result = subprocess.run(
        [NODE, "-", str(APP_JS), scenario],
        input=HARNESS,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {scenario}" in result.stdout
