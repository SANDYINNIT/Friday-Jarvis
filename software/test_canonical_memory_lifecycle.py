"""Canonical memory lifecycle and domain dispatch through synthetic stores."""

import sqlite3

import pytest

from source.server import lab_memory
from source.server.memory import MemoryStore
from source.server.semantic_memory import SemanticMemoryStore
from source.server.status_ui import StatusBus, WebviewBridge


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setenv("FRIDAY_MEMORY_DB", str(db))
    lab = lab_memory.LabMemory(db, tmp_path / "world.db")
    monkeypatch.setattr(lab_memory, "shared", lambda: lab)
    return db, lab, WebviewBridge(StatusBus(), None)


def test_correction_chain_filters_recall_search_relations_and_embeddings(memory_env, monkeypatch):
    db, lab, bridge = memory_env
    base = MemoryStore(db)
    semantic = SemanticMemoryStore(db)
    monkeypatch.setattr(semantic, "_embedding", lambda _: [1.0, 0.0])
    first = lab.remember("orchard", "apple cider", pinned=True,
                         source="user", epistemic="told", source_event_id="example")
    first_id = first["id"]
    semantic._store_embedding(first_id, first["content"], [1.0, 0.0])
    semantic.link_facts("orchard", "produces", "cider", source_memory_id=first_id)
    lab.create_summary(first_id, "orchard cider summary")
    second = lab.correct(first_id, "pear juice", "confirmed produce")["record"]
    third = lab.correct(second["id"], "plum jam", "confirmed again")["record"]

    assert [r["id"] for r in base.recall("orchard")] == [third["id"]]
    assert [r["id"] for r in base.list_active()] == [third["id"]]
    assert base.recall("cider") == []
    assert lab.index_search("cider", domain="assistant") == []
    assert [r["id"] for r in lab.index_search("plum", domain="assistant")] == [third["id"]]
    assert [r["id"] for r in semantic.recall_semantic("orchard")] == [third["id"]]
    assert semantic.related_facts("orchard") == []
    assert lab.list_summaries(first_id) == []
    assert lab.list_summaries(first_id, include_stale=True)[0]["stale"] == 1
    assert lab.pin(first_id)["ok"] is False
    assert [lab.why_remember(i)["version"] for i in (first_id, second["id"], third["id"])] == [1, 2, 3]
    assert third["source_event_id"] == "example" and third["pinned"] == 1

    edited = bridge.update_memory(f"mem-{third['id']}", "orchard", "peach preserves")
    assert edited["ok"] is True and edited["id"] != third["id"]
    assert bridge.update_memory(f"mem-{third['id']}", "orchard", "late edit")["ok"] is False
    assert MemoryStore(db).recall("orchard")[0]["content"] == "peach preserves"
    with sqlite3.connect(db) as conn:
        history = conn.execute(
            "SELECT content, superseded_by FROM memories ORDER BY id"
        ).fetchall()
    assert history == [("apple cider", second["id"]), ("pear juice", third["id"]),
                       ("plum jam", edited["id"]), ("peach preserves", None)]


def test_deletion_blocks_reappearance_and_reports_outcomes(memory_env):
    db, lab, bridge = memory_env
    item = lab.remember("orchard", "plum jam")
    lab.create_summary(item["id"], "plum summary")
    ref = f"mem-{item['id']}"
    assert bridge.delete_memory(ref) == {"ok": True, "deleted": 1}
    assert bridge.delete_memory(ref)["ok"] is False
    assert bridge.add_memory("orchard", "plum jam")["ok"] is False
    assert lab.pin(item["id"])["ok"] is False
    reopened = lab_memory.LabMemory(db)
    assert MemoryStore(db).list_active() == []
    assert reopened.index_search("plum", domain="assistant") == []
    assert reopened.list_summaries(item["id"]) == []
    assert reopened.list_summaries(item["id"], include_stale=True)[0]["stale"] == 1


