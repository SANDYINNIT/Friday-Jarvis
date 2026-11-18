"""Focused real-client/config/store/turn tests; synthetic state, no live network."""
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from source.server import n8n_bridge as n
from source.server.n8n_turn import WorkflowTurn, event_loop
spec = importlib.util.spec_from_file_location("offline_peer", ROOT/"tools/n8n_acceptance/offline_peer.py")
peer_module = importlib.util.module_from_spec(spec);spec.loader.exec_module(peer_module)
Peer = peer_module.OfflineN8N

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="friday-n8n-", dir=os.environ.get("FRIDAY_TEST_SCRATCH"))
        self.root = Path(self.tmp.name)
        self.config = n.Config(self.root/"n8n.json")
        self.secret = "synthetic_"+"0"*40
        self.peer = Peer(self.secret)
        self.bridge = n.Bridge(self.config, http=self.peer, session="session-current")
    def tearDown(self):
        self.tmp.cleanup()
    def save(self, **kwargs):
        result = self.bridge.save({"deployment":"https://automation.invalid","secret":self.secret,
                                 "enabled":True,"capabilities":["chat","gmail.send"],"notifications":True,**kwargs},0)
        if result["ok"]:self.peer.client=self.config.read()["client_id"]
        return result
    def request(self, **kwargs):
        return self.bridge.operate("request", {"capability":"chat","message":"synthetic request",
            "turn_id":"turn-1","generation":"generation-1"}, **kwargs)
    def event(self, seq=1, kind="notice", **kwargs):
        value=self.config.read()
        p={"sequence":seq,"type":kind,"text":"synthetic notice <script>inert</script>"}
        if kind=="decision":
            action={"action":"gmail.send","arguments":{"to":"test@example.invalid","subject":"synthetic"}}
            p={"sequence":seq,"type":kind,**action,"token":"testtoken_"+"b"*40,
               "action_digest":hashlib.sha256(n.encoded(action)).hexdigest()}
        return n.envelope("event",value["client_id"],self.bridge.session,p,**kwargs)
    def test_import_snapshot_and_empty_inbox_create_nothing(self):
        self.assertTrue(self.bridge.snapshot()["ok"]);self.assertEqual(self.bridge.inbox()["events"],[])
        self.assertEqual(list(self.root.iterdir()),[]);self.assertFalse(self.peer.calls)
    def test_save_is_passive_and_reopen_preserves_identity(self):
        self.assertTrue(self.save()["ok"]);self.assertFalse(self.peer.calls)
        other=n.Config(self.config.path).read()
        self.assertEqual(other["revision"],1);self.assertTrue(other["client_id"])
        self.assertNotIn(self.secret,json.dumps(self.bridge.snapshot()))
        self.assertNotIn("automation.invalid",json.dumps(self.bridge.snapshot()))
    def test_unknown_setting_has_no_file_write(self):
        result=self.bridge.save({"approved":True},0)
        self.assertFalse(result["ok"]);self.assertFalse(self.config.path.exists())
    def test_enabled_requires_all_fields(self):
        self.assertFalse(self.bridge.save({"enabled":True},0)["ok"])
        self.assertFalse(self.config.path.exists())
    def test_invalid_auth_endpoint_and_capability_refused(self):
        for changes in ({"secret":"short"},{"deployment":"https://user:pw@host.invalid"},
                        {"deployment":"http://remote.invalid"},{"capabilities":["unknown"]}):
            self.assertFalse(self.bridge.save(changes,0)["ok"])
    def test_revision_conflict_does_not_clobber(self):
        self.save();before=self.config.path.read_bytes()
        self.assertFalse(self.bridge.save({"enabled":False},0)["ok"])
        self.assertEqual(before,self.config.path.read_bytes())
    def test_failed_replace_preserves_previous_file(self):
        self.save();before=self.config.path.read_bytes()
        with patch.object(n.os,"replace",side_effect=OSError("PRIVATE")):
            result=self.bridge.save({"enabled":False},1)
        self.assertFalse(result["ok"]);self.assertEqual(before,self.config.path.read_bytes())
        self.assertNotIn("PRIVATE",json.dumps(result))
    def test_malformed_existing_config_not_repaired(self):
        self.config.path.write_text("{");before=self.config.path.read_bytes()
        self.assertFalse(self.bridge.save({"enabled":False},0)["ok"])
        self.assertEqual(before,self.config.path.read_bytes())
    def test_rotation_and_removal_do_not_connect(self):
        self.save();self.assertTrue(self.bridge.save({"secret":"new_"+"z"*40},1)["ok"])
        self.assertTrue(self.bridge.save({"remove_secret":True},2)["ok"])
        self.assertFalse(self.config.read()["enabled"]);self.assertFalse(self.config.read()["secret"])
        self.assertFalse(self.peer.calls)
    def test_status_test_can_run_while_requests_disabled(self):
        self.save(enabled=False)
        self.assertTrue(self.bridge.operate("status",{})["ok"])
        self.assertEqual(self.request()["outcome"],"not_dispatched");self.assertEqual(self.peer.calls,["status"])
    def test_unselected_capability_refused(self):
        self.save(capabilities=["chat"])
        result=self.bridge.operate("request",{"capability":"gmail.send","message":"x","turn_id":"a","generation":"b"})
        self.assertFalse(result["ok"]);self.assertFalse(self.peer.calls)
    def test_accepted_is_not_completed(self):
        self.save();r=self.request()
        self.assertTrue(r["ok"]);self.assertFalse(r["workflow_completion"])
    def test_partial_is_not_completed(self):
        self.save();self.peer.outcome="partial"
        r=self.request();self.assertEqual(r["outcome"],"partial");self.assertFalse(r["workflow_completion"])
    def test_remote_completed_is_explicit_report_not_action_proof(self):
        self.save();self.peer.outcome="completed";r=self.request()
        self.assertTrue(r["workflow_completion"]);self.assertFalse(self.peer.actions)
    def test_duplicate_request_id_never_repeats_dispatch(self):
        self.save();self.request(request_id="same")
        r=self.request(request_id="same")
        self.assertEqual(r["error"],"already_dispatched");self.assertEqual(len(self.peer.calls),1)
    def test_timeout_is_unknown_no_automatic_repeat(self):
        self.save();self.peer.fail=True
        r=self.request(request_id="timeout");self.assertEqual(r["outcome"],"unknown");self.assertEqual(r["request_id"],"timeout")
        self.assertNotIn("SECRET",json.dumps(r));self.request(request_id="timeout")
        self.assertEqual(len(self.peer.calls),1)
    def test_authentication_error_not_success(self):
        self.save();self.peer.status=403
        r=self.request();self.assertEqual(r["error"],"authentication_failed");self.assertEqual(r["outcome"],"unknown")
    def test_invalid_signature_rejected(self):
        self.save()
        def corrupt(body):
            body["payload"]["extra"]="changed";return body
        # Signature generated by response matches changed body; simulate invalid header after signing.
        original=self.peer.__call__
        def http(*a,**kw):
            response=original(*a,**kw);response.headers["X-Friday-Signature"]="0"*64;return response
        self.bridge.http=http;self.assertEqual(self.request()["error"],"authentication_failed")
    def test_wrong_response_account_client_and_session_refused(self):
        self.save()
        for field,value in (("account_id","other"),("client_id","other"),("session_id","old")):
            self.peer.transform=lambda body,f=field,v=value:{**body,f:v}
            self.assertEqual(self.request()["error"],"wrong_owner")
    def test_stop_before_dispatch_spends_nothing(self):
        self.save();spent=[]
        r=self.request(live=lambda:False,spend=lambda:spent.append(1))
        self.assertEqual(r["outcome"],"not_dispatched");self.assertFalse(spent);self.assertFalse(self.peer.calls)
    def test_tool_budget_refused_before_dispatch(self):
        self.save();r=self.request(spend=lambda:False)
        self.assertEqual(r["outcome"],"not_dispatched");self.assertFalse(self.peer.calls)
    def test_newer_session_or_stop_rejects_late_response(self):
        self.save();self.peer.before=self.bridge.invalidate
        r=self.request();self.assertEqual(r["outcome"],"unknown");self.assertEqual(r["error"],"stale_remote_outcome")
    def test_response_oversized_refused(self):
        self.save();self.peer.transform=lambda body:{**body,"payload":{"outcome":"completed","data":"x"*70000}}
        self.assertEqual(self.request()["error"],"response_too_large")
    def test_expiry_and_timestamp_validation(self):
        self.save();event=self.event(now=time.time()-200)
        with self.assertRaises(n.Refused):n.validate_envelope(event,client=self.config.read()["client_id"])
        for v in (float("nan"),float("inf")):
            event=self.event();event["expires_at"]=v
            with self.assertRaises(n.Refused):n.validate_envelope(event,client=self.config.read()["client_id"])
    def test_duplicate_keys_and_non_finite_json_rejected(self):
        for raw in ('{"id":1,"id":2}','{"x":NaN}'):
            with self.assertRaises(n.Refused):n.literal(raw)
    def test_pull_persists_before_ack_and_duplicate_is_inert(self):
        self.save();self.peer.events=[self.event()]
        self.assertTrue(self.bridge.pull()["ok"]);self.assertEqual(self.peer.receipts,{self.peer.events[0]["id"]})
        self.assertTrue(self.bridge.pull()["ok"]);self.assertEqual(len(self.bridge.inbox()["events"]),1)
        self.assertFalse(self.peer.actions)
    def test_notice_previews_do_not_execute_markup(self):
        self.save();self.peer.events=[self.event()];self.bridge.pull()
        self.assertIn("<script>",self.bridge.inbox()["events"][0]["text"])
        self.assertFalse(self.peer.actions)
    def test_same_event_id_changed_content_refused(self):
        self.save();event=self.event();self.peer.events=[event];self.bridge.pull()
        self.peer.events[0]["payload"]["text"]="changed"
        self.assertEqual(self.bridge.pull()["error"],"event_id_conflict")
    def test_out_of_order_batch_rolls_back_all_new_events(self):
        self.save();self.peer.events=[self.event(2),self.event(1)]
        self.assertEqual(self.bridge.pull()["error"],"out_of_order")
        self.assertEqual(self.bridge.inbox()["events"],[]);self.assertFalse(self.peer.receipts)
    def test_cross_account_event_refused_and_not_acked(self):
        self.save();event=self.event();event["account_id"]="guest";self.peer.events=[event]
        self.assertFalse(self.bridge.pull()["ok"]);self.assertFalse(self.peer.receipts)
    def test_stale_session_event_refused(self):
        self.save();event=self.event();event["session_id"]="old";self.peer.events=[event]
        self.assertEqual(self.bridge.pull()["error"],"stale_session")
    def test_decision_requires_exact_hash_and_consumes_once(self):
        self.save();event=self.event(kind="decision");self.peer.events=[event];self.bridge.pull()
        preview=self.bridge.decision_preview(event["id"])
        self.assertNotIn("token",preview)
        self.assertFalse(self.bridge.decide(event["id"],"wrong","approve")["ok"])
        self.assertTrue(self.bridge.decide(event["id"],preview["digest"],"approve")["ok"])
        self.assertEqual(len(self.peer.actions),1)
        self.assertEqual(self.bridge.inbox()["events"][0]["state"],"decision_received")
        self.assertFalse(self.bridge.decide(event["id"],preview["digest"],"approve")["ok"])
        self.assertEqual(len(self.peer.actions),1)
    def test_rejection_never_runs_action(self):
        self.save();event=self.event(kind="decision");self.peer.events=[event];self.bridge.pull()
        self.assertTrue(self.bridge.decide(event["id"],event["payload"]["action_digest"],"reject")["ok"])
        self.assertFalse(self.peer.actions)
    def test_bare_approved_boolean_not_a_decision(self):
        self.save();event=self.event(kind="decision");self.peer.events=[event];self.bridge.pull()
        self.assertFalse(self.bridge.decide(event["id"],event["payload"]["action_digest"],True)["ok"])
        self.assertFalse(self.peer.actions)
    def test_reopen_old_decision_not_transferred_to_new_session(self):
        self.save();event=self.event(kind="decision");self.peer.events=[event];self.bridge.pull()
        other=n.Bridge(n.Config(self.config.path),http=self.peer,session="replacement")
        self.assertFalse(other.decide(event["id"],event["payload"]["action_digest"],"approve")["ok"])
    def test_notifications_off_does_not_pull(self):
        self.save(notifications=False)
        self.assertFalse(self.bridge.pull()["ok"]);self.assertFalse(self.peer.calls)
    def test_observation_becomes_stale(self):
        self.save();self.bridge.operate("status",{})
        self.bridge.clock=lambda:time.time()+70
        self.assertEqual(self.bridge.snapshot()["observation"]["state"],"stale")

    def test_concurrent_config_writer_refused_without_partial_write(self):
        self.save();prior=self.config.path.read_bytes()
        lock=self.config.path.with_name("n8n.json.write-lock");lock.write_text("owned")
        try:
            self.assertEqual(self.bridge.save({"enabled":False},1)["error"],"configuration_busy")
            self.assertEqual(self.config.path.read_bytes(),prior)
        finally:lock.unlink()
    def test_ack_failure_keeps_durable_event_for_later_receipt(self):
        self.save();self.peer.events=[self.event()]
        original=self.peer.__call__
        def http(url,**kw):
            if url.endswith("/ack"):raise TimeoutError("synthetic")
            return original(url,**kw)
        self.bridge.http=http;r=self.bridge.pull()
        self.assertTrue(r["ok"]);self.assertFalse(r["receipt"]["ok"])
        self.assertEqual(len(self.bridge.inbox()["events"]),1);self.assertFalse(self.peer.receipts)
        self.bridge.http=self.peer;self.assertTrue(self.bridge.pull()["receipt"]["ok"])
        self.assertEqual(len(self.bridge.inbox()["events"]),1)
    def test_storage_failure_prevents_remote_dispatch(self):
        self.save()
        with patch.object(self.bridge.receipts,"write",side_effect=OSError("PRIVATE")):
            self.assertEqual(self.request()["outcome"],"not_dispatched")
        self.assertFalse(self.peer.calls)
    def test_identity_mismatch_existing_request_id_not_replayed(self):
        self.save();self.request(request_id="exact")
        result=self.bridge.operate("request",{"capability":"chat","message":"different","turn_id":"turn-1","generation":"generation-1"},request_id="exact")
        self.assertEqual(result["error"],"id_conflict");self.assertEqual(len(self.peer.calls),1)

    def test_disabled_permission_or_stop_cannot_approve_saved_event(self):
        self.save();event=self.event(kind="decision");self.peer.events=[event];self.bridge.pull()
        digest=event["payload"]["action_digest"]
        self.bridge.runtime_live=lambda:False
        self.assertFalse(self.bridge.decide(event["id"],digest,"approve")["ok"])
        self.bridge.runtime_live=lambda:True
        self.assertTrue(self.bridge.save({"capabilities":["chat"]},1)["ok"])
        self.assertEqual(self.bridge.decide(event["id"],digest,"approve")["error"],"capability_not_allowed")
        self.assertFalse(self.peer.actions)
    def test_remote_approved_boolean_request_does_not_create_permission(self):
        self.save()
        result=self.bridge.operate("request",{"capability":"gmail.send","message":"send","turn_id":"a","generation":"b","approved":True})
        self.assertEqual(result["error"],"unsupported_arguments")
        self.assertFalse(self.peer.calls)
    def test_public_operation_never_exposes_remote_sensitive_payload(self):
        response={"ok":True,"outcome":"ready","result":{"secret":"PRIVATE","endpoint":"https://private.invalid"}}
        self.assertNotIn("PRIVATE",json.dumps(n.public_operation(response)))
        self.assertNotIn("private.invalid",json.dumps(n.public_operation(response)))

    def test_sensitive_decision_arguments_refused_not_masked_for_approval(self):
        self.save();event=self.event(kind="decision")
        event["payload"]["arguments"]["endpoint"]="https://private.invalid"
        action={k:event["payload"][k] for k in ("action","arguments")}
        event["payload"]["action_digest"]=hashlib.sha256(n.encoded(action)).hexdigest()
        self.peer.events=[event];self.bridge.pull()
        self.assertFalse(self.bridge.decide(event["id"],event["payload"]["action_digest"],"approve")["ok"])
        self.assertFalse(self.peer.actions)
    def test_notice_never_echoes_shared_credential(self):
        self.save();event=self.event();event["payload"]["text"]=self.secret
        self.peer.events=[event];self.bridge.pull()
        self.assertNotIn(self.secret,json.dumps(self.bridge.inbox()))

