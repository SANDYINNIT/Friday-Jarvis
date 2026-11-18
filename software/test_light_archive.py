"""Light archive verification with the actual admitted runtime and fake I/O."""
import asyncio
from contextlib import closing
from datetime import datetime
import sqlite3
from types import SimpleNamespace

from source.server import light_archive
from source.server.server import _AttemptOutput, LOCAL_OWNERSHIP
from source.server.status_ui import StatusBus, WebviewBridge
from test_request_reply_ownership import drain, isolated_server, telegram


def reader(archive):
    runtime = SimpleNamespace(light_archive=archive)
    return WebviewBridge(StatusBus(), runtime)


def conversations(archive):
    bridge = reader(archive)
    return [(row, bridge.get_conversation_transcript(row["id"])["rows"])
            for row in bridge.get_conversation_library()["rows"]]


async def submit(runtime, text):
    return await asyncio.to_thread(runtime.submit_text, text)


def enqueue(runtime, kind, content, *, attempt=None, role="assistant"):
    attempt = runtime.server_state["turn_attempt"] if attempt is None else attempt
    frame = _AttemptOutput(attempt, {"role": role, "type": kind, "content": content})
    runtime.output_queue.sync_q.put(frame)
    return frame


def test_admitted_history_reopens_and_ignores_duplicate_and_old_generation(isolated_server):
    runtime, _ = isolated_server
    archive = runtime.light_archive

    async def scenario():
        await runtime.server.app.router.startup()
        assert (await submit(runtime, "First archive fixture"))["accepted"]
        old = runtime.server_state["turn_attempt"]
        assert conversations(archive)[0][1][0]["text"] == "First archive fixture"
        frame = enqueue(runtime, "message", "Here is the first answer.")
        runtime.output_queue.sync_q.put(frame)
        enqueue(runtime, "reasoning", "hidden reasoning")
        enqueue(runtime, "message", "", role="assistant")
        enqueue(runtime, "status", "complete", role="server")
        await drain(runtime)
        runtime.respond_thread = None
        assert (await submit(runtime, "Second archive fixture"))["accepted"]
        enqueue(runtime, "message", "Stale text from the old generation", attempt=old)
        runtime.complete("Second answer.")
        await drain(runtime)
        assert runtime.server_state["turn_ownership"] is LOCAL_OWNERSHIP

    asyncio.run(scenario())
    reopened = light_archive.ConversationArchive(archive.path)
    reopened.recover()
    [(row, messages)] = conversations(reopened)
    assert row["source"] == "hud" and row["title"] == "First archive fixture"
    assert [m["text"] for m in messages] == ["First archive fixture", "Here is the first answer.",
                                             "Second archive fixture", "Second answer."]
    assert {m["outcome"] for m in messages} == {"completed"}
    assert len({m["id"] for m in messages}) == 4 and len({m["turn_id"] for m in messages}) == 2
    assert all(datetime.fromisoformat(m["timestamp"]).utcoffset().total_seconds() == 0 for m in messages)
    assert conversations(reopened) == conversations(reopened)


def test_checkpoint_recovers_interrupted_and_fences_old_writer(isolated_server, monkeypatch):
    runtime, _ = isolated_server
    archive = runtime.light_archive
    monkeypatch.setattr(light_archive, "CHECKPOINT_SECONDS", 0.01)
    monkeypatch.setenv("FRIDAY_REDACTION", "0")

    async def scenario():
        await runtime.server.app.router.startup()
        await submit(runtime, "A checkpoint test")
        enqueue(runtime, "message", "<think>Never persist this</think>Saved prefix. Bearer sk-abcdefghij")
        await drain(runtime)
        await asyncio.sleep(0.04)
        assert conversations(archive)[0][1][-1]["text"] == "Saved prefix. Bearer"
        reopened = light_archive.ConversationArchive(archive.path)
        reopened.recover()
        before = conversations(reopened)
        assert {m["outcome"] for m in before[0][1]} == {"interrupted"}
        runtime.complete("Old writer must stay fenced.")
        await drain(runtime)
        assert conversations(reopened) == before
        assert reader(archive).get_conversation_library()["warning"] == light_archive.WARNING
        raw = archive.path.read_bytes()
        assert b"sk-abcdefghij" not in raw and b"Never persist this" not in raw

    asyncio.run(scenario())


