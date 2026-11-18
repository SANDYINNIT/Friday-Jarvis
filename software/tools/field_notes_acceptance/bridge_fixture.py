"""Real HUD chat/Stop bridge with synthetic in-memory stores and fake transports."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

root = Path(__file__).resolve().parents[2]
n8n_mode = "n8n" in sys.argv[2:]
completion_records_mode = "completion-records" in sys.argv[2:]
town_life_mode = "town-life" in sys.argv[2:]
town_install_mode = "town-install" in sys.argv[2:]
town_mode = town_life_mode or "town" in sys.argv[2:]
roster_controls_mode = "roster-controls" in sys.argv[2:]
roster_mode = town_mode or town_install_mode or roster_controls_mode or "roster" in sys.argv[2:]
planner_mode = "planner" in sys.argv[2:]
knowledge_ingest_mode = "knowledge-ingest" in sys.argv[2:]
library_mode = knowledge_ingest_mode or "library" in sys.argv[2:]
archive_mode = "archive" in sys.argv[2:]
voice_mode = "voice" in sys.argv[2:]
readiness_mode = "rollout-readiness" in sys.argv[2:]
routing_mode = readiness_mode or "routing" in sys.argv[2:]
capability_mode = "capabilities" in sys.argv[2:]
workflow_mode = "workflows" in sys.argv[2:]
settings_mode = "settings" in sys.argv[2:]
worker_mode = "worker-provenance" in sys.argv[2:]
workspace_mode = planner_mode or worker_mode or "workspace" in sys.argv[2:]
mcp_approval_mode = "mcp-approval" in sys.argv[2:]
companion_mode = "companion" in sys.argv[2:]
area = (root / ".venv" / "field-notes-ui-check" / sys.argv[1]).resolve()
assert area.is_relative_to(root / ".venv" / "field-notes-ui-check")
area.mkdir(parents=True)
for name in ("HOME", "USERPROFILE", "TEMP", "TMP"):
    os.environ[name] = str(area)
for name in ("FRIDAY_IDENTITY_READER", "FRIDAY_IDENTITY_INTEGRATION"):
    os.environ[name] = "0"
if routing_mode:
    os.environ["FRIDAY_MODAL_BASE"] = "https://fixture-private.invalid/v1"
    os.environ["FRIDAY_CREDENTIALS_FILE"] = str(area / "unused-credentials.json")
    os.environ.pop("SSLKEYLOGFILE", None)
sys.path.insert(0, str(root))


def guard(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
        raise RuntimeError("offline shipped UI fixture")


sys.addaudithook(guard)
# These background adapters are outside the chat boundary being exercised.
sys.modules["source.server.lab_authority"] = SimpleNamespace(shared=lambda: None)
if not town_life_mode:
    sys.modules["source.server.lab_budget"] = SimpleNamespace(shared=lambda: None)
if not roster_mode and not workspace_mode and not routing_mode:
    sys.modules["source.server.lab_state"] = SimpleNamespace(
        ensure_started=lambda: False, note=lambda *args: None, cancel_execution=lambda *args: None,
        capture_identity_world=lambda: None, capture_town_view=lambda: (None,None,False))
sys.modules["source.server.server"] = SimpleNamespace(_pending_queue=lambda state: runtime.queued)
from source.server.status_ui import StatusBus, WebviewBridge

bus = StatusBus()
bus.chat.append("user", "Help me review the plan for this afternoon.")
bus.chat.append("assistant", "We can work through it here. What would you like to start with?")
calls = []


class Runtime:
    def __init__(self):
        self.server_state = {"is_paused": False}
        self.active = False
        self.queued = []
        self.mode = "accept"
        self.chat_error = False
        self.status_error = False
        self.last_served = "brain_fixture/fixture-previous-model"
        self.selected_only = False
        self.cancel_mode = "accept"
        self.respond_thread = SimpleNamespace(is_alive=lambda: self.active)
        self.voice_queue = FixtureVoiceQueue()
        self.output_queue = SimpleNamespace(sync_q=self.voice_queue)

    def submit_text(self, text):
        calls.append(["submit_text", text])
        if self.mode == "reject":
            return {"accepted": False, "message": "Fixture admission refused. Your draft is kept."}
        if self.mode == "throw":
            raise RuntimeError("synthetic transport failure")
        if self.mode == "unconfirmed":
            return {}
        queued = self.active
        self.active = True
        if queued:
            self.queued.append(text)
        bus.chat.append("user", text, status="queued" if queued else "complete")
        bus.publish("thinking", "Working on the current turn")
        return {"accepted": True, "status": "queued" if queued else "accepted"}

    def cancel_owned_turn(self, reason):
        calls.append(["cancel_owned_turn", reason])
        if self.cancel_mode == "throw":
            raise RuntimeError("synthetic cancellation boundary failure")
        active, queued = self.active, len(self.queued)
        self.active = False
        self.queued.clear()
        bus.chat.finalize_streaming()
        bus.publish("idle", "Stopped by user")
        return active, queued


class FixtureVoiceQueue:
    def __init__(self):
        self.frames = []
        self.reject = False

    def put_nowait(self, frame):
        if self.reject:
            raise RuntimeError("fixture queue rejected setting")
        self.frames.append(frame)


runtime = Runtime()
bridge = WebviewBridge(bus, runtime)
n8n_data = None
if n8n_mode:
    from n8n_data import N8NData
    n8n_data = N8NData(area,runtime,bridge)
if companion_mode:
    from source.server.companion import Companion
    runtime.companion = Companion(area / "companion.json")
    companion_owner, companion_generation = object(), object()
if mcp_approval_mode:
    pending_mcp = {"id": "fixture-proposal", "tool": "studio:read",
                   "arguments": '{"item":"<img src=x onerror=bad()>"}'}
    runtime.server_state["mcp_turn"] = SimpleNamespace(preview=lambda: pending_mcp or None)

    def decide_mcp(proposal_id, approve):
        if proposal_id != pending_mcp.get("id") or approve is not True:
            return {"ok": False}
        pending_mcp.clear()
        return {"ok": True}

    runtime.decide_mcp_proposal = decide_mcp
if archive_mode:
    from source.server.light_archive import ConversationArchive

    writer = ConversationArchive(area / "synthetic-light.db")
    owner, generation = object(), object()
    writer.admit(owner, generation, "text", "Review the archived fixture")
    writer.turn.checkpoint_at = 0
    writer.append(owner, generation, object(),
                  "Saved fragment <img src=x onerror='window.archiveRan=true'> visible now. ")
    runtime.light_archive = ConversationArchive(writer.path)
    runtime.light_archive.recover()
roster_data = None
workspace_data = None
planner_data = None
library_data = None
voice_data = None
routing_data = None
capability_data = None
workflow_data = None
settings_data = None
if roster_mode:
    from roster_data import RosterData
    roster_data = RosterData(area,runtime,bridge,installed=town_mode, controls=roster_controls_mode)
    if town_install_mode:
        from source.server.identity_integration import IdentityIntegration

        roster_data.world.paused = True
        assert roster_data.store.save(roster_data.world)["ok"]
        runtime.server_state["identity_integration"] = IdentityIntegration(roster_data.catalog, roster_data.shadow, {})
        os.environ["FRIDAY_IDENTITY_INTEGRATION"] = "1"
        os.environ["FRIDAY_TOWN_INSTALL_OPERATOR"] = "1"
    if town_life_mode:
        from source.server import lab_budget, lab_state
        from source.server.identity_integration import IdentityIntegration

        routes = (roster_data.primary, roster_data.fallback, roster_data.local)
        integration = IdentityIntegration(roster_data.catalog, roster_data.shadow, {
            ("brain", route.deployment): ("http://fixture.invalid", route.provider,
                route.deployment, {route.model}) for route in routes})
        runtime.server_state["identity_integration"] = integration
        lab_budget.configure(area / "source-world.db")
        lab_state.configure_background_life(integration, lambda: runtime.active)
        def never_plan(*_):
            raise AssertionError("A paused Town must not dispatch")
        lab_state.background_life().planner = never_plan
if workspace_mode:
    from workspace_data import WorkspaceData
    workspace_data = WorkspaceData(area,bridge)
    if worker_mode:
        workspace_data.historical_result(area, runtime)
if planner_mode:
    from planner_data import PlannerData
    planner_data = PlannerData(area,runtime,bridge)
if library_mode:
    from library_data import LibraryData
    library_data = LibraryData(area, runtime, bridge)
if knowledge_ingest_mode:
    os.environ["FRIDAY_KNOWLEDGE_DB"] = str(area / "knowledge.db")
if voice_mode:
    from voice_data import VoiceData
    voice_data = VoiceData(runtime, bus)
if routing_mode:
    from routing_data import RoutingData
    from source.server import lab_state
    from source.server.world import sim

    lab_state._world = sim.new_world(seed=26)
    lab_state.ensure_started = lambda: False
    routing_data = RoutingData(area, runtime)
if capability_mode:
    from capability_data import CapabilityData
    capability_data = CapabilityData(area, runtime)
if workflow_mode:
    from workflow_data import WorkflowData
    workflow_data = WorkflowData(area, runtime)

if settings_mode:
    from settings_data import SettingsData
    settings_data = SettingsData(area,runtime)


def core():
    serving = {"active": runtime.active, "duty": "brain",
               "served_by": "brain_fixture/fixture-thinking-model" if runtime.active else runtime.last_served}
    return {"available": True, "brain": {"pool": "brain_fixture" if runtime.active else None,
            "model": "fixture-thinking-model" if runtime.active else None,
            "last_pool": "brain_fixture", "last_model": "fixture-selected-model"},
            "authority": {} if runtime.selected_only else {"serving": serving},
            "vision": {"model": "configured-only-model", "available": None},
            "chain": [], "providers": [], "quota": {}, "voice": {}, "tts": {}, "prompt_render": {}}


def control(command):
    action = command["action"]
    if action.startswith("n8n:") and n8n_data:
        return n8n_data.control(command)
    if action.startswith("companion:") and companion_mode:
        if action == "companion:publish":
            return {"ok": runtime.companion.publish(companion_owner, companion_generation,
                        command["payload"], live=lambda:True)}
        if action == "companion:reader":
            runtime.companion_reader_error = command["failed"]
        elif action == "companion:reopen":
            runtime.companion = Companion(area / "companion.json")
        elif action == "companion:external-change":
            return runtime.companion.save(command["changes"],runtime.companion.preferences()["revision"])
        elif action == "companion:write-failure":
            runtime.companion_write_error = command["failed"]
        return {"ok":True}
    if action.startswith("settings:") and settings_data:
        return settings_data.control(command)
    if action.startswith(("roster:", "town:")) and roster_data:
        return roster_data.control(command)
    if action.startswith("workspace:") and workspace_data:
        return workspace_data.control(command)
    if action.startswith("planner:") and planner_data:
        return planner_data.control(command)
    if action.startswith("library:") and library_data:
        return library_data.control(command)
    if action == "knowledge:fail-write" and knowledge_ingest_mode:
        import sqlite3

        with sqlite3.connect(os.environ["FRIDAY_KNOWLEDGE_DB"]) as conn:
            conn.execute("""CREATE TRIGGER fail_import BEFORE UPDATE ON source BEGIN
                SELECT RAISE(ABORT, 'fixture-private-write-error'); END""")
        return {"ok": True}
    if action.startswith("voice:") and voice_data:
        return voice_data.control(command)
    if action.startswith("routing:") and routing_data:
        return routing_data.control(command)
    if action.startswith("capability:") and capability_data:
        return capability_data.control(command)
    if action.startswith("workflow:") and workflow_data:
        return workflow_data.control(command)
    if action == "mode":
        runtime.mode = command["value"]
    elif action == "cancel_mode":
        runtime.cancel_mode = command["value"]
    elif action == "chunk" and runtime.active:
        bus.chat.merge_stream(command["text"])
    elif action == "finish":
        bus.chat.finalize_streaming()
        runtime.active = False
        runtime.last_served = "brain_fixture/fixture-thinking-model"
        bus.publish("idle", "Standing by")
    elif action == "chat_error":
        runtime.chat_error = command["value"]
    elif action == "status_error":
        runtime.status_error = command["value"]
    elif action == "selected_only":
        runtime.selected_only = True
    elif action == "history":
        for index in range(35):
            bus.chat.append("user", "Earlier fixture message %d\n" % index + "History line. " * 15)
    elif action == "append":
        bus.chat.append("assistant", command["text"])
    elif action != "read":
        raise ValueError("unsupported fixture command")
    return {"calls": calls, "chat": bus.chat.snapshot(), "paused": runtime.server_state["is_paused"]}


methods = {
    "get_status": bridge.get_status, "get_chat": bridge.get_chat,
    "send_text": bridge.send_text, "stop_turn": bridge.stop_turn,
    "get_turn": bridge.get_turn, "get_core": core, "set_paused": bridge.set_paused,
    "get_identity_views": bridge.get_identity_views,
    "get_town_view": bridge.get_town_view,
    "town_transport": bridge.town_transport,
    "get_town_install_preview": bridge.get_town_install_preview,
    "confirm_town_installation": bridge.confirm_town_installation,
    "rollback_town_installation": bridge.rollback_town_installation,
    "get_speech": lambda: {}, "get_events": lambda: [], "get_logs": lambda: "",
    "get_sys_stats": lambda: {}, "get_focus": lambda: {}, "get_ear_status": lambda: {},
    "get_startup": lambda: {"available":False},
    "get_settings": lambda: {}, "get_tasks": lambda: {"tasks": [], "pending": []},
    "get_memory": lambda: [], "get_knowledge": lambda: {}, "get_project_notes": lambda: [],
    "get_conversation_library": bridge.get_conversation_library,
    "get_voice_screen": bridge.get_voice_screen,
    "get_routing_view": bridge.get_routing_view,
    "get_rollout_readiness": bridge.get_rollout_readiness,
    "get_capability_inventory": bridge.get_capability_inventory,
    "get_workflow_view": bridge.get_workflow_view,
    "workflow_request_target": bridge.workflow_request_target,
    "get_tools": lambda: [], "get_skills": lambda: [], "get_schedule": lambda: {"tasks": []},
    "get_ai_agents": lambda: {}, "get_roster": lambda: {}, "get_world": lambda: {},
    "get_lab": lambda: {}, "get_budgets": lambda: {},
}
if roster_mode:
    methods["get_lab"] = bridge.get_lab
    methods["get_character_memory"] = lambda *_: {"items": [], "available": False}
if archive_mode:
    methods["get_conversation_transcript"] = bridge.get_conversation_transcript
if roster_controls_mode:
    methods["set_catalog_serving"] = bridge.set_catalog_serving
if town_life_mode:
    methods.update(set_catalog_background_life=bridge.set_catalog_background_life,
                   set_catalog_life_budget=bridge.set_catalog_life_budget)
if workspace_mode:
    methods.update(get_roster=workspace_data.snapshot, assign_task=bridge.assign_task,
                   world_command=bridge.world_command, review_task=bridge.review_task,
                   read_artifact=bridge.read_artifact)
if planner_mode:
    methods.update(get_tasks=bridge.get_tasks, get_schedule=bridge.get_schedule,
                   decide_pending_request=bridge.decide_pending_request)
if library_mode:
    for method in ("get_memory_library", "get_knowledge_library", "get_conversation_library",
                   "get_conversation_transcript", "add_memory", "update_memory", "delete_memory",
                   "character_correct", "character_pin", "character_forget"):
        methods[method] = getattr(bridge, method)
if knowledge_ingest_mode:
    methods.update(import_knowledge=bridge.import_knowledge, search_knowledge=bridge.search_knowledge,
                   get_knowledge_source=bridge.get_knowledge_source)
if mcp_approval_mode:
    methods["decide_mcp_proposal"] = bridge.decide_mcp_proposal
if companion_mode:
    def read_companion():
        if getattr(runtime,"companion_reader_error",False):
            raise OSError("synthetic reader failure")
        return bridge.get_companion()
    def save_companion(changes, revision):
        if getattr(runtime,"companion_write_error",False):
            return {"ok":False,"message":"Synthetic save failed. Your edit is kept."}
        return bridge.set_companion(changes, revision)
    methods.update(get_companion=read_companion, set_companion=save_companion,
                   acknowledge_companion=bridge.acknowledge_companion,
                   hide_companion_surface=lambda:{"ok":True})
if voice_mode:
    methods["get_speech"] = bridge.get_speech
if workflow_mode:
    methods["get_tasks"] = bridge.get_tasks
if settings_mode:
    methods.update(get_startup=bridge.get_startup,set_startup=bridge.set_startup,get_settings=bridge.get_settings)
if n8n_mode:
    methods.update(get_n8n=n8n_data.snapshot,set_n8n=n8n_data.save,
        test_n8n=bridge.test_n8n,pull_n8n=bridge.pull_n8n,
        decide_n8n_request=bridge.decide_n8n_request,
        preview_n8n_decision=bridge.preview_n8n_decision,
        decide_n8n_event=bridge.decide_n8n_event)

if completion_records_mode:
    from source.server.schedule_store import ScheduleStore
    from source.server.light_archive import ConversationArchive
    os.environ['FRIDAY_TASK_DB']=str(area/'personal-tasks.db')
    os.environ['FRIDAY_SCHEDULE_FILE']=str(area/'schedule.json')
    runtime.server_state['schedule_store']=ScheduleStore()
    archive=ConversationArchive(area/'light-archive.db');owner,generation=object(),object()
    archive.admit(owner,generation,'hud','Saved synthetic question')
    archive.turn.checkpoint_at=0
    archive.append(owner,generation,1,'Saved synthetic answer')
    archive.finish(owner);runtime.light_archive=archive
    for method in ('get_tasks','get_schedule','mutate_personal_task','mutate_calendar','get_conversation_transcript','export_conversation','delete_conversation'):
        methods[method]=getattr(bridge,method)
print("READY", flush=True)
for line in sys.stdin:
    request = json.loads(line)
    try:
        name, args = request["method"], request.get("args", [])
        if name == "_fixture":
            result = control(args[0])
        else:
            calls.append([name, args])
            if name == "get_chat" and runtime.chat_error or name == "get_status" and runtime.status_error:
                raise RuntimeError("synthetic read failure")
            if name == "get_identity_views" and getattr(runtime, "roster_error", False):
                raise RuntimeError("synthetic identity transport failure")
            if name == "get_memory_library" and getattr(runtime, "library_error", False):
                raise RuntimeError("synthetic library transport failure")
            if name == "get_voice_screen" and getattr(runtime, "voice_reader_error", False):
                raise RuntimeError("synthetic voice reader failure")
            if name == "get_routing_view" and getattr(runtime, "routing_error", False):
                raise RuntimeError("synthetic routing reader failure")
            if name == "get_capability_inventory" and getattr(runtime, "capability_error", False):
                raise RuntimeError("synthetic capability reader failure")
            if name == "get_workflow_view" and getattr(runtime, "workflow_error", False):
                raise RuntimeError("synthetic workflow reader failure")
            if name == "get_settings" and getattr(runtime, "settings_host_error", False):
                raise RuntimeError("synthetic host reader failure")
            result = methods[name](*args)
    except Exception as error:
        result = {"fixture_error": type(error).__name__}
    print("REPLY " + json.dumps([request["id"], result]), flush=True)
