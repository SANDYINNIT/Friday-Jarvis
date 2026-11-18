"""Dedicated public HTTP boundary for the Light server's n8n webhook."""

import asyncio
import hmac
import json
import math
import os
import socket
import threading

import httpx
import uvicorn


MAX_BODY = 64 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_IN_FLIGHT = 8
BODY_TIMEOUT = 10.0


def upstream_deadline(value):
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
    except (TypeError, ValueError):
        seconds = 240.0
    return min(600.0, seconds + min(seconds, 60.0) + 5.0)


class _TooLarge(Exception):
    pass


class _BadBody(Exception):
    pass


class _Disconnected(Exception):
    pass


async def _read_body(receive, declared):
    body = bytearray()
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            raise _Disconnected
        chunk = event.get("body", b"")
        if len(body) + len(chunk) > MAX_BODY:
            raise _TooLarge
        body.extend(chunk)
        if not event.get("more_body"):
            if declared is not None and len(body) != declared:
                raise _BadBody
            return bytes(body)


async def _respond(send, status, body, headers):
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _reject(send, status, reason, *, allow_post=False):
    headers = [(b"content-type", b"application/json")]
    if allow_post:
        headers.append((b"allow", b"POST"))
    await _respond(send, status, json.dumps({"ok": False, "error": reason}).encode(), headers)


class PublicIngress:
    """One authorized route with a fixed upstream. No interpreter objects."""

    def __init__(self, light_port, secret, *, upstream_ready=None, text_timeout=None, transport=None):
        self._url = f"http://127.0.0.1:{int(light_port)}/n8n"
        self._secret = secret
        self._upstream_ready = upstream_ready
        self._transport = transport
        self._deadline = upstream_deadline(text_timeout)
        self._in_flight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        headers = {}
        for name, value in scope.get("headers", ()):
            headers.setdefault(name.lower(), []).append(value)
        if b"upgrade" in headers or any(
            part.strip().lower() == b"upgrade"
            for value in headers.get(b"connection", ())
            for part in value.split(b",")
        ):
            await _reject(send, 400, "upgrades are not supported")
            return
        if (scope.get("raw_path") != b"/n8n" or scope.get("path") != "/n8n"
                or scope.get("query_string", b"")):
            await _reject(send, 404, "not found")
            return
        if scope.get("method") != "POST":
            await _reject(send, 405, "method not allowed", allow_post=True)
            return

        try:
            secret = str(self._secret() or "").encode("utf-8")
        except Exception:
            secret = b""
        if not secret:
            await _reject(send, 403, "n8n inbound is not configured")
            return
        tokens = headers.get(b"x-friday-token", [])
        auth = headers.get(b"authorization", [])
        if len(tokens) > 1 or len(auth) > 1:
            await _reject(send, 401, "unauthorized")
            return
        token = tokens[0].decode("latin-1").strip() if tokens else ""
        if not token and auth:
            authorization = auth[0].decode("latin-1")
            if authorization.lower().startswith("bearer "):
                token = authorization[7:].strip()
        if not token or not hmac.compare_digest(token.encode("utf-8"), secret):
            await _reject(send, 401, "unauthorized")
            return
        if self._upstream_ready is not None and not self._upstream_ready():
            await _reject(send, 503, "local FRIDAY server is not ready")
            return

        lengths = headers.get(b"content-length", [])
        declared = None
        if lengths:
            value = lengths[0]
            if len(lengths) != 1 or not value.isdigit():
                await _reject(send, 400, "invalid content length")
                return
            significant = value.lstrip(b"0")
            if len(significant) > 5 or (significant and int(significant) > MAX_BODY):
                await _reject(send, 413, "payload too large")
                return
            declared = int(significant or b"0")

        if self._in_flight >= MAX_IN_FLIGHT:
            await _reject(send, 503, "public ingress is busy")
            return
        self._in_flight += 1
        try:
            try:
                body = await asyncio.wait_for(_read_body(receive, declared), BODY_TIMEOUT)
            except asyncio.TimeoutError:
                await _reject(send, 408, "request body timed out")
                return
            except _TooLarge:
                await _reject(send, 413, "payload too large")
                return
            except _BadBody:
                await _reject(send, 400, "invalid request body")
                return
            except _Disconnected:
                return

            upstream_headers = [(b"x-friday-token", token.encode("latin-1"))]
            if headers.get(b"content-type"):
                upstream_headers.append((b"content-type", headers[b"content-type"][0]))
            if self._upstream_ready is not None and not self._upstream_ready():
                await _reject(send, 503, "local FRIDAY server is not ready")
                return
            try:
                status, response_headers, response_body = await asyncio.wait_for(
                    self._forward(body, upstream_headers), self._deadline)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                await _reject(send, 504, "local n8n handler timed out")
                return
            except (httpx.HTTPError, _TooLarge):
                await _reject(send, 502, "local n8n handler unavailable")
                return
            await _respond(send, status, response_body, response_headers)
        finally:
            self._in_flight -= 1

    async def _forward(self, body, headers):
        async with httpx.AsyncClient(
            follow_redirects=False, trust_env=False, transport=self._transport,
            timeout=httpx.Timeout(self._deadline, connect=2.0),
        ) as client:
            # The local n8n handler sends JSON, with no content coding.
            headers = [*headers, (b"accept-encoding", b"identity")]
            async with client.stream("POST", self._url, content=body, headers=headers) as reply:
                response_headers = [
                    (name, value) for name, value in reply.headers.raw
                    if name.lower() in (b"content-type", b"content-encoding")
                ]
                response_body = bytearray()
                async for chunk in reply.aiter_raw():
                    if len(response_body) + len(chunk) > MAX_RESPONSE:
                        raise _TooLarge
                    response_body.extend(chunk)
                return reply.status_code, response_headers, bytes(response_body)


