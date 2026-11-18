"""Opt-in, bounded Home Assistant integration boundary for FRIDAY.

This adapter is intentionally not wired into the voice server or Qwen3.  REST
operations are used for the small public API; the optional WebSocket transport
has one reader task and correlates every response by Home Assistant request id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import os
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx


LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_STATES = 200
MAX_RESPONSE_CHARS = 100_000
MAX_SERVICE_DATA_CHARS = 20_000


@dataclass(frozen=True)
class HomeAssistantConfig:
    url: str = ""
    token: str = ""
    allowed_services: frozenset[str] = frozenset()
    enabled: bool = False


def load_config(environ=None) -> HomeAssistantConfig:
    environ = os.environ if environ is None else environ
    url = str(environ.get("FRIDAY_HA_URL", "")).strip().rstrip("/")
    token = str(environ.get("FRIDAY_HA_TOKEN", "")).strip()
    raw = str(environ.get("FRIDAY_HA_ALLOWED_SERVICES", ""))
    allowed = frozenset(item.strip().lower() for item in raw.split(",") if item.strip())
    parsed = urlparse(url)
    valid_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    enabled = environ.get("FRIDAY_HOME_ASSISTANT") == "1" and bool(token) and valid_url and bool(allowed)
    return HomeAssistantConfig(url=url, token=token, allowed_services=allowed, enabled=enabled)


def _redact(value: Any, token: str) -> str:
    text = str(value)
    return text.replace(token, "[REDACTED]") if token else text


def _bounded_timeout(value: Any) -> float:
    try:
        return max(0.1, min(float(value), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _bounded_json(value: Any, limit: int = MAX_RESPONSE_CHARS) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        raise ValueError("Home Assistant response exceeded configured limit")
    return value


ConfirmationCallback = Callable[[str, str, dict[str, Any]], bool | Awaitable[bool]]
WebSocketFactory = Callable[[str], Awaitable[Any]]


class HomeAssistantAdapter:
    """Standalone Home Assistant client with default-deny service execution."""

    def __init__(self, *, config=None, timeout=DEFAULT_TIMEOUT_SECONDS,
                 confirmation_callback: ConfirmationCallback | None = None,
                 http_client=None, websocket_factory: WebSocketFactory | None = None):
        self.config = config or load_config()
        self.timeout = _bounded_timeout(timeout)
        self._confirm = confirmation_callback
        self._http = http_client
        self._owns_http = http_client is None
        self._ws_factory = websocket_factory
        self._ws = None
        self._ws_reader_task = None
        self._ws_lock = asyncio.Lock()
        self._ws_connect_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._started = False
        self._last_error = ""

    @property
    def enabled(self):
        return self.config.enabled

    def status(self):
        return {
            "enabled": self.enabled,
            "running": self._started,
            "url": self.config.url if self.enabled else "",
            "allowed_service_count": len(self.config.allowed_services),
            "websocket_connected": self._ws is not None,
            "pending_requests": len(self._pending),
            "last_error": self._last_error,
        }

    async def start(self):
        if not self.enabled:
            self._last_error = "Home Assistant integration is disabled"
            return False
        if self._started:
            return True
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.config.url,
                headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        self._started = True
        self._last_error = ""
        return True

    async def stop(self, timeout=3.0):
        self._started = False
        task = self._ws_reader_task
        self._ws_reader_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            close = getattr(ws, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    try:
                        await asyncio.wait_for(result, _bounded_timeout(timeout))
                    except Exception:
                        pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Home Assistant adapter stopped"))
        self._pending.clear()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        return True

    async def _rest(self, method, path, *, json_body=None):
        if not self._started:
            await self.start()
        if not self.enabled or self._http is None:
            return {"ok": False, "error": "Home Assistant integration is disabled"}
        try:
            response = await self._http.request(method, path, json=json_body)
            response.raise_for_status()
            payload = response.json()
            _bounded_json(payload)
            return {"ok": True, "result": payload}
        except Exception as error:
            if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or "timeout" in error.__class__.__name__.lower():
                self._last_error = "Home Assistant request timed out"
                return {"ok": False, "error": self._last_error}
            self._last_error = _redact(f"Home Assistant request failed: {error}", self.config.token)[:500]
            LOGGER.warning("Home Assistant request failed: %s", self._last_error)
            return {"ok": False, "error": self._last_error}

    async def list_states(self, limit=MAX_STATES):
        limit = max(1, min(int(limit), MAX_STATES))
        result = await self._rest("GET", "/api/states")
        if not result["ok"]:
            return {"ok": False, "states": [], "error": result["error"]}
        states = result["result"]
        if not isinstance(states, list):
            return {"ok": False, "states": [], "error": "Home Assistant states response was not a list"}
        return {"ok": True, "states": states[:limit], "count": min(len(states), limit), "truncated": len(states) > limit, "error": ""}

    async def call_service(self, domain, service, data=None):
        domain, service = str(domain).strip().lower(), str(service).strip().lower()
        key = f"{domain}.{service}"
        data = {} if data is None else data
        if not domain or not service or not isinstance(data, dict):
            return {"ok": False, "action": key, "error": "domain, service, and object data are required"}
        if len(json.dumps(data, default=str)) > MAX_SERVICE_DATA_CHARS:
            return {"ok": False, "action": key, "error": "service data exceeded configured limit"}
        if not self.enabled:
            return {"ok": False, "action": key, "error": "Home Assistant integration is disabled"}
        if key not in self.config.allowed_services:
            return {"ok": False, "action": key, "error": "service is not allowlisted"}
        if self._confirm is None:
            return {"ok": False, "action": key, "error": "confirmation required"}
        try:
            approved = self._confirm(domain, service, data)
            if inspect.isawaitable(approved):
                approved = await approved
        except Exception as error:
            return {"ok": False, "action": key, "error": _redact(f"confirmation failed: {error}", self.config.token)}
        if not approved:
            return {"ok": False, "action": key, "error": "confirmation denied"}
        result = await self._rest("POST", f"/api/services/{domain}/{service}", json_body=data)
        if not result["ok"]:
            return {"ok": False, "action": key, "error": result["error"]}
        return {"ok": True, "action": key, "result": result["result"], "error": ""}

    async def websocket_request(self, message):
        """Send one HA WebSocket command; the sole reader resolves its id."""
        if not self.enabled:
            return {"ok": False, "error": "Home Assistant integration is disabled"}
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return {"ok": False, "error": "WebSocket message must contain a type"}
        for attempt in range(2):
            request_id = None
            try:
                await self._ensure_websocket()
                request_id = self._next_id
                self._next_id += 1
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self._pending[request_id] = future
                outbound = dict(message, id=request_id)
                async with self._ws_lock:
                    await self._ws.send(json.dumps(outbound))
                payload = await asyncio.wait_for(future, self.timeout)
                _bounded_json(payload)
                return {"ok": bool(payload.get("success", True)), "id": request_id, "result": payload, "error": "" if payload.get("success", True) else "Home Assistant WebSocket command failed", "reconnected": attempt == 1}
            except asyncio.TimeoutError:
                error_text = "Home Assistant WebSocket request timed out"
            except Exception as error:
                error_text = _redact(f"Home Assistant WebSocket request failed: {error}", self.config.token)[:500]
            finally:
                if request_id is not None:
                    self._pending.pop(request_id, None)
            if attempt == 0:
                await self._reset_websocket()
                continue
            self._last_error = error_text
            return {"ok": False, "error": error_text, "reconnected": True}

    async def _ensure_websocket(self):
        async with self._ws_connect_lock:
            await self._ensure_websocket_locked()

    async def _ensure_websocket_locked(self):
        if self._ws is not None and self._ws_reader_task and not self._ws_reader_task.done():
            return
        if self._ws_factory is None:
            import websockets
            ws_url = self.config.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
            self._ws = await websockets.connect(ws_url, open_timeout=self.timeout, close_timeout=self.timeout)
        else:
            ws_url = self.config.url + "/api/websocket"
            self._ws = await self._ws_factory(ws_url)
        greeting = json.loads(await asyncio.wait_for(self._ws.recv(), self.timeout))
        if greeting.get("type") == "auth_required":
            await self._ws.send(json.dumps({"type": "auth", "access_token": self.config.token}))
            auth = json.loads(await asyncio.wait_for(self._ws.recv(), self.timeout))
            if auth.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant WebSocket authentication failed")
        elif greeting.get("type") != "auth_ok":
            raise RuntimeError("unexpected Home Assistant WebSocket greeting")
        self._ws_reader_task = asyncio.create_task(self._read_websocket(), name="friday-home-assistant-ws-reader")

    async def _reset_websocket(self):
        task, self._ws_reader_task = self._ws_reader_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            close = getattr(ws, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.gather(result, return_exceptions=True)

    async def _read_websocket(self):
        try:
            while self._ws is not None:
                message = json.loads(await self._ws.recv())
                request_id = message.get("id")
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            safe = _redact(error, self.config.token)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(f"Home Assistant WebSocket disconnected: {safe}"))
