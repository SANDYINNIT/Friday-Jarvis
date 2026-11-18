"""Regression tests for the fruitless screen-search guard (ROUND ZB-FS).

The confirmed failure, 2026-11-03: asked to find text that was NOT on the
screen, the model re-shot and re-OCRed for 330 s+ (17 tools, ~7 minutes) and
only died because the process was killed. Every tool finished in under 2 s, the
wording of each empty result differed, and the only backstops were the 24-tool
hard cap and the 600 s wall clock - both minutes away.

What this file pins:

* the DETECTOR sees exactly the nil-result lines FRIDAY's own tooling and the
  model print, and nothing else (a false positive here would kill real work);
* the GUARD stops at the cap, sets `stop_event`, and tells Sir the TRUTH about
  an absent target rather than blaming a loop;
* a search that finally MATCHES is never interrupted - late success counts;
* `/chat/cancel` exists, is guarded before it reads anything, stops an active
  turn through the documented mechanism, is idempotent when idle, and cancels
  queued lifecycle records instead of letting them run later;
* queued/concurrent state is left consistent (nothing is corrupted by a cancel).

Conventions follow the house style: the real source is located with `ast`,
unparsed, and executed inside a synthetic `async def` + one-iteration loop (a
bare `continue` outside a loop is a SyntaxError, and `await` needs a coroutine).
"""

import ast
import asyncio
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())

import pytest

from source.server import chat_lifecycle
from source.server import server as srv

SERVER_PATH = Path(__file__).resolve().parent / "source" / "server" / "server.py"
SERVER_SRC = SERVER_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SERVER_SRC)


def _find_async(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError("async function %r not found in server.py" % name)


def _find_def(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("function %r not found in server.py" % name)


def _search_guard_source():
    """`console_text = ...` plus the fruitless-search `if` that follows it.

    Both live inside the `console` branch of `new_output`, not in its top-level
    body, so the search walks every statement list under the function.
    """
    node = _find_async("new_output")
    for parent in ast.walk(node):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for index, stmt in enumerate(body):
            if not (isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "console_text"
                    for t in stmt.targets)):
                continue
            guard = body[index + 1]
            unparsed = ast.unparse(guard)
            if "search_misses" in unparsed:
                return ast.unparse(ast.Module(body=[stmt, guard],
                                              type_ignores=[]))
            raise AssertionError(
                "the statement after `console_text = ...` is no longer the "
                "fruitless-search guard (got %r...) - the guard must stay "
                "BEFORE the flood guard, which needs byte-identical repetition"
                % unparsed[:90]
            )
    raise AssertionError("console_text assignment not found in new_output")


SEARCH_GUARD_SRC = _search_guard_source()


class _StopEvent:
    def __init__(self):
        self.calls = 0

    def set(self):
        self.calls += 1


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, message, *args):
        self.lines.append((message, args))

    def info(self, message, *args):
        self.lines.append((message, args))


def _namespace(cap=4, external=False, chat_id=None):
    """Bare namespace holding real helpers + recording stubs."""
    spoken = []
    statuses = []
    rows = []
    replies = []
    cleared = []
    recorded = []
    log = _Log()

    def _ownership(_state):
        return SimpleNamespace(is_external=bool(external),
                               chat_id=chat_id, muted=False)

    ns = {
        "search_miss_target": srv.search_miss_target,   # real detector
        "SEARCH_MISS_CAP": cap,
        "turn_guard": {"flood_stopped": False, "search_misses": 0,
                       "search_miss_target": ""},
        "interpreter": SimpleNamespace(stop_event=_StopEvent()),
        "server_state": {"pending": []},
        "output": {"type": "console", "content": "", "end": False},
        "_flog": log,
        "publish_status": lambda *a, **k: statuses.append(a),
        "STATUS_ERROR": "error",
        "chat_append": lambda *a, **k: rows.append((a, k)),
        "queue_speech_only": lambda text: spoken.append(text),
        "_ownership": _ownership,
        "_post_reply": lambda state, text: replies.append(text),
        "_clear_ownership": lambda state: cleared.append(True),
        "TEXT_CHANNEL_CHAT_ID": srv.TEXT_CHANNEL_CHAT_ID,
        "_record_turn_output": lambda state, output: recorded.append(output),
        # Route-only names (the /chat/cancel body runs in this namespace too).
        "_guard": lambda request: None,
        "STATUS_IDLE": "idle",
        "JSONResponse": lambda payload: payload,
        "request": object(),
    }

    async def terminal_notice(owner, text):
        if owner.chat_id == srv.TEXT_CHANNEL_CHAT_ID:
            replies.append(text)
        elif owner.is_external:
            replies.append(("telegram", owner.chat_id, text))
        else:
            spoken.append(text)

    ns["_terminal_notice"] = terminal_notice
    ns["spoken"] = spoken
    ns["statuses"] = statuses
    ns["rows"] = rows
    ns["replies"] = replies
    ns["cleared"] = cleared
    ns["recorded"] = recorded
    ns["log"] = log
    return ns


