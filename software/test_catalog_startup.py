import json
import os
import sqlite3
import sys
from types import SimpleNamespace
import urllib.error

import pytest
from typer.testing import CliRunner

from source.server import catalog_startup as startup
from source.server.identity_integration import discover_inventory
from source.server.world.identity_catalog import IdentityCatalog, read_shadow_projection
from source.server.world.identity_install import apply
from source.server.world.identity_reader import IdentityReader
from source.server.world.identity_shadow import DutyShadow
from source.server.world.store import Store
from source.server.world.sim import new_world


@pytest.fixture
def deployment(tmp_path):
    profile = tmp_path / ".friday"
    private = profile / "catalog-activation"
    private.mkdir(parents=True)
    paths = SimpleNamespace(manifest=private / "deployments.json", catalog=private / "identity-catalog.db",
                            history=private / "identity-duty-history.db", world=profile / "world.db")
    endpoint = "https://private-endpoint.invalid/v1"
    local = "http://127.0.0.1:11434"
    router = SimpleNamespace(MODAL_BASE=endpoint, LOCAL_MODEL="ollama_chat/local", LOCAL_BASE=local,
                             resolve=lambda _: [("brain_modal", "vendor/primary", endpoint), ("local", None, local)])
    vision = SimpleNamespace(_modal_vision_model=lambda: "vendor/primary", VISION_GEMINI_CHAIN=["gemini-test"],
                             GEMINI_BASE="https://fixture-gemini.invalid", OPENROUTER_VISION_MODEL="image-model",
                             OPENROUTER_BASE="https://fixture-openrouter.invalid", LOCAL_VISION_CHAIN=["local-vision"],
                             select_vision_model=lambda: "local-vision", LOCAL_CHAT_URL=local + "/api/chat")
    manifest = {"complete": True, "deployments": {
        "brain_modal": {"deployment": "modal-primary", "base": endpoint},
        "vision_modal": {"deployment": "modal-primary", "base": endpoint},
        "local": {"deployment": "ollama-local", "base": local},
        "vision_local": {"deployment": "ollama-local", "base": vision.LOCAL_CHAT_URL},
        "vision_gemini": {"deployment": "gemini-main", "base": vision.GEMINI_BASE},
        "vision_openrouter": {"deployment": "openrouter-main", "base": vision.OPENROUTER_BASE},
    }, "services": [{"provider": "groq", "deployment": "groq-listen", "model": "whisper-test",
                     "kind": "service", "capabilities": ["listen"]}]}
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    rows, _ = discover_inventory(manifest, router, vision)
    catalog = IdentityCatalog(paths.catalog)
    revision = catalog.reconcile(rows, complete=True)
    shadow = DutyShadow(paths.catalog, paths.history, enabled=True)
    world = new_world(seed=17)
    world.paused = True
    store = Store(paths.world)
    try:
        assert store.save(world)["ok"]
        reader = IdentityReader(shadow, lambda: world, enabled=True)
        _, receipt = apply(catalog, store, world, reader, reader.snapshot()["installation_proposal"])
        assert receipt["ok"]
    finally:
        store.close()
    binding = next(row for row in read_shadow_projection(paths.catalog)["bindings"].values()
                   if row["provider"] == "modal" and row["capability"] == "brain")
    revision = catalog.set_serving(binding["character_id"], binding["id"], "brain", False, revision, lambda _: True)
    return SimpleNamespace(paths=paths, home=tmp_path, manifest=manifest, resolver=(router, vision),
                           revision=revision, endpoint=endpoint)


def prepare(fixture, env=None):
    return startup.prepare(env or {}, fixture.home, resolver=fixture.resolver)


def test_existing_catalog_attaches_without_reconciliation_or_town_write(deployment, monkeypatch):
    paths = deployment.paths
    world_before, catalog_before = paths.world.read_bytes(), paths.catalog.read_bytes()
    before = read_shadow_projection(paths.catalog)
    monkeypatch.setattr(IdentityCatalog, "reconcile", lambda *_, **__: pytest.fail("startup tried to reconcile"))
    selected = prepare(deployment)
    assert (selected.catalog, selected.history, selected.world, selected.revision) == (
        paths.catalog, paths.history, paths.world, deployment.revision)
    attached = selected.attach()
    assert attached.route_disabled("brain", "brain_modal", "vendor/primary", deployment.endpoint)
    assert not attached.route_disabled("vision", "vision_modal", "vendor/primary", deployment.endpoint)
    assert read_shadow_projection(paths.catalog) == before
    assert paths.catalog.read_bytes() == catalog_before and paths.world.read_bytes() == world_before
    assert deployment.endpoint not in repr(selected)


