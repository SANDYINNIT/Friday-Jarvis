"""Normal HUD supervision with a real loopback ASGI listener and fake UI/devices."""
import asyncio
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from source.server.light_lifecycle import RECOVERY_EXIT, cleanup_light, listener_serving, run_hud


def test_failure_stack_is_bounded_locations_without_private_content():
    import json
    from source.server.light_lifecycle import server_thread_stack

    entered, release = threading.Event(), threading.Event()

    def owned_wait():
        private_request = 'never-copy-private-request-or-credential-value'
        entered.set()
        release.wait(2)
        return private_request

    worker = threading.Thread(target=owned_wait)
    worker.start()
    try:
        assert entered.wait(1)
        snapshot = server_thread_stack(worker)
        assert 1 <= len(snapshot) <= 16
        assert any(frame['function'] == 'owned_wait' for frame in snapshot)
        assert all(set(frame) == {'file', 'function', 'line'} for frame in snapshot)
        assert all('/' not in frame['file'] and '\\' not in frame['file'] for frame in snapshot)
        assert 'never-copy-private' not in json.dumps(snapshot)
    finally:
        release.set()
        worker.join(2)


def test_modern_hud_initialization_failure_uses_existing_fallback(monkeypatch):
    from source.server import status_ui

    fallback = []
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.setattr(status_ui, "FaceStatusWindow", lambda *_:
                        SimpleNamespace(run=lambda: fallback.append("fallback ran")))
    status_ui.run_status_window(status_ui.StatusBus(), SimpleNamespace())
    assert fallback == ["fallback ran"]


@pytest.mark.parametrize("failure", ["lost-listener", "unresponsive-listener", "server-exit", "startup-timeout", "hud-error", "intentional"])
def test_normal_hud_supervises_owned_asgi_listener(monkeypatch, failure, capsys):
    from source.server import light_lifecycle

    observed = threading.Event()
    terminated = threading.Event()
    failures, exits, cleaned, loop = [], [], [], []
    unresponsive = threading.Event()

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            loop.append(asyncio.get_running_loop())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        assert scope["path"] == "/ping"
        await send({"type": "http.response.start", "status": 503 if unresponsive.is_set() else 200})
        await send({"type": "http.response.body", "body": b"unavailable" if unresponsive.is_set() else b"pong"})
        observed.set()

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_config=None, access_log=False))
    interpreter = SimpleNamespace(server=SimpleNamespace(uvicorn_server=server),
                                  stt=SimpleNamespace(shutdown=lambda: cleaned.append("audio")),
                                  close_mcp=lambda: cleaned.append("mcp"))
    worker = threading.Thread(target=server.run, daemon=True)
    if failure == "startup-timeout":
        # Live worker without its own ASGI startup cannot become ready.
        worker = threading.Thread(target=lambda: terminated.wait(5), daemon=True)
    worker.start()

    def terminate(code):
        exits.append(code)
        terminated.set()

    def window():
        if failure == "startup-timeout":
            assert terminated.wait(5)
            return
        assert observed.wait(4)
        assert listener_serving(interpreter, worker, "127.0.0.1", port)
        assert not listener_serving(interpreter, worker, "127.0.0.1", port + 1)
        if failure == "lost-listener":
            def close():
                for listener in server.servers:
                    listener.close()
            loop[0].call_soon_threadsafe(close)
            assert worker.is_alive() and not server.should_exit
        elif failure == "unresponsive-listener":
            # A bound socket with failing HTTP readiness is not a lost socket.
            time.sleep(0.1)  # let the initial healthy monitor request finish
            unresponsive.set()
            assert listener_serving(interpreter, worker, "127.0.0.1", port)
        elif failure == "server-exit":
            server.should_exit = True
        elif failure == "intentional":
            cleanup_light(interpreter)
            worker.join(4)
            return
        else:
            raise RuntimeError("fake HUD failed")
        assert terminated.wait(5)

    # On a deliberate server exit, allow the server thread to return before
    # the miss window elapses so the distinct worker-exit branch is tested.
    interval = 0.15 if failure == "server-exit" else 0.03
    try:
        run_hud(interpreter, worker, "127.0.0.1", port, window, failures.append,
                terminate=terminate, startup_seconds=0.15 if failure == "startup-timeout" else 4,
                interval=interval)
        worker.join(4)
        assert not worker.is_alive()
        assert not listener_serving(interpreter, worker, "127.0.0.1", port)
        assert sorted(cleaned) == ["audio", "mcp"]
        expected = {"lost-listener": "Light listener lost after readiness",
                    "unresponsive-listener": "Light listener unresponsive after readiness",
                    "server-exit": "Light server thread exited",
                    "startup-timeout": "Light listener startup deadline exceeded",
                    "hud-error": "HUD and fallback exited with an error"}
        assert failures == ([] if failure == "intentional" else [expected[failure]])
        assert exits == ([] if failure == "intentional" else [RECOVERY_EXIT])
        assert ("Owned listener ready" in capsys.readouterr().out) is (failure != "startup-timeout")
    finally:
        server.should_exit = True
        terminated.set()
        worker.join(4)
