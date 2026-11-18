"""Synthetic n8n transport attached to the real WebviewBridge."""
from pathlib import Path
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tools/n8n_acceptance"))
from offline_peer import OfflineN8N
from source.server.n8n_bridge import Bridge,Config
from source.server.n8n_turn import WorkflowTurn,Request
import time

class N8NData:
    def __init__(self, area, runtime, bridge):
        self.area,self.runtime,self.bridge=area,runtime,bridge
        self.peer=OfflineN8N("synthetic_"+"0"*40)
        runtime.n8n_bridge=Bridge(Config(area/"n8n.json"),http=self.peer,session="ui-session")
        self.controller=WorkflowTurn(runtime.n8n_bridge)
        runtime.server_state["n8n_turn"]=self.controller
        runtime.decide_workflow_request=lambda id,choice:self.controller.decide(id,choice,lambda item:True)
        self.fail_write=self.fail_read=False
    def snapshot(self):
        if self.fail_read:raise OSError("PRIVATE")
        return self.bridge.get_n8n()
    def save(self, changes, revision):
        if self.fail_write:
            with patch("source.server.n8n_bridge.os.replace",side_effect=OSError("PRIVATE")):
                result = self.bridge.set_n8n(changes,revision)
        else:
            result = self.bridge.set_n8n(changes,revision)
        if result["ok"]:
            self.peer.client=self.runtime.n8n_bridge.config.read()["client_id"]
        return result
    def control(self, command):
        action=command["action"]
        if action=="n8n:read":pass
        elif action=="n8n:proposal":
            self.controller.pending=Request("fixture-request",object(),object(),
                {"capability":"chat","message":"Synthetic <script>inert</script> request"},time.monotonic()+45)
        elif action=="n8n:fail-write":self.fail_write=command["value"]
        elif action=="n8n:fail-read":self.fail_read=command["value"]
        elif action=="n8n:external":self.runtime.n8n_bridge.save({"notifications":True},self.runtime.n8n_bridge.config.read()["revision"])
        elif action=="n8n:reopen":
            self.runtime.n8n_bridge=Bridge(Config(self.area/"n8n.json"),http=self.peer,session="ui-session")
            self.controller.bridge=self.runtime.n8n_bridge
        return {"connections":len(self.peer.calls),"operations":self.peer.calls,
                "saved":(self.area/"n8n.json").exists(),
                "revision":self.runtime.n8n_bridge.config.read()["revision"]}
