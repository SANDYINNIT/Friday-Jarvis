"""Opt-in MCP runtime with independently owned SDK sessions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import math
import json
import os
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

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
            "input_schema": deepcopy(self.input_schema),
            "risk_class": self.risk_class,
            "enabled": self.enabled,
            "confirmation_required": self.confirmation_required,
        }


@dataclass
class _ServerState:
    config: dict[str, Any]
    session: Any = None
    tools: list[MCPToolMetadata] | None = None
    discovered_at: float | None = None
    status: str = "not_connected"
    owner: asyncio.Task | None = None


ConfirmationCallback = Callable[[MCPToolMetadata, dict[str, Any]], bool | Awaitable[bool]]
SessionFactory = Callable[[dict[str, Any], AsyncExitStack], Awaitable[Any]]


def _bounded_timeout(value: Any, field: str) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as error:
        raise MCPConfigError(f"{field} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise MCPConfigError(f"{field} must be greater than zero")
    return min(value, MAX_TIMEOUT_SECONDS)


def _validate_config(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {"servers"} or not isinstance(raw["servers"], list):
        raise MCPConfigError("config must be an object containing only a servers list")
    if len(raw["servers"]) > 16:
        raise MCPConfigError("at most 16 MCP servers are supported")
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
        if transport != "stdio":
            try:
                url = urlsplit(item["url"])
                if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
                    raise ValueError
            except (KeyError, TypeError, ValueError, AttributeError):
                raise MCPConfigError(f"servers[{index}].url must be an http(s) URL without credentials") from None
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
        with config_path.open("rb") as source:
            contents = source.read(65537)
        if len(contents) > 65536:
            raise MCPConfigError("MCP config exceeds the size limit")
        raw = json.loads(contents)
    except (OSError, json.JSONDecodeError) as error:
        raise MCPConfigError(f"could not read MCP config: {error}") from error
    return _validate_config(raw)


def _risk(override: str | None) -> str:
    if override in {"read_only", "external", "destructive", "unknown"}:
        return override
    return "unknown"


def _bounded_json(value, limit):
    """Reject oversized/deep data before building a serialized copy."""
    remaining = limit

    def visit(item, depth=0):
        nonlocal remaining
        if depth > 12:
            raise ValueError
        remaining -= 1
        if isinstance(item, str):
            remaining -= len(item)
        if remaining < 0:
            raise ValueError
        if isinstance(item, dict):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError
    return encoded


def supported_schema(schema):
    """Validate a small literal JSON Schema subset without reference resolution."""
    def visit(node, depth=0):
        if depth > 5 or not isinstance(node, dict):
            raise ValueError
        kind = node.get("type")
        keys = {"type", "description", "title"}
        if kind == "object":
            keys |= {"properties", "required", "additionalProperties"}
            props = node.get("properties", {})
            if (not isinstance(props, dict) or len(props) > 32
                    or type(node.get("additionalProperties", True)) is not bool):
                raise ValueError
            required = node.get("required", [])
            if not isinstance(required, list) or not all(isinstance(key, str) and key in props for key in required):
                raise ValueError
            for name in props:
                if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
                    raise ValueError
        elif kind == "array":
            keys |= {"items", "minItems", "maxItems"}
            if "items" not in node:
                raise ValueError
        elif kind in ("string", "number", "integer", "boolean", "null"):
            keys |= {"enum"}
            keys |= {"minLength", "maxLength"} if kind == "string" else set()
            keys |= {"minimum", "maximum"} if kind in ("number", "integer") else set()
            if "enum" in node and (not isinstance(node["enum"], list) or len(node["enum"]) > 32
                                    or any(isinstance(item, (dict, list)) for item in node["enum"])):
                raise ValueError
            if "enum" in node:
                from .capability_inventory import text

                if any(isinstance(item, str) and (len(item) > 160 or text(item, 160) != item)
                       for item in node["enum"]):
                    raise ValueError
        else:
            raise ValueError
        if not node.keys() <= keys:
            raise ValueError
        # Annotations have no validation meaning and are never sent to models.
        return {key: ({name: visit(child, depth + 1) for name, child in value.items()}
                      if key == "properties" else visit(value, depth + 1) if key == "items" else value)
                for key, value in node.items() if key not in ("description", "title")}

    try:
        if _jsonschema is None or not isinstance(schema, dict) or schema.get("type") != "object":
            return None
        _bounded_json(schema, 16384)
        clean = visit(schema)
        _bounded_json(clean, 8192)
        _jsonschema.Draft202012Validator.check_schema(clean)
        return clean
    except (ValueError, TypeError, RecursionError, _jsonschema.SchemaError if _jsonschema else ValueError):
        return None


def _validate_arguments(metadata, arguments):
    try:
        if not isinstance(arguments, dict):
            return False
        _bounded_json(arguments, 4096)
        return _jsonschema.Draft202012Validator(metadata.input_schema).is_valid(arguments)
    except (TypeError, ValueError, RecursionError):
        return False


class MCPToolAdapter:
    """Compatibility facade for cached listings and consent-guarded calls."""

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
            env = {}
            env.update({key: os.environ[value] for key, value in config.get("env_from", {}).items() if value in os.environ})
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=config["command"], args=config.get("args", []), env=env,
            ), errlog=errlog))
        else:
            import httpx

            def client_factory(**kwargs):
                return httpx.AsyncClient(**kwargs, follow_redirects=False, trust_env=False)

            headers = {key: os.environ[value] for key, value in config.get("headers_env", {}).items() if value in os.environ}
            if transport == "sse":
                streams = await stack.enter_async_context(sse_client(
                    config["url"], headers=headers, httpx_client_factory=client_factory))
            else:
                client = await stack.enter_async_context(client_factory(
                    headers=headers, timeout=self.timeout(self._states[config["name"]], "connect")))
                streams = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
        async def notification(message):
            root = getattr(message, "root", None)
            state = self._states[config["name"]]
            if isinstance(message, Exception):
                state.status = "connection_failed"
            elif getattr(root, "method", None) == "notifications/tools/list_changed":
                state.status = "schema_changed"

        session = await stack.enter_async_context(ClientSession(streams[0], streams[1],
                                                                 message_handler=notification))
        await session.initialize()
        return session


    async def _own_server(self, state):
        # AnyIO transport contexts must enter and exit in this same task.
        state.status = "connecting"
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(self.timeout(state, "connect")):
                    state.session = await self._session_factory(state.config, stack)
                state.status = "discovering"
                async with asyncio.timeout(self.timeout(state, "connect")):
                    await self._discover(state)
                if state.status == "discovering":
                    state.status = "discovered"
                await asyncio.Future()
        except asyncio.CancelledError:
            if state.status not in ("call_timeout", "call_failed"):
                state.status = "disconnected"
            raise
        except Exception:
            state.status = "discovery_failed" if state.status == "discovering" else "connection_failed"
        finally:
            state.session = None

    @staticmethod
    def timeout(state, operation):
        field = operation + "_timeout_seconds"
        return min(30, _bounded_timeout(state.config.get(field, DEFAULT_TIMEOUT_SECONDS), field))

    async def start(self):
        self._started = True
        for state in self._states.values():
            if not state.config.get("enabled"):
                state.status = "disabled"
            elif state.owner is None or state.owner.done():
                state.owner = asyncio.create_task(self._own_server(state))

    async def stop(self):
        tasks = []
        for state in self._states.values():
            if state.owner is not None and not state.owner.done():
                if state.status not in ("call_timeout", "call_failed"):
                    state.status = "disconnected"
                tasks.append(state.owner)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=4)
        self._started = False
        return {"ok": all(task.done() for task in tasks)}

    def cached_inventory(self):
        """Display existing metadata only. Never connects, discovers or authorizes."""
        return [{
            "server": state.config["name"], "enabled": state.config.get("enabled", False),
            "session_attached": state.session is not None,
            "status": state.status, "discovered_at": state.discovered_at,
            "tools": None if state.tools is None else [tool.as_dict() for tool in state.tools],
        } for state in self._states.values()]

    async def _discover(self, state):
        result = await state.session.list_tools()
        allowed = set(state.config.get("allow_tools", []))
        overrides = state.config.get("risk_overrides", {})
        tools, seen = [], set()
        for tool in result.tools[:100]:
            name = str(tool.name)
            if not _NAME_RE.fullmatch(name):
                continue
            if name in seen:
                tools = [item for item in tools if item.name != name]
                continue
            seen.add(name)
            schema = supported_schema(getattr(tool, "inputSchema", None))
            tools.append(MCPToolMetadata(state.config["name"], name,
                str(getattr(tool, "description", "") or "")[:500], schema,
                _risk(overrides.get(name)), name in allowed, True))
        state.tools = tools
        state.discovered_at = time.time()

    async def list_tools(self):
        return [item for row in self.cached_inventory() for item in row["tools"] or []]

    def registrations(self):
        return [(state, state.session, tool) for state in self._states.values()
                if state.status == "discovered" and state.session is not None
                for tool in state.tools or () if tool.enabled and tool.input_schema is not None][:32]

    @staticmethod
    def current(reference):
        state, session, tool = reference
        return (state.status == "discovered" and state.session is session
                and any(tool is item for item in state.tools or ()))

    def validate(self, reference, arguments):
        return self.current(reference) and _validate_arguments(reference[2], arguments)

    async def dispatch(self, reference, arguments, permit):
        state, session, tool = reference
        if not self.validate(reference, arguments):
            return {"ok": False, "stage": "validation", "error": "Invalid arguments or stale MCP registration"}
        try:
            async with asyncio.timeout(self.timeout(state, "call")):
                if not self.current(reference) or not permit():
                    return {"ok": False, "stage": "permission", "error": "MCP dispatch permission expired"}
                result = await session.call_tool(tool.name, arguments)
        except TimeoutError:
            state.status = "call_timeout"
            if state.owner:
                state.owner.cancel()
            return {"ok": False, "stage": "dispatch", "error": "MCP timed out. Remote outcome unknown."}
        except Exception:
            state.status = "call_failed"
            if state.owner:
                state.owner.cancel()
            return {"ok": False, "stage": "dispatch", "error": "MCP transport failed. Remote outcome unknown."}
        if not self.current(reference):
            return {"ok": False, "stage": "dispatch", "error": "MCP session changed. Remote outcome unknown."}
        try:
            blocks = getattr(result, "content", ())
            content = [block.text for block in blocks[:16] if getattr(block, "type", None) == "text"]
            structured = getattr(result, "structuredContent", None)
            payload = {"text": content, "structured": structured,
                       "omitted_content": len(blocks) != len(content)}
            if not content and structured is None and not getattr(result, "isError", False):
                return {"ok": False, "stage": "result", "error": "MCP returned no supported result content."}
            encoded = _bounded_json(payload, min(self._max_bytes, 16000))
            if len(encoded) > min(self._max_chars, 8000):
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            return {"ok": False, "stage": "result", "error": "MCP result unavailable or exceeds the limit. Remote outcome unknown."}
        if getattr(result, "isError", False):
            return {"ok": False, "stage": "remote_tool", "error": "MCP tool reported an error", "result": payload}
        return {"ok": True, "stage": "remote_tool", "result": payload}

    async def call_tool(self, server, tool_name, arguments=None):
        reference = next((ref for ref in self.registrations()
                          if ref[2].server == server and ref[2].name == tool_name), None)
        if reference is None:
            return {"ok": False, "error": "tool is not connected and allowlisted"}
        arguments = {} if arguments is None else arguments
        if not self.validate(reference, arguments):
            return {"ok": False, "error": "invalid arguments"}
        if self._confirm is None:
            return {"ok": False, "error": "confirmation required"}
        arguments = deepcopy(arguments)
        decision = self._confirm(reference[2], deepcopy(arguments))
        if inspect.isawaitable(decision):
            decision = await decision
        if not decision:
            return {"ok": False, "error": "confirmation denied"}
        return await self.dispatch(reference, arguments, lambda: True)
