"""Screen context collection must not stall Light readiness or outlive its owner."""
import asyncio
import threading

import httpx
import pytest

from test_request_reply_ownership import isolated_server, server, drain


def test_screen_context_does_not_block_the_light_listener(isolated_server, monkeypatch):
    runtime, _ = isolated_server
    entered, release, blocked = threading.Event(), threading.Event(), threading.Event()
    observations = []

    def vision(_request):
        entered.set()
        blocked.set()
        release.wait(0.5)
        blocked.clear()
        return 'Synthetic screen context'

    monkeypatch.setattr(server, 'is_screen_request', lambda _text: True)
    monkeypatch.setattr(server, 'screen_response', vision)
    monkeypatch.setattr(server, '_record_fresh_screen_evidence', lambda _state: None)

    async def scenario():
        await runtime.server.app.router.startup()
        loop = asyncio.get_running_loop()
        scheduled = []

        async def ping():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.server.app), base_url='http://fixture') as client:
                response = await client.get('/ping')
                observations.append((response.status_code, response.text, blocked.is_set()))

        def request_ping():
            if entered.wait(1):
                loop.call_soon_threadsafe(lambda: scheduled.append(asyncio.create_task(ping())))
            release.wait(0.15)
            release.set()

        timer = threading.Thread(target=request_ping)
        timer.start()
        try:
            runtime.stt.transcript = 'Friday describe the synthetic screen'
            await runtime.input({'role': 'user', 'type': 'audio', 'start': True})
            await runtime.input({'role': 'user', 'type': 'audio', 'end': True})
            while not scheduled:
                await asyncio.sleep(0.001)
            await asyncio.gather(*scheduled)
            assert observations == [(200, 'pong', True)], 'Screen/vision work blocked HTTP readiness'
        finally:
            release.set()
            timer.join(1)
            await runtime.server.app.router.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize('late_error', [False, True])
def test_stop_during_vision_rejects_late_output_and_releases_owner(isolated_server, monkeypatch, late_error):
    runtime, _ = isolated_server
    entered, release = threading.Event(), threading.Event()
    recorded = []

    def vision(_request):
        entered.set()
        release.wait(2)
        if late_error:
            raise RuntimeError('synthetic late vision error')
        return 'Obsolete synthetic screen context'

    monkeypatch.setattr(server, 'is_screen_request', lambda _text: True)
    monkeypatch.setattr(server, 'screen_response', vision)
    monkeypatch.setattr(server, '_record_fresh_screen_evidence', lambda state: recorded.append(state))

    async def scenario():
        await runtime.server.app.router.startup()
        runtime.stt.transcript = 'Friday describe the synthetic screen'
        await runtime.input({'type': 'audio', 'start': True})
        task = asyncio.create_task(runtime.input({'type': 'audio', 'end': True}))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            owner = runtime.server_state['turn_ownership']
            assert runtime.cancel_owned_turn('fixture_stop')[0]
            release.set()
            await task
            await drain(runtime)
            assert runtime.dispatched == [] and recorded == []
            assert runtime.server_state['turn_ownership'] is server.LOCAL_OWNERSHIP
            assert runtime.server_state['turn_ownership'] is not owner
            assert runtime.server_state.get('active_vision_pool') is None
            assert not runtime.server_state.get('_input_error_seen')
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.server.app.router.shutdown()

    asyncio.run(scenario())


def test_vision_worker_preserves_generation_context_and_input_pause_scope(isolated_server, monkeypatch):
    from contextlib import contextmanager
    from contextvars import ContextVar

    runtime, _ = isolated_server
    entered, release = threading.Event(), threading.Event()
    generation = ContextVar('fixture_identity_generation', default=None)
    observed = []

    @contextmanager
    def scoped(_state, attempt):
        token = generation.set(attempt)
        try:
            yield
        finally:
            generation.reset(token)

    def vision(_request):
        observed.append(generation.get())
        entered.set()
        release.wait(2)
        return 'Synthetic verified-context result'

    monkeypatch.setattr(server._identity, 'scoped', scoped)
    monkeypatch.setattr(server, 'is_screen_request', lambda _text: True)
    monkeypatch.setattr(server, 'screen_response', vision)
    monkeypatch.setattr(server, '_record_fresh_screen_evidence', lambda _state: None)

    async def scenario():
        await runtime.server.app.router.startup()
        runtime.stt.transcript = 'Friday describe the synthetic screen'
        await runtime.input({'type': 'audio', 'start': True})
        task = asyncio.create_task(runtime.input({'type': 'audio', 'end': True}))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            owner, attempt = runtime.server_state['turn_ownership'], runtime.server_state['turn_attempt']
            assert runtime.server_state['audio_link'].set_voice(True, runtime.output_queue.sync_q)
            release.set()
            await task
            assert observed == [attempt] and generation.get() is None
            assert runtime.server_state['turn_ownership'] is owner
            assert len(runtime.dispatched) == 1  # Input pause does not cancel admitted work.
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.server.app.router.shutdown()

    asyncio.run(scenario())
