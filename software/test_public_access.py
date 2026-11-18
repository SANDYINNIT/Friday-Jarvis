"""Round ZB-EXPOSE: guarding FRIDAY's routes once they are reachable online.

n8n's cloud workflow must POST to FRIDAY for notifications, so her port gets
exposed through a tunnel. A tunnel forwards THE WHOLE PORT, and this port also
serves `/chat` (a full turn with tool access), the screen-history frames and
`/open_app` - all written for a localhost-only server that trusted its caller.

The rule is self-detecting rather than a flag that could be set wrong: a request
relayed in from outside carries `X-Forwarded-For`, a local HUD call to
`127.0.0.1` does not.

Tests are hermetic: the config path is redirected to an empty temp dir so they
never depend on (or leak) the machine's real shared secret.
"""

import ast
import os
import unittest
import tempfile

from source.server.public_access import (  # noqa: E402
    arrived_through_tunnel, check_access)

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")


class Headers(dict):
    """Starlette-like case-insensitive header mapping."""

    def get(self, key, default=None):
        return dict.get(self, str(key).lower(), default)


class Request:
    def __init__(self, headers=None):
        self.headers = Headers(headers or {})


def _hermetic_environ(secret=""):
    """Config pointing at an empty path - never the machine's real secret."""
    empty = os.path.join(tempfile.mkdtemp(prefix="pa-"), "n8n.json")
    environ = {"FRIDAY_N8N_CONFIG": empty}
    environ.pop("FRIDAY_N8N_SECRET", None)
    if secret:
        environ["FRIDAY_N8N_SECRET"] = secret
    return environ


class TestLocalCallersAreUntouched(unittest.TestCase):
    """The HUD connects directly. If this broke, FRIDAY would be unusable."""

    def test_direct_local_request_is_allowed(self):
        self.assertIsNone(check_access(Request(), _hermetic_environ()))

    def test_local_request_allowed_even_with_no_secret_configured(self):
        self.assertIsNone(check_access(Request(), _hermetic_environ()))

    def test_a_token_on_a_local_request_is_still_allowed(self):
        self.assertIsNone(check_access(
            Request({"x-friday-token": "anything"}), _hermetic_environ()))

    def test_no_forwarding_header_means_not_a_tunnel(self):
        self.assertFalse(arrived_through_tunnel(Request().headers))
        self.assertFalse(arrived_through_tunnel(None))
        self.assertFalse(arrived_through_tunnel({}))


class TestTunnelCallersAreGuarded(unittest.TestCase):
    """Anything relayed in must prove who it is."""

    def test_tunnel_without_token_is_401(self):
        status, _message = check_access(
            Request({"x-forwarded-for": "1.2.3.4"}), _hermetic_environ("s3cret"))
        self.assertEqual(status, 401)

    def test_tunnel_with_wrong_token_is_401(self):
        status, _message = check_access(
            Request({"x-forwarded-for": "1.2.3.4", "x-friday-token": "nope"}),
            _hermetic_environ("s3cret"))
        self.assertEqual(status, 401)

    def test_tunnel_with_right_token_is_allowed(self):
        self.assertIsNone(check_access(
            Request({"x-forwarded-for": "1.2.3.4", "x-friday-token": "s3cret"}),
            _hermetic_environ("s3cret")))

    def test_bearer_form_is_accepted(self):
        self.assertIsNone(check_access(
            Request({"x-forwarded-for": "1.2.3.4",
                     "authorization": "Bearer s3cret"}),
            _hermetic_environ("s3cret")))

    def test_exposed_with_no_secret_anywhere_fails_closed(self):
        """Never fall back to trusting the network - same rule as POST /n8n."""
        status, message = check_access(
            Request({"x-forwarded-for": "1.2.3.4"}), _hermetic_environ())
        self.assertEqual(status, 403)
        self.assertIn("no shared secret", message)

    def test_every_standard_forwarding_header_is_detected(self):
        for header in ("x-forwarded-for", "x-forwarded-proto", "x-real-ip",
                       "cf-connecting-ip"):
            self.assertTrue(arrived_through_tunnel(Request({header: "1.2.3.4"}).headers),
                            "%s should count as forwarded" % header)

    def test_a_local_request_wearing_a_token_is_not_blocked(self):
        """A forwarded token must never be *required* of a local caller."""
        self.assertIsNone(check_access(
            Request({"x-friday-token": ""}), _hermetic_environ()))


class TestTheGuardCannotBreakARoute(unittest.TestCase):
    def test_malformed_headers_do_not_raise(self):
        class Weird:
            headers = object()          # no get(), not a mapping
        result = check_access(type("R", (), {"headers": Weird()})(),
                              _hermetic_environ("s3cret"))
        # Either it refuses (safe) or allows - but it must never raise.
        self.assertTrue(result is None or isinstance(result, tuple))

    def test_a_missing_request_attribute_does_not_raise(self):
        result = check_access(type("R", (), {})(), _hermetic_environ())
        self.assertTrue(result is None or isinstance(result, tuple))


class TestSensitiveRoutesAreActuallyGuarded(unittest.TestCase):
    """The point of the whole module: prove the four routes call it."""

    def _source(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            return handle.read()

    def _route_body(self, name):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                return ast.unparse(node)
        raise AssertionError("route %r not found" % name)

    def test_chat_is_guarded(self):
        body = self._route_body("chat")
        self.assertIn("_guard(request)", body)
        self.assertLess(body.index("_guard(request)"),
                        body.index("await request.body()"),
                        "/chat must be guarded BEFORE its body is read")

    def test_screen_history_is_guarded(self):
        self.assertIn("_guard(request)", self._route_body("screen_history"))

    def test_screen_history_stats_is_guarded(self):
        self.assertIn("_guard(request)", self._route_body("screen_history_stats"))

    def test_open_app_is_guarded(self):
        self.assertIn("_guard(request)", self._route_body("open_app"))

    def test_ping_stays_open(self):
        """Health checks (and the launcher) rely on /ping; it leaks nothing."""
        self.assertNotIn("_guard", self._route_body("ping"))

    def test_the_guard_helper_exists_and_returns_json(self):
        self.assertIn("def _guard(request)", self._source())
        self.assertIn('JSONResponse({"ok": False, "error": message}', self._source())

    def test_n8n_keeps_its_own_fail_closed_check(self):
        """/n8n is guarded separately and ALWAYS - not only when forwarded,
        because an unforwarded LAN caller must not reach it either."""
        body = self._route_body("n8n_inbound")
        self.assertIn("shared_secret", body)
        self.assertNotIn("_guard(request)", body)


class TestSecretIsUsedCorrectly(unittest.TestCase):
    def test_comparison_is_constant_time(self):
        module_source = open(
            os.path.join(os.path.dirname(SERVER_PY), "public_access.py"),
            encoding="utf-8").read()
        self.assertIn("hmac.compare_digest", module_source,
                      "the shared secret must not be compared with ==")
