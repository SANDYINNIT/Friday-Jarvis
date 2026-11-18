"""Exercise Light admission and Telegram dispatch with entirely synthetic I/O."""

import asyncio
import logging
import threading
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from source.server import (
    action_confirmations, activity_journal, brain_keepalive, display_patch, gemini_tts,
    lab_authority, lab_budget, lab_state,
    pending_requests, prompt_render_cache, remote_telegram, screen_capture_service,
    screen_companion, screen_understanding, self_improve, server, window_events,
)
from source.server.status_ui import WebviewBridge


class BackgroundStub:
    active = lambda self: False

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def stop(self):
        pass


class FakeSpeech(BackgroundStub):
    def __init__(self, *args, **kwargs):
        self.fed = []
        self.callbacks = []
        self.transcript = ""

    def text(self):
        return self.transcript

    def is_playing(self):
        return False

    def feed(self, text):
        self.fed.append(text)

    def play_async(self, *, on_audio_chunk, **kwargs):
        self.callbacks.append(on_audio_chunk)


class SyntheticInterpreter:
    def __init__(self):
        from interpreter.core.async_core import AsyncInterpreter

        self.server = SimpleNamespace(app=FastAPI(), run=lambda: None)
        self.llm = SimpleNamespace(_friday_trace_wrapped=True)
        self.tts = "openai"
        self.output_queue = None
        self.stop_event = threading.Event()
        self.respond_thread = None
        self.messages = []
        self.dispatched = []
        self.print = self.debug = self.play_audio = False
        self.output = AsyncInterpreter.output.__get__(self)
        self.respond = AsyncInterpreter.respond.__get__(self)
        self.auto_run = False
        self.frames = []

    async def input(self, frame):
        if "start" in frame:
            assert self.respond_thread is None or not self.respond_thread.is_alive()
        if "content" in frame:
            self.dispatched.append(frame["content"])
        if "end" in frame:
            from interpreter.core.friday_execution import bind_response

            self.stop_event.clear()
            self.producer = bind_response(self, self.respond)
            self.respond_thread = SimpleNamespace(is_alive=lambda: True)

    def _respond_and_store(self):
        yield from self.frames

    def complete(self, text):
        self.frames = [{"role": "assistant", "type": "message", "content": text},
                       {"role": "assistant", "type": "message", "end": True}]
        self.producer()
        self.respond_thread = None


