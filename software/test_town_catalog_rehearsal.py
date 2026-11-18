"""Rehearse the reviewed route groups and exact Town operator on synthetic stores."""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from source.server import api_pools, brain_router as brain, cloud_stt, gemini_tts, lab_state, screen_understanding as vision
from source.server.identity_integration import from_environment
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world import Store, sim
from source.server.world.identity_backup import verify_backup


def test_rehearse_catalog_operator_and_town_failover(tmp_path, monkeypatch):
    bases = {
        "Modal": "http://modal.rehearsal.invalid/v1",
        "Groq": "http://groq.rehearsal.invalid/v1",
        "OpenRouter": "http://router.rehearsal.invalid/v1",
        "Gemini": "http://gemini.rehearsal.invalid/v1",
        "Ollama": "http://ollama.rehearsal.invalid",
    }
    for module, attr, provider in (
        (brain, "MODAL_BASE", "Modal"), (brain, "GROQ_BASE", "Groq"),
        (brain, "OPENROUTER_BASE", "OpenRouter"), (vision, "OPENROUTER_BASE", "OpenRouter"),
        (brain, "GEMINI_BASE", "Gemini"), (vision, "GEMINI_BASE", "Gemini"),
        (brain, "LOCAL_BASE", "Ollama"),
    ):
        monkeypatch.setattr(module, attr, bases[provider])
    monkeypatch.setattr(vision, "LOCAL_CHAT_URL", bases["Ollama"] + "/api/chat")
    monkeypatch.setenv("FRIDAY_MODAL_VISION_MODEL", brain.MODAL_MODEL)
    for key, value in {"FRIDAY_IDENTITY_INTEGRATION": "1", "FRIDAY_IDENTITY_READER": "1",
                       "FRIDAY_TOWN_INSTALL_OPERATOR": "1", "FRIDAY_VISION_MODEL": "qwen2.5vl:3b"}.items():
        monkeypatch.setenv(key, value)
    declarations = {
        "brain_modal": ("Modal", "rehearsal-modal"), "vision_modal": ("Modal", "rehearsal-modal"),
        "brain_groq": ("Groq", "rehearsal-groq"),
        "brain_openrouter": ("OpenRouter", "rehearsal-router"),
        "vision_openrouter": ("OpenRouter", "rehearsal-router"),
        "brain_gemini": ("Gemini", "rehearsal-gemini"), "vision_gemini": ("Gemini", "rehearsal-gemini"),
        "local": ("Ollama", "rehearsal-ollama"), "vision_local": ("Ollama", "rehearsal-ollama"),
    }
    manifest = {"complete": True,
                "deployments": {pool: {"deployment": ref, "base": bases[provider] +
                    ("/api/chat" if pool == "vision_local" else "")}
                    for pool, (provider, ref) in declarations.items()},
                "services": [
                    {"provider": "groq", "deployment": "rehearsal-listen", "model": cloud_stt.GROQ_STT_MODEL,
                     "kind": "service", "capabilities": ["listen"]},
                    {"provider": "gemini", "deployment": "rehearsal-voice", "model": gemini_tts.GEMINI_TTS_MODEL_CHAIN[0],
                     "kind": "service", "capabilities": ["voice_out"]},
                ]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    identity = from_environment(brain, vision, {
        "FRIDAY_IDENTITY_INTEGRATION": "1", "FRIDAY_IDENTITY_DEPLOYMENTS": json.dumps(manifest),
        "FRIDAY_IDENTITY_CATALOG_DB": str(tmp_path / "catalog.db"),
        "FRIDAY_IDENTITY_HISTORY_DB": str(tmp_path / "duty.db"),
    })
    assert identity is not None
    projected = identity.shadow.snapshot()
    chars = projected["roster"]["characters"]
    bindings = [binding for row in chars for binding in row["bindings"]]
    assert len(chars) == 15 and len(bindings) == 21
    assert len([row for row in chars if len(row["bindings"]) == 2]) == 6
    assert len(projected["roster"]["services"]) == 2
    assert all(row["representation"] == "procedural" for row in projected["roster"]["services"])
    modal = next(row for row in bindings if row["capability"] == "brain" and row["provider"] == "modal")
    groq = next(row for row in bindings if row["capability"] == "brain" and row["model"] == brain.GROQ_FAST_MODEL)
    modal_id, groq_id = modal["character_id"], groq["character_id"]
    assert modal_id != groq_id

    world = sim.new_world()
    world.paused = True
    original = deepcopy(world.characters)
    store = Store(tmp_path / "world.db")
    assert store.save(world)["ok"]
    monkeypatch.setattr(lab_state, "_world", world)
    monkeypatch.setattr(lab_state, "_store", store)
    monkeypatch.setattr(lab_state, "_background_life", None)
    monkeypatch.setattr(lab_state, "_thread", None)
    monkeypatch.setattr(api_pools, "_pools", None)
    monkeypatch.setattr(api_pools, "_model_bans", {})
    bridge = WebviewBridge(StatusBus(), SimpleNamespace(server_state={"identity_integration": identity}))

    def residents():
        return {row["source_id"]: row for row in bridge.get_town_view()["residents"]}

    def select(request, capability, generation, pool, binding, base):
        # Same trusted selection/result boundary used by the fake transport
        # and by the real Light Brain/Vision observers.
        attempt = identity.select(request, capability, generation, pool, binding["model"], base)
        assert attempt is not None
        return attempt

    try:
        preview = bridge.get_town_install_preview()
        assert preview["confirmation"] and len(preview["placements"]) == 15
        assert preview["verified_links"] == [] and {r["id"] for r in preview["unresolved"]} == {
            "code", "friday", "research", "vision"}
        installed = bridge.confirm_town_installation(preview["confirmation"])
        assert installed["status"] == "installed" and installed["created"] == 15
        manifest_backup = verify_backup(tmp_path / "town-install-backups" / installed["backup_id"])
        assert manifest_backup["world_snapshot_id"] == preview["world_snapshot_id"]
        store.close()
        store = Store(tmp_path / "world.db")
        lab_state._store, lab_state._world = store, store.load()
        receipt = bridge.get_town_install_preview()["receipt"]
        assert receipt["batch_id"] == installed["batch_id"] and receipt["rollback_allowed"]
        assert len(lab_state._world.identity_links) == 15 and len(lab_state._world.characters) == 19
        assert lab_state._world.characters[:4] == original
        assert all(not c.memory for c in lab_state._world.characters[4:])
        assert bridge.rollback_town_installation(installed["batch_id"])["status"] == "rolled_back"
        assert store.load().characters == original and not store.identity_links()

        # A new exact proposal after simulation advances gets a new receipt.
        assert lab_state._advance_world(lab_state._world, force=True, pause_after=True)["ok"]
        second = bridge.get_town_install_preview()
        later = bridge.confirm_town_installation(second["confirmation"])
        assert later["status"] == "installed"
        shadow = identity.shadow
        request = shadow.begin_request("modal-success")
        first = select(request, "brain", 1, "brain_modal", modal, bases["Modal"])
        assert residents()[modal_id]["serving"] == ["brain"]
        assert shadow.finish(first, "success")
        assert residents()[modal_id]["serving"] == [] and residents()[modal_id]["last_served"] == ["brain"]
        shadow.end_request(request)

        request = shadow.begin_request("modal-failover")
        first = select(request, "brain", 1, "brain_modal", modal, bases["Modal"])
        concurrent = select(request, "vision", 1, "vision_modal", modal, bases["Modal"])
        assert shadow.finish(first, "failure")
        api_pools._model_bans["brain_modal", modal["model"]] = time.monotonic() + 60
        fallback = select(request, "brain", 2, "brain_groq", groq, bases["Groq"])
        assert not shadow.finish(first, "success")
        view = residents()
        assert view[modal_id]["serving"] == ["vision"]
        assert view[modal_id]["route_failures"] == [{"capability": "brain", "state": "cooldown"}]
        assert view[groq_id]["serving"] == ["brain"] and not view[groq_id]["route_failures"]
        lab_state._world.paused = False
        lab_state._offer_serving_rest(lab_state._world, identity, shadow.snapshot())
        assert next(c for c in lab_state._world.characters if c.id == modal_id).goal != "rest"
        lab_state._world.paused = True
        assert shadow.finish(concurrent, "success") and shadow.finish(fallback, "success")
        assert residents()[groq_id]["last_served"] == ["brain"] and not residents()[groq_id]["serving"]
        assert residents()[modal_id]["last_served"] == ["vision"]
        shadow.end_request(request)

        lab_state._world.paused = False
        modal_resident = next(c for c in lab_state._world.characters if c.id == modal_id)
        start = modal_resident.x, modal_resident.y
        lab_state._offer_serving_rest(lab_state._world, identity, shadow.snapshot())
        assert modal_resident.goal == "rest"
        lab_state._offer_serving_rest(lab_state._world, identity, shadow.snapshot())
        for _ in range(30):
            previous = modal_resident.x, modal_resident.y
            sim.tick(lab_state._world)
            assert abs(modal_resident.x - previous[0]) + abs(modal_resident.y - previous[1]) <= 1
            if modal_resident.state == "resting":
                break
        assert (modal_resident.x, modal_resident.y) != start and modal_resident.state == "resting"
        lab_state._world.paused = True
        assert store.save(lab_state._world)["ok"]
        assert bridge.rollback_town_installation(later["batch_id"])["status"] == "conflict"
        assert len(store.load().characters) == 19

        request = shadow.begin_request("both-fail")
        failed = select(request, "brain", 1, "brain_modal", modal, bases["Modal"])
        assert shadow.finish(failed, "failure")
        failed_fallback = select(request, "brain", 2, "brain_groq", groq, bases["Groq"])
        assert shadow.finish(failed_fallback, "failure")
        assert all(not residents()[cid]["serving"] and residents()[cid]["route_failures"]
                   for cid in (modal_id, groq_id))
        assert residents()[groq_id]["last_served"] == ["brain"]
        from source.server import identity_integration
        monkeypatch.setattr(identity_integration, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + 120))
        api_pools._model_bans["brain_modal", modal["model"]] = time.monotonic() - 1
        assert all(not residents()[cid]["route_failures"] for cid in (modal_id, groq_id))
        assert all(not c.memory for c in store.load().characters if c.id in (modal_id, groq_id))
        assert {c.id for c in store.load().characters if c.memory} == {c.id for c in original}
        assert lab_state._world.model_budget == lab_state._world.planner_calls == 0

        if os.environ.get("FRIDAY_TOWN_REHEARSAL_EVIDENCE"):
            output = Path(os.environ["FRIDAY_TOWN_REHEARSAL_EVIDENCE"])
            output.write_text(json.dumps({
                "rehearsal_only": True, "catalog_revision": preview["catalog_revision"],
                "deployments": {pool: {"public_reference": ref, "synthetic_base": True}
                                for pool, (_, ref) in declarations.items()},
                "services": manifest["services"], "bindings": bindings,
                "placements": preview["placements"], "world_snapshot": preview["world_snapshot_id"],
                "first_receipt": installed, "first_backup": manifest_backup,
                "first_rollback": "rolled_back", "second_receipt": later,
                "second_rollback": "refused after simulated activity",
                "scratch": str(tmp_path),
            }, indent=2), encoding="utf-8")
    finally:
        store.close()