def test_stop_and_provider_exhaustion_preserve_partials(isolated_server):
    runtime, _ = isolated_server

    async def scenario():
        await runtime.server.app.router.startup()
        await submit(runtime, "Stop fixture")
        enqueue(runtime, "message", "Partial before Stop")
        await drain(runtime)
        state = runtime.server_state
        stop = WebviewBridge(StatusBus(), runtime).stop_turn()
        assert stop["ok"]
        enqueue(runtime, "message", "Discard this late tail")
        enqueue(runtime, "status", "complete", role="server")
        runtime.respond_thread = None
        await drain(runtime)
        assert state["turn_ownership"] is LOCAL_OWNERSHIP
        await submit(runtime, "Failure fixture")
        enqueue(runtime, "message", "Partial before failure")
        enqueue(runtime, "error", "private raw provider failure", role="server")
        enqueue(runtime, "status", "complete", role="server")
        runtime.respond_thread = None
        await drain(runtime)
        await drain(runtime)
        [(row, messages)] = conversations(runtime.light_archive)
        assert [m["outcome"] for m in messages if m["speaker"] == "user"] == ["cancelled", "failed"]
        assert [(m["text"], m["state"]) for m in messages if m["speaker"] == "assistant"][:2] == [
            ("Partial before Stop", "partial"), ("Partial before failure", "partial")]
        assert "private raw provider failure" not in str(messages) and "Discard this" not in str(messages)

    asyncio.run(scenario())


def test_real_provider_retry_reuses_turn_and_reconciles_repeated_text(isolated_server):
    runtime, _ = isolated_server
    router = runtime.server_state["brain_router"]

    def hop(*args, **kwargs):
        router.current = ("fixture-fallback", None, "fixture")
        return True

    router.failover_to_next = hop

    async def scenario():
        await runtime.server.app.router.startup()
        await submit(runtime, "Retry this fixture")
        old = runtime.server_state["turn_attempt"]
        enqueue(runtime, "message", "Identical visible answer.")
        enqueue(runtime, "error", "503 synthetic", role="server")
        enqueue(runtime, "status", "complete", role="server")
        runtime.respond_thread = None
        await drain(runtime)
        assert runtime.server_state["turn_attempt"] is not old and len(runtime.dispatched) == 2
        enqueue(runtime, "message", "Obsolete reply", attempt=old)
        runtime.complete("Identical visible answer.")
        await drain(runtime)
        [(_, messages)] = conversations(runtime.light_archive)
        assert [m["text"] for m in messages] == ["Retry this fixture", "Identical visible answer."]
        assert len({m["turn_id"] for m in messages}) == 1
        assert all(m["outcome"] == "completed" for m in messages)

    asyncio.run(scenario())


def test_failed_checkpoint_keeps_last_valid_rows_and_warns(isolated_server):
    runtime, _ = isolated_server
    archive = runtime.light_archive

    async def scenario():
        await runtime.server.app.router.startup()
        await submit(runtime, "Stored first request")
        runtime.complete("Stored first answer.")
        await drain(runtime)
        baseline = conversations(archive)
        with closing(sqlite3.connect(archive.path)) as conn:
            conn.execute("""CREATE TRIGGER reject_fixture BEFORE INSERT ON message
                BEGIN SELECT RAISE(ABORT,'private error fixture'); END""")
        assert (await submit(runtime, "Will not be saved"))["accepted"]
        runtime.complete("Also not saved")
        await drain(runtime)
        assert conversations(archive) == baseline
        result = reader(archive).get_conversation_library()
        assert result["state"] == "ready" and result["warning"] == light_archive.WARNING
        assert "private error fixture" not in str(result)

    asyncio.run(scenario())


def test_queued_and_rejected_are_not_archived_and_owners_stay_separate(isolated_server):
    runtime, _ = isolated_server

    async def scenario():
        await runtime.server.app.router.startup()
        await submit(runtime, "Desktop owner request")
        queued = await telegram(runtime, 101, "First phone owner's request")
        assert queued.ok
        assert [entry["source"] for entry, _ in conversations(runtime.light_archive)] == ["hud"]
        runtime.complete("Desktop owner answer.")
        await drain(runtime)
        runtime.complete("First phone owner's answer.")
        await drain(runtime)
        await telegram(runtime, 202, "Second phone owner's request")
        runtime.complete("Second phone owner's answer.")
        await drain(runtime)
        runtime.server_state["is_paused"] = True
        assert not (await submit(runtime, "Rejected request"))["accepted"]
        rows = conversations(runtime.light_archive)
        assert len(rows) == 3
        assert sorted((entry["source"], messages[-1]["text"]) for entry, messages in rows) == [
            ("hud", "Desktop owner answer."), ("telegram", "First phone owner's answer."),
            ("telegram", "Second phone owner's answer.")]
        assert "Rejected request" not in str(rows)
        with closing(sqlite3.connect(runtime.light_archive.path)) as conn:
            assert len({tag for tag, in conn.execute("SELECT owner FROM conversation WHERE source='telegram'")}) == 2

    asyncio.run(scenario())
