"""ROUND ZB-L2: capture-timestamp truthfulness and short-term screen history.

Two separate claims are tested here, and they are different claims:

1. A frame's age must be TRUE. The capture loop used to read dxcam's cached
   frame and stamp it with the current time, so a screen that had not changed
   in minutes still reported itself as freshly photographed. These tests drive
   a camera fake that behaves the way dxcam actually behaves and assert that
   "nothing new" never advances the clock.

2. History must be useful, bounded, honest, and memory-only. Useful (a model can
   ask what was on screen N seconds ago), bounded (by seconds AND by count),
   honest (a moment that is gone returns nothing rather than the current
   screen), and memory-only (nothing is ever written to disk).
"""

import os
import tempfile
import unittest
from unittest import mock

from source.server import screen_capture_service as capture


def _frame(value=7, size=(1920, 1080)):
    """A BGRA frame shaped the way dxcam hands them over."""
    import numpy

    return numpy.full((size[1], size[0], 4), value, dtype=numpy.uint8)


class _HonestCamera:
    """A dxcam stand-in that tells the truth about what it has.

    The production bug lived in exactly this distinction, so the fake keeps it
    sharp: `grab(new_frame_only=True)` yields a frame only when the compositor
    has actually produced one, while `get_latest_frame()` and
    `grab(new_frame_only=False)` hand back whatever is already cached - which is
    the stale read that must never reach the timestamp.
    """

    def __init__(self, size=(1920, 1080)):
        self.size = size
        self.released = False
        self.calls = []
        self._pending = None
        self._last = None
        self._seq = 0

    def queue(self, value=None):
        """Make one genuinely new frame available."""
        self._seq += 1
        self._pending = _frame(value if value is not None else self._seq, self.size)
        return self._pending

    def grab(self, region=None, new_frame_only=False):
        self.calls.append(("grab", bool(new_frame_only)))
        if new_frame_only:
            frame, self._pending = self._pending, None
            if frame is not None:
                self._last = frame
            return frame
        return self._last          # the cached read

    def get_latest_frame(self):
        self.calls.append(("get_latest_frame", None))
        return self._last

    def release(self):
        self.released = True

    def cached_reads(self):
        """Calls that would have returned a possibly-stale cached frame."""
        return [c for c in self.calls
                if c[0] == "get_latest_frame" or (c[0] == "grab" and not c[1])]


class _Clock:
    """Monotonic clock the test drives, so nothing has to sleep."""

    def __init__(self, start=10_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _Env:
    """Set env vars for the duration of a test."""

    def __init__(self, **pairs):
        self.pairs = pairs

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.pairs}
        for key, value in self.pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _buffer(camera, clock, size=(1920, 1080)):
    buffer = capture._FrameBuffer(0, 0, size)
    buffer.camera = camera
    return buffer


