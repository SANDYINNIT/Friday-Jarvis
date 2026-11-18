"""Round ZB-ACT: the background activity journal.

Sir asked for a MACHINE, not the AI, taking notes about the PC so FRIDAY can
answer "what was I doing five minutes ago?" from a record. These tests pin the
three properties that make it trustworthy:

1. ROLLING - only the last N seconds exist; older notes are deleted, and the
   document is rewritten from the live buffer rather than appended, so the file
   IS the window.
2. HONEST - it reads the sampler's real buffer. With no samples it says so,
   rather than inventing a plausible-sounding summary.
3. LOCAL AND REDACTED - key text is redacted before it is written, and the
   sampler involves no model at all (a tape recorder, not a narrator).

Every test injects its own clock, so nothing sleeps.
"""

import os
import tempfile

from source.server.activity_journal import (  # noqa: E402
    ActivityJournal, get_journal)


def _journal(window=60.0, interval=5.0, **extra):
    environ = {
        "FRIDAY_HOME": tempfile.mkdtemp(prefix="friday-act-"),
        "FRIDAY_ACTIVITY_WINDOW": str(window),
        "FRIDAY_ACTIVITY_INTERVAL": str(interval),
        "FRIDAY_ACTIVITY_JOURNAL": "1",
    }
    environ.update(extra)
    clock = {"now": 1000.0}
    wall = {"now": 1_700_000_000.0}
    journal = ActivityJournal(
        environ=environ,
        clock=lambda: clock["now"],
        wall=lambda: wall["now"],
    )
    return journal, clock, wall


def _feed(journal, n, step=5.0, clock=None, wall=None):
    for _ in range(n):
        if clock is not None:
            clock["now"] += step
        if wall is not None:
            wall["now"] += step
        journal._sample()


class TestRollingWindow:
    def test_samples_older_than_the_window_are_dropped(self):
        journal, clock, wall = _journal(window=60.0)
        _feed(journal, 20, step=5.0, clock=clock, wall=wall)   # 100s of samples
        with journal._lock:
            oldest = journal._samples[0]["mono"]
        assert oldest >= clock["now"] - 60.0, (
            "the journal kept samples older than its window")

    def test_the_document_is_rewritten_not_appended(self):
        """Old content must vanish. If the file were append-only it would grow
        forever and stop meaning 'the last five minutes'."""
        journal, clock, wall = _journal(window=60.0)
        _feed(journal, 4, step=5.0, clock=clock, wall=wall)
        journal._flush()
        early = open(journal.path(), encoding="utf-8").read()
        _feed(journal, 20, step=5.0, clock=clock, wall=wall)
        journal._flush()
        late = open(journal.path(), encoding="utf-8").read()
        assert len(late) < len(early) * 3, "the document is growing without bound"
        assert late.count("cpu=") < early.count("cpu=") + 25

    def test_the_document_is_small_and_bounded(self):
        journal, clock, wall = _journal(window=300.0)
        # 60 samples at 5s = 300s, exactly one window: enough to prove the file
        # stays small without paying for hundreds of real psutil walks.
        _feed(journal, 60, step=5.0, clock=clock, wall=wall)
        journal._flush()
        text = open(journal.path(), encoding="utf-8").read()
        assert len(text) < 200_000, "rolling document should stay small"

    def test_the_key_buffer_is_bounded(self):
        journal, _clock, _wall = _journal()
        for i in range(3000):
            journal._on_key("k%d" % i)
        with journal._lock:
            assert len(journal._keys) <= 150


class TestHonesty:
    def test_empty_journal_says_so_rather_than_inventing(self):
        journal, _clock, _wall = _journal()
        summary = journal.summary()
        assert "no activity notes" in summary, summary

    def test_the_document_says_nothing_when_empty(self):
        journal, _clock, _wall = _journal()
        journal._flush()
        text = open(journal.path(), encoding="utf-8").read()
        assert "No samples yet" in text

    def test_summary_reports_real_numbers(self):
        journal, clock, wall = _journal()
        _feed(journal, 3, step=5.0, clock=clock, wall=wall)
        summary = journal.summary()
        assert "3 samples" in summary, summary

    def test_the_document_states_it_is_machine_recorded(self):
        journal, clock, wall = _journal()
        _feed(journal, 2, step=5.0, clock=clock, wall=wall)
        journal._flush()
        text = open(journal.path(), encoding="utf-8").read()
        assert "Machine-recorded" in text
        assert "No model" in text


