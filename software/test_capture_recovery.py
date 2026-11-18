"""DXcam dead-camera / topology-change recovery.

2026-10-02. The first version of the capture service had a condition reading

    elif buffer.camera is None and not buffer.thumb:

so a camera that died while a previous thumbnail was still held would NEVER be
rebuilt. The thumbnail outliving the camera is exactly the normal case, so the
one thing the guard was written for - a dead camera - was the one thing it
skipped. Three more holes sat next to it:

  * `rebind()` replaced `self._buffers` with `[]` when enumeration came back
    empty, and `poll_once` only ever rebuilt from inside its per-buffer loop, so
    a transient full-adapter loss disabled capture until the process restarted;
  * a monitor added, unplugged or re-resized while every camera stayed healthy
    was never noticed, because the output layout was only read at startup;
  * a permanently absent output would rebuild twice a second forever.

These tests drive all four with a fake dxcam; no GPU is involved.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import screen_capture_service as capture

ACCESS_LOST = 0x887A0026
DEVICE_REMOVED = 0x887A0005
OUTPUT_STALE = 0x887A0029
NOT_FOUND = 0x887A0002
WAIT_TIMEOUT = 0x887A0027
UNSUPPORTED = 0x887A0004


class _WinError(Exception):
    """Stands in for _ctypes.COMError / OSError, which carry the HRESULT first."""

    def __init__(self, hresult, text="failed"):
        super().__init__(hresult, text)
        self.winerror = hresult


def _frame(size=(64, 40)):
    import numpy

    return numpy.zeros((size[1], size[0], 4), dtype=numpy.uint8)


class _FakeCamera:
    def __init__(self, behaviour=None):
        self.released = False
        self._behaviour = behaviour or (lambda: _frame())

    def get_latest_frame(self):
        if self._behaviour is not None:
            return self._behaviour()
        return _frame()

    def grab(self, new_frame_only=False):
        return self.get_latest_frame()

    def release(self):
        self.released = True


class _FakeDxcam:
    """Enough of the dxcam surface for the service to bind, poll and fail."""

    def __init__(self, outputs, cameras=None):
        self._outputs = outputs
        self._cameras = cameras if cameras is not None else {}
        self.created = []
        self.released = []

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
        self.created.append((device_idx, output_idx))
        return self._cameras.get((device_idx, output_idx))

    def set_outputs(self, outputs):
        self._outputs = outputs


DUAL = [(0, 0, (1920, 1080), True), (0, 1, (1920, 1080), False)]


class ClassificationTests(unittest.TestCase):
    """The documented HRESULTs mean three different things."""

    def test_access_lost_means_rebuild_that_camera(self):
        kind, code, name = capture._classify(_WinError(ACCESS_LOST))
        self.assertEqual(kind, "rebuild")
        self.assertEqual(name, "ACCESS_LOST")
        self.assertEqual(code, ACCESS_LOST)

    def test_device_removed_means_rebuild(self):
        self.assertEqual(capture._classify(_WinError(DEVICE_REMOVED))[0], "rebuild")

    def test_output_stale_means_the_layout_changed(self):
        kind, _code, name = capture._classify(_WinError(OUTPUT_STALE))
        self.assertEqual(kind, "rebind")
        self.assertEqual(name, "RESTRICT_TO_OUTPUT_STALE")

    def test_out_of_range_output_index_means_rebind(self):
        self.assertEqual(capture._classify(_WinError(NOT_FOUND))[0], "rebind")

    def test_wait_timeout_is_transient_and_must_not_tear_down(self):
        """Tearing a camera down on a late frame becomes a rebuild storm."""
        self.assertEqual(capture._classify(_WinError(WAIT_TIMEOUT))[0], "transient")

    def test_an_unknown_failure_is_treated_as_a_rebuild(self):
        """Recreating a camera is cheap; ignoring a dead one is not."""
        self.assertEqual(capture._classify(RuntimeError("who knows"))[0], "rebuild")

    def test_a_bare_oserror_with_no_code_is_still_handled(self):
        self.assertEqual(capture._classify(OSError("access violation"))[0], "rebuild")

    def test_an_unrelated_hresult_is_not_treated_as_a_rebuild(self):
        self.assertEqual(capture._classify(_WinError(UNSUPPORTED))[0], "fatal")


class BufferTests(unittest.TestCase):
    def test_create_returning_none_is_an_error_not_a_silent_dead_camera(self):
        """dxcam.create() returns None for a bad index rather than raising."""
        buffer = capture._FrameBuffer(0, 7)
        fake = _FakeDxcam([])
        with self.assertRaises(LookupError):
            buffer.open(fake)
        self.assertIsNone(buffer.camera)

    def test_a_dead_camera_is_closed_and_the_reason_recorded(self):
        camera = _FakeCamera(behaviour=lambda: (_ for _ in ()).throw(
            _WinError(ACCESS_LOST, "duplication invalid")))
        buffer = capture._FrameBuffer(0, 0)
        buffer.camera = camera
        self.assertFalse(buffer.refresh(_FakeDxcam(DUAL)))
        self.assertIsNone(buffer.camera, "a dead camera must not stay attached")
        self.assertTrue(camera.released)
        self.assertIn("ACCESS_LOST", buffer.last_failure)

    def test_a_transient_failure_keeps_the_camera_attached(self):
        camera = _FakeCamera(behaviour=lambda: (_ for _ in ()).throw(
            _WinError(WAIT_TIMEOUT, "frame late")))
        buffer = capture._FrameBuffer(0, 0)
        buffer.camera = camera
        self.assertFalse(buffer.refresh(_FakeDxcam(DUAL)))
        self.assertIsNotNone(buffer.camera,
                             "a late frame must not cost us the camera")
        self.assertFalse(camera.released)

    def test_an_empty_frame_is_not_treated_as_failure(self):
        buffer = capture._FrameBuffer(0, 0)
        buffer.camera = _FakeCamera(behaviour=lambda: None)
        self.assertFalse(buffer.refresh(_FakeDxcam(DUAL)))
        self.assertIsNotNone(buffer.camera)
        self.assertEqual(buffer.failures, 0)

    def test_a_good_frame_clears_the_failure_record(self):
        buffer = capture._FrameBuffer(0, 0)
        buffer.note_failure("ACCESS_LOST")
        buffer.camera = _FakeCamera()
        self.assertTrue(buffer.refresh(_FakeDxcam(DUAL)))
        self.assertEqual(buffer.failures, 0)
        self.assertEqual(buffer.last_failure, "")


class DeadCameraRecoveryTests(unittest.TestCase):
    """The bug: `and not buffer.thumb` meant a dead camera was never rebuilt."""

    def setUp(self):
        self.service = capture.ScreenCaptureService(target_fps=0)
        self.dxcam = _FakeDxcam(
            DUAL, cameras={(0, 0): _FakeCamera(), (0, 1): _FakeCamera()})
        patcher = mock.patch.object(capture, "_import_dxcam",
                                    return_value=self.dxcam)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.service.stop)
        self.service.start()

    def _break_monitor(self, index, hresult=ACCESS_LOST):
        """Make a monitor's camera fail on open, and every time after."""
        device, output = self.service._buffers[index].device_idx, \
            self.service._buffers[index].output_idx
        self.dxcam._cameras[(device, output)] = _FakeCamera(
            behaviour=lambda: (_ for _ in ()).throw(
                _WinError(hresult, "duplication invalid")))

    def test_a_dead_camera_is_rebuilt_even_with_a_stale_thumbnail(self):
        monitor = self.service._buffers[1]
        self.assertTrue(monitor.refresh(self.dxcam))
        self.assertIsNotNone(monitor.thumb)

        # Kill it the way DXGI does.
        monitor.camera._behaviour = lambda: (_ for _ in ()).throw(
            _WinError(ACCESS_LOST, "duplication invalid"))
        self.assertFalse(monitor.refresh(self.dxcam))
        self.assertIsNotNone(monitor.thumb, "the old thumb is still there")

        # A working camera is available again on the next attempt.
        monitor.camera = None
        replacement = _FakeCamera()
        self.dxcam._cameras[(0, 1)] = replacement
        self.assertTrue(self.service.poll_once() > 0)
        self.assertEqual(monitor.failures, 0)
        self.assertIsNotNone(monitor.camera)

    def test_a_vanished_output_is_detected_and_the_layout_rebound(self):
        self.dxcam.set_outputs([DUAL[0]])
        self.service.rebind(self.dxcam)
        self.assertEqual(len(self.service._buffers), 1)

        # The monitor comes back.
        self.dxcam.set_outputs(DUAL)
        self.dxcam._cameras[(0, 1)] = _FakeCamera()
        self.service._next_topology_check = 0.0
        self.assertGreaterEqual(self.service.poll_once(), 2)
        self.assertEqual(len(self.service._buffers), 2)

    def test_a_monitor_added_while_all_cameras_are_healthy_is_noticed(self):
        """No error is raised in this case, so only the layout check can catch it."""
        self.dxcam.set_outputs(DUAL + [(0, 2, (2560, 1440), False)])
        self.dxcam._cameras[(0, 2)] = _FakeCamera()
        self.service._next_topology_check = 0.0
        self.service.poll_once()
        self.assertEqual(len(self.service._buffers), 3)

    def test_an_unchanged_layout_does_not_thrash_working_cameras(self):
        # Cameras are created lazily on the first refresh, so warm up first and
        # only then assert that later cycles create nothing new.
        self.service.poll_once()
        created_before = len(self.dxcam.created)
        self.assertEqual(created_before, 2)
        for _ in range(5):
            self.service._next_topology_check = 0.0
            self.service.poll_once()
        self.assertEqual(len(self.dxcam.created), created_before)
        self.assertEqual(len(self.service._buffers), 2)

    def test_health_names_the_degraded_monitor_instead_of_claiming_all_is_well(self):
        """A successful refresh clears the flag; a STUCK one must be reported."""
        self._break_monitor(1)
        self.service.poll_once()
        health = self.service.health()
        self.assertTrue(health["degraded"], health)
        self.assertIn("out1", health["degraded"][0])
        self.assertIn("ACCESS_LOST", health["degraded"][0])

    def test_a_recovered_monitor_is_no_longer_reported_as_degraded(self):
        self._break_monitor(1)
        self.service.poll_once()
        self.assertTrue(self.service.health()["degraded"])

        self.dxcam._cameras[(0, 1)] = _FakeCamera()
        self.service.poll_once()
        self.assertFalse(self.service.health()["degraded"])