def _n8n_secret():
    from .n8n_runtime import get_adapter

    return get_adapter().config.get("secret")


def _serve(light_port, domain, upstream_ready, report):
    stopped = threading.Event()

    def open_tunnel(server, port):
        # A successfully bound socket does not mean the ASGI loop is ready yet.
        for _ in range(100):
            if stopped.is_set():
                return
            if server.started:
                break
            stopped.wait(0.05)
        else:
            report("Public n8n ingress unavailable: local listener did not start.")
            return
        try:
            import ngrok

            listener = ngrok.forward(f"127.0.0.1:{port}", authtoken_from_env=True, domain=domain)
            if not stopped.is_set():
                report(f"Public n8n ingress: {listener.url()}/n8n")
                stopped.wait()
            ngrok.disconnect(listener.url())
        except Exception as error:
            report(f"Public n8n ingress unavailable: tunnel error ({type(error).__name__}).")

    try:
        if not _n8n_secret():
            report("Public n8n ingress unavailable: shared secret is not configured.")
            return
        ingress = PublicIngress(light_port, _n8n_secret, upstream_ready=upstream_ready,
                                text_timeout=os.getenv("FRIDAY_TEXT_TIMEOUT"))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                ingress, http="h11", ws="none", lifespan="off", proxy_headers=False,
                access_log=False, log_level="warning", timeout_keep_alive=5,
                timeout_graceful_shutdown=2, limit_concurrency=32, backlog=32,
            ))
            threading.Thread(target=open_tunnel, args=(server, port), daemon=True,
                             name="friday-n8n-tunnel").start()
            server.run(sockets=[listener])
    except (Exception, SystemExit) as error:
        report(f"Public n8n ingress unavailable: listener error ({type(error).__name__}).")
    finally:
        stopped.set()


def start_public_ingress(light_port, domain=None, *, upstream_ready, report=print):
    worker = threading.Thread(target=_serve, args=(light_port, domain, upstream_ready, report),
                              daemon=True, name="friday-n8n-ingress")
    worker.start()
    return worker
