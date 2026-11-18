"""Round ZB-N8N: the INBOUND endpoint (n8n -> FRIDAY).

`n8n_runtime.py` already covered the OUTBOUND direction (FRIDAY -> n8n named
webhooks, with fallback to her own resources) and has its own suite. This file
covers what was missing: n8n POSTing a notice to FRIDAY so it runs as a real
turn, and the security around that.

TRUST MODEL
-----------
`/n8n` can make FRIDAY act, so it FAILS CLOSED:
  * no shared secret configured -> 403 for everyone (not "allow, it's local")
  * wrong or missing token     -> 401, constant-time comparison
  * oversized body             -> 413, so a hostile client cannot pin memory

The secret is the SAME one `n8n_runtime` already sends as `X-Friday-Token` when
FRIDAY calls n8n, so there is one secret in both directions.
"""

import ast
import os
import tempfile
import time

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
with open(SERVER_PY, encoding="utf-8") as _fh:
    SOURCE = _fh.read()
TREE = ast.parse(SOURCE)


def _find_async(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError("could not find async def %r" % name)


class _FakeResponse:
    def __init__(self, payload, status=200, **kwargs):
        # FastAPI's JSONResponse takes `status_code`; PlainTextResponse passes
        # status too. Accept both spellings.
        self._payload = payload
        self.status_code = int(kwargs.get("status_code") or status)

    def json(self):
        return self._payload


class _FakeRequest:
    def __init__(self, body="", headers=None):
        self._body = body.encode("utf-8")
        self.headers = headers or {}

    async def body(self):
        return self._body


def _load_endpoint(secret="s3cret", *, dispatch_result=None, reply_text="hello"):
    """Compile the REAL `n8n_inbound` body into a callable, with stubs for the
    server-local names it closes over. Returns (callable, namespace, calls)."""
    import textwrap

    fn = _find_async("n8n_inbound")
    # The real function is nested inside start_server, so its statements unparse
    # with 8-space indentation. Our wrapper is at column 0: strip and re-indent.
    body = textwrap.indent(
        textwrap.dedent("\n".join(ast.unparse(s) for s in fn.body)), "    ")

    calls = {"dispatch": [], "sweeps": []}

    class _Recorder:
        pass

    async def _fake_dispatch(text, reply_channel=None, reply_token=None, source=None):
        calls["dispatch"].append((text, reply_channel, reply_token, source))
        return {"accepted": True}

    class _Waiter:
        def __init__(self, text):
            self._text = text
            self._used = False

        async def get(self, timeout=None):
            if self._used:
                raise _Timeout()
            self._used = True
            return self._text

        def empty(self):
            return True

    class _Timeout(__import__("asyncio").TimeoutError):
        """Matches what asyncio.wait_for raises, so the real drain loop's
        handler catches it and terminates cleanly."""
        pass

    class _Registry:
        def __init__(self):
            self.items = []

        def register(self, request):
            self.items.append(request)
            return request

        def sweep(self):
            calls["sweeps"].append(True)
            return 0

    from source.server.chat_lifecycle import ChatRequest, LifecycleRegistry
    from source.server.server import (
        LOCAL_OWNERSHIP, TEXT_CHANNEL_CHAT_ID, _close_reply_box,
        _install_ownership, _new_reply_token, _ownership)

    registry = _Registry()
    server_state = {
        "chat_requests": registry,
        # The endpoint looks the waiter up by the token _new_reply_token()
        # returns ("tok-1"), so pre-seed that box with one chunk of text.
        "reply_waiters": {"tok-1": _Waiter(reply_text)},
        # NB: no "turn_lock" key - the endpoint uses setdefault(), which only
        # fills a MISSING key, so seeding it with None would leave it None.
        "is_paused": False,
    }

    # ROUND ZB-PE: the parked-question branch closes over `pending_store` and
    # `_pending_worker`, both defined inside start_server. Supply a REAL store
    # (on a temp path) and a recording worker so the parking path is exercised
    # with production logic, not a stub that always agrees.
    from source.server.pending_requests import PendingStore

    _tmp = tempfile.mkdtemp(prefix="n8n-pending-")
    _store_path = os.path.join(_tmp, "pending.json")
    _pending_store = PendingStore(
        path=_store_path, environ={"FRIDAY_PENDING_PATH": _store_path})
    _workers = []

    async def _pending_worker(correlation_id):
        _workers.append(correlation_id)

    namespace = {
        "request": None,                     # filled per call
        "server_state": server_state,
        "JSONResponse": _FakeResponse,
        "PlainTextResponse": lambda *a, **k: _FakeResponse({}, 200),
        "asyncio": __import__("asyncio"),
        "os": os,
        "json": __import__("json"),
        "time": time,
        "text_in": "", "kind": "", "payload": {},
        "reply_token": None, "chat_request": None, "chat_requests": registry,
        "turn_lock": None, "waiter": None, "parts": None, "deadline": None,
        "remaining": None, "reply": None, "result": None, "parsed": None,
        "_key": "", "value": None, "timeout": 30.0,
        # the real helpers
        "ChatRequest": ChatRequest,
        "LifecycleRegistry": LifecycleRegistry,
        "_new_reply_token": lambda state: "tok-1",
        "_close_reply_box": lambda state, token: None,
        "_install_ownership": _install_ownership,
        "_ownership": _ownership,
        "LOCAL_OWNERSHIP": LOCAL_OWNERSHIP,
        "TEXT_CHANNEL_CHAT_ID": TEXT_CHANNEL_CHAT_ID,
        "dispatch_text": _fake_dispatch,
        # start_server supplies this optional, inert initiative sink. The
        # AST-extracted endpoint must retain it without creating a live profile.
        "personality": __import__("types").SimpleNamespace(suggest=lambda *a, **k: calls.setdefault("suggestions", []).append(a)),
        "on_tts_chunk": None,
        # ROUND ZB-PE
        "pending_store": _pending_store,
        "_pending_worker": _pending_worker,
        "workers": _workers,
    }

    # Replace the adapter lookup and auth with controllable stubs, keeping the
    # REAL control flow (fail-closed, constant-time compare, size limit).
    wrapper = (
        "import hmac as _hmac\n"
        "class _Adapter:\n"
        "    config = {'secret': " + repr(secret) + "}\n"
        "def _n8n_adapter():\n"
        "    return _Adapter()\n"
        "async def _endpoint(request):\n"
        + body + "\n"
    )
    namespace["_hmac"] = __import__("hmac")
    exec(compile(wrapper, "<n8n_inbound>", "exec"), namespace)
    return namespace["_endpoint"], namespace, calls


class TestFailClosed:
    def test_no_secret_configured_refuses_everyone(self):
        endpoint, namespace, _ = _load_endpoint(secret="")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"hi"}', {"x-friday-token": "anything"})))
        assert response.status_code == 403
        assert "not configured" in str(response.json()["error"])

    def test_wrong_token_is_401(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"hi"}', {"x-friday-token": "wrong"})))
        assert response.status_code == 401

    def test_missing_token_is_401(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest('{"text":"hi"}')))
        assert response.status_code == 401

    def test_the_token_is_never_echoed_back(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"hi"}', {"x-friday-token": "right"})))
        assert "right" not in str(response.json())

    def test_the_endpoint_refuses_before_dispatching(self):
        endpoint, namespace, calls = _load_endpoint(secret="right")
        namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"hi"}', {"x-friday-token": "wrong"})))
        assert calls["dispatch"] == [], "an unauthenticated request was dispatched"


