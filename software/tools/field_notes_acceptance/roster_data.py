"""Synthetic stores/observations for shipped Roster browser checks only."""
from copy import deepcopy
import os
from threading import Thread
from types import SimpleNamespace

from source.server import lab_state
from source.server.identity_integration import IdentityIntegration
from source.server.world import Store, sim
from source.server.world.identity_catalog import IdentityCatalog, Route
from source.server.world.identity_shadow import DutyShadow
from source.server.world.identity_reader import IdentityReader, ResidentLink


class RosterData:
    def __init__(self, area, runtime, bridge, *, installed=False, controls=False):
        self.runtime, self.bridge = runtime, bridge
        self.primary = Route("fixture", "primary", "fixture-primary")
        self.fallback = Route("fixture", "fallback", "fixture-fallback")
        self.local = Route("ollama", "workshop", "fixture-local")
        self.catalog = IdentityCatalog(area / "catalog.db")
        def item(route, caps, kind="reasoning"):
            return {**route.public(), "capabilities": caps, "kind": kind}
        self.inventory = [item(self.primary,["brain","vision"]),item(self.fallback,["brain"]),
                          item(self.local,["brain"]),item(Route("fixture","listen","fixture-listener"),["listen"],"service"),
                          item(Route("fixture","voice","fixture-speaker"),["voice_out"],"service"),
                          item(Route("fixture","workflow","fixture-workflows"),["workflow"],"service")]
        self.catalog.reconcile(self.inventory, complete=True, legacy_refs=[("memory","research")])
        self.shadow = DutyShadow(self.catalog.path, area / "history.db", enabled=True)
        self.world = sim.new_world(seed=19)
        self.world.characters[0].memory = ["Original synthetic memory; ownership unchanged"]
        self.store = Store(area / "source-world.db")
        self.store.save(self.world)
        lab_state._world = self.world
        lab_state._store = self.store
        lab_state.ensure_started = lambda: False
        if controls:
            routes = [(self.primary, "brain"), (self.primary, "vision"),
                      (self.fallback, "brain"), (self.local, "brain")]
            matchers = {(cap, route.deployment): ("http://fixture.invalid", route.provider,
                        route.deployment, frozenset([route.model])) for route, cap in routes}
            integration = IdentityIntegration(self.catalog, self.shadow, matchers)
        else:
            integration = SimpleNamespace(shadow=self.shadow)
        runtime.server_state["identity_integration"] = integration
        os.environ["FRIDAY_IDENTITY_READER"] = "1"  # this fixture process only
        pair = bridge.get_identity_views()
        self.ids = {binding["deployment"]: row["id"] for row in pair["roster"]["characters"] for binding in row["bindings"]}
        if controls:
            self.world.identity_links = [ResidentLink("town","friday",self.ids["primary"],"fixture-approved-link",True).__dict__]
        self.request = self.brain = self.vision = None
        if installed:
            self.world.paused = True
            self.world.characters[0].needs["energy"] = 96
            self.world.characters[0].goal = "rest"
            reader = IdentityReader(self.shadow, lab_state.capture_identity_world, enabled=True)
            links = [ResidentLink("town","friday",self.ids["primary"],"fixture-approved-link",True)]
            proposal = reader.snapshot(links=links)["installation_proposal"]
            result = lab_state.install_catalog_residents(self.catalog,reader,proposal,links=links)
            assert result["ok"], result
            self.world = lab_state._world
            self.inventory.append(item(Route("fixture","future","fixture-proposal"),["brain"]))
            self.catalog.reconcile(self.inventory,complete=True)
            # Actual ticker. The fixture starts paused so manual steps are deterministic.
            lab_state._stop.clear()
            lab_state._thread = Thread(target=lab_state._loop,daemon=True)
            lab_state._thread.start()

    def control(self, command):
        action = command["action"]
        if action == "town:unloaded":
            with lab_state._lock:
                lab_state._stop.set()
                lab_state._world = lab_state._store = None
                def forbidden(*_args, **_kwargs):
                    raise AssertionError("Town initialized an unavailable fixture store")
                lab_state._load_world = forbidden
            return self.bridge.get_town_view()
        if action == "town:state":
            world, journal, _ = lab_state.capture_town_view()
            return {"pair": self.bridge.get_town_view(), "journal": journal,
                    "planner_calls": world.planner_calls, "memories": {c.id:c.memory for c in world.characters}}
        if action == "roster:split":
            if self.request:
                self.shadow.end_request(self.request)
            self.request = self.shadow.begin_request("fixture-request")
            self.brain = self.shadow.observe_selected(self.request,"brain",1,self.primary)
            self.vision = self.shadow.observe_selected(self.request,"vision",1,self.primary)
            self.shadow.finish(self.brain,"failure")
            self.brain = self.shadow.observe_selected(self.request,"brain",2,self.fallback)
        elif action == "roster:finish-brain":
            self.shadow.finish(self.brain,"success")
        elif action == "roster:disabled":
            os.environ["FRIDAY_IDENTITY_READER"] = "0"
        elif action == "roster:enabled":
            os.environ["FRIDAY_IDENTITY_READER"] = "1"
        elif action == "roster:archive":
            self.catalog.reconcile([row for row in self.inventory if row["deployment"] != "primary"],complete=True)
        elif action == "roster:restore":
            self.catalog.reconcile(self.inventory,complete=True)
        elif action == "roster:map-primary":
            self.world.identity_links = [ResidentLink("town","friday",self.ids["primary"],"fixture-approved-link",True).__dict__]
        elif action == "roster:empty":
            self.catalog.reconcile([],complete=True)
        elif action == "roster:transport-error":
            self.runtime.roster_error = True
        elif action == "roster:transport-recover":
            self.runtime.roster_error = False
        elif action != "roster:read":
            raise ValueError("unknown roster fixture action")
        return {"ids": self.ids, "pair": self.bridge.get_identity_views(),
                "source_ids": [char.id for char in self.world.characters],
                "memories": deepcopy(self.world.characters[0].memory)}
