"""Actual Light admission, completion interception, HUD consent and tool budget."""
import asyncio
import json

import pytest

from source.server import server, status_ui
from source.server.mcp_runtime import MCPRuntime
from source.server.mcp_turn import ModelTools, PREFIX
from test_mcp_runtime import FakeSession, FakeTool, config, wait_until
from test_request_reply_ownership import drain, isolated_server, telegram


@pytest.fixture
def connection(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [config("a"), config("b"), config("off", enabled=False),
                                           config("broken")]}))
    monkeypatch.setenv("FRIDAY_MCP_CONFIG", str(path))
    sessions, closed = {}, []
    schema = {"type": "object", "properties": {"item": {"type": "string"}},
              "required": ["item"], "additionalProperties": False}

    async def factory(self, cfg, stack):
        name = cfg["name"]
        stack.callback(closed.append, name)
        if name == "broken":
            raise RuntimeError("unavailable private fixture")
        sessions[name] = FakeSession([FakeTool("read", schema)])
        return sessions[name]

    monkeypatch.setattr(MCPRuntime, "_sdk_session", factory)
    return sessions, closed


def provider_stream(params, arguments='{"item":"exact"}'):
    definitions = [t["function"] for t in params.get("tools", ()) if t["function"]["name"].startswith(PREFIX)]
    if not definitions:
        yield {"choices": [{"delta": {"content": "Ordinary answer"}, "finish_reason": "stop"}]}
        return
    assert len(definitions) == 2
    assert definitions[0]["name"] != definitions[1]["name"]
    assert "private.invalid" not in str(definitions)
    name = definitions[0]["name"]
    for function in ({"name": name[:12], "arguments": arguments[:5]},
                     {"name": name[12:], "arguments": arguments[5:]}):
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": function}]}}]}
    yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}


def produce(runtime):
    def frames():
        params = {"model": "fixture-model", "messages": [{"role": "system", "content": "fixture"}],
                  "tools": [{"type": "function", "function": {"name": "execute"}}]}
        for chunk in runtime.llm.completions(**params):
            content = chunk["choices"][0]["delta"].get("content")
            if content:
                yield {"role": "assistant", "type": "message", "content": content}
        yield {"role": "assistant", "type": "message", "end": True}

    runtime._respond_and_store = frames
    runtime.producer()
    runtime.respond_thread = None


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
@pytest.mark.parametrize("mode", ["approve", "decline", "stop", "budget", "generation", "schema", "invalid"])
def test_light_native_proposal_consent_and_final_gate(connection, isolated_server, mode):
    runtime, _ = isolated_server
    sessions, closed = connection
    app = runtime.server.app
    adapter = runtime.server_state["mcp_runtime"]
    bridge = status_ui.WebviewBridge(runtime.fixture_status, runtime)

    async def scenario():
        assert not sessions
        bridge.get_capability_inventory()
        assert not sessions
        await app.router.startup()
        try:
            await wait_until(lambda: len(adapter.registrations()) == 2)
            assert adapter._states["broken"].status == "connection_failed"
            runtime.fixture_stream = (lambda params: provider_stream(params, '{"item":"first","item":"last"}')
                                      if mode == "invalid" else provider_stream(params))
            assert (await asyncio.to_thread(runtime.submit_text, "Use an MCP fixture tool"))["accepted"]
            worker = asyncio.create_task(asyncio.to_thread(produce, runtime))
            if mode != "invalid":
                await wait_until(lambda: bridge.get_turn()["mcp_proposal"] is not None)
                proposal = bridge.get_turn()["mcp_proposal"]
                assert proposal["tool"] == "a:read" and json.loads(proposal["arguments"]) == {"item": "exact"}
                assert not bridge.decide_mcp_proposal("wrong-id", True)["ok"]
                assert not bridge.decide_mcp_proposal(proposal["id"], "yes")["ok"]
                assert not sessions["a"].calls
                if mode == "stop":
                    bridge.stop_turn()
                elif mode == "generation":
                    runtime.server_state["turn_attempt"] = object()
                elif mode == "schema":
                    adapter._states["a"].status = "schema_changed"
                else:
                    if mode == "budget":
                        from interpreter.core.friday_execution import gate_for
                        gate = gate_for(runtime)
                        gate.budget_owner = server._ownership(runtime.server_state)
                        gate.used = gate.limit
                    assert bridge.decide_mcp_proposal(proposal["id"], mode != "decline")["ok"]
                    assert not bridge.decide_mcp_proposal(proposal["id"], True)["ok"]
            await asyncio.wait_for(worker, timeout=4)
            await drain(runtime)
            assert sessions["a"].calls == ([("read", {"item": "exact"})] if mode == "approve" else [])
            assert not sessions["b"].calls
            if mode == "approve":
                feed = bridge.get_chat()
                assert any("MCP tool output (untrusted)" in row["content"] and "<script>" in row["content"]
                           for row in feed if row["role"] == "tool")
                assert bridge.get_turn()["mcp_proposal"] is None
        finally:
            await app.router.shutdown()
        assert set(closed) == {"a", "b", "broken"}

    asyncio.run(scenario())


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
def test_light_remote_and_grounded_cannot_register_mcp(connection, isolated_server):
    runtime, _ = isolated_server
    app = runtime.server.app
    bridge = status_ui.WebviewBridge(runtime.fixture_status, runtime)
    assert bridge.import_knowledge("Fixture source", "Peregrine reference for this question")["ok"]
    runtime.fixture_stream = provider_stream

    async def scenario():
        await app.router.startup()
        try:
            await wait_until(lambda: len(runtime.server_state["mcp_runtime"].registrations()) == 2)
            assert (await asyncio.to_thread(runtime.submit_text, "What does the Peregrine reference say?"))["accepted"]
            await asyncio.to_thread(produce, runtime)
            await drain(runtime)
            grounded = runtime.fixture_completions[-1]
            assert "tools" not in grounded and "local_reference_passages" in str(grounded["messages"])
            await telegram(runtime, "101", "Ordinary remote question")
            await asyncio.to_thread(produce, runtime)
            await drain(runtime)
            names = [t["function"]["name"] for t in runtime.fixture_completions[-1]["tools"]]
            assert names == ["execute"]
            assert not any(session.calls for session in connection[0].values())
        finally:
            await app.router.shutdown()

    asyncio.run(scenario())