class TestCostGuards:
    """The top-process walk is the one expensive thing this journal does.

    Measured 2026-10-19: walking `psutil.process_iter` on EVERY sample cost
    ~970ms of CPU per tick - 22% of a core, sustained. Isolating the parts later
    showed the walk is ~860ms while a cached sample is <0.01ms and writing the
    document is 1.55ms, i.e. that one detail is ~100% of the running cost. It is
    now OFF by default and, when enabled, refreshed on a slow cadence.
    """

    def test_the_defaults_are_light(self):
        """The shipped defaults, pinned so they cannot quietly get expensive
        again - or start running when nobody asked for it.

        Off by default (Sir, 2026-10-19): it samples the keyboard and the
        mouse, so it runs only when explicitly enabled. 30s sampling, 5-minute
        window, no process walk, masked keys.
        """
        journal, _clock, _wall = _journal()
        environ_only = ActivityJournal(environ={})
        assert environ_only._interval() == 30.0
        assert environ_only._window_seconds() == 300.0
        assert environ_only._top_enabled() is False
        assert environ_only._top_every() == 300.0
        assert environ_only.enabled() is False, "the journal must be opt-in"
        assert journal.enabled() is True, "the env flag in this helper enables it"

    def test_the_marker_file_enables_it(self):
        """The easy toggle: create a file, no env vars inside a hidden launcher."""
        home = tempfile.mkdtemp(prefix="friday-marker-")
        environ = {"FRIDAY_HOME": home}
        journal, _clock, _wall = _journal()
        journal.environ = environ
        assert journal.enabled() is False
        with open(os.path.join(home, "activity_journal.on"), "w") as handle:
            handle.write("on\n")
        assert journal.enabled() is True, "the marker file should enable it"

    def test_env_zero_overrides_the_marker_file(self):
        home = tempfile.mkdtemp(prefix="friday-marker-")
        with open(os.path.join(home, "activity_journal.on"), "w") as handle:
            handle.write("on\n")
        journal, _clock, _wall = _journal(FRIDAY_ACTIVITY_JOURNAL="0")
        journal.environ = {"FRIDAY_HOME": home, "FRIDAY_ACTIVITY_JOURNAL": "0"}
        assert journal.enabled() is False, (
            "an explicit 0 must force it off even with the marker present")

    def test_the_keystroke_buffer_is_capped(self):
        """Keystrokes arrive per-keystroke, not per-sample, so this is the one
        buffer that could otherwise grow with typing rather than with time."""
        journal, _clock, _wall = _journal()
        for i in range(5000):
            journal._on_key("k%d" % i)
        with journal._lock:
            assert len(journal._keys) <= 150

    def test_the_walk_is_off_by_default(self):
        """Off by default: the free window title + process name already answer
        'what was I doing?', and the walk is the entire CPU cost."""
        journal, _clock, _wall = _journal()
        assert journal._top_enabled() is False
        calls = {"n": 0}

        def counting_walk():
            calls["n"] += 1
            return "chrome.exe", 55.0

        journal._walk_top = counting_walk
        name, usage = journal._top_process()
        assert calls["n"] == 0, "the process list was walked despite being disabled"
        assert name == "" and usage == 0.0

    def test_the_walk_can_be_enabled(self):
        journal, _clock, _wall = _journal(FRIDAY_ACTIVITY_TOP="1")
        assert journal._top_enabled() is True

    def test_the_process_walk_is_reused_within_its_ttl(self):
        journal, clock, _wall = _journal(FRIDAY_ACTIVITY_TOP="1",
                                         FRIDAY_ACTIVITY_TOP_EVERY="60")
        calls = {"n": 0}

        def counting_walk():
            calls["n"] += 1
            return "chrome.exe", 55.0

        journal._top_cache = None
        journal._walk_top = counting_walk        # stand in for the real process walk

        journal._sample()
        clock["now"] += 5.0
        journal._sample()
        clock["now"] += 5.0
        journal._sample()
        assert calls["n"] == 1, (
            "the process list was walked %d times in 10s - it must be cached "
            "for the whole TOP_EVERY window" % calls["n"])

    def test_the_walk_happens_again_after_the_ttl(self):
        journal, clock, _wall = _journal(FRIDAY_ACTIVITY_TOP="1",
                                         FRIDAY_ACTIVITY_TOP_EVERY="60")
        calls = {"n": 0}

        def counting_walk():
            calls["n"] += 1
            return "chrome.exe", 55.0

        journal._top_cache = None
        journal._walk_top = counting_walk
        journal._sample()
        clock["now"] += 61.0
        journal._sample()
        assert calls["n"] == 2, "the cache never expired"

    def test_the_top_cadence_is_configurable_and_bounded(self):
        journal, _clock, _wall = _journal(FRIDAY_ACTIVITY_TOP_EVERY="99999")
        assert journal._top_every() == 600.0, "unbounded value was not clamped"
        journal2, _c2, _w2 = _journal(FRIDAY_ACTIVITY_TOP_EVERY="1")
        assert journal2._top_every() == 5.0, "tiny value was not clamped"

    def test_the_top_cadence_defaults_to_five_minutes(self):
        journal, _clock, _wall = _journal()
        assert journal._top_every() == 300.0


