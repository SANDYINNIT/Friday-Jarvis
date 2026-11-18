"""Offline bridge follow-up runner; all state and audit evidence stay in the isolated test area.

--baseline replays the three original failures with UI asset reads forbidden.
The default selection covers corrected bridge, ownership and task contracts.
"""
import json
import hashlib
import os
from pathlib import Path
import platform
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
if "--baseline" in sys.argv:
    evidence = ROOT / "tools/field_notes_acceptance/bridge-source-before.json"
    if evidence.exists():
        before = json.loads(evidence.read_text(encoding="utf-8-sig"))
        expected = next(row["SHA256"] for row in before if row["Path"] == "test_app_bridge.py")
        if hashlib.sha256((ROOT / "test_app_bridge.py").read_bytes()).hexdigest().upper() != expected:
            raise SystemExit("Original fixtures have changed; saved baseline evidence must not be overwritten")
state_root = ROOT / ".venv" / "field-notes-ui-check"
state_root.mkdir(parents=True, exist_ok=True)
area = Path(tempfile.mkdtemp(prefix="bridge-regression-", dir=state_root))
for name in ("HOME", "USERPROFILE", "TEMP", "TMP"):
    os.environ[name] = str(area)
for name, value in {
    "FRIDAY_AUDIT_DIR": area / "audit", "FRIDAY_LOG_DIR": area / "logs",
    "FRIDAY_EXHAUSTION_LEDGER": area / "exhaustion.json",
    "FRIDAY_MEMORY_DB": area / "memory.db", "FRIDAY_WORLD_DB": area / "world.db",
    "FRIDAY_LIGHT_ARCHIVE_DB": area / "light_conversations.db",
    "FRIDAY_KNOWLEDGE_DB": area / "knowledge.db",
    "FRIDAY_TASK_DB": area / "tasks.db", "FRIDAY_SCHEDULE_FILE": area / "schedule.json",
    "FRIDAY_SCRATCH_DIR": area / "scratch", "FRIDAY_CONFIRM_STATE_DIR": area / "consent",
}.items():
    os.environ[name] = str(value)
os.environ.update(FRIDAY_IDENTITY_READER="0", FRIDAY_IDENTITY_INTEGRATION="0",
                  FRIDAY_ACCOUNTS="0", LITELLM_LOCAL_MODEL_COST_MAP="True",
                  FRIDAY_MODAL_BASE="https://synthetic.invalid/v1")
os.environ.pop("SSLKEYLOGFILE", None)
platform.uname()
numpy_probe = ROOT / ".venv/Lib/site-packages/numpy/testing/_private/utils.py"
ui = str(ROOT / "source/server/ui").lower()
baseline = "--baseline" in sys.argv


def audit(event, args):
    if baseline and event == "open" and isinstance(args[0], (str, bytes)):
        if str(Path(os.fsdecode(args[0])).resolve()).lower().startswith(ui + os.sep):
            raise RuntimeError("UI assets unavailable in Python-only causal comparison")
    if event == "subprocess.Popen":
        executable, command, cwd, env = args
        frame = sys._getframe(1)
        while frame and frame.f_code.co_name != "check_support_sve":
            frame = frame.f_back
        if (executable in (None, "lscpu") and command == "lscpu" and cwd is None
                and env is None and frame
                and Path(frame.f_code.co_filename).resolve() == numpy_probe.resolve()):
            raise FileNotFoundError(2, "Linux CPU probe unavailable on Windows")
        exact = [sys.executable, '-c', "import sys; sys.stdout.buffer.write(b'ok \\x81\\x90 tail')"]
        caller=sys._getframe(1)
        while caller and caller.f_code.co_name!='test_replace_decodes_bad_bytes_without_losing_the_text':caller=caller.f_back
        if caller and Path(caller.f_code.co_filename).resolve()==ROOT/'test_priority1_runtime_defects.py':
            from subprocess import list2cmdline
            if command==list2cmdline(exact) and cwd is None and env is None and executable in (None,sys.executable):
                return
        from subprocess import list2cmdline
        node = str(Path("C:/Program Files/nodejs/node.exe"))
        node_args = [node, "-", str(ROOT / "source/server/ui/app.js")]
        frame = sys._getframe(1)
        allowed_node_checks = {
            "test_hud_renders_real_outcomes_and_attempt_review_controls": "test_task_attempt_lifecycle.py",
            "test_hud_waits_for_explicit_success_and_displays_each_experience_once": "test_canonical_memory_lifecycle.py",
            "test_production_polling": "test_ui_polling.py",
        }
        while frame and frame.f_code.co_name not in allowed_node_checks:
            frame = frame.f_back
        if frame and frame.f_code.co_name == "test_production_polling":
            scenario = frame.f_locals.get("scenario")
            if scenario not in {"status-recovery", "missing-bridge", "render-failure",
                                "single-chain:ready-first", "single-chain:timer-first",
                                "aux:get_chat", "aux:get_logs", "aux:get_sys_stats",
                                "aux:get_ear_status", "aux:get_focus"}:
                raise RuntimeError("Bridge fixture denied unknown polling scenario")
            node_args.append(scenario)
        expected = list2cmdline(node_args)
        if (frame and Path(frame.f_code.co_filename).resolve() == ROOT / allowed_node_checks[frame.f_code.co_name]
                and (executable is None or str(executable).lower() == node.lower())
                and isinstance(command, str) and command.lower() == expected.lower()):
            return
        raise RuntimeError("Bridge fixture denied child process")
    if event in ("socket.connect", "socket.bind"):
        if not (isinstance(args[1], tuple) and args[1][0] in ("127.0.0.1", "::1")):
            raise RuntimeError("Bridge fixture denied non-loopback socket")
    if event == "socket.getaddrinfo" and args[0] not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError("Bridge fixture denied external DNS")


