"""Optional n8n workflow automation, with FRIDAY's own resources as fallback.

n8n (https://n8n.io) is an external workflow-automation server. FRIDAY treats
it as OPTIONAL, exactly like the cloud model pools and Home Assistant:

  * If an n8n webhook is configured and answers  -> FRIDAY DELEGATES the
    workflow to n8n and reports the result it returns.
  * If n8n is not configured, or is down, or the webhook fails
    -> `available()` is False, `call()` returns a truthful "not available"
    result, and FRIDAY does the job with her OWN built-in resources
    (reminders, the calendar store, the local brain, her own code).

Nothing here is auto-fired by phrase matching. The brain is told about these
functions in the live system prompt (profiles/default.py) and CHOOSES them when
a request genuinely maps to a workflow; otherwise it keeps using her own tools.

WHERE TO CONNECT IT (nothing is wired up by default)
-----------------------------------------------------
Create a file  %USERPROFILE%\\.friday\\n8n.json :

    {
      "base_url": "http://localhost:5678",
      "timeout_seconds": 20,
      "workflows": {
        "morning_briefing": {
          "webhook_url": "http://localhost:5678/webhook/friday-morning",
          "description": "Opens the work environment and reports the plan"
        },
        "supplier_invoice_query": {
          "webhook_url": "http://localhost:5678/webhook/friday-supplier",
          "description": "Emails a supplier, logs the reply, notifies Sir"
        }
      }
    }

In n8n: add a **Webhook** node (path e.g. `friday-morning`), copy its
production URL (`.../webhook/<path>`), activate the workflow, and paste that URL
above. A shared secret can be added with `X-Friday-Token` + `secret`.

The same thing can be set with environment variables instead of the file:
`FRIDAY_N8N` (master switch, must be "1"), `FRIDAY_N8N_BASE_URL`,
`FRIDAY_N8N_API_KEY` (optional, for the REST API), and
`FRIDAY_N8N_WEBHOOK_<NAME>` per workflow.

Why webhook-first: n8n's own guidance is that the Webhook trigger is the right
abstraction for ~90% of external triggers because it accepts a custom JSON
payload. The REST API (`POST /api/v1/workflows/{id}/execute`) is newer, needs an
API key, and does not carry per-call payloads. An n8n **MCP Server Trigger** is a
documented future upgrade path if n8n should expose many tools instead.

Design rules followed from home_assistant.py / mcp_runtime.py:
  - two independent gates: explicit opt-in AND a configured base URL
  - absent config = disabled, never an error
  - public methods never raise; they return {"ok": bool, ...}
  - every request is bounded by a clamped timeout
  - the webhook secret is redacted out of every error string
  - every call is written to the audit log
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse

import requests

try:
    from .audit import log_action
except Exception:  # pragma: no cover - audit is optional
    def log_action(**_kwargs):
        return None

DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".friday", "n8n.json")
DEFAULT_BASE_URL = "http://localhost:5678"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_RESPONSE_CHARS = 20_000
PROBE_TIMEOUT_SECONDS = 3.0
STATE_TTL_SECONDS = 60.0
MAX_WORKFLOWS = 50

try:
    from .redaction import redact_secret_values
except Exception:  # pragma: no cover
    def redact_secret_values(payload):
        return payload


def _bounded_timeout(value, default=DEFAULT_TIMEOUT_SECONDS) -> float:
    try:
        return max(0.5, min(float(value), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return default


def _redact(text, secret) -> str:
    text = str(text or "")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text[:500]


def _valid_url(url) -> bool:
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _unwrap_n8n(payload):
    """n8n wraps webhook responses as [{"json": {...}}] - unwrap to the payload."""
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        inner = payload[0]
        if set(inner.keys()) == {"json"} and isinstance(inner["json"], (dict, list)):
            return inner["json"]
    return payload


def load_config(environ=None, path=None) -> dict:
    """Read the n8n config. Missing/invalid file simply means "not connected".

    `environ` is injectable so tests never touch the real machine config.
    """
    environ = os.environ if environ is None else environ
    config_path = path or environ.get("FRIDAY_N8N_CONFIG") or DEFAULT_CONFIG_PATH

    file_config = {}
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as handle:
                file_config = json.load(handle)
            if not isinstance(file_config, dict):
                file_config = {}
    except Exception:
        file_config = {}

    base_url = (file_config.get("base_url")
                or environ.get("FRIDAY_N8N_BASE_URL")
                or DEFAULT_BASE_URL).rstrip("/")
    api_key = file_config.get("api_key") or environ.get("FRIDAY_N8N_API_KEY") or ""
    secret = file_config.get("secret") or environ.get("FRIDAY_N8N_SECRET") or ""
    timeout = _bounded_timeout(file_config.get("timeout_seconds",
                                              environ.get("FRIDAY_N8N_TIMEOUT")))

    workflows = {}
    raw_workflows = file_config.get("workflows")
    if isinstance(raw_workflows, dict):
        workflows.update(raw_workflows)
    for name, url in environ.items():
        if name.startswith("FRIDAY_N8N_WEBHOOK_"):
            key = name[len("FRIDAY_N8N_WEBHOOK_"):].strip().lower()
            if key and url.strip():
                workflows.setdefault(key, {})["webhook_url"] = url.strip()

    clean = {}
    for name, spec in list(workflows.items())[:MAX_WORKFLOWS]:
        if isinstance(spec, str):
            spec = {"webhook_url": spec}
        if not isinstance(spec, dict):
            continue
        url = str(spec.get("webhook_url") or "").strip()
        if not url or not _valid_url(url):
            continue
        clean[str(name).strip().lower()] = {
            "webhook_url": url,
            "description": str(spec.get("description") or ""),
        }

    enabled = environ.get("FRIDAY_N8N", "1" if clean else "0").strip() == "1"
    configured = bool(clean) and _valid_url(base_url)

    return {
        "enabled": bool(enabled and configured),
        "base_url": base_url if (enabled and configured) else "",
        "api_key": api_key,
        "secret": secret,
        "timeout": timeout,
        "workflows": clean,
        "config_path": config_path,
        "reason": ("" if (enabled and configured)
                   else ("n8n is not configured" if not clean
                         else "n8n integration is disabled (FRIDAY_N8N=0)")),
    }


class N8NAdapter:
    """Opt-in n8n client. One instance per process; safe to share."""

    def __init__(self, config=None, http_post=None, http_get=None):
        self.config = config if config is not None else load_config()
        self._post = http_post or requests.post
        self._get = http_get or requests.get
        self._started = False
        self._last_error = ""
        self._last_success_at = 0.0
        self._last_workflow = ""
        self._calls = 0
        self._failures = 0
        # Reachability cache: the HUD and the brain both ask "is n8n up?" and
        # an uncached probe would fire two live HTTP requests every time.
        self._cache = {"at": 0.0, "connected": False, "reason": ""}

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> bool:
        """Idempotent; never raises. False means "FRIDAY must use her own tools"."""
        if self._started:
            return self.available()["connected"]
        self._started = True
        if not self.config["enabled"]:
            self._last_error = self.config["reason"] or "n8n is not configured"
            return False
        return True

    def available(self, force: bool = False) -> dict:
        """Is n8n actually reachable right now? Cached for STATE_TTL_SECONDS.

        A fresh probe is a pair of real HTTP requests, so the answer is reused
        for a minute: the HUD polls on a timer and the brain may ask several
        times in one turn. `force=True` bypasses the cache (used after a call
        fails, when the state may genuinely have changed).
        """
        if not self.config["enabled"]:
            return {"connected": False, "reason": self.config["reason"] or "n8n is not configured",
                    "workflow_count": 0, "last_error": self._last_error}
        base = self.config["base_url"]
        if not force and (time.monotonic() - self._cache["at"]) < STATE_TTL_SECONDS:
            ok = self._cache["connected"]
            detail = self._cache["reason"]
        else:
            ok, detail = self._probe(base)
            self._cache = {"at": time.monotonic(), "connected": ok, "reason": detail}
        return {
            "connected": ok,
            "reason": "" if ok else (self._last_error or detail or "n8n did not answer"),
            "base_url": base,
            "workflow_count": len(self.config["workflows"]),
            "last_workflow": self._last_workflow,
            "calls": self._calls,
            "failures": self._failures,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
        }

    def _probe(self, base_url) -> tuple:
        try:
            response = self._get(f"{base_url}/healthz", timeout=PROBE_TIMEOUT_SECONDS)
            if response.status_code < 500:
                return True, ""
        except Exception:
            pass
        # /healthz can be disabled; a reachable UI is still a working n8n.
        try:
            response = self._get(base_url, timeout=PROBE_TIMEOUT_SECONDS)
            return response.status_code < 500, f"HTTP {response.status_code}"
        except Exception as error:
            return False, _redact(f"{type(error).__name__}: {error}", self.config["secret"])

    def status(self) -> dict:
        """Non-secret snapshot for logs and the HUD. Never contains the secret."""
        snap = {
            "enabled": self.config["enabled"],
            "url": self.config["base_url"] if self.config["enabled"] else "",
            "workflow_count": len(self.config["workflows"]),
            "workflows": sorted(self.config["workflows"]),
            "config_path": self.config["config_path"],
            "reason": self.config["reason"],
            "last_workflow": self._last_workflow,
            "last_error": self._last_error,
        }
        if self.config["enabled"]:
            snap["available"] = self.available()
        return snap

    def connection_help(self) -> str:
        """Exactly where to connect n8n - shown when it is not connected."""
        lines = [
            "n8n is NOT connected, so I am using my own built-in resources instead.",
            "To connect n8n later:",
            f"  1. Create the file {self.config['config_path']} with:",
            '       {"base_url": "http://localhost:5678",',
            '        "workflows": {"<name>": {"webhook_url": "http://localhost:5678/webhook/<path>",',
            '                                      "description": "what this workflow does"}}}',
            "  2. In n8n add a Webhook node (path <path>), copy its production URL,",
            "     activate the workflow, and paste that URL into the file above.",
            "  3. Restart FRIDAY. I detect it automatically and hand work over to n8n.",
            "Environment variables work too: FRIDAY_N8N=1, FRIDAY_N8N_BASE_URL,",
            "FRIDAY_N8N_WEBHOOK_<NAME>.",
        ]
        return "\n".join(lines)

    # -- calling ------------------------------------------------------------ #
    def call(self, workflow: str, data=None, timeout=None) -> dict:
        """Run one n8n workflow by name. Never raises.

        {"ok": True,  "workflow": name, "result": <n8n payload>}
        {"ok": False, "error": ..., "fallback": "use FRIDAY's own resources"}
        """
        name = str(workflow or "").strip().lower()
        if not self.config["enabled"]:
            return self._fail(name, self.config["reason"] or "n8n is not configured")
        spec = self.config["workflows"].get(name)
        if not spec:
            known = ", ".join(sorted(self.config["workflows"])) or "none configured"
            return self._fail(name, f"no n8n workflow named '{name}' (configured: {known})")

        headers = {"Content-Type": "application/json"}
        if self.config["secret"]:
            headers["X-Friday-Token"] = self.config["secret"]
        body = redact_secret_values(data or {})
        waited = _bounded_timeout(timeout or self.config["timeout"])
        url = spec["webhook_url"]

        try:
            response = self._post(url, json=body, headers=headers, timeout=waited)
        except Exception as error:
            return self._fail(name, _redact(f"n8n webhook failed: {type(error).__name__}: {error}",
                                            self.config["secret"]))
        if response.status_code >= 400:
            return self._fail(name, f"n8n returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception:
            payload = (getattr(response, "text", "") or "")[:MAX_RESPONSE_CHARS]
        payload = _unwrap_n8n(payload)

        self._calls += 1
        self._last_workflow = name
        self._last_success_at = time.time()
        self._last_error = ""
        log_action(actor="n8n", risk="external",
                   operation=f"n8n_workflow:{name}", allowed=True, outcome="ok")
        return {"ok": True, "workflow": name, "result": payload,
                "description": spec.get("description", "")}

    def _fail(self, workflow, error) -> dict:
        self._calls += 1
        self._failures += 1
        self._last_error = _redact(error, self.config["secret"])
        # A failed call is fresh evidence the connection changed; drop the cache
        # so the next available() re-probes instead of reporting a stale "up".
        self._cache = {"at": 0.0, "connected": False, "reason": self._last_error}
        log_action(actor="n8n", risk="external",
                   operation=f"n8n_workflow:{workflow or 'unknown'}",
                   allowed=False, outcome=_redact(error, self.config["secret"]))
        return {"ok": False, "workflow": workflow, "error": error,
                "fallback": "n8n unavailable - do the task with FRIDAY's own resources instead"}

    def list_workflows(self) -> dict:
        """Which workflows FRIDAY may hand over to, and what each is for."""
        if not self.config["workflows"]:
            return {"ok": False, "workflows": [], "error": self.config["reason"] or "no n8n workflows configured",
                    "help": self.connection_help()}
        rows = [{"name": name, "description": spec.get("description", ""),
                 "webhook_host": urlparse(spec["webhook_url"]).netloc}
                for name, spec in sorted(self.config["workflows"].items())]
        return {"ok": True, "workflows": rows, "connected": self.available()["connected"]}


_ADAPTER = None


def get_adapter(reload: bool = False):
    """Process-wide shared adapter.

    One instance means the reachability cache (and the config file read) happen
    once per process instead of once per caller - the HUD polls on a timer and
    the brain may ask several times in a single turn. `reload=True` forces a
    fresh read of the config (used after Sir edits n8n.json).
    """
    global _ADAPTER
    if _ADAPTER is None or reload:
        _ADAPTER = N8NAdapter()
    return _ADAPTER