# ---------------------------------------------------------------------------
# 1. Timestamp truthfulness
# ---------------------------------------------------------------------------
class TimestampTruthfulnessTests(unittest.TestCase):
    def test_a_cycle_with_nothing_new_keeps_the_previous_frame_and_its_age(self):
        clock = _Clock()
        camera = _HonestCamera()
        camera.queue(value=10)
        with mock.patch("time.monotonic", clock):
            buffer = _buffer(camera, clock)
            self.assertTrue(buffer.refresh(None), "first frame is genuine")
            stamp = buffer.captured_mono
            raw = buffer.raw

            clock.advance(0.5)
            self.assertFalse(buffer.refresh(None),
                             "no new frame must report no frame")
            self.assertIs(buffer.raw, raw,
                          "the cached frame must be left exactly as it was")
            self.assertEqual(buffer.captured_mono, stamp,
                             "the timestamp must not be re-stamped")
            self.assertGreaterEqual(buffer.age(), 0.5,
                                    "the age must keep growing while idle")

    def test_production_never_reads_the_cached_frame(self):
        """The regression itself: a cached read is what made the age a lie."""
        clock = _Clock()
        camera = _HonestCamera()
        camera.queue(value=3)
        with mock.patch("time.monotonic", clock):
            buffer = _buffer(camera, clock)
            buffer.refresh(None)
            for _ in range(5):
                buffer.refresh(None)
        self.assertEqual(camera.cached_reads(), [],
                         "refresh() must only ever ask for a NEW frame")
        self.assertTrue(all(c == ("grab", True) for c in camera.calls),
                        "expected only grab(new_frame_only=True), got %r"
                        % (camera.calls,))

    def test_a_genuine_frame_does_advance_the_timestamp(self):
        clock = _Clock()
        camera = _HonestCamera()
        with mock.patch("time.monotonic", clock):
            buffer = _buffer(camera, clock)
            camera.queue(value=1)
            buffer.refresh(None)
            first = buffer.captured_mono
            clock.advance(1.0)
            camera.queue(value=2)
            buffer.refresh(None)
            self.assertEqual(buffer.captured_mono, first + 1.0)

    def test_an_old_frame_is_reported_as_stale_rather_than_served(self):
        """This is the harm the old timestamp caused: stale looked fresh.

        Goes through the real service read path, because `frame()`'s TTL check
        is the thing that was being fed a false age.
        """
        clock = _Clock()
        camera = _HonestCamera()
        camera.queue(value=5)
        with mock.patch("time.monotonic", clock):
            buffer = _buffer(camera, clock)
            buffer.refresh(None)
            service = capture.ScreenCaptureService.__new__(
                capture.ScreenCaptureService)
            service._lock = capture.threading.Lock()
            service._buffers = [buffer]
            self.assertIsNotNone(service.frame(0, capture.DEFAULT_MAX_AGE),
                                 "a just-captured frame is servable")
            clock.advance(capture.HARD_STALE_SECONDS + 5)
            self.assertIsNone(service.frame(0, capture.DEFAULT_MAX_AGE),
                              "an old frame must NOT be served as current")
            self.assertGreater(buffer.age(), capture.DEFAULT_MAX_AGE)
            self.assertGreater(buffer.age(), capture.HARD_STALE_SECONDS)

    def test_a_wall_clock_jump_cannot_make_age_go_negative(self):
        """NTP/DST must not turn a stale frame into a 'brand new' one."""
        clock = _Clock()
        camera = _HonestCamera()
        camera.queue(value=9)
        with mock.patch("time.monotonic", clock):
            buffer = _buffer(camera, clock)
            buffer.refresh(None)
            clock.advance(2.0)
            buffer.captured_at = capture.time.time() + 3600  # clock jumped
            age = buffer.age()
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        self.assertAlmostEqual(age, 2.0, places=3)

    def test_age_is_none_before_anything_was_captured(self):
        clock = _Clock()
        buffer = capture._FrameBuffer(0, 0)
        with mock.patch("time.monotonic", clock):
            self.assertIsNone(buffer.age())


