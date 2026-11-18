"""Background activity journal - a MACHINE taking notes, not the AI.

WHAT THIS IS
------------
A small always-on sampler that keeps a rolling note of what the PC is doing, so
FRIDAY can answer "what was I doing five minutes ago?" from a record rather than
from the model's memory or a single screen grab.

It deliberately does NOT use the model. It is a tape recorder, not a narrator:
cheap, always-on, and it cannot confabulate. The existing 15-second screen
history answers "what did the screen LOOK like"; this answers "what was
happening" - CPU, the focused window, mouse position, and keys pressed.

ROLLING WINDOW
--------------
Only the last `FRIDAY_ACTIVITY_WINDOW` seconds (default 300 = 5 minutes) are
kept. Older samples are dropped as new ones arrive, and the document is rewritten
from the live buffer each tick, so the file itself is always exactly "the last
five minutes" - the previous period disappears rather than accumulating.

WHAT IS RECORDED
----------------
Every interval (default 30s): CPU %, memory %, foreground window title + its
process, mouse position, and the number of keys pressed since the previous
sample. Typed CHARACTERS are masked to a single dot by default, because an
arbitrary password does not match any secret format the project redactor
knows and would be stored verbatim. Named keys (Enter, Tab) keep their name, so
the shape of typing is still visible. `FRIDAY_ACTIVITY_KEYS=verbatim` opts into
recording characters, with redaction still applied.

The "hottest process" detail is OFF by default (`FRIDAY_ACTIVITY_TOP=1` to
enable): measuring it showed the process-list walk is ~860ms of CPU and, with a
free window title + process name already recorded, it is the entire running
cost for very little added information.

PRIVACY
-------
Everything stays local, in the user's own home directory. Nothing here is sent
to any provider, and nothing here is read into a prompt unless FRIDAY is asked
about it. Key text is redacted at write time, so a stray password typed at the
keyboard is not recorded verbatim.

OFF BY DEFAULT. It samples the keyboard and the mouse, so it only runs when
someone asked for it: `FRIDAY_ACTIVITY_JOURNAL=1`, or create the marker file
`%USERPROFILE%\\.friday\\activity_journal.on`. An explicit
`FRIDAY_ACTIVITY_JOURNAL=0` always wins.
"""

import collections
import os
import threading
import time

DEFAULT_INTERVAL = 30.0        # 30s: cheap enough to be worth leaving on
DEFAULT_WINDOW = 300.0         # 5 minutes, per Sir's spec
MAX_INTERVAL = 120.0
MIN_INTERVAL = 1.0
# Hard ceiling on retained keystroke entries regardless of window. Keystrokes
# arrive per-keystroke, not per-sample, so this is the one buffer that could
# otherwise grow with typing rather than with time.
KEY_BUFFER = 150


def _journal_path(environ=None):
    environ = os.environ if environ is None else environ
    root = environ.get("FRIDAY_HOME") or os.path.join(
        os.path.expanduser("~"), ".friday")
    return environ.get("FRIDAY_ACTIVITY_JOURNAL_PATH") or os.path.join(
        root, "activity_journal.md")


def _marker_path(environ=None):
    """Presence of this file turns the journal on - the easy toggle.

    Env vars inside a hidden auto-start launcher are awkward to reach; creating
    or deleting a file is not.
    """
    environ = os.environ if environ is None else environ
    root = environ.get("FRIDAY_HOME") or os.path.join(
        os.path.expanduser("~"), ".friday")
    return os.path.join(root, "activity_journal.on")


def _redact(text):
    try:
        from .redaction import redact
        return redact(str(text))
    except Exception:
        return str(text)[:64]


