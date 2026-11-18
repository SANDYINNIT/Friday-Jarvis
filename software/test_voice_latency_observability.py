"""Observability for the prompt render, and the conversational TTS brief.

Two things this round is responsible for:

1. **MEASUREMENT** - the system-prompt render runs inside Open Interpreter's
   respond loop, between `brain select` and `llm_request_start`, with no trace
   stage of its own. Measured from 177 real turns in boot_console.log it costs
   a median **7.0s** (p90 9.0s) of the ~10s before FRIDAY speaks, and it was
   invisible except as a silent gap in the log. It is now recorded per render
   and exported on `GET /voice/trace` as `prompt_render`.

2. **NATURALNESS (unverified perceptually)** - Gemini's documented expressive
   control for `gemini-3.1-flash-tts-preview` is style-by-prompt, not SSML
   `<prosody>` and not numeric rate/pitch. The brief therefore lives in the
   instruction text and must do two opposing things at once: sound like a real
   person (including a genuine hesitation), while never touching the words -
   figures, chess moves, code, instructions and safety messages stay
   word-for-word. Nothing here claims the voice *sounds* better; no human has
   compared before/after audio.
"""

import ast
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

import pytest

from source.server import prompt_render_cache as prc
from source.server import server as srv

ROOT = Path(__file__).resolve().parent


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
    prc._state["recent"] = []
    yield
    prc.uninstall()


def _install(monkeypatch, calls, delay=0.0):
    from interpreter.core import respond

    def original(interpreter, message):
        calls["n"] += 1
        if delay:
            import time

            time.sleep(delay)
        return "rendered#%d" % calls["n"]

    monkeypatch.setattr(respond, "render_message", original)
    assert prc.install() is True
    return respond


# --------------------------------------------------------------------------
# 1. The render is now measurable.
# --------------------------------------------------------------------------
class TestTheRenderIsMeasured:
    def test_a_real_render_records_its_turn_duration_and_timestamp(self,
                                                                  monkeypatch):
        calls = {"n": 0}
        # 0.06s, not 0.01s: Windows timer granularity can return from a 10ms
        # sleep a hair early, which rounded to 0.000 and failed intermittently.
        respond = _install(monkeypatch, calls, delay=0.06)
        prc.begin_turn()          # what dispatch does at every turn boundary
        respond.render_message(_FakeInterpreter(), "TEMPLATE")

        recent = prc.stats()["recent"]
        assert len(recent) == 1, "a real render left no evidence"
        entry = recent[0]
        assert entry["epoch"] == 1, "not tied to the turn it cost"
        assert entry["seconds"] >= 0.03, entry
        assert entry["at"] > 0, "no wall clock, so it cannot line up with the log"

    def test_a_render_before_any_turn_still_records_epoch_zero(self, monkeypatch):
        """The first render of a process predates dispatch's begin_turn()."""
        calls = {"n": 0}
        respond = _install(monkeypatch, calls)
        respond.render_message(_FakeInterpreter(), "TEMPLATE")
        assert prc.stats()["recent"][0]["epoch"] == 0

    def test_a_cache_hit_adds_no_history(self, monkeypatch):
        # History is of COST, not of calls - a hit cost nothing.
        calls = {"n": 0}
        respond = _install(monkeypatch, calls)
        who = _FakeInterpreter()
        respond.render_message(who, "TEMPLATE")
        respond.render_message(who, "TEMPLATE")
        assert calls["n"] == 1
        assert len(prc.stats()["recent"]) == 1

    def test_history_is_bounded_so_the_trace_cannot_grow(self, monkeypatch):
        calls = {"n": 0}
        respond = _install(monkeypatch, calls)
        for turn in range(20):
            prc.begin_turn()
            respond.render_message(_FakeInterpreter(), "TEMPLATE")
        recent = prc.stats()["recent"]
        assert len(recent) == 12, "recent must stay bounded"
        assert recent[-1]["epoch"] == 20, "the newest render must survive"

    def test_begin_turn_keeps_the_history(self, monkeypatch):
        """It is EVIDENCE of cost, not cache state - a new turn must not wipe it."""
        calls = {"n": 0}
        respond = _install(monkeypatch, calls)
        respond.render_message(_FakeInterpreter(), "TEMPLATE")
        prc.begin_turn()
        assert len(prc.stats()["recent"]) == 1

    def test_stats_are_json_serialisable_for_the_route(self):
        import json

        json.dumps(prc.stats())


