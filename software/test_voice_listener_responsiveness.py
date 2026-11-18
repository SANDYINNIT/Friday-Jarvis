"""An in-flight transcription must not stop the actual Light readiness route."""
import asyncio
import socket
import threading
import time

import httpx
import pytest
import requests
import uvicorn

from test_request_reply_ownership import isolated_server, server


@pytest.mark.parametrize('provider', ['cloud', 'local'])
def test_transcription_does_not_block_light_ping(isolated_server, monkeypatch, provider):
    runtime, _ = isolated_server
    entered, release, blocked = threading.Event(), threading.Event(), threading.Event()
    observations = []

    def transcribe(*_args):
        blocked.set()
        entered.set()
        release.wait(0.5)
        blocked.clear()
        return ('', 'fixture', '') if provider == 'cloud' else ''

    monkeypatch.setattr(server.api_pools, 'cloud_stt_enabled', lambda: provider == 'cloud')
    monkeypatch.setattr(server, 'transcribe_wav', transcribe)
    runtime.stt.text = transcribe if provider == 'local' else lambda: ''

    async def scenario():
        await runtime.server.app.router.startup()
        loop = asyncio.get_running_loop()

        async def ping():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.server.app), base_url='http://fixture') as client:
                response = await client.get('/ping')
                observations.append((response.status_code, response.text, blocked.is_set()))

        scheduled = []

        def request_ping():
            if entered.wait(1):
                loop.call_soon_threadsafe(lambda: scheduled.append(asyncio.create_task(ping())))
            release.wait(0.15)
            release.set()

        timer = threading.Thread(target=request_ping)
        timer.start()
        try:
            runtime.audio_chunks = [bytes(1024)]
            await runtime.input({'role': 'user', 'type': 'audio', 'end': True})
            while not scheduled:
                await asyncio.sleep(0.001)
            await asyncio.gather(*scheduled)
            assert observations == [(200, 'pong', True)], 'Slow STT blocked the listener readiness route'
        finally:
            release.set()
            timer.join(1)
            await runtime.server.app.router.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['stt', 'vision'])
def test_slow_input_context_keeps_the_owned_tcp_listener_and_watchdog_ready(isolated_server, monkeypatch, stage):
    from source.server import light_lifecycle

    runtime, _ = isolated_server
    entered, release, ping_ready = threading.Event(), threading.Event(), threading.Event()
    pings_during_transcription, failures, exits = [], [], []

    def transcribe(_wav):
        entered.set()
        release.wait(5)
        return ('', 'fixture', '') if stage == 'stt' else 'Synthetic screen context'

    monkeypatch.setattr(server.api_pools, 'cloud_stt_enabled', lambda: stage == 'stt')
    if stage == 'stt':
        monkeypatch.setattr(server, 'transcribe_wav', transcribe)
        runtime.stt.text = lambda: ''
    else:
        monkeypatch.setattr(server, 'is_screen_request', lambda _text: True)
        monkeypatch.setattr(server, 'screen_response', transcribe)
        monkeypatch.setattr(server, '_record_fresh_screen_evidence', lambda _state: None)
        runtime.stt.transcript = 'Friday describe the synthetic screen'
    real_session = requests.Session

    class ObservedSession:
        def __init__(self):
            self.session = real_session()
            self.session.trust_env = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.session.close()

        def get(self, *args, **kwargs):
            response = self.session.get(*args, **kwargs)
            if response.status_code == 200 and response.text == 'pong':
                ping_ready.set()
                if entered.is_set() and not release.is_set():
                    pings_during_transcription.append(1)
            return response

    monkeypatch.setattr(light_lifecycle.requests, 'Session', ObservedSession)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    owned = uvicorn.Server(uvicorn.Config(runtime.server.app, host='127.0.0.1', port=port, log_config=None, access_log=False))
    runtime.server.uvicorn_server = owned
    worker = threading.Thread(target=owned.run, daemon=True)
    worker.start()

    def window():
        assert ping_ready.wait(4)
        async def submit():
            if stage == 'vision':
                await runtime.input({'type': 'audio', 'start': True})
            runtime.audio_chunks = [bytes(1024)]
            await runtime.input({'type': 'audio', 'end': True})
        future = asyncio.run_coroutine_threadsafe(submit(), runtime.server_state['event_loop'])
        assert entered.wait(2)
        try:
            # Longer than three one-second ping timeouts plus short checks.
            # No real STT/provider/device is involved.
            time.sleep(3.5)
            assert failures == [] and exits == []
            assert len(pings_during_transcription) >= 3
        finally:
            release.set()
            future.result(timeout=3)
            light_lifecycle.cleanup_light(runtime)
            worker.join(4)

    try:
        light_lifecycle.run_hud(runtime, worker, '127.0.0.1', port, window,
                                failures.append, terminate=exits.append,
                                startup_seconds=4, interval=0.02)
        assert failures == [] and exits == []
        assert not worker.is_alive()
    finally:
        release.set()
        owned.should_exit = True
        worker.join(4)