# ---------------------------------------------------------------------------
# 2. History tiers
# ---------------------------------------------------------------------------
class HistoryTierTests(unittest.TestCase):
    def _run_captures(self, buffer, camera, clock, count, step=0.5):
        for _ in range(count):
            camera.queue()
            clock.advance(step)
            buffer.refresh(None)

    def test_every_genuine_capture_records_a_tiny_tier_frame(self):
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"):
            buffer = _buffer(camera, clock, size=(320, 200))
            self._run_captures(buffer, camera, clock, 6)
        self.assertEqual(len(buffer.history), 6)
        self.assertTrue(all(e["tiny"] is not None for e in buffer.history))

    def test_a_cycle_with_nothing_new_does_not_enter_history(self):
        """Otherwise history becomes a wall of duplicates posing as a timeline."""
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"):
            buffer = _buffer(camera, clock, size=(320, 200))
            self._run_captures(buffer, camera, clock, 3)
            self.assertEqual(len(buffer.history), 3)
            for _ in range(5):
                clock.advance(0.5)
                buffer.refresh(None)
            self.assertEqual(len(buffer.history), 3,
                             "idle cycles must not add history entries")

    def test_readable_tier_is_throttled_while_the_tiny_tier_is_not(self):
        """~1 fps readable (memory budget) vs every capture for change/age."""
        clock = _Clock()
        camera = _HonestCamera(size=(1920, 1080))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            with mock.patch.object(capture, "HISTORY_READABLE_INTERVAL", 1.0):
                buffer = _buffer(camera, clock)
                self._run_captures(buffer, camera, clock, 20, step=0.5)
        readable = sum(1 for e in buffer.history if e["readable"] is not None)
        self.assertEqual(len(buffer.history), 20, "tiny tier keeps every capture")
        self.assertGreaterEqual(readable, 9,
                                "readable tier must still exist")
        self.assertLessEqual(readable, 12,
                             "readable tier must stay near 1 fps, got %d"
                             % readable)

    def test_history_expires_once_it_is_outside_the_window(self):
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="4"):
            buffer = _buffer(camera, clock, size=(320, 200))
            self._run_captures(buffer, camera, clock, 6, step=0.5)
            self.assertTrue(buffer.history)
            clock.advance(60)
            self.assertEqual(buffer.history_read(), None)
            self.assertEqual(len(buffer.history), 0)

    def test_entry_count_is_capped_regardless_of_the_clock(self):
        with mock.patch.object(capture, "HISTORY_MAX_ENTRIES", 5):
            clock = _Clock()
            camera = _HonestCamera(size=(320, 200))
            with mock.patch("time.monotonic", clock), _Env(
                    FRIDAY_SCREEN_HISTORY_SECONDS="3600"):
                buffer = _buffer(camera, clock, size=(320, 200))
                self._run_captures(buffer, camera, clock, 25, step=0.1)
            self.assertEqual(len(buffer.history), 5)

    def test_the_entry_cap_is_also_enforced_when_lowered_after_capture(self):
        """The deque's maxlen is fixed at construction; the runtime clamp in
        _expire_history is what makes a lowered cap take effect."""
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="3600"):
            buffer = _buffer(camera, clock, size=(320, 200))
            for _ in range(12):
                camera.queue()
                clock.advance(0.1)
                buffer.refresh(None)
            self.assertEqual(len(buffer.history), 12)
            with mock.patch.object(capture, "HISTORY_MAX_ENTRIES", 3):
                camera.queue()
                clock.advance(0.1)
                self.assertTrue(buffer.refresh(None))
            self.assertEqual(len(buffer.history), 3,
                             "a lowered cap must take effect immediately")
            self.assertEqual(buffer.history[-1]["seq"], 12,
                             "the newest entry must survive, not the oldest")

    def test_zero_seconds_disables_history_cleanly(self):
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="0"):
            self.assertFalse(capture.history_enabled())
            buffer = _buffer(camera, clock, size=(320, 200))
            self._run_captures(buffer, camera, clock, 5)
            self.assertEqual(len(buffer.history), 0)
            self.assertIsNone(buffer.history_read())
            self.assertEqual(buffer.history_since(), [])

    def test_disabling_history_stops_collecting_and_drops_what_was_there(self):
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = _buffer(camera, clock, size=(320, 200))
            self._run_captures(buffer, camera, clock, 3)
            self.assertTrue(buffer.history)
            with _Env(FRIDAY_SCREEN_HISTORY_SECONDS="0"):
                camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
            self.assertEqual(len(buffer.history), 0)