async def _drive(ns, lines):
    """Run the REAL guard over `lines` of console output, one per iteration.

    The one-iteration loop is what makes the guard's bare `continue` legal, and
    the async wrapper is what makes its `await telegram_adapter.send_text(...)`
    legal - both are why this block is exec'd rather than reimplemented.
    """
    for line in lines:
        ns["output"] = {"type": "console", "content": line, "end": False}
        exec(COMPILED_GUARD, ns)
        # exec only DEFINES the coroutine function; without this call the whole
        # guard would be skipped and every assertion would fail confusingly.
        result = ns["_guard_once"]()
        if asyncio.iscoroutine(result):
            await result
    return ns


def _run(lines, cap=4, external=False, chat_id=None):
    ns = _namespace(cap=cap, external=external, chat_id=chat_id)
    asyncio.run(_drive(ns, lines))
    return ns


# The block, wrapped so `continue` and `await` both have somewhere to live.
COMPILED_GUARD = compile(
    "async def _guard_once():\n"
    "    for _once in range(1):\n"
    + textwrap.indent(SEARCH_GUARD_SRC, "        ")
    + "\n",
    "<fruitless-search-guard>",
    "exec",
)


# --------------------------------------------------------------------------
# 1. The detector: real observed lines in, everything else out.
# --------------------------------------------------------------------------
class TestDetector:
    @pytest.mark.parametrize("line,target", [
        ('[friday find_text] "test marker" not visible on screen.', "test marker"),
        ('[find_text] "Marker" not visible on screen.', "Marker"),
        ("find_text 'test marker' -> []", "test marker"),
        ("hits 'test marker': []", "test marker"),
        ("fallback hits 'marker': []", "marker"),
        ('[friday find_text] "TEST MARKER" not visible on screen.', "TEST MARKER"),
    ])
    def test_every_nil_result_line_friday_actually_prints_counts(self, line, target):
        # All six were captured from the real 2026-11-03 runaway log.
        assert srv.search_miss_target(line) == target

    @pytest.mark.parametrize("line", [
        "shot size: (1920, 1080)",
        "err: []",
        "hits the target: [{'coordinates': (1, 2)}]",
        "find_text 'Save' -> [{'coordinates': (10, 10)}]",
        "The save button is not visible",
        "",
        "   ",
        None,
        "1 2 3 4",
    ])
    def test_real_work_is_never_counted_as_a_miss(self, line):
        # A false positive here kills a healthy turn - the flood guard already
        # did that once (see the "false statement" incident in server.py).
        assert srv.search_miss_target(line) is None

    def test_a_miss_without_a_readable_target_is_still_a_miss(self):
        # None = "not a miss"; "" = "a miss we could not attribute". They must
        # stay distinguishable or the guard cannot count at all.
        assert srv.search_miss_target("[find_text] not visible on screen.") == ""

    def test_the_detector_never_reaches_for_start_server_closure_names(self):
        # test_audit_regressions fails module-level functions that do.
        node = _find_def("search_miss_target")
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        forbidden = {"_flog", "trace", "server_state", "interpreter",
                     "turn_guard", "chat_append", "publish_status",
                     "queue_speech_only", "STATUS_ERROR"}
        assert not (names & forbidden), names & forbidden