class TotalLossTests(unittest.TestCase):
    """A transient full-adapter loss used to disable capture until a restart."""

    def setUp(self):
        self.service = capture.ScreenCaptureService(target_fps=0)
        self.dxcam = _FakeDxcam(DUAL, cameras={(0, 0): _FakeCamera(),
                                               (0, 1): _FakeCamera()})
        patcher = mock.patch.object(capture, "_import_dxcam",
                                    return_value=self.dxcam)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.service.stop)
        self.service.start()

    def test_the_service_comes_back_when_every_output_vanishes_and_returns(self):
        self.dxcam.set_outputs([])
        self.service.rebind(self.dxcam, force=True)
        self.assertEqual(len(self.service._buffers), 0)
        self.assertEqual(self.service.poll_once(), 0)

        self.dxcam.set_outputs(DUAL)
        self.service._next_rebuild_at = 0.0      # skip the backoff for the test
        self.assertGreater(self.service.poll_once(), 0)
        self.assertEqual(len(self.service._buffers), 2)
        self.assertTrue(self.service.health()["enabled"])

    def test_a_permanently_absent_output_backs_off_instead_of_hot_looping(self):
        self.dxcam.set_outputs([])
        self.service.rebind(self.dxcam, force=True)
        self.service._next_rebuild_at = 0.0
        self.service.poll_once()
        attempts_first = len(self.dxcam.created)
        for _ in range(50):
            self.service.poll_once()
        self.assertEqual(len(self.dxcam.created), attempts_first,
                         "backoff must stop the rebuild storm")
        self.assertGreater(self.service._next_rebuild_at, 0.0)

    def test_backoff_delays_grow_and_stay_capped(self):
        service = capture.ScreenCaptureService(target_fps=0)
        service._next_rebuild_at = 0.0
        delays = [service._schedule_retry() for _ in range(12)]
        self.assertEqual(delays[0], capture.REBUILD_BACKOFF_BASE)
        self.assertTrue(all(d <= capture.REBUILD_BACKOFF_CAP for d in delays))
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])),
                        "delay must not shrink while still failing")


class ReaderSafetyTests(unittest.TestCase):
    def test_a_dead_monitor_never_serves_a_frozen_frame(self):
        service = capture.ScreenCaptureService(target_fps=0)
        buffer = capture._FrameBuffer(0, 0)
        service._buffers = [buffer]
        buffer.raw, buffer.pil = _frame(), object()
        buffer.captured_at = 0.0            # never captured
        self.assertIsNone(service.frame(0))

        buffer.captured_at = __import__("time").time() - 10_000
        self.assertIsNone(service.frame(0), "a 10,000s old frame is not fresh")


if __name__ == "__main__":
    unittest.main()