"""Round ZB-SC: the screen companion ("FRIDAY, help me in this game").

Sir wants what commercial assistants do: look at the screen while he plays and
call out what matters - an enemy, an opening, a concrete tip. Not a description
every second, and not a narrating chatbot: ONE useful line when there is
something to say, silence when there is not.

These tests pin the decision path with injected capture/describe/speak and an
injected clock, so no screen, no provider call and no sleeping is involved.
"""

import ast
import os
import time
import unittest

from source.server.screen_companion import (  # noqa: E402
    WATCH_PROMPT,
    ScreenCompanion,
    _is_nothing,
    _normalise,
)

ENEMY_LINE = "Enemy top-left behind the crate - break right before he fires."
OTHER_LINE = "Door open on the right, take it."
NOTHING = "NOTHING"


class _Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _companion(descriptions, clock=None, capture=b"jpeg", interval=8.0,
               min_gap=15.0):
    """A companion whose describe() walks a script of canned observations."""
    clock = clock or _Clock()
    spoken = []
    seen = {"n": 0}

    def describe(_jpeg, _question):
        index = min(seen["n"], len(descriptions) - 1)
        seen["n"] += 1
        return descriptions[index]

    companion = ScreenCompanion(
        capture=lambda: capture,
        describe=describe,
        speak=lambda text: spoken.append(text),
        clock=clock,
    )
    companion._interval = interval
    companion._min_gap = min_gap
    return companion, spoken, seen


class TestTheQuestion:
    def test_it_asks_for_one_short_positioned_line(self):
        assert "ONE short" in WATCH_PROMPT
        assert "25 words" in WATCH_PROMPT
        # Spatial specificity is the point of the whole feature.
        assert "position" in WATCH_PROMPT.lower() or "left" in WATCH_PROMPT

    def test_nothing_is_the_official_silence(self):
        assert _is_nothing("NOTHING")
        assert _is_nothing("Nothing.")
        assert _is_nothing("")
        assert _is_nothing("  ")
        assert not _is_nothing(ENEMY_LINE)


class TestDecisionPath:
    def test_a_real_observation_is_returned_to_speak(self):
        companion, _spoken, _seen = _companion([ENEMY_LINE])
        line, ok = companion._cycle()
        assert ok is True
        assert line == ENEMY_LINE

    def test_silence_is_not_returned(self):
        companion, _spoken, _seen = _companion([NOTHING])
        line, ok = companion._cycle()
        assert ok is True, "a quiet screen is not a failure"
        assert line is None

    def test_the_brief_is_passed_to_the_vision_model(self):
        seen = []

        def describe(_jpeg, question):
            seen.append(question)
            return NOTHING

        companion = ScreenCompanion(
            capture=lambda: b"j", describe=describe, speak=lambda t: None)
        companion._brief = "help me play this game"
        companion._interval = 8.0
        companion._min_gap = 0.0
        companion._cycle()
        assert "help me play this game" in seen[0]
        assert "ONE short" in seen[0]

    def test_a_capture_failure_is_not_a_silent_success(self):
        companion, _sp, _sv = _companion([ENEMY_LINE], capture=None)
        line, ok = companion._cycle()
        assert ok is False
        assert line is None
        assert "capture" in companion.status()["last_error"]


class TestItNeverNags:
    """`_cycle` decides; `_loop` speaks exactly what it returns. Collecting the
    returned lines therefore tests the real decision path without threads."""

    @staticmethod
    def _drive(companion, cycles):
        return [line for line, ok in (companion._cycle() for _ in range(cycles))
                if ok and line]

    def test_the_same_line_is_not_repeated(self):
        companion, _sp, _seen = _companion([ENEMY_LINE] * 6, min_gap=0.0)
        spoken = self._drive(companion, 6)
        assert spoken == [ENEMY_LINE], (
            "a static screen would make the companion repeat itself forever")

    def test_a_changed_observation_is_spoken_again(self):
        companion, _sp, _seen = _companion([ENEMY_LINE, OTHER_LINE] * 4,
                                           min_gap=0.0)
        spoken = self._drive(companion, 4)
        # Dedup is consecutive-only: an alternating view IS new information each
        # time, and suppressing it would hide a real change.
        assert spoken == [ENEMY_LINE, OTHER_LINE, ENEMY_LINE, OTHER_LINE]

    def test_the_min_gap_throttles_speaking(self):
        companion, _sp, _seen = _companion([ENEMY_LINE] * 6,
                                           interval=1.0, min_gap=15.0)
        assert companion._cycle()[0] == ENEMY_LINE          # first speaks
        companion._next_speak_at = companion._clock() + 15.0
        companion._last_spoken_prefix = "something else entirely"
        assert companion._cycle()[0] is None                # inside the gap
        companion._next_speak_at = companion._clock()
        companion._last_spoken_prefix = "something else entirely"
        assert companion._cycle()[0] == ENEMY_LINE          # gap elapsed

    def test_it_counts_what_it_suppressed(self):
        companion, _sp, _seen = _companion([ENEMY_LINE] * 4, min_gap=0.0)
        self._drive(companion, 4)
        status = companion.status()
        assert status["spoken"] == 1
        assert status["suppressed_similar"] == 3