class IntegrationTests(unittest.TestCase):
    def test_native_definition_and_parse_use_exact_workflow_domain(self):
        from source.server.mcp_turn import ModelTools
        from source.server.n8n_turn import WORKFLOW
        params={"tools":[]}
        tool=ModelTools(None,params,workflow=["chat"])
        definition=params["tools"][0]["function"]
        self.assertEqual(definition["name"],"friday_workflow_request")
        self.assertEqual(definition["parameters"]["properties"]["capability"]["enum"],["chat"])
        self.assertNotIn("approved",definition["parameters"]["properties"])
        self.assertEqual(tool.filter({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{
          "name":"friday_workflow_request","arguments":'{"capability":"chat","message":"test"}'}}]},
          "finish_reason":"tool_calls"}]}),[])
        self.assertEqual(tool.proposal(),(WORKFLOW,{"capability":"chat","message":"test"}))
    def test_unconfigured_does_not_register_tool(self):
        from source.server.mcp_turn import ModelTools
        params={"tools":[]};self.assertFalse(ModelTools(None,params).refs)
        self.assertEqual(params["tools"],[])
    def test_actual_light_live_guard_refuses_external_stop_old_generation_and_grounded(self):
        import ast, threading
        from types import SimpleNamespace
        tree=ast.parse((ROOT/"source/server/server.py").read_text(encoding="utf-8"))
        nodes={node.name:node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name in ("mcp_live","spend_mcp")}
        own=SimpleNamespace(is_external=False);generation=object()
        state={"turn_attempt":generation,"turn_started_at":time.monotonic()}
        stop=threading.Event()
        env={"execution_gate":object(),"_ownership":lambda _:own,"server_state":state,
             "interpreter":SimpleNamespace(stop_event=stop),"time":time}
        exec(compile(ast.Module(body=[nodes["mcp_live"]],type_ignores=[]),"<actual-Light-guard>","exec"),env)
        item=SimpleNamespace(owner=own,generation=generation,deadline=time.monotonic()+5)
        self.assertTrue(env["mcp_live"](item))
        for key,value in (("cancelled_owner",own),("tool_cap_terminated",True),("turn_budget_enforced",True),("knowledge_turn",object()),("turn_attempt",object())):
            before=state.get(key);state[key]=value;self.assertFalse(env["mcp_live"](item));state[key]=before
        state["turn_attempt"]=generation;stop.set();self.assertFalse(env["mcp_live"](item));stop.clear()
        own.is_external=True;self.assertFalse(env["mcp_live"](item))
    def test_actual_light_tool_budget_refuses_unbound_owner_and_exhaustion(self):
        import ast,threading
        from types import SimpleNamespace
        tree=ast.parse((ROOT/"source/server/server.py").read_text(encoding="utf-8"))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="spend_mcp")
        owner,generation=object(),object();worker=SimpleNamespace(owner=owner,attempt=generation,stopped=False)
        gate=SimpleNamespace(bound=worker,owns=lambda w:True,budget_owner=owner,used=0)
        item=SimpleNamespace(owner=owner,generation=generation)
        env={"execution_lock":threading.RLock(),"mcp_live":lambda _:True,"execution_gate":gate,
             "HARD_TOOL_CAP":2,"tool_counter":{"n":0},"_stamp_progress":lambda _:None,"server_state":{}}
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<actual-Light-budget>","exec"),env)
        self.assertTrue(env["spend_mcp"](item));self.assertTrue(env["spend_mcp"](item))
        self.assertFalse(env["spend_mcp"](item));self.assertEqual(gate.used,2)
        gate.used=0;env["tool_counter"]["n"]=0;worker.attempt=object()
        self.assertFalse(env["spend_mcp"](item))