sys.addaudithook(audit)
import pytest

original = [
    "test_app_bridge.py::TestStopTurn::test_stop_cancels_queued_lifecycle_records",
    "test_app_bridge.py::TestStopTurn::test_stop_sets_the_documented_mechanism_only_when_active",
    "test_app_bridge.py::TestAssignToReviewFlow::test_end_to_end_assignment_review",
]
selection = original if "--baseline" in sys.argv else [
    "test_app_bridge.py::TestStopTurn", "test_app_bridge.py::TestAssignToReviewFlow",
    "test_request_reply_ownership.py", "test_task_attempt_lifecycle.py",
]
if "--roster" in sys.argv:
    selection = ["test_roster_identity_bridge.py", "test_identity_reader.py", "test_identity_shadow.py",
                 "test_app_bridge.py::TestStopTurn",
                 "test_task_attempt_lifecycle.py::test_hud_renders_real_outcomes_and_attempt_review_controls"]
if "--roster-controls" in sys.argv:
    selection = ["test_roster_controls.py", "test_identity_integration.py", "test_identity_catalog.py",
                 "test_roster_identity_bridge.py", "test_routing_view.py",
                 "test_lab_policy.py::TestBulkSwitch", "test_lab_policy.py::TestIndividualAndEmergencySeparation",
                 "test_lab_policy.py::TestAuthoritativeLifeShape", "test_lab_selection.py",
                 "test_request_reply_ownership.py::test_provider_error_and_next_http_reply_stay_with_their_owners",
                 "test_intent_judge_removed.py::VoicePathBehaviourTests"]
if "--town" in sys.argv:
    selection = ["test_town_view_bridge.py", "test_town_routines_journal.py", "test_lab_map.py",
                 "test_identity_install.py::test_install_preserves_legacy_and_creates_npc_ids_once",
                 "test_identity_reader.py", "test_roster_identity_bridge.py", "test_app_bridge.py::TestStopTurn"]
if "--town-life" in sys.argv:
    selection = ["test_town_background_life.py", "test_identity_reader.py",
                 "test_town_routines_journal.py", "test_lab_budget.py",
                 "test_roster_identity_bridge.py", "test_identity_catalog.py"]
if "--town-life-boundary" in sys.argv:
    selection = ["test_town_background_life.py", "test_request_reply_ownership.py"]
if "--workspace" in sys.argv:
    selection = ["test_task_attempt_lifecycle.py", "test_app_bridge.py::TestAssignToReviewFlow",
                 "test_app_bridge.py::TestStopTurn"]
if "--worker-provenance" in sys.argv:
    selection = ["test_worker_provenance.py", "test_task_attempt_lifecycle.py",
                 "test_app_bridge.py::TestAssignToReviewFlow", "test_app_bridge.py::TestStopTurn",
                 "test_identity_integration.py"]
if "--worker-transport" in sys.argv:
    selection = ["test_worker_provenance.py", "test_agent_executor.py::TestHonestyAndSeparation"]
