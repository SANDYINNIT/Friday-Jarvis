"""Always-live screen frame buffer, with the existing Pillow path as fallback.

WHY THIS EXISTS
---------------
FRIDAY used to take a brand-new screenshot every single time she was asked
anything about the screen. Measured on this machine:

    PIL.ImageGrab.grab(all_screens=True)   51 ms   (3840x1080, CPU)
    PIL.ImageGrab.grab(bbox=640x360)       50 ms   <- the SAME cost
    dxcam grab()                           2.1 ms  (native 1920x1080)

The region result is the important part: Pillow captures the entire desktop and
then crops, so asking for less pixels saves NOTHING. `_capture_regions()` in
screen_understanding grabs once PER MONITOR, so one screen question on this
dual-monitor setup burned roughly 100 ms of CPU before a single byte reached a
vision model.

DXcam (Desktop Duplication) hands back the frame the GPU already has, so this
service keeps exactly ONE fresh frame per monitor in memory and every reader
takes that instead of re-capturing.

WHAT THIS DOES NOT DO
---------------------
It never sends anything to a vision model. It holds pixels and answers "what is
the newest frame?". Deciding whether a frame is worth *describing* stays with
screen_understanding and, ultimately, with FRIDAY.

FALLBACK / SAFETY
-----------------
Every public function fails soft: if dxcam is missing, fails, or goes stale, the
caller transparently gets `None` and uses the existing Pillow capture. Two
recurring hazards are handled explicitly:

* `DXGI_ERROR_ACCESS_LOST` - the duplication is invalid. This machine has TWO
  virtual display adapters installed (Parsec and DeskIn) which can appear and
  disappear at runtime and invalidate a cached output. We rebuild instead of
  wedging.
* Adapter/output rebinding - dxcam caches output pointers, so after an adapter
  change the camera must be recreated (DXcam issue #141).
"""

from __future__ import annotations

import collections
import os
import threading
import time

from .file_logger import friday_logger as _friday_logger

_log = _friday_logger()

# 2 fps is deliberate: high enough that a frame is never more than ~0.5s stale,
# low enough to be invisible in CPU/GPU usage. Set to 0 to disable the thread
# entirely and fall back to on-demand capture only.
DEFAULT_TARGET_FPS = float(os.getenv("FRIDAY_CAPTURE_FPS", "2"))
# How old a cached frame may be before we stop trusting it and capture fresh.
DEFAULT_MAX_AGE = float(os.getenv("FRIDAY_CAPTURE_MAX_AGE", "5"))
# Ring buffer depth. 1 frame per monitor is ~8.3 MB; this is the whole memory cost.
MAX_BUFFER_LEN = int(os.getenv("FRIDAY_CAPTURE_BUFFER", "2"))
# A frame older than this is treated as dead even if the buffer still holds it.
HARD_STALE_SECONDS = float(os.getenv("FRIDAY_CAPTURE_HARD_STALE", "60"))
# Rebuild backoff. A monitor that is genuinely unplugged must not make us
# release and recreate every camera twice a second forever.
REBUILD_BACKOFF_BASE = float(os.getenv("FRIDAY_CAPTURE_REBUILD_BACKOFF", "1.0"))
REBUILD_BACKOFF_CAP = float(os.getenv("FRIDAY_CAPTURE_REBUILD_BACKOFF_MAX", "15"))
# How often to re-check the monitor layout even when nothing is failing, so a
# monitor plugged in (or a resolution changed) is noticed without an error first.
TOPOLOGY_POLL_SECONDS = float(os.getenv("FRIDAY_CAPTURE_TOPOLOGY_POLL", "15"))
# Only complain once the retry delay is long enough to be worth a log line, so a
# monitor being unplugged does not spam the log on its way up the backoff curve.
_WARN_BACKOFF_SECONDS = float(os.getenv("FRIDAY_CAPTURE_BACKOFF_WARN", "5"))


def capture_backoff_warn_threshold():
    return _WARN_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# ROUND ZB-L2: short-term, memory-only screen history.
#
# This answers "what was on the screen a few seconds ago?", which the single
# newest frame structurally cannot: by the time Sir asks, the pixels that
# mattered are gone. It is deliberately NOT a recording:
#   * memory only - never written to disk, no database, no video file
#   * bounded twice - by seconds AND by entry count
#   * expires automatically, so it disappears on its own
#   * never injected into a prompt wholesale; the model asks for it deliberately
# Two tiers, because those questions need very different amounts of detail:
#   tiny    64x36 grayscale, every capture: did something change, when, where
#   readable 960x540 grayscale, throttled: OCR / compare recent UI states
# Memory, at 2 fps capture and a 15 s window, two monitors:
#   tiny      2 x 30 x 2.3 KB              ~= 0.14 MB
#   readable  2 x 15 x 506 KB (1 per sec)  ~= 15.2 MB
# ~15.4 MB total, which is the budget this was designed to. Full-resolution RGB
# is never stored - at 1920x1080 it would be 6 MB per frame, i.e. ~2.7 GB.
# ---------------------------------------------------------------------------
# Same 64x36 grayscale tile the change detector already builds, reused as the
# tiny history tier (see _CHANGE_SIZE below for the detection copy).
HISTORY_TINY_SIZE = (64, 36)
HISTORY_READABLE_SIZE = (960, 540)
# Readable frames are throttled to ~1 fps even though capture runs at 2 fps.
# They exist to be OCR'd; twice per second adds nothing a reader can use and
# doubles the memory. This is what keeps the readable tier inside its budget.
HISTORY_READABLE_INTERVAL = float(
    os.getenv("FRIDAY_SCREEN_HISTORY_READABLE_INTERVAL", "1.0"))
