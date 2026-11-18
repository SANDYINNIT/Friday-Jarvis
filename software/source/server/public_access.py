"""Guard FRIDAY's HTTP routes when they become reachable from the internet.

WHY THIS EXISTS
---------------
n8n's cloud workflow has to POST to FRIDAY for notifications, so her server gets
exposed through a tunnel. But a tunnel forwards THE WHOLE PORT, and this port
also serves:

  * POST /chat                - makes FRIDAY run a turn with full tool access
  * GET  /screen/history      - returns captured frames of Sir's screen
  * GET  /screen/history/stats
  * GET  /open_app            - raises the HUD window

`/n8n` was already fail-closed on a shared secret. The others were written when
the server was localhost-only and trusted its caller - which stops being true
the moment a tunnel points at it.

HOW IT DECIDES
--------------
The rule is self-detecting, so there is no "exposed" flag to get wrong: a
request that arrived through a tunnel carries `X-Forwarded-For` (ngrok and every
standard proxy add it), while a request from the local HUD hitting
`127.0.0.1:10101` directly does not.

    no forwarding header   -> local caller        -> allow (HUD keeps working)
    forwarding header      -> came from outside   -> require the shared secret

The secret is the same one used for `X-Friday-Token` outbound to n8n and for
`POST /n8n`, so there is exactly one credential to manage.

A caller on the same machine could omit the header to bypass this - but a
caller already on the machine has stronger options than HTTP. The threat this
guards against is the exposed tunnel, and that threat always arrives forwarded.
"""

import hmac
from urllib.parse import urlparse

__all__ = ["arrived_through_tunnel", "check_access", "status_and_message"]

# Headers every mainstream tunnel/proxy adds. Present => it did not come in
# directly from this machine's own client.
FORWARDING_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
    "cf-connecting-ip",   # Cloudflare
    "x-ngrok-skip-browser-warning",
)


def arrived_through_tunnel(headers):
    """True when the request was relayed in from outside this machine.

    `headers` is any mapping with case-insensitive-ish keys (Starlette's
    Headers qualifies; a plain dict does NOT, hence the lower() pass).
    """
    if headers is None:
        return False
    get = getattr(headers, "get", None)
    if callable(get):
        return any(get(name) for name in FORWARDING_HEADERS)
    try:
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    except Exception:
        return False
    return any(lowered.get(name) for name in FORWARDING_HEADERS)


def _presented_token(headers):
    """Read the shared secret from X-Friday-Token or `Bearer <token>`."""
    if headers is None:
        return ""
    get = getattr(headers, "get", None)
    if not callable(get):
        try:
            headers = {str(k).lower(): v for k, v in dict(headers).items()}
        except Exception:
            return ""
        get = headers.get
    token = str(get("x-friday-token") or "").strip()
    if token:
        return token
    authorization = str(get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _secret(environ):
    """The shared secret, read the same way n8n_runtime reads it."""
    environ = environ if environ is not None else {}
    raw = environ.get("FRIDAY_N8N_SECRET")
    if raw:
        return str(raw)
    try:
        from .n8n_runtime import load_config
        return str(load_config(environ if environ else None).get("secret") or "")
    except Exception:
        return ""


def check_access(request, environ=None):
    """Return None when the caller may proceed, else (status_code, message).

    Call this FIRST in a route body. It never raises.
    """
    try:
        headers = getattr(request, "headers", None)
        if not arrived_through_tunnel(headers):
            return None                     # local caller, always fine
        secret = _secret(environ)
        if not secret:
            # Exposed with no credential configured: refuse rather than trust
            # the network. Same fail-closed rule as POST /n8n.
            return 403, ("FRIDAY is reachable from the internet and no shared "
                         "secret is configured, so this route is closed. Set "
                         "FRIDAY_N8N_SECRET (or n8n.json `secret`).")
        if not _presented_token(headers):
            return 401, "missing X-Friday-Token"
        expected = secret.encode("utf-8")
        presented = _presented_token(headers).encode("utf-8")
        if not hmac.compare_digest(expected, presented):
            return 401, "invalid X-Friday-Token"
        return None
    except Exception as error:              # a guard must never break a route
        return 500, "access check failed: %s" % type(error).__name__


def status_and_message(result):
    """Turn check_access()'s result into (status, text) or (0, "") if allowed."""
    if result is None:
        return 0, ""
    status, message = result
    return int(status), str(message)
