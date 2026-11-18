"""Explicit admitted-turn n8n proposal, using Light's existing live/spend guards."""
import asyncio
from dataclasses import dataclass
import json
import time
from uuid import uuid4
from .n8n_bridge import CAPABILITIES, Refused, bounded

WORKFLOW = "friday-workflow-request-v1"


@dataclass
class Request:
    id: str
    owner: object
    generation: object
    arguments: dict
    deadline: float
    approved: bool | None = None


class WorkflowTurn:
    def __init__(self, bridge):
        self.bridge = bridge
        self.pending = None

    def preview(self):
        item = self.pending
        if item is None or item.approved is not None or time.monotonic() >= item.deadline:
            return None
        # Request arguments are explicit human review, not general diagnostics.
        return {"id": item.id, "kind": "n8n_request", "capability": item.arguments["capability"],
                "message": item.arguments["message"], "expires_in": max(0, int(item.deadline-time.monotonic()))}

    def decide(self, proposal_id, choice, live):
        item = self.pending
        if (item is None or item.id != proposal_id or choice not in ("send", "cancel")
                or item.approved is not None or not live(item) or time.monotonic() >= item.deadline):
            return {"ok": False, "error": "stale_proposal"}
        item.approved = choice == "send"
        return {"ok": True}

    async def execute(self, owner, generation, arguments, deadline, live, spend):
        if self.pending is not None:
            return {"ok": False, "error": "proposal_busy"}
        if (not isinstance(arguments, dict) or set(arguments) != {"capability", "message"}
                or arguments.get("capability") not in CAPABILITIES
                or not isinstance(arguments.get("message"), str) or not 1 <= len(arguments["message"]) <= 4000):
            return {"ok": False, "error": "invalid_arguments"}
        bounded(arguments, 8000)
        item = self.pending = Request(uuid4().hex, owner, generation, arguments.copy(),
                                      min(deadline, time.monotonic()+45))
        try:
            while item.approved is None:
                if not live(item) or time.monotonic() >= item.deadline:
                    return {"ok": False, "error": "stopped_or_expired"}
                await asyncio.sleep(0.05)
            if not item.approved:
                return {"ok": False, "error": "cancelled_before_dispatch"}
            if not live(item):
                return {"ok": False, "error": "stopped_or_expired"}
            # Spend is checked in Bridge immediately before dispatch and only once.
            payload = {**arguments, "turn_id": item.id, "generation": item.id}
            task = asyncio.create_task(asyncio.to_thread(self.bridge.operate, "request", payload,
                        request_id=item.id, live=lambda: live(item), spend=lambda: spend(item)))
            try:
                while not task.done():
                    if not live(item):
                        self.bridge.invalidate()
                        return {"ok": False, "error": "stopped", "outcome": "unknown",
                                "message": "Local wait stopped. Remote cancellation is not established."}
                    await asyncio.wait({task}, timeout=0.05)
                return task.result() if live(item) else {"ok": False, "error": "stale_result", "outcome": "unknown"}
            finally:
                if not task.done():
                    task.cancel()
        finally:
            if self.pending is item:
                self.pending = None


async def event_loop(bridge, stopped):
    """Server-owned lifecycle task, independent of page refresh; 30s bounded cadence."""
    while not stopped.is_set():
        try:
            value = bridge.config.read()
            if value["enabled"] and value["notifications"]:
                await asyncio.to_thread(bridge.pull)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stopped.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
