"""Production Lab admission, roster, SQLite and shelf with synthetic providers."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
from threading import Barrier, Event

import pytest

from source.server import lab_artifacts, lab_state
from source.server.status_ui import WebviewBridge
from source.server.world.executor import AgentExecutor
from source.server.world.roster import Roster
from source.server.world.store import Store


def answer(goal, **_):
    return {"ok": True, "text": goal, "provider": "synthetic", "model": "test"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "world.db"
    monkeypatch.setattr(lab_state, "db_path", lambda: str(db))
    monkeypatch.setattr(lab_state, "ensure_started", lambda: True)
    lab_state.reset()
    lab_state._execution_queue_reset()
    shelf = lab_artifacts.configure(db, root=str(tmp_path / "files"))
    _, roster = lab_state._load_world()
    runner = AgentExecutor(roster, runner=answer, note=lab_state.note)
    monkeypatch.setattr(lab_state, "_executor", runner)
    try:
        yield roster, shelf, runner, db
    finally:
        thread = lab_state._executor_thread
        if thread is not None:
            thread.join(timeout=4)
        lab_state._execution_queue_reset()
        lab_state.reset()
        lab_artifacts.reset_shared()


def assigned(roster, goal, *, review=False):
    created = roster.create_task(goal, owner="research", approval_required=review)
    assert created["ok"], created
    task_id = created["task"]["id"]
    assert roster.assign(task_id, "research")["ok"]
    return task_id


def claim(roster, task_id, *, retry=False):
    admission = roster.admit(task_id, retry=retry)
    assert admission["ok"], admission
    attempt = admission["attempt"]
    assert roster.mark_running(task_id, attempt=attempt)["ok"]
    return attempt


def test_workspace_bridge_forwards_manual_policy_and_requires_exact_review(env, monkeypatch):
    roster, _, _, _ = env
    monkeypatch.setattr(lab_state, "_live_duty_active", lambda: True)
    bridge = WebviewBridge(None)
    assert not bridge.assign_task("research", "invalid policy", None, "manual")["ok"]
    assert not roster.tasks()
    first = bridge.assign_task("research", "manual policy", None, True)
    second = bridge.assign_task("research", "default policy")
    assert first["task"]["approval_required"] is True
    assert second["task"]["approval_required"] is False
    task_id = first["task"]["id"]
    attempt = first["task"]["attempt"]
    monkeypatch.setattr(lab_state, "_live_duty_active", lambda: False)
    lab_state._drain_execution_queue()
    for _ in range(2):
        thread = lab_state._executor_thread
        if thread is not None:
            thread.join(timeout=4)
    assert roster.task(task_id)["state"] == "awaiting_review"
    assert roster.task(second["task"]["id"])["state"] == "approved"
    for stale in (None, "", second["task"]["attempt"]):
        assert not bridge.review_task(task_id, "approve", stale)["ok"]
        assert roster.task(task_id)["state"] == "awaiting_review"
    assert bridge.review_task(task_id, "approve", attempt)["ok"]
    assert not bridge.review_task(task_id, "reject", attempt)["ok"]


def test_workspace_cancel_does_not_park_a_replacement_attempt(env, monkeypatch):
    roster, _, _, _ = env
    monkeypatch.setattr(lab_state, "_live_duty_active", lambda: True)
    bridge = WebviewBridge(None)
    task_id = assigned(roster, "cancel exact attempt")
    old = bridge.world_command("execute", {"task_id": task_id})["attempt"]
    assert bridge.world_command("cancel_execution", {"task_id": task_id, "attempt": old})["cancelled"]
    new = bridge.world_command("execute", {"task_id": task_id, "retry": True})["attempt"]
    assert new != old
    assert not bridge.world_command("cancel_execution", {"task_id": task_id, "attempt": old})["ok"]
    assert not bridge.world_command("cancel_execution", {"attempt": new})["ok"]
    assert roster.task(task_id)["state"] == "queued"
    assert bridge.world_command("cancel_execution", {"task_id": task_id, "attempt": new})["cancelled"]
    assert roster.task(task_id)["state"] == "paused"


def test_duplicate_and_capacity_during_live_duty_and_inline_admission(env, monkeypatch):
    roster, shelf, runner, _ = env
    duty = Event()
    duty.set()
    entered, release = Event(), Event()
    calls = []

    def blocked(goal, **kwargs):
        calls.append(goal)
        if goal == "first":
            entered.set()
            assert release.wait(4)
        return answer(goal)

    runner._runner = blocked
    monkeypatch.setattr(lab_state, "_live_duty_active", duty.is_set)
    monkeypatch.setenv("FRIDAY_EXECUTION_QUEUE_CAP", "2")
    a, b, c = (assigned(roster, goal) for goal in ("first", "second", "third"))
    try:
        one = lab_state.execute_task(a, background=False)
        two = lab_state.execute_task(b)
        assert one["deferred_for_live_duty"] and two["position"] == 2
        duplicate = lab_state.execute_task(a)
        assert duplicate["dedup"] and duplicate["attempt"] == one["attempt"]
        assert not lab_state.execute_task(a, timeout=11)["ok"]
        assert not lab_state.execute_task(c)["ok"]
        assert roster.task(c)["state"] == "assigned"
        assert lab_state.queue_state()["queued"] == [a, b]
        assert calls == []

        duty.clear()
        lab_state._drain_execution_queue()
        assert entered.wait(4)
        assert not lab_state.execute_task(a, background=False)["ok"]
        third = lab_state.execute_task(c, background=False)
        assert third["ok"] and third["queued"]
        assert calls == ["first"]
    finally:
        release.set()
        for _ in range(3):
            thread = lab_state._executor_thread
            if thread is not None:
                thread.join(timeout=4)
    assert calls == ["first", "second", "third"]
    assert {row["task_id"] for row in shelf.list()} == {a, b, c}


def test_simultaneous_admissions_share_one_persisted_attempt(env, monkeypatch):
    roster, _, _, _ = env
    task_id = assigned(roster, "admit once")
    monkeypatch.setattr(lab_state, "_live_duty_active", lambda: True)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=4)
        return lab_state.execute_task(task_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        outcomes = [future.result(timeout=6) for future in futures]
    assert all(outcome["ok"] for outcome in outcomes)
    assert outcomes[0]["attempt"] == outcomes[1]["attempt"]
    assert sum(bool(outcome.get("dedup")) for outcome in outcomes) == 1
    assert lab_state.queue_state()["queued"] == [task_id]
    assert len(roster.task(task_id)["attempts"]) == 1
    assert lab_state.cancel_execution("queued stop", task_id)["cancelled"]
    assert not lab_state.queue_state()["queued"]


def test_stop_during_provider_then_retry_and_late_callback(env):
    roster, shelf, runner, _ = env
    entered, release = Event(), Event()
    calls = []

    def blocked(goal, **kwargs):
        calls.append(goal)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4)
            return answer("stale provider answer")
        return answer("new provider answer")

    runner._runner = blocked
    task_id = assigned(roster, "cancel me")
    assert not lab_state.cancel_execution()["cancelled"]
    started = lab_state.execute_task(task_id)
    thread = lab_state._executor_thread
    try:
        assert entered.wait(4)
        assert not lab_state.review_task(task_id, "reject")["ok"]
        cancelled = lab_state.cancel_execution("fixture stop", task_id)
        assert cancelled["cancelled"] and cancelled["attempt"] == started["attempt"]
        assert roster.task(task_id)["state"] == "paused"
        assert not lab_state.execute_task(task_id, retry=True)["ok"]
        assert shelf.list(task_id) == []
    finally:
        release.set()
        thread.join(timeout=4)
    retried = lab_state.execute_task(task_id, retry=True, background=False)
    assert retried["ok"] and retried["attempt"] != started["attempt"]
    assert not roster.complete(task_id, "late", attempt=started["attempt"], writer=shelf)["ok"]
    assert not roster.fail(task_id, "late failure", attempt=started["attempt"])["ok"]
    assert [x["state"] for x in roster.task(task_id)["attempts"]] == ["paused", "approved"]
    rows = shelf.list(task_id)
    assert len(rows) == 1 and rows[0]["attempt"] == retried["attempt"]
    assert shelf.read_text(rows[0]["id"]) == "new provider answer"


def test_review_duplicate_completion_and_notification_are_idempotent(env):
    roster, shelf, _, _ = env
    task_id = assigned(roster, "review this", review=True)
    bridge = WebviewBridge(None)
    result = lab_state.execute_task(task_id, background=False)
    attempt, art_id = result["attempt"], result["artifact_id"]
    assert result["state"] == "awaiting_review"
    duplicate = roster.complete(task_id, "different duplicate", attempt=attempt, writer=shelf)
    assert duplicate["dedup"] and duplicate["artifact_id"] == art_id
    lab_state.note("task_completed", {"task": task_id, "attempt": attempt, "artifact_text": "event replay"})
    assert not bridge.review_task(task_id, "approve", "old-attempt")["ok"]
    assert bridge.review_task(task_id, "reject", attempt)["state"] == "rejected"
    assert not bridge.review_task(task_id, "approve", attempt)["ok"]
    assert not roster.complete(task_id, "late after reject", attempt=attempt, writer=shelf)["ok"]
    assert roster.task(task_id)["state"] == "rejected"
    assert len(shelf.list(task_id)) == 1
    assert bridge.read_artifact(art_id)["text"] == "review this"
    assert len([row for row in bridge.get_roster()["artifacts"] if row["task"] == task_id]) == 1


def test_failed_artifact_write_rolls_back_and_retry_preserves_failure(env, monkeypatch):
    roster, shelf, _, db = env
    task_id = assigned(roster, "persist once")

    def deny_rename(*_):
        raise OSError("synthetic disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(lab_artifacts.os, "replace", deny_rename)
        failure = lab_state.execute_task(task_id, background=False)
    assert not failure["ok"] and failure["state"] == "failed"
    assert "artifact write failed" in failure["error"]
    assert shelf.list() == []
    assert list(shelf._root.iterdir()) == []
    result = lab_state.execute_task(task_id, background=False, retry=True)
    assert result["ok"] and result["attempt"] != failure["attempt"]
    disk = Store(db)
    try:
        attempts = Roster(disk).task(task_id)["attempts"]
        assert [item["state"] for item in attempts] == ["failed", "approved"]
        assert "artifact write failed" in attempts[0]["error"]
        assert attempts[-1]["artifact_id"] == result["artifact_id"]
    finally:
        disk.close()


def test_roster_write_failure_does_not_publish_an_artifact_row(env, monkeypatch):
    roster, shelf, runner, _ = env
    task_id = assigned(roster, "metadata rollback")
    original = roster.store._exec_tx
    failed = Event()

    def fail_after_artifact(sql, params=()):
        if not failed.is_set() and sql.startswith("INSERT INTO roster_tasks") and params[3] == "approved":
            failed.set()
            raise OSError("synthetic roster write fault")
        return original(sql, params)

    monkeypatch.setattr(roster.store, "_exec_tx", fail_after_artifact)
    result = lab_state.execute_task(task_id, background=False)
    assert failed.is_set() and not result["ok"] and result["state"] == "failed"
    assert roster.task(task_id)["artifacts"] == []
    assert shelf.list(task_id) == []
    recovered = lab_state.execute_task(task_id, retry=True, background=False)
    assert recovered["ok"] and shelf.read_text(recovered["artifact_id"]) == "metadata rollback"


def test_reopen_parks_interrupted_work_and_fences_old_roster(env):
    roster, shelf, _, db = env
    task_id = assigned(roster, "interrupted")
    old = claim(roster, task_id)
    other = assigned(roster, "waiting")
    assert roster.admit(other)["ok"]
    disk = Store(db)
    try:
        restored = Roster(disk)
        for key in (task_id, other):
            assert restored.task(key)["state"] == "paused"
            assert restored.task(key)["attempts"][-1]["state"] == "interrupted"
        assert not restored.admit(task_id)["ok"]
        new = claim(restored, task_id, retry=True)
        stale = roster.complete(task_id, "old worker", attempt=old, writer=shelf)
        assert not stale["ok"] and shelf.list() == []
        saved = restored.complete(task_id, "new worker", attempt=new, writer=shelf)
        assert saved["ok"]
        assert [row["attempt"] for row in shelf.list(task_id)] == [new]
        assert shelf.read_text(saved["artifact_id"]) == "new worker"
    finally:
        disk.close()


def test_shelf_all_filter_error_mapping_and_readback(env):
    _, shelf, _, _ = env
    a = shelf.record("task-a", "attempt-a", text="first\r\nsecond", ok=False, error="recorded failure")
    b = shelf.record("task-b", "attempt-b", text="other")
    assert a["ok"] and b["ok"]
    bridge = WebviewBridge(None)
    all_rows = bridge.get_artifacts()["artifacts"]
    assert {row["id"] for row in all_rows} == {a["id"], b["id"]}
    [row] = bridge.get_artifacts("task-a")["artifacts"]
    assert row["error"] == "recorded failure" and not row["ok"] and not row["revoked"]
    assert bridge.get_artifacts("task-missing")["artifacts"] == []
    assert bridge.read_artifact(a["id"])["text"] == "first\r\nsecond"
    assert shelf.revoke(a["id"])["ok"]
    assert bridge.get_artifacts("task-a")["artifacts"][0]["file_state"] == "revoked"
    assert not bridge.read_artifact(a["id"])["ok"]
    Path(b["path"]).unlink()  # only a synthetic test file
    assert bridge.get_artifacts("task-b")["artifacts"][0]["file_state"] == "missing"


def test_legacy_error_is_visible_without_rewriting_the_row(env):
    _, shelf, _, _ = env
    recorded = shelf.record("legacy-task", "legacy-attempt", text="historic text")
    with shelf._lock:
        shelf._conn.execute(
            "UPDATE lab_artifacts SET error='0',revoked=? WHERE id=?",
            ("legacy failure", recorded["id"]))
    [row] = WebviewBridge(None).get_artifacts("legacy-task")["artifacts"]
    assert row["error"] == "legacy failure" and not row["revoked"]
    assert shelf.read_text(recorded["id"]) == "historic text"
    persisted = shelf._conn.execute(
        "SELECT revoked FROM lab_artifacts WHERE id=?", (recorded["id"],)).fetchone()
    assert persisted[0] == "legacy failure"


def test_cross_connection_duplicate_does_not_replace_first_artifact(env):
    _, shelf, _, db = env
    second = Store(db)
    try:
        other = lab_artifacts.LabArtifacts(second._conn, root=shelf._root, lock=second._lock)
        barrier = Barrier(2)

        def record(writer, text):
            barrier.wait(timeout=4)
            return writer.record("task", "attempt", text=text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record, writer, text)
                       for writer, text in ((shelf, "a"), (other, "b"))]
            results = [future.result(timeout=6) for future in futures]
        assert all(result["ok"] for result in results)
        assert sum(bool(result.get("dedup")) for result in results) == 1
        [row] = shelf.list()
        winner = "a" if not results[0].get("dedup") else "b"
        assert shelf.read_text(row["id"]) == winner
        assert len(list(shelf._root.glob("*.md"))) == 1
    finally:
        second.close()


def test_hud_renders_real_outcomes_and_attempt_review_controls():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the synthetic HUD check")
    source = Path(__file__).parent / "source" / "server" / "ui" / "app.js"
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
function block(start, stop) {
  const a = source.indexOf(start), b = source.indexOf(stop, a);
  assert(a >= 0 && b > a);
  return source.slice(a, b);
}
const dom = {};
const toasts = [];
const calls = [];
let response;
const no = () => {};
for (const id of ['roster-approvals', 'roster-artifacts', 'roster-task-status',
                  'assign-agent', 'assign-goal', 'assign-budget', 'assign-status']) {
  dom[id] = {innerHTML: '', textContent: '', querySelectorAll: () => [], value: '',
             classList: {add: no, remove: no, toggle: no}};
}
Object.assign(dom['assign-agent'], {value: 'research'});
const context = vm.createContext({
  $: id => dom[id], RENDER: {}, S: {}, LAST: {}, has: () => true,
  esc: String, trun: String, isNum: Number.isFinite,
  renderRosterChain: no, layoutRoster: no, paintRosterInspector: no, renderRosterPeople: no,
  refresh: no, toast: value => toasts.push(value),
  call: async (...args) => {calls.push(args); return response;},
});
vm.runInContext(block('  async function doReview(', '  /* =====') +
  block('  let rosterSel = null;', '  /* =====') +
  block('  async function onAssignSubmit(', '  /* ====='), context);
(async () => {
  context.RENDER.get_roster({
    agents: [{id:'research'}],
    approvals: [{id:'open', state:'awaiting_review', attempt:'run-2'},
                {id:'closed', state:'rejected', attempt:'run-1'}],
    tasks: [{id:'closed', state:'failed', goal:'Historical', error:'disk failed'}],
    artifacts: [{id:'artifact', file_backed:true, file_state:'missing'}],
  }, 'ok');
  assert.match(dom['roster-approvals'].innerHTML, /data-roster-review="open" data-attempt="run-2"/);
  assert.doesNotMatch(dom['roster-approvals'].innerHTML, /data-roster-review="closed"/);
  assert.match(dom['roster-task-status'].innerHTML, /disk failed/);
  assert.match(dom['roster-artifacts'].innerHTML, /missing/);
  assert.doesNotMatch(dom['roster-artifacts'].innerHTML, /data-art-toggle/);
  response = {ok:false, reason:'Review changed'};
  await context.doReview('open', 'approve', 'run-2', {});
  assert.deepEqual(calls.at(-1), ['review_task','open','approve','run-2']);
  assert.equal(toasts.at(-1), 'Review changed');
  dom['assign-goal'].value = 'test';
  response = {ok:true, task:{id:'saved', state:'assigned'},
              execution:{ok:false, reason:'Queue is full.'}};
  await context.onAssignSubmit({preventDefault:no, currentTarget:{querySelector:() => ({})}});
  assert.equal(dom['assign-status'].textContent, 'Task saved. Queue is full.');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-", str(source)], input=harness, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