@pytest.mark.parametrize("bad", ["missing", "malformed", "incomplete", "duplicate", "omitted-pool",
                                "endpoint-change", "new-model", "wrong-catalog", "wrong-history", "missing-links"])
def test_invalid_configuration_fails_without_store_mutation(deployment, bad):
    paths = deployment.paths
    if bad == "missing":
        paths.manifest.unlink()
    elif bad == "malformed":
        paths.manifest.write_text("invalid " + deployment.endpoint, encoding="utf-8")
    elif bad == "duplicate":
        paths.manifest.write_text('{"complete":false,"complete":true}', encoding="utf-8")
    elif bad in ("incomplete", "omitted-pool", "endpoint-change"):
        if bad == "incomplete":
            deployment.manifest["complete"] = False
        elif bad == "omitted-pool":
            del deployment.manifest["deployments"]["vision_gemini"]
        else:
            deployment.manifest["deployments"]["brain_modal"]["base"] += "/changed"
        paths.manifest.write_text(json.dumps(deployment.manifest), encoding="utf-8")
    elif bad == "new-model":
        deployment.resolver[1].VISION_GEMINI_CHAIN.append("unexpected-model")
    elif bad in ("wrong-catalog", "wrong-history"):
        target = paths.catalog if bad == "wrong-catalog" else paths.history
        target.write_bytes(b"not a database")
    else:
        store = Store(paths.world)
        try:
            with store.transaction():
                store._exec_tx("DELETE FROM identity_resident_links")
        finally:
            store.close()
    snapshots = {path: path.read_bytes() for path in (paths.catalog, paths.history, paths.world)}
    with pytest.raises(startup.CatalogStartupError) as raised:
        prepare(deployment)
    assert deployment.endpoint not in str(raised.value)
    assert all(path.read_bytes() == contents for path, contents in snapshots.items())


@pytest.mark.parametrize("env,match", [
    ({"FRIDAY_CATALOG_MODE": "automatic"}, "FRIDAY_CATALOG_MODE"),
    ({"FRIDAY_IDENTITY_INTEGRATION": "0"}, "both identity gates"),
    ({"FRIDAY_IDENTITY_READER": "0"}, "both identity gates"),
    ({"FRIDAY_TOWN_INSTALL_OPERATOR": "1"}, "FRIDAY_TOWN_INSTALL_OPERATOR"),
    ({"FRIDAY_IDENTITY_CATALOG_DB": "override.db"}, "Clear identity selector"),
    ({"FRIDAY_WORLD_DB": "another-world.db"}, "FRIDAY_WORLD_DB"),
    ({"FRIDAY_CATALOG_MODE": "explicit", "FRIDAY_IDENTITY_DEPLOYMENTS": "{}"}, "complete inline manifest"),
    ({"FRIDAY_CATALOG_MODE": "legacy", "FRIDAY_IDENTITY_READER": "1"}, "Legacy mode conflicts"),
])
def test_conflicting_overrides_fail(deployment, env, match):
    with pytest.raises(startup.CatalogStartupError, match=match):
        prepare(deployment, env)


def test_explicit_and_legacy_choices(deployment):
    paths = deployment.paths
    assert prepare(deployment, {"FRIDAY_CATALOG_MODE": "legacy"}) is None
    env = {"FRIDAY_CATALOG_MODE": "explicit", "FRIDAY_IDENTITY_DEPLOYMENTS": json.dumps(deployment.manifest),
           "FRIDAY_IDENTITY_CATALOG_DB": str(paths.catalog), "FRIDAY_IDENTITY_HISTORY_DB": str(paths.history),
           "FRIDAY_WORLD_DB": str(paths.world)}
    assert prepare(deployment, env).revision == deployment.revision


def test_existing_only_attach_wont_recreate_missing_history(deployment):
    selected = prepare(deployment)
    selected.history.unlink()
    with pytest.raises(startup.CatalogStartupError, match="could not be attached"):
        selected.attach()
    assert not selected.history.exists()


def test_pending_rollback_reports_recovery_and_leaves_files_alone(deployment, monkeypatch):
    def pending_rollback(*_):
        error = sqlite3.OperationalError("private backend text")
        error.sqlite_errorcode = sqlite3.SQLITE_READONLY_ROLLBACK
        raise error

    paths = deployment.paths
    snapshots = {path: path.read_bytes() for path in (paths.catalog, paths.history, paths.world)}
    monkeypatch.setattr(startup, "_check_world", pending_rollback)
    with pytest.raises(startup.CatalogStartupError, match="pending SQLite rollback journal") as raised:
        prepare(deployment)
    assert "private backend text" not in str(raised.value)
    assert all(path.read_bytes() == contents for path, contents in snapshots.items())