# Hard cap regardless of the time window, so a clock problem cannot grow forever.
HISTORY_MAX_ENTRIES = int(os.getenv("FRIDAY_SCREEN_HISTORY_ENTRIES", "64"))
# What she says when the requested moment is gone. Never a guess.
HISTORY_UNAVAILABLE = (
    "I don't have that part of my recent visual memory anymore.")


def history_seconds():
    """Configured history window. 0 (or less) disables history cleanly."""
    try:
        return float(os.getenv("FRIDAY_SCREEN_HISTORY_SECONDS", "15"))
    except (TypeError, ValueError):
        return 15.0


def history_enabled():
    return history_seconds() > 0

# Change detection runs on a heavily downscaled grayscale frame: 64*36 = 2.3 KB.
_CHANGE_SIZE = (64, 36)
# Mean absolute difference (0..255) above which we call the screen "changed".
CHANGE_THRESHOLD = float(os.getenv("FRIDAY_CAPTURE_CHANGE_THRESHOLD", "1.5"))


class CaptureHealth:
    """Honest, self-reported state - FRIDAY must never claim it can see the
    screen when it cannot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.reason = "not started"
        self.devices = []
        self.outputs = []
        self.monitors = []
        self.running = False
        self.rebuilds = 0
        self.last_error = ""
        self.last_frame_at = 0.0
        self.frames_seen = 0
        self.degraded = []

    def snapshot(self):
        with self.lock:
            return {
                "enabled": self.enabled,
                "running": self.running,
                "reason": self.reason,
                "devices": list(self.devices),
                "outputs": list(self.outputs),
                "monitors": list(self.monitors),
                "rebuilds": self.rebuilds,
                "last_error": self.last_error,
                "degraded": list(self.degraded),
                "last_frame_age": (round(time.time() - self.last_frame_at, 2)
                                   if self.last_frame_at else None),
                "frames_seen": self.frames_seen,
                "target_fps": DEFAULT_TARGET_FPS,
            }


HEALTH = CaptureHealth()


def _import_dxcam():
    try:
        import dxcam  # noqa: WPS433 - optional by design
        return dxcam
    except Exception as error:
        _log.info("dxcam unavailable (%s); using the Pillow capture path",
                  type(error).__name__)
        return None


# --------------------------------------------------------------------------
# DXGI error classification
# --------------------------------------------------------------------------
# HRESULTs are the documented values from WinError.h
# (https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-error).
# They matter because they say three different things:
#   * rebuild this camera  - the duplication/device is invalid
#   * the topology changed - our output index no longer means what it did
#   * just try again      - nothing is wrong, the frame was simply late
_DXGI_ACCESS_LOST = 0x887A0026        # "desktop duplication interface is invalid"
_DXGI_DEVICE_REMOVED = 0x887A0005     # "destroy and recreate the device"
_DXGI_DEVICE_RESET = 0x887A0007       # "destroy and recreate the device"
_DXGI_DEVICE_HUNG = 0x887A0006
_DXGI_OUTPUT_STALE = 0x887A0029       # "the output is now disconnected or changed"
_DXGI_NOT_FOUND = 0x887A0002          # enumerated ordinal is out of range
_DXGI_ACCESS_DENIED = 0x887A002B
_DXGI_SESSION_DISCONNECTED = 0x887A0028

# Rebuild the camera, then retry.
REBUILD_CODES = frozenset({
    _DXGI_ACCESS_LOST, _DXGI_DEVICE_REMOVED, _DXGI_DEVICE_RESET,
    _DXGI_DEVICE_HUNG, _DXGI_OUTPUT_STALE,
})
# The monitor layout itself changed: re-enumerate and rebind every output.
REBIND_CODES = frozenset({_DXGI_OUTPUT_STALE, _DXGI_NOT_FOUND})
# Transient: do NOT tear the camera down, just report no frame this cycle.
TRANSIENT_CODES = frozenset({0x887A0027})   # WAIT_TIMEOUT

_CODE_NAMES = {
    _DXGI_ACCESS_LOST: "ACCESS_LOST",
    _DXGI_DEVICE_REMOVED: "DEVICE_REMOVED",
    _DXGI_DEVICE_RESET: "DEVICE_RESET",
    _DXGI_DEVICE_HUNG: "DEVICE_HUNG",
    _DXGI_OUTPUT_STALE: "RESTRICT_TO_OUTPUT_STALE",
    _DXGI_NOT_FOUND: "NOT_FOUND",
    _DXGI_ACCESS_DENIED: "ACCESS_DENIED",
    _DXGI_SESSION_DISCONNECTED: "SESSION_DISCONNECTED",
    0x887A0027: "WAIT_TIMEOUT",
}


def _signed(hresult):
    return hresult - 0x100000000 if hresult >= 0x80000000 else hresult


def _classify(error):
    """('rebuild'|'rebind'|'transient'|'fatal', hresult, name).

    Windows raises this as a COMError/OSError carrying the HRESULT as
    `args[0]`, but a ctypes access violation surfaces as a plain OSError with no
    code at all - so an uncoded failure is treated as a rebuild, which is the
    safe direction: recreating a camera is cheap and always safe, whereas
    ignoring a dead camera is how you serve a frozen frame forever.
    """
    code = getattr(error, "winerror", None)
    if code is None:
        args = getattr(error, "args", None) or ()
        if args and isinstance(args[0], int):
            code = args[0]
    if code is None or code == 0:
        return "rebuild", 0, type(error).__name__
    hresult = code & 0xFFFFFFFF
    if hresult in TRANSIENT_CODES:
        return "transient", hresult, _CODE_NAMES.get(hresult, "0x%X" % hresult)
    if hresult in REBIND_CODES:
        return "rebind", hresult, _CODE_NAMES.get(hresult, "0x%X" % hresult)
    if hresult in REBUILD_CODES:
        return "rebuild", hresult, _CODE_NAMES.get(hresult, "0x%X" % hresult)
    return "fatal", hresult, _CODE_NAMES.get(hresult, "0x%X" % hresult)


class _Output(tuple):
    """Keep the existing four-field iterable contract, with evidence metadata."""
    def __new__(cls, device, index, size, primary, rotation=0, name=None, bounds=None):
        value = super().__new__(cls, (device, index, size, primary))
        value.geometry = (rotation, name, bounds)
        return value


def _enumerated_outputs(dxcam):
    """(device_idx, output_idx, size, primary) for every DXGI output.

    Returns an empty list rather than raising: capture must never be the reason
    a turn fails.
    """
    out = []
    try:
        raw = dxcam.output_info()
    except Exception:
        return out
    if isinstance(raw, str):
        raw = raw.splitlines()
    for line in raw or []:
        # 'Device[0] Output[1]: Res:(1920, 1080) Rot:0 Primary:False'
        try:
            dev = int(line.split("Device[")[1].split("]")[0])
            idx = int(line.split("Output[")[1].split("]")[0])
            res = line.split("Res:(")[1].split(")")[0].replace(",", "x").strip()
            width, height = res.split("x")
            primary = "Primary:True" in line
            rotation = int(line.split("Rot:")[1].split()[0]) if "Rot:" in line else None
            name = bounds = None
            # Optional adapter for the installed DXcam factory. Missing or
            # changed internals mean unbound evidence, never index guessing.
            try:
                output = getattr(dxcam, "__factory").outputs[dev][idx]
                output.update_desc()
                desc = output.desc
                name = str(desc.DeviceName)
                area = desc.DesktopCoordinates
                bounds = (area.left, area.top, area.right-area.left, area.bottom-area.top)
                rotation = output.rotation_angle
            except Exception:
                pass
            out.append(_Output(dev, idx, (int(width), int(height)), primary,
                               rotation, name, bounds))
        except Exception:
            continue
    return out


def _to_pil(array):
    """numpy BGRA -> PIL RGB. Imported lazily so the module stays cheap."""
    try:
        import numpy as np
        from PIL import Image

        data = np.asarray(array)
        if data.ndim == 3 and data.shape[2] == 4:
            data = data[:, :, :3][:, :, ::-1]  # BGR -> RGB
        return Image.fromarray(np.ascontiguousarray(data))
    except Exception as error:
        _log.warning("frame conversion failed: %s", type(error).__name__)
        return None


def _thumb_from_raw(array):
    """64x36 grayscale thumbnail straight from the raw BGRA buffer."""
    try:
        import numpy as np

        data = np.asarray(array)
        if data.ndim == 3 and data.shape[2] >= 3:
            data = data[:, :, :3]
        return np.asarray(
            data[:: max(1, data.shape[0] // 36), :: max(1, data.shape[1] // 64)],
            dtype=np.uint8,
        )
    except Exception:
        return None


def _gray_resize(array, size):
    """Grayscale frame downscaled to `size` from a raw BGRA buffer.

    Returns a compact uint8 (H, W) array, or None.

    Done through PIL rather than with numpy colour maths on purpose. The first
    version of this weighted the channels by hand, and because numpy promotes
    uint8 to float64 for the arithmetic that produced a ~16 MB float temporary
    per call, per monitor, per second - the measured RSS cost of history was
    600 MB instead of the 15 MB the buffers actually occupy. PIL does the same
    luma conversion and the resample in C, allocates a fraction of that, and
    `convert("L")` is the standard BT.601 luma transform anyway.
    """
    try:
        import numpy as np
        from PIL import Image

        data = np.asarray(array)
        if data.ndim == 3 and data.shape[2] >= 4:
            # frombuffer on a 4-channel uint8 buffer is a view, not a copy.
            data = np.ascontiguousarray(data)
            image = Image.frombuffer("RGBA", (data.shape[1], data.shape[0]),
                                     data, "raw", "BGRA", 0, 1)
        elif data.ndim == 3 and data.shape[2] >= 3:
            image = Image.fromarray(np.ascontiguousarray(data[:, :, :3]))
        else:
            return None
        width, height = size
        # Never upscale: a 960x540 target on a 320x200 monitor would store
        # 506 KB of invented detail instead of 64 KB of real pixels.
        width = max(1, min(int(width), int(image.size[0])))
        height = max(1, min(int(height), int(image.size[1])))
        if (width, height) == tuple(image.size):
            small = image.convert("L")
        else:
            small = image.convert("L").resize((width, height), Image.BILINEAR)
        return np.ascontiguousarray(np.asarray(small), dtype=np.uint8)
    except Exception:
        return None


class _FrameBuffer:
    """One camera, one monitor, newest frame only."""

    def __init__(self, device_idx, output_idx, size=None):
        self.device_idx = device_idx
        self.output_idx = output_idx
        self.size = size
        self.geometry = None
        self.camera = None
        self.raw = None          # newest BGRA frame, converted on demand
        self.pil = None          # memoised PIL view of `raw`
        self.captured_at = 0.0   # wall clock, labelling only
        self.captured_mono = 0.0  # monotonic, used for every age calculation
        self.thumb = None
        self.failures = 0        # consecutive cycles with no frame
        self.last_failure = ""   # human-readable reason, surfaced in health()
        # ROUND ZB-L2: short-term, memory-only history for THIS monitor.
        self.history = collections.deque(maxlen=HISTORY_MAX_ENTRIES)
        self._history_seq = 0
        self._last_readable_mono = None

    def age(self):
        """Seconds since the last GENUINE capture, or None if never captured.

        ROUND ZB-L2: computed from the monotonic stamp so a wall-clock jump
        cannot make a stale frame look brand new (or vice versa).
        """
        if not self.captured_mono:
            return None
        return max(0.0, time.monotonic() - self.captured_mono)

    def wall_age(self):
        """Age measured against the wall clock, for labelling only."""
        return (time.time() - self.captured_at) if self.captured_at else None

    def note_success(self):
        self.failures = 0
        self.last_failure = ""

    def note_failure(self, why):
        self.failures += 1
        self.last_failure = why

    # -- short-term history (ROUND ZB-L2) ---------------------------------
    def _expire_history(self, now_mono):
        """Drop anything older than the configured window. Memory only."""
        seconds = history_seconds()
        if seconds <= 0:
            self.history.clear()
            return
        cutoff = now_mono - seconds
        while self.history and self.history[0]["mono"] < cutoff:
            self.history.popleft()
        while len(self.history) > HISTORY_MAX_ENTRIES:
            self.history.popleft()

    def _push_history(self, frame, thumb):
        """Record one GENUINE capture. Called only from refresh(), after a new
        frame has actually arrived - a cached frame must never enter history,
        or the history would be a wall of duplicates pretending to be a
        timeline."""
        seconds = history_seconds()
        if seconds <= 0:
            if self.history:
                self.history.clear()
            return
        now_mono = time.monotonic()
        entry = {
            "monitor": self.output_idx,
            "device": self.device_idx,
            "seq": self._history_seq,
            "wall": time.time(),
            "mono": now_mono,
            "tiny": thumb,
            "readable": None,
            "readable_w": 0,
            "readable_h": 0,
        }
        last = self._last_readable_mono
        if last is None or (now_mono - last) >= HISTORY_READABLE_INTERVAL:
            gray = _gray_resize(frame, HISTORY_READABLE_SIZE)
            if gray is not None:
                entry["readable"] = gray
                entry["readable_w"] = int(gray.shape[1])
                entry["readable_h"] = int(gray.shape[0])
                self._last_readable_mono = now_mono
        self._history_seq += 1
        self.history.append(entry)
        self._expire_history(now_mono)

    def clear_history(self, why=""):
        """Wipe this monitor's history only. Used when its geometry changes:
        frames captured at one resolution are meaningless at another."""
        self.history.clear()
        self._last_readable_mono = None
        if why:
            _log.info("screen history cleared for monitor %s output %s (%s)",
                      self.device_idx, self.output_idx, why)

    def history_read(self, age_seconds=None, *, max_age=None, now_mono=None):
        """(age_seconds, entry) for the frame nearest `age_seconds`, or None.

        `age_seconds` is measured back from now. Omit it for the newest entry.

        Returns None when nothing is in range, and callers must report that
        honestly rather than substituting the current screen, which would be a
        different thing entirely. In particular, a request for a moment that
        has already fallen out of the window is NOT quietly answered with the
        oldest frame still held - "the oldest thing I remember" is not what was
        asked for, and passing it off as such is the same mistake as answering
        with the live screen.
        """
        limit = history_seconds()
        if limit <= 0:
            return None
        if not self.history:
            return None
        now_mono = time.monotonic() if now_mono is None else now_mono
        self._expire_history(now_mono)
        if not self.history:
            return None
        if age_seconds is None:
            entry = self.history[-1]
        else:
            try:
                requested = float(age_seconds)
            except (TypeError, ValueError):
                return None
            if requested < 0:
                requested = 0.0
            # Outside the retained window: we simply do not have that moment.
            if requested > limit:
                return None
            wanted = now_mono - requested
            entry = min(self.history,
                        key=lambda e: abs(e["mono"] - wanted))
        age = max(0.0, now_mono - entry["mono"])
        if age > limit:
            return None
        if max_age is not None and age > float(max_age):
            return None
        return (age, entry)

    def history_since(self, seconds=None, *, limit=8, now_mono=None):
        """Newest-first entries within the window, each with its own age."""
        if not self.history:
            return []
        now_mono = time.monotonic() if now_mono is None else now_mono
        self._expire_history(now_mono)
        out = []
        for entry in reversed(self.history):
            age = max(0.0, now_mono - entry["mono"])
            if seconds is not None and age > float(seconds):
                break
            out.append((age, entry))
            if len(out) >= max(1, int(limit or 1)):
                break
        return out

    def history_bytes(self):
        """Rough resident cost of this monitor's history, in bytes."""
        total = 0
        for entry in self.history:
            if entry["tiny"] is not None:
                total += int(getattr(entry["tiny"], "nbytes", 0) or 0)
            if entry["readable"] is not None:
                total += int(getattr(entry["readable"], "nbytes", 0) or 0)
        return total

    def readable_image(self, age_seconds=None, *, max_age=None, now_mono=None):
        """(age_seconds, PIL 'L' image) for the readable tier, or None.

        The readable tier is throttled to ~1 fps while capture runs at 2 fps,
        so the frame NEAREST the requested moment often has no readable copy at
        all. Answering None there made "what was there a moment ago?" fail most
        of the time, so we fall back to the nearest frame that does - at most
        about a second away - and report THAT frame's real age. The fallback is
        therefore honest: the caller is told exactly how old the picture is, and
        the tiny tier above is never promoted to stand in for a readable one.

        Returns None when the moment is outside the window, or when nothing
        readable survives in it.
        """
        found = self.history_read(age_seconds, max_age=max_age,
                                  now_mono=now_mono)
        if found is None:
            return None
        age, entry = found
        if entry.get("readable") is not None:
            return self._as_image(age, entry)
        reference = time.monotonic() if now_mono is None else now_mono
        try:
            wanted = reference - float(age_seconds or 0.0)
        except (TypeError, ValueError):
            return None
        best = None
        for candidate in self.history:
            if candidate.get("readable") is None:
                continue
            delta = abs(candidate["mono"] - wanted)
            if best is None or delta < best[0]:
                best = (delta, candidate)
        if best is None:
            return None
        entry = best[1]
        age = max(0.0, reference - entry["mono"])
        limit = history_seconds()
        if limit <= 0 or age > limit:
            return None
        if max_age is not None and age > float(max_age):
            return None
        return self._as_image(age, entry)

    def _as_image(self, age, entry):
        gray = entry.get("readable")
        if gray is None:
            return None
        try:
            from PIL import Image

            return (age, Image.fromarray(gray))
        except Exception:
            return None

    def open(self, dxcam):
        self.close()
        self.camera = dxcam.create(
            device_idx=self.device_idx,
            output_idx=self.output_idx,
            output_color="BGRA",
            max_buffer_len=MAX_BUFFER_LEN,
        )
        # dxcam.create() returns None for an out-of-range device/output index
        # rather than raising, so a vanished output must be handled here or the
        # buffer silently stops updating while still looking healthy.
        if self.camera is None:
            raise LookupError("dxcam.create returned None for device %s output %s"
                              % (self.device_idx, self.output_idx))
        return self.camera

    def refresh(self, dxcam):
        """Pull the newest GENUINE frame into memory.

        Returns True if we got a genuinely new one. On failure the camera is
        closed and the reason recorded, so the caller can rebuild; it never
        leaves a dead camera attached pretending to be live.

        ROUND ZB-L2 (2026-10-07) - CAPTURE TIMESTAMP TRUTHFULNESS.
        This used to read `get_latest_frame()`, which returns whatever dxcam
        currently has cached, and then stamped `captured_at = time.time()`.
        With no new frame from the compositor that re-stamps the SAME cached
        pixels as if they had just been photographed, so `frame()`'s age check
        always answered "fresh" even for a screen that had not changed in
        minutes - and every downstream freshness claim inherited the lie.

        Now it asks for a NEW frame only. `grab(new_frame_only=True)` returns
        None when nothing new arrived, and in that case NOTHING is replaced:
        not the frame, not the timestamp, not the change thumbnail, and no
        history entry is pushed. The last genuinely captured frame - and its
        true age - are preserved, which is what makes the age meaningful.
        The old `grab(new_frame_only=False)` fallback is gone: it was the same
        cached frame by another name, and it is precisely what made the
        timestamp untrue.
        """
        try:
            if self.camera is None:
                self.open(dxcam)
            frame = self.camera.grab(new_frame_only=True)
            if frame is None or getattr(frame, "size", 0) == 0:
                # A perfectly healthy camera can legitimately have nothing new
                # yet. That is NOT a failure - do not tear it down, and do not
                # pretend the previous frame is a new one.
                return False
            # 2026-10-02: keep the RAW frame and convert lazily. Measured: doing
            # the BGRA->RGB numpy conversion for both monitors at 2 fps cost
            # 8.6% of a core continuously, almost all of it that conversion.
            # Holding the raw array and converting only when someone actually
            # asks for an image cuts the idle cost to almost nothing, and a
            # cached read stays instant because the PIL image is memoised.
            self.raw = frame
            self.captured_at = time.time()
            # Monotonic companion: wall-clock can jump (NTP, DST, a manual clock
            # change) and an age computed from it can go NEGATIVE, which would
            # make a stale frame look brand new. Age maths uses the monotonic
            # stamp; the wall-clock one exists only to label history.
            self.captured_mono = time.monotonic()
            self.pil = None
            if self.size is None:
                try:
                    self.size = (int(frame.shape[1]), int(frame.shape[0]))
                except Exception:
                    pass
            try:
                # Change detection on a 64x36 grayscale thumb (~2.3 KB), taken
                # straight from the raw buffer so it costs almost nothing.
                self.thumb = _thumb_from_raw(frame)
            except Exception:
                self.thumb = None
            self._push_history(frame, self.thumb)
            self.note_success()
            return True
        except Exception as error:
            kind, hresult, name = _classify(error)
            self.note_failure("%s (0x%08X)" % (name, hresult))
            with HEALTH.lock:
                HEALTH.last_error = "%s: %s" % (type(error).__name__,
                                                str(error)[:120])
            # transient == "the frame was late". Keep the camera; a teardown here
            # would turn a one-off hiccup into a rebuild storm.
            if kind != "transient":
                self.close()
            return False

    def close(self):
        camera, self.camera = self.camera, None
        if camera is not None:
            try:
                camera.release()
            except Exception:
                pass


