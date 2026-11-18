"""ZB-L4-3: execute the production trigger and completion closure across turns.

The provider is a deterministic stream, not a live brain. Queue tests execute
the production admission branch, so an overlapping arrival cannot re-arm the
active turn. This extends the existing OpenCode harness without replacing it.
"""
import ast
import asyncio
import collections
from pathlib import Path

import pytest

from test_tool_cap_nudge import _Turn
from source.server.server import (
    _pending_push,
    _stamp_progress,
    restore_turn_baseline,
    snapshot_turn_baseline,
)

SERVER = Path(__file__).parent / "source/server/server.py"


def call(turn, model="openai/test", extra=None):
    params = {"messages": list(turn.messages), "model": model}
    if extra is not None:
        params["extra_body"] = dict(extra)
    list(turn.ns["traced_completions"](**params))
    # **params creates a new dict at the call boundary. Observe the actual
    # provider input, not the caller's unmodified dictionary.
    return turn.interpreter.llm.last_params


def begin(turn):
    turn.user("next independent request")
    params = call(turn)
    assert turn.state["tool_cap_nudged"] is False
    assert turn.state["pending_tool_cap_nudge"] is None
    assert turn.ns["tool_counter"]["n"] == 0
    assert "extra_body" not in params


def tool(turn, number):
    turn.assistant_code("print(%d)" % number)
    turn.ns["tool_counter"]["n"] = number
    turn.fire_tool_cap_nudge()
    turn.console(str(number))


@pytest.mark.parametrize("model", ["openai/test", "ollama_chat/qwen3:8b", "gemini/test"])
def test_two_long_turns_rearm_once_each_and_preserve_l4_2(model):
    turn = _Turn(tool_cap=12)
    for _ in range(2):
        begin(turn)
        for number in range(1, 12):
            tool(turn, number)
            assert turn.state["tool_cap_nudged"] is False
            assert "extra_body" not in call(turn, model)
        tool(turn, 12)
        assert turn.state["tool_cap_nudged"] is True
        assert turn.state["pending_tool_cap_nudge"]
        params = call(turn, model, {"keep": 7})
        assert params["extra_body"]["keep"] == 7
        assert (params["extra_body"].get("reasoning_effort") == "low") == model.startswith("openai/")
        if "qwen3" in model:
            assert params["think"] is False
        tool(turn, 13)
        assert turn.state["pending_tool_cap_nudge"] is None
        assert "extra_body" not in call(turn, model)
    assert turn.ns["__traces"].count("tool_cap_nudge_delivered") == 2


@pytest.mark.parametrize("lengths", [(12, 0, 12), (12, 3, 12), (1, 2, 0)])
def test_short_turns_neither_inherit_nor_set_latch(lengths):
    turn = _Turn(tool_cap=12)
    for length in lengths:
        begin(turn)
        for number in range(1, length + 1):
            tool(turn, number)
            call(turn)
        assert turn.state["tool_cap_nudged"] == (length >= 12)


def test_undelivered_nudge_is_discarded_then_next_turn_can_nudge():
    turn = _Turn(tool_cap=12)
    begin(turn)
    tool(turn, 12)
    begin(turn)
    assert "tool_cap_nudge_discarded" in turn.ns["__traces"]
    tool(turn, 12)
    assert call(turn)["extra_body"]["reasoning_effort"] == "low"


def admission_branch():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    dispatch = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "dispatch_text")
    return next(n for n in dispatch.body if isinstance(n, ast.If)
                and ast.unparse(n.test) == "busy or _pending_has(server_state)"
                and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                        and x.func.id == "_pending_push" for x in ast.walk(n)))


def test_overlapping_arrivals_and_progress_do_not_rearm_active_turn():
    turn = _Turn(tool_cap=12)
    begin(turn)
    tool(turn, 12)
    call(turn)
    turn.state["pending_turns"] = collections.deque()
    from source.server.server import _PENDING_QUEUE_MAX, _request_owner
    ns = {"server_state": turn.state, "busy": True,
          "_pending_push": _pending_push, "_PENDING_QUEUE_MAX": _PENDING_QUEUE_MAX,
          "source": "text", "trace": lambda *a, **kw: None}
    fn = ast.AsyncFunctionDef(name="admit", args=ast.arguments(posonlyargs=[],
        args=[ast.arg(arg=n) for n in ("request", "owner")],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=[admission_branch()], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(SERVER), "exec"), ns)

    async def arrivals():
        return await asyncio.gather(ns["admit"]("one", _request_owner("token1")),
                                    ns["admit"]("two", _request_owner("token2")))

    assert all(r["status"] == "queued" for r in asyncio.run(arrivals()))
    # Existing early watchdog stamps can run at admission. They must never
    # become the nudge reset site, even with turn_started=True.
    _stamp_progress(turn.state, turn_started=True)
    assert turn.state["tool_cap_nudged"] is True
    assert [r["reply_token"] for r in turn.state["pending_turns"]] == ["token1", "token2"]
    tool(turn, 13)
    assert turn.state["pending_tool_cap_nudge"] is None
    for _ in range(2):
        begin(turn)
        tool(turn, 12)
        assert call(turn)["extra_body"]["reasoning_effort"] == "low"


def test_no_mid_turn_reset_on_tool_results_or_provider_retry():
    turn = _Turn(tool_cap=12)
    begin(turn)
    tool(turn, 12)
    call(turn)
    turn.console("completed result available for provider retry")
    for model in ("openai/other-provider", "ollama_chat/qwen3:8b"):
        assert "extra_body" not in call(turn, model)
        assert turn.state["tool_cap_nudged"] is True
    tool(turn, 15)
    assert turn.state["pending_tool_cap_nudge"] is None
