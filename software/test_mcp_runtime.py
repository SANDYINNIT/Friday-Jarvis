import asyncio

from source.server.mcp_runtime import MCPConfigError, MCPRuntime, load_config


class FakeTool:
    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object"}


class FakeResult:
    def __init__(self, value, error=False):
        self.isError = error
        self.value = value

    def model_dump(self, **kwargs):
        return {"isError": self.isError, "value": self.value}


class FakeSession:
    def __init__(self, tools, value="ok", fail_call=False):
        self.tools, self.value, self.fail_call = tools, value, fail_call
        self.closed = False

    async def list_tools(self):
        return type("Tools", (), {"tools": self.tools})()

    async def call_tool(self, name, arguments):
        if self.fail_call:
            raise RuntimeError("worker failed")
        if self.value == "timeout":
            await asyncio.sleep(1)
        return FakeResult(self.value)


def run(coro):
    return asyncio.run(coro)


def config(name="local", allow=("read",), **extra):
    value = {"name": name, "enabled": True, "transport": "stdio", "command": "fake", "allow_tools": list(allow)}
    value.update(extra)
    return value


def test_missing_config_disables(tmp_path):
    assert load_config(tmp_path / "missing.json") == []


def test_config_is_strict_and_secret_safe(tmp_path):
    (tmp_path / "bad.json").write_text('{"servers": [{"name":"x","enabled":true,"transport":"stdio","command":"x","token":"bad"}]}')
    try:
        load_config(tmp_path / "bad.json")
    except MCPConfigError:
        pass
    else:
        raise AssertionError("invalid config accepted")


def test_discovery_metadata_allowlist_and_confirmation():
    async def factory(cfg, stack):
        return FakeSession([FakeTool("read", "read status", {"type": "object"}), FakeTool("delete", "delete item")])
    runtime = MCPRuntime([config()], session_factory=factory)
    tools = run(runtime.list_tools())
    assert tools[0]["server"] == "local"
    assert tools[0]["input_schema"] == {"type": "object"}
    assert tools[0]["enabled"] is True
    assert tools[1]["enabled"] is False
    assert tools[1]["risk_class"] == "destructive"
    assert run(runtime.call_tool("local", "delete"))["error"] == "tool is not allowlisted"


def test_timeout_returns_truthful_error_and_disconnects():
    async def factory(cfg, stack):
        return FakeSession([FakeTool("read", "read")], value="timeout")
    runtime = MCPRuntime([config()], session_factory=factory)
    runtime._states["local"].config["call_timeout_seconds"] = 0.01
    result = run(runtime.call_tool("local", "read"))
    assert result["ok"] is False
    assert "timed out" in result["error"] or "failed after reconnect" in result["error"]


def test_reconnect_and_server_isolation():
    calls = {"a": 0, "b": 0}
    async def factory(cfg, stack):
        calls[cfg["name"]] += 1
        return FakeSession([FakeTool("read", "read")], fail_call=calls[cfg["name"]] == 1 if cfg["name"] == "a" else False)
    runtime = MCPRuntime([config("a"), config("b")], session_factory=factory)
    result = run(runtime.call_tool("a", "read"))
    assert result["ok"] is True and result["retried"] is True
    assert run(runtime.call_tool("b", "read"))["ok"] is True
    assert calls == {"a": 2, "b": 1}


def test_shutdown_is_clean_and_unknown_requires_confirmation():
    async def factory(cfg, stack):
        return FakeSession([FakeTool("mystery", "do something")])
    runtime = MCPRuntime([config(allow=("mystery",))], session_factory=factory)
    assert run(runtime.call_tool("local", "mystery"))["error"] == "confirmation required"
    assert run(runtime.stop())["ok"] is True