class SetupTests(unittest.TestCase):
    def test_private_cli_show_is_passive(self):
        import contextlib,io
        import n8n_setup
        with tempfile.TemporaryDirectory(prefix="friday-n8n-cli-",dir=os.environ.get("FRIDAY_TEST_SCRATCH")) as area:
            path=Path(area)/"n8n.json"
            with patch.dict(os.environ,{"FRIDAY_N8N_CONFIG":str(path)}):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(n8n_setup.main(["show"]),0)
                self.assertIn('"enabled": false',output.getvalue())
                self.assertEqual(list(Path(area).iterdir()),[])
    def test_private_prompt_refuses_noninteractive_input(self):
        import n8n_setup
        with patch.object(n8n_setup.sys.stdin,"isatty",return_value=False):
            with self.assertRaises(n.Refused):n8n_setup.hidden("not echoed")
        import warnings
        def echo_fallback(prompt):
            warnings.warn("would echo",n8n_setup.getpass.GetPassWarning)
            return "must not return"
        with patch.object(n8n_setup.sys.stdin,"isatty",return_value=True), patch.object(n8n_setup.sys.stderr,"isatty",return_value=True), patch.object(n8n_setup.getpass,"getpass",side_effect=echo_fallback):
            with self.assertRaises(n.Refused):n8n_setup.hidden("not echoed")
    def test_unknown_cli_argument_never_echoes_value(self):
        import contextlib,io,n8n_setup
        with contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit):n8n_setup.main(["show","PRIVATE_SECRET"])
        self.assertNotIn("PRIVATE_SECRET",output.getvalue())