class TestAcceptedRequests:
    def test_bearer_header_is_also_accepted(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"hi"}',
                         {"authorization": "Bearer right"})))
        assert response.status_code == 200

    def test_oversized_body_is_refused(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest("x" * 70000, {"x-friday-token": "right"})))
        assert response.status_code == 413

    def test_empty_body_is_400(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest("", {"x-friday-token": "right"})))
        assert response.status_code == 400

    def test_plain_text_body_is_accepted(self):
        endpoint, namespace, calls = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest("a new email arrived", {"x-friday-token": "right"})))
        assert response.status_code == 200
        sent = calls["dispatch"][0][0]
        # The notice keeps its original text...
        assert sent.endswith("a new email arrived"), sent
        # ...with a provenance prefix, so FRIDAY can tell an incoming workflow
        # notice from her own task and judge whether a reply is warranted.
        assert sent.startswith("[incoming from n8n workflow, kind=custom]"), sent
        assert calls["dispatch"][0][1] == "text"
        assert calls["dispatch"][0][3] == "n8n"

    def test_kind_and_payload_are_parsed_and_echoed(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        body = ('{"text":"new email", "kind":"email_notice", '
                '"payload":{"from":"boss@example.com"}}')
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest(body, {"x-friday-token": "right"})))
        payload = response.json()
        assert payload["kind"] == "email_notice"
        assert payload["payload"]["from"] == "boss@example.com"


class TestReplySemantics:
    """Deliberate silence must not look like a failure to the workflow.

    FRIDAY reads an incoming notice, judges it not worth answering, and says
    nothing. That is a SUCCESSFUL turn - reporting ok=False would tell n8n its
    push failed when it did not. `replied` is what separates the two.
    """

    def test_a_reply_reports_ok_and_replied_true(self):
        endpoint, namespace, _ = _load_endpoint(secret="right", reply_text="Yes, go ahead.")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"n"}', {"x-friday-token": "right"})))
        payload = response.json()
        assert payload["ok"] is True
        assert payload["replied"] is True
        assert payload["reply"] == "Yes, go ahead."
        assert payload["state"] == "completed"

    def test_silence_reports_ok_but_replied_false(self):
        """The judge decided there was nothing to say - not a failure."""
        endpoint, namespace, _ = _load_endpoint(secret="right", reply_text="")
        response = namespace["asyncio"].run(endpoint(
            _FakeRequest('{"text":"n"}', {"x-friday-token": "right"})))
        payload = response.json()
        assert payload["ok"] is True, "deliberate silence must not read as failure"
        assert payload["replied"] is False
        assert payload["reply"] == ""
        assert payload["state"] == "completed"


