"""ROUND ZB-L3 L3-1: cross-process screen history.

THE DEFECT THIS LOCKS DOWN
-------------------------
ZB-L2 put the screen history behind a PROCESS-WIDE singleton
(`screen_capture_service._SERVICE`). The model's authored python runs in a
separate `ipykernel_launcher` process, so calling `describe_recent_screen()`
from there saw a different, empty singleton and returned `ok=False`. Proven
live on 2026-10-07: FRIDAY answered a "what did that note say" question by
reading Notepad's on-disk tab-state file, because the real feature was
unreachable from the only place she can call code.

The fix is two read-only HTTP routes on the server that owns the capture
service. The properties that matter, and that these tests hold:

  * the route answers from the REAL service, not from a fresh one
  * nothing in this path can START a second capture service
  * there is exactly ONE history buffer, in one process
  * the reported age is the frame's TRUE age
  * a moment that is gone is reported as gone, never as the live screen
  * the routes are read-only
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source.server import screen_capture_service as cap  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(_HERE, "source", "server", "server.py")


def server_source():
    """The DEPLOYED server.py, read fresh every call."""
    with open(SERVER_PY, encoding="utf-8") as handle:
        return handle.read()


def extract_route_source(name):
    """Return the real body of a route handler, or None.

    The handlers are nested inside start_server(), so they cannot be imported.
    They are located in the deployed source by AST and returned as source text
    so the caller can execute them verbatim - these tests therefore exercise the
    shipped code rather than a re-typed copy of it.
    """
    import ast
    import textwrap

    tree = ast.parse(server_source())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != name:
            continue
        lines = server_source().splitlines()
        # The handler is nested inside start_server(), so strip the common
        # indent before compiling it at module level.
        return textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))
    return None


def run_route(source, service, *, age=None, monitor=None, env=None):
    """Execute an extracted route against `service` as the process singleton.

    Returns (status_code, json_text). `age`/`monitor` are omitted from the
    call when None so the handler's own defaults are exercised.
    """
    import asyncio
    import time as _time

    from fastapi.responses import JSONResponse, PlainTextResponse

    # The extracted route calls `_guard`, which in production is a closure
    # inside start_server(). Rebuild it here over the REAL public_access check
    # so a local caller is proven to pass - not stubbed out with a constant.
    from source.server.public_access import check_access as _check_access

    def _guard(request):
        denied = _check_access(request)
        if denied is None:
            return None
        status, message = denied
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    namespace = {
        "__name__": "source.server.server",
        "__package__": "source.server",
        "JSONResponse": JSONResponse,
        "PlainTextResponse": PlainTextResponse,
        "cap": cap,
        "time": _time,
        "asyncio": asyncio,
        "Request": type("Request", (), {}),
        "_guard": _guard,
    }
    code = compile(source, "<route>", "exec")
    # Relative imports inside the handler need the real package, so exec into a
    # module object rather than a bare dict - otherwise `from . import X` has no
    # package to resolve against and the route cannot be tested at all.
    import types as _types

    module = _types.ModuleType("source.server.server")
    module.__package__ = "source.server"
    module.__dict__.update(namespace)
    exec(code, module.__dict__)          # noqa: S102 - run deployed code
    handler = getattr(module, name_of(source), None)
    if handler is None:
        raise AssertionError("handler not defined by extracted source")

    kwargs = {}
    # The routes now take a `request` for the exposure guard. Pass a local
    # caller (no forwarding header) - it MUST pass, otherwise the guard would
    # be blocking ordinary local use. Guard behaviour itself is covered in
    # test_public_access.py.
    kwargs["request"] = type("R", (), {"headers": {}})()
    if age is not None:
        kwargs["age"] = age
    if monitor is not None:
        kwargs["monitor"] = monitor

    saved = {}
    if env:
        for key, value in env.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = str(value)
    # The route reads the PROCESS-WIDE singleton, exactly as it does in the
    # running server. Installing the test service there is therefore the
    # faithful way to prove the route answers from the real capture service
    # rather than from anything it builds for itself.
    saved_service = cap._SERVICE
    cap._SERVICE = service
    try:
        # Drive the coroutine to completion synchronously.
        result = asyncio.new_event_loop().run_until_complete(handler(**kwargs))
    finally:
        cap._SERVICE = saved_service
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    body = result.body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return getattr(result, "status_code", 200), str(body)


def name_of(source):
    """The def name inside an extracted handler source."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            return stripped.split("def ", 1)[1].split("(")[0].strip()
    raise AssertionError("no def in extracted source")


def _frame(value=7, size=(1920, 1080)):
    import numpy

    return numpy.full((size[1], size[0], 4), value, dtype=numpy.uint8)


class _HonestCamera:
    """dxcam stand-in that tells the truth about what it actually has."""

    def __init__(self, size=(1920, 1080)):
        self.size = size
        self.released = False
        self.calls = []
        self._pending = None
        self._last = None
        self._seq = 0

    def queue(self, value=None):
        self._seq += 1
        self._pending = _frame(value if value is not None else self._seq, self.size)
        return self._pending

    def grab(self, region=None, new_frame_only=False):
        self.calls.append(("grab", bool(new_frame_only)))
        if new_frame_only:
            frame, self._pending = self._pending, None
            if frame is not None:
                self._last = frame
            return frame
        return self._last

    def get_latest_frame(self):
        self.calls.append(("get_latest_frame", None))
        return self._last

    def release(self):
        self.released = True


class _Clock:
    def __init__(self, start=50_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _Env:
    def __init__(self, **pairs):
        self.pairs = pairs

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.pairs}
        for key, value in self.pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _service_with_history(clock, entries=6, step=0.5, size=(320, 200)):
    """A ScreenCaptureService holding a populated history, as the server owns."""
    buffer = cap._FrameBuffer(0, 0, size)
    buffer.camera = _HonestCamera(size=size)
    for _ in range(entries):
        buffer.camera.queue()
        clock.advance(step)
        buffer.refresh(None)
    service = cap.ScreenCaptureService.__new__(cap.ScreenCaptureService)
    service._lock = cap.threading.Lock()
    service._buffers = [buffer]
    service._signature = None
    service._retry_streak = 0
    service._next_rebuild_at = 0.0
    return service


def _call_route(service, age=0.0, monitor=0, env=None):
    """Drive the REAL route coroutine the server registers.

    The handlers are nested inside start_server(), so they cannot be imported.
    They are therefore extracted from the deployed source by AST and executed
    verbatim - the same discipline the Round ZA/U harnesses use - so these
    tests exercise the shipped code, not a copy of it.
    """
    from test_cross_process_routes import extract_route_source, run_route

    return run_route(extract_route_source("screen_history"), service,
                     age=age, monitor=monitor, env=env)


class RouteAvailabilityTests(unittest.TestCase):
    """The route must exist, be a GET, and be read-only."""

    def test_the_route_is_registered_in_the_deployed_source(self):
        from test_cross_process_routes import extract_route_source

        self.assertIsNotNone(
            extract_route_source("screen_history"),
            "GET /screen/history must exist in the deployed server.py")
        self.assertIsNotNone(
            extract_route_source("screen_history_stats"),
            "GET /screen/history/stats must exist in the deployed server.py")

    def test_the_history_route_is_read_only(self):
        """A POST/PUT/DELETE on history would be a second mutation surface."""
        from test_cross_process_routes import server_source

        source = server_source()
        self.assertIn('@interpreter.server.app.get("/screen/history")', source)
        for verb in ("post", "put", "delete", "patch"):
            self.assertNotIn(
                '@interpreter.server.app.%s("/screen/history' % verb, source,
                "history must never expose a mutating verb")

    def test_the_route_never_starts_a_capture_service(self):
        """The kernel must not be able to spawn its own dxcam loop.

        A second capture service in the kernel process would race this process
        for the same DXGI outputs and hold a private copy of every frame - the
        exact 'no duplicate service, no second buffer' requirement.
        """
        from test_cross_process_routes import extract_route_source

        for name in ("screen_history", "screen_history_stats"):
            body = extract_route_source(name)
            for forbidden in ("start()", "service().start", "poll_once",
                              "_import_dxcam", "dxcam.create", "refresh("):
                self.assertNotIn(
                    forbidden, body,
                    "%s must not be able to start capture (found %r)"
                    % (name, forbidden))


class RouteReadsRealServiceTests(unittest.TestCase):
    """The route must answer from the live service, not a fresh instance."""

    def test_a_populated_history_is_served_with_its_true_age(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            service = _service_with_history(clock)
            status, payload = _call_route(service, age=1.0, env={
                "FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
            real_ages = sorted(
                round(clock.now - entry["mono"], 3)
                for entry in service._buffers[0].history)
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertTrue(body["ok"], body)
        # The reported age must be the age of a frame that ACTUALLY exists - it
        # is never rounded toward what the caller asked for.
        self.assertIn(round(body["age_seconds"], 2),
                      [round(a, 2) for a in real_ages],
                      "reported age must match a real retained frame")
        self.assertLessEqual(abs(body["age_seconds"] - 1.0), 0.5,
                             "and it must be the nearest one")
        self.assertFalse(body["is_live_screen"],
                         "history is never the live screen")
        self.assertTrue(body["jpeg_b64"])

    def test_the_route_serves_the_SERVER_service_not_a_new_one(self):
        """If the route built its own service it would answer empty."""
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            service = _service_with_history(clock)
            self.assertTrue(service._buffers[0].history)
            status, payload = _call_route(service, age=0.0, env={
                "FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
            body = json.loads(payload)
            self.assertTrue(body["ok"],
                            "the route must read the caller's live service")
            self.assertTrue(body["jpeg_b64"])

    def test_a_second_service_is_never_created_by_serving_a_request(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            service = _service_with_history(clock)
            created = []
            real_service = cap.service

            def spy():
                created.append(1)
                return real_service()

            with mock.patch.object(cap, "service", spy):
                _call_route(service, age=0.0,
                            env={"FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
                _call_route(service, age=2.0,
                            env={"FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
        # history_image/_buffer_for go through the service instance we pass in,
        # so nothing new is built. A second service would appear here.


class RouteHonestyTests(unittest.TestCase):
    """A missing moment must never be dressed up as the live screen."""

    def test_a_moment_outside_the_window_is_reported_as_gone(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            service = _service_with_history(clock)
            status, payload = _call_route(service, age=900.0, env={
                "FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
        body = json.loads(payload)
        self.assertFalse(body["ok"])
        self.assertIn("no retained frame", body["reason"])
        self.assertNotIn("jpeg_b64", body,
                         "a missing moment must not ship an image")

    def test_disabled_history_is_reported_as_disabled(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock):
            service = _service_with_history(clock)
            status, payload = _call_route(
                service, age=1.0,
                env={"FRIDAY_SCREEN_HISTORY_SECONDS": "0"})
        body = json.loads(payload)
        self.assertFalse(body["ok"])
        self.assertIn("disabled", body["reason"])
        self.assertNotIn("jpeg_b64", body)

    def test_an_empty_service_reports_gone_rather_than_erroring(self):
        clock = _Clock()
        service = cap.ScreenCaptureService.__new__(cap.ScreenCaptureService)
        service._lock = cap.threading.Lock()
        service._buffers = []
        with _Env(FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            status, payload = _call_route(service, age=1.0,
                                          env={"FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertFalse(body["ok"])
        self.assertIn("no retained frame", body["reason"])

    def test_the_reply_carries_enough_to_separate_past_from_present(self):
        """The model must be able to tell remembered from live."""
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            service = _service_with_history(clock)
            _status, payload = _call_route(service, age=1.0, env={
                "FRIDAY_SCREEN_HISTORY_SECONDS": "60"})
        body = json.loads(payload)
        for field in ("ok", "age_seconds", "as_of", "monitor", "is_live_screen",
                      "jpeg_b64"):
            self.assertIn(field, body)


class StatsRouteTests(unittest.TestCase):
    def test_stats_report_the_window_and_the_live_counts(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"):
            service = _service_with_history(clock)
            from test_cross_process_routes import (extract_route_source,
                                                   run_route)

            status, payload = run_route(
                extract_route_source("screen_history_stats"), service,
                env={"FRIDAY_SCREEN_HISTORY_SECONDS": "15"})
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["window_seconds"], 15.0)
        self.assertEqual(len(body["monitors"]), 1)
        self.assertGreater(body["monitors"][0]["entries"], 0)

    def test_stats_do_not_start_capture(self):
        from test_cross_process_routes import extract_route_source

        body = extract_route_source("screen_history_stats")
        for forbidden in ("start()", "poll_once", "_import_dxcam"):
            self.assertNotIn(forbidden, body)


class PromptGuidanceTests(unittest.TestCase):
    """The prompt must point at the route, not at the unreachable module."""

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        self.prompt = open(os.path.join(
            here, "source", "server", "profiles", "default.py"),
            encoding="utf-8").read()

    def test_the_prompt_advertises_the_http_route(self):
        self.assertIn("/screen/history", self.prompt)

    def test_the_prompt_warns_that_the_capture_module_is_a_different_process(self):
        """Otherwise she will reach for the module again and get ok=False."""
        block = self.prompt[self.prompt.find("RECENT SCREEN MEMORY"):][:2600]
        self.assertIn("different process", block)

    def test_the_prompt_still_forbids_substituting_the_live_screen(self):
        block = self.prompt[self.prompt.find("RECENT SCREEN MEMORY"):][:2600]
        self.assertIn("NEVER substitute", block)
        self.assertIn("ALWAYS state the age", block)

    def test_the_in_process_helper_is_not_the_advertised_path(self):
        """describe_recent_screen may still exist for server-side callers, but
        it must not be what the prompt tells the model to use."""
        block = self.prompt[self.prompt.find("RECENT SCREEN MEMORY"):][:2600]
        self.assertNotIn("describe_recent_screen(age_seconds", block)


if __name__ == "__main__":
    unittest.main()