def test_builtin_delta_remains_a_builtin():
    from types import SimpleNamespace

    stream = ModelTools(SimpleNamespace(registrations=lambda: []), {"tools": []})
    frame = {"choices": [{"delta": {"tool_calls": [{"index": 0,
              "function": {"name": "execute", "arguments": "{}"}}]}}]}
    assert stream.filter(frame) == [frame]
    assert stream.proposal() is None


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
@pytest.mark.parametrize("during_call", [False, True])
def test_stop_after_approval_fences_dispatch_and_late_output(connection, isolated_server, monkeypatch, during_call):
    runtime, _ = isolated_server
    sessions, _ = connection
    bridge = status_ui.WebviewBridge(runtime.fixture_status, runtime)
    app = runtime.server.app
    adapter = runtime.server_state["mcp_runtime"]

    async def scenario():
        await app.router.startup()
        try:
            await wait_until(lambda: len(adapter.registrations()) == 2)
            entered, release = asyncio.Event(), asyncio.Event()
            dispatch, call = adapter.dispatch, sessions["a"].call_tool

            async def delay_dispatch(*args):
                entered.set()
                await release.wait()
                return await dispatch(*args)

            async def late_call(*args):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass  # Simulate a transport that still returns after Stop.
                return await call(*args)

            if during_call:
                monkeypatch.setattr(sessions["a"], "call_tool", late_call)
            else:
                monkeypatch.setattr(adapter, "dispatch", delay_dispatch)
            runtime.fixture_stream = provider_stream
            assert (await asyncio.to_thread(runtime.submit_text, "Propose fixture MCP call"))["accepted"]
            worker = asyncio.create_task(asyncio.to_thread(produce, runtime))
            await wait_until(lambda: bridge.get_turn()["mcp_proposal"] is not None)
            proposal = bridge.get_turn()["mcp_proposal"]
            assert bridge.decide_mcp_proposal(proposal["id"], True)["ok"]
            await asyncio.wait_for(entered.wait(), timeout=4)
            await asyncio.to_thread(bridge.stop_turn)
            release.set()
            await asyncio.wait_for(worker, timeout=4)
            await drain(runtime)
            assert len(sessions["a"].calls) == int(during_call)
            assert not any("MCP tool output" in row["content"] for row in bridge.get_chat())
            assert not bridge.decide_mcp_proposal(proposal["id"], True)["ok"]
            if during_call:
                assert any("Remote outcome unknown" in row["content"] for row in bridge.get_chat())
            runtime.fixture_stream = lambda _: iter([{"choices": [{"delta": {"content": "New turn answer"},
                                                                    "finish_reason": "stop"}]}])
            assert (await asyncio.to_thread(runtime.submit_text, "New local request"))["accepted"]
            assert not bridge.decide_mcp_proposal(proposal["id"], True)["ok"]
            await asyncio.to_thread(produce, runtime)
            await drain(runtime)
            assert any("New turn answer" in row["content"] for row in bridge.get_chat())
        finally:
            await app.router.shutdown()

    asyncio.run(scenario())