@pytest.fixture
def isolated_server(monkeypatch, tmp_path, request):
    monkeypatch.setenv("FRIDAY_LIGHT_ARCHIVE_DB", str(tmp_path / "light_conversations.db"))
    monkeypatch.setenv("FRIDAY_KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    def noop(*args, **kwargs):
        pass

    monkeypatch.setenv("FRIDAY_HARD_TOOL_CAP", "1")
    monkeypatch.setattr(server, "_final_narration_completion", async_noop)
    monkeypatch.setattr(server, "_verified_work_summary",
                        lambda: {"verified": [], "unverified": []})

    monkeypatch.setattr(server, "threading", SimpleNamespace(
        Lock=threading.Lock, Thread=BackgroundStub, Timer=BackgroundStub,
    ))
    for name in ("DesktopEar", "SocialGuard", "FocusTracker", "WelcomeBackMonitor"):
        monkeypatch.setattr(server, name, BackgroundStub)
    for name in ("configure_console_output", "setup_file_logging", "_configure_calendar_route",
                 "log_action", "maybe_run_digest", "maybe_record_project_note", "_log_empty_turn",
                 "_play_chirp", "handle_ear_command"):
        monkeypatch.setattr(server, name, noop)
    monkeypatch.setattr(server, "_ScheduleStore", BackgroundStub)
    monkeypatch.setattr(server, "_automatic_memory_enabled", lambda _: False)
    monkeypatch.setattr(server, "_reminders_enabled", lambda _: False)
    monkeypatch.setattr(server, "_voice_trace_get", noop)
    monkeypatch.setattr(server, "friday_logger", lambda: logging.getLogger("ownership-fixture"))
    monkeypatch.setattr(server, "AudioToTextRecorder", FakeSpeech)
    monkeypatch.setattr(server, "TextToAudioStream", FakeSpeech)
    monkeypatch.setattr(server, "is_screen_request", lambda _: False)
    monkeypatch.setattr(server, "BrainRouter", lambda **_: SimpleNamespace(
        new_turn=noop, apply_for=lambda *_: ("fixture", None, "fixture-model"),
        mark_success=noop, mark_failure=noop,
        failover_to_next=noop, current=None,
    ))
    for module, name in (
        (activity_journal, "get_journal"), (screen_companion, "get_companion"),
        (gemini_tts, "CloudFirstTTSEngine"),
    ):
        monkeypatch.setattr(module, name, BackgroundStub)
    for module, name in (
        (action_confirmations, "note_user_turn"), (brain_keepalive, "start"),
        (display_patch, "install_display_helpers"),
        (lab_authority, "shared"), (lab_budget, "shared"),
        (lab_state, "ensure_started"), (lab_state, "note"),
        (lab_state, "cancel_execution"), (prompt_render_cache, "install"),
        (self_improve, "install_ambient_probe"), (self_improve, "append_outcome"),
        (self_improve, "scratchpad_note"), (self_improve, "scratchpad_reset"),
        (screen_capture_service, "start"), (window_events, "start"), (window_events, "tracker"),
    ):
        monkeypatch.setattr(module, name, noop)
    monkeypatch.setattr(lab_state, "health", lambda: {"start_error": "fixture"})
    monkeypatch.setattr(self_improve, "scratchpad_path", lambda: tmp_path / "scratchpad")
    monkeypatch.setattr(server.api_pools, "cloud_brain_enabled", lambda: False)
    monkeypatch.setattr(server.api_pools, "cloud_stt_enabled", lambda: False)
    real_store = pending_requests.PendingStore
    monkeypatch.setattr(pending_requests, "PendingStore",
                        lambda: real_store(path=tmp_path / "pending.json"))
    monkeypatch.setattr(pending_requests, "get_pending_store", lambda store: store)
    monkeypatch.setattr(server, "load_telegram_config", lambda: remote_telegram.TelegramConfig(
        token="synthetic-token", allowed_chat_ids=frozenset({"101", "202", "303"}),
    ))
    monkeypatch.setattr(remote_telegram.TelegramRemoteAdapter, "start", async_noop)
    deliveries = []

    async def transport(self, chat_id, text):
        deliveries.append(("text", str(chat_id), text))
        return True

    async def photo(self, chat_id, jpeg, caption=None):
        deliveries.append(("photo", str(chat_id), caption))
        assert jpeg == b"fixture-photo"
        return True

    monkeypatch.setattr(remote_telegram.TelegramRemoteAdapter, "_send", transport)
    monkeypatch.setattr(remote_telegram.TelegramRemoteAdapter, "send_photo", photo)
    monkeypatch.setattr(screen_understanding, "capture_screen_jpeg", lambda: b"fixture-photo")
    monkeypatch.setattr(server, "author_screen_photo", lambda *_: ("Fixture caption", "Fixture view"))

    def router(text, **kwargs):
        if "raise-fixture" in text:
            raise ValueError("synthetic failure")
        if text == "deterministic-fixture":
            return {"response": "Deterministic answer."}
        return None

    monkeypatch.setattr(server, "route_command", router)
    interpreter = SyntheticInterpreter()
    bus = None
    if getattr(request, "param", None) in ("knowledge", "mcp"):
        from source.server import n8n_runtime
        from source.server.status_ui import StatusBus

        monkeypatch.setattr(n8n_runtime, "get_adapter", lambda: SimpleNamespace(
            config={"secret": "fixture-token"}))
        bus = interpreter.fixture_status = StatusBus()
        interpreter.fixture_completions = []

        def complete(**params):
            interpreter.fixture_completions.append(params)
            stream = getattr(interpreter, "fixture_stream", None)
            if stream is not None:
                yield from stream(params)
                return
            yield {"choices": [{"delta": {"content": "Fixture provider text"}, "finish_reason": "stop"}]}

        interpreter.llm = SimpleNamespace(completions=complete, model="fixture-model")
    server.start_server("127.0.0.1", 0, interpreter, voice=True, debug=False, status_bus=bus)
    return interpreter, deliveries


async def async_noop(*args, **kwargs):
    pass


async def drain(interpreter):
    marker = {"ignored": True, "end": True}
    interpreter.output_queue.sync_q.put(marker)
    delivered = []
    while (frame := await asyncio.wait_for(interpreter.output(), timeout=2)) != marker:
        delivered.append(frame)
    return delivered


async def telegram(interpreter, chat_id, text):
    adapter = interpreter.server_state["telegram_adapter"]
    adapter.min_message_interval = 0
    return await adapter.process_update({"message": {
        "text": text, "chat": {"id": chat_id, "type": "private"}, "from": {"id": chat_id},
    }})


async def admit_local(interpreter, channel):
    if channel == "hud":
        assert (await asyncio.to_thread(interpreter.submit_text, "local request"))["accepted"]
    else:
        interpreter.stt.transcript = "Friday please answer my local question"
        await interpreter.input({"start": True})
        await interpreter.input({"end": True})
    assert len(interpreter.dispatched) == 1


@pytest.mark.parametrize("channel", ["hud", "voice"])
def test_local_owner_survives_telegram_queue_and_late_frames(isolated_server, channel):
    interpreter, deliveries = isolated_server

    async def scenario():
        await interpreter.server.app.router.startup()
        state = interpreter.server_state
        await admit_local(interpreter, channel)
        first = state["turn_ownership"]
        attempt = state["turn_attempt"]
        queued = await telegram(interpreter, 101, "Find a result and then take a screenshot afterwards")
        assert queued.ok and state["turn_ownership"] is first
        assert len(interpreter.dispatched) == 1
        assert len(deliveries) == 1 and deliveries[0][1] == "101"
        record = state["pending_turns"][0]["ownership"]
        assert record.chat_id == "101" and record.muted
        with pytest.raises(AttributeError):
            record.chat_id = "202"
        interpreter.complete("Local reply.")
        await drain(interpreter)
        assert state["turn_ownership"] is record
        assert interpreter.tts.fed == ["Local reply."]
        assert not any(kind == "photo" for kind, _, _ in deliveries)
        for frame in (
            {"role": "assistant", "type": "message", "content": "stale"},
            {"role": "server", "type": "error", "content": "stale error"},
            {"role": "server", "type": "status", "content": "complete"},
        ):
            interpreter.output_queue.sync_q.put(server._AttemptOutput(attempt, frame))
        await drain(interpreter)
        assert state["turn_ownership"] is record and not state["turn_failed"]
        interpreter.complete("Telegram reply.")
        await drain(interpreter)
        assert deliveries[-1] == ("photo", "101", "Telegram reply.")
        assert interpreter.tts.fed == ["Local reply."]
        assert state["turn_ownership"] is server.LOCAL_OWNERSHIP

    asyncio.run(scenario())


def test_telegram_owner_survives_queued_hud_and_stale_playback(isolated_server):
    interpreter, deliveries = isolated_server

    async def scenario():
        await interpreter.server.app.router.startup()
        state = interpreter.server_state
        await telegram(interpreter, 101, "Read something")
        first = state["turn_ownership"]
        await asyncio.to_thread(interpreter.submit_text, "deterministic-fixture")
        assert state["turn_ownership"] is first
        interpreter.complete("Text only.")
        await drain(interpreter)
        assert deliveries[-1] == ("text", "101", "Text only.")
        await drain(interpreter)
        assert interpreter.tts.fed == ["Deterministic answer."]
        callback = interpreter.tts.callbacks[-1]
        await telegram(interpreter, 202, "Another text only request")
        callback(b"earlier local audio")
        assert b"earlier local audio" in await drain(interpreter)
        assert not state["tts_audio_seen"]
        interpreter.cancel_owned_turn("test stop")
        callback(b"obsolete audio")
        assert b"obsolete audio" not in await drain(interpreter)
        interpreter.complete("Discard this.")
        await drain(interpreter)
        assert deliveries[-1] == ("text", "202", "Cancelled.")
        assert interpreter.tts.fed == ["Deterministic answer."]

    asyncio.run(scenario())


def test_refusal_and_handler_exception_do_not_steal_reply(isolated_server):
    interpreter, deliveries = isolated_server

    async def scenario():
        await interpreter.server.app.router.startup()
        state = interpreter.server_state
        await telegram(interpreter, 101, "Start one request")
        active = state["turn_ownership"]
        state["is_paused"] = True
        refused = await telegram(interpreter, 202, "Take a screenshot")
        assert "paused" in refused.text and state["turn_ownership"] is active
        assert not state["pending_turns"]
        state["is_paused"] = False
        await telegram(interpreter, 202, "raise-fixture")
        await telegram(interpreter, 303, "Start another request")
        assert state["turn_ownership"] is active
        interpreter.complete("First answer.")
        await drain(interpreter)
        await drain(interpreter)
        assert ("text", "101", "First answer.") in deliveries
        assert ("text", "202", "I couldn't start that request.") in deliveries
        assert state["turn_ownership"].chat_id == "303"
        interpreter.complete("Third answer.")
        await drain(interpreter)
        failed = await telegram(interpreter, 202, "raise-fixture")
        assert failed.reason == "handler_error"
        await drain(interpreter)
        assert deliveries[-2] == ("text", "303", "Third answer.")
        assert state["turn_ownership"] is server.LOCAL_OWNERSHIP
        assert not interpreter.tts.fed

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_path", ["hud", "http"])
def test_stop_preserves_queue_and_blocks_cancelled_reply_and_screenshot(isolated_server, stop_path):
    interpreter, deliveries = isolated_server

    async def scenario():
        app = interpreter.server.app
        await app.router.startup()
        state = interpreter.server_state
        await telegram(interpreter, 101, "Do a task then take a screenshot")
        active = state["turn_ownership"]
        await telegram(interpreter, 202, "Next request")
        if stop_path == "hud":
            bridge = WebviewBridge(None, interpreter)
            result = await asyncio.to_thread(bridge.stop_turn)
        else:
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 10))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                result = (await client.post("/chat/cancel")).json()
        assert result["stopped_active_turn"] and interpreter.stop_event.is_set()
        assert state["turn_ownership"] is active
        interpreter.complete("Cancelled stale result.")
        await drain(interpreter)
        assert ("text", "101", "Cancelled.") in deliveries
        assert state["turn_ownership"].chat_id == "202"
        interpreter.complete("Next answer.")
        await drain(interpreter)
        assert deliveries[-1] == ("text", "202", "Next answer.")
        assert not any(kind == "photo" for kind, _, _ in deliveries)
        assert not interpreter.tts.fed

    asyncio.run(scenario())