class TestBounds:
    def test_it_refuses_to_start_unwired(self):
        companion = ScreenCompanion()          # nothing injected
        assert companion.start("help") is False
        assert "not wired" in companion.status()["last_error"]

    def test_start_stop_is_idempotent(self):
        companion, _sp, _sv = _companion([NOTHING])
        companion._speak = lambda t: None
        assert companion.start("game", interval=8.0) is True
        assert companion.start("again") is False, "second start must not double-run"
        assert companion.stop("done") is True
        assert companion.stop("done") is False, "second stop must report it was idle"

    def test_stop_makes_the_running_loop_exit(self):
        companion, _sp, _sv = _companion([NOTHING])
        companion._speak = lambda t: None
        companion.start("game")
        assert companion._stop.is_set() is False
        companion.stop("done")
        assert companion._stop.is_set() is True

    def test_status_reports_honestly(self):
        companion, _sp, _sv = _companion([ENEMY_LINE])
        companion._speak = lambda t: None
        assert companion.status()["running"] is False
        companion.start("game")
        status = companion.status()
        assert status["running"] is True
        assert status["brief"] == "game"
        assert status["remaining_seconds"] > 0
        companion.stop()
        assert companion.status()["running"] is False

    def test_the_observation_is_visible_to_the_brain(self):
        companion, _sp, _sv = _companion([ENEMY_LINE])
        companion._cycle()
        assert ENEMY_LINE in companion.status()["last_observation"]

    def test_interval_and_duration_are_clamped(self):
        companion = ScreenCompanion(capture=lambda: b"j", describe=lambda j, q: NOTHING,
                                    speak=lambda t: None)
        assert companion._clamp("nonsense", 2, 60, 8) == 8
        assert companion._clamp("1", 2, 60, 8) == 2, "below the floor clamps UP to the floor"
        assert companion._clamp("9999", 2, 60, 8) == 60, "above the ceiling must clamp down"
        assert companion._clamp("-3", 2, 60, 8) == 8, "a nonsense/negative value falls back to the default"
        assert companion._clamp(0, 2, 60, 8) == 8

    def test_configured_values_are_clamped_too(self):
        environ = {"FRIDAY_WATCH_INTERVAL": "9999", "FRIDAY_WATCH_DURATION": "1"}
        companion = ScreenCompanion(capture=lambda: b"j", describe=lambda j, q: NOTHING,
                                    speak=lambda t: None, environ=environ)
        assert companion._configured_interval() == 60.0
        assert companion._configured_duration() == 30.0


class TestTheLoopActuallySpeaks:
    """The first version returned the line from `_cycle` and then threw it away
    in `_loop`, so the companion watched forever and said nothing. A decision
    path test cannot catch that - only running the real thread can."""

    def test_the_loop_speaks_what_the_cycle_found(self):
        spoken = []
        companion = ScreenCompanion(
            capture=lambda: b"j",
            describe=lambda _j, _q: ENEMY_LINE,
            speak=spoken.append)
        assert companion.start("game", duration=30.0, interval=0.05) is True
        try:
            # Shrink the window so the test is fast; the thread is real.
            companion._deadline = companion._clock() + 0.6
            companion._next_speak_at = companion._clock()
            limit = time.time() + 5.0
            while time.time() < limit and not spoken:
                time.sleep(0.05)
            assert spoken, (
                "the watch loop never spoke - it discards what the cycle found")
            assert spoken[0] == ENEMY_LINE
        finally:
            companion.stop("test over")

    def test_a_stopped_companion_stops_speaking(self):
        spoken = []
        companion = ScreenCompanion(
            capture=lambda: b"j",
            describe=lambda _j, _q: ENEMY_LINE,
            speak=spoken.append)
        companion.start("game", duration=30.0, interval=0.05)
        companion._deadline = companion._clock() + 0.6
        companion._next_speak_at = companion._clock()
        limit = time.time() + 5.0
        while time.time() < limit and not spoken:
            time.sleep(0.05)
        companion.stop("test over")
        seen_at_stop = len(spoken)
        time.sleep(0.4)
        assert len(spoken) == seen_at_stop, (
            "it kept speaking after stop() - the watcher outlived its owner")