def test_character_bridge_dispatches_by_domain_and_owner(memory_env):
    db, lab, bridge = memory_env
    canonical = lab.remember("shared", "canonical sample")
    exp = lab.record_experience("char-a", 1, "observation", "original sample")
    assert canonical["id"] == exp["id"]
    ref = f"exp-{exp['id']}"
    with sqlite3.connect(db.with_name("world.db")) as world:
        world.execute("CREATE TABLE events (id INTEGER, tick INTEGER, actor TEXT, kind TEXT, text TEXT)")
        world.execute("INSERT INTO events VALUES (1, 1, 'char-a', 'note', 'event sample')")
    snapshot = bridge.get_character_memory("char-a")
    assert snapshot["experiences"][0]["ref"] == ref
    assert snapshot["scrapbook"]["events"][0]["ref"] == "evt-1"
    assert bridge.get_character_memory("char-b")["experiences"] == []
    for invalid in (canonical["id"], "mem-1", "evt-1", "exp-0", "exp-²",
                    "exp-9223372036854775808", "exp-" + "9" * 5000):
        assert bridge.character_correct("char-a", invalid, "wrong", "example")["ok"] is False
        assert bridge.character_forget("char-a", invalid)["ok"] is False
        assert bridge.character_pin("char-a", invalid, True)["ok"] is False
    assert bridge.delete_memory(ref)["ok"] is False
    assert bridge.update_memory("evt-1", "shared", "wrong")["ok"] is False
    assert bridge.character_correct("char-b", ref, "wrong", "example")["ok"] is False
    assert bridge.character_forget("char-b", ref)["ok"] is False
    assert bridge.character_pin("char-b", ref, True)["ok"] is False
    assert bridge.character_pin("char-a", ref, "false")["ok"] is False

    assert bridge.character_correct("char-a", ref, "corrected sample", "example")["ok"] is True
    assert bridge.character_pin("char-a", ref, True)["ok"] is True
    saved = lab_memory.LabMemory(db).scrapbook("char-a")["experiences"][0]
    assert saved["pinned"] == 1 and saved["text"].startswith("corrected sample")
    assert bridge.character_forget("char-a", ref)["ok"] is True
    assert bridge.character_forget("char-a", ref)["ok"] is False
    assert bridge.character_pin("char-a", ref, False)["ok"] is False
    assert lab_memory.LabMemory(db).scrapbook("char-a")["experiences"] == []
    assert MemoryStore(db).list_active()[0]["content"] == "canonical sample"



@pytest.mark.parametrize("prefer_fts5", [True, False])
def test_active_reads_filter_deleted_expired_and_revoked_after_reopen(tmp_path, prefer_fts5):
    db = tmp_path / "memory.db"
    lab = lab_memory.LabMemory(db, prefer_fts5=prefer_fts5)
    base = MemoryStore(db)
    expired = lab.remember("old", "marker expired", expires_at="2000-01-01T00:00:00+00:00")
    deleted = lab.remember("remove", "marker deleted")
    current = lab.remember("keep", "marker current")
    lab.create_summary(deleted["id"], "marker deleted summary")
    assert not lab.create_summary(expired["id"], "expired summary")["ok"]
    assert not lab.regenerate_summary(expired["id"])["ok"]
    assert base.forget(memory_id=deleted["id"]) == 1
    lab.grant_artifact("active-artifact", label="marker current artifact")
    lab.grant_artifact("revoked-artifact", label="marker revoked artifact")
    assert lab.revoke_artifact("revoked-artifact")["ok"]

    reopened = lab_memory.LabMemory(db, prefer_fts5=prefer_fts5)
    assert [r["id"] for r in reopened.index_search("marker")] == [current["id"]]
    assert [r["id"] for r in reopened.recall("marker")] == [current["id"]]
    assert [r["id"] for r in MemoryStore(db).list_active()] == [current["id"]]
    assert reopened.list_summaries(deleted["id"]) == []
    assert [r["artifact_id"] for r in reopened.recall("marker", domain="artifact")] == ["active-artifact"]
    assert [r["artifact_id"] for r in reopened.list_artifacts()] == ["active-artifact"]
    assert len(reopened.list_artifacts(include_revoked=True)) == 2


