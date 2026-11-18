"""Bind model MCP proposals and local HUD consent to an admitted Light turn."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import time
from uuid import uuid4

PREFIX = "friday_mcp_"


def _field(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _literal_object(value):
    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate argument")
        return result

    result = json.loads(value, object_pairs_hook=pairs)
    json.dumps(result, allow_nan=False)
    if not isinstance(result, dict):
        raise ValueError("arguments must be an object")
    return result


class ModelTools:
    """Buffer one native MCP function call without forwarding it to OI's code parser."""
    def __init__(self, runtime, params, *, presentation=False, highlight=False, workflow=False):
        self.refs = {}
        self.name = self.arguments = ""
        self.held = []
        self.native = self.invalid = self.seen = self.finished = False
        self.bytes = 0
        definitions = []
        for reference in runtime.registrations() if runtime is not None else ():
            tool = reference[2]
            digest = hashlib.sha256(f"{tool.server}:{tool.name}".encode()).hexdigest()[:32]
            name = PREFIX + digest
            self.refs[name] = reference
            definitions.append({"type": "function", "function": {
                "name": name, "description": f"Propose local HUD approval for MCP tool {tool.server}:{tool.name}.",
                "parameters": deepcopy(tool.input_schema),
            }})
        if workflow:
            from .n8n_turn import WORKFLOW
            self.refs["friday_workflow_request"] = WORKFLOW
            definitions.append({"type": "function", "function": {
                "name": "friday_workflow_request",
                "description": "Propose an explicitly requested n8n operation for exact local owner review. Never use for ordinary chat unless the user selects n8n. Sending is not action approval: remote writes require a separate exact pending decision. No automatic retry after unknown remote outcome.",
                "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["capability", "message"], "properties": {
                        "capability": {"type": "string", "enum": list(workflow)},
                        "message": {"type": "string", "minLength": 1, "maxLength": 4000}}}}})
        if presentation:
            from .companion import PRESENTATION
            name = "friday_present"
            self.refs[name] = PRESENTATION
            definitions.append({"type": "function", "function": {
                "name": name,
                "description": "Show the user a local teaching card or notification. Display only: no tools, code, HTML, URLs or remote assets. Use a diagram with rect/ellipse/line/arrow/text shapes, or a 3D mesh with numeric vertices and indexed polygon faces. Never claim that a diagram or plan executed an action.",
                "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["id", "kind", "title", "text"], "properties": {
                        "id": {"type": "string", "maxLength": 64},
                        "kind": {"type": "string", "enum": ["lesson", "notification"]},
                        "title": {"type": "string", "maxLength": 120},
                        "text": {"type": "string", "maxLength": 8000},
                        "scene": {"type": "object", "properties": {
                            "type": {"type": "string", "enum": ["diagram", "mesh"]},
                            "shapes": {"type": "array", "maxItems": 64, "items": {"type": "object"}},
                            "vertices": {"type": "array", "maxItems": 256, "items": {"type": "array", "items": {"type": "number"}}},
                            "faces": {"type": "array", "maxItems": 512, "items": {"type": "array", "items": {"type": "integer"}}}
                        }, "additionalProperties": False}
                    }}}})
        if highlight:
            from .companion import HIGHLIGHT
            self.refs["friday_highlight"] = HIGHLIGHT
            definitions.append({"type":"function","function":{
                "name":"friday_highlight",
                "description":"Draw one temporary display-only highlight on an exact window the user asked to mark. Never clicks or edits the app. Use verified current virtual-desktop pixel bounds from UI inspection, never guessed coordinates. Rect is [left,top,right,bottom]; ordered points permit oriented arrows. Existing visual consent still applies.",
                "parameters":{"type":"object","additionalProperties":False,
                    "required":["query","shape","label"],"properties":{
                        "query":{"type":"string","maxLength":120},
                        "shape":{"type":"string","enum":["rect","arrow","label"]},
                        "label":{"type":"string","maxLength":80},
                        "rect":{"type":"array","minItems":4,"maxItems":4,"items":{"type":"integer"}},
                        "points":{"type":"array","minItems":2,"maxItems":2,"items":{"type":"array","minItems":2,"maxItems":2,"items":{"type":"integer"}}},
                        "ttl":{"type":"number","minimum":1,"maximum":30}
                    }}}})
        if definitions:
            params["tools"] = [*params["tools"], *definitions]

    def filter(self, chunk):
        choices = _field(chunk, "choices") or ()
        if not choices:
            return [chunk] if not self.seen or self.native else []
        choice = choices[0]
        finish = _field(choice, "finish_reason")
        delta = _field(choice, "delta") or {}
        calls = _field(delta, "tool_calls") or ()
        legacy = _field(delta, "function_call")
        if self.native:
            # MCP cannot follow a built-in invocation in the same completion.
            if any(_field(call, "index", 0) != 0 for call in calls):
                return []
            return [chunk]
        if not calls and not legacy and not self.seen:
            return [chunk]
        if calls or legacy:
            self.seen = True
            if len(choices) != 1 or len(calls) > 1 or (calls and legacy):
                self.invalid = True
            for call in calls or ():
                if _field(call, "index", 0) != 0:
                    self.invalid = True
            function = _field(calls[0], "function") if calls else legacy
            self.name += _field(function, "name") or ""
            arguments = _field(function, "arguments") or ""
            self.bytes += len(arguments)
            # Presentation remains bounded, but a mesh can legitimately need
            # more than the remote-tool argument budget. MCP retains 4 KiB.
            argument_limit = 48000 if self.name == "friday_present" else 4096
            if len(self.name) > 80 or self.bytes > argument_limit:
                self.invalid = True
            if not self.invalid and self.name in {"execute", "python", "functions"}:
                self.native = True
                output, self.held = self.held + [chunk], []
                return output
            if not self.invalid:
                self.arguments += arguments
                if len(self.held) < 128:
                    self.held.append(chunk)
                else:
                    self.invalid = True
        if finish:
            self.finished = finish in ("tool_calls", "function_call")
        return []

    def proposal(self):
        if not self.seen or self.native:
            return None
        if self.invalid or not self.finished or self.name not in self.refs:
            return False
        try:
            return self.refs[self.name], _literal_object(self.arguments)
        except (ValueError, TypeError, RecursionError):
            return False