if "--planner" in sys.argv:
    selection = ["test_planner_bridge.py", "test_pending_requests.py", "test_task_store.py",
                 "test_request_reply_ownership.py::test_correlated_integration_decisions_on_real_admission_and_polling",
                 "test_request_reply_ownership.py::test_hud_pending_decision_uses_existing_authority_and_callback",
                 "test_task_attempt_lifecycle.py::test_workspace_bridge_forwards_manual_policy_and_requires_exact_review"]
if "--library" in sys.argv:
    selection = ["test_library_bridge.py", "test_canonical_memory_lifecycle.py",
                 "test_app_bridge.py::TestStopTurn"]
if "--knowledge-ingest" in sys.argv:
    selection = ["test_local_knowledge.py", "test_library_bridge.py",
                 "test_light_archive.py", "test_request_reply_ownership.py"]
if "--knowledge-transport" in sys.argv:
    selection = ["test_local_knowledge.py", "test_tool_execution_boundary.py"]
if "--voice" in sys.argv:
    selection = ["test_voice_screen_bridge.py", "test_playback_acknowledgement.py", "test_ui_polling.py",
                 "test_priority1_runtime_defects.py::InputErrorResilienceTests", "test_app_bridge.py::TestStopTurn"]
if "--audio-session" in sys.argv:
    selection = ["test_audio_session.py", "test_client_speech_endpoint.py", "test_client_listening_state.py",
                 "test_voice_screen_bridge.py", "test_playback_acknowledgement.py", "test_speech_queue.py",
                 "test_request_reply_ownership.py", "test_telegram_mute_ownership.py"]
if "--archive" in sys.argv:
    selection = ["test_light_archive.py", "test_library_bridge.py",
                 "test_request_reply_ownership.py", "test_chat_reply_ownership.py", "test_redaction.py"]
if "--routing" in sys.argv:
    selection = ["test_routing_view.py", "test_identity_shadow.py", "test_identity_integration.py",
                 "test_status_ui.py", "test_app_bridge.py::TestStopTurn"]
if "--rollout-readiness" in sys.argv:
    selection = ["test_rollout_readiness.py", "test_routing_view.py", "test_roster_identity_bridge.py",
                 "test_town_background_life.py", "test_capability_inventory.py"]
if "--town-install" in sys.argv:
    selection = ["test_town_install_operator.py", "test_town_view_bridge.py",
                 "test_identity_install.py::test_install_preserves_legacy_and_creates_npc_ids_once",
                 "test_identity_install.py::test_revalidation_rejects_without_partial_changes",
                 "test_identity_install.py::test_receipt_and_rollback_record_failures_cannot_partially_publish",
                 "test_identity_install.py::test_older_rollback_cannot_undo_a_newer_installation",
                 "test_identity_install.py::test_pristine_rollback_survives_reopen_and_keeps_catalog_reservations"]
if "--light-town-rehearsal" in sys.argv:
    os.environ["FRIDAY_TOWN_REHEARSAL_EVIDENCE"] = str(area / "town-rehearsal.json")
    selection = ["test_light_lifecycle.py", "test_town_catalog_rehearsal.py",
                 "test_identity_integration.py", "test_identity_shadow.py",
                 "test_town_view_bridge.py", "test_town_install_operator.py",
                 "test_public_ingress.py::test_main_starts_local_client_and_hud_without_waiting_for_ingress"]
if "--capabilities" in sys.argv:
    selection = ["test_capability_inventory.py", "test_mcp_runtime.py",
                 "test_self_awareness.py::SkillCatalogTests", "test_tool_execution_boundary.py",
                 "test_app_bridge.py::TestStopTurn"]
if "--mcp-light" in sys.argv:
    selection = ["test_mcp_light.py", "test_mcp_runtime.py", "test_capability_inventory.py",
                 "test_tool_execution_boundary.py", "test_local_knowledge.py",
                 "test_town_background_life.py::test_provider_wire_has_exact_target_and_no_tools"]
if "--mcp-boundary" in sys.argv:
    selection = ["test_mcp_light.py", "test_mcp_runtime.py", "test_capability_inventory.py"]
if "--workflows" in sys.argv:
    selection = ["test_workflow_view.py", "test_n8n_runtime.py", "test_pending_requests.py",
                 "test_planner_bridge.py", "test_app_bridge.py::TestStopTurn"]

if "--settings" in sys.argv:
    selection = ["test_settings_bridge.py", "test_status_ui.py", "test_voice_screen_bridge.py", "test_ui_polling.py",
                 "test_app_bridge.py::TestStopTurn"]

