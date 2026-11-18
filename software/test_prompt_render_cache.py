"""Per-turn caching of Open Interpreter's system-prompt render.

The measured problem: respond.py re-renders the whole system prompt (9 {{}}
kernel blocks, 5.55s on this machine) before EVERY LLM call, and a tool turn
makes 3 calls - so ~16s of a 47s turn was re-rendering an unchanged template.

These tests pin the two properties that make it safe:
  * it reuses the render ONLY within the same turn, and
  * it can never serve a previous turn's prompt (two independent keys).
"""

import pytest

from source.server import prompt_render_cache as prc


class _FakeInterpreter:
    def __init__(self, user_count=1):
        self.messages = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": "q%d" % i} for i in range(user_count)
        ]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("FRIDAY_PROMPT_RENDER_CACHE", "1")
    prc.uninstall()
    prc._state["hits"] = 0
    prc._state["misses"] = 0
    prc._state["seconds_saved_estimate"] = 0.0
    prc._state["last_render_seconds"] = 0.0
    prc._state["epoch"] = 0
    yield
    prc.uninstall()


def _install(monkeypatch, calls):
    from interpreter.core import respond

    def original(interpreter, message):
        calls["n"] += 1
        return "rendered#%d" % calls["n"]

    monkeypatch.setattr(respond, "render_message", original)
    assert prc.install() is True
    return respond


def test_a_second_render_in_the_same_turn_is_a_cache_hit(monkeypatch):
    calls = {"n": 0}
    respond = _install(monkeypatch, calls)
    who = _FakeInterpreter()

    first = respond.render_message(who, "TEMPLATE")
    second = respond.render_message(who, "TEMPLATE")

    assert calls["n"] == 1, "re-rendered an unchanged prompt inside one turn"
    assert first == second
    assert prc.stats()["hits"] == 1


def test_a_new_turn_forces_a_fresh_render(monkeypatch):
    calls = {"n": 0}
    respond = _install(monkeypatch, calls)
    who = _FakeInterpreter()

    first = respond.render_message(who, "TEMPLATE")
    prc.begin_turn()
    second = respond.render_message(who, "TEMPLATE")

    assert calls["n"] == 2, "a new turn reused the previous turn's prompt"
    assert first != second


def test_identical_turn_markers_do_not_share_an_interpreters_private_prompt(monkeypatch):
    calls={'n':0};respond=_install(monkeypatch,calls)
    first,second=_FakeInterpreter(),_FakeInterpreter()
    assert respond.render_message(first,'TEMPLATE')!=respond.render_message(second,'TEMPLATE')
    assert calls['n']==2


def test_a_new_user_message_invalidates_without_begin_turn(monkeypatch):
    """Self-healing key: entry points that forget begin_turn() must still be safe."""
    calls = {"n": 0}
    respond = _install(monkeypatch, calls)
    who = _FakeInterpreter(user_count=1)

    first = respond.render_message(who, "TEMPLATE")
    who.messages.append({"role": "user", "content": "next question"})
    second = respond.render_message(who, "TEMPLATE")

    assert calls["n"] == 2, "served a stale prompt across a new user message"
    assert first != second


def test_tool_messages_inside_a_turn_do_not_invalidate(monkeypatch):
    """Tool results reach the model as messages, not the system prompt, so the
    cache must survive them - that is where all the savings come from."""
    calls = {"n": 0}
    respond = _install(monkeypatch, calls)
    who = _FakeInterpreter(user_count=1)

    respond.render_message(who, "TEMPLATE")
    who.messages.append({"role": "console", "content": "FRIDAY health: ok"})
    who.messages.append({"role": "tool", "content": "done"})
    respond.render_message(who, "TEMPLATE")

    assert calls["n"] == 1, "tool traffic defeated the cache inside one turn"


def test_a_changed_template_always_re_renders(monkeypatch):
    calls = {"n": 0}
    respond = _install(monkeypatch, calls)
    who = _FakeInterpreter()

    respond.render_message(who, "TEMPLATE-A")
    respond.render_message(who, "TEMPLATE-B")

    assert calls["n"] == 2


def test_it_measures_what_a_re_render_would_have_cost(monkeypatch):
    calls = {"n": 0}
    respond = _install(monkeypatch, calls)
    who = _FakeInterpreter()

    respond.render_message(who, "T")     # miss, records the render time
    prc.begin_turn()
    respond.render_message(who, "T")     # miss
    respond.render_message(who, "T")     # hit -> credits the saved cost

    assert prc.stats()["hits"] == 1
    assert prc.stats()["seconds_saved_estimate"] >= 0.0


def test_opt_out_env_disables_installing(monkeypatch):
    from interpreter.core import respond

    def original(interpreter, message):
        return "raw"

    monkeypatch.setattr(respond, "render_message", original)
    monkeypatch.setenv("FRIDAY_PROMPT_RENDER_CACHE", "0")
    assert prc.install() is False
    assert prc.enabled() is False
    assert respond.render_message(_FakeInterpreter(), "T") == "raw"


def test_uninstall_restores_the_original(monkeypatch):
    from interpreter.core import respond

    def original(interpreter, message):
        return "original"

    monkeypatch.setattr(respond, "render_message", original)
    assert prc.install() is True
    assert getattr(respond.render_message, "_friday_render_cache", False) is True
    prc.uninstall()
    assert respond.render_message(_FakeInterpreter(), "T") == "original"


def test_install_is_idempotent(monkeypatch):
    calls = {"n": 0}
    _install(monkeypatch, calls)
    assert prc.install() is True
    assert prc.install() is True
    who = _FakeInterpreter()
    prc.install()
    prc.install()
    respond_render = __import__("interpreter.core.respond", fromlist=["x"]).render_message
    respond_render(who, "T")
    respond_render(who, "T")
    # Only one wrap: a single original call.
    assert calls["n"] == 1


def test_install_is_reflected_in_stats(monkeypatch):
    calls = {"n": 0}
    _install(monkeypatch, calls)
    stats = prc.stats()
    assert stats["installed"] is True
    assert stats["hits"] == 0 and stats["misses"] == 0