def test_cancelled_http_waiter_is_skipped_and_next_telegram_runs(isolated_server):
    interpreter, deliveries = isolated_server

    async def scenario():
        app = interpreter.server.app
        await app.router.startup()
        state = interpreter.server_state
        await admit_local(interpreter, "hud")
        first = state["turn_ownership"]
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 10))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            waiting = asyncio.create_task(client.post("/chat", content="Abandoned queued command"))
            await asyncio.sleep(0)  # /chat creates its reply box
            await asyncio.sleep(0)  # /chat's lock acquisition runs
            async with asyncio.timeout(2):
                while not state["pending_turns"]:
                    await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        assert state["turn_ownership"] is first
        await telegram(interpreter, 202, "Next request")
        interpreter.complete("Local answer.")
        await drain(interpreter)
        assert state["turn_ownership"].chat_id == "202"
        assert all("Abandoned queued command" not in text for text in interpreter.dispatched)
        interpreter.complete("Next answer.")
        await drain(interpreter)
        assert deliveries[-1] == ("text", "202", "Next answer.")

    asyncio.run(scenario())


def test_reader_disconnect_does_not_drop_finalization(isolated_server, monkeypatch):
    interpreter, deliveries = isolated_server

    async def scenario():
        await interpreter.server.app.router.startup()
        state = interpreter.server_state
        entered, release = asyncio.Event(), asyncio.Event()
        adapter = state["telegram_adapter"]
        send_text = adapter.send_text

        async def blocked_send(chat_id, text):
            entered.set()
            await release.wait()
            return await send_text(chat_id, text)

        monkeypatch.setattr(adapter, "send_text", blocked_send)
        await telegram(interpreter, 101, "First request")
        await telegram(interpreter, 202, "Second request")
        interpreter.complete("First answer.")

        async def consume():
            while True:
                await interpreter.output()

        reader = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert state["turn_ownership"].chat_id == "101"
        release.set()
        await drain(interpreter)
        assert deliveries.count(("text", "101", "First answer.")) == 1
        assert state["turn_ownership"].chat_id == "202"
        interpreter.complete("Second answer.")
        await drain(interpreter)
        assert deliveries[-1] == ("text", "202", "Second answer.")

    asyncio.run(scenario())