@pytest.mark.parametrize('boundary', ['cancel', 'pause', 'reconnect', 'new_capture', 'stop'])
@pytest.mark.parametrize('late_error', [False, True])
def test_superseded_transcription_cannot_admit_or_poison_a_turn(isolated_server, monkeypatch, boundary, late_error):
    runtime, _ = isolated_server
    link = runtime.server_state['audio_link']
    entered, release = threading.Event(), threading.Event()
    local_calls = []

    def transcribe(_wav):
        entered.set()
        release.wait(2)
        if late_error:
            raise RuntimeError('synthetic old transcription failure')
        return 'Friday, obsolete synthetic request', 'fixture', ''

    monkeypatch.setattr(server.api_pools, 'cloud_stt_enabled', lambda: True)
    monkeypatch.setattr(server, 'transcribe_wav', transcribe)
    runtime.stt.text = lambda: local_calls.append(1) or 'Friday, wrong fallback'

    async def scenario():
        await runtime.server.app.router.startup()
        session = link.connect(True)
        await runtime.input({'role': 'user', 'type': 'audio', 'start': True})
        runtime.audio_chunks = [bytes(1024)]
        original_owner = runtime.server_state['turn_ownership']
        task = asyncio.create_task(runtime.input({'role': 'user', 'type': 'audio', 'end': True}))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            if boundary == 'cancel':
                await runtime.input({'type': 'audio_capture_cancel'})
            elif boundary == 'pause':
                assert link.set_voice(True, runtime.output_queue.sync_q)
            elif boundary == 'reconnect':
                link.disconnect(session)
                assert link.connect(True) != session
            elif boundary == 'new_capture':
                await runtime.input({'role': 'user', 'type': 'audio', 'start': True})
                runtime.server_state['background_voice_until'] = 12345
            else:
                runtime.cancel_owned_turn('fixture_stop')
            release.set()
            await task
            assert runtime.dispatched == [] and local_calls == []
            assert runtime.server_state['turn_ownership'] is original_owner
            assert not runtime.server_state.get('_input_error_seen')
            assert runtime.server_state.get('active_stt_pool') is None
            if boundary == 'new_capture':
                assert runtime.server_state['background_voice_until'] == 12345
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runtime.server.app.router.shutdown()

    asyncio.run(scenario())


def test_cancelled_transcription_finishes_owned_work_before_session_release(isolated_server, monkeypatch):
    runtime, _ = isolated_server
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def transcribe(_wav):
        entered.set()
        release.wait(2)
        finished.set()
        return 'Friday, never admit this cancelled result', 'fixture', ''

    monkeypatch.setattr(server.api_pools, 'cloud_stt_enabled', lambda: True)
    monkeypatch.setattr(server, 'transcribe_wav', transcribe)

    async def scenario():
        runtime.audio_chunks = [bytes(1024)]
        task = asyncio.create_task(runtime.input({'type': 'audio', 'end': True}))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set() and runtime.dispatched == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