def test_catalog_town_loading_skips_migration_and_visibility_preserves_pause(deployment, monkeypatch):
    from source.server import lab_state as lab, status_ui

    lab.reset()
    try:
        def forbid(*_, **__):
            pytest.fail("world migration attempted")
        monkeypatch.setattr(Store, "_migrate", forbid)
        lab.select_installed_world(deployment.paths.world)
        world, _ = lab._load_world()
        assert world.paused
        start_tick = world.tick
        world_bytes = deployment.paths.world.read_bytes()
        assert status_ui._boot_resume_presence(start_hidden=False)["ok"]
        assert lab.set_presence("open", visibility=True)["ok"]
        assert world.paused and world.tick == start_tick and deployment.paths.world.read_bytes() == world_bytes
        assert lab.command("play")["ok"]
        assert not world.paused
        assert lab.set_presence("away", visibility=True)["ok"] and world.paused
        assert lab.set_presence("open", visibility=True)["ok"] and not world.paused
    finally:
        lab.reset()


@pytest.mark.parametrize("background", [False, True])
def test_shared_cli_passes_prepared_identity_to_light(deployment, monkeypatch, background):
    import main
    import source.server
    from source.server import lab_state, light_lifecycle, tls_trust

    def refuse_external(*_, **__):
        raise urllib.error.URLError("no local server")
    monkeypatch.setattr("urllib.request.urlopen", refuse_external)
    monkeypatch.setattr(main, "time", SimpleNamespace(sleep=lambda *_: None))
    real_prepare = startup.prepare
    monkeypatch.setattr(startup, "prepare",
                        lambda: real_prepare(os.environ, deployment.home, resolver=deployment.resolver))
    for name in (*startup._SELECTORS, *startup._GATES, "FRIDAY_CATALOG_MODE", "FRIDAY_WORLD_DB",
                 "FRIDAY_TOWN_INSTALL_OPERATOR", "FRIDAY_AUDIO_STARTUP_CHECK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tls_trust, "bootstrap_windows_tls", lambda: None)
    invoked = []

    def server(host, port, interpreter, voice, debug, bus):
        assert os.environ["FRIDAY_IDENTITY_INTEGRATION"] == os.environ["FRIDAY_IDENTITY_READER"] == "1"
        assert os.environ.get("FRIDAY_TOWN_INSTALL_OPERATOR") in (None, "0")
        assert interpreter.friday_startup_identity.catalog.path == deployment.paths.catalog
        assert lab_state.db_path() == str(deployment.paths.world)
        invoked.append("light")

    def thread(target, args):
        return SimpleNamespace(start=lambda: target(*args))

    # Replace external boot effects. The shipped CLI and catalog attachment remain real.
    monkeypatch.setattr(source.server, "server", SimpleNamespace(start_server=server), raising=False)
    monkeypatch.setitem(sys.modules, "source.server.server", source.server.server)
    monkeypatch.setattr(main, "threading", SimpleNamespace(Thread=thread))
    monkeypatch.setattr(main, "signal", SimpleNamespace(SIGINT=2, SIGTERM=15, signal=lambda *_: None))
    monkeypatch.setitem(sys.modules, "source.clients.light-python.client", SimpleNamespace(run=lambda *_: None))
    monkeypatch.setattr(light_lifecycle, "run_hud", lambda *args: invoked.append("hud"))
    profile = deployment.home / "test-profile.py"
    profile.write_text("from types import SimpleNamespace\ninterpreter = SimpleNamespace()\n", encoding="utf-8")
    args = ["--status-ui", "--profile", str(profile)]
    args += ["--background", "--client", "light-python"] if background else ["--server", "light"]
    lab_state.reset()
    try:
        result = CliRunner().invoke(main.app, args)
        assert result.exit_code == 0, repr(result.exception)
        assert invoked == ["light", "hud"]
        assert deployment.endpoint not in result.output
    finally:
        lab_state.reset()


def test_cli_missing_configuration_exits_before_services_or_profile(deployment, monkeypatch):
    import main

    deployment.paths.manifest.unlink()
    real_prepare = startup.prepare
    monkeypatch.setattr(startup, "prepare",
                        lambda: real_prepare({}, deployment.home, resolver=deployment.resolver))
    monkeypatch.setattr(main.importlib.util, "spec_from_file_location",
                        lambda *_, **__: pytest.fail("profile loaded despite invalid catalog"))
    result = CliRunner().invoke(main.app, ["--server", "light", "--profile", str(deployment.paths.world)])
    assert result.exit_code == 78 and "selected manifest or identity database pair" in result.output
    assert deployment.endpoint not in result.output