class TestPrivacy:
    def test_key_characters_are_masked_by_default(self):
        """Typed characters must never be stored verbatim by default.

        The project redactor only catches known secret FORMATS, so an arbitrary
        password passes straight through it (measured: "hunter2" came back
        verbatim). Default behaviour records the SHAPE of typing, not its text.
        """
        journal, clock, wall = _journal()
        for char in "hunter2":
            journal._on_key(char)
        journal._on_key("enter")          # named keys keep their name
        _feed(journal, 2, step=5.0, clock=clock, wall=wall)
        journal._flush()
        text = open(journal.path(), encoding="utf-8").read()
        keys_section = text.split("## Recent keys")[1].split("## Recent clicks")[0]
        assert "hunter2" not in keys_section, keys_section
        assert "•" in keys_section, "no per-character masking happened"
        assert "enter" in keys_section, "named keys should stay readable"

    def test_verbatim_is_possible_but_only_by_opt_in(self):
        journal, clock, wall = _journal(FRIDAY_ACTIVITY_KEYS="verbatim")
        for char in "hi":
            journal._on_key(char)
        _feed(journal, 1, step=5.0, clock=clock, wall=wall)
        journal._flush()
        text = open(journal.path(), encoding="utf-8").read()
        keys_section = text.split("## Recent keys")[1].split("## Recent clicks")[0]
        # The renderer joins keys with a space, so check the characters landed.
        assert "h" in keys_section and "i" in keys_section, keys_section
        assert "•" not in keys_section, "verbatim opt-in must not mask"

    def test_the_module_imports_no_model_stack(self):
        """A tape recorder, not a narrator: it must not drag in the brain."""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "source", "server", "activity_journal.py")
        if not os.path.exists(path):
            path = "source/server/activity_journal.py"
        source = open(path, encoding="utf-8").read()
        for banned in ("litellm", "interpreter", "brain_router", "openai"):
            assert banned not in source, "activity_journal should not import %s" % banned


class TestLifecycle:
    def test_disabled_by_flag(self):
        journal, _clock, _wall = _journal(FRIDAY_ACTIVITY_JOURNAL="0")
        assert journal.enabled() is False
        assert journal.start() is False

    def test_start_is_idempotent_and_never_raises(self):
        journal, _clock, _wall = _journal(FRIDAY_ACTIVITY_JOURNAL="0")
        assert journal.start() is False
        assert journal.start() is False

    def test_status_reports_running_state_and_path(self):
        journal, _clock, _wall = _journal()
        status = journal.status()
        assert status["enabled"] is True
        assert status["running"] is False
        assert status["path"].endswith(".md")

    def test_the_singleton_is_reused(self):
        first = get_journal()
        second = get_journal()
        assert first is second

    def test_the_write_is_atomic(self):
        """A reader must never see a half-written journal."""
        journal, clock, wall = _journal()
        _feed(journal, 2, step=5.0, clock=clock, wall=wall)
        journal._flush()
        assert not os.path.exists(journal.path() + ".tmp"), (
            "a temporary file was left behind - the write is not atomic")