@dataclass
class Proposal:
    id: str
    owner: object
    generation: object
    reference: tuple
    arguments: dict
    deadline: float
    approved: bool | None = None
    dispatched: bool = False


class MCPTurn:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = None

    def preview(self):
        item = self.pending
        if item is None or item.approved is not None or item.deadline <= time.monotonic():
            return None
        tool = item.reference[2]
        return {"id": item.id, "tool": f"{tool.server}:{tool.name}",
                "arguments": json.dumps(item.arguments, ensure_ascii=False, sort_keys=True)}

    def decide(self, request_id, approve, live):
        item = self.pending
        if (item is None or item.id != request_id or type(approve) is not bool
                or item.approved is not None or not live(item)
                or item.deadline <= time.monotonic() or not self.runtime.current(item.reference)):
            return False
        item.approved = approve
        return True

    async def execute(self, owner, generation, reference, arguments, deadline, live, spend, report):
        if self.pending is not None or not self.runtime.validate(reference, arguments):
            return "MCP proposal refused: invalid arguments or stale tool registration."
        item = self.pending = Proposal(uuid4().hex, owner, generation, reference,
                                       arguments, min(deadline, time.monotonic() + 45))
        try:
            while item.approved is None:
                if not live(item) or not self.runtime.current(reference):
                    return "MCP proposal expired before dispatch."
                await asyncio.sleep(0.1)
            if not item.approved:
                return "MCP call declined before dispatch."
            dispatched = False

            def permit():
                nonlocal dispatched
                dispatched = live(item) and spend(item)
                item.dispatched = dispatched
                return dispatched

            task = asyncio.create_task(self.runtime.dispatch(reference, arguments, permit))
            try:
                while not task.done():
                    if not live(item):
                        return "MCP stopped. Remote outcome unknown." if dispatched else "MCP stopped before dispatch."
                    await asyncio.wait({task}, timeout=0.1)
                if not live(item):
                    return "MCP stopped. Remote outcome unknown." if dispatched else "MCP stopped before dispatch."
                result = task.result()
            finally:
                if not task.done():
                    task.cancel()
                    task.add_done_callback(_discard)
            if result.get("result") is not None and not self.runtime.current(reference):
                return "MCP session or schema changed. Remote outcome unknown."
            if result.get("result") is not None:
                report(item, json.dumps(result["result"], ensure_ascii=False))
            return ("MCP tool returned a result. See the local tool output." if result["ok"]
                    else result["error"])
        finally:
            if self.pending is item:
                self.pending = None


def _discard(task):
    if not task.cancelled():
        task.exception()
