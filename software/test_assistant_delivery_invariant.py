"""BUILD 1: the assistant-delivery invariant, proven against the REAL path.

WHY THIS FILE EXISTS
--------------------
Round Y BUILD 1 investigated three organic `content_but_no_text` anomaly records
in `~/.friday/logs/turns.jsonl` (2026-10-03/04) where the provider returned
content (`content_len: 126`, `finish_reason: "stop"`, `finished: true`) but the
turn recorded `assistant_len: 0`.

The investigation PROVED there is no current defect on the normal path: a
finished, non-empty assistant message is recorded by `_record_turn_output` AND
delivered. The only suppression is `barge_in`, which is a deliberate user
interrupt.

So these tests are not a regression test for a fix - no fix was made. They lock
in the invariant the investigation established, so a future change cannot quietly
break the recording/delivery path that was proven correct. This is the "verify
the actual shared path" requirement, not "the unit test passes".

WHAT IS REAL HERE
-----------------
* `_begin_turn_log` and `_record_turn_output` are MODULE-LEVEL in server.py, so
  they are imported directly from the production module. Not reimplemented.
* The `type == "message"` branch is extracted BY AST from the real `new_output`
  closure and executed as-is. Its text is never edited or paraphrased.

WHAT IS DELIBERATELY NOT ASSERTED
---------------------------------
The cross-turn leak of `llm_response_shape` (reset only at server.py:1989,
inside the LLM layer, gated on a user row) is NOT enshrined here. It is a real
defect, it is documented in MEMORY.md, and it is left for an authorised
instrumentation fix. Asserting current behaviour as "correct" here would make a
future fix look like a regression.
"""

import ast
import asyncio
import os
import sys
import textwrap
import types
import unittest

sys.path.insert(0, os.getcwd())
SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")

from source.server import server as srv  # noqa: E402  real production module