# ---------------------------------------------------------------------------
# 3. Honest reads
# ---------------------------------------------------------------------------
class HistoryReadTests(unittest.TestCase):
    def _seeded(self, count=10, step=0.5, size=(320, 200)):
        clock = _Clock()
        camera = _HonestCamera(size=size)
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = _buffer(camera, clock, size=size)
            for _ in range(count):
                camera.queue()
                clock.advance(step)
                buffer.refresh(None)
        return buffer, clock

    def test_asking_for_a_moment_returns_that_frame_with_its_true_age(self):
        buffer, clock = self._seeded(count=10, step=0.5)
        with mock.patch("time.monotonic", clock):
            result = buffer.history_read(age_seconds=2.0)
        self.assertIsNotNone(result)
        age, entry = result
        # 2.0s back lands on whichever capture was nearest: grid is every 0.5s.
        self.assertLessEqual(abs(age - 2.0), 0.3)
        self.assertIsNotNone(entry["tiny"])
        self.assertIn("mono", entry)
        self.assertIn("wall", entry)

    def test_the_newest_entry_is_the_default_read(self):
        buffer, clock = self._seeded(count=5, step=0.5)
        with mock.patch("time.monotonic", clock):
            age, entry = buffer.history_read()
        self.assertAlmostEqual(age, 0.0, places=3)
        self.assertEqual(entry["seq"], len(buffer.history) - 1)

    def test_a_moment_that_is_gone_returns_nothing_not_the_current_screen(self):
        """The honesty test. A missing moment must not be papered over."""
        buffer, clock = self._seeded(count=5, step=0.5)
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="2"):
            self.assertIsNone(buffer.history_read(age_seconds=120.0))
            clock.advance(600)
            self.assertIsNone(buffer.history_read())
            self.assertIsNone(buffer.history_read(age_seconds=0.0))

    def test_reads_outside_an_explicit_max_age_are_refused(self):
        buffer, clock = self._seeded(count=10, step=0.5)
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            self.assertIsNone(buffer.history_read(age_seconds=4.0, max_age=1.0))

    def test_history_since_is_newest_first_and_reports_ages(self):
        buffer, clock = self._seeded(count=8, step=0.5)
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            rows = buffer.history_since(limit=4)
        self.assertEqual(len(rows), 4)
        ages = [age for age, _entry in rows]
        self.assertEqual(ages, sorted(ages), "must be newest first")
        seqs = [entry["seq"] for _age, entry in rows]
        self.assertEqual(seqs, sorted(seqs, reverse=True))

    def test_history_since_respects_a_since_bound(self):
        buffer, clock = self._seeded(count=10, step=0.5)
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            rows = buffer.history_since(seconds=1.0)
        self.assertTrue(rows)
        for age, _entry in rows:
            self.assertLessEqual(age, 1.0 + 0.5)

    def test_an_empty_buffer_reads_as_nothing(self):
        buffer = capture._FrameBuffer(0, 0)
        self.assertIsNone(buffer.history_read())
        self.assertEqual(buffer.history_since(), [])
        self.assertEqual(buffer.history_bytes(), 0)


