"""Real Light admission/completion interception; fake provider, no live services."""
import asyncio
import json

import pytest

from source.server.status_ui import WebviewBridge
from test_request_reply_ownership import isolated_server, drain, telegram
from test_mcp_light import produce


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
@pytest.mark.parametrize("mode", ["normal", "stop", "cap", "stale", "external", "disabled", "invalid"])
def test_admitted_presentation_respects_owner_generation_and_budget(isolated_server, mode, tmp_path):
    runtime, _ = isolated_server
    bridge = WebviewBridge(runtime.fixture_status, runtime)
    companion = runtime.companion
    companion.path = tmp_path / "companion.json"
    if mode == "disabled":
        assert companion.save({"teaching": False}, companion.preferences()["revision"])["ok"]
    registered=[]

    def provider(params):
        names=[tool["function"]["name"] for tool in params.get("tools",())]
        registered.extend(names)
        if "friday_present" not in names:
            yield {"choices":[{"delta":{"content":"Ordinary fixture reply"},"finish_reason":"stop"}]}
            return
        content = {"id":"fixture-card","kind":"lesson","title":"Actual fake-provider card",
                   "text":"A real admitted result, with inert <script> markup.",
                   "scene":{"type":"mesh","vertices":[[0,0,0],[1,0,0],[0,1,1]],"faces":[[0,1,2]]}}
        if mode == "invalid": content["scene"]={"type":"html","code":"not executed"}
        arguments=json.dumps(content)
        yield {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"friday_present","arguments":arguments[:30]}}]}}]}
        if mode == "stop": bridge.stop_turn()
        elif mode == "cap": runtime.server_state["tool_cap_terminated"]=True
        elif mode == "stale": runtime.server_state["turn_attempt"]=object()
        yield {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":arguments[30:]}}]},"finish_reason":"tool_calls"}]}

    async def scenario():
        await runtime.server.app.router.startup()
        try:
            runtime.fixture_stream=provider
            if mode == "external":
                await telegram(runtime, "101", "Show me a lesson")
                assert runtime.server_state["turn_ownership"].is_external
            else: assert (await asyncio.to_thread(runtime.submit_text,"Show me a 3D teaching model"))["accepted"]
            await asyncio.to_thread(produce,runtime)
            await drain(runtime)
            assert len(companion.snapshot()["rows"]) == (1 if mode == "normal" else 0)
            if mode in ("external","disabled"): assert "friday_present" not in registered
            if mode == "normal":
                from interpreter.core.friday_execution import gate_for
                assert gate_for(runtime).used == 1
                assert bridge.get_turn()["mcp_proposal"] is None
        finally:
            await runtime.server.app.router.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
@pytest.mark.parametrize("mode", ["normal", "stop", "stale", "cap"])
def test_native_highlight_rechecks_before_display_and_stop_clears_exact_mark(isolated_server, tmp_path, monkeypatch, mode):
    from source.server import screen_overlay
    runtime,_=isolated_server
    runtime.companion.path=tmp_path / "preferences.json"
    bridge=WebviewBridge(runtime.fixture_status,runtime)
    calls=[]
    def annotate(**payload):
        calls.append(payload)
        screen_overlay._active={"query":"fixture","deadline":10**12}
        return {"ok":True}
    monkeypatch.setattr(screen_overlay,"annotate",annotate)
    monkeypatch.setattr(screen_overlay,"_active",None)
    monkeypatch.setattr(screen_overlay,"_window",{})
    monkeypatch.setattr(screen_overlay,"_audit",lambda **kw:None)
    def provider(params):
        assert any(t["function"]["name"]=="friday_highlight" for t in params["tools"])
        yield {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"friday_highlight",
            "arguments":json.dumps({"query":"fixture","shape":"arrow","label":"target","points":[[10,20],[100,20]]})}}]}}]}
        if mode=="stop":bridge.stop_turn()
        elif mode=="stale":runtime.server_state["turn_attempt"]=object()
        elif mode=="cap":runtime.server_state["tool_cap_terminated"]=True
        yield {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}
    async def scenario():
        await runtime.server.app.router.startup()
        try:
            runtime.fixture_stream=provider
            assert (await asyncio.to_thread(runtime.submit_text,"Highlight the target"))["accepted"]
            await asyncio.to_thread(produce,runtime)
            await drain(runtime)
            assert len(calls)==(1 if mode=="normal" else 0)
            if mode=="normal":
                assert runtime.companion.overlay is not None
                bridge.stop_turn()
                assert screen_overlay._active is None
                assert runtime.companion.overlay is None
        finally:await runtime.server.app.router.shutdown()
    asyncio.run(scenario())
