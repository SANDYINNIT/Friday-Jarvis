"""LAB-3R6 regression coverage for the defects the user's live acceptance hit.

Each test pins a ROOT CAUSE discovered 2026-11-06 (not a symptom):
  1. drawWorld read `room.center` off a {loc, rect} wrapper -> TypeError
     -> the render threw -> ST="error" -> "Bridge call failed" + blank
     transport pills while the header said "connected".
  2. get_lab merged only name/needs/bar/blocked_reason, so x/y/state/goal/
     room were the throwaway spawn cells -> static sprites forever.
  3. get_lab never started the ticker (a populated but frozen world).
  4. ensure_started latched _started=True BEFORE the try -> one failed boot
     killed the ticker for the whole process (no reset() caller in prod).
  5. roster edges wrote display labels ("FRIDAY")/sentinels ("world","?")
     as endpoints -> 79 persisted rows un-drawable ("unknown agents").
  6. boot never cleared a persisted presence=away (frozen town on restart).
  7. The rendered-UI string contracts for the redesign (count pill split,
     chain section, human card labels).
All fixtures are explicit temp paths configured BEFORE any bridge use.
"""

import os
import sqlite3
import tempfile

import pytest

from source.server import lab_state as lab
from source.server import lab_authority as authority_mod
from source.server import lab_budget as budget_mod
from source.server import lab_artifacts as artifacts_mod
from source.server import lab_memory as memory_mod
from source.server.status_ui import StatusBus, WebviewBridge, \
    _boot_resume_presence
from source.server.world.store import Store
from source.server.world.roster import Roster


@pytest.fixture()
def lab_env(tmp_path, monkeypatch):
    database = tmp_path / "world.db"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
    lab.reset()
    lab._execution_queue_reset()
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    artifacts_mod.reset_shared()
    memory_mod.reset_shared()
    authority_mod.configure(tmp_path / "authority.db")
    budget_mod.configure(database)
    artifacts_mod.configure(database, root=str(tmp_path / "artifacts"))
    memory_mod.configure(tmp_path / "character_mem.db",
                         world_db=str(database))
    yield {"db": database,
           "bridge": WebviewBridge(StatusBus(), None)}
    lab.reset()
    lab._execution_queue_reset()
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    artifacts_mod.reset_shared()
    memory_mod.reset_shared()


class TestWorkspaceRendererContract:
    """Defect 1: the wrapper/rect .center TypeError."""

    def test_drawworld_reads_center_off_the_rect(self):
        from pathlib import Path
        js = (Path(__file__).parent / "source" / "server" / "ui" / "app.js"
              ).read_text(encoding="utf-8")
        # rooms[] entries are {loc, rect}; .center lives on the rect
        assert "room.rect || room" in js, (
            "drawWorld must read .center off room.rect - room.center was "
            "undefined and flipped ST to error ('Bridge call failed')")
        # a draw fault must not rewrite a healthy payload into ST=error
        assert "workspace render failed:" in js, (
            "RENDER.get_world must isolate drawWorld in its own try/catch")
        # and the error branch may only run for a real refresh() failure
        assert js.index("const rct = room.rect || room") < \
            js.index("void idx;")

    def test_error_branch_still_covers_real_transport_failures(self):
        # the st!=="ok" branch must still blank pills (real failures stay
        # honest) - only the DRAW path became isolated.
        from pathlib import Path
        js = (Path(__file__).parent / "source" / "server" / "ui" / "app.js"
              ).read_text(encoding="utf-8")
        assert '["world-clock", "world-tick", "world-speed", "world-paused"' in js


class TestGetLabLiveMerge:
    """Defects 2+3: static characters and a never-started ticker."""

    def test_get_lab_merges_live_position_state_goal(self, lab_env, monkeypatch):
        bridge = lab_env["bridge"]
        # spy on ensure_started: get_lab must ask for the ticker
        calls = {"n": 0}
        real = lab.ensure_started

        def spy():
            calls["n"] += 1
            return False                      # never spawn a thread here
        monkeypatch.setattr(lab, "ensure_started", spy)
        world, _roster = lab._load_world()
        # move a character OFF its spawn cell and change state/goal
        char = world.characters[0]
        char.x, char.y = 3, 11
        char.state = "eating"
        char.goal = "eat"
        payload = bridge.get_lab()
        monkeypatch.setattr(lab, "ensure_started", real)
        assert calls["n"] >= 1, (
            "get_lab must ensure the ticker (it used to render a populated "
            "but frozen world)")
        assert payload["map"].get("ok") is True, payload["map"]
        merged = {str(c.get("id")): c
                  for c in payload["map"].get("characters") or []}
        live = merged.get(str(char.id))
        assert live is not None, merged
        assert live.get("x") == 3 and live.get("y") == 11, (
            "position must come from the LIVE world, not the throwaway "
            "spawn world: %s" % live)
        assert live.get("state") == "eating", live
        assert live.get("goal") == "eat", live


class TestTickerRetry:
    """Defect 4: a failed start must be retryable."""

    def test_failed_start_releases_the_latch(self, lab_env, monkeypatch):
        assert lab._started is False
        boom = ["first"]
        # fail the real loader once
        real_load = lab._load_world

        def failing(*a, **k):
            if boom[0] == "first":
                raise RuntimeError("simulated boot fault")
            return real_load(*a, **k)
        monkeypatch.setattr(lab, "_load_world", failing)
        assert lab.ensure_started() is False
        assert lab._started is False, (
            "a failed start must release the latch (production has no "
            "reset() caller - one bad boot killed the ticker forever)")
        assert "simulated boot fault" in (lab._start_error or "")
        # recover: the SECOND call must actually retry and succeed
        boom[0] = "second"
        try:
            assert lab.ensure_started() is True
            assert lab._thread is not None
        finally:
            lab.stop()
            lab.reset()


