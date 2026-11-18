"""Round ZB-F: `tts_start` / `tts_first_audio` must pair on EVERY speaking turn.

WHAT WAS WRONG
--------------
The log showed `tts_start` at 14:33:29 and 14:35:14 with no `tts_first_audio`
anywhere near them, which reads exactly like the TTS engine failing to produce
audio. It was not a TTS failure.

`on_tts_chunk` traces `tts_first_audio` only when the "seen audio yet" flag flips,
and that flag was initialised ONCE in `start_server` at boot and set on the
process's first playback - never re-armed for a later turn. So the marker could
fire at most ONCE per server process: after that, every healthy speaking turn
logged `tts_start` alone for the rest of the process's life.

WHY THE FLAG LIVES ON `server_state`
-------------------------------------
The first attempt stored it as a `start_server` closure local and re-armed it
with `nonlocal` inside `traced_completions`. That broke the suite: several
suites compile `traced_completions` in isolation and a `nonlocal` with no
enclosing binding is a SyntaxError, taking down 19 errors and 50 failures.
Keeping it on `server_state` needs no `nonlocal` (only rebinding the NAME would)
and every such harness already passes `server_state`.
"""

import ast
import os
import types

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
with open(SERVER_PY, encoding="utf-8") as _fh:
    SOURCE = _fh.read()
TREE = ast.parse(SOURCE)

FLAG = "tts_audio_seen"


def _find(name, kind=(ast.FunctionDef, ast.AsyncFunctionDef)):
    for node in ast.walk(TREE):
        if isinstance(node, kind) and node.name == name:
            return node
    raise AssertionError("could not find %r" % name)


def _flag_dict_init():
    """The boot-time `"tts_audio_seen": False` entry in the initial server_state
    dict literal (not an assignment statement, so it needs its own lookup)."""
    hits = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == FLAG
                    and isinstance(value, ast.Constant) and value.value is False):
                hits.append(node)
    return hits


def _flag_writes(scope_name=None):
    """Every `server_state["tts_audio_seen"] = <bool>` assignment.

    `scope_name` narrows to a specific enclosing function, which is how we
    distinguish the boot-time init from the per-turn re-arm.
    """
    hits = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == FLAG):
            continue
        if not (isinstance(node.value, ast.Constant)
                and node.value.value is False):
            continue
        if scope_name is None:
            hits.append(node)
            continue
        owner = next((fn for fn in ast.walk(TREE)
                      if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and fn.name == scope_name
                      and fn.lineno <= node.lineno <= (fn.end_lineno or 0)), None)
        if owner is not None:
            hits.append(node)
    return hits


def _rearm_assignment():
    nodes = _flag_writes("traced_completions")
    if not nodes:
        raise AssertionError(
            "server_state['tts_audio_seen'] is never re-armed inside "
            "traced_completions - the marker would fire once per PROCESS")
    return nodes[0]


class TestMarkerIsRearmed:
    def test_the_flag_is_rearmed_per_turn(self):
        """THE defect: without this, tts_first_audio fires once per PROCESS."""
        assert _flag_writes("traced_completions"), (
            "the marker flag is never re-armed per turn, so tts_first_audio "
            "can only fire on a process's first playback")
        node = _rearm_assignment()
        assert node.value.value is False

    def test_boot_initialises_the_flag(self):
        """A fresh process must start with the flag clear, or the first turn
        would be silenced by whatever the previous run left behind."""
        init = _flag_dict_init()
        assert init, "server_state has no `tts_audio_seen: False` entry at boot"

    def test_it_is_written_exactly_twice(self):
        # Once at boot (dict init), once per-turn (assignment). Fewer = the old
        # bug; more = someone is latching it somewhere else.
        per_turn = _flag_writes("traced_completions")
        assert len(per_turn) == 1, per_turn
        assert len(_flag_dict_init()) == 1, _flag_dict_init()

    def test_the_local_closure_variable_is_gone(self):
        """The first attempt kept it as a start_server local, which forced a
        `nonlocal` and broke isolated compilation. It must be gone entirely."""
        local_decl = [
            n for n in ast.walk(TREE)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == FLAG for t in n.targets)
        ]
        assert not local_decl, (
            "tts_audio_seen is a bare local again - that is what forced the "
            "nonlocal that broke 19 test errors across the suite: %s" % local_decl)

    def test_no_nonlocal_is_used_for_the_flag(self):
        """The regression this design avoids: `nonlocal tts_audio_seen` inside
        traced_completions makes every suite that compiles that function in
        isolation fail with SyntaxError."""
        for node in ast.walk(TREE):
            if isinstance(node, ast.Nonlocal) and FLAG in node.names:
                raise AssertionError(
                    "tts_audio_seen must stay on server_state; a `nonlocal` "
                    "breaks isolated compilation of traced_completions")

    def test_the_rearm_sits_with_the_other_per_turn_budgets(self):
        """It must be gated by the SAME per-turn condition as the tool budget,
        so a later turn cannot arrive without it."""
        fn = _find("traced_completions")

        def _targets(node):
            return [ast.unparse(t).replace("'", '"') for t in node.targets]

        tool_resets = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Assign) and _targets(n) == ['tool_counter["n"]']
        ]
        assert tool_resets, "per-turn tool budget reset not found"
        parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}

        def _containing_if(node):
            cur = node
            while cur is not None:
                if isinstance(cur, ast.If):
                    return cur
                cur = parents.get(cur)
            return None

        assert _containing_if(_rearm_assignment()) is _containing_if(tool_resets[0]), (
            "the marker re-arm is gated differently from the tool budget")