if "--tray-baseline" in sys.argv:
    selection = ["test_tray_identity.py::test_exact_tooltip_without_mojibake",
                 "test_tray_identity.py::test_repeated_initialization_has_one_owner"]

if "--tray" in sys.argv:
    os.environ["FRIDAY_CREDENTIALS_FILE"] = str(area / "unused-credentials.json")
    selection = ["test_tray_identity.py", "test_tray_assets.py", "test_tray_window_owner.py",
                 "test_settings_bridge.py", "test_startup_warmup.py", "test_app_bridge.py::TestStopTurn",
                 "test_hud_geometry.py::TestGeometryStoreHandling::test_save_is_atomic_and_creates_folder",
                 "test_open_app_presence.py"]

if "--window-polish" in sys.argv:
    selection = ["test_window_integration.py", "test_tray_window_owner.py", "test_tray_assets.py",
                 "test_hud_geometry.py::TestGeometryStoreHandling", "test_open_app_presence.py"]

if "--opening" in sys.argv:
    selection = ["test_window_integration.py", "test_tray_window_owner.py",
                 "test_open_app_presence.py", "test_catalog_startup.py"]

if "--companion-clicky" in sys.argv:
    selection = ["test_companion.py", "test_companion_runtime.py", "test_companion_clicky.py",
                 "test_window_integration.py", "test_request_reply_ownership.py", "test_tool_execution_boundary.py"]


if "--companion-clicky-boundary" in sys.argv:
    selection = ["test_companion_clicky.py", "test_companion_runtime.py"]

if "--personality-light" in sys.argv:
    selection = ["test_personality_light.py"]

if "--completion-voice" in sys.argv:
    selection = ["test_prompt_batch.py", "test_audio_session.py", "test_voice_trace.py",
                 "test_voice_latency_observability.py", "test_voice_completion.py"]

if "--completion-core" in sys.argv:
    selection = [
        'test_priority1_runtime_defects.py::SubprocessDecodeTests::test_replace_decodes_bad_bytes_without_losing_the_text',
        'test_priority1_runtime_defects.py::VerifiedPathTests::test_the_prompt_no_longer_advertises_a_stale_screenshot_dir',
        'test_priority1_runtime_defects.py::VerifiedPathTests::test_the_prompt_paths_are_resolved_from_disk',
        'test_priority1_runtime_defects.py::VerifiedPathTests::test_the_screenshots_path_is_the_one_actually_written',
        'test_request_reply_ownership.py','test_light_archive.py','test_personality_light.py']


if "--completion-records" in sys.argv:
    selection=["test_completion_records.py","test_planner_bridge.py","test_light_archive.py","test_library_bridge.py","test_companion.py","test_prompt_batch.py","test_routing_view.py"]

if "--completion-integrated" in sys.argv:
    selection=["test_completion_records.py","test_accounts.py","test_completion_companion.py",
               "test_prompt_batch.py","test_voice_completion.py","test_request_reply_ownership.py",
               "test_tool_execution_boundary.py","test_local_knowledge.py","test_mcp_light.py",
               "test_routing_view.py","test_town_background_life.py","test_light_archive.py",
               "test_personality_light.py"]

if "--completion-final" in sys.argv:
    selection=["test_completion_records.py","test_accounts.py","test_completion_companion.py","test_prompt_render_cache.py",
               "test_canonical_memory_lifecycle.py","test_task_attempt_lifecycle.py","test_companion_clicky.py"]

if "--completion-last" in sys.argv:
    selection=["test_completion_records.py","test_accounts.py","test_completion_companion.py"]

if "--completion-calendar" in sys.argv:
    selection=["test_completion_records.py::test_calendar_atomic_local_edits_dst_reopen_and_wrong_source",
               "test_completion_records.py::test_calendar_civil_dates_and_cross_instance_refresh",
               "test_planner_bridge.py::test_calendar_midnight_dst_order_and_civil_dates",
               "test_planner_bridge.py::test_calendar_reader_distinguishes_empty_corrupt_and_disconnected",
               "test_planner_bridge.py::test_failed_schedule_write_does_not_publish_event"]

if "--pretest-review" in sys.argv:
    selection=["test_pretest_followup.py"]
if "--pretest-regressions" in sys.argv:
    selection=["test_pretest_followup.py","test_prompt_render_cache.py","test_completion_records.py",
               "test_planner_bridge.py","test_request_reply_ownership.py","test_light_archive.py",
               "test_tool_execution_boundary.py"]