class TestRosterEdgeContract:
    """Defect 5: edge endpoints must be node ids, not display labels."""

    def _roster(self, tmp_path):
        store = Store(str(tmp_path / "edge.db"))
        return store, Roster(store)

    def test_new_edges_use_real_ids_and_skip_unattributable(self, tmp_path):
        _store, roster = self._roster(tmp_path)
        roster.add_agent("friday", "FRIDAY", "coordinator")
        roster.add_agent("code", "Code", "builder")
        task = roster.create_task("ship it", owner="code")
        roster.assign(task["task"]["id"], "code")
        roster.pause_for_rest(task["task"]["id"], "code")
        edges = roster.edges()
        assert edges, edges
        for e in edges:
            assert e["from"] in ("friday", "code", "world"), e
            assert e["to"] in ("friday", "code", "world"), e
            assert e["from"] != "?" and e["to"] != "?", e
        # the assign edge names the boss by ID, never the display label
        assert any(e["from"] == "code" and e["to"] == "friday" and
                   e["kind"] == "assign" for e in edges), edges

    def test_legacy_display_label_rows_are_normalized_on_load(self, tmp_path):
        store, roster = self._roster(tmp_path)
        roster.add_agent("friday", "FRIDAY", "coordinator")
        roster.add_agent("code", "Code", "builder")
        roster._save()
        # simulate the legacy writer: display label + sentinel endpoints
        store._conn.execute(
            "INSERT INTO edges (src, dst, kind, state, at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("code", "FRIDAY", "assign", "active", 0.0, "legacy row"))
        store._conn.commit()
        fresh = Roster(store)
        fresh._load()
        edges = [e for e in fresh.edges() if e["kind"] == "assign"]
        assert edges, fresh.edges()
        assert edges[0]["from"] == "code"
        assert edges[0]["to"] == "friday", (
            "legacy 'FRIDAY' label rows must resolve to the agent id: %s"
            % edges[0])

    def test_unknown_endpoints_stay_visible_to_the_renderer(self, tmp_path):
        # rows the resolver cannot map are PRESERVED (never silently
        # discarded); the renderer counts them honestly instead.
        store, roster = self._roster(tmp_path)
        roster.add_agent("code", "Code", "builder")
        roster._save()
        store._conn.execute(
            "INSERT INTO edges (src, dst, kind, state, at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("code", "mystery-node", "assign", "active", 0.0, "?"))
        store._conn.commit()
        fresh = Roster(store)
        fresh._load()
        assert any(e["to"] == "mystery-node" for e in fresh.edges()), (
            fresh.edges())


class TestBootPresence:
    """Defect 6: a visible boot resumes free NPC life (unless paused)."""

    def test_away_presence_resumes_on_visible_boot(self, lab_env):
        lab._load_world()
        lab.set_presence("away")
        assert lab.presence() == "away"
        out = _boot_resume_presence(start_hidden=False)
        assert out.get("ok") is True, out
        assert lab.presence() == "open"
        assert lab._load_world()[0].paused is False

    def test_explicit_pause_is_respected(self, lab_env):
        lab._load_world()
        lab.set_presence("closed")
        out = _boot_resume_presence(start_hidden=False)
        assert out.get("ok") is True, out
        assert lab.presence() == "closed", (
            "an explicit Pause life must survive a boot")

    def test_hidden_boot_keeps_away(self, lab_env):
        lab._load_world()
        lab.set_presence("away")
        out = _boot_resume_presence(start_hidden=True)
        assert out.get("skipped") == "hidden boot", out
        assert lab.presence() == "away"


class TestRedesignSourceContracts:
    """The rendered redesign must ship the reconciled populations + human
    labels (live rendering exercised in the Playwright acceptance run)."""

    def test_count_pill_splits_residents_and_chain(self):
        from pathlib import Path
        js = (Path(__file__).parent / "source" / "server" / "ui" / "app.js"
              ).read_text(encoding="utf-8")
        # LAB-3R7 reworded the pill to plain language (design constraint:
        # no compressed abbreviations): "Residents 0/4 AI · Providers 11/12
        # AI (1 local)" - the population split and the null-local marker
        # remain the contract.
        assert "Residents " in js and "· Providers" in js, (
            "the life pill must label residents and providers separately")
        assert ("entry.local === null" in js or
                "local === undefined" in js), (
            "residents must be identified by local === null in the pill")

    def test_roster_chain_section_and_human_labels(self):
        from pathlib import Path
        root = Path(__file__).parent / "source" / "server" / "ui"
        js = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        assert 'function renderRosterChain' in js
        assert 'id="roster-chain"' in html
        assert "per request from the model chain" in js, (
            "'runtime selected' must not be shown as if it were a model")
        assert "SERVING AS FRIDAY" in js and "FRIDAY duty" in js
        # human labels on the main experience (raw method names moved to
        # collapsed diagnostics)
        assert "get_lab().map" not in html or '<details class="diag"' in html
        assert "live simulation" in html
        assert "selected character" in html
        assert '<details class="diag"' in html
        # camera/lAYOUT: fluid canvas + re-fit on resize + two columns
        assert "clamp(300px" in css
        assert "lab.fitted = false" in js
        assert "@media (max-width: 960px)" in css
        assert "inspector-card" in css and "inspector-card" in html
        # readable scene: name tags + room labels + restrained bubbles
        assert "roomList" in js, "room labels must be drawn on the town map"