class TestEveryPlaybackStartIsTraced:
    def _playback_sites(self):
        sites = []
        for node in ast.walk(TREE):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_start_owned_playback"):
                sites.append(node.lineno)
        return sites

    def test_all_three_playback_sites_trace_tts_start(self):
        """Streaming branch, end branch and the voice-pause ack all start
        playback through `on_tts_chunk`, so each must trace `tts_start` -
        otherwise it produces `tts_first_audio` with nothing to pair it with."""
        sites = self._playback_sites()
        assert len(sites) >= 3, "expected three playback sites, found %d" % len(sites)
        lines = SOURCE.splitlines()
        for lineno in sites:
            # Wider window, and match the call PREFIX: the call now also carries
            # the serving TTS stage (`trace("tts_start", tts_stage=...)`).
            window = "\n".join(lines[lineno - 1: lineno + 9])
            assert 'trace("tts_start"' in window, (
                "play_async at line %d starts audio but never traces tts_start"
                % lineno
            )

    def test_first_audio_still_traces_exactly_once_per_turn(self):
        body = ast.unparse(_find("_record_tts_chunk"))
        # `ast.unparse` canonicalises quotes, so match the name not the literal.
        assert body.count(FLAG) >= 2, body
        assert body.count("tts_first_audio") == 1, body


class TestTheMarkerActuallyFiresAgain:
    """Behavioural: execute the REAL marker body and the REAL reset statement
    inside a purpose-built scope, so this tests production logic rather than a
    re-implementation of it."""

    @staticmethod
    def _build_source(with_reset):
        fn = _find("_record_tts_chunk")
        marker = "\n".join(ast.unparse(s) for s in fn.body)
        reset_line = ast.unparse(_rearm_assignment()) if with_reset else "pass"
        return (
            "def build():\n"
            '    server_state = {"tts_audio_seen": False}   # boot init\n'
            "    events = []\n"
            "    def trace(name):\n"
            "        events.append(name)\n"
            "    def on_chunk(chunk):\n"
            + "\n".join("        " + line for line in marker.splitlines()) + "\n"
            "    def per_turn_reset():\n"
            "        " + reset_line + "\n"
            "    return on_chunk, per_turn_reset, events\n"
        )

    @staticmethod
    def _run(turns, with_reset, chunk="x"):
        namespace = {
            "self": types.SimpleNamespace(
                output_queue=types.SimpleNamespace(
                    sync_q=types.SimpleNamespace(put=lambda *_: None))),
            "server_state": {},       # name only; build() shadows it
        }
        exec(TestTheMarkerActuallyFiresAgain._build_source(with_reset), namespace)
        on_chunk, reset, events = namespace["build"]()
        for _ in range(turns):
            reset()                 # the real per-turn re-arm
            on_chunk(chunk)         # first audio of this turn
            on_chunk(chunk)         # further audio, same turn
        return events

    def test_two_turns_each_get_a_marker(self):
        events = self._run(turns=2, with_reset=True)
        assert events.count("tts_first_audio") == 2, (
            "a later speaking turn produced no marker: %s" % events)

    def test_three_turns_each_get_a_marker(self):
        events = self._run(turns=3, with_reset=True)
        assert events.count("tts_first_audio") == 3, events

    def test_without_the_reset_only_the_first_turn_marks(self):
        """Negative control: the OLD bug. Deleting the reset line from server.py
        makes this fail here rather than in tomorrow's log."""
        events = self._run(turns=3, with_reset=False)
        assert events.count("tts_first_audio") == 1, (
            "boot-latching must produce exactly one marker total: %s" % events)

    def test_the_marker_is_not_repeated_within_one_turn(self):
        assert self._run(turns=1, with_reset=True).count("tts_first_audio") == 1
