"""Isolated public ingress checks. No real credentials, tunnel or provider."""

import ast
import asyncio
import collections
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

from source.server import public_ingress as ingress


TOKEN = b"fixture-token"
AUTH = [(b"x-friday-token", TOKEN)]
ROOT = Path(__file__).resolve().parent


def invoke(app, *, method="POST", path="/n8n", raw_path=b"/n8n", query=b"",
           headers=(), messages=(), scope_type="http"):
    async def run():
        received = collections.deque(messages)
        sent = []

        async def receive():
            assert received, "a denied request must not read the body"
            return received.popleft()

        async def send(message):
            sent.append(message)

        await app({"type": scope_type, "method": method, "path": path,
                   "raw_path": raw_path, "query_string": query,
                   "headers": headers}, receive, send)
        return sent

    return asyncio.run(run())


def body(data=b"fixture"):
    return [{"type": "http.request", "body": data}]


@pytest.fixture
def composition():
    """Use the installed interpreter router with an entirely synthetic engine."""
    from interpreter.core.async_core import create_router

    calls = []
    inputs = []
    output_reads = []

    async def consume():
        output_reads.append(True)
        if len(output_reads) == 1:
            return {"role": "assistant", "type": "message", "content": "fixture-output"}
        await asyncio.Event().wait()

    async def submit(message):
        inputs.append(message)

    local = FastAPI()
    local.include_router(create_router(SimpleNamespace(
        input=submit, output=consume, unsent_messages=collections.deque(),
        require_acknowledge=False, debug=False,
    )))

    @local.get("/ping")
    def ping():
        return PlainTextResponse("pong")

    async def forward_probe(request):
        calls.append((str(request.url), await request.aread()))
        raise AssertionError("denied traffic reached the local app")

    app = ingress.PublicIngress(32109, lambda: TOKEN.decode(),
                                transport=httpx.MockTransport(forward_probe))
    return SimpleNamespace(local=local, public=app, calls=calls,
                           inputs=inputs, output_reads=output_reads)


@pytest.mark.parametrize(("changes", "status"), [
    ({"headers": []}, 401),
    ({"headers": [(b"x-friday-token", b"wrong")]}, 401),
    ({"headers": AUTH * 2}, 401),
    ({"headers": [(b"authorization", b"Bearer fixture-token")] * 2}, 401),
    ({"headers": [(b"x-friday-token", b"wrong"), (b"authorization", b"Bearer fixture-token")]}, 401),
    ({"path": "/", "raw_path": b"/"}, 404),
    ({"path": "/chat", "raw_path": b"/chat"}, 404),
    ({"path": "/openai/chat/completions", "raw_path": b"/openai/chat/completions"}, 404),
    ({"path": "/settings", "raw_path": b"/settings"}, 404),
    ({"path": "/screen/history", "raw_path": b"/screen/history"}, 404),
    ({"path": "/n8n/pending", "raw_path": b"/n8n/pending", "method": "GET"}, 404),
    ({"path": "/n8n/pending/id", "raw_path": b"/n8n/pending/id", "method": "GET"}, 404),
    ({"method": "GET"}, 405),
    ({"method": "OPTIONS"}, 405),
    ({"method": "HEAD"}, 405),
    ({"raw_path": b"/%6e8n"}, 404),
    ({"raw_path": b"%2fn8n"}, 404),
    ({"raw_path": b"/prefix/../n8n"}, 404),
    ({"raw_path": b"//n8n"}, 404),
    ({"raw_path": b"/n8n/"}, 404),
    ({"raw_path": b"http://localhost/n8n"}, 404),
    ({"raw_path": None}, 404),
    ({"query": b"route=/settings"}, 404),
    ({"headers": AUTH + [(b"upgrade", b"websocket")]}, 400),
    ({"headers": AUTH + [(b"connection", b"keep-alive, Upgrade")]}, 400),
])
def test_denied_traffic_never_reaches_local_app(composition, changes, status):
    assert invoke(composition.public, **{"headers": AUTH, **changes})[0]["status"] == status
    assert composition.calls == composition.inputs == composition.output_reads == []


def test_websocket_scope_denied_before_accept_and_local_socket_still_works(composition, monkeypatch):
    denied = invoke(composition.public, scope_type="websocket", path="/", raw_path=b"/")
    assert denied == [{"type": "websocket.close", "code": 1008}]
    assert composition.calls == composition.inputs == composition.output_reads == []
    monkeypatch.setenv("INTERPRETER_REQUIRE_AUTH", "False")
    with TestClient(composition.local) as client:
        assert client.get("/ping").text == "pong"
        with client.websocket_connect("/") as ws:
            ws.send_json({"role": "user", "type": "message", "content": "fixture-input"})
            assert ws.receive_json()["content"] == "fixture-output"
    assert composition.inputs[0]["content"] == "fixture-input"