def test_direct_screenshot_and_queue_capacity_are_owner_scoped(isolated_server):
    interpreter, deliveries = isolated_server

    async def scenario():
        await interpreter.server.app.router.startup()
        state = interpreter.server_state
        await admit_local(interpreter, "hud")
        owner = state["turn_ownership"]
        await telegram(interpreter, 101, "Take a screenshot")
        for index in range(server._PENDING_QUEUE_MAX - 1):
            await telegram(interpreter, 202, f"Queued fixture request {index}")
        accepted = list(state["pending_turns"])
        refused = await telegram(interpreter, 303, "Overflow request")
        assert "queue is full" in refused.text
        assert list(state["pending_turns"]) == accepted
        assert state["turn_ownership"] is owner
        assert not any(kind == "photo" for kind, _, _ in deliveries)
        interpreter.complete("Local answer.")
        await drain(interpreter)
        assert deliveries[-1] == ("photo", "101", "Fixture caption")
        await drain(interpreter)
        assert state["turn_ownership"].chat_id == "202"
        assert [row for row in deliveries if row[0] == "photo"] == [
            ("photo", "101", "Fixture caption")
        ]

    asyncio.run(scenario())


def test_tool_cap_notifies_only_its_owner_and_drains_next(isolated_server):
    interpreter, deliveries = isolated_server

    async def scenario():
        await interpreter.server.app.router.startup()
        state = interpreter.server_state
        await telegram(interpreter, 101, "Run a bounded task")
        await telegram(interpreter, 202, "Next request")
        interpreter.frames = [
            {"role": "assistant", "type": "code", "start": True},
            {"role": "assistant", "type": "code", "start": True},
            {"role": "assistant", "type": "code", "start": True},
            {"role": "assistant", "type": "message", "content": "Late model result."},
        ]
        interpreter.producer()
        interpreter.respond_thread = None
        await drain(interpreter)
        assert state["turn_ownership"].chat_id == "202"
        final = [text for kind, chat, text in deliveries if kind == "text" and chat == "101"]
        assert len(final) == 2 and "Late model result." not in final
        assert interpreter.stop_event.is_set() is False  # next turn re-armed it
        interpreter.complete("Next answer.")
        await drain(interpreter)
        assert deliveries[-1] == ("text", "202", "Next answer.")
        assert not interpreter.tts.fed

    asyncio.run(scenario())


