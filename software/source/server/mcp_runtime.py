"""Opt-in, isolated MCP client runtime for FRIDAY.

This module is deliberately not imported by the voice path.  It owns one
long-lived SDK session per configured server and exposes a small adapter
boundary for a future model integration.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from .audit import log_action

try:
    import jsonschema as _jsonschema
except ImportError:  # pragma: no cover - jsonschema is present in the dev env
    _jsonschema = None


DEFAULT_CONFIG_PATH = Path.home() / ".friday" / "mcp.json"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_RESPONSE_CHARS = 100_000
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key|authorization)")
_DESTRUCTIVE_RE = re.compile(r"(?i)(delete|remove|destroy|terminate|kill|shutdown|write|update|overwrite|send|publish|upload|execute)")
_EXTERNAL_RE = re.compile(r"(?i)(http|web|browser|email|message|search|remote|cloud|network)")


class MCPRuntimeError(Exception):
    """Base error for configuration and runtime failures."""


class MCPConfigError(MCPRuntimeError):
    """Configuration is invalid or unsafe."""


@dataclass(frozen=True)
class MCPToolMetadata:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_class: str
    enabled: bool
    confirmation_required: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "tool_name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_class": self.risk_class,
            "enabled": self.enabled,
            "confirmation_required": self.confirmation_required,
        }


@dataclass
class _ServerState:
    config: dict[str, Any]
    stack: AsyncExitStack | None = None
    session: Any = None
    tools: list[MCPToolMetadata] | None = None


ConfirmationCallback = Callable[[MCPToolMetadata, dict[str, Any]], bool | Awaitable[bool]]
SessionFactory = Callable[[dict[str, Any], AsyncExitStack], Awaitable[Any]]


def _bounded_timeout(value: Any, field: str) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as error:
        raise MCPConfigError(f"{field} must be a number") from error
    if value <= 0:
        raise MCPConfigError(f"{field} must be greater than zero")
    return min(value, MAX_TIMEOUT_SECONDS)


def _validate_config(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {"servers"} or not isinstance(raw["servers"], list):
        raise MCPConfigError("config must be an object containing only a servers list")
    servers = []
    names = set()
    allowed = {
        "name", "enabled", "transport", "command", "args", "env_from", "url",
        "headers_env", "allow_tools", "risk_overrides", "connect_timeout_seconds", "call_timeout_seconds",
    }
    for index, item in enumerate(raw["servers"]):
        if not isinstance(item, dict) or not set(item).issubset(allowed):
            raise MCPConfigError(f"servers[{index}] contains unsupported fields")
        name = item.get("name")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or name in names:
            raise MCPConfigError(f"servers[{index}].name is invalid or duplicated")
        names.add(name)
        transport = item.get("transport")
        if transport not in {"stdio", "streamable_http", "sse"}:
            raise MCPConfigError(f"servers[{index}].transport is unsupported")
        if transport == "stdio" and (not isinstance(item.get("command"), str) or not item["command"].strip()):
            raise MCPConfigError(f"servers[{index}].command is required for stdio")
        if transport != "stdio" and (not isinstance(item.get("url"), str) or not item["url"].startswith(("http://", "https://"))):
            raise MCPConfigError(f"servers[{index}].url must be an http(s) URL")
        if not isinstance(item.get("enabled", False), bool):
            raise MCPConfigError(f"servers[{index}].enabled must be boolean")
        for field in ("args", "allow_tools"):
            if field in item and (not isinstance(item[field], list) or not all(isinstance(value, str) for value in item[field])):
                raise MCPConfigError(f"servers[{index}].{field} must be a string list")
        for field in ("env_from", "headers_env", "risk_overrides"):
            if field in item and (not isinstance(item[field], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in item[field].items())):
                raise MCPConfigError(f"servers[{index}].{field} must be a string map")
        for key in item.get("env_from", {}):
            if _SECRET_KEY_RE.search(key):
                raise MCPConfigError(f"servers[{index}].env_from must not use secret-looking child names")
        for field in ("connect_timeout_seconds", "call_timeout_seconds"):
            if field in item:
                _bounded_timeout(item[field], field)
        servers.append(dict(item))
    return servers


def load_config(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Load an allowlisted config; absent config intentionally disables MCP."""
    config_path = Path(path or os.environ.get("FRIDAY_MCP_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        return []
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MCPConfigError(f"could not read MCP config: {error}") from error
    return _validate_config(raw)


def _risk(name: str, description: str, override: str | None) -> str:
    if override in {"read_only", "external", "destructive", "unknown"}:
        return override
    text = f"{name} {description}"
    if _DESTRUCTIVE_RE.search(text):
        return "destructive"
    if _EXTERNAL_RE.search(text):
        return "external"
    if re.search(r"(?i)(get|list|read|fetch|inspect|status|search)", text):
        return "read_only"
    return "unknown"


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _validate_arguments(metadata: MCPToolMetadata, arguments: dict[str, Any]) -> str | None:
    """Validate call arguments against the tool's JSON schema.

    Returns a human-readable validation error, or ``None`` when the call is
    acceptable (or no usable schema was provided).
    """
    schema = metadata.input_schema
    if not isinstance(schema, dict) or not schema:
        return None
    required = schema.get("required")
    if isinstance(required, list):
        missing = [name for name in required if name not in arguments]
        if missing:
            return "missing required argument(s): " + ", ".join(missing)
    if _jsonschema is None:
        return None
    try:
        _jsonschema.validate(instance=arguments, schema=schema)
    except _jsonschema.ValidationError as error:
        return (str(error.message or error) or error.__class__.__name__)[:300]
    except _jsonschema.SchemaError as error:
        return "tool schema is invalid"
    return None


class MCPToolAdapter:
    """Future model boundary; it does not register tools with Open Interpreter."""

    def __init__(self, runtime: "MCPRuntime"):
        self.runtime = runtime

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self.runtime.list_tools()

    async def call_tool(self, server: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.runtime.call_tool(server, tool_name, arguments)


class MCPRuntime:
    def __init__(
        self,
        config: list[dict[str, Any]] | None = None,
        *,
        confirmation_callback: ConfirmationCallback | None = None,
        session_factory: SessionFactory | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_response_chars: int = MAX_RESPONSE_CHARS,
    ):
        self._configs = load_config() if config is None else _validate_config({"servers": config})
        self._states = {item["name"]: _ServerState(item) for item in self._configs}
        self._confirm = confirmation_callback
        self._session_factory = session_factory or self._sdk_session
        self._max_bytes = max_response_bytes
        self._max_chars = max_response_chars
        self._started = False

    async def _sdk_session(self, config: dict[str, Any], stack: AsyncExitStack) -> ClientSession:
        transport = config["transport"]
        if transport == "stdio":
            env = dict(os.environ)
            env.update({key: os.environ[value] for key, value in config.get("env_from", {}).items() if value in os.environ})
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=config["command"], args=config.get("args", []), env=env,
            )))
        else:
            headers = {key: os.environ[value] for key, value in config.get("headers_env", {}).items() if value in os.environ}
            if transport == "sse":
                streams = await stack.enter_async_context(sse_client(config["url"], headers=headers))
            else:
                import httpx
                client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=config.get("connect_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)))
                streams = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await asyncio.wait_for(session.initialize(), _bounded_timeout(config.get("connect_timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "connect_timeout_seconds"))
        return session

    async def _connect(self, state: _ServerState) -> Any:
        if state.session is not None:
            return state.session
        state.stack = AsyncExitStack()
        try:
            state.session = await asyncio.wait_for(self._session_factory(state.config, state.stack), _bounded_timeout(state.config.get("connect_timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "connect_timeout_seconds"))
            return state.session
        except Exception:
            await self._close_state(state)
            raise

    async def _close_state(self, state: _ServerState) -> None:
        state.session = None
        state.tools = None
        if state.stack is not None:
            stack, state.stack = state.stack, None
            await stack.aclose()

    async def start(self) -> dict[str, Any]:
        self._started = True
        results = {}
        for name, state in self._states.items():
            if not state.config.get("enabled", False):
                results[name] = {"ok": True, "enabled": False}
                continue
            try:
                await self._connect(state)
                results[name] = {"ok": True, "enabled": True}
            except Exception as error:
                results[name] = {"ok": False, "enabled": True, "error": f"connect failed: {error}"}
        return results

    async def stop(self) -> dict[str, Any]:
        for state in self._states.values():
            await self._close_state(state)
        self._started = False
        return {"ok": True}

    async def refresh(self) -> list[dict[str, Any]]:
        for state in self._states.values():
            state.tools = None
        return await self.list_tools()

    async def _list_server_tools(self, state: _ServerState) -> list[MCPToolMetadata]:
        if state.tools is not None:
            return state.tools
        result = await asyncio.wait_for(state.session.list_tools(), _bounded_timeout(state.config.get("call_timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "call_timeout_seconds"))
        allowed = set(state.config.get("allow_tools", []))
        overrides = state.config.get("risk_overrides", {})
        state.tools = []
        for tool in result.tools:
            name = str(tool.name)
            description = str(getattr(tool, "description", "") or "")
            state.tools.append(MCPToolMetadata(state.config["name"], name, description, dict(getattr(tool, "inputSchema", {}) or {}), _risk(name, description, overrides.get(name)), name in allowed, _risk(name, description, overrides.get(name)) != "read_only"))
        return state.tools

    async def list_tools(self) -> list[dict[str, Any]]:
        output = []
        for state in self._states.values():
            if not state.config.get("enabled", False):
                continue
            try:
                await self._connect(state)
                output.extend(tool.as_dict() for tool in await self._list_server_tools(state))
            except Exception as error:
                output.append({"server": state.config["name"], "error": f"tool discovery failed: {error}"})
        return output

    async def call_tool(self, server: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        state = self._states.get(server)
        if state is None or not state.config.get("enabled", False):
            return {"ok": False, "server": server, "tool": tool_name, "error": "server is not configured and enabled"}
        if not isinstance(arguments, dict):
            return {"ok": False, "server": server, "tool": tool_name, "error": "arguments must be an object"}
        try:
            await self._connect(state)
            metadata = next((tool for tool in await self._list_server_tools(state) if tool.name == tool_name), None)
            if metadata is None or not metadata.enabled:
                return {"ok": False, "server": server, "tool": tool_name, "error": "tool is not allowlisted"}
            validation_error = _validate_arguments(metadata, arguments)
            if validation_error:
                log_action(actor="mcp", risk=metadata.risk_class,
                           operation=f"mcp:{server}:{tool_name}",
                           allowed=True, outcome="invalid-arguments",
                           args=arguments, error=validation_error)
                return {"ok": False, "server": server, "tool": tool_name,
                        "error": f"arguments failed schema validation: {validation_error}"}
            if metadata.confirmation_required:
                if self._confirm is None:
                    return {"ok": False, "server": server, "tool": tool_name, "error": "confirmation required"}
                decision = self._confirm(metadata, arguments)
                if inspect.isawaitable(decision):
                    decision = await decision
                if not decision:
                    return {"ok": False, "server": server, "tool": tool_name, "error": "confirmation denied"}
            started = time.monotonic()
            result = await asyncio.wait_for(state.session.call_tool(tool_name, arguments), _bounded_timeout(state.config.get("call_timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "call_timeout_seconds"))
            payload = result.model_dump(by_alias=True, mode="json") if hasattr(result, "model_dump") else result
            if _json_size(payload) > self._max_bytes:
                return {"ok": False, "server": server, "tool": tool_name, "error": "MCP response exceeded byte limit", "latency": time.monotonic() - started}
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) > self._max_chars:
                return {"ok": False, "server": server, "tool": tool_name, "error": "MCP response exceeded character limit", "latency": time.monotonic() - started}
            latency = time.monotonic() - started
            is_error = bool(getattr(result, "isError", False))
            log_action(actor="mcp", risk=metadata.risk_class,
                       operation=f"mcp:{server}:{tool_name}",
                       allowed=True, outcome="error" if is_error else "ok",
                       args=arguments, latency_ms=round(latency * 1000))
            return {"ok": not is_error, "server": server, "tool": tool_name, "result": payload, "error": "" if not is_error else "MCP tool returned an error", "latency": latency}
        except asyncio.TimeoutError:
            await self._close_state(state)
            return {"ok": False, "server": server, "tool": tool_name, "error": "MCP operation timed out"}
        except Exception as first_error:
            await self._close_state(state)
            try:
                await self._connect(state)
                state.tools = None
                metadata = next((tool for tool in await self._list_server_tools(state) if tool.name == tool_name), None)
                if metadata is None or not metadata.enabled:
                    raise MCPRuntimeError("tool is not allowlisted after reconnect")
                result = await asyncio.wait_for(state.session.call_tool(tool_name, arguments), _bounded_timeout(state.config.get("call_timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "call_timeout_seconds"))
                payload = result.model_dump(by_alias=True, mode="json") if hasattr(result, "model_dump") else result
                if _json_size(payload) > self._max_bytes or len(json.dumps(payload, ensure_ascii=False, default=str)) > self._max_chars:
                    raise MCPRuntimeError("MCP response exceeded configured limit")
                return {"ok": not bool(getattr(result, "isError", False)), "server": server, "tool": tool_name, "result": payload, "retried": True, "error": ""}
            except Exception as retry_error:
                await self._close_state(state)
                return {"ok": False, "server": server, "tool": tool_name, "error": f"MCP call failed after reconnect: {retry_error}", "initial_error": str(first_error)}
