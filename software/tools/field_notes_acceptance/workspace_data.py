"""Isolated real task ledger and artifact shelf with a controllable model runner."""
from collections import defaultdict
from pathlib import Path
from threading import Event, Lock

from source.server import lab_artifacts, lab_state as lab
from source.server.world.executor import AgentExecutor


class WorkspaceData:
    def __init__(self, area, bridge):
        self.path = area / 'workspace-world.db'
        self.root = area / 'workspace-artifacts'
        self.bridge = bridge
        self.calls = defaultdict(list)
        self.lock = Lock()
        self.read_failure = False
        lab.db_path = lambda: str(self.path)
        lab.ensure_started = lambda: False  # no background world loop is needed
        lab._live_duty_active = lambda: False
        self.shelf = lab_artifacts.configure(self.path, root=str(self.root))
        self.open_world()

    def open_world(self):
        _, self.roster = lab._load_world()
        lab._executor = AgentExecutor(self.roster, runner=self.run, note=lab.note)

    def historical_result(self, area, runtime):
        """Exercise the default worker with fake HTTP and a real catalog."""
        import os
        from types import SimpleNamespace
        from source.server import brain_router, identity_integration, lab_authority
        from source.server.world import executor
        from source.server.world.identity_catalog import IdentityCatalog, Route
        from source.server.world.identity_shadow import DutyShadow

        route = Route("modal", "workspace-fixture", "vendor/fixture")
        catalog = IdentityCatalog(area / "workspace-catalog.db")
        catalog.reconcile([{**route.public(), "kind": "reasoning", "capabilities": ["brain"]}],
                          complete=True)
        shadow = DutyShadow(catalog.path, area / "workspace-duty.db", enabled=True)
        base = "https://workspace-provider.invalid/v1"
        integration = identity_integration.IdentityIntegration(catalog, shadow, {
            ("brain", "brain_modal"): (base, route.provider, route.deployment, {route.model})})
        runtime.server_state["identity_integration"] = integration
        os.environ["FRIDAY_IDENTITY_READER"] = "1"  # this offline fixture only
        brain_router.resolve = lambda _: [("brain_modal", route.model, base)]
        brain_router.api_pools.pool = lambda _: SimpleNamespace(next_key=lambda: "fixture-key")
        brain_router.api_pools.model_succeed = lambda *_: None
        lab_authority.pool_user_disabled = lambda *_: False
        executor._post_chat = lambda *_: (200, {"choices": [{"message": {"content": "Fixture output"}}]})
        lab._executor = AgentExecutor(self.roster, identity=integration, note=lab.note)
        row = self.roster.create_assignment("Review the recorded executor", "research")["task"]
        assert lab.execute_task(row["id"], background=False)["ok"]
        catalog.reconcile([], complete=True)

    def run(self, goal, **_):
        if goal.startswith('Fail'):
            return {'ok': False, 'error': 'Synthetic executor failure'}
        ready = Event()
        with self.lock:
            self.calls[goal].append(ready)
            attempt = len(self.calls[goal])
        if not ready.wait(40):
            return {'ok': False, 'error': 'Synthetic runner release timed out'}
        return {'ok': True, 'text': f'Persisted output for {goal} (run {attempt})\n<em>Literal result text</em>',
                'provider': 'fixture-provider', 'model': 'fixture-executor'}

    def snapshot(self):
        if self.read_failure:
            return {'error': 'Synthetic ledger read failed.'}
        return self.bridge.get_roster()

    def control(self, command):
        action = command['action']
        if action == 'workspace:release':
            with self.lock:
                self.calls[command['goal']][-1].set()
        elif action == 'workspace:reopen':
            thread = lab._executor_thread
            if thread is not None:
                thread.join(timeout=3)
                assert not thread.is_alive()
            lab._execution_queue_reset()
            lab.reset()
            self.open_world()
        elif action == 'workspace:artifact':
            task_id, state = command['task_id'], command['state']
            if state == 'error':
                current = self.roster.task(task_id)
                assert self.shelf.record(task_id, current['attempt'], text='Synthetic error bytes',
                                         ok=False, error='Synthetic artifact error')['ok']
            else:
                artifact = self.shelf.list(task_id=task_id)[0]
                if state == 'revoked':
                    assert self.shelf.revoke(artifact['id'])['ok']
                elif state == 'missing':
                    original = Path(artifact['path']).resolve()
                    assert original.is_relative_to(self.root.resolve())
                    original.rename(original.with_suffix('.fixture-saved'))
                else:
                    raise ValueError('Unsupported fixture artifact state')
        elif action == 'workspace:read-error':
            self.read_failure = bool(command['value'])
        elif action != 'workspace:snapshot':
            raise ValueError('Unsupported Workspace fixture action')
        return self.snapshot()