# --------------------------------------------------------------------------
# 2. An absent target stops the turn, promptly and truthfully.
# --------------------------------------------------------------------------
class TestAbsentTargetRepeatedSearch:
    def test_the_cap_is_reached_and_the_turn_is_stopped(self):
        line = '[friday find_text] "test marker" not visible on screen.'
        ns = _run([line] * 4, cap=4)
        assert ns["interpreter"].stop_event.calls == 1, "turn was not stopped"

    def test_it_stops_on_the_cap_not_one_line_later(self):
        line = "[find_text] \"Save\" not visible on screen."
        assert _run([line] * 3, cap=4)["interpreter"].stop_event.calls == 0
        assert _run([line] * 4, cap=4)["interpreter"].stop_event.calls == 1

    def test_the_user_hears_a_truthful_bounded_failure(self):
        ns = _run(['[friday find_text] "test marker" not visible on screen.'] * 4)
        assert len(ns["spoken"]) == 1, "the turn went silent instead of answering"
        spoken = ns["spoken"][0]
        # Truthfulness: name the actual target and the actual reason.
        assert "test marker" in spoken
        assert "not there" in spoken
        # Never blame a loop for a lookup that simply failed.
        assert "loop" not in spoken.lower()
        assert "screenshot" in spoken.lower()

    def test_the_hud_and_the_log_are_updated_too(self):
        ns = _run(["find_text 'gone' -> []"] * 4)
        assert ns["statuses"], "HUD status never published"
        assert any("not on screen" in str(s) for s in ns["statuses"])
        assert any("fruitless search guard" in str(line[0]) for line in ns["log"].lines)
        assert any("SEARCH STOPPED" in str(r) for r in ns["rows"])

    def test_the_turn_output_is_still_recorded_before_it_stops(self):
        # Otherwise the UI loses the line that explains why it stopped.
        ns = _run(["hits 'gone': []"] * 4)
        assert len(ns["recorded"]) == 1

    def test_only_the_line_that_trips_is_recorded_once(self):
        ns = _run(["hits 'gone': []"] * 3 + ["[friday find_text] \"x\" not visible on screen."])
        assert ns["interpreter"].stop_event.calls == 1
        assert len(ns["recorded"]) == 1

    def test_a_text_channel_caller_gets_the_answer_instead_of_hanging(self):
        # Without this the /chat caller waits out its whole 240 s timeout.
        ns = _run(["find_text 'gone' -> []"] * 4,
                  external=True, chat_id=srv.TEXT_CHANNEL_CHAT_ID)
        assert ns["replies"], "the waiting /chat caller received nothing"
        assert "not there" in ns["replies"][0]
        assert not ns["cleared"], "guard released ownership before the worker completed"

    def test_a_telegram_stop_reaches_the_phone(self):
        ns = _run(["find_text 'gone' -> []"] * 4, external=True, chat_id="-100555")
        telegram = [r for r in ns["replies"] if r[0] == "telegram"]
        assert telegram, "the phone never got the honest stop"
        assert "not there" in telegram[0][2]

    def test_the_local_desktop_turn_still_speaks(self):
        # A local turn gets speech; an external one gets its channel. Both.
        ns = _run(["[find_text] \"x\" not visible on screen."]* 4)
        assert ns["spoken"], "a desk turn was left silent after being stopped"
        assert ns["replies"] == [], "a local turn must not also post a reply"