def test_local_hud_bridge_still_submits_to_its_interpreter(monkeypatch):
    from source.server import lab_budget, lab_state
    from source.server.status_ui import StatusBus, WebviewBridge

    monkeypatch.setattr(lab_budget, "shared", lambda: None)
    monkeypatch.setattr(lab_state, "ensure_started", lambda: True)
    local_calls = []

    def submit(text):
        local_calls.append(text)
        return {"accepted": True, "status": "queued"}

    bridge = WebviewBridge(StatusBus(), SimpleNamespace(
        server_state={"is_paused": False}, submit_text=submit,
    ))
    assert bridge.get_status()["paused"] is False
    assert bridge.send_text("fixture HUD")["accepted"] is True
    assert local_calls == ["fixture HUD"]


def test_missing_secret_fails_closed(composition):
    composition.public._secret = lambda: None
    assert invoke(composition.public, headers=AUTH)[0]["status"] == 403
    assert not composition.calls


def test_unready_or_stopping_interpreter_does_not_receive_credentials(composition):
    app = composition.public
    app._upstream_ready = lambda: False
    assert invoke(app, headers=AUTH)[0]["status"] == 503
    checks = iter((True, False))
    app._upstream_ready = lambda: next(checks)
    assert invoke(app, headers=AUTH, messages=body())[0]["status"] == 503
    assert not composition.calls


def test_request_limits_before_forward(composition, monkeypatch):
    app = composition.public
    assert invoke(app, headers=AUTH + [(b"content-length", b"65537")])[0]["status"] == 413
    assert invoke(app, headers=AUTH, messages=[
        {"type": "http.request", "body": b"x" * 65536, "more_body": True},
        *body(b"y"),
    ])[0]["status"] == 413
    assert invoke(app, headers=AUTH + [(b"content-length", b"2")],
                  messages=body(b"x"))[0]["status"] == 400
    monkeypatch.setattr(ingress, "BODY_TIMEOUT", 0.01)

    async def time_out():
        async def receive():
            await asyncio.Event().wait()

        sent = []

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "POST", "path": "/n8n",
                   "raw_path": b"/n8n", "headers": AUTH}, receive, send)
        return sent[0]["status"]

    assert asyncio.run(time_out()) == 408
    assert not composition.calls


def test_concurrency_is_bounded_and_slot_is_released(monkeypatch):
    monkeypatch.setattr(ingress, "MAX_IN_FLIGHT", 1)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

        app = ingress.PublicIngress(32109, lambda: TOKEN.decode(),
                                    transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://fixture.invalid") as client:
            first = asyncio.create_task(client.post("/n8n", headers=AUTH, content=b"one"))
            await entered.wait()
            assert (await client.post("/n8n", headers=AUTH, content=b"two")).status_code == 503
            release.set()
            assert (await first).status_code == 200
            assert (await client.post("/n8n", headers=AUTH, content=b"three")).status_code == 200

    asyncio.run(run())


def test_upstream_timeout_redirect_and_response_cap():
    seen = []

    async def upstream(request):
        seen.append(str(request.url))
        payload = await request.aread()
        if payload == b"wait":
            await asyncio.Event().wait()
        if payload == b"large":
            return httpx.Response(200, stream=httpx.ByteStream(b"x" * (ingress.MAX_RESPONSE + 1)))
        return httpx.Response(307, headers={"location": "https://fixture.invalid/settings"},
                              stream=httpx.ByteStream(b"redirect"))

    app = ingress.PublicIngress(32109, lambda: TOKEN.decode(),
                                transport=httpx.MockTransport(upstream))
    app._deadline = 0.03
    assert invoke(app, headers=AUTH, messages=body(b"wait"))[0]["status"] == 504
    assert invoke(app, headers=AUTH, messages=body(b"large"))[0]["status"] == 502
    result = invoke(app, headers=AUTH, messages=body(b"redirect"))
    assert result[0]["status"] == 307 and result[1]["body"] == b"redirect"
    assert not dict(result[0]["headers"]).get(b"location")
    assert seen == ["http://127.0.0.1:32109/n8n"] * 3


@pytest.mark.parametrize(("value", "expected"), [
    (None, 305), ("nan", 305), ("inf", 305), ("0", 305), ("30", 65),
    ("240", 305), ("999", 600),
])
def test_upstream_budget_matches_the_local_wait_contract(value, expected):
    assert ingress.upstream_deadline(value) == expected


@pytest.mark.parametrize(("payload", "expected_status", "expected_state"), [
    (b"fixture notice", 200, "completed"),
    (b'{"kind":"question","text":"fixture question"}', 200, "pending"),
    (b" ", 400, None),
])
def test_existing_n8n_handler_contract_through_public_ingress(payload, expected_status, expected_state):
    # Reuse the existing real handler extraction with fake dispatch and temp store.
    from test_n8n_inbound import _FakeRequest, _load_endpoint

    endpoint, _, calls = _load_endpoint(secret=TOKEN.decode(), reply_text="fixture reply")

    async def upstream(request):
        raw = await request.aread()
        assert raw == payload
        assert request.headers["content-type"] == "application/json"
        reply = await endpoint(_FakeRequest(raw.decode(), dict(request.headers)))
        return httpx.Response(reply.status_code, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(reply.json()).encode()))

    app = ingress.PublicIngress(32109, lambda: TOKEN.decode(),
                                transport=httpx.MockTransport(upstream))
    response = invoke(app, headers=[(b"authorization", b"bEaReR fixture-token"),
                                    (b"content-type", b"application/json")],
                      messages=body(payload))
    assert response[0]["status"] == expected_status
    data = json.loads(response[1]["body"])
    assert data.get("state") == expected_state
    if expected_state == "completed":
        assert data["reply"] == "fixture reply" and len(calls["dispatch"]) == 1
    elif expected_state == "pending":
        assert data["correlation_id"] and not calls["dispatch"]