# ---------------------------------------------------------------------------
# 4. Per-monitor isolation and topology
# ---------------------------------------------------------------------------
class PerMonitorTests(unittest.TestCase):
    def test_one_monitor_has_no_access_to_another_monitors_history(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            first = _buffer(_HonestCamera(size=(320, 200)), clock, (320, 200))
            second = capture._FrameBuffer(0, 1, (320, 200))
            second.camera = _HonestCamera(size=(320, 200))
            for _ in range(3):
                first.camera.queue()
                second.camera.queue()
                clock.advance(0.5)
                first.refresh(None)
                second.refresh(None)
            self.assertTrue(first.history)
            self.assertTrue(second.history)
            self.assertEqual(first.history_read()[1]["monitor"], 0)
            self.assertEqual(second.history_read()[1]["monitor"], 1)

    def test_an_untouched_monitor_reads_as_having_no_history(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            busy = _buffer(_HonestCamera(size=(320, 200)), clock, (320, 200))
            idle = capture._FrameBuffer(0, 1, (320, 200))
            for _ in range(3):
                busy.camera.queue()
                clock.advance(0.5)
                busy.refresh(None)
            self.assertTrue(busy.history)
            self.assertIsNone(idle.history_read())

    def test_clearing_history_empties_only_the_named_monitor(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            first = _buffer(_HonestCamera(size=(320, 200)), clock, (320, 200))
            second = _buffer(_HonestCamera(size=(320, 200)), clock, (320, 200))
            second.output_idx = 1
            for _ in range(2):
                first.camera.queue()
                second.camera.queue()
                clock.advance(0.5)
                first.refresh(None)
                second.refresh(None)
            first.clear_history("test")
            self.assertEqual(len(first.history), 0)
            self.assertEqual(len(second.history), 2)


# ---------------------------------------------------------------------------
# 5. Memory budget and privacy
# ---------------------------------------------------------------------------
class BudgetTests(unittest.TestCase):
    def test_two_monitors_at_two_fps_for_fifteen_seconds_fit_the_budget(self):
        # Deliberately NOT overriding HISTORY_READABLE_INTERVAL: this asserts the
        # memory cost of the configuration that actually ships.
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"):
            buffers = []
            for index in (0, 1):
                buffer = capture._FrameBuffer(0, index, (1920, 1080))
                buffer.camera = _HonestCamera()
                buffers.append(buffer)
            # 15 s at 2 fps = 30 genuine captures per monitor.
            for step in range(30):
                for buffer in buffers:
                    buffer.camera.queue()
                clock.advance(0.5)
                for buffer in buffers:
                    buffer.refresh(None)
            total = sum(b.history_bytes() for b in buffers)
            tiny = sum(1 for b in buffers for e in b.history if e["tiny"] is not None)
            readable = sum(1 for b in buffers for e in b.history
                           if e["readable"] is not None)
        self.assertEqual(tiny, 60, "tiny tier: 2 monitors x 30 captures")
        self.assertGreaterEqual(readable, 28, "readable tier: ~1 per second")
        self.assertLessEqual(total, 16 * 1024 * 1024,
                             "history must stay inside its ~15 MB budget, "
                             "got %.1f MB" % (total / 1048576.0))
        self.assertGreater(total, 12 * 1024 * 1024,
                           "the readable tier must actually be stored; "
                           "got only %.1f MB" % (total / 1048576.0))

    def test_history_is_memory_only_and_never_written_to_disk(self):
        """No database, no image files, no video - just RAM."""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as sandbox:
            cwd = os.getcwd()
            os.chdir(sandbox)
            try:
                with mock.patch("time.monotonic", clock), _Env(
                        FRIDAY_SCREEN_HISTORY_SECONDS="60"):
                    buffer = _buffer(_HonestCamera(), clock)
                    for _ in range(8):
                        buffer.camera.queue()
                        clock.advance(0.5)
                        buffer.refresh(None)
                    self.assertTrue(buffer.history)
                    buffer.history_read()
                    buffer.history_since()
                    self.assertTrue(buffer.history_bytes() > 0)
                leftovers = os.listdir(sandbox)
            finally:
                os.chdir(cwd)
        self.assertEqual(leftovers, [],
                         "history must leave nothing on disk, found %r"
                         % (leftovers,))

    def test_stats_report_the_configured_window_and_live_counts(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"):
            buffer = _buffer(_HonestCamera(size=(320, 200)), clock, (320, 200))
            for _ in range(4):
                buffer.camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
            self.assertEqual(capture.history_seconds(), 15.0)
            self.assertTrue(capture.history_enabled())
            self.assertEqual(len(buffer.history), 4)
            self.assertGreater(buffer.history_bytes(), 0)


class ServiceLevelApiTests(unittest.TestCase):
    """The wrappers the model actually calls."""

    def _service(self, buffers):
        service = capture.ScreenCaptureService.__new__(
            capture.ScreenCaptureService)
        service._lock = capture.threading.Lock()
        service._buffers = list(buffers)
        return service

    def test_service_reads_are_equivalent_to_the_buffer_reads(self):
        clock = _Clock()
        camera = _HonestCamera(size=(320, 200))
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = _buffer(camera, clock, size=(320, 200))
            for _ in range(6):
                camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
            service = self._service([buffer])
            direct = buffer.history_read(age_seconds=1.5)
            through = service.history_read(0, 1.5)
            self.assertIsNotNone(through)
            self.assertAlmostEqual(through[0], direct[0], places=6)
            self.assertEqual(through[1]["seq"], direct[1]["seq"])
            self.assertEqual(len(service.history_since(0, limit=3)), 3)
            self.assertIsNone(service.history_read(0, 999.0))

    def test_service_reads_are_safe_when_no_monitor_is_bound(self):
        service = self._service([])
        self.assertIsNone(service.history_read(0, 1.0))
        self.assertEqual(service.history_since(0), [])

    def test_stats_expose_the_window_and_the_per_monitor_counts(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"):
            buffers = []
            for index in (0, 1):
                buffer = capture._FrameBuffer(0, index, (320, 200))
                buffer.camera = _HonestCamera(size=(320, 200))
                buffers.append(buffer)
            for _ in range(4):
                for buffer in buffers:
                    buffer.camera.queue()
                clock.advance(0.5)
                for buffer in buffers:
                    buffer.refresh(None)
            stats = self._service(buffers).history_stats()
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["window_seconds"], 15.0)
        self.assertEqual(len(stats["monitors"]), 2)
        self.assertEqual([m["entries"] for m in stats["monitors"]], [4, 4])
        self.assertEqual([m["monitor"] for m in stats["monitors"]], [0, 1])
        self.assertTrue(all(m["bytes"] > 0 for m in stats["monitors"]))

    def test_clearing_by_name_leaves_the_other_monitor_alone(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffers = []
            for index in (0, 1):
                buffer = capture._FrameBuffer(0, index, (320, 200))
                buffer.camera = _HonestCamera(size=(320, 200))
                buffers.append(buffer)
                for _ in range(3):
                    buffer.camera.queue()
                    clock.advance(0.5)
                    buffer.refresh(None)
            service = self._service(buffers)
            cleared = 0
            for buffer in buffers:
                if buffer.output_idx == 1:
                    buffer.clear_history("test")
                    cleared += 1
            self.assertEqual(cleared, 1)
            self.assertEqual(len(buffers[0].history), 3)
            self.assertEqual(len(buffers[1].history), 0)

    def test_history_image_is_none_when_the_moment_is_gone(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = _buffer(_HonestCamera(size=(320, 200)), clock,
                             (320, 200))
            for _ in range(3):
                buffer.camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
            self.assertIsNotNone(buffer.readable_image(age_seconds=0.0))
            self.assertIsNone(buffer.readable_image(age_seconds=500.0))

    def test_a_throttled_request_falls_back_to_a_readable_frame_with_its_true_age(self):
        """The readable tier runs at ~1 fps, so the nearest frame often has no
        readable copy. Falling back is fine as long as the age reported is the
        age of the frame actually returned."""
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"), \
                mock.patch.object(capture, "HISTORY_READABLE_INTERVAL", 1.0):
            buffer = _buffer(_HonestCamera(size=(320, 200)), clock,
                             (320, 200))
            for _ in range(6):
                buffer.camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
            throttled = [e for e in buffer.history if e["readable"] is None]
            self.assertTrue(throttled, "expected some throttled entries")
            target = throttled[-1]
            age_now = buffer.readable_image(
                age_seconds=max(0.0, clock.now - target["mono"]))
            self.assertIsNotNone(age_now,
                                 "should find a readable frame nearby")
            age, image = age_now
            # The reported age must be the TRUE age of a frame that exists -
            # never the age that was asked for.
            real_ages = [round(clock.now - e["mono"], 3) for e in buffer.history]
            self.assertIn(round(age, 3), real_ages,
                          "reported age must match a real frame's age")
            self.assertLessEqual(age, capture.HISTORY_READABLE_INTERVAL + 1e-6,
                                 "fallback must be within one readable interval")
            self.assertEqual(image.size, (320, 200),
                             "a 320x200 monitor must not be upscaled to 960x540")

    def test_history_image_is_none_when_no_readable_frame_survives(self):
        """The tiny tier must never be promoted to stand in for a readable one."""
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"), \
                mock.patch.object(capture, "HISTORY_READABLE_INTERVAL", 100000.0):
            buffer = _buffer(_HonestCamera(size=(320, 200)), clock,
                             (320, 200))
            # The very first capture always gets a readable copy, so pretend one
            # has just been taken to leave the window with none at all.
            buffer._last_readable_mono = clock.now
            for _ in range(3):
                buffer.camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
            self.assertTrue(all(e["readable"] is None for e in buffer.history),
                            "setup: no readable frames expected")
            self.assertIsNone(buffer.readable_image(age_seconds=0.0))
            self.assertIsNone(buffer.readable_image(age_seconds=1.0))


class OnDemandReadTests(unittest.TestCase):
    """OCR of a past frame happens only when asked for, and never lies."""

    def test_a_disabled_history_reports_unavailable_rather_than_the_live_screen(self):
        from source.server import screen_understanding as su

        with _Env(FRIDAY_SCREEN_HISTORY_SECONDS="0"):
            result = su.describe_recent_screen(age_seconds=3.0)
        self.assertFalse(result["ok"])
        self.assertIn("disabled", result["reason"])
        self.assertIsNone(result["age_seconds"])

    def test_a_moment_that_is_gone_is_reported_as_unavailable(self):
        from source.server import screen_understanding as su

        with mock.patch.object(su, "_history_readable_image",
                               return_value=None):
            result = su.describe_recent_screen(age_seconds=90.0)
        self.assertFalse(result["ok"])
        self.assertIn("no retained frame", result["reason"])
        self.assertIn("don't have", result["text"])

    def test_an_available_moment_returns_its_text_and_its_true_age(self):
        from source.server import screen_understanding as su
        from PIL import Image

        image = Image.new("L", (64, 48), 128)
        with mock.patch.object(su, "_history_readable_image",
                               return_value=(2.5, image)), \
                mock.patch.object(su, "_ocr_of_image", return_value="Error 404"):
            result = su.describe_recent_screen(age_seconds=2.0, mode="ocr")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Error 404")
        self.assertAlmostEqual(result["age_seconds"], 2.5, places=2)
        self.assertIsNotNone(result["as_of"])

    def test_an_illegible_frame_is_admitted_rather_than_guessed(self):
        from source.server import screen_understanding as su
        from PIL import Image

        image = Image.new("L", (64, 48), 128)
        with mock.patch.object(su, "_history_readable_image",
                               return_value=(1.0, image)), \
                mock.patch.object(su, "_ocr_of_image", return_value=""):
            result = su.describe_recent_screen(age_seconds=1.0)
        self.assertFalse(result["ok"])
        self.assertIn("no legible text", result["text"])

    def test_ordinary_capture_never_triggers_ocr(self):
        """The whole point of 'on request': no per-capture OCR cost."""
        from source.server import screen_understanding as su

        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="15"), \
                mock.patch.object(su, "_ocr_of_image",
                                  return_value="SHOULD NOT HAPPEN") as spy:
            buffer = _buffer(_HonestCamera(size=(320, 200)), clock,
                             (320, 200))
            for _ in range(10):
                buffer.camera.queue()
                clock.advance(0.5)
                buffer.refresh(None)
        self.assertEqual(spy.call_count, 0,
                         "capturing must never OCR on its own")

    def test_vision_mode_is_marked_uncertain(self):
        from source.server import screen_understanding as su
        from PIL import Image

        image = Image.new("L", (64, 48), 128)
        with mock.patch.object(su, "_history_readable_image",
                               return_value=(1.0, image)), \
                mock.patch.object(su, "_cloud_vision",
                                  # The REAL contract is a 4-tuple (ok, payload,
                                  # provider, error) - see su._cloud_vision and
                                  # its other call sites, which all unpack 4.
                                  # Mocking it as a bare string made this test
                                  # pass while production assigned the tuple to
                                  # `text` and crashed into the except block, so
                                  # mode="vision" history reads always failed.
                                  return_value=(True, "an error dialog",
                                                 "vision_modal", "")):
            result = su.describe_recent_screen(age_seconds=1.0, mode="vision")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "vision")
        self.assertEqual(result["text"], "an error dialog")
        self.assertFalse(result["certain"],
                         "a vision paraphrase must be hedged, not asserted")


class RebindHistoryTests(unittest.TestCase):
    """A rebind must drop only history that is no longer meaningful."""

    class _Outputs:
        def __init__(self, outputs):
            self._outputs = outputs

        def output_info(self):
            if not self._outputs:
                return ""
            return "\n".join(
                "Device[%d] Output[%d]: Res:(%d, %d) Rot:0 Primary:%s"
                % (d, i, s[0], s[1], "True" if p else "False")
                for d, i, s, p in self._outputs)

        def device_info(self):
            return "fake adapter"

        def create(self, device_idx=0, output_idx=None, **kwargs):
            return _HonestCamera()

    def _service(self, buffers):
        service = capture.ScreenCaptureService.__new__(
            capture.ScreenCaptureService)
        service._lock = capture.threading.Lock()
        service._buffers = list(buffers)
        service._signature = None
        service._retry_streak = 0
        service._next_rebuild_at = 0.0
        return service

    def _populated(self, clock, size=(320, 200)):
        buffer = capture._FrameBuffer(0, 0, size)
        buffer.camera = _HonestCamera(size=size)
        for _ in range(4):
            buffer.camera.queue()
            clock.advance(0.5)
            buffer.refresh(None)
        return buffer

    def test_an_unchanged_monitor_keeps_its_history_across_a_rebind(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = self._populated(clock)
            self.assertEqual(len(buffer.history), 4)
            service = self._service([buffer])
            self.assertTrue(service.rebind(self._Outputs(
                [(0, 0, (320, 200), True)])))
            self.assertEqual(len(service._buffers), 1)
            self.assertEqual(len(service._buffers[0].history), 4,
                             "same device, output and size: history survives")

    def test_a_resized_monitor_loses_its_history(self):
        """Frames from the old geometry would be a lie at the new one."""
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = self._populated(clock)
            self.assertTrue(buffer.history)
            service = self._service([buffer])
            self.assertTrue(service.rebind(self._Outputs(
                [(0, 0, (1920, 1080), True)])))
            self.assertEqual(len(service._buffers), 1)
            self.assertEqual(len(service._buffers[0].history), 0)

    def test_a_vanished_monitor_is_gone_and_a_new_one_starts_empty(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = self._populated(clock)
            service = self._service([buffer])
            self.assertTrue(service.rebind(self._Outputs(
                [(0, 1, (320, 200), True)])))
            self.assertEqual([b.output_idx for b in service._buffers], [1])
            self.assertEqual(len(service._buffers[0].history), 0)

    def test_going_down_to_no_outputs_is_reported_as_a_failure(self):
        clock = _Clock()
        with mock.patch("time.monotonic", clock), _Env(
                FRIDAY_SCREEN_HISTORY_SECONDS="60"):
            buffer = self._populated(clock)
            service = self._service([buffer])
            self.assertFalse(service.rebind(self._Outputs([])))
            self.assertEqual(service._buffers, [])


def _bgra(r=0, g=0, b=0, size=(40, 40)):
        """A BGRA frame, which is what dxcam actually hands over (see
        _to_pil's BGR->RGB swap, which this must agree with)."""
        import numpy

        frame = numpy.zeros((size[1], size[0], 4), dtype=numpy.uint8)
        frame[:, :, 0], frame[:, :, 1], frame[:, :, 2] = b, g, r
        frame[:, :, 3] = 255
        return frame


class GrayTierTests(unittest.TestCase):
    """The readable tier must be real luma at a sane size, or OCR is garbage."""

    def test_a_small_monitor_is_not_upscaled(self):
        import numpy

        small = capture._gray_resize(_frame(5, (320, 200)),
                                     capture.HISTORY_READABLE_SIZE)
        self.assertIsNotNone(small)
        self.assertEqual(small.shape, (200, 320),
                         "upscaling would store invented detail")
        self.assertEqual(small.dtype, numpy.uint8)

    def test_a_full_hd_monitor_lands_on_the_readable_size(self):
        import numpy

        big = capture._gray_resize(_frame(5, (1920, 1080)),
                                   capture.HISTORY_READABLE_SIZE)
        self.assertEqual(big.shape, (540, 960))
        self.assertEqual(big.nbytes, 540 * 960)

    def test_grayscale_is_luma_not_a_single_channel(self):
        """Dropping to one channel would make white text on black vanish and
        coloured UIs unreadable."""
        red = capture._gray_resize(_bgra(r=255), (20, 20))
        green = capture._gray_resize(_bgra(g=255), (20, 20))
        blue = capture._gray_resize(_bgra(b=255), (20, 20))
        white = capture._gray_resize(_bgra(255, 255, 255), (20, 20))
        self.assertGreater(int(green.mean()), int(red.mean()))
        self.assertGreater(int(red.mean()), int(blue.mean()))
        self.assertEqual(int(white.mean()), 255)

    def test_grayscale_agrees_with_the_bgr_to_rgb_swapping_helper(self):
        """If the two disagreed, history would show different colours to the
        live pipeline."""
        import numpy

        red = _bgra(r=255, size=(60, 60))
        live = capture._to_pil(red)
        historic = capture._gray_resize(red, (60, 60))
        # Both are luma of the SAME colours, so they must agree closely. If the
        # two read the channel order differently, history would show different
        # colours to the live pipeline.
        live_gray = float(numpy.asarray(live.convert("L")).mean())
        self.assertLess(abs(live_gray - float(historic.mean())), 3.0)

    def test_grayscale_of_a_garbage_input_is_none_not_an_exception(self):
        self.assertIsNone(capture._gray_resize(object(), (20, 20)))


if __name__ == "__main__":
    unittest.main()