if "--pretest-publication" in sys.argv:
    selection=["test_pretest_followup.py::test_calendar_failed_publication_cannot_replace_its_last_readable_file"]

if "--pretest-cache-controls" in sys.argv:
    selection=["test_pretest_followup.py::test_disabling_an_installed_prompt_cache_really_restores_the_renderer",
               "test_pretest_followup.py::test_replaced_cache_wrapper_cannot_publish_a_late_old_render"]

if "--second-bug-pass" in sys.argv:
    selection=["test_second_bug_pass.py"]
if "--second-bug-regressions" in sys.argv:
    selection=["test_second_bug_pass.py","test_completion_records.py","test_canonical_memory_lifecycle.py",
               "test_local_knowledge.py","test_light_archive.py","test_request_reply_ownership.py","test_accounts.py","test_task_store.py"]

if "--second-source-lifecycle" in sys.argv:
    selection=["test_second_bug_pass.py::test_stale_knowledge_actions_cannot_create_a_missing_store"]

if "--second-connection-lifecycle" in sys.argv:
    selection=["test_second_bug_pass.py::test_task_operations_close_owned_connections_on_success_and_failure"]

if "--second-legacy-receipt" in sys.argv:
    selection=["test_second_bug_pass.py::test_legacy_deleted_task_receipt_is_refused_without_backfill"]
if "--second-task-final" in sys.argv:
    selection=["test_second_bug_pass.py","test_task_store.py","test_accounts.py","test_completion_records.py"]

if "--voice-latency-new" in sys.argv:
    selection = ["test_voice_latency_20261114.py"]
if "--voice-latency-regressions" in sys.argv:
    selection = ["test_voice_latency_20261114.py", "test_prompt_batch.py", "test_prompt_render_cache.py", "test_pretest_followup.py", "test_voice_completion.py", "test_audio_session.py", "test_request_reply_ownership.py", "test_voice_trace.py"]
if "--voice-capture-initial" in sys.argv or "--voice-capture-new" in sys.argv:
    selection = ["test_voice_capture_responsiveness.py"]
if "--voice-capture-regressions" in sys.argv or "--voice-capture-audio-final" in sys.argv:
    selection = ["test_voice_capture_responsiveness.py", "test_client_speech_endpoint.py", "test_voice_completion.py", "test_audio_session.py", "test_request_reply_ownership.py", "test_barge_in.py", "test_voice_trace.py"]
if "--voice-listener-initial" in sys.argv or "--voice-listener-new" in sys.argv:
    selection = ["test_voice_listener_responsiveness.py"]
if "--voice-listener-regressions" in sys.argv:
    selection = ["test_voice_listener_responsiveness.py", "test_light_lifecycle.py", "test_audio_session.py", "test_voice_capture_responsiveness.py", "test_request_reply_ownership.py", "test_barge_in.py"]
if "--vision-listener-initial" in sys.argv or "--vision-listener-new" in sys.argv:
    selection = ["test_vision_listener_responsiveness.py"]
if "--vision-listener-regressions" in sys.argv:
    selection = ["test_vision_listener_responsiveness.py", "test_voice_listener_responsiveness.py", "test_light_lifecycle.py", "test_audio_session.py", "test_request_reply_ownership.py", "test_screen_companion.py", "test_identity_integration.py::test_real_vision_fallback_only_records_verified_transport", "test_identity_integration.py::test_real_light_fallback_and_generation_fence"]

class Results:
    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed:
            self.reports.append({"test": report.nodeid, "phase": report.when,
                                 "outcome": report.outcome,
                                 "error": str(report.longrepr) if report.failed else ""})


results = Results()
plugins=[results]
if '--completion-core' in sys.argv:
    from tools.field_notes_acceptance.completion_profile_fixture import ProfileIsolation
    plugins.append(ProfileIsolation())
extra_selection = (["-k", "not test_correlated_integration_decisions_on_real_admission_and_polling"]
                   if "--voice-capture-audio-final" in sys.argv else [])