class TestTheTraceEndpointReportsIt:
    def test_the_route_wires_the_section_in(self):
        source = (ROOT / "source" / "server" / "server.py").read_text(
            encoding="utf-8")
        assert '"prompt_render": _prompt_render_stats()' in source, (
            "/voice/trace stopped exporting the render cost"
        )

    def test_the_helper_reports_available_and_a_note(self):
        stats = srv._prompt_render_stats()
        assert stats["available"] is True
        assert "recent" in stats
        assert "seconds" in stats["note"]
        assert isinstance(stats.get("epoch"), int)

    def test_the_helper_never_raises(self, monkeypatch):
        # A trace endpoint must not 500 because a counter moved.
        import builtins

        real_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            # `from . import prompt_render_cache` reports the PACKAGE as `name`
            # and the member via `fromlist`, so both must be inspected.
            if (name and "prompt_render_cache" in str(name)) or any(
                    "prompt_render_cache" in str(item) for item in (fromlist or ())):
                raise ImportError("simulated")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        stats = srv._prompt_render_stats()
        assert stats["available"] is False
        assert stats["error"]

    def test_the_helper_is_module_level_and_free_of_closure_names(self):
        # test_audit_regressions applies the same rule to every module-level
        # function; asserting it here keeps the new helper honest too.
        tree = ast.parse((ROOT / "source" / "server" / "server.py").read_text(
            encoding="utf-8"))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_prompt_render_stats")
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        forbidden = {"_flog", "trace", "server_state", "interpreter",
                     "turn_guard", "chat_append", "publish_status",
                     "queue_speech_only", "STATUS_ERROR"}
        assert not (names & forbidden), names & forbidden


# --------------------------------------------------------------------------
# 2. The conversational brief: delivery changes, words never do.
# --------------------------------------------------------------------------
def _synthesize_and_capture(monkeypatch, text):
    """Run the REAL `_synthesize` with the network mocked; return its payload."""
    from source.server import gemini_tts

    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"inlineData": {
                "data": base64.b64encode(b"FAKE_PCM").decode("ascii")}}]}}]}

    def _post(url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return _Response()

    monkeypatch.setattr(gemini_tts.requests, "post", _post)
    ok, pcm, error, _quota, _hint = gemini_tts._synthesize("k", text, "m")
    assert ok is True, error
    return captured


def _instruction(payload):
    return payload["contents"][0]["parts"][0]["text"]


class TestTheConversationalBrief:
    def test_the_spoken_text_is_passed_through_word_for_word(self, monkeypatch):
        """THE non-negotiable: instructions must never rewrite the reply."""
        sentence = ("Player two to f5 in 3 moves. Total 42 items, "
                    "then close the window. Wear eye protection.")
        payload = _synthesize_and_capture(monkeypatch, sentence)
        assert _instruction(payload).endswith(sentence), (
            "the reply was altered before synthesis"
        )
        # The instruction is prepended, never interleaved with the words.
        assert _instruction(payload).count(sentence) == 1

    def test_the_brief_protects_the_precise_content(self, monkeypatch):
        payload = _synthesize_and_capture(monkeypatch, "ok")
        brief = _instruction(payload)
        for protected in ("Numbers", "chess moves", "code",
                          "instructions", "safety messages"):
            assert protected in brief, "no guard for %r" % protected
        assert "word-for-word" in brief

    def test_hesitation_is_delivery_not_an_inserted_word(self, monkeypatch):
        payload = _synthesize_and_capture(monkeypatch, "ok")
        brief = _instruction(payload)
        # It MUST ask for genuine hesitation...
        assert "genuine hesitation" in brief
        # ...and MUST forbid producing it by adding words.
        assert "never a word you insert" in brief
        assert '"um"' in brief and '"uh"' in brief
        assert "do not add" in brief.lower()
        assert "do not omit any" in brief.lower()

    def test_it_does_not_ask_for_fillers_or_extra_silence(self, monkeypatch):
        payload = _synthesize_and_capture(monkeypatch, "ok")
        brief = _instruction(payload).lower()
        for forbidden in ("add a filler", "insert a pause", "wait before",
                          "add silence", "pause for "):
            assert forbidden not in brief, forbidden

    def test_the_persona_and_the_old_guarantees_are_still_there(self,
                                                                monkeypatch):
        payload = _synthesize_and_capture(monkeypatch, "ok")
        brief = _instruction(payload)
        assert "F.R.I.D.A.Y." in brief
        assert "no artificial cheerfulness" in brief
        assert "Synthesize speech only" in brief

    def test_expressiveness_is_style_by_prompt_not_ssml(self, monkeypatch):
        """Confirmed against the Gemini docs: 3.1-flash-tts-preview exposes
        steerable prompts and audio tags, NOT `<prosody>` or rate/pitch."""
        payload = _synthesize_and_capture(monkeypatch, "ok")
        blob = repr(payload)
        assert "<prosody" not in blob
        assert "speechMarks" not in blob
        assert "SSML" not in blob
        # The style lives in the instruction, which is where this model reads it.
        assert "Read this reply aloud naturally" in _instruction(payload)
