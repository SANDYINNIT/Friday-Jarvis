"""WP7: MCP pre-call JSON-schema validation and 3-stage gate tests."""
import pytest

from source.server.mcp_runtime import MCPToolMetadata, MCPRuntime, _validate_arguments


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "recursive": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["path"],
}


def tool_metadata(enabled=True, confirmation_required=False):
    return MCPToolMetadata(
        server="fs", name="read_file", description="reads a file",
        input_schema=TOOL_SCHEMA, risk_class="read_only",
        enabled=enabled, confirmation_required=confirmation_required,
    )


def test_valid_arguments_pass():
    assert _validate_arguments(tool_metadata(), {"path": "a.txt", "recursive": True}) is None


def test_missing_required_is_rejected():
    error = _validate_arguments(tool_metadata(), {"recursive": True})
    assert error is not None
    assert "path" in error


def test_wrong_type_is_rejected():
    error = _validate_arguments(tool_metadata(), {"path": "a.txt", "recursive": "yes"})
    assert error is not None


def test_minimum_enforced():
    error = _validate_arguments(tool_metadata(), {"path": "a.txt", "limit": 0})
    assert error is not None


def test_empty_schema_passes():
    meta = MCPToolMetadata("s", "t", "d", {}, "unknown", True, False)
    assert _validate_arguments(meta, {"anything": 1}) is None


class FakeSession:
    def __init__(self):
        self.tool_calls = []

    async def list_tools(self):
        from types import SimpleNamespace
        return SimpleNamespace(tools=[SimpleNamespace(name="read_file", description="reads",
                                                      inputSchema=TOOL_SCHEMA)])

    async def call_tool(self, name, arguments):
        self.tool_calls.append((name, arguments))
        from types import SimpleNamespace
        return SimpleNamespace(isError=False, model_dump=lambda **kw: {"seen": arguments, "name": name})


async def _run_case(arguments):
    session = FakeSession()

    async def factory(config, stack):
        return session

    runtime = MCPRuntime(
        config=[{
            "name": "fs", "enabled": True, "transport": "stdio",
            "command": "node", "args": [], "allow_tools": ["read_file"],
        }],
        confirmation_callback=lambda *a: True,
        session_factory=factory,
    )
    result = await runtime.call_tool("fs", "read_file", arguments)
    return result, session


def _run_call(arguments):
    import asyncio
    return asyncio.run(_run_case(arguments))


def test_call_tool_rejects_invalid_arguments_before_call():
    result, session = _run_call({"recursive": True})
    assert result["ok"] is False
    assert "schema validation" in result["error"]
    assert session.tool_calls == []


def test_call_tool_allows_valid_arguments():
    result, session = _run_call({"path": "a.txt"})
    assert result["ok"] is True
    assert result["result"]["seen"] == {"path": "a.txt"}
    assert session.tool_calls == [("read_file", {"path": "a.txt"})]