def test_key_change_and_legacy_upserts_do_not_revive_history(memory_env):
    db, lab, bridge = memory_env
    base = MemoryStore(db)
    original = lab.remember("display", "original old", topic="settings")
    current = bridge.update_memory(original["id"], "screen", "current new")
    assert current["ok"] and current["ref"] == f"mem-{current['id']}"
    assert base.remember("display", "original old", topic="settings", source="automatic") is None
    assert base.remember("screen", "original old", topic="settings", source="automatic") is None
    assert base.recall("original") == []
    assert base.recall("current")[0]["id"] == current["id"]
    assert base.forget(memory_id=original["id"]) == 0
    assert lab.unforget(original["id"])["ok"] is False
    assert lab.pin(current["id"])["ok"] is True
    base.remember("newer", "separate", topic="settings")
    base.remember("other", "another", topic="settings")
    assert base.prune_topic("settings", keep=1) == 1
    assert lab.unpin(current["id"])["ok"] is True
    assert base.forget(memory_id=current["id"]) == 1
    assert base.recall("current") == []
    assert lab.unforget(current["id"])["ok"] is True
    assert MemoryStore(db).recall("current")[0]["id"] == current["id"]


def test_two_bridge_edits_share_one_current_version_and_failed_edit_rolls_back(memory_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db, lab, bridge = memory_env
    original = lab.remember("subject", "original")
    ref = f"mem-{original['id']}"
    lab.create_summary(original["id"], "existing summary")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER test_reject_correction BEFORE INSERT ON memory_corrections "
            "BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END"
        )
    assert bridge.update_memory(ref, "subject", "failed write")["ok"] is False
    assert bridge.get_memory()[0]["ref"] == ref
    assert lab.list_summaries(original["id"])[0]["summary"] == "existing summary"
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER test_reject_correction")

    barrier = Barrier(2)

    def edit(value):
        barrier.wait(timeout=5)
        return bridge.update_memory(ref, "subject", value)

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(edit, ("first candidate", "second candidate"), timeout=15))
    winners = [outcome for outcome in outcomes if outcome["ok"]]
    assert len(winners) == 1
    assert [row["id"] for row in MemoryStore(db).list_active()] == [winners[0]["id"]]
    assert lab.list_summaries(original["id"]) == []



def test_hud_waits_for_explicit_success_and_displays_each_experience_once():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the synthetic HUD check")
    app = Path(__file__).parent / "source" / "server" / "ui" / "app.js"
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
function block(start, stop) {
  const a = source.indexOf(start), b = source.indexOf(stop, a);
  assert(a >= 0 && b > a);
  return source.slice(a, b);
}
const dom = {};
const toasts = [];
const calls = [];
let result, refreshes = 0;
const no = () => {};
for (const id of ['memory-new-key', 'memory-new-content', 'memory-add-status', 'lab-char-mem-l']) {
  dom[id] = {value: '', textContent: '', innerHTML: '', classList: {add:no, remove:no}};
}
const context = vm.createContext({
  $: id => dom[id], document:{getElementById:id => dom[id]},
  S:{}, RENDER:{}, LAST:{}, window:{_labMemGen:0}, esc:String,
  has: () => true, isMissing: () => false, toast: text => toasts.push(text),
  call: async (...args) => {calls.push(args); return result;},
  refresh: () => {refreshes++;},
});
vm.runInContext(block('  RENDER.get_memory =', '  /* ====') +
  block('  async function loadLabCharMemory(', '  function labActionChar('), context);
(async () => {
  result = {ok:false, reason:'Already inactive'};
  await context.deleteMem('mem-1');
  assert.equal(toasts.at(-1), 'Already inactive');
  assert.deepEqual(calls.at(-1), ['delete_memory','mem-1']);
  dom['memory-new-key'].value = 'key';
  dom['memory-new-content'].value = 'draft';
  await context.addMemory();
  assert.equal(dom['memory-add-status'].textContent, 'Already inactive');
  assert.equal(dom['memory-new-content'].value, 'draft');
  result = {ok:true, ref:'mem-2'};
  await context.addMemory();
  assert.equal(dom['memory-add-status'].textContent, 'Memory stored.');
  assert.equal(dom['memory-new-content'].value, '');
  assert.equal(refreshes, 2);
  const exp = {ref:'exp-1', text:'Owned observation'};
  result = {ok:true, experiences:[exp],
            scrapbook:{experiences:[exp], events:[{ref:'evt-1', text:'Event'}]}};
  await context.loadLabCharMemory('char-a');
  assert.equal((dom['lab-char-mem-l'].innerHTML.match(/Owned observation/g)||[]).length, 1);
  assert.match(dom['lab-char-mem-l'].innerHTML, /journal · read-only/);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-", str(app)], input=harness, text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