class LegacyBoundaryTests(unittest.TestCase):
    def test_v2_config_cannot_use_unguarded_legacy_dispatch(self):
        from source.server.n8n_runtime import load_config,N8NAdapter
        with tempfile.TemporaryDirectory(prefix="friday-n8n-legacy-",dir=os.environ.get("FRIDAY_TEST_SCRATCH")) as area:
            config=n.Config(Path(area)/"n8n.json");value=n.default()
            config.path.write_bytes(n.encoded(value))
            legacy=load_config(environ={},path=str(config.path))
            self.assertFalse(legacy["enabled"])
            adapter=N8NAdapter(config=legacy,http_post=lambda *a,**k:self.fail("unguarded dispatch"))
            self.assertEqual(adapter.call("send_email",{"approved":True})["error"],"guarded_request_required")
            self.assertIn("exact local",adapter.connection_help())

class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_approval_dispatches_with_shared_live_spend(self):
        class Fake:
            def operate(self,*a,**kw):
                if kw["live"]() and kw["spend"]():return {"ok":True,"outcome":"accepted"}
                return {"ok":False}
        t=WorkflowTurn(Fake());owner,generation=object(),object();spent=[]
        live=lambda item:item.owner is owner and item.generation is generation
        task=asyncio.create_task(t.execute(owner,generation,{"capability":"chat","message":"test"},time.monotonic()+5,live,lambda item:spent.append(1) or True))
        await asyncio.sleep(.01);p=t.preview()
        self.assertFalse(t.decide("wrong","send",live)["ok"])
        self.assertTrue(t.decide(p["id"],"send",live)["ok"])
        self.assertFalse(t.decide(p["id"],"send",live)["ok"])
        self.assertTrue((await task)["ok"]);self.assertEqual(spent,[1])
    async def test_stop_while_waiting_never_dispatches(self):
        class Fake:
            def operate(self,*a,**kw):raise AssertionError("must not dispatch")
        t=WorkflowTurn(Fake());ok=True
        task=asyncio.create_task(t.execute(object(),object(),{"capability":"chat","message":"test"},time.monotonic()+5,lambda _:ok,lambda _:True))
        await asyncio.sleep(.01);ok=False
        self.assertEqual((await task)["error"],"stopped_or_expired")
    async def test_no_model_boolean_can_grant_request(self):
        t=WorkflowTurn(None)
        self.assertFalse(t.decide("wrong",True,lambda _:True)["ok"])
    async def test_disabled_event_lifecycle_has_zero_calls(self):
        class Fake:
            config=type("Config",(),{"read":lambda _:{"enabled":False,"notifications":False}})()
            def pull(self):raise AssertionError("disabled must not call")
        stopped=asyncio.Event();task=asyncio.create_task(event_loop(Fake(),stopped))
        await asyncio.sleep(.01);stopped.set();await task

if __name__=="__main__":unittest.main(verbosity=2)