def _real_message_branch():
    """The REAL message-handling branch, extracted by AST from new_output."""
    with open(SERVER_PY, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    new_output = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "new_output"
    )
    for node in ast.walk(new_output):
        if isinstance(node, ast.If):
            try:
                test = ast.unparse(node.test)
            except Exception:
                continue
            if "output.get(" in test and "message" in test and "type" in test:
                with open(SERVER_PY, encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
                return textwrap.dedent(
                    "\n".join(lines[node.lineno - 1:node.end_lineno])
                )
    raise AssertionError("the message branch was not found in new_output")


BRANCH_SRC = _real_message_branch()


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _TTS:
    """Minimal TTS double: records what production code decided to speak."""

    def __init__(self, sink):
        self._sink = sink
        self._text = ""

    def text(self):
        return self._text

    def feed(self, content):
        self._sink.append(content)
        self._text += str(content or "")

    def is_playing(self):
        return False

    def stop(self):
        pass

    def play_async(self, *a, **k):
        pass


def drive_real_branch(output, barge_in=False, telegram_turn_mute=False):
    """Run the REAL branch against the REAL turn-log functions.

    Returns the observable effects: what was recorded into the turn log and what
    was handed to TTS. Uses the production source verbatim inside a
    single-iteration loop so the branch's own `continue` ends the run.
    """
    spoken = []
    chat_rows = []
    interpreter = types.SimpleNamespace(
        tts=_TTS(spoken), play_audio=False, on_tts_chunk=lambda *a, **k: None
    )
    server_state = {
        "barge_in": barge_in,
        "telegram_turn_mute": telegram_turn_mute,
        "tts_echo_turn_active": False,
        "recent_tts_texts": [],
        "tts_echo_buffer": "",
        "turn_log": None,
    }
    # Option 2 (2026-10-18): muting lives on the immutable ownership record, not
    # on the raw flag, so a muted fixture must install ownership the way the
    # real admission paths do. Setting the legacy flag alone no longer mutes.
    if telegram_turn_mute:
        srv._install_ownership(server_state, srv.TurnOwnership(
            channel="telegram", chat_id="probe", speaks_locally=False))
    else:
        srv._clear_ownership(server_state)
    # REAL production functions.
    srv._begin_turn_log(server_state, user="probe", source="text")

    namespace = {
        "server_state": server_state,
        "output": output,
        "self": interpreter,
        "asyncio": asyncio,
        "chat_append": lambda role, content, status="complete": chat_rows.append(
            (role, content, status)
        ),
        "_record_turn_output": srv._record_turn_output,
        "_looks_like_tool_json": srv._looks_like_tool_json,
        "_is_tts_text_speakable": lambda content, existing: True,
        "_remember_tts_text": lambda *a, **k: None,
        "_speech_playback": lambda *_: object(),
        "_start_owned_playback": lambda player, **kwargs: player.tts.play_async(**kwargs),
        "publish_status": lambda *a, **k: None,
        "trace": lambda *a, **k: None,
        "delimiters": ".?!;,",
        "STATUS_SPEAKING": "speaking",
        "_redact_text": lambda s: s,
        "_flog": _NullLog(),
        # The real branch reads the ownership record for its TTS gate.
        "_ownership": srv._ownership,
        "_clear_ownership": srv._clear_ownership,
        "_external_reply_pending": srv._external_reply_pending,
        # tts_start now records which stage served the utterance; supply a
        # plausible one so the extracted branch runs unchanged.
        "_tts_stage_now": lambda: "gemini",
        # ROUND ZB-REC: the branch now consults the TTS breaker and repairs the
        # playback latch before feeding. Supply the REAL helpers - the fixture
        # has no `tts_engine`, so they report healthy and this invariant still
        # exercises the normal delivery path rather than the degraded one.
        "tts_is_degraded": srv.tts_is_degraded,
        "tts_degraded_status": srv.tts_degraded_status,
        "note_tts_degraded": srv.note_tts_degraded,
        "_tts_recover_latch": srv._tts_recover_latch,
    }
    driver = ("async def _drive():\n"
              "    for _single in (1,):\n"
              + textwrap.indent(BRANCH_SRC, "        "))
    exec(compile(driver, "<real new_output message branch>", "exec"), namespace)
    asyncio.run(namespace["_drive"]())

    turn = server_state.get("turn_log") or {}
    return {
        "assistant_len": turn.get("assistant_len"),
        "assistant_chunks": turn.get("assistant_chunks"),
        "snippet": turn.get("assistant_snippet"),
        "spoken": list(spoken),
        "chat_rows": list(chat_rows),
        "server_state": server_state,
    }


ANSWER = "The answer is 84. Here it is in full."


class AssistantDeliveryInvariantTests(unittest.TestCase):
    """A finished, non-empty provider message must be recorded AND delivered."""

    def frame(self, content=ANSWER):
        return {"role": "assistant", "type": "message", "content": content}

    def test_the_real_branch_source_is_exercised(self):
        """Guard the harness itself: a silently-empty extraction would make
        every other assertion in this file vacuous."""
        self.assertIn("output.get", BRANCH_SRC)
        self.assertIn("message", BRANCH_SRC)
        self.assertIn("_record_turn_output", BRANCH_SRC,
                      "the real branch must be the one calling the recorder")
        self.assertIn("tts.feed", BRANCH_SRC,
                      "the real branch must be the one feeding TTS")

    def test_non_empty_message_is_recorded_in_the_turn_log(self):
        result = drive_real_branch(self.frame())
        self.assertEqual(result["assistant_len"], len(ANSWER))
        self.assertEqual(result["assistant_chunks"], 1)
        self.assertEqual(result["snippet"], ANSWER)

    def test_non_empty_message_is_delivered_to_tts(self):
        result = drive_real_branch(self.frame())
        self.assertEqual("".join(result["spoken"]), ANSWER)

    def test_non_empty_message_reaches_the_chat_feed(self):
        result = drive_real_branch(self.frame())
        self.assertEqual(len(result["chat_rows"]), 1)
        self.assertEqual(result["chat_rows"][0][0], "assistant")

    def test_recorded_length_equals_delivered_length(self):
        """The invariant that was violated in the historical records:
        assistant_len must agree with what the user actually received."""
        result = drive_real_branch(self.frame())
        self.assertEqual(result["assistant_len"], len("".join(result["spoken"])))

    def test_streamed_chunks_accumulate_to_the_same_total(self):
        """OI streams a reply in pieces; the total must be identical."""
        pieces = ["The answer ", "is 84. ", "Here it is in full."]
        server_state = {"turn_log": None}
        srv._begin_turn_log(server_state, user="probe", source="text")
        for piece in pieces:
            srv._record_turn_output(server_state, self.frame(piece))
        turn = server_state["turn_log"]
        self.assertEqual(turn["assistant_len"], sum(len(p) for p in pieces))
        self.assertEqual(turn["assistant_chunks"], len(pieces))
        self.assertEqual(turn["assistant_snippet"], "".join(pieces))

    def test_a_second_turn_starts_from_a_fresh_turn_log(self):
        """Turn boundaries must not inherit the previous turn's length."""
        server_state = {"turn_log": None}
        srv._begin_turn_log(server_state, user="one", source="text")
        srv._record_turn_output(server_state, self.frame(ANSWER))
        self.assertEqual(server_state["turn_log"]["assistant_len"], len(ANSWER))
        srv._begin_turn_log(server_state, user="two", source="text")
        self.assertEqual(server_state["turn_log"]["assistant_len"], 0)
        self.assertEqual(server_state["turn_log"]["assistant_snippet"], "")


class BargeInSuppressionTests(unittest.TestCase):
    """A user interrupt suppresses BOTH delivery and recording, by design.

    This is the one current path that produces `assistant_len == 0` for a
    non-empty provider message. It is correct behaviour - the user interrupted,
    so the rest of the reply must not be spoken - and it is pinned here so a
    future change has to make a deliberate decision rather than drift.
    """

    def frame(self, content=ANSWER):
        return {"role": "assistant", "type": "message", "content": content}

    def test_barge_in_records_nothing(self):
        result = drive_real_branch(self.frame(), barge_in=True)
        self.assertEqual(result["assistant_len"], 0)
        self.assertEqual(result["assistant_chunks"], 0)
        self.assertEqual(result["snippet"], "")

    def test_barge_in_speaks_nothing(self):
        result = drive_real_branch(self.frame(), barge_in=True)
        self.assertEqual(result["spoken"], [])

    def test_barge_in_adds_no_chat_row(self):
        result = drive_real_branch(self.frame(), barge_in=True)
        self.assertEqual(result["chat_rows"], [])

    def test_muted_telegram_turn_is_still_recorded(self):
        """Muting is for SPEECH only. The text must still reach the turn log,
        because the phone delivery path reads assistant_snippet - losing it here
        would silently drop the Telegram answer."""
        result = drive_real_branch(self.frame(), telegram_turn_mute=True)
        self.assertEqual(result["assistant_len"], len(ANSWER))
        self.assertEqual(result["spoken"], [],
                         "a muted turn must not be spoken locally")


class EmptyContentIsNotRecordedTests(unittest.TestCase):
    """An empty frame must not inflate the recorded length."""

    def test_empty_message_frame_records_nothing(self):
        result = drive_real_branch({"role": "assistant", "type": "message",
                                    "content": ""})
        self.assertEqual(result["assistant_len"], 0)
        self.assertEqual(result["spoken"], [])


if __name__ == "__main__":
    unittest.main()