def test_provider_error_and_next_http_reply_stay_with_their_owners(isolated_server):
    interpreter, deliveries = isolated_server

    async def scenario():
        app = interpreter.server.app
        await app.router.startup()
        state = interpreter.server_state
        await telegram(interpreter, 101, "First request")
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 10))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            waiting = asyncio.create_task(client.post("/chat", content="deterministic-fixture"))
            async with asyncio.timeout(2):
                while not state["pending_turns"]:
                    await asyncio.sleep(0)
            attempt = state["turn_attempt"]
            interpreter.output_queue.sync_q.put(server._AttemptOutput(attempt, {
                "role": "server", "type": "error", "content": "Synthetic provider error",
            }))
            interpreter.output_queue.sync_q.put(server._AttemptOutput(attempt, {
                "role": "server", "type": "status", "content": "complete",
            }))
            interpreter.respond_thread = None
            await drain(interpreter)
            await drain(interpreter)
            response = await asyncio.wait_for(waiting, timeout=2)
        assert response.status_code == 200
        assert response.text == "Deterministic answer."
        assert deliveries[-1][1] == "101" and "unavailable" in deliveries[-1][2]
        assert not interpreter.tts.fed

    asyncio.run(scenario())

def test_correlated_integration_decisions_on_real_admission_and_polling(
    isolated_server, monkeypatch,
):
    from source.server import n8n_runtime

    interpreter, deliveries = isolated_server
    store = interpreter.server_state["pending_store"]
    first = store.add("Should I schedule the call?")
    second = store.add("Should I send the update?")
    callbacks = []
    consent_inputs = []

    def callback(url, *, json, headers, timeout):
        assert url == "http://127.0.0.1:9/webhook/pending-answer"
        assert headers["X-Friday-Token"] == "fixture-token"
        persisted = store.get(json["correlation_id"])
        assert persisted.state == "answered" and persisted.answer == json["answer"]
        callbacks.append(json)
        return SimpleNamespace(status_code=200, json=lambda: {"accepted": "transport-only"})

    adapter = n8n_runtime.N8NAdapter(
        config=n8n_runtime.load_config(path=store.path + ".adapter", environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_SECRET": "fixture-token",
            "FRIDAY_N8N_WEBHOOK_PENDING_ANSWER": "http://127.0.0.1:9/webhook/pending-answer",
        }),
        http_post=callback,
    )
    monkeypatch.setattr(n8n_runtime, "_ADAPTER", adapter)
    monkeypatch.setattr(action_confirmations, "note_user_turn",
                        lambda text, **_: consent_inputs.append(text))

    async def scenario():
        app = interpreter.server.app
        await app.router.startup()
        await telegram(interpreter, 101, "How is the weather tomorrow?")
        assert interpreter.dispatched and callbacks == []
        assert first.is_open() and second.is_open()
        interpreter.complete("Normal chat reply.")
        await drain(interpreter)

        answer = "yes, say hello and then take a screenshot afterwards"
        for command, expected in (
            ("n8n decision: yes", "Which n8n request?"),
            ("n8n decision artifact-7: approve", "Use an n8n request ID"),
            (f"n8n decision {second.id}: {answer}", "application acceptance is unconfirmed"),
            (f"n8n decision {second.id}: no", "already resolved"),
            (f"n8n decision {first.id}: ignore", "Local pending status"),
        ):
            await telegram(interpreter, 101, command)
            await drain(interpreter)
            assert deliveries[-1][1] == "101"
            assert expected.lower() in deliveries[-1][2].lower()
        assert [data["correlation_id"] for data in callbacks] == [second.id]
        assert len(consent_inputs) == 1
        assert "weather tomorrow" in consent_inputs[0].lower()
        assert not interpreter.tts.fed
        assert len(interpreter.dispatched) == 1
        assert all(kind == "text" for kind, _, _ in deliveries)

        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 10))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            headers = {"X-Friday-Token": "fixture-token"}
            ignored = await client.get(f"/n8n/pending/{first.id}", headers=headers)
            answered = await client.get(f"/n8n/pending/{second.id}", headers=headers)
        assert ignored.status_code == answered.status_code == 200
        assert ignored.json()["state"] == "ignored"
        assert answered.json()["state"] == "answered" and answered.json()["answer"] == answer

    asyncio.run(scenario())