class TestSpeaking:
    def test_speak_now_queues_the_line(self):
        spoken = []
        companion = ScreenCompanion(capture=lambda: b"j", describe=lambda j, q: NOTHING,
                                    speak=spoken.append)
        assert companion.speak_now("Door open on the right.") is True
        assert spoken == ["Door open on the right."]

    def test_speak_now_refuses_empty(self):
        spoken = []
        companion = ScreenCompanion(capture=lambda: b"j", describe=lambda j, q: NOTHING,
                                    speak=spoken.append)
        assert companion.speak_now("   ") is False
        assert spoken == []

    def test_a_broken_speaker_does_not_raise(self):
        def boom(_text):
            raise RuntimeError("audio is down")
        companion = ScreenCompanion(capture=lambda: b"j", describe=lambda j, q: NOTHING,
                                    speak=boom)
        assert companion.speak_now("hello") is False
        assert "speak failed" in companion.status()["last_error"]


SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
with open(SERVER_PY, encoding="utf-8") as _fh:
    SERVER_SOURCE = _fh.read()


class TestServerRoutes:
    """The sandbox is a separate process, so it reaches the wired companion
    over localhost - exactly how /screen/history already works."""

    def _body(self, name):
        tree = ast.parse(SERVER_SOURCE)
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == name)
        return ast.unparse(node)

    def test_all_three_routes_exist(self):
        for route in ("companion_start", "companion_stop", "companion_status"):
            assert route in SERVER_SOURCE, route

    def test_every_route_is_guarded(self):
        """They control behaviour and report what is on screen, so a forwarded
        caller must present the shared secret."""
        for route in ("companion_start", "companion_stop", "companion_status"):
            body = self._body(route)
            assert "_guard(request)" in body, route
            assert body.index("_guard(request)") < body.index("screen_companion")

    def test_start_never_raises_to_the_caller(self):
        body = self._body("companion_start")
        assert "except Exception" in body
        # ast.unparse canonicalises quotes, so match either form.
        assert "'ok': False" in body or '"ok": False' in body
        assert "status_code=500" in body

    def test_a_missing_companion_reports_honestly(self):
        body = self._body("companion_start")
        assert "not wired" in body
        assert "503" in body

    def test_the_helpers_talk_to_localhost(self):
        """The template must reach the server, not import the module."""
        profile = os.path.join(os.path.dirname(SERVER_PY), "profiles", "default.py")
        source = open(profile, encoding="utf-8").read()
        assert "/companion/start" in source
        assert "/companion/stop" in source
        assert "/companion/status" in source
        assert "127.0.0.1:10101" in source, "it must reach the server's port"
        # The wrong fix - importing the module from the sandbox - must be gone.
        assert "from source.server.screen_companion import" not in source

    def test_the_helpers_use_the_right_verb_per_route(self):
        """`/companion/status` is a GET; POSTing to it returns 405.

        That is exactly what happened on the first live run: start and stop
        worked, every status read failed with 405, and the empty `last_error`
        hid why. Pin the verbs so the pair cannot drift apart again.
        """
        # Route verbs live in server.py; the helper that calls them lives in
        # the profile. Both halves must agree or the pair 405s.
        assert '@interpreter.server.app.get("/companion/status")' in SERVER_SOURCE
        assert '@interpreter.server.app.post("/companion/start")' in SERVER_SOURCE
        assert '@interpreter.server.app.post("/companion/stop")' in SERVER_SOURCE

        profile = os.path.join(os.path.dirname(SERVER_PY), "profiles", "default.py")
        source = open(profile, encoding="utf-8").read()
        assert '_companion_call("/companion/status", method="GET")' in source

    def test_the_prompt_tells_her_not_to_improvise(self):
        """The first live test made her build her own companion with a print
        fallback, so every tip went to the console instead of her speakers."""
        profile = os.path.join(os.path.dirname(SERVER_PY), "profiles", "default.py")
        source = open(profile, encoding="utf-8").read()
        assert "CALL THE HELPER DIRECTLY" in source
        assert "queue_direct_response" in source, (
            "the prompt must warn that importing it fails - it is a closure")


if __name__ == "__main__":
    unittest.main()