# --------------------------------------------------------------------------
# 3. Normal searches - including a LATE match - are untouched.
# --------------------------------------------------------------------------
class TestSuccessfulAndLateMatch:
    def test_a_single_miss_is_not_a_runaway(self):
        ns = _run(["[find_text] \"Save\" not visible on screen."])
        assert ns["interpreter"].stop_event.calls == 0
        assert ns["spoken"] == [], "one failed lookup must not interrupt the turn"

    def test_a_late_match_after_three_misses_still_succeeds(self):
        # Miss, miss, miss, then the text appears. The counter only ever sees
        # misses, so a hit can never push it over the cap.
        lines = ["[friday find_text] \"Installing\" not visible on screen."] * 3
        lines.append("hits 'Installing': [{'coordinates': (40, 40)}]")
        ns = _run(lines, cap=4)
        assert ns["interpreter"].stop_event.calls == 0, "late match was interrupted"

    def test_the_budget_is_per_TURN_not_per_target(self):
        """Deliberate, and the reason the observed runaway is caught.

        The model evaded any per-target reading by rewording every attempt -
        "test marker" / "marker" / "TEST MARKER" were three DIFFERENT strings
        in the real log, so a per-query counter would have sat at 3 forever
        while the turn ran past 330 s. Counting misses for the TURN means a
        reworded retry still counts, which is what makes this prompt.

        Trade-off, accepted on purpose: four DISTINCT one-off lookups that all
        miss also reach the cap. That answer is still honest ("I searched for X
        and it is not there - tell me what else to try"), it is env-tunable
        (FRIDAY_SEARCH_MISS_CAP), and it is strictly better than the previous
        floor of 600 s.
        """
        lines = [
            '[friday find_text] "alpha" not visible on screen.',
            '[find_text] "beta" not visible on screen.',
            "find_text 'gamma' -> []",
            "hits 'delta': []",
        ]
        ns = _run(lines, cap=4)
        assert ns["interpreter"].stop_event.calls == 1, (
            "a per-target budget would leave this turn running"
        )
        assert "delta" in ns["spoken"][0], "the answer should name the last miss"

    def test_rewording_the_same_target_still_counts(self):
        # The exact evasion from the 2026-11-03 log: same absent thing, three
        # different spellings.
        lines = [
            '[friday find_text] "test marker" not visible on screen.',
            '[find_text] "TEST MARKER" not visible on screen.',
            "[friday find_text] \"marker\" not visible on screen.",
            "hits 'Marker': []",
        ]
        ns = _run(lines, cap=4)
        assert ns["interpreter"].stop_event.calls == 1

    def test_three_distinct_misses_are_still_below_the_cap(self):
        # The preserved half: a handful of one-off checks is not this failure.
        lines = [
            '[friday find_text] "alpha" not visible on screen.',
            '[find_text] "beta" not visible on screen.',
            "find_text 'gamma' -> []",
        ]
        ns = _run(lines, cap=4)
        assert ns["interpreter"].stop_event.calls == 0
        assert ns["spoken"] == []

    def test_a_healthy_turn_with_no_searches_is_untouched(self):
        ns = _run(["shot size: (1920, 1080)", "compiled OK", "done"])
        assert ns["interpreter"].stop_event.calls == 0
        assert ns["spoken"] == []

    def test_the_cap_is_configurable(self):
        line = "[find_text] \"x\" not visible on screen."
        assert _run([line] * 2, cap=2)["interpreter"].stop_event.calls == 1
        assert _run([line] * 2, cap=9)["interpreter"].stop_event.calls == 0


# --------------------------------------------------------------------------
# 4. Interaction with the guards that already exist.
# --------------------------------------------------------------------------
class TestGuardInteractions:
    def test_it_does_not_double_stop_when_the_flood_guard_already_fired(self):
        ns = _namespace()
        ns["turn_guard"]["flood_stopped"] = True
        asyncio.run(_drive(ns, ["[find_text] \"x\" not visible on screen."] * 9))
        assert ns["interpreter"].stop_event.calls == 0
        assert ns["spoken"] == []

    def test_the_guard_runs_before_the_byte_identical_flood_guard(self):
        # The flood guard needs the SAME bytes in a row; this failure rewords
        # every line, so placement is the whole point of the fix.
        node = _find_async("new_output")
        for parent in ast.walk(node):
            body = getattr(parent, "body", None)
            if not isinstance(body, list):
                continue
            for index, stmt in enumerate(body):
                if (isinstance(stmt, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "console_text"
                        for t in stmt.targets)):
                    assert "search_misses" in ast.unparse(body[index + 1]), (
                        "the fruitless-search guard must be the first thing "
                        "after `console_text` is computed"
                    )
                    return
        raise AssertionError("console_text assignment not found")

    def test_the_budget_resets_with_the_other_per_turn_guards(self):
        # Otherwise one bad turn would silence the next one.
        assert 'turn_guard["search_misses"] = 0' in SERVER_SRC
        assert 'turn_guard["search_miss_target"] = ""' in SERVER_SRC
        reset_at = SERVER_SRC.index('turn_guard["search_misses"] = 0')
        flood_reset = SERVER_SRC.index('turn_guard["flood_stopped"] = False')
        assert reset_at > flood_reset, "reset must sit with the other budgets"

    def test_the_cap_default_and_its_env_override_follow_the_house_convention(self):
        line = ('SEARCH_MISS_CAP = int(float(os.getenv('
                '"FRIDAY_SEARCH_MISS_CAP", "4")))')
        assert line in SERVER_SRC, "cap must be env-overridable, spelled like its neighbours"
        assert 'os.environ.get("FRIDAY_SEARCH_MISS_CAP"' not in SERVER_SRC

    def test_the_existing_guards_are_still_present(self):
        # This round ADDS a guard; it must not quietly remove one.
        assert 'over_events = turn_guard["same_run"] > TOOL_FLOOD_EVENT_CAP' in SERVER_SRC
        assert 'if tool_counter["n"] > HARD_TOOL_CAP:' in SERVER_SRC
        assert 'TOOL_TIMEOUT_SECONDS' in SERVER_SRC
        assert SERVER_SRC.count("interpreter.stop_event.set()") >= 3

    def test_the_stop_message_never_claims_a_loop_for_a_missing_target(self):
        # The flood guard's own wording is separately pinned by
        # test_model_self_awareness; this asserts OUR message distinguishes them.
        node = _find_async("new_output")
        source = ast.unparse(node)
        assert "instead of taking more screenshots" in source
        assert "I searched the screen for" in source