class ScreenCaptureService:
    """Keeps one fresh frame per monitor in memory. Never raises."""

    def __init__(self, target_fps=DEFAULT_TARGET_FPS):
        self.target_fps = max(0.0, float(target_fps))
        self._lock = threading.RLock()
        self._buffers = []
        self._thread = None
        self._stop = threading.Event()
        self._signature = None      # monitor layout we are currently bound to
        self._retry_streak = 0      # consecutive failed rebinds (backoff)
        self._next_rebuild_at = 0.0
        self._next_topology_check = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        with self._lock:
            if self._thread is not None:
                return HEALTH.snapshot()
            dxcam = _import_dxcam()
            if dxcam is None:
                with HEALTH.lock:
                    HEALTH.enabled = False
                    HEALTH.reason = "dxcam not installed - Pillow fallback"
                return HEALTH.snapshot()
            outputs = _enumerated_outputs(dxcam)
            with HEALTH.lock:
                HEALTH.outputs = ["%s" % (o,) for o in outputs]
                try:
                    HEALTH.devices = [dxcam.device_info()]
                except Exception:
                    HEALTH.devices = []
            if not outputs:
                with HEALTH.lock:
                    HEALTH.enabled = False
                    HEALTH.reason = "no DXGI outputs found - Pillow fallback"
                return HEALTH.snapshot()
            self.rebind(dxcam)
            if self.target_fps > 0:
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run, name="friday-capture", daemon=True)
                self._thread.start()
            with HEALTH.lock:
                HEALTH.enabled = True
                HEALTH.running = self._thread is not None
                HEALTH.reason = ("capturing %d monitor(s) at %g fps"
                                 % (len(self._buffers), self.target_fps))
            return HEALTH.snapshot()

    def stop(self):
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
            for buffer in self._buffers:
                buffer.close()
            self._buffers = []
            # Forget the bound layout so a later start()/rebind() re-enumerates
            # instead of assuming the topology is still what it was.
            self._signature = None
            self._retry_streak = 0
            self._next_rebuild_at = 0.0
        if thread is not None:
            thread.join(timeout=2.0)
        with HEALTH.lock:
            HEALTH.running = False

    def _run(self):
        interval = 1.0 / self.target_fps if self.target_fps > 0 else 1.0
        while not self._stop.wait(interval):
            self.poll_once()

    # -- capture -----------------------------------------------------------
    def _output_signature(self, outputs):
        """Identity of the monitor layout.

        Used to notice that the topology changed even when every camera is still
        happily producing frames: a monitor added, removed, or re-resized changes
        this tuple, and dxcam's cached output pointers are then wrong.
        """
        return tuple(sorted((int(o[0]), int(o[1]), tuple(o[2] or ()), bool(o[3]),
                             repr(getattr(o, "geometry", None))) for o in outputs))

    def _due_for_retry(self, now):
        """Backoff so a permanently absent output cannot become a hot loop.

        Without this, `poll_once` at 2 fps would release and recreate every camera
        twice a second for as long as a monitor stayed unplugged.
        """
        if self._next_rebuild_at and now < self._next_rebuild_at:
            return False
        return True

    def _schedule_retry(self):
        """Exponential-ish backoff, capped at MIN_REBUILD_INTERVAL."""
        delay = min(REBUILD_BACKOFF_CAP,
                    REBUILD_BACKOFF_BASE * (2 ** min(self._retry_streak, 5)))
        self._retry_streak += 1
        self._next_rebuild_at = time.time() + delay
        return delay

    def poll_once(self):
        """Refresh every buffer once, and heal whatever is broken.

        Three distinct recoveries, because they have different causes:

        * a camera that died          -> recreate that one camera
        * the output layout changed   -> re-enumerate and rebind every output
        * no outputs at all           -> keep polling so the service comes BACK
                                         when the adapter returns (previously a
                                         transient full-adapter loss disabled
                                         capture permanently until restart)
        """
        dxcam = _import_dxcam()
        if dxcam is None:
            return 0
        now = time.time()
        got = 0
        dead_cameras = []
        needs_rebind = False
        with self._lock:
            buffers = list(self._buffers)

        if not buffers:
            # Zero outputs. Try to come back rather than staying dead - and
            # respect the backoff, otherwise a detached adapter means
            # re-enumerating twice a second for as long as it stays away.
            if not self._due_for_retry(now):
                return 0
            if self.rebind(dxcam, force=True):
                self._retry_streak = 0
                self._next_rebuild_at = 0.0
                with HEALTH.lock:
                    HEALTH.reason = "recovered %d monitor(s)" % len(self._buffers)
                # Fall through and capture in the SAME cycle, so recovery is not
                # a 500ms hole in what FRIDAY can see.
                with self._lock:
                    buffers = list(self._buffers)
            else:
                delay = self._schedule_retry()
                if delay >= capture_backoff_warn_threshold():
                    _log.warning("screen capture: no DXGI outputs; retrying"
                                 " in %.1fs", delay)
                return 0

        # Even when everything is healthy, the layout can change: a monitor added,
        # unplugged or re-resized. dxcam caches output pointers, so those become
        # wrong without any error being raised.
        if now >= self._next_topology_check:
            self._next_topology_check = now + TOPOLOGY_POLL_SECONDS
            current = _enumerated_outputs(dxcam)
            if self._output_signature(current) != self._signature:
                _log.info("screen capture: monitor layout changed, rebinding")
                self._retry_streak = 0
                self._next_rebuild_at = 0.0
                self.rebind(dxcam, force=True)
                with self._lock:
                    buffers = list(self._buffers)

        for buffer in buffers:
            if buffer.refresh(dxcam):
                got += 1
                continue
            if buffer.camera is None:
                # refresh() closed it (or it never opened): the camera itself is
                # the problem. Check whether the output still exists at all.
                dead_cameras.append(buffer)
                if buffer.failures >= 1 and not self._output_exists(dxcam, buffer):
                    needs_rebind = True

        # A camera that cannot be reopened is usually an output that moved or
        # vanished, so re-enumerate rather than retry a dead index forever.
        if dead_cameras and not needs_rebind and self._due_for_retry(now):
            needs_rebind = True

        if needs_rebind:
            delay = None
            if self._due_for_retry(now):
                delay = self._schedule_retry()
            recovered = self.rebind(dxcam)
            if recovered:
                self._retry_streak = 0
                self._next_rebuild_at = 0.0
                with HEALTH.lock:
                    HEALTH.reason = "rebuilt %d output(s)" % len(self._buffers)
            elif delay is not None:
                _log.warning("screen capture: no DXGI outputs to rebind to; "
                             "retrying in %.1fs (%s)",
                             delay, dead_cameras[0].last_failure or "unknown")

        with HEALTH.lock:
            # Honest per-monitor state, so FRIDAY can say *which* monitor she lost
            # rather than claiming to see the screen.
            HEALTH.degraded = ["dev%d/out%d: %s" % (b.device_idx, b.output_idx,
                                                    b.last_failure or "no frame")
                               for b in buffers if b.last_failure]

        if got:
            with HEALTH.lock:
                HEALTH.frames_seen += got
                HEALTH.last_frame_at = time.time()
                HEALTH.running = True
        return got

    def _output_exists(self, dxcam, buffer):
        try:
            outputs = _enumerated_outputs(dxcam)
        except Exception:
            return True      # cannot tell: assume yes, let the open retry
        return any(int(d) == buffer.device_idx and int(i) == buffer.output_idx
                   for d, i, _s, _p in outputs)

    def rebind(self, dxcam=None, force=False):
        """Re-enumerate outputs and rebind every camera (topology changed).

        Without `force` this is a no-op when the layout is unchanged, so the
        normal path never thrashes working cameras.
        """
        dxcam = dxcam or _import_dxcam()
        if dxcam is None:
            return False
        outputs = _enumerated_outputs(dxcam)
        signature = self._output_signature(outputs)
        with self._lock:
            if not force and signature == self._signature and self._buffers:
                return False
            # ROUND ZB-L2: a rebind is not a reason to forget everything. An
            # output that is still the same device, same output index and SAME
            # SIZE keeps its recent history - its frames are still meaningful.
            # Anything that actually changed (new monitor, re-indexed output,
            # different resolution) gets a clean slate, because a frame
            # captured at the old geometry would be a lie at the new one.
            carried = {}
            for buffer in self._buffers:
                carried[(buffer.device_idx, buffer.output_idx)] = buffer
            rebuilt = []
            kept = 0
            for output in outputs:
                device, index, size, _primary = output
                geometry = getattr(output, "geometry", None)
                old = carried.pop((device, index), None)
                if old is None:
                    new = _FrameBuffer(device, index, size)
                    new.geometry = geometry
                    rebuilt.append(new)
                    continue
                old_size = old.size
                same_geometry = (old_size == size and
                                 (old.geometry == geometry or
                                  old.geometry is None and geometry == (0, None, None)))
                if same_geometry:
                    rebuilt.append(old)
                    old.geometry = geometry
                    old.close()
                    kept += 1
                else:
                    new = _FrameBuffer(device, index, size)
                    new.geometry = geometry
                    rebuilt.append(new)
                    old.close()
            for orphan in carried.values():
                orphan.close()
            self._buffers = rebuilt
            self._signature = signature
        with HEALTH.lock:
            HEALTH.outputs = ["%s" % (o,) for o in outputs]
            HEALTH.rebuilds += 1
            HEALTH.enabled = bool(outputs)
            HEALTH.degraded = []
            if not outputs:
                HEALTH.reason = "no DXGI outputs - Pillow fallback, retrying"
            else:
                HEALTH.monitors = ["dev%d/out%d %s%s" % (
                    d, i, "%dx%d" % tuple(s) if s else "?",
                    " (primary)" if p else "")
                    for d, i, s, p in outputs]
        _log.info("screen capture rebind: %d output(s), %d kept with history "
                  "(rebuild #%d)", len(outputs), kept, HEALTH.rebuilds)
        return bool(outputs)

    def rebuild(self, dxcam=None):
        """Force a full rebind. Kept for callers that know the topology moved."""
        with self._lock:
            self._signature = None
        return self.rebind(dxcam)

    # -- reading -----------------------------------------------------------
    def frame(self, monitor_index=0, max_age=DEFAULT_MAX_AGE):
        """Newest PIL frame for a monitor, or None if there isn't a fresh one.

        monitor_index is the DXGI output order (0 = primary). Callers that want
        a guaranteed-fresh capture must fall back to _capture_regions().
        """
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = DEFAULT_MAX_AGE
        with self._lock:
            if not self._buffers:
                return None
            try:
                index = int(monitor_index if monitor_index is not None else 0)
            except (TypeError, ValueError, OverflowError):
                return None
            if index < 0 or index >= len(self._buffers):
                return None
            buffer = self._buffers[index]
            age = buffer.age()
            if age is None or age > max_age or age > HARD_STALE_SECONDS:
                return None
            image = buffer.pil
            if image is None:
                image = _to_pil(buffer.raw)
                buffer.pil = image
        return image

    def monitor_count(self):
        with self._lock:
            return len(self._buffers)

    def labelled_frames(self, layout, max_age=DEFAULT_MAX_AGE):
        """Atomic, exact display-identity match; no positional label inference."""
        with self._lock:
            if not layout or len(layout) != len(self._buffers):
                return None
            regions, used = [], set()
            for monitor in layout:
                name, bounds = monitor.get("device_name"), tuple(monitor.get("bounds") or ())
                matches = [(i, b) for i, b in enumerate(self._buffers)
                           if b.geometry and name and b.geometry[1] == name
                           and b.geometry[2] == bounds]
                if len(matches) != 1 or matches[0][0] in used:
                    return None
                index, buffer = matches[0]
                age = buffer.age()
                if age is None or age > min(float(max_age), HARD_STALE_SECONDS):
                    return None
                image = buffer.pil
                if image is None:
                    image = buffer.pil = _to_pil(buffer.raw)
                if image is None:
                    return None
                used.add(index)
                regions.append({"label": monitor["label"], "image": image,
                                "primary": bool(monitor["primary"])})
            return regions

    # -- history reads (ROUND ZB-L2) -------------------------------------
    def _buffer_for(self, monitor_index=0):
        with self._lock:
            if not self._buffers:
                return None
            try:
                index = int(monitor_index if monitor_index is not None else 0)
            except (TypeError, ValueError, OverflowError):
                return None
            if index < 0 or index >= len(self._buffers):
                return None
            return self._buffers[index]

    def history_read(self, monitor_index=0, age_seconds=None, **kwargs):
        """(age_seconds, entry) nearest `age_seconds` back, or None.

        None is the honest answer for "that moment is gone"; it must never be
        quietly filled in with the CURRENT screen, because "what was there a
        minute ago" and "what is there now" are different questions.
        """
        buffer = self._buffer_for(monitor_index)
        if buffer is None:
            return None
        with self._lock:
            return buffer.history_read(age_seconds, **kwargs)

    def history_since(self, monitor_index=0, seconds=None, **kwargs):
        """Newest-first (age, entry) pairs inside the window."""
        buffer = self._buffer_for(monitor_index)
        if buffer is None:
            return []
        with self._lock:
            return buffer.history_since(seconds, **kwargs)

    def history_stats(self):
        """Per-monitor entry count and approximate bytes, for measurement."""
        with self._lock:
            return {
                "enabled": history_enabled(),
                "window_seconds": history_seconds(),
                "entries_cap": HISTORY_MAX_ENTRIES,
                "readable_interval": HISTORY_READABLE_INTERVAL,
                "monitors": [{
                    "monitor": b.output_idx,
                    "entries": len(b.history),
                    "readable": sum(1 for e in b.history
                                    if e.get("readable") is not None),
                    "bytes": b.history_bytes(),
                } for b in self._buffers],
            }

    def changed(self, monitor_index=0):
        """True when the screen differs from the previous poll.

        Cheap by construction: a 64x36 grayscale thumbnail (~2.3 KB), compared
        with a mean absolute difference. Used to decide that something HAPPENED,
        never to decide what it means.
        """
        with self._lock:
            if not self._buffers:
                return False
            try:
                index = int(monitor_index if monitor_index is not None else 0)
            except (TypeError, ValueError, OverflowError):
                return False
            if index < 0 or index >= len(self._buffers):
                return False
            buffer = self._buffers[index]
            thumb, previous = buffer.thumb, getattr(buffer, "_last_thumb", None)
            buffer._last_thumb = thumb
        if thumb is None:
            return False
        if previous is None or previous.size != thumb.size:
            return True
        try:
            import numpy as np

            thumb = np.asarray(thumb)
            if previous.shape != thumb.shape:
                return True
            return float(np.abs(
                thumb.astype(np.int16) - previous.astype(np.int16)
            ).mean()) > CHANGE_THRESHOLD
        except Exception:
            return False

    def health(self):
        return HEALTH.snapshot()


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def service():
    """Process-wide singleton."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ScreenCaptureService()
        return _SERVICE


def start():
    return service().start()


def stop():
    return service().stop()


def latest_frame(monitor_index=0, max_age=DEFAULT_MAX_AGE):
    return service().frame(monitor_index, max_age)


def monitor_count():
    return service().monitor_count()


def labelled_frames(layout, max_age=DEFAULT_MAX_AGE):
    return service().labelled_frames(layout, max_age)


def screen_changed(monitor_index=0):
    return service().changed(monitor_index)


# -- memory-only screen history (ROUND ZB-L2) ----------------------------
def history_read(monitor_index=0, age_seconds=None, **kwargs):
    """Nearest retained frame to `age_seconds` back, or None when it is gone."""
    return service().history_read(monitor_index, age_seconds, **kwargs)


def history_since(monitor_index=0, seconds=None, **kwargs):
    return service().history_since(monitor_index, seconds, **kwargs)


def history_stats():
    return service().history_stats()


def history_image(monitor_index=0, age_seconds=None, **kwargs):
    """(age_seconds, PIL image) of a retained frame, or None if it is gone."""
    buffer = service()._buffer_for(monitor_index)
    if buffer is None:
        return None
    with service()._lock:
        return buffer.readable_image(age_seconds, **kwargs)


def clear_screen_history(monitor_index=None, why="requested"):
    """Wipe history now. Used to honour a request like 'forget what you saw'."""
    with _SERVICE_LOCK:
        target = _SERVICE
    if target is None:
        return 0
    cleared = 0
    with target._lock:
        buffers = list(target._buffers)
    for buffer in buffers:
        if monitor_index is None or buffer.output_idx == int(monitor_index):
            buffer.clear_history(why)
            cleared += 1
    return cleared


def health():
    return service().health()