exit_code = pytest.main(["-q", "--disable-warnings", "--tb=short", *extra_selection, *selection], plugins=plugins)
record = ROOT / "tools/field_notes_acceptance" / (
    "vision-listener-initial.json" if "--vision-listener-initial" in sys.argv else
    "vision-listener-new.json" if "--vision-listener-new" in sys.argv else
    "vision-listener-regressions.json" if "--vision-listener-regressions" in sys.argv else
    "voice-listener-initial.json" if "--voice-listener-initial" in sys.argv else
    "voice-listener-new.json" if "--voice-listener-new" in sys.argv else
    "voice-listener-regressions.json" if "--voice-listener-regressions" in sys.argv else
    "voice-capture-audio-final.json" if "--voice-capture-audio-final" in sys.argv else
    "voice-capture-initial.json" if "--voice-capture-initial" in sys.argv else
    "voice-capture-new.json" if "--voice-capture-new" in sys.argv else
    "voice-capture-regressions.json" if "--voice-capture-regressions" in sys.argv else
    "voice-latency-new.json" if "--voice-latency-new" in sys.argv else
    "voice-latency-regressions.json" if "--voice-latency-regressions" in sys.argv else
    "second-legacy-receipt.json" if "--second-legacy-receipt" in sys.argv else
    "second-task-final.json" if "--second-task-final" in sys.argv else
    "second-connection-lifecycle.json" if "--second-connection-lifecycle" in sys.argv else
    "second-source-lifecycle.json" if "--second-source-lifecycle" in sys.argv else
    "second-bug-pass.json" if "--second-bug-pass" in sys.argv else
    "second-bug-regressions.json" if "--second-bug-regressions" in sys.argv else
    "pretest-cache-controls.json" if "--pretest-cache-controls" in sys.argv else
    "pretest-publication.json" if "--pretest-publication" in sys.argv else
    "pretest-review.json" if "--pretest-review" in sys.argv else
    "pretest-regressions.json" if "--pretest-regressions" in sys.argv else
    "completion-calendar.json" if "--completion-calendar" in sys.argv else
    "completion-last.json" if "--completion-last" in sys.argv else
    "completion-final.json" if "--completion-final" in sys.argv else
    "completion-integrated.json" if "--completion-integrated" in sys.argv else
    "completion-records.json" if "--completion-records" in sys.argv else
    "completion-core.json" if "--completion-core" in sys.argv else
    "completion-voice.json" if "--completion-voice" in sys.argv else
    "personality-light-regressions.json" if "--personality-light" in sys.argv else
    "companion-clicky-boundary.json" if "--companion-clicky-boundary" in sys.argv else
    "companion-clicky-regressions.json" if "--companion-clicky" in sys.argv else
    "opening-regressions.json" if "--opening" in sys.argv else
    "light-town-rehearsal-regressions.json" if "--light-town-rehearsal" in sys.argv else
    "town-install-regressions.json" if "--town-install" in sys.argv else
    "rollout-readiness-regressions.json" if "--rollout-readiness" in sys.argv else
    "mcp-boundary.json" if "--mcp-boundary" in sys.argv else
    "mcp-light-regressions.json" if "--mcp-light" in sys.argv else
    "town-life-boundary.json" if "--town-life-boundary" in sys.argv else
    "town-life-regressions.json" if "--town-life" in sys.argv else
    "knowledge-ingest-transport.json" if "--knowledge-transport" in sys.argv else
    "knowledge-ingest-regressions.json" if "--knowledge-ingest" in sys.argv else
    "worker-provenance-transport.json" if "--worker-transport" in sys.argv else
    "worker-provenance-regressions.json" if "--worker-provenance" in sys.argv else
    "light-archive-regressions.json" if "--archive" in sys.argv else
    "audio-session-regressions.json" if "--audio-session" in sys.argv else
    "roster-controls-regressions.json" if "--roster-controls" in sys.argv else
    "window-polish-regressions.json" if "--window-polish" in sys.argv else
    "tray-regressions.json" if "--tray" in sys.argv else
    "tray-regressions-baseline.json" if "--tray-baseline" in sys.argv else
    "settings-regressions.json" if "--settings" in sys.argv else
    "workflow-regressions.json" if "--workflows" in sys.argv else
    "capability-regressions.json" if "--capabilities" in sys.argv else
    "routing-regressions.json" if "--routing" in sys.argv else
    "voice-regressions.json" if "--voice" in sys.argv else
    "library-regressions.json" if "--library" in sys.argv else
    "planner-regressions.json" if "--planner" in sys.argv else
    "workspace-regressions.json" if "--workspace" in sys.argv else
    "town-regressions.json" if "--town" in sys.argv else
    "roster-regressions.json" if "--roster" in sys.argv else
    "bridge-before.json" if "--baseline" in sys.argv else "bridge-after.json")
record.write_text(json.dumps({"exit_code": int(exit_code), "ui_reads_forbidden": baseline,
                              "scratch": str(area), "reports": results.reports}, indent=2), encoding="utf-8")
raise SystemExit(exit_code)