class ActivityJournal:
    """Rolling, model-free record of machine activity."""

    def __init__(self, environ=None, clock=time.monotonic, wall=time.time):
        self.environ = os.environ if environ is None else environ
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._samples = collections.deque()
        self._keys = collections.deque(maxlen=KEY_BUFFER)
        self._clicks = collections.deque(maxlen=200)
        self._started = False
        self._stop = threading.Event()
        self._thread = None
        self._listeners = []
        self._last_sample_at = 0.0
        self._peak_cpu = 0.0
        self._peak_at = 0.0
        self._window_switches = 0
        self._last_window = ""
        self._last_error = ""
        self._samples_written = 0
        self._top_cache = None

    # -- configuration -----------------------------------------------------
    def _interval(self):
        raw = self.environ.get("FRIDAY_ACTIVITY_INTERVAL")
        try:
            value = float(raw) if raw not in (None, "") else DEFAULT_INTERVAL
        except (TypeError, ValueError):
            value = DEFAULT_INTERVAL
        return max(MIN_INTERVAL, min(MAX_INTERVAL, value))

    def _window_seconds(self):
        raw = self.environ.get("FRIDAY_ACTIVITY_WINDOW")
        try:
            value = float(raw) if raw not in (None, "") else DEFAULT_WINDOW
        except (TypeError, ValueError):
            value = DEFAULT_WINDOW
        return max(30.0, min(3600.0, value))

    def enabled(self):
        """Off by default (Sir, 2026-10-19: "turn off the journal for now").

        Turn it back on either way:
          * `FRIDAY_ACTIVITY_JOURNAL=1`, or
          * create the marker file `%USERPROFILE%\\.friday\\activity_journal.on`

        An explicit env value always wins, so `FRIDAY_ACTIVITY_JOURNAL=0`
        forces it off even if the marker file exists. Defaulting to off means a
        feature that samples the keyboard and mouse does not run unless someone
        actually asked for it.
        """
        raw = str(self.environ.get("FRIDAY_ACTIVITY_JOURNAL", "")).strip().lower()
        if raw in ("0", "false", "no", "off"):
            return False
        if raw in ("1", "true", "yes", "on"):
            return True
        try:
            return os.path.exists(_marker_path(self.environ))
        except Exception:
            return False

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        """Idempotent, never raises. Returns True when the sampler is running."""
        if not self.enabled():
            return False
        with self._lock:
            if self._started:
                return True
            self._started = True
        self._start_listeners()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="friday-activity-journal")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception:
                pass
        self._listeners = []

    def _start_listeners(self):
        """Keyboard/mouse hooks. Best-effort: journaling degrades, never breaks."""
        try:
            from pynput import keyboard, mouse

            def on_press(key):
                try:
                    label = getattr(key, "char", None) or str(key).replace("Key.", "")
                    if label:
                        self._on_key(label)
                except Exception:
                    pass

            def on_click(_x, _y, button, pressed):
                if pressed:
                    self._on_click(button)

            kbd = keyboard.Listener(on_press=on_press)
            ptr = mouse.Listener(on_click=on_click)
            kbd.daemon = True
            ptr.daemon = True
            kbd.start()
            ptr.start()
            self._listeners = [kbd, ptr]
        except Exception as error:
            # A journal without key hooks is still useful - window and CPU
            # alone answer a lot of "what was I doing".
            self._last_error = "input hooks unavailable: %s" % type(error).__name__

    def _on_key(self, label):
        with self._lock:
            self._keys.append((self._wall(), self._mask_key(label)))

    def _mask_key(self, label):
        """Typed CHARACTERS are masked unless Sir explicitly opts in.

        Keystroke text is the single most dangerous thing to journal: a
        password, a message, a token. The project redactor only catches known
        secret *formats*, so an arbitrary password sails straight through it
        (measured: "hunter2" came back verbatim). So the default records the
        SHAPE of typing - a dot per character, and named keys by name - which
        still answers "was he typing, and was it prose or shortcuts?" without
        storing what was typed. FRIDAY_ACTIVITY_KEYS=verbatim opts in, and even
        then the redactor still runs over it.
        """
        if str(self.environ.get("FRIDAY_ACTIVITY_KEYS", "")).strip().lower() in (
                "verbatim", "1", "true", "yes"):
            return _redact(label)
        text = str(label)
        if len(text) == 1:
            return "•"
        return text[:16]

    def _on_click(self, button):
        with self._lock:
            self._clicks.append((self._wall(), str(button).replace("Button.", "")))

    # -- sampling -----------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._sample()
                self._flush()
            except Exception as error:
                self._last_error = type(error).__name__ + ": " + str(error)[:160]
            self._stop.wait(self._interval())

    def _foreground(self):
        """(window title, process name) for the focused window, or ("", "")."""
        try:
            import win32gui
            import win32process
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return "", ""
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = ""
            try:
                import psutil
                process = psutil.Process(pid).name()
            except Exception:
                process = ""
            return title[:160], process
        except Exception:
            return "", ""

    def _top_enabled(self):
        """Is the (expensive) hottest-process detail wanted at all?

        Measured 2026-10-19: one `_walk_top()` costs ~860ms of CPU because
        `psutil.process_iter` passes over ~250 processes twice to make
        `cpu_percent` meaningful. Every other field in a sample costs effectively
        nothing (<0.01ms cached, 1.55ms to write the document), so this single
        detail is ~100% of the journal's running cost - 1.5% of a core at a
        60s refresh, for what is meant to be a note-taker.

        Genuinely useful ("was it the browser or the compiler?"), but the window
        title and its process name are FREE and already answer most of "what was
        I doing?", so the walk is off by default. FRIDAY_ACTIVITY_TOP=1 turns it
        back on, and FRIDAY_ACTIVITY_TOP_EVERY controls how often it re-walks.
        """
        return str(self.environ.get("FRIDAY_ACTIVITY_TOP", "0")).strip() != "0"

    def _top_process(self):
        """Top CPU process, opt-in and refreshed on a slow cadence.

        See `_top_enabled`: this one detail is the journal's entire running cost.
        When enabled it is refreshed once per `_top_every()` seconds and reused
        in between; when disabled it costs nothing and returns empty.
        """
        if not self._top_enabled():
            return "", 0.0
        now = self._clock()
        if self._top_cache and (now - self._top_cache[0]) < self._top_every():
            return self._top_cache[1], self._top_cache[2]
        try:
            name, usage = self._walk_top()
            self._top_cache = (now, name, usage)
            return name, usage
        except Exception:
            return "", 0.0

    def _walk_top(self):
        """The expensive bit: one full pass over the process list.

        Split out from `_top_process` so the cache around it can be tested
        without actually walking several hundred processes in the test run.
        """
        import psutil
        best = None
        best_cpu = 0.0
        for proc in psutil.process_iter(["name", "cpu_percent"]):
            try:
                usage = float(proc.info.get("cpu_percent") or 0.0)
            except Exception:
                continue
            if usage > best_cpu:
                best_cpu = usage
                best = str(proc.info.get("name") or "")
        return best or "", best_cpu

    def _top_every(self):
        """How often (seconds) to re-walk the process list for the top process.

        Only relevant when `_top_enabled()`. Default 300: "what is hottest right
        now" rarely changes within five minutes, and each walk costs ~860ms.
        """
        raw = self.environ.get("FRIDAY_ACTIVITY_TOP_EVERY")
        try:
            value = float(raw) if raw not in (None, "") else 300.0
        except (TypeError, ValueError):
            value = 300.0
        return max(5.0, min(600.0, value))

    def _mouse_position(self):
        try:
            from pynput.mouse import Controller
            x, y = Controller().position
            return int(x), int(y)
        except Exception:
            try:
                import win32api
                return int(win32api.GetCursorPos()[0]), int(win32api.GetCursorPos()[1])
            except Exception:
                return None, None

    def _sample(self):
        import psutil

        now = self._clock()
        wall = self._wall()
        cpu = float(psutil.cpu_percent(interval=None))
        memory = float(psutil.virtual_memory().percent)
        top_name, top_cpu = self._top_process()
        title, proc = self._foreground()
        mouse_x, mouse_y = self._mouse_position()

        with self._lock:
            keys_since = len([k for k in self._keys if k[0] >= wall - self._interval() * 1.5])
            clicks_since = len([c for c in self._clicks if c[0] >= wall - self._interval() * 1.5])
            if title and title != self._last_window:
                if self._last_window:
                    self._window_switches += 1
                self._last_window = title
            if cpu > self._peak_cpu:
                self._peak_cpu = cpu
                self._peak_at = wall
            self._samples.append({
                "at": wall,
                "mono": now,
                "cpu": round(cpu, 1),
                "mem": round(memory, 1),
                "top": top_name,
                "top_cpu": round(top_cpu, 1),
                "window": title,
                "process": proc,
                "mouse": (mouse_x, mouse_y),
                "keys": keys_since,
                "clicks": clicks_since,
            })
            self._trim_locked()

    def _trim_locked(self):
        """Drop everything older than the rolling window."""
        horizon = self._clock() - self._window_seconds()
        while self._samples and self._samples[0]["mono"] < horizon:
            self._samples.popleft()
        wall_horizon = self._wall() - self._window_seconds()
        while self._keys and self._keys[0][0] < wall_horizon:
            self._keys.popleft()
        while self._clicks and self._clicks[0][0] < wall_horizon:
            self._clicks.popleft()

    # -- output -------------------------------------------------------------
    def _render(self):
        with self._lock:
            samples = list(self._samples)
            keys = list(self._keys)
            clicks = list(self._clicks)
            stats = {
                "peak_cpu": self._peak_cpu,
                "peak_at": self._peak_at,
                "switches": self._window_switches,
                "last_error": self._last_error,
                "samples": len(samples),
            }

        if not samples:
            return "# FRIDAY activity journal\n\n(No samples yet - the sampler is starting up.)\n"

        def stamp(value):
            try:
                return time.strftime("%H:%M:%S", time.localtime(value))
            except Exception:
                return "?"

        window = samples[-1]["at"] - samples[0]["at"]
        lines = [
            "# FRIDAY activity journal - rolling window",
            "",
            "Updated: %s   |   window: last %.0fs   |   samples: %d"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), max(window, 0.0), len(samples)),
            "",
            "> Machine-recorded only. No model saw this. Local file; never sent anywhere.",
            "> Older than this window is already deleted - the file IS the last five minutes.",
            "",
            "## Timeline",
            "",
            "```",
        ]
        for sample in samples:
            mouse = sample["mouse"]
            mouse_text = "(%s,%s)" % mouse if mouse and mouse[0] is not None else "(-,-)"
            lines.append(
                "%s  cpu=%5.1f%%  mem=%5.1f%%  keys=%-3d clicks=%-2d  mouse=%-12s  %s | %s"
                % (stamp(sample["at"]), sample["cpu"], sample["mem"],
                   sample["keys"], sample["clicks"], mouse_text,
                   sample["process"] or "?", sample["window"] or "(no window)")
            )
        lines.append("```")
        lines.append("")
        lines.append("## Recent keys (redacted, newest last)")
        lines.append("")
        if keys:
            rendered = " ".join(_redact(label) for _ts, label in keys[-200:])
            lines.append("`%s`" % rendered[:1500])
        else:
            lines.append("(no keys recorded in this window)")
        lines.append("")
        lines.append("## Recent clicks")
        lines.append("")
        lines.append(", ".join("%s %s" % (stamp(ts), btn) for ts, btn in clicks[-40:])
                     or "(none)")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append("- peak CPU %.1f%% at %s" % (stats["peak_cpu"], stamp(stats["peak_at"])))
        lines.append("- window switches in window: %d" % stats["switches"])
        if stats["last_error"]:
            lines.append("- sampler note: %s" % stats["last_error"])
        lines.append("")
        return "\n".join(lines)

    def _flush(self):
        """Rewrite the rolling document atomically so a reader never sees a
        half-written file."""
        path = _journal_path(self.environ)
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(self._render())
            os.replace(temporary, path)
            self._samples_written += 1
        except Exception as error:
            self._last_error = "write failed: %s" % type(error).__name__

    # -- queries ------------------------------------------------------------
    def summary(self, seconds=None):
        """A compact text summary for FRIDAY to quote - never invented."""
        seconds = float(seconds or self._window_seconds())
        horizon = self._clock() - seconds
        with self._lock:
            recent = [s for s in self._samples if s["mono"] >= horizon]
        if not recent:
            return "I have no activity notes for the last %.0f seconds." % seconds
        first, last = recent[0], recent[-1]
        windows = [s["window"] for s in recent if s["window"]]
        top_window = ""
        counts = {}
        for w in windows:
            counts[w] = counts.get(w, 0) + 1
        if counts:
            top_window = max(counts, key=counts.get)
        keys_total = sum(s["keys"] for s in recent)
        peak = max(s["cpu"] for s in recent)
        return (
            "Activity over the last %.0fs: %d samples, peak CPU %.0f%%, "
            "%d keys pressed, focused window most often %r. "
            "Most recent: %r (%s)."
            % (seconds, len(recent), peak, keys_total,
               top_window or "(unknown)", last["window"] or "(none)",
               last["process"] or "?")
        )

    def path(self):
        return _journal_path(self.environ)

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled(),
                "running": self._started and not self._stop.is_set(),
                "samples": len(self._samples),
                "keys_buffered": len(self._keys),
                "window_seconds": self._window_seconds(),
                "interval": self._interval(),
                "writes": self._samples_written,
                "path": self.path(),
                "last_error": self._last_error,
            }


_JOURNAL = None


def get_journal(reload=False):
    """Process-wide singleton - one sampler, not one per caller."""
    global _JOURNAL
    if _JOURNAL is None or reload:
        _JOURNAL = ActivityJournal()
    return _JOURNAL