class TestParkingOwnerQuestions:
    """A question only Sir can answer must be PARKED, not answered.

    The bug being fixed: n8n asked "shall I schedule the call?" and FRIDAY
    returned that question to n8n as her reply - handing back what the workflow
    already knew and telling it nothing. The workflow must get a correlation id
    and an acknowledgement, not its own question.
    """

    def test_an_explicit_owner_question_is_parked_not_answered(self):
        endpoint, namespace, calls = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest(
            '{"text":"Señor, le ha llegado un correo. ¿Le agenda la llamada?",'
            ' "kind":"email_question", "needs_owner":true}',
            {"x-friday-token": "right"})))
        payload = response.json()
        assert response.status_code == 200
        assert payload["ok"] is True
        assert payload["state"] == "pending", payload
        assert payload["correlation_id"].startswith("pr-")
        assert calls["dispatch"] == [], (
            "the question was dispatched as a normal turn - it must be parked")
        assert namespace["workers"] == [payload["correlation_id"]], (
            "the background worker was not started for the parked question")

    def test_the_acknowledgement_is_not_the_question(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest(
            '{"text":"Should I reply to him?", "kind":"email_question"}',
            {"x-friday-token": "right"})))
        reply = response.json()["reply"]
        assert "Should I reply to him?" not in reply, (
            "the workflow was handed its own question back: %r" % reply)

    def test_the_parked_question_is_in_the_store(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest(
            '{"text":"¿Le respondo?", "kind":"email_question"}',
            {"x-friday-token": "right"})))
        correlation_id = response.json()["correlation_id"]
        store = namespace["pending_store"]
        stored = store.get(correlation_id)
        assert stored is not None
        assert stored.text == "¿Le respondo?"
        assert stored.state == "open"

    def test_a_needs_owner_flag_alone_is_enough(self):
        endpoint, namespace, _ = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest(
            '{"text":"New email: call him back?","payload":{"needs_owner":true}}',
            {"x-friday-token": "right"})))
        assert response.json()["state"] == "pending"

    def test_a_plain_notice_keeps_the_old_behaviour(self):
        """Compatibility: without an owner marker, nothing about /n8n changes."""
        endpoint, namespace, calls = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest(
            '{"text":"a new email arrived"}', {"x-friday-token": "right"})))
        assert response.json()["state"] == "completed"
        assert calls["dispatch"], "a plain notice must still run a turn"
        assert namespace["workers"] == [], "a plain notice must not be parked"

    def test_parking_still_requires_the_token(self):
        endpoint, namespace, calls = _load_endpoint(secret="right")
        response = namespace["asyncio"].run(endpoint(_FakeRequest(
            '{"text":"question?","kind":"email_question"}')))
        assert response.status_code == 401
        assert namespace["workers"] == []
        assert len(namespace["pending_store"]) == 0


class TestPollingEndpoints:
    """n8n learns the outcome by polling - no callback required."""

    def _source(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            return handle.read()

    def test_both_polling_routes_exist(self):
        source = self._source()
        assert '@interpreter.server.app.get("/n8n/pending")' in source
        assert '@interpreter.server.app.get("/n8n/pending/{correlation_id}")' in source

    def test_polling_routes_are_guarded(self):
        """They expose email content, so they are guarded ALWAYS - not only
        when the request arrives through a tunnel."""
        source = self._source()
        for route in ("n8n_pending_list", "n8n_pending_one"):
            tree = ast.parse(source)
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name == route)
            body = ast.unparse(node)
            assert "_require_n8n_token(request)" in body, route
            assert body.index("_require_n8n_token(request)") < body.index("pending_store")

    def test_the_guard_is_shared_with_n8n_inbound(self):
        source = self._source()
        assert "def _require_n8n_token(request)" in source
        tree = ast.parse(source)
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "n8n_inbound")
        assert "_hmac.compare_digest" in ast.unparse(node)

    def test_a_parked_view_exposes_the_correlation_id(self):
        from source.server.pending_requests import PendingRequest
        request = PendingRequest("call him?")
        view = request.public_view()
        assert view["correlation_id"] == request.id
        assert view["state"] == "open"


class TestRouteIsRegistered:
    def test_the_route_exists_and_is_a_post(self):
        assert '@interpreter.server.app.post("/n8n")' in SOURCE

    def test_it_runs_a_real_turn_not_a_deterministic_handler(self):
        # The whole point: n8n notices go through dispatch_text, so memory,
        # routing, ownership and the lifecycle all apply - not a canned reply.
        fn = _find_async("n8n_inbound")
        body = ast.unparse(fn)
        assert "dispatch_text" in body
        assert "chat_request" in body, "no lifecycle ownership on the inbound turn"
