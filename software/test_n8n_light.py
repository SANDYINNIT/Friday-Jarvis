"""Actual Light proposal/admission/owner/budget/Stop path, fake n8n/provider."""
import asyncio,json
import pytest
from source.server import n8n_bridge as n,status_ui
from test_mcp_light import produce
from test_mcp_runtime import wait_until
from test_request_reply_ownership import drain,isolated_server
from tools.n8n_acceptance.offline_peer import OfflineN8N

@pytest.fixture
def workflow_connection(tmp_path,monkeypatch):
    cfg=n.Config(tmp_path/"n8n.json")
    key="synthetic_"+"0"*40
    value=cfg.save({"deployment":"https://automation.invalid","secret":key,
                  "enabled":True,"capabilities":["chat"],"notifications":False},0)
    peer=OfflineN8N(key);peer.client=value["client_id"]
    def attach(runtime):
        if getattr(runtime,"n8n_bridge",None) is None:
            runtime.n8n_bridge=n.Bridge(cfg,http=peer,session="actual-light-fixture")
        return runtime.n8n_bridge
    monkeypatch.setattr(n,"for_runtime",attach)
    return peer

def stream(params):
    defs=[t["function"] for t in params.get("tools",()) if t["function"]["name"]=="friday_workflow_request"]
    assert len(defs)==1
    args=json.dumps({"capability":"chat","message":"Explicit synthetic workflow request"})
    yield {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"friday_workflow_request","arguments":args}}]}}]}
    yield {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}

@pytest.mark.parametrize("isolated_server",["mcp"],indirect=True)
@pytest.mark.parametrize("mode",["send","cancel","stop","budget","generation","timeout","late"])
def test_actual_light_workflow_request(workflow_connection,isolated_server,mode):
    runtime,_=isolated_server
    peer=workflow_connection
    bridge=status_ui.WebviewBridge(runtime.fixture_status,runtime)
    runtime.fixture_stream=stream
    async def scenario():
        await runtime.server.app.router.startup()
        try:
            assert not peer.calls
            assert (await asyncio.to_thread(runtime.submit_text,"Use the explicitly selected n8n fixture"))["accepted"]
            worker=asyncio.create_task(asyncio.to_thread(produce,runtime))
            await wait_until(lambda:bridge.get_n8n()["proposal"] is not None)
            pending=bridge.get_n8n()["proposal"]
            assert bridge.get_turn()["n8n_request_pending"]
            assert not bridge.decide_n8n_request("wrong","send")["ok"]
            assert not bridge.decide_n8n_request(pending["id"],True)["ok"]
            if mode=="stop":bridge.stop_turn()
            elif mode=="generation":runtime.server_state["turn_attempt"]=object()
            else:
                if mode=="budget":
                    from interpreter.core.friday_execution import gate_for
                    gate=gate_for(runtime);gate.budget_owner=runtime.server_state["turn_ownership"];gate.used=gate.limit
                if mode=="timeout":peer.fail=True
                if mode=="late":peer.before=lambda:runtime.server_state.__setitem__("turn_attempt",object())
                assert bridge.decide_n8n_request(pending["id"],"cancel" if mode=="cancel" else "send")["ok"]
                assert not bridge.decide_n8n_request(pending["id"],"send")["ok"]
            await asyncio.wait_for(worker,timeout=5)
            await drain(runtime)
            assert peer.calls==(["request"] if mode in ("send","timeout","late") else [])
            assert not peer.actions
            replies=[row for row in bridge.get_chat() if "n8n reply (untrusted)" in row["content"]]
            assert bool(replies)==(mode=="send")
            if replies:assert "<script>inert</script>" in replies[0]["content"]
            assert bridge.get_n8n()["proposal"] is None
        finally:
            await runtime.server.app.router.shutdown()
    asyncio.run(scenario())