# --------------------------------------------------------------------------
# 5. POST /chat/cancel - the missing practical cancel.
# --------------------------------------------------------------------------
def _cancel_route_source():
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_cancel":
            return node
    raise AssertionError("chat_cancel route not found - /chat has no cancel")


CANCEL_ROUTE = _cancel_route_source()


def test_cancel_route_remains_guarded():
    body = ast.unparse(CANCEL_ROUTE)
    assert body.index("_guard(request)") < body.index("_cancel_requests(")


class TestConcurrentAndQueuedSafety:
    def test_cancel_all_leaves_terminal_records_alone(self):
        registry = chat_lifecycle.LifecycleRegistry()
        queued = registry.register(chat_lifecycle.ChatRequest("a", "one", 30))
        active = registry.register(chat_lifecycle.ChatRequest("b", "two", 30))
        active.activate()
        done = registry.register(chat_lifecycle.ChatRequest("c", "three", 30))
        done.activate()
        done.complete()

        assert registry.cancel_all() == 2
        assert queued.state == chat_lifecycle.CANCELLED
        assert active.state == chat_lifecycle.CANCELLED
        assert done.state == chat_lifecycle.COMPLETED, "a finished record was rewritten"

    def test_cancel_all_is_idempotent(self):
        registry = chat_lifecycle.LifecycleRegistry()
        registry.register(chat_lifecycle.ChatRequest("a", "one", 30))
        assert registry.cancel_all() == 1
        assert registry.cancel_all() == 0

    def test_a_cancelled_request_still_refuses_late_work(self):
        request = chat_lifecycle.ChatRequest("tok", "find it", 30)
        request.activate()
        request.cancel()
        assert request.may_run is False
        # Late chunks are refused rather than accepted into a dead request.
        assert request.accept_result([{"type": "message", "content": "late"}]) is False

    def test_two_requests_are_not_cross_cancelled_by_token(self):
        # LifecycleRegistry keys by opaque token only; one caller cannot
        # address another's request.
        registry = chat_lifecycle.LifecycleRegistry()
        registry.register(chat_lifecycle.ChatRequest("tok-a", "first", 30))
        other = registry.register(chat_lifecycle.ChatRequest("tok-b", "second", 30))
        registry.cancel_all()
        assert registry.get("tok-a").text == "first"
        assert other.text == "second"


# --------------------------------------------------------------------------
# 7. Structure of the guard itself, so it cannot silently rot.
# --------------------------------------------------------------------------
class TestGuardStructure:
    def test_the_guard_is_inside_new_output_where_console_text_exists(self):
        assert "search_misses" in SEARCH_GUARD_SRC
        assert "console_text" in SEARCH_GUARD_SRC

    def test_it_uses_stop_event_not_a_nonexistent_interpreter_stop(self):
        assert "interpreter.stop_event.set()" in SEARCH_GUARD_SRC
        assert "interpreter.stop(" not in SEARCH_GUARD_SRC

    def test_it_delivers_to_every_channel_the_other_guards_use(self):
        # Local voice + HUD + text + Telegram, mirroring the flood guard, so a
        # stop never leaves one channel silent.
        for expected in ("await _terminal_notice(", "_ownership(server_state)",
                         "publish_status(", "chat_append("):
            assert expected in SEARCH_GUARD_SRC, expected

    def test_it_never_finalises_the_turn_itself(self):
        # OI emits `complete` itself when it unwinds; sending a synthetic
        # completion here would finalise the turn twice - the same rule
        # queue_speech_only is pinned to by test_failover_fixes.
        assert '{"type": "status"' not in SEARCH_GUARD_SRC
        assert '"content": "complete"' not in SEARCH_GUARD_SRC
        # `status="complete"` is only the UI row state, which is fine.
        assert 'chat_append(' in SEARCH_GUARD_SRC

    def test_it_records_output_before_continuing(self):
        assert "_record_turn_output(server_state, output)" in SEARCH_GUARD_SRC
        assert SEARCH_GUARD_SRC.rstrip().endswith("continue")