def test_hud_pending_decision_uses_existing_authority_and_callback(isolated_server, monkeypatch):
    from source.server import n8n_runtime

    interpreter, _deliveries = isolated_server
    state = interpreter.server_state
    pending = state['pending_store']
    first, second = pending.add('First question'), pending.add('Second question')
    callbacks = []

    def callback(name, payload):
        assert name == 'pending_answer'
        assert pending.get(payload['correlation_id']).state == 'answered'
        callbacks.append(payload['correlation_id'])
        return {'http_status':200, 'result':{'ok':False}}

    adapter = SimpleNamespace(config={'enabled':True}, call=callback)
    monkeypatch.setattr(n8n_runtime, 'get_adapter', lambda: adapter)
    bridge = WebviewBridge(None, interpreter)
    state['is_paused'] = True
    assert not bridge.decide_pending_request(second.id, 'answer')['ok']
    assert callbacks == []
    state['is_paused'] = False
    saved = bridge.decide_pending_request(second.id, 'answer')
    assert saved['ok'] and saved['request']['correlation_id'] == second.id
    assert 'application failure' in saved['message'] and 'completion is unverified' in saved['message']
    assert not bridge.decide_pending_request(second.id, 'repeat')['ok']
    assert not bridge.decide_pending_request('artifact-7', 'approve')['ok']
    assert first.is_open() and callbacks == [second.id]
    assert not interpreter.dispatched
