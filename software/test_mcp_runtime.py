"""Offline MCP discovery, transport ownership and dispatch policy."""
import asyncio
from types import SimpleNamespace

import pytest

from source.server.mcp_runtime import MCPConfigError, MCPRuntime, load_config


class FakeTool:
    def __init__(self, name, schema=None):
        self.name = name
        self.description = "read safely https://private.invalid/secret"
        self.inputSchema = schema if schema is not None else {"type": "object"}


class FakeSession:
    def __init__(self, tools=(), *, error=False, slow=False):
        self.tools = list(tools)
        self.calls = []
        self.error, self.slow = error, slow

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.slow:
            await asyncio.sleep(10)
        if self.error:
            raise RuntimeError("private endpoint and credential")
        return SimpleNamespace(isError=False, structuredContent={"value": 7},
                               content=[SimpleNamespace(type="text", text="<script>inert</script>")])


async def wait_until(condition):
    async with asyncio.timeout(4):
        while not condition():
            await asyncio.sleep(0.01)


def config(name="studio", allow=("read",), **extra):
    return {"name": name, "enabled": True, "transport": "stdio", "command": "fixture",
            "allow_tools": list(allow), **extra}


def test_missing_config_and_strict_config(tmp_path):
    assert load_config(tmp_path / "missing.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text('{"servers":[{"name":"x","enabled":true,"transport":"stdio","command":"x","token":"bad"}]}')
    with pytest.raises(MCPConfigError):
        load_config(bad)


def test_discovery_disabled_failure_schema_and_owned_cleanup():
    async def scenario():
        calls, closed = [], []

        async def factory(cfg, stack):
            name = cfg["name"]
            calls.append(name)
            task = asyncio.current_task()
            async def close():
                assert asyncio.current_task() is task
                closed.append(name)
            stack.push_async_callback(close)
            if name == "broken":
                raise RuntimeError("private endpoint")
            return FakeSession([FakeTool("read"), FakeTool("unsupported",
                {"type": "object", "$ref": "https://private.invalid"})])

        runtime = MCPRuntime([config("a", ("read", "unsupported")), config("b"),
                              config("broken"), config("off", enabled=False)],
                             session_factory=factory)
        assert not await runtime.list_tools() and not calls
        await runtime.start()
        await wait_until(lambda: len(runtime.registrations()) == 2)
        assert set(calls) == {"a", "b", "broken"}
        rows = runtime.cached_inventory()
        assert rows[2]["status"] == "connection_failed"
        assert "private endpoint" not in str(rows)
        assert rows[0]["tools"][1]["input_schema"] is None
        assert rows[0]["tools"][0]["confirmation_required"]
        assert rows[0]["tools"][0]["risk_class"] == "unknown"
        assert not (await runtime.call_tool("a", "read"))["ok"]
        assert (await runtime.stop())["ok"]
        assert set(closed) == {"a", "b", "broken"}
        assert runtime.cached_inventory()[0]["tools"]
        assert runtime.registrations() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_uncertain_remote_outcome_never_retries(failure):
    async def scenario():
        session = FakeSession([FakeTool("read")], error=failure == "exception", slow=failure == "timeout")
        opened = []
        async def factory(cfg, stack):
            opened.append(True)
            return session
        runtime = MCPRuntime([config(call_timeout_seconds=0.02)], session_factory=factory)
        await runtime.start()
        await wait_until(runtime.registrations)
        ref = runtime.registrations()[0]
        result = await runtime.dispatch(ref, {}, lambda: True)
        assert not result["ok"] and result["stage"] == "dispatch"
        assert "outcome unknown" in result["error"] and "private" not in str(result)
        assert session.calls == [("read", {})] and opened == [True]
        await runtime.stop()
    asyncio.run(scenario())


def test_dispatch_refuses_old_session_and_reports_remote_tool_failure():
    async def scenario():
        sessions = []
        async def factory(cfg, stack):
            session = FakeSession([FakeTool("read")])
            sessions.append(session)
            return session
        runtime = MCPRuntime([config()], session_factory=factory)
        await runtime.start()
        await wait_until(runtime.registrations)
        old = runtime.registrations()[0]
        await runtime.stop()
        await runtime.start()
        await wait_until(runtime.registrations)
        assert not (await runtime.dispatch(old, {}, lambda: True))["ok"]
        assert not sessions[1].calls
        async def fail(*_):
            return SimpleNamespace(isError=True, structuredContent={"reported": "failure"}, content=[])
        sessions[1].call_tool = fail
        outcome = await runtime.dispatch(runtime.registrations()[0], {}, lambda: True)
        assert outcome["stage"] == "remote_tool" and not outcome["ok"]
        await runtime.stop()
    asyncio.run(scenario())


def test_sdk_stdio_session_and_owned_transport_cleanup_without_inheriting_secrets(monkeypatch):
    from contextlib import asynccontextmanager
    from source.server import mcp_runtime as module

    async def scenario():
        order = []

        @asynccontextmanager
        async def transport(parameters, *, errlog):
            assert parameters.command == "fixture"
            assert parameters.env == {"EXPLICIT": "fixture-value"}
            owner = asyncio.current_task()
            order.append("open transport")
            try:
                yield "read", "write"
            finally:
                assert asyncio.current_task() is owner
                order.append("close transport")

        @asynccontextmanager
        async def session(*streams, message_handler):
            assert streams == ("read", "write")
            adapter = FakeSession([FakeTool("read")])
            async def initialize():
                order.append("initialize")
            adapter.initialize = initialize
            order.append("open session")
            try:
                yield adapter
            finally:
                order.append("close session")

        monkeypatch.setenv("EXPLICIT_VALUE", "fixture-value")
        monkeypatch.setenv("UNRELATED_API_KEY", "never forwarded")
        monkeypatch.setattr(module, "stdio_client", transport)
        monkeypatch.setattr(module, "ClientSession", session)
        runtime = MCPRuntime([config(env_from={"EXPLICIT": "EXPLICIT_VALUE"})])
        await runtime.start()
        await wait_until(runtime.registrations)
        assert (await runtime.stop())["ok"]
        assert order == ["open transport", "open session", "initialize", "close session", "close transport"]

    asyncio.run(scenario())


def test_oversize_or_binary_only_result_does_not_become_success():
    async def scenario():
        session = FakeSession([FakeTool("read")])
        async def factory(cfg, stack):
            return session
        runtime = MCPRuntime([config()], session_factory=factory)
        await runtime.start()
        await wait_until(runtime.registrations)
        try:
            for value, blocks in (({"large": "x" * 20000}, []),
                                  (None, [SimpleNamespace(type="image")])):
                async def call(*_):
                    return SimpleNamespace(isError=False, structuredContent=value, content=blocks)
                session.call_tool = call
                outcome = await runtime.dispatch(runtime.registrations()[0], {}, lambda: True)
                assert not outcome["ok"] and outcome["stage"] == "result"
        finally:
            await runtime.stop()
    asyncio.run(scenario())