@contextmanager
def listener(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, http="h11", ws="none", lifespan="off", access_log=False, log_level="error",
            proxy_headers=False, timeout_graceful_shutdown=1,
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 4
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "isolated loopback listener did not start"
            yield port
        finally:
            server.should_exit = True
            thread.join(4)
            assert not thread.is_alive()


def test_loopback_parser_fixed_destination_and_header_filtering():
    seen = []

    async def upstream(scope, receive, send):
        payload = await receive()
        seen.append((scope["method"], scope["raw_path"], dict(scope["headers"]), payload["body"]))
        await send({"type": "http.response.start", "status": 202,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"state":"pending"}'})

    def send(port, method, target, extra=None, payload=b"x"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, target, body=payload,
                               headers={"X-Friday-Token": TOKEN.decode(), **(extra or {})})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    with listener(upstream) as local_port:
        with listener(ingress.PublicIngress(local_port, lambda: TOKEN.decode())) as public_port:
            assert public_port != local_port
            for target in ("/%6e8n", "/%2fn8n", "//n8n", "/a/../n8n", "/n8n/",
                           "/n8n?foo", "http://fixture.invalid/n8n", "/n8n/pending/id"):
                assert send(public_port, "POST", target)[0] == 404, target
            assert send(public_port, "POST", "/n8n", {"Connection": "Upgrade",
                                                      "Upgrade": "websocket"})[0] == 400
            assert send(public_port, "GET", "/", {
                "Connection": "Upgrade", "Upgrade": "websocket",
                "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            }, payload=b"")[0] == 400
            assert send(public_port, "POST", "/n8n", {"Content-Length": "65537"})[0] == 413
            assert not seen
            assert send(public_port, "POST", "/n8n", {
                "Host": "fixture.invalid", "Forwarded": "host=other.invalid",
                "X-Forwarded-Host": "other.invalid", "Cookie": "session=fixture",
                "X-API-KEY": "fixture-key", "Content-Type": "text/plain",
            }) == (202, b'{"state":"pending"}')
    method, path, forwarded, payload = seen.pop()
    assert (method, path, payload) == ("POST", b"/n8n", b"x")
    assert forwarded[b"host"] == f"127.0.0.1:{local_port}".encode()
    assert forwarded[b"x-friday-token"] == TOKEN
    assert forwarded[b"content-type"] == b"text/plain"
    assert not ({b"forwarded", b"x-forwarded-host", b"cookie", b"x-api-key"} & forwarded.keys())
    assert not seen


def test_tunnel_worker_uses_a_dedicated_port_and_handles_provider_failure(monkeypatch):
    attempted = []
    finished = threading.Event()
    reported = []

    class FakeServer:
        started = False

        def __init__(self, config):
            assert isinstance(config.app, ingress.PublicIngress)

        def run(self, sockets):
            self.port = sockets[0].getsockname()[1]
            self.started = True
            assert finished.wait(2)

    def forward(target, **kwargs):
        attempted.append(target)
        raise RuntimeError("fixture credential must not be printed")

    def report(line):
        reported.append(line)
        finished.set()

    monkeypatch.setattr(ingress.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(ingress, "_n8n_secret", lambda: TOKEN.decode())
    monkeypatch.setitem(sys.modules, "ngrok", SimpleNamespace(forward=forward))
    worker = ingress.start_public_ingress(32109, upstream_ready=lambda: True, report=report)
    assert worker.daemon
    worker.join(4)
    assert not worker.is_alive()
    assert len(attempted) == 1 and attempted[0].startswith("127.0.0.1:")
    assert attempted[0] != "127.0.0.1:32109"
    assert reported == ["Public n8n ingress unavailable: tunnel error (RuntimeError)."]


@pytest.mark.parametrize("expose", [False, True])
def test_main_starts_local_client_and_hud_without_waiting_for_ingress(monkeypatch, expose):
    # Run the shipped entrypoint with inert collaborators instead of loading a
    # real profile or opening the native window, microphone or any server.
    from source.server import catalog_startup
    monkeypatch.setattr(catalog_startup, "prepare", lambda: None)
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    run.decorator_list = []
    run.args.defaults = []
    sequence = []
    server_calls = []
    ports = []

    class Thread:
        def __init__(self, *, target, args=(), daemon=False, name=""):
            self.target, self.args, self.name = target, args, name

        def start(self):
            if self.name == "friday-readiness":
                sequence.append("readiness")
            else:
                self.target(*self.args)

        def is_alive(self):
            return True

    def public(port, domain, *, upstream_ready):
        sequence.append("ingress")
        ports.append((port, domain))
        assert not upstream_ready()
        local.started = True
        assert upstream_ready()
        local.config.port += 1
        assert not upstream_ready()
        raise RuntimeError("fixture")

    from source.server import tls_trust
    monkeypatch.setattr(tls_trust, "bootstrap_windows_tls", lambda: None)
    monkeypatch.setattr(ingress, "start_public_ingress", public)
    from source.server import light_lifecycle
    def run_hud(_interpreter, _thread, host, port, show, _report):
        assert (host, port) == ("127.0.0.1", 32109)
        sequence.append("watchdog")
        show()
    monkeypatch.setattr(light_lifecycle, "run_hud", run_hud)
    local = SimpleNamespace(started=False, should_exit=False,
                            config=SimpleNamespace(host="127.0.0.1", port=32109))
    interpreter = SimpleNamespace(server=SimpleNamespace(uvicorn_server=local))
    # run() now imports these collaborators inside its body. Namespace-only
    # replacements below do not intercept those imports.
    from source.server import server as server_module, status_ui as ui_module
    monkeypatch.setattr(server_module, "start_server", lambda *a: (sequence.append("server"), server_calls.append(a)))
    monkeypatch.setattr(ui_module, "StatusBus", lambda *a: object())
    monkeypatch.setattr(ui_module, "attach_log_capture", lambda _: None)
    monkeypatch.setattr(ui_module, "run_status_window", lambda *a, **k: sequence.append("hud"))
    loader = SimpleNamespace(exec_module=lambda _: None)
    namespace = {
        "__file__": str(ROOT / "main.py"),
        "sys": sys,
        "os": __import__("os"),
        "importlib": SimpleNamespace(
            util=SimpleNamespace(spec_from_file_location=lambda *args: SimpleNamespace(loader=loader),
                                 module_from_spec=lambda _: SimpleNamespace(interpreter=interpreter)),
            import_module=lambda *a, **k: SimpleNamespace(run=lambda url, _: sequence.append(url)),
        ),
        "threading": SimpleNamespace(Thread=Thread),
        "signal": SimpleNamespace(SIGINT=2, SIGTERM=15, signal=lambda *a: None),
        "StatusBus": lambda *a: object(),
        "STATUS_THINKING": "thinking",
        "attach_log_capture": lambda _: None,
        "warmup_services": lambda *a: None,
        "start_server": lambda *a: (sequence.append("server"), server_calls.append(a)),
        "run_status_window": lambda *a, **k: sequence.append("hud"),
    }
    exec(compile(ast.Module(body=[run], type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
    namespace["run"](
        server="light", server_host="0.0.0.0", server_port=32109,
        expose=expose, domain=None, client="light-python", server_url=None,
        qr=False, profiles=False, profile=str(ROOT / "source/server/profiles/default.py"),
        debug=False, status_ui=True, background=True, multimodal=False,
    )
    assert server_calls[0][:2] == ("127.0.0.1", 32109)
    assert sequence == ["server", "127.0.0.1:32109"] + (["ingress"] if expose else []) + ["watchdog", "hud"]
    assert ports == ([(32109, None)] if expose else [])
