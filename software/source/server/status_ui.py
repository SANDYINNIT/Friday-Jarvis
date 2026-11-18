"""Optional FRIDAY status face with a local pywebview orb and Tk fallback."""

from dataclasses import dataclass
import logging
import math
import os
import sys
from pathlib import Path
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone


STATUS_IDLE = "idle"
STATUS_LISTENING = "listening"
STATUS_THINKING = "thinking"
STATUS_SPEAKING = "speaking"
STATUS_ERROR = "error"
_VALID_STATES = frozenset({STATUS_IDLE, STATUS_LISTENING, STATUS_THINKING, STATUS_SPEAKING, STATUS_ERROR})
_STATE_LABELS = {STATUS_IDLE: "STANDBY", STATUS_LISTENING: "LISTENING", STATUS_THINKING: "THINKING", STATUS_SPEAKING: "SPEAKING", STATUS_ERROR: "ERROR"}
_STATE_COLORS = {STATUS_IDLE: "#4b7895", STATUS_LISTENING: "#42d6ff", STATUS_THINKING: "#b28cff", STATUS_SPEAKING: "#52f2b1", STATUS_ERROR: "#ff637d"}
UI_DIRECTORY = Path(__file__).with_name("ui")
UI_ENTRYPOINT = UI_DIRECTORY / "index.html"

# The single brain failover chain, in order, as provider stages. Used only to
# tell the user HOW FAR DOWN the chain FRIDAY had to go (the HUD pill that used
# to show a fake "tier"). Model-level order lives in brain_router.
_CHAIN_STAGES = ("brain_modal", "brain_groq", "brain_openrouter", "brain_gemini", "local")


def _openrouter_vision_model_display():
    """The OpenRouter vision model actually configured, for the HUD agent list.

    Round N (2026-10-03). This used to be a hardcoded
    `meta-llama/llama-3.2-11b-vision-instruct:free`, which OpenRouter has since
    retired, so the HUD advertised a model the code never called. Read the real
    value so the display cannot drift from the request again. Falls back to a
    neutral label rather than guessing if the module cannot be imported.
    """
    try:
        from .screen_understanding import OPENROUTER_VISION_MODEL

        return OPENROUTER_VISION_MODEL or "(not configured)"
    except Exception:
        return "(unavailable)"


def _openrouter_brain_model_display():
    """Configured OpenRouter brain model, without duplicating router policy."""
    try:
        from .brain_router import OPENROUTER_BRAIN_MODEL

        return OPENROUTER_BRAIN_MODEL or "(not configured)"
    except Exception:
        return "(unavailable)"


def _chain_position(pool):
    """Human label for a brain pool's depth in the failover chain."""
    if not pool:
        return "---"
    try:
        step = _CHAIN_STAGES.index(pool) + 1
    except ValueError:
        return str(pool).replace("_", " ").upper()
    return "STEP %d/%d" % (step, len(_CHAIN_STAGES))


@dataclass(frozen=True)
class StatusSnapshot:
    state: str
    label: str
    detail: str
    sequence: int
    updated_at: float


class StatusBus:
    """Thread-safe status/event boundary between FRIDAY workers and the UI."""

    def __init__(self, initial_state=STATUS_IDLE, initial_detail=""):
        if initial_state not in _VALID_STATES:
            raise ValueError(f"Unknown FRIDAY status: {initial_state}")
        self._lock = threading.Lock()
        self._snapshot = StatusSnapshot(initial_state, _STATE_LABELS[initial_state], str(initial_detail), 0, time.monotonic())
        self.chat = ChatBuffer()
        self.logs = LogBuffer()

    def publish(self, state, detail=""):
        if state not in _VALID_STATES:
            raise ValueError(f"Unknown FRIDAY status: {state}")
        with self._lock:
            previous = self._snapshot
            self._snapshot = StatusSnapshot(state, _STATE_LABELS[state], str(detail), previous.sequence + 1, time.monotonic())
            return self._snapshot

    def snapshot(self):
        with self._lock:
            return self._snapshot


class LogBuffer:
    """Thread-safe ring buffer that mirrors recent console/log output."""

    def __init__(self, limit=400):
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._lines = deque(maxlen=self.limit)

    def append(self, text):
        if text is None:
            return
        text = str(text).rstrip("\n")
        if not text:
            return
        if len(text) > 2000:
            text = text[:2000] + " ..."
        with self._lock:
            self._lines.append(text)

    def snapshot(self, limit=None):
        with self._lock:
            lines = list(self._lines)
        if limit:
            lines = lines[-int(limit):]
        return lines


class _ConsoleTee:
    """Forward writes to the real stream and the StatusBus log buffer."""

    def __init__(self, real_stream, buffer):
        self._real = real_stream
        self._buffer = buffer

    def write(self, data):
        try:
            self._real.write(str(data))
        except Exception:
            pass
        try:
            self._buffer.append(str(data))
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")

    def fileno(self):
        return self._real.fileno()


class _LoggingBridge(logging.Handler):
    """Route Python logging records (e.g. uvicorn) into the StatusBus log buffer."""

    def __init__(self, buffer, level=logging.INFO):
        super().__init__(level)
        self._buffer = buffer

    def emit(self, record):
        try:
            message = self.format(record)
            if message:
                self._buffer.append(message)
        except Exception:
            pass


def attach_log_capture(status_bus):
    """Tee console output and logging records into the status bus log buffer.

    This keeps the desktop app logs current without the user needing to
    watch a terminal window.
    """
    if status_bus is None or getattr(status_bus, "_console_tied", False):
        return
    status_bus._console_tied = True
    stdout_tee = _ConsoleTee(sys.stdout, status_bus.logs)
    stderr_tee = _ConsoleTee(sys.stderr, status_bus.logs)
    sys.stdout = stdout_tee
    sys.stderr = stderr_tee
    bridge = _LoggingBridge(status_bus.logs)
    logging.getLogger().addHandler(bridge)


class ChatBuffer:
    """Small thread-safe transcript shared by the server and optional UI."""

    def __init__(self, limit=100):
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._messages = deque(maxlen=self.limit)

    def append(self, role, content, status="complete", timestamp=None, *, knowledge=None):
        role = str(role)
        if role not in {"user", "assistant", "tool"}:
            raise ValueError("chat role must be user, assistant, or tool")
        content = str(content or "")[:12000]
        if not content:
            return None
        message = {
            "role": role,
            "content": content,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "status": str(status),
        }
        if knowledge is not None:
            message["knowledge"] = knowledge
        with self._lock:
            self._messages.append(message)
        return dict(message)

    def upsert_tool(self, content, status="complete", key=None):
        """Create or REPLACE a tool row in place, keyed by `key`.

        Tool output (notably the script FRIDAY wrote) arrives in many small
        fragments. Appending one row per fragment spams the conversation
        stream, and a plain append means the last state of a long script is
        never visible if the turn ends abruptly. This keeps exactly ONE row per
        `key` and rewrites its content as the value grows, so the feed always
        shows the newest full text of that step.

        Returns the stored message dict.
        """
        content = str(content or "")[:12000]
        with self._lock:
            if key is not None:
                for message in reversed(self._messages):
                    if message["role"] == "tool" and message.get("key") == key:
                        message["content"] = content or message["content"]
                        message["status"] = str(status)
                        return dict(message)
            message = {
                "role": "tool",
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": str(status),
                "key": key,
            }
            self._messages.append(message)
            return dict(message)

    def snapshot(self):
        with self._lock:
            return [dict(message) for message in self._messages]

    def merge_stream(self, content, *, knowledge=None):
        """Append an incremental assistant chunk onto the last assistant row."""
        content = str(content or "")[:12000]
        if not content:
            return None
        with self._lock:
            if self._messages and self._messages[-1]["role"] == "assistant":
                message = self._messages[-1]
                message["content"] = (message["content"] + content)[:12000]
                message["status"] = "streaming"
                if knowledge is not None:
                    message["knowledge"] = knowledge
                return dict(message)
        return self.append("assistant", content, status="streaming", knowledge=knowledge)

    def finalize_streaming(self):
        with self._lock:
            if self._messages and self._messages[-1]["status"] == "streaming":
                self._messages[-1]["status"] = "complete"

    def ensure_complete(self, content, role="assistant"):
        """Guarantee the final turn text appears in the feed exactly once.

        The turn snapshot is the source of truth: if the last assistant row is
        an incomplete/partial streaming version of that snapshot, heal it; if
        nothing was streamed, add the row. Never duplicates.
        """
        content = str(content or "")[:12000]
        if not content:
            return None
        with self._lock:
            last = self._messages[-1] if self._messages else None
            if last and last["role"] == "assistant":
                if last["content"].strip() == content.strip():
                    last["status"] = "complete"
                    return dict(last)
                if content.strip().find(last["content"].strip()) >= 0:
                    # Partial/mangled streaming row of the same reply: heal it.
                    last["content"] = content
                    last["status"] = "complete"
                    return dict(last)
        return self.append(role, content, status="complete")


def chat_payload(status_bus):
    return status_bus.chat.snapshot() if status_bus is not None else []


def status_payload(status_bus):
    """Return the small JSON-safe status shape consumed by the browser UI."""
    snapshot = status_bus.snapshot()
    return {
        "state": snapshot.state,
        "label": snapshot.label,
        "detail": snapshot.detail,
        "sequence": snapshot.sequence,
        "updated_at": snapshot.updated_at,
        "color": _STATE_COLORS[snapshot.state],
    }


def ui_settings(interpreter=None, status_bus=None):
    """Build display-only settings without importing or touching Tkinter."""
    llm = getattr(interpreter, "llm", None)
    try:
        model = str(getattr(llm, "model", "unknown"))
    except Exception:
        model = "unknown"
    from .capability_inventory import text
    from .semantic_memory import semantic_memory_enabled
    return {
        "interpreter model setting": text(model),
        "semantic embeddings": "configured (explicit calls only)" if semantic_memory_enabled() else "disabled; keyword fallback",
        "version": _project_version(),
        "status UI": "enabled",
        "current state": status_bus.snapshot().label if status_bus else "unknown",
    }


def _memory_db_path():
    from .memory import default_db_path
    return default_db_path()


_SECRET_LINE = re.compile(r"(?i)(api[_ -]?key|token|password|secret|authorization)\s*[:=]|bearer\s+\S+|sk-[A-Za-z0-9_-]+")


def project_log_tail(root=None, *, limit=200):
    """Tail FRIDAY log files, redacting likely secret-bearing lines.

    When ``root`` is given (tests, legacy callers) it scans that directory's
    known log filenames; otherwise it tails the real FRIDAY log file.
    """
    candidates = []
    if root is not None:
        root = Path(root)
        candidates = [
            root / "friday.log",
            root / "runtime.log",
            root / "logs" / "friday.log",
            root / "logs" / "runtime.log",
        ]
    else:
        try:
            from .file_logger import log_file_path
            candidates = [Path(log_file_path())]
        except Exception:
            candidates = []
        candidates += [
            Path.home() / ".friday" / "logs" / "friday.log",
            Path(__file__).resolve().parents[2] / "friday.log",
            Path(__file__).resolve().parents[2] / "runtime.log",
        ]
    files = [path for path in candidates if path.is_file()]
    if not files:
        return "No FRIDAY log file found yet. Run the assistant once, then reopen this window."
    lines = []
    for path in files[:1]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            lines.append(f"[{path.name}] unavailable: {error}")
            continue
        for line in content[-limit:]:
            lines.append("[redacted]" if _SECRET_LINE.search(line) else line[:2000])
    return "\n".join(lines)[-64000:]


_PROJECT_OMIT = {
    "$recycle.bin", "system volume information", "filehistory", "programdata",
    "program files", "program files (x86)", "windows",
}


def _classify_project(name):
    lower = name.lower()
    if any(token in lower for token in ("ai", "llm", "gpt", "ollama", "agent", "jarvis", "friday", "voice", "leon")):
        if any(token in lower for token in ("jarvis", "friday", "voice", "assistant", "agent")):
            return "assistant"
        return "ai"
    if any(token in lower for token in ("code", "dev", "git", "project", "source", "python", "java", "software", "sdk", "api")):
        return "coding"
    if any(token in lower for token in ("game", "minecraft", "sims", "mod", "curseforge", "blockbench")):
        return "game"
    if any(token in lower for token in ("video", "film", "media", "music", "download")):
        return "media"
    if any(token in lower for token in ("home", "iot", "hass", "automation", "sensor")):
        return "automation"
    return "misc"


class FaceStatusWindow:
    """A small opt-in shell; every Tk call is made by this window's UI thread."""

    def __init__(self, status_bus, interpreter=None):
        self.status_bus = status_bus
        self.interpreter = interpreter
        self._last_sequence = -1
        self._root = self._canvas = None
        self._status_var = self._detail_var = None
        self._memory_tree = self._memory_message = self._log_text = None
        self._memory_records = {}
        self._particles = [
            (math.cos(index * 2.399), math.sin(index * 2.399), 0.25 + (index % 17) / 22)
            for index in range(520)
        ]

    def run(self):
        import tkinter as tk
        from tkinter import ttk
        self._root = tk.Tk()
        self._root.title("FRIDAY")
        self._root.configure(bg="#07111f")
        self._root.geometry("820x680")
        self._root.minsize(700, 560)
        style = ttk.Style(self._root)
        style.configure("TNotebook", background="#07111f", borderwidth=0)
        style.configure("TFrame", background="#07111f")
        style.configure("TLabel", background="#07111f", foreground="#dceeff")

        header = tk.Frame(self._root, bg="#07111f")
        header.pack(fill="x")
        self._canvas = tk.Canvas(header, width=420, height=260, bg="#07111f", highlightthickness=0)
        self._canvas.pack(side="left")
        labels = tk.Frame(header, bg="#07111f")
        labels.pack(side="left", fill="both", expand=True, padx=12)
        self._status_var = tk.StringVar(value="STANDBY")
        self._detail_var = tk.StringVar(value="")
        tk.Label(labels, textvariable=self._status_var, bg="#07111f", fg="#dceeff", font=("Consolas", 16, "bold")).pack(anchor="w")
        tk.Label(labels, textvariable=self._detail_var, bg="#07111f", fg="#7895ab", font=("Consolas", 9), wraplength=300, justify="left").pack(anchor="w", pady=6)

        notebook = ttk.Notebook(self._root)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_memory_tab(notebook, tk, ttk)
        self._build_settings_tab(notebook, tk, ttk)
        self._build_logs_tab(notebook, tk, ttk)
        self._root.after(0, self._refresh)
        self._root.mainloop()

    def _build_memory_tab(self, notebook, tk, ttk):
        frame = ttk.Frame(notebook, padding=10); notebook.add(frame, text="Memory")
        toolbar = ttk.Frame(frame); toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Refresh", command=self._refresh_memory).pack(side="left")
        ttk.Button(toolbar, text="Soft-delete selected", command=self._delete_memory).pack(side="left", padx=8)
        self._memory_message = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self._memory_message).pack(side="left")
        columns = ("id", "scope", "key", "content", "updated")
        self._memory_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column, width in (("id", 45), ("scope", 90), ("key", 150), ("content", 360), ("updated", 150)):
            self._memory_tree.heading(column, text=column.title()); self._memory_tree.column(column, width=width, anchor="w")
        self._memory_tree.pack(fill="both", expand=True, pady=(8, 0))
        self._refresh_memory()

    def _build_settings_tab(self, notebook, tk, ttk):
        frame = ttk.Frame(notebook, padding=16); notebook.add(frame, text="Settings")
        for row, (name, value) in enumerate(ui_settings(self.interpreter, self.status_bus).items()):
            ttk.Label(frame, text=name.title() + ":", font=("Consolas", 10, "bold")).grid(row=row, column=0, sticky="nw", padx=(0, 20), pady=5)
            ttk.Label(frame, text=value, wraplength=560).grid(row=row, column=1, sticky="nw", pady=5)

    def _build_logs_tab(self, notebook, tk, ttk):
        frame = ttk.Frame(notebook, padding=10); notebook.add(frame, text="Logs")
        ttk.Button(frame, text="Refresh", command=self._refresh_logs).pack(anchor="w")
        self._log_text = tk.Text(frame, height=20, bg="#081522", fg="#c7d8e8", insertbackground="#c7d8e8", wrap="none", state="disabled")
        self._log_text.pack(fill="both", expand=True, pady=(8, 0))
        self._refresh_logs()

    def _refresh_memory(self):
        try:
            from .memory import MemoryStore
            records = MemoryStore().list_active(limit=100)
            for item in self._memory_tree.get_children(): self._memory_tree.delete(item)
            self._memory_records = {}
            for record in records:
                item = self._memory_tree.insert("", "end", values=(record["id"], record["scope"], record["key"], record["content"][:500], record["updated_at"]))
                self._memory_records[item] = record["id"]
            self._memory_message.set(f"{len(records)} active records")
        except Exception as error:
            self._memory_message.set(f"Database error: {error}")

    def _delete_memory(self):
        selected = self._memory_tree.selection()
        if not selected: return
        try:
            from .memory import MemoryStore
            count = MemoryStore().forget(memory_id=self._memory_records[selected[0]])
            self._memory_message.set("Memory deleted." if count else "Memory is no longer active.")
            self._refresh_memory()
        except Exception as error:
            self._memory_message.set(f"Database error: {error}")

    def _refresh_logs(self):
        self._log_text.configure(state="normal"); self._log_text.delete("1.0", "end"); self._log_text.insert("1.0", project_log_tail()); self._log_text.configure(state="disabled")

    def _refresh(self):
        snapshot = self.status_bus.snapshot()
        if snapshot.sequence != self._last_sequence:
            self._status_var.set(snapshot.label); self._detail_var.set(snapshot.detail); self._last_sequence = snapshot.sequence
        self._draw_face(snapshot)
        self._root.after(80, self._refresh)

    def _draw_face(self, snapshot):
        canvas = self._canvas
        canvas.delete("face")
        color = _STATE_COLORS[snapshot.state]
        pulse = (math.sin(time.monotonic() * 3.0) + 1.0) / 2.0
        center_x, center_y = 210, 128
        activity = pulse * (0.12 if snapshot.state == STATUS_IDLE else 0.28)
        for index, (x, y, radius) in enumerate(self._particles):
            angle = time.monotonic() * (0.15 + (index % 7) * 0.01) + index * 2.399
            distance = 48 + radius * 76 + math.sin(angle * 2 + index) * activity * 30
            px = center_x + math.cos(angle) * distance
            py = center_y + math.sin(angle) * distance * 0.72
            size = 1 + (index % 3) * 0.45 + activity * 2
            canvas.create_oval(px - size, py - size, px + size, py + size, fill=color, outline=color, tags="face")


# ---------------------------------------------------------------------------
# ROUND ZB-C2/C3: push events into the HUD so the globe bounce and captions
# follow what is ACTUALLY being spoken, rather than what the model produced.
#
# pywebview has no push API other than `window.evaluate_js`, and the window may
# not be registered yet when the first event fires, so the window is resolved
# lazily. Best-effort by design: a missing HUD must never break speech.
# ---------------------------------------------------------------------------
_UI_WINDOW = None
# (module-level singleton moved into lab_authority.shared - ROUND LAB-2B)

def set_ui_window(window):
    """Record the HUD window (optional; emit_ui_event falls back to webview)."""
    global _UI_WINDOW
    _UI_WINDOW = window


_WINDOW_OPEN_REVISION = 0
_WINDOW_OPENING = None


class WindowOpening:
    """Presentation observations for one exact HUD; never assistant readiness."""
    def __init__(self, visible):
        from uuid import uuid4
        self.epoch = uuid4().hex
        self._visible = visible
        self._lock = threading.RLock()
        self._ready = False
        self._closed = False
        self._last_visible = False
        self._revision = 0
        self._reported = set()

    def loaded(self, *_):
        with self._lock:
            if not self._closed:
                self._ready = True
        self.snapshot()
        # Installed pywebview stores callback return values in a set.
        # Event handlers must return None, not this observation dictionary.

    def close(self, *_):
        with self._lock:
            self._closed = True

    def hidden(self, *_):
        with self._lock:
            self._last_visible = False

    def snapshot(self):
        with self._lock:
            visible = None
            if self._ready and not self._closed:
                try:
                    visible = self._visible()
                except Exception:
                    pass
                if visible is True:
                    if not self._last_visible:
                        self._revision += 1
                        self._reported.clear()
                    self._last_visible = True
                elif visible is False:
                    self._last_visible = False
            return {"epoch": self.epoch, "revision": self._revision,
                    "ready": self._ready and not self._closed,
                    "visible": visible}

    def acknowledge(self, epoch, revision, outcome):
        allowed={"playing","complete","dismissed","reduced_motion","low_power","preferences_unavailable","animation_unavailable"}
        with self._lock:
            if (self._closed or not self._ready or epoch!=self.epoch or type(revision) is not int
                    or revision!=self._revision or not isinstance(outcome,str) or outcome not in allowed):
                return {"ok":False}
            if outcome not in self._reported:
                self._reported.add(outcome)
                print(f"[window-opening] revision={revision} outcome={outcome}",flush=True)
            return {"ok":True}


def _bind_window_opening(window, bridge):
    """Wait for document load before observing the owned WinForms HWND.

    WinForms briefly emits Shown with zero opacity when creating hidden
    windows. That is not a user-visible Open. Loaded + actual visibility
    avoids consuming the reveal during that transient initialization.
    """
    def visible():
        import win32gui
        import win32process
        gui = getattr(window, "gui", None)
        form = gui.BrowserView.instances.get(window.uid)
        if form is None or form.pywebview_window is not window:
            return None
        hwnd = int(form.Handle.ToInt64())
        if win32process.GetWindowThreadProcessId(hwnd)[1] != os.getpid():
            return None
        return bool(win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd))

    global _WINDOW_OPENING
    opening = WindowOpening(visible)
    bridge._window_opening = opening
    _WINDOW_OPENING = opening
    events = window.events
    if getattr(events, "loaded", None) is not None:
        events.loaded += opening.loaded
        if events.loaded.is_set():
            opening.loaded()
    if getattr(events, "minimized", None) is not None:
        events.minimized += opening.hidden
    events.closed += opening.close
    return opening


def note_window_open():
    """Presentation revision only; no store, provider or lifecycle changes."""
    global _WINDOW_OPEN_REVISION
    _WINDOW_OPEN_REVISION += 1
    if _WINDOW_OPENING is not None:
        _WINDOW_OPENING.snapshot()


def _globe_window():
    """The globe window by TITLE, never `windows[0]`."""
    try:
        import webview
        for window in (getattr(webview, "windows", None) or []):
            try:
                if (window.title or "").strip() == "FRIDAY Globe":
                    return window
            except Exception:
                continue
    except Exception:
        pass
    return None


# Events the globe page actually listens for (`window.GLOBE_EVENT`).
_GLOBE_EVENTS = ("audio_start", "audio_chunk", "audio_end", "stop")


def _boot_resume_presence(start_hidden: bool) -> dict:
    """LAB-3R6: a VISIBLE HUD at boot must resume free NPC life unless
    the user explicitly paused it ("closed" = Pause life button). A previous
    session's window-X / tray-close persisted presence=away -> world.paused=1
    in the store, and boot never cleared it, so a fresh session silently
    showed a frozen town (ticker alive, paused).

    Extracted as a module function so the decision is testable WITHOUT
    running a webview. Hidden boots (tray auto-start) keep the away state:
    the documented visibility rule says background = away.
    """
    if start_hidden:
        return {"ok": True, "skipped": "hidden boot"}
    try:
        from . import lab_state as _ls
        current = _ls.presence()
        if current != "closed":
            out = _ls.set_presence("open", visibility=True)
            return {"ok": bool(out.get("ok")), "previous": current,
                    "state": out.get("state")}
        return {"ok": True, "previous": current, "state": current,
                "note": "explicit Pause life respected"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def _roster_authority():
    """Full ONE-authority projection for the roster page (cast/life/serving),
    or an honest unavailable payload - never a guess."""
    try:
        from . import lab_authority as _lab_auth
        authority = _lab_auth.shared()
        if authority is not None:
            return _lab_auth.public_state(authority)
        return {"ok": False, "reason": "authority not configured"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}


def _serving_summary(authority):
    serving = authority.get("serving") or {}
    return {"serving": serving, "served_by": str(serving.get("served_by") or ""),
            "authority_rev": int(authority.get("rev") or 0)}


def emit_ui_event(kind, data=None):
    """Best-effort push to the HUD **and** the globe. Returns True when sent.

    ROUND ZB-G3 (2026-11-04). This used to fall back to `webview.windows[0]`.
    Since the globe is deliberately created BEFORE the HUD (so its `html=`
    content resolves), `windows[0]` IS the globe - so every push evaluated
    `window.FRIDAY_EVENT(...)` on a page that only defines `GLOBE_EVENT`.
    Result: the HUD's push path was dead and the globe never bounced, while
    the *tests* still passed because each half was tested in isolation.

    Now: the HUD is resolved by title (the same rule as `_main_hud_window`),
    and audio/stop events are additionally delivered to the globe so the bounce
    follows REAL playback instead of never firing at all.
    """
    import json as _json
    payload = _json.dumps({"type": str(kind), "data": data or {}})
    sent = False

    hud = _UI_WINDOW
    if hud is None:
        hud = _main_hud_window()
    if hud is not None:
        try:
            hud.evaluate_js("window.FRIDAY_EVENT && window.FRIDAY_EVENT(%s)"
                            % payload)
            sent = True
        except Exception:
            pass

    if str(kind) in _GLOBE_EVENTS:
        globe = _globe_window()
        if globe is not None:
            try:
                globe.evaluate_js(
                    "window.GLOBE_EVENT && window.GLOBE_EVENT(%s)" % payload)
                sent = True
            except Exception:
                pass
    return sent


# Living-Town authority singleton: ONE connection to the same database
# `lab_state.db_path()` uses, opened lazily on the first `get_lab()` call and
# never at import time (the HUD must not touch disk just by being imported).
# Keyed by path so a test that repoints the lab database re-opens cleanly.
# (the old module-level _LAB_AUTHORITY pair is gone: lab_authority.shared()
# owns the one singleton now - ROUND LAB-2B)


def _parse_memory_ref(ref):
    """Resolve an explicit memory domain and a valid SQLite row ID."""
    from .memory import memory_row_id

    if type(ref) is str:
        domain, _, tail = ref.strip().lower().partition("-")
        if domain in ("exp", "mem", "evt"):
            try:
                return domain, memory_row_id(tail)
            except ValueError:
                return None, "malformed memory reference"
    return None, "typed memory reference required"


def _canonical_memory_id(ref):
    """Accept mem-N and legacy numeric IDs on canonical methods only."""
    from .memory import memory_row_id

    try:
        return memory_row_id(ref)
    except ValueError:
        domain, parsed = _parse_memory_ref(ref)
        if domain == "mem":
            return parsed
        raise ValueError("canonical memory reference required (mem-N)") from None


def _project_notes_path():
    return Path(__file__).resolve().parents[2] / "memory" / "project_notes.md"


class WebviewBridge:
    """Safe, JSON-shaped API exposed to the local webview only.

    IMPORTANT: pywebview walks every public attribute of this object to build
    its JS API, recursing into any non-callable object attributes. The
    interpreter must therefore only be reachable via private names so pywebview
    never descends into interpreter.computer.skills (whose broken NewSkill
    property crashes the whole bridge).
    """

    def __init__(self, status_bus, interpreter=None):
        self._status_bus = status_bus
        self._interpreter = interpreter
        self._hud_visible = True
        # ROUND LAB-2B: warm the lab authority singleton the first time the
        # bridge exists (i.e. as soon as the HUD is created) so the serving
        # projection exists for real turns even before the LAB pane is ever
        # opened. Best-effort and never blocking: a lab fault must not keep
        # the HUD from starting.
        try:
            from . import lab_authority as _lab_auth
            _lab_auth.shared()
        except Exception:
            pass
        # ROUND LAB-2C: warm the durable budget the same way (one singleton;
        # additive table creation happens against the production database the
        # app itself selects; under pytest nothing opens the real store).
        try:
            from . import lab_budget as _lab_budget
            _lab_budget.shared()
        except Exception:
            pass

    def get_status(self):
        payload = status_payload(self._status_bus)
        server_state = getattr(self._interpreter, "server_state", None) or {}
        paused = server_state.get("is_paused")
        payload["paused"] = paused if type(paused) is bool else None
        # ROUND ZB-APP: the HUD polls this every second, so it is the cheapest
        # place to bring the Future Lab ticker up — it then runs whenever the
        # FRIDAY window exists (including a hidden tray auto-start) without
        # touching server startup ordering. Idempotent and never raises.
        try:
            from . import lab_state
            lab_state.ensure_started()
        except Exception:
            pass
        return payload

    def send_text(self, text):
        submit = getattr(self._interpreter, "submit_text", None)
        if not callable(submit):
            return {"accepted": False, "status": "unavailable", "message": "FRIDAY server is not ready yet."}
        return submit(str(text or ""))

    def get_chat(self):
        return chat_payload(self._status_bus)

    def get_settings(self):
        return ui_settings(self._interpreter, self._status_bus)

    def get_n8n(self):
        from .n8n_bridge import for_runtime
        bridge = for_runtime(self._interpreter)
        result = bridge.snapshot()
        result["inbox"] = bridge.inbox()
        controller = (getattr(self._interpreter, "server_state", None) or {}).get("n8n_turn")
        result["proposal"] = controller.preview() if controller is not None else None
        return result

    def set_n8n(self, changes, revision):
        from .n8n_bridge import for_runtime
        return for_runtime(self._interpreter).save(changes, revision)

    def test_n8n(self):
        from .n8n_bridge import for_runtime
        from .n8n_bridge import public_operation
        return public_operation(for_runtime(self._interpreter).operate("status", {}))

    def pull_n8n(self):
        from .n8n_bridge import for_runtime
        from .n8n_bridge import public_operation
        return public_operation(for_runtime(self._interpreter).pull())

    def decide_n8n_request(self, proposal_id, choice):
        submit = getattr(self._interpreter, "decide_workflow_request", None)
        return submit(proposal_id, choice) if callable(submit) else {"ok": False, "error": "request_owner_unavailable"}

    def preview_n8n_decision(self, event_id):
        from .n8n_bridge import for_runtime, Refused
        try:
            preview = for_runtime(self._interpreter).decision_preview(event_id)
            # Exact non-sensitive arguments only. Sensitive previews refuse
            # rather than hiding fields and asking the owner to approve them.
            return {"ok": True, **preview}
        except (Refused, OSError, ValueError):
            return {"ok": False, "error": "decision_unavailable"}

    def decide_n8n_event(self, event_id, digest, choice):
        from .n8n_bridge import for_runtime
        from .n8n_bridge import public_operation
        return public_operation(for_runtime(self._interpreter).decide(event_id, digest, choice))

    def get_companion(self):
        from .companion import for_runtime
        presenter = for_runtime(self._interpreter)
        return presenter.snapshot() if presenter is not None else {
            "available": False, "message": "The local companion runtime is not ready."}

    def set_companion(self, changes, revision):
        if isinstance(changes, dict) and set(changes) & {"private_banter", "proactive_suggestions"}:
            return self.set_personality(changes, revision)
        from .companion import for_runtime
        presenter = for_runtime(self._interpreter)
        return presenter.save(changes, revision) if presenter is not None else {
            "ok": False, "message": "The local companion runtime is not ready."}

    def get_personality(self):
        personality = getattr(self._interpreter, "personality", None)
        return personality.snapshot() if personality is not None else {
            "available": False, "message": "Personality settings are unavailable; nothing was initialized."}

    def set_personality(self, changes, revision):
        personality = getattr(self._interpreter, "personality", None)
        return personality.save(changes, revision) if personality is not None else {
            "ok": False, "message": "Personality settings are unavailable; nothing was saved."}

    def acknowledge_companion(self, identifier):
        from .companion import for_runtime
        presenter = for_runtime(self._interpreter)
        return presenter.acknowledge(identifier) if presenter is not None else {"ok": False}

    def get_window_state(self):
        controls = getattr(self, "_window_controls", None)
        if controls is None:
            return {"available": False, "integrated": False, "maximized": None}
        value = {**controls.snapshot(), "opening_revision": _WINDOW_OPEN_REVISION}
        opening = getattr(self, "_window_opening", None)
        if opening is not None:
            value["opening"] = opening.snapshot()
        return value

    def report_window_opening(self, epoch, revision, outcome):
        opening=getattr(self,"_window_opening",None)
        return opening.acknowledge(epoch,revision,outcome) if opening is not None else {"ok":False}

    def window_action(self, action):
        controls = getattr(self, "_window_controls", None)
        if controls is None:
            return {"ok": False, "message": "Native window controls are unavailable."}
        return controls.action(action)

    def get_skills(self):
        root = Path(__file__).resolve().parents[2]
        server_dir = root / "source" / "server"
        try:
            files = [p.name for p in server_dir.glob("*.py")]
            return files
        except Exception:
            return ["server.py", "command_router.py", "memory.py"]

    def get_project_notes(self):
        notes_path = _project_notes_path()
        try:
            if notes_path.is_file():
                return notes_path.read_text(encoding="utf-8")
            return "No project notes recorded yet."
        except Exception as error:
            return f"Error reading notes: {error}"

    def get_memory(self):
        from .memory import MemoryStore

        return [{**row, "ref": f"mem-{row['id']}"}
                for row in MemoryStore().list_active(limit=100)]

    def get_memory_library(self):
        from . import lab_memory, library_views, memory

        return library_views.memory_library(memory.default_db_path(), lab_memory.shared())

    def get_knowledge_library(self):
        from . import lab_memory, library_views, memory
        from .local_knowledge import KnowledgeStore

        result = library_views.knowledge_library(
            memory.default_db_path(), lab_memory.shared(), _project_notes_path())
        try:
            result["imports"] = KnowledgeStore().directory()
        except Exception:
            result["imports"] = {"state": "error", "rows": []}
        return result

    def import_knowledge(self, title, text, filename=None, source_id=None, expected_version=None):
        from .local_knowledge import KnowledgeStore

        try:
            return {"ok": True, **KnowledgeStore().publish(
                title, text, filename, source_id=source_id, expected_version=expected_version)}
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        except Exception:
            return {"ok": False, "reason": "Could not publish the source. Existing versions were kept."}

    def search_knowledge(self, query):
        from .local_knowledge import KnowledgeStore

        try:
            return {"ok": True, "rows": KnowledgeStore().search(query)}
        except Exception:
            return {"ok": False, "reason": "The local index could not be searched."}

    def get_knowledge_source(self, source_id, version_id):
        from .local_knowledge import KnowledgeStore

        try:
            source = KnowledgeStore().details(source_id, version_id)
            if source is not None:
                return {"ok": True, "source": source}
            return {"ok": False, "reason": "That source version is no longer active."}
        except Exception:
            return {"ok": False, "reason": "The source details could not be read."}

    def get_conversation_library(self):
        from .library_views import conversation_library

        return conversation_library(self._interpreter)

    def get_conversation_transcript(self, conversation_id):
        from .library_views import conversation_transcript

        return conversation_transcript(self._interpreter, conversation_id)

    def export_conversation(self, conversation_id):
        import json
        result=self.get_conversation_transcript(conversation_id)
        if result.get('state')!='ready':return {'ok':False,'message':'Saved conversation unavailable.'}
        if result.get('oversize'):return {'ok':False,'message':'This archive exceeds the safe transcript preview limit; no export was created.'}
        payload=json.dumps({'format':'friday-transcript-1','id':conversation_id,
            'truncated':bool(result.get('truncated') or result.get('incomplete')),'messages':result.get('rows',[])},ensure_ascii=False,indent=2)
        if len(payload.encode('utf-8'))>1_000_000:return {'ok':False,'message':'Export exceeds the1MiB limit; no file was created.'}
        return {'ok':True,'filename':'friday-conversation.json','content':payload,'truncated':bool(result.get('truncated') or result.get('incomplete'))}

    def delete_conversation(self, conversation_id, revision, confirm=False):
        archive=getattr(self._interpreter,'light_archive',None)
        return archive.remove(conversation_id,revision,confirm) if archive is not None else {'ok':False,'message':'Light archive unavailable.'}

    def delete_memory(self, memory_id):
        from .memory import MemoryStore

        try:
            row_id = _canonical_memory_id(memory_id)
            deleted = MemoryStore().forget(memory_id=row_id)
        except ValueError as error:
            return {"ok": False, "reason": str(error), "deleted": 0}
        except Exception as error:
            return {"ok": False, "error": type(error).__name__, "deleted": 0}
        if not deleted:
            return {"ok": False, "reason": "memory is no longer active", "deleted": 0}
        return {"ok": True, "deleted": deleted}

    def add_memory(self, key, content):
        from .memory import MemoryStore

        try:
            if not isinstance(key, str) or not isinstance(content, str):
                raise ValueError("key and content are required")
            record = MemoryStore().remember(key=key, content=content, source="user")
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}
        if record is None:
            return {"ok": False, "reason": "memory was corrected or forgotten; use Edit for a current memory"}
        return {**record, "ok": True, "ref": f"mem-{record['id']}"}

    def update_memory(self, memory_id, key, content, expected=None):
        from .memory import MemoryStore

        try:
            row_id = _canonical_memory_id(memory_id)
            if not isinstance(key, str) or not isinstance(content, str):
                raise ValueError("key and content are required")
            store = MemoryStore()
            if expected is None:
                record = store.update(row_id, key=key, content=content)
            else:
                result = store.correct(row_id, key=key, content=content,
                                       reason="user edit", expected=expected)
                record = result["record"] if result else None
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}
        if record is None:
            return {"ok": False, "reason": "memory is inactive or the edit did not change it"}
        return {**record, "ok": True, "ref": f"mem-{record['id']}"}

    def get_startup(self):
        """Settings tab: startup-on-boot + tray availability."""
        from .startup_settings import startup_snapshot
        return startup_snapshot()

    def set_startup(self, enabled, expected=None):
        """Toggle FRIDAY auto-start at Windows boot."""
        from .startup_settings import set_startup_checked
        return set_startup_checked(enabled, expected)

    def get_app_window(self):
        """Whether the HUD window is currently visible."""
        return {"visible": self._hud_visible}

    def set_app_window(self, visible):
        """Close App (background): hide/show the HUD window from the tray."""
        self._hud_visible = bool(visible)
        return {"visible": self._hud_visible}

    def set_paused(self, paused):
        """Session voice input setting. Queue acceptance is not a device acknowledgement."""
        interpreter = self._interpreter
        server_state = getattr(interpreter, "server_state", None)
        sync_queue = getattr(getattr(interpreter, "output_queue", None), "sync_q", None)
        if (type(paused) is not bool or not isinstance(server_state, dict)
                or not callable(getattr(sync_queue, "put_nowait", None))):
            return {"accepted": False, "message": "Voice input control is unavailable."}
        apply_setting = getattr(interpreter, "set_session_voice_input", None)
        if callable(apply_setting):
            receipt = apply_setting(paused)
            if receipt is None:
                return {"accepted": False, "message": "Could not queue the voice setting. The previous setting was kept."}
            resume = getattr(interpreter, "resume_owned_voice", None)
            if not paused and callable(resume):
                try:
                    resume()
                except Exception:
                    pass
            return {"paused": paused, "accepted": True, "client_receipt": receipt,
                    "message": "Session setting queued. Audio client receipt is separate."}
        from contextlib import nullcontext

        with server_state.get("hot_window_lock") or nullcontext():
            try:
                sync_queue.put_nowait({"voice_paused": paused, "end": True})
            except Exception:
                return {"accepted": False, "message": "Could not send the voice setting. It was kept as it was."}
            server_state["is_paused"] = paused
            if paused:
                server_state.update(hot_window_active=False, hot_window_expires_at=0.0)
        resume = getattr(interpreter, "resume_owned_voice", None)
        message = "Voice input paused for this session." if paused else "Voice input resumed for this session."
        if not paused and callable(resume):
            try:
                resume()
            except Exception:
                message = "Voice input setting resumed. The resume follow-up could not be scheduled."
        return {"paused": paused, "accepted": True, "message": message}

    def get_voice_screen(self):
        """Read only the current process, with no device probes or media content."""
        state = getattr(self._interpreter, "server_state", None)
        if not isinstance(state, dict):
            return {"available": False}
        from .voice_screen import voice_screen_snapshot

        payload = voice_screen_snapshot(self._status_bus.snapshot(), state, self.get_speech())
        queue = getattr(getattr(self._interpreter, "output_queue", None), "sync_q", None)
        payload["listening"]["control_available"] = callable(getattr(queue, "put_nowait", None))
        return payload

    def get_logs(self):
        """Return the real FRIDAY log tail (file logger is the single source of truth)."""
        return project_log_tail()

    def get_sys_stats(self):
        """Return lightweight live system telemetry for the HUD."""
        stats = {}
        try:
            import psutil
            stats["cpu"] = psutil.cpu_percent(interval=None)
            stats["ram"] = dict(psutil.virtual_memory()._asdict())
            stats["ram"]["percent"] = psutil.virtual_memory().percent
            stats["disk"] = dict(psutil.disk_usage("C:\\")._asdict())
            stats["boot_time"] = psutil.boot_time()
        except Exception as error:
            stats["error"] = f"psutil unavailable: {error}"
        return stats

    def get_knowledge(self):
        """Sections of the basic knowledge FRIDAY relies on, incl. live D:\\ projects."""
        sections = {
            "core_capabilities": [
                "Voice & wake word FRIDAY, plus an always-listening desktop ear (last 2 minutes of system audio)",
                "Follow-up window: about 10 seconds after each reply to continue the conversation",
                "Memory: persistent facts, automatic learning, and project notes",
                "Screen understanding for 'what is on my screen' questions",
                "Web: live documentation search and page fetching on demand",
                "Reminders: relative, clock, and 'before I log off' timers from natural language",
            ],
            "development_workflow": [
                "Default local model qwen3:8b for conversation and tools",
                "Tool loop: ask -> execute -> verify -> report truthfully",
                "Project notes follow code work automatically (memory/project_notes.md)",
                "Proactive diagnostics check RAM, temperatures-sidekicks, and git status",
            ],
            "windows_administration": [
                "Window control: list/focus/minimize with before-after verification",
                "Screen understanding routes visual questions to the vision path",
                "Social call guard: FRIDAY stays silent during Discord/WhatsApp/Instagram/Telegram calls",
            ],
            "d_drive_projects": self._list_d_projects(),
        }
        sections["project_notes"] = [
            line for line in self.get_project_notes().splitlines() if line.strip()
        ][-12:]
        return sections

    def _list_d_projects(self, limit=40):
        anchors = [Path("D:\\"), Path.home() / "Desktop"]
        projects = []
        seen = set()
        for anchor in anchors:
            try:
                if not anchor.is_dir():
                    continue
                children = sorted((child for child in anchor.iterdir() if child.is_dir()), key=lambda c: c.name.lower())
            except OSError:
                continue
            for child in children:
                name = child.name.strip()
                if not name or name.lower() in _PROJECT_OMIT or name in seen:
                    continue
                seen.add(name)
                projects.append({"name": name, "kind": _classify_project(name)})
                if len(projects) >= limit:
                    return projects
        return projects

    def get_tools(self):
        """Sections of the tools and skills FRIDAY currently knows and uses."""
        root = Path(__file__).resolve().parents[2]
        sections = {}
        server_dir = root / "source" / "server"
        sections["core_modules"] = sorted(
            path.name
            for path in server_dir.glob("*.py")
            if not path.name.startswith(("__", "test", "_"))
        )
        try:
            sections["profiles"] = sorted(
                path.stem
                for path in (server_dir / "profiles").glob("*.py")
                if path.stem not in ("__init__",)
            )
        except OSError:
            sections["profiles"] = []
        skills_dir = root / "skills"
        try:
            sections["skill_files"] = sorted(path.name for path in skills_dir.iterdir()) if skills_dir.is_dir() else []
        except OSError:
            sections["skill_files"] = []
        sections["system_integrations"] = [
            "Windows: window list/focus/minimize, foreground tracking, screen understanding",
            "Audio: wake word, STT, Edge-TTS voice, WASAPI loopback desktop ear",
            "Web: Bing search + live page/doc fetching (requests + stdlib HTMLParser)",
            "Memory: SQLite friday_memory.db, automatic extraction, project notes",
            "Reminders: SQLite friday_reminders.db, polling worker, spoken delivery",
            "Tasks: SQLite friday_tasks.db, add/list/complete/delete, natural-language due dates",
            "Diagnostics: CPU/RAM/disk/processes/services/network, health verdict, watchdog reset",
            "Automation: optional n8n webhook workflows (off until connected; FRIDAY falls back to her own tools)",
        ]
        return sections

    def get_capability_inventory(self):
        """Inspect FRIDAY metadata without invoking discovery or execution."""
        from .capability_inventory import capability_inventory

        return capability_inventory(self._interpreter)

    def get_workflow_view(self):
        from .workflow_view import workflow_snapshot

        return workflow_snapshot(self._interpreter)

    def workflow_request_target(self, reference):
        from .workflow_view import request_target

        return request_target(self._interpreter, reference)

    def get_focus(self):
        state = getattr(self._interpreter, "server_state", None)
        if state and state.get("focus"):
            return state["focus"]
        try:
            from .focus_tracker import foreground_info, _is_editor
            title, exe = foreground_info()
            return {"title": title[:160], "exe": exe, "since": "", "editor": _is_editor(exe)}
        except Exception:
            return {"title": "", "exe": "", "since": "", "editor": False}

    def get_ear_status(self):
        state = getattr(self._interpreter, "server_state", None)
        ear = state.get("desktop_ear") if state else None
        guard = state.get("social_guard") if state else None
        return {
            "enabled": bool(ear),
            "capturing": bool(ear and ear.active),
            "window_seconds": 120,
            "error": (str(ear.error) if ear and ear.error else ""),
            "social_call_active": bool(guard and guard.active()),
        }

    def get_schedule(self):
        """Read the active local schedule without initializing a new store."""
        state = getattr(self._interpreter, "server_state", {}) or {}
        store = state.get("schedule_store")
        if store is None:
            return {"available": False, "events": [], "count": 0, "status": "disconnected"}
        try:
            return store.agenda()
        except Exception:
            return {"available": False, "events": [], "count": 0, "status": "error"}

    def mutate_calendar(self, reference, action, values, revision, confirm=False):
        store=(getattr(self._interpreter,'server_state',{}) or {}).get('schedule_store')
        if store is None:return {'ok':False,'message':'The local calendar is unavailable.'}
        return store.mutate(reference,action,values,revision,confirm)

    # ------------------------------------------------------------------
    # ROUND ZB-APP — live truth for the redesigned app + Future Lab.
    # Every value below is read from the running process. A failure degrades to
    # an explicit null/"unavailable" rather than a plausible-looking guess.
    # ------------------------------------------------------------------
    def get_routing_view(self):
        """Field Notes routing view. Never warms pools, stores or adapters."""
        state = getattr(self._interpreter, "server_state", None)
        if not isinstance(state, dict):
            return {"available": False}
        from .routing_view import routing_snapshot

        return routing_snapshot(state)

    def get_rollout_readiness(self):
        """Report observed rollout prerequisites without initializing a subsystem."""
        state = getattr(self._interpreter, "server_state", None)
        if not isinstance(state, dict):
            return {"available": False}
        from .rollout_readiness import rollout_readiness

        return rollout_readiness(state)

    def get_core(self):
        """Live model/provider/TTS truth for the AI Core screen."""
        out = {
            "available": False,
            "brain": {"pool": None, "model": None, "last_pool": None,
                      "last_model": None},
            "vision": {"model": None, "available": None},
            "tts": {"stage": None, "url": None, "degraded": False,
                    "reason": "", "retry_in_seconds": 0},
            "chain": list(_CHAIN_STAGES), "chain_step": "",
            "prompt_render": {"available": False},
            "voice": {"render_seconds": None, "first_token_seconds": None,
                      "route_seconds": None, "turns": 0},
            "providers": [], "quota": {},
        }
        server_state = getattr(self._interpreter, "server_state", None) or {}
        try:
            router = server_state.get("brain_router")
            if router is not None:
                out["brain"] = {
                    "pool": getattr(router, "current_pool", None),
                    "model": getattr(router, "current_model", None),
                    "last_pool": getattr(router, "last_pool", None),
                    "last_model": getattr(router, "last_model", None),
                }
            active = out["brain"]["pool"] or out["brain"]["last_pool"]
            out["chain_step"] = _chain_position(active) if active else ""
            out["available"] = True
        except Exception as error:
            out["error"] = type(error).__name__
        # ROUND LAB-2B: the serving projection. Every view reads the ONE
        # authority; get_core no longer has to be its own truth for "who is
        # FRIDAY right now". Absent authority (test sandbox) -> a small,
        # honest "unavailable" record instead of an invented one.
        try:
            from . import lab_authority as _lab_auth
            authority = _lab_auth.shared()
            if authority is not None:
                state = authority.snapshot()
                out["authority"] = {
                    "rev": state.get("rev"),
                    "axes": state.get("axes"),
                    "serving": state.get("serving"),
                    "precedence": state.get("precedence"),
                }
            else:
                out["authority"] = {"ok": False, "reason": "not configured"}
        except Exception as error:
            out["authority"] = {"ok": False, "error": type(error).__name__}
        try:
            from . import api_pools
            pools = api_pools.describe_pools() or {}
            out["quota"] = {name: {"available": info.get("available", 0),
                                   "total": info.get("total", 0)}
                            for name, info in pools.items()}
            out["providers"] = sorted(pools)
            vision_pools = [n for n in pools if "vision" in n]
            if vision_pools:
                out["vision"]["available"] = any(
                    pools[n].get("available") for n in vision_pools)
        except Exception:
            pass
        try:
            from . import gemini_tts as _gt
            out["tts"]["stage"] = getattr(_gt, "LAST_STAGE", None)
            out["tts"]["url"] = getattr(_gt, "LOCAL_TTS_URL", None)
        except Exception:
            pass
        try:
            from . import server as _srv
            degraded = _srv.tts_degraded_status(server_state) or {}
            if isinstance(degraded, dict):
                out["tts"]["degraded"] = bool(degraded.get("degraded"))
                out["tts"]["reason"] = str(degraded.get("reason") or "")
                out["tts"]["retry_in_seconds"] = degraded.get("retry_in_seconds", 0)
        except Exception:
            pass
        try:
            from . import prompt_render_cache as _prc
            out["prompt_render"] = _prc.stats()
            out["prompt_render"]["available"] = True
        except Exception:
            pass
        try:
            # Process uptime (honest source: the OS, not a narrative counter).
            import os as _os
            import time as _time
            import psutil
            out["uptime_seconds"] = int(max(
                0, _time.time() - psutil.Process(_os.getpid()).create_time()))
        except Exception:
            out["uptime_seconds"] = None
        try:
            from .voice_trace import get_trace as _get_trace
            recorder = _get_trace()
            out["voice"]["turns"] = recorder.turn_count()
            last = recorder.last() or {}
            stages = last.get("stages") or {}
            out["voice"]["first_token_seconds"] = stages.get("first_token")
            # Contract key: the frontend labels this "first audio".
            out["voice"]["render_seconds"] = stages.get("tts_first_audio")
            out["voice"]["route_seconds"] = stages.get("route")
        except Exception:
            pass
        return out

    def get_turn(self):
        """Live turn state for the Voice & Screen screen and the Stop button."""
        server_state = getattr(self._interpreter, "server_state", None) or {}
        active = False
        try:
            thread = getattr(self._interpreter, "respond_thread", None)
            active = bool(thread is not None and thread.is_alive())
        except Exception:
            active = False
        queued = 0
        try:
            from .server import _pending_queue
            queued = len(_pending_queue(server_state))
        except Exception:
            queued = 0
        status = status_payload(self._status_bus)
        mcp = server_state.get("mcp_turn")
        return {
            "active": active,
            "label": status.get("label"),
            "queued": queued,
            "elapsed_seconds": None,          # not measured yet — say nothing
            "source": status.get("source") or "",
            "utterance_id": None,
            "stop_available": bool(active or queued),
            "mcp_proposal": mcp.preview() if mcp is not None else None,
            "n8n_request_pending": bool(server_state.get("n8n_turn") is not None and server_state["n8n_turn"].preview()),
            "paused": bool(server_state.get("is_paused")),
        }

    def decide_mcp_proposal(self, proposal_id, approve):
        decide = getattr(self._interpreter, "decide_mcp_proposal", None)
        return decide(proposal_id, approve) if callable(decide) else {"ok": False}

    def get_tasks(self):
        """Project personal tasks, reminders and exact n8n requests separately."""
        out = {"tasks": [], "reminders": [], "pending": [], "sources": {}}
        state = getattr(self._interpreter, "server_state", {}) or {}
        try:
            from .task_store import TaskStore
            result = TaskStore().list_tasks(status="all")
            if not result.get("ok"):
                raise OSError("Personal task store unavailable")
            out["tasks"] = result["tasks"]
            out["sources"]["tasks"] = "ready"
        except Exception:
            out["sources"]["tasks"] = "error"

        for name, store_key in (("reminders", "reminder_store"), ("pending", "pending_store")):
            store = state.get(store_key)
            if store is None:
                out["sources"][name] = "disconnected"
                continue
            try:
                if name == "reminders":
                    out[name] = store.list()
                else:
                    out[name] = store.owner_views()
                out["sources"][name] = "ready"
            except Exception:
                out["sources"][name] = "error"
        out["decision_available"] = callable(getattr(self._interpreter, "submit_pending_decision", None))
        personality = getattr(self._interpreter, "personality", None)
        if personality is not None and out["sources"].get("tasks") == "ready":
            # Reuse this reader's existing authoritative result. No extra store
            # construction, scan, model request or task execution for initiative.
            for task in out["tasks"]:
                if task.get("status") == "open" and task.get("priority") == "high":
                    personality.suggest("personal_task", str(task["id"]), current=True,
                        live=lambda task=task: task.get("status") == "open" and task.get("priority") == "high")
        return out

    def mutate_personal_task(self, reference, action, values, revision=None, confirm=False, operation_id=None):
        from .task_store import TaskStore
        if action=='delete' and confirm is not True:
            return {'ok':False,'message':'Confirm deletion of this exact personal task and its notes.'}
        return TaskStore().mutate(reference,action,values,revision,operation_id)

    def decide_pending_request(self, correlation_id, answer):
        """Submit one explicit local-owner action to the existing n8n resolver."""
        submit = getattr(self._interpreter, "submit_pending_decision", None)
        if not callable(submit):
            return {"ok": False, "message": "Direct decisions are unavailable. Use the exact n8n command in chat."}
        try:
            return submit(correlation_id, answer)
        except Exception:
            return {"ok": False, "message": "The decision could not be confirmed. Check its status before retrying."}

    def get_identity_views(self, selected_character_id=None, catalog_revision=None):
        """Read paired catalog views without discovery, startup or store creation.

        The reader gate is independent of observation/enrollment. A disabled or
        unavailable reader exposes only already-loaded legacy resident labels.
        Catalog-scoped serving writes require the already initialized authority.
        """
        from . import lab_state
        return self._read_identity_views(lab_state.capture_identity_world, selected_character_id, catalog_revision)

    def _read_identity_views(self, capture, selected_character_id, catalog_revision):

        def unavailable(reason, enabled=False):
            world = capture()
            legacy = [] if world is None else [
                {"source_resident_id": char.id, "name": char.name,
                 "activity": char.state, "resolution": "unresolved"}
                for char in world.characters
            ]
            return {"enabled": enabled, "available": False, "reason": reason,
                    "catalog_revision": None, "selected_character_id": None,
                    "legacy_residents": legacy}

        if os.environ.get("FRIDAY_IDENTITY_READER") != "1":
            return unavailable("Catalog reader is disabled. Legacy identities remain separate.")
        from uuid import UUID
        try:
            valid_selection = selected_character_id is None or (
                isinstance(selected_character_id, str)
                and str(UUID(selected_character_id)) == selected_character_id)
        except (ValueError, TypeError, AttributeError):
            valid_selection = False
        if not valid_selection:
            return unavailable("Invalid character UUID.", True)
        try:
            if catalog_revision is not None and (
                    type(catalog_revision) is not int or catalog_revision < 1):
                return unavailable("Invalid catalog revision.", True)
            state = getattr(self._interpreter, "server_state", None) or {}
            integration = state.get("identity_integration")
            shadow = getattr(integration, "shadow", None)
            if shadow is None:
                return unavailable("Catalog observation source is unavailable. Enrollment was not started.", True)
            from .world.identity_reader import IdentityReader
            from . import lab_state
            reader = IdentityReader(shadow, capture, enabled=True)
            enrolled = getattr(integration, "enrolled", None)
            controller = getattr(lab_state, "background_life", lambda: None)()
            life = controller if controller is not None and controller.identity is integration else None
            result = reader.snapshot(selected_character_id, catalog_revision=catalog_revision,
                                     enrolled=enrolled if callable(enrolled) else None,
                                     life=life.projection if life else None)
            if life is not None:
                result["life_budget_enabled"] = life.enabled
            return result
        except Exception:
            return unavailable("Catalog view could not be read. No settings were changed.", True)

    def set_catalog_serving(self, character_id, binding_id, capability, allowed, revision):
        """Write only through an already initialized catalog authority."""
        if os.environ.get("FRIDAY_IDENTITY_READER") != "1":
            return {"ok": False, "message": "Catalog controls are unavailable."}
        state = getattr(self._interpreter, "server_state", None) or {}
        integration = state.get("identity_integration")
        setter = getattr(integration, "set_serving", None)
        if not callable(setter):
            return {"ok": False, "message": "Catalog controls are unavailable."}
        try:
            updated = setter(character_id, binding_id, capability, allowed, revision)
            return {"ok": True, "catalog_revision": updated, "binding_id": binding_id, "allowed": allowed}
        except ValueError:
            return {"ok": False, "message": "The binding or revision changed. Refresh before saving."}
        except Exception:
            return {"ok": False, "message": "The serving preference could not be saved."}

    def _catalog_life_controller(self):
        from . import lab_state

        if os.environ.get("FRIDAY_IDENTITY_READER") != "1":
            return None
        state = getattr(self._interpreter, "server_state", None) or {}
        controller = lab_state.background_life()
        return controller if controller and controller.identity is state.get("identity_integration") else None

    def set_catalog_background_life(self, character_id, binding_id, mode, revision):
        from . import lab_state

        try:
            controller = self._catalog_life_controller()
            if controller is None:
                return {"ok": False, "message": "The background scheduler is unavailable."}
            updated, changed = lab_state.set_catalog_life(
                controller.identity, character_id, binding_id, mode, revision)
            return {"ok": True, "catalog_revision": updated, "changed": changed}
        except ValueError:
            return {"ok": False, "message": "The resident, binding or revision changed. Refresh before saving."}
        except Exception:
            return {"ok": False, "message": "The life preference could not be saved."}

    def set_catalog_life_budget(self, enabled):
        try:
            controller = self._catalog_life_controller()
            budget = controller.budget() if controller is not None else None
            if budget is not None and type(enabled) is bool:
                if not enabled:
                    controller.invalidate()
                saved = budget.set_bucket_settings("life", enabled=enabled)
                if saved.get("ok") and saved.get("settings", {}).get("enabled") is enabled:
                    controller.enabled = enabled
                    controller.invalidate()
                    return {"ok": True, "enabled": enabled}
        except Exception:
            pass
        return {"ok": False, "message": "The Life budget change could not be confirmed."}

    def get_town_install_preview(self):
        from . import lab_state
        state = getattr(self._interpreter, "server_state", None) or {}
        return lab_state.town_install_operator(state.get("identity_integration"))

    def confirm_town_installation(self, preview):
        from . import lab_state
        state = getattr(self._interpreter, "server_state", None) or {}
        return lab_state.town_install_operator(state.get("identity_integration"), "install", preview)

    def rollback_town_installation(self, batch_id):
        from . import lab_state
        state = getattr(self._interpreter, "server_state", None) or {}
        return lab_state.town_install_operator(state.get("identity_integration"), "rollback", batch_id)

    def get_town_view(self, selected_character_id=None, catalog_revision=None):
        """Project the loaded simulator and exact identity links for the Town UI."""
        from . import lab_state
        from .world import sim

        world, journal, ticking = lab_state.capture_town_view()
        pair = self._read_identity_views(lambda: world, selected_character_id, catalog_revision)
        result = {"available": world is not None, "identity_available": pair.get("available") is True,
                  "identity_reason": pair.get("reason"), "catalog_revision": pair.get("catalog_revision"),
                  "selected_character_id": pair.get("selected_character_id")}
        if world is None:
            return result

        town = pair.get("town", {})
        duty = pair.get("duty", {})
        identity = (getattr(self._interpreter, "server_state", None) or {}).get("identity_integration")
        failures = {}
        for failure in pair.get("route_failures", ()):
            try:
                observation = identity.town_failure_state(failure)
            except Exception:
                observation = None
            if observation:
                failures.setdefault(failure["character_id"], []).append({
                    "capability": failure["capability"], "state": observation,
                })
        rows = pair.get("roster", {}).get("characters", [])
        linked = {row["placement"]["source_resident_id"]: row for row in rows
                  if row["placement"].get("source_resident_id")}
        residents = []
        for char in world.characters:
            row = linked.get(char.id)
            resident = {"source_id": char.id, "source_name": char.name,
                        "character_id": row["id"] if row else None,
                        "name": row["name"] if row else char.name,
                        "status": ("archived_link" if row["status"] == "archived" else "installed") if row else
                                  "unresolved" if result["identity_available"] else "legacy",
                        "x": char.x, "y": char.y, "activity": char.state, "room": char.room,
                        "goal": char.goal, "needs": dict(char.needs), "blocked_reason": char.blocked_reason}
            if row:
                resident["background_life"] = row["background_life"]
                resident["bindings"] = row["bindings"]
                resident["serving"] = row["duty"]
                resident["route_failures"] = failures.get(row["id"], [])
                resident["last_served"] = [cap for cap, record in duty.get("last_success", {}).items()
                                           if record["character_id"] == row["id"]]
            residents.append(resident)
        fixtures = [{"x": x, "y": y, "kind": kind} for room, items in sim.ROOM_FIXTURES.items()
                    if any(loc.id == room for loc in world.locations) for x, y, kind in items
                    if (x, y) not in world.walkable]
        snapshot = sim.snapshot(world)
        result.update(tick=world.tick, clock=world.clock, paused=world.paused, ticking=ticking,
                      controls_available=journal is not None, residents=residents, journal=journal,
                      walkable=sorted(world.walkable), locations=snapshot["locations"], fixtures=fixtures,
                      proposals=[{"character_id": row["id"], "name": row["name"],
                                  "cell": row["placement"].get("cell"), "reason": row["placement"].get("reason")}
                                 for row in town.get("residents", [])
                                 if row["placement"]["status"] in ("proposed", "unplaced")],
                      services=town.get("services", []))
        return result

    def town_transport(self, action):
        from . import lab_state

        result = lab_state.town_transport(action)
        return {"ok": result.get("ok") is True,
                "reason": None if result.get("ok") else "Town control failed or the simulation is unavailable."}

    def get_roster(self):
        """Future Lab roster: agents, real handoff edges, artifacts, approvals."""
        try:
            from . import lab_state
            lab_state.ensure_started()
            payload = lab_state.roster_snapshot(artifact_reader=self.get_artifacts)
            if not isinstance(payload, dict):
                return {"agents": [], "edges": [], "artifacts": [], "approvals": []}
            if payload.get("error"):
                return {"agents": [], "edges": [], "artifacts": [], "approvals": [],
                        "tasks": [], "error": "The task ledger could not be read."}
            agents = payload.get("agents") or []
            tasks = payload.get("tasks") or []
            edges = payload.get("edges") or []
            shelf = payload.get("artifact_shelf") or {"ok": False, "reason": "artifact shelf unavailable"}
            artifacts = [{**row, "task": row["task_id"], "file_backed": True}
                         for row in shelf.get("artifacts", [])]
            recorded = {(row["task"], row["id"]) for row in artifacts}
            file_by_id = {row["id"]: row for row in artifacts}
            for task in tasks:
                for artifact in (task.get("artifacts") or []):
                    if (task.get("id"), artifact.get("id")) in recorded:
                        continue
                    entry = dict(artifact)
                    entry["task"] = task.get("id")
                    source = file_by_id.get(entry.get("id")) if entry.get("source_task") else None
                    if source is not None:
                        entry = {**source, **entry}
                    else:
                        entry["file_state"] = ("unavailable" if entry.get("file_backed")
                                               else "historical")
                    artifacts.append(entry)
                    recorded.add((entry["task"], entry.get("id")))
            approvals = [
                {"id": t.get("id"), "goal": t.get("goal"),
                 "owner": t.get("owner"), "state": t.get("state"), "attempt": t.get("attempt"),
                 "requires_approval": True}
                for t in tasks if t.get("state") in ("awaiting_review", "approved",
                                                      "rejected")
            ]
            authority = _roster_authority()
            return {"agents": agents, "edges": edges, "artifacts": artifacts,
                    "artifact_error": None if shelf.get("ok") else
                    (shelf.get("reason") or shelf.get("error") or "artifact shelf unavailable"),
                    "approvals": approvals, "tasks": tasks,
                    "running": payload.get("running", False),
                    **_serving_summary(authority),
                    # LAB-3R6: the roster page must identify the CONCRETE
                    # configured providers/models (cast + life + serving)
                    # next to the workforce graph - two populations, clearly
                    # labelled, from the ONE authority.
                    "authority": authority}
        except Exception as error:
            return {"agents": [], "edges": [], "artifacts": [], "approvals": [],
                    "error": type(error).__name__}

    def get_world(self):
        try:
            from . import lab_state
            lab_state.ensure_started()
            payload = lab_state.world_snapshot()
            return payload if isinstance(payload, dict) else {"running": False}
        except Exception as error:
            return {"running": False, "error": type(error).__name__,
                    "characters": [], "locations": [], "last_events": []}

    def get_lab(self):
        """Living-Town pane: pixel map + live characters + authority truth.

        JSON-safe and NEVER raises: each section degrades to an explicit
        JSON-safe and NEVER raises: each section degrades to an explicit
        {"ok": False, "error": <TypeName>} instead of a plausible-looking
        guess. The authority is the ONE process-wide singleton from
        lab_authority.shared() (ROUND LAB-2B): get_core, get_ai_agents,
        get_roster and get_lab all read the same record.
        """
        out = {}
        try:
            # LAB-3R6: this page USED to render a fully populated world even
            # when the ticker never started (get_lab never ensured it) -
            # harmless-looking, but a dead ticker then shows "4 residents"
            # standing still forever. Idempotent; failures surface through
            # the health/status path, not by faking data here.
            from . import lab_state as _lab_state
            _lab_state.ensure_started()
        except Exception:
            pass
        live_ids = []
        try:
            from . import lab_map, lab_state
            world = lab_state.world_snapshot()
            out["map"] = lab_map.snapshot(live=world)
            live_ids = [c["id"] for c in world.get("characters", [])]
            # Character sprite layout (block_row / directions) lives in the SAME
            # tiles.json the map was built from. The page runs from file://,
            # where fetch() of the manifest is blocked, so it rides along here
            # instead of being fetched twice (or guessed) client-side.
            if out["map"].get("ok"):
                manifest = lab_map.load_manifest()
                if isinstance(manifest, dict) and not manifest.get("_error"):
                    out["map"].setdefault("art", {})["sprite"] = {
                        "characters": manifest.get("characters") or [],
                        "directions": manifest.get("directions") or [],
                        "frame_size": manifest.get("frame_size") or [16, 24],
                    }
        except Exception as error:
            out["map"] = {"ok": False, "error": type(error).__name__}

        try:
            from . import lab_authority as _lab_auth
            # One process-wide authority (LAB-2B). Under pytest with no
            # explicit configure() this returns None, and the projection is
            # reported honestly unavailable instead of opening the real store.
            authority = _lab_auth.shared()
            if authority is not None:
                out["authority"] = _lab_auth.public_state(authority)
                # LAB-3R5: also expose the life mode of every LIVE SIM
                # resident (their ids are not cast ids). No resident-level
                # provider classification exists yet, so `local` is null
                # here - honestly "unknown", never guessed from a name.
                try:
                    life = out["authority"].get("life") or {}
                    for cid in live_ids:
                        cid = str(cid)
                        if cid in life:
                            continue
                        override = authority.life_override(cid)
                        life[cid] = {
                            "mode": override or "npc",
                            "override": bool(override),
                            "local": None,
                            "source": "override" if override
                                      else "resident default",
                        }
                    out["authority"]["life"] = life
                except Exception:
                    pass
            else:
                out["authority"] = {"ok": False,
                                    "reason": "authority not configured"}
        except Exception as error:
            out["authority"] = {"ok": False, "error": type(error).__name__}
        return out

    def get_events(self, limit=100):
        try:
            from . import lab_state
            return lab_state.events(limit)
        except Exception as error:
            return {"events": [], "error": type(error).__name__}

    # ------------------------------------------------------------------
    # ROUND LAB-2C: budgets, usage and away/closed-town controls. Real, read/
    # write the SAME durable ledger the execution path uses; every failure
    # path reports an explicit refusal reason instead of a decorative zero.
    # ------------------------------------------------------------------
    def get_budgets(self):
        """Durable background-life settings + real usage + honest unknowns."""
        try:
            from . import lab_budget as _budget
            from . import lab_state
            lab_state.ensure_started()
            budget = _budget.shared()
            if budget is None:
                return {"ok": False, "configured": False,
                        "reason": "budget not configured (test sandbox)",
                        "not_yet_wired": list(_budget.NOT_YET_WIRED)}
            payload = {"ok": True, "configured": True}
            start, _end = budget._period_window(budget.settings("life"))
            used = budget.used("life", since=start)
            settings = budget.settings("life")
            payload["settings"] = {k: v for k, v in settings.items()
                                   if k != "enabled"}
            payload["enabled"] = settings.get("enabled", False)
            payload["usage"] = used
            payload["remaining"] = budget.effective_limits(
                "life").get("limits", {})
            payload["check"] = budget.check("life", role="worker")
            payload["rev"] = budget.rev()
            payload["presence"] = lab_state.presence()
            payload["not_yet_wired"] = list(_budget.NOT_YET_WIRED)
            return payload
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def set_life_budget(self, enabled=None, max_model_calls_day=None,
                        max_requests_day=None, max_tokens_day=None,
                        max_cost_day=None, reset_tz=None,
                        reset_period=None):
        """the user's opt-in control. Always an explicit action; never auto-run."""
        try:
            from . import lab_budget as _budget
            budget = _budget.shared()
            if budget is None:
                return {"ok": False, "configured": False,
                        "reason": "budget not configured"}
            kwargs = {}
            for key in ("enabled", "max_model_calls_day", "max_requests_day",
                        "max_tokens_day", "max_cost_day", "reset_tz",
                        "reset_period"):
                if key in (enabled, max_model_calls_day, max_requests_day,
                           max_tokens_day, max_cost_day, reset_tz,
                           reset_period):
                    pass
            kwargs["enabled"] = enabled
            kwargs["max_model_calls_day"] = max_model_calls_day
            kwargs["max_requests_day"] = max_requests_day
            kwargs["max_tokens_day"] = max_tokens_day
            kwargs["max_cost_day"] = max_cost_day
            kwargs["reset_tz"] = reset_tz
            kwargs["reset_period"] = reset_period
            return budget.set_bucket_settings("life", **kwargs)
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def set_presence(self, state):
        """Open / away / closed town. Background-only; never Exit FRIDAY."""
        try:
            from . import lab_state
            return lab_state.set_presence(str(state or ""))
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def catch_up(self, max_ticks=None):
        """Explicit, bounded, zero-model-call catch-up (deterministic)."""
        try:
            from . import lab_state
            if max_ticks is not None:
                try:
                    max_ticks = int(max_ticks)
                except Exception:
                    max_ticks = None
            return lab_state.catch_up(max_ticks=max_ticks)
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def get_usage(self):
        """Read-only: REAL usage totals for a live display; no estimates."""
        try:
            from . import lab_budget as _budget
            budget = _budget.shared()
            if budget is None:
                return {"ok": False, "configured": False,
                        "reason": "budget not configured"}
            start, _end = budget._period_window(budget.settings("life"))
            payload = budget.used("life", since=start)
            payload["ok"] = True
            return payload
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    # ------------------------------------------------------------------
    # ROUND LAB-3: character-memory inspector controls. REAL lab_memory
    # singleton, not a helper mock. Every touch is honest (returns a real
    # reason when the lab memory is unconfigured), and the private
    # Friday memory is NEVER touched.
    # ------------------------------------------------------------------
    def lab_budget_enable(self, max_requests_day=None):
        """Atomic user action: enable the durable bg-life budget."""
        try:
            from . import lab_budget as _budget
            budget = _budget.shared()
            if budget is None:
                return {"ok": False, "configured": False,
                        "reason": "budget not configured"}
            return budget.set_bucket_settings("life", enabled=True,
                                              max_requests_day=max_requests_day)
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def get_character_memory(self, char_id, limit=12):
        """Return one character's live experiences and read-only events."""
        try:
            from . import lab_memory as _lm

            if not isinstance(char_id, str) or not char_id.strip():
                raise ValueError("char_id is required")
            memory = _lm.shared()
            if memory is None:
                return {"ok": False, "configured": False,
                        "reason": "character memory not configured"}
            owner = char_id.strip()
            scrapbook = memory.scrapbook(owner, limit=max(1, min(int(limit), 50)))
            rows = scrapbook["experiences"]
            return {"ok": True, "char_id": owner, "memory_rows": rows,
                    "experiences": rows, "scrapbook": scrapbook}
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def _mutate_character_experience(self, operation, char_id, ref, *args, **options):
        from . import lab_memory as _lm

        try:
            domain, parsed = _parse_memory_ref(ref)
            if domain is None:
                return {"ok": False, "reason": parsed}
            if domain == "mem":
                return {"ok": False, "reason": "shared canonical memory is read-only here"}
            if domain == "evt":
                return {"ok": False, "reason": "journal events are read-only"}
            if not isinstance(char_id, str) or not char_id.strip():
                return {"ok": False, "reason": "char_id is required"}
            memory = _lm.shared()
            if memory is None:
                return {"ok": False, "configured": False, "reason": "memory not configured"}
            mutate = {
                "correct": memory.correct_experience,
                "forget": memory.forget_experience,
                "pin": memory.set_experience_pin,
            }[operation]
            result = mutate(parsed, *args, expect_char_id=char_id.strip(), **options)
            return {**result, "domain": "experience"}
        except ValueError as error:
            return {"ok": False, "reason": str(error)}
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def character_correct(self, char_id, ref, new_content, reason="", expected_text=None):
        options = {} if expected_text is None else {"expected_text": expected_text}
        return self._mutate_character_experience(
            "correct", char_id, ref, new_content, reason, **options)

    def character_forget(self, char_id, ref):
        return self._mutate_character_experience("forget", char_id, ref)

    def character_pin(self, char_id, ref, pinned=True):
        return self._mutate_character_experience("pin", char_id, ref, pinned)

    def lab_set_global_ai(self, enabled):
        """LAB-3R5 bulk 'AI powered': enables eligible NONLOCAL brain-lane
        residents ONLY.

        Local models are never bulk-enabled (they change only through
        `lab_set_life_mode`); the local classifier is `derive_cast()` built
        from the REAL pool inventory (config-derived pool names, never
        display names or a hardcoded provider). Returns the authority's own
        changed/skipped lists so the UI read-back is the truth.
        """
        try:
            from . import lab_authority as _la
            authority = _la.shared()
            if authority is None:
                return {"ok": False, "configured": False,
                        "reason": "authority not configured"}
            cast = _la.derive_cast()
            ids, local = [], set()
            for member in cast.get("cast", []):
                if member.get("lane") not in ("main_brain", "fallback_brain"):
                    continue          # capability lanes are not residents
                cid = str(member.get("char_id") or "")
                if not cid:
                    continue
                ids.append(cid)
                if member.get("local"):
                    local.add(cid)
            out = authority.apply_global_ai(
                bool(enabled), local_ids=tuple(ids),
                is_local=lambda cid: str(cid) in local)
            out["eligible"] = ids
            out["cast_source"] = cast.get("source")
            return out
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def lab_set_life_mode(self, char_id, mode):
        """Individual resident life mode (npc/ai/off) - the ONLY way a
        LOCAL resident ever becomes ai, and the only way any resident is
        opted back out. Delegates to the authority's validated setter."""
        try:
            from . import lab_authority as _la
            authority = _la.shared()
            if authority is None:
                return {"ok": False, "configured": False,
                        "reason": "authority not configured"}
            cid = str(char_id or "").strip()
            if not cid:
                return {"ok": False, "reason": "char_id is required"}
            return authority.set_life_override(cid, str(mode or ""))
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def get_character_journal(self, char_id, date=None):
        """Honestly scopes character journal/scrapbook to the request day."""
        try:
            from . import lab_memory as _lm
            memory = _lm.shared()
            if memory is None:
                return {"ok": False, "configured": False,
                        "reason": "memory not configured"}
            return memory.journal_recap(date or _lm._timestamp()[:10], char_id=char_id)
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def lab_budget_disable(self):
        """Atomic user action: disable the durable bg-life budget."""
        try:
            from . import lab_budget as _budget
            budget = _budget.shared()
            if budget is None:
                return {"ok": False, "configured": False,
                        "reason": "budget not configured"}
            return budget.set_bucket_settings("life", enabled=False)
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def lab_budget_pause(self):
        """Atomic user action: pause the town (bg-life ticks stop, zero cost)."""
        try:
            from . import lab_state
            return lab_state.set_presence("closed")
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def lab_budget_open(self):
        """Atomic user action: open the town (bg-life ticks resume)."""
        try:
            from . import lab_state
            return lab_state.set_presence("open")
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def get_artifacts(self, task_id=None):
        """Results shelf: real artifacts roster-attached, with honest
        file-missing/revoked state and per-task summary."""
        try:
            from . import lab_artifacts as _art_mod
            _artifacts = _art_mod.shared()
            if _artifacts is None:
                return {"ok": False, "configured": False,
                        "reason": "artifacts not configured"}
            rows = _artifacts.list(task_id=task_id)
            # build per-task summaries
            grouped = {}
            for row in rows:
                grouped.setdefault(row["task_id"], []).append(row)
            tasks = {task: _artifacts.summary(task, rows=items)
                     for task, items in grouped.items()}
            return {"ok": True, "artifacts": rows, "summaries": tasks,
                    "not_yet_wired": list(_art_mod.NOT_YET_WIRED)}
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def read_artifact(self, art_id: str):
        """Read one artifact by its opaque ID - scoped, never a caller path."""
        try:
            from . import lab_artifacts as _art_mod
            _artifacts = _art_mod.shared()
            if _artifacts is None:
                return {"ok": False, "configured": False,
                        "reason": "artifacts not configured"}
            if _artifacts.is_revoked(str(art_id or "")):
                return {"ok": False, "id": str(art_id or ""),
                        "reason": "revoked by the user"}
            file_state = _artifacts.get_file_state(str(art_id or ""))
            return file_state
        except Exception as error:
            return {"ok": False, "error": type(error).__name__}

    def get_speech(self):
        """ROUND ZB-C: caption + REAL playback acknowledgement for the HUD.

        `client_playback_observable` is true only because the output client told
        us it started writing audio to the device - the server itself runs
        muted and can never know. Everything here is measured, never inferred.
        """
        server_state = getattr(self._interpreter, "server_state", None) or {}
        try:
            from .speech_playback import speech_snapshot
            payload = speech_snapshot(server_state)
            return payload if isinstance(payload, dict) else {}
        except Exception as error:
            return {"client_playback_observable": False,
                    "error": type(error).__name__}

    def stop_turn(self, reason="ui"):
        """Always-visible Stop. Mirrors POST /chat/cancel.

        Cancellation belongs to the server-loop owner callback. Never emulate
        it by mutating shared flags from the HUD thread. An acknowledgement
        means the callback returned; it does not prove running tools terminated.
        """
        stopped, cancelled_requests = False, 0
        cancel = getattr(self._interpreter, "cancel_owned_turn", None)
        failure = None
        if self._interpreter is not None and not callable(cancel):
            failure = "Owner-scoped cancellation is unavailable. Stop was not confirmed."
        elif callable(cancel):
            try:
                stopped, cancelled_requests = cancel("hud_stop")
            except Exception:
                failure = "Owner-scoped cancellation failed. Stop was not confirmed."
        if stopped:
            try:
                from . import lab_state
                lab_state.cancel_execution("hud_stop")
            except Exception:
                pass
        try:
            from . import lab_state
            lab_state.note("stop", {"reason": str(reason or "ui"),
                                    "stopped": bool(stopped),
                                    "cancelled_requests": cancelled_requests})
        except Exception:
            pass
        result = {"ok": failure is None, "stopped_active_turn": bool(stopped),
                  "cancelled_requests": cancelled_requests}
        if failure:
            result["reason"] = failure
        return result

    def world_command(self, cmd, args=None):
        try:
            from . import lab_state
            lab_state.ensure_started()
            return lab_state.command(cmd, args or {})
        except Exception as error:
            return {"ok": False, "reason": type(error).__name__}

    def assign_task(self, agent_id, text, budget=None, approval_required=False):
        if type(approval_required) is not bool:
            return {"ok": False, "reason": "Choose an explicit review policy."}
        try:
            from . import lab_state
            lab_state.ensure_started()
            # The UI sends a single number as the budget in some builds.
            if not isinstance(budget, dict):
                try:
                    budget = {"max_steps": max(1, int(budget))}
                except Exception:
                    budget = None
            return lab_state.assign_task(agent_id, text, budget, approval_required=approval_required)
        except Exception as error:
            return {"ok": False, "reason": type(error).__name__}

    def review_task(self, task_id, verdict, attempt=None):
        if str(verdict or "").strip().lower() in {"approve", "approved", "reject", "rejected"} and (
                not isinstance(attempt, str) or not attempt.strip()):
            return {"ok": False, "reason": "Select the exact attempt awaiting review."}
        try:
            from . import lab_state
            lab_state.ensure_started()
            return lab_state.review_task(task_id, verdict, attempt=attempt)
        except Exception as error:
            return {"ok": False, "reason": type(error).__name__}

    def get_ai_agents(self):
        """Return live status, active tasks, providers, and models of all AI agents."""
        state = getattr(self._interpreter, "server_state", {}) or {}
        respond_thread = getattr(self._interpreter, "respond_thread", None)
        is_busy = respond_thread is not None and respond_thread.is_alive()

        router = state.get("brain_router")
        # getattr with a default, NOT a bare attribute read: the tier attributes
        # were removed on 2026-09-30 (there is one chain now, no tiers) and this
        # HUD method was still reading router.current_tier, so every AI-Core
        # refresh raised AttributeError. The unit tests did NOT catch it because
        # they mock the router - a mock tolerates any attribute.
        active_pool = getattr(router, "current_pool", None) if router else None
        active_model = getattr(router, "current_model", None) if router else None

        # Live "what is running / what ran last" for the HUD header, even
        # when the current turn has finished (current clears on success).
        if router is not None:
            last_pool = getattr(router, "last_pool", None) or active_pool
            last_model = getattr(router, "last_model", None) or active_model
        else:
            last_pool, last_model = active_pool, active_model
        
        try:
            from . import api_pools
            pools_stats = api_pools.describe_pools()
        except Exception:
            api_pools = None
            pools_stats = {}

        def get_pool_health(pool_name):
            info = pools_stats.get(pool_name, {})
            total = info.get("total", 0)
            banned = info.get("banned", 0)
            if total == 0:
                return "no-keys", "Unconfigured (No API Keys)"
            if banned >= total:
                return "exhausted", "Exhausted (Rate Limited / Banned)"
            if banned > 0:
                return "available", f"Available ({total - banned}/{total} Keys Active)"
            return "available", f"Available ({total}/{total} Keys Active)"

        agents = []

        # 0. Modal PRIMARY brain (self-hosted, added 2026-09-30).
        try:
            from .brain_router import MODAL_BASE, MODAL_MODEL

            modal_ready = bool(MODAL_BASE)
        except Exception:
            modal_ready, MODAL_MODEL = False, ""
        m_health, m_desc = get_pool_health("brain_modal")
        if modal_ready:
            m_in_use = is_busy and active_pool == "brain_modal"
            agents.append({
                "id": "agent-modal-primary",
                "name": "Modal Primary Brain",
                "role": "Primary dialogue, reasoning & tool control",
                "provider": "Modal (self-hosted)",
                "model": MODAL_MODEL,
                "status": "in-use" if m_in_use else m_health,
                "task": ("Answering you & executing tools" if m_in_use
                         else ("Standby (Ready)" if m_health == "available" else m_desc)),
                "description": "Primary brain and primary screen vision. Self-hosted, so no public quota to exhaust.",
                "health_desc": m_desc,
            })

        # 1. Groq Primary Conversation Agent.
        # These two Groq panels were once separated by TIER ("normal" vs "hard").
        # Tiers were removed on 2026-09-30, so they are now separated by MODEL -
        # otherwise both light up at once whenever Groq serves the turn, which
        # would make the HUD lie about what is running.
        try:
            from .brain_router import GROQ_FAST_MODEL, GROQ_STRONG_MODEL
        except Exception:
            GROQ_FAST_MODEL, GROQ_STRONG_MODEL = "openai/gpt-oss-20b", "openai/gpt-oss-120b"
        p_health, p_desc = get_pool_health("brain_groq")
        groq_active = is_busy and active_pool == "brain_groq"
        in_use = groq_active and active_model == GROQ_FAST_MODEL
        status = "in-use" if in_use else p_health
        task = "Conversing with user & executing tools" if in_use else ("Standby (Ready)" if p_health == "available" else p_desc)
        agents.append({
            "id": "agent-groq-conv",
            "name": "Groq Primary Dialogue Agent",
            "role": "Conversation & Tool Control",
            "provider": "Groq Cloud",
            "model": GROQ_FAST_MODEL,
            "status": status,
            "task": task,
            "description": "Fast cloud dialogue engine & Python tool execution.",
            "health_desc": p_desc,
        })

        # 2. Groq Deep Reasoning Agent
        in_use = groq_active and active_model == GROQ_STRONG_MODEL
        status = "in-use" if in_use else p_health
        task = "Deep reasoning & architecture analysis" if in_use else ("Standby (Ready)" if p_health == "available" else p_desc)
        agents.append({
            "id": "agent-groq-reasoning",
            "name": "Groq Deep Reasoning Agent",
            "role": "Complex Logic & Refactoring",
            "provider": "Groq Cloud",
            "model": "openai/gpt-oss-120b",
            "status": status,
            "task": task,
            "description": "High-capacity reasoning engine for difficult debugging & multi-step plans.",
            "health_desc": p_desc,
        })

        # 3. OpenRouter Backup Agent
        or_health, or_desc = get_pool_health("brain_openrouter")
        in_use = is_busy and active_pool == "brain_openrouter"
        status = "in-use" if in_use else or_health
        task = "Executing fallback cloud completion" if in_use else ("Standby (Ready)" if or_health == "available" else or_desc)
        agents.append({
            "id": "agent-openrouter-brain",
            "name": "OpenRouter Fallback Agent",
            "role": "General Brain Backup",
            "provider": "OpenRouter Cloud",
            "model": _openrouter_brain_model_display(),
            "status": status,
            "task": task,
            "description": "Secondary cloud failover for general dialogue when Groq is busy or exhausted.",
            "health_desc": or_desc,
        })

        # 4. Gemini High-Value Research Agent
        gem_health, gem_desc = get_pool_health("brain_gemini")
        in_use = is_busy and active_pool == "brain_gemini"
        status = "in-use" if in_use else gem_health
        task = "Performing high-stakes research & analysis" if in_use else ("Standby (Ready)" if gem_health == "available" else gem_desc)
        gem_chain_label = "gemini-3.8-flash -> 3.7 -> 3.5-flash -> 3.5/3.1 flash-lite"
        try:
            from . import brain_router as _br
            gem_chain_label = " -> ".join(m.replace("gemini-", "") for m in _br.GEMINI_MODEL_CHAIN)
        except Exception:
            pass
        agents.append({
            "id": "agent-gemini-research",
            "name": "Gemini Research & Large-Context Agent",
            "role": "Research & Explicit Override",
            "provider": "Google Gemini Cloud",
            "model": gem_chain_label,
            "status": status,
            "task": task,
            "description": "Per-model quota chain on ONE account (strongest first); a hot model falls to the next free model.",
            "health_desc": gem_desc,
        })

        # 5. Local Qwen3 Offline Brain
        in_use = is_busy and active_pool == "local"
        status = "in-use" if in_use else "standby"
        task = "Executing local Qwen3 inference" if in_use else "Configured local fallback"
        agents.append({
            "id": "agent-local-qwen",
            "name": "Local Qwen3 Offline Brain",
            "role": "Offline Dialogue & Tools",
            "provider": "Local Ollama Engine",
            "model": "ollama_chat/qwen3:8b",
            "status": status,
            "task": task,
            "description": "Zero-quota local baseline model running on CPU/RAM.",
            "health_desc": "Configured local fallback; readiness is verified when used",
        })

        # 6. Gemini vision fallback after primary self-hosted Modal vision.
        gv_health, gv_desc = get_pool_health("vision_gemini")
        vision_active = state.get("active_vision_pool") == "active"
        in_use = vision_active and gv_health == "available"
        status = "in-use" if in_use else gv_health
        task = "Analyzing active screen capture" if in_use else ("Standby (Ready)" if gv_health == "available" else gv_desc)
        gv_chain_label = gem_chain_label
        try:
            from . import screen_understanding as _su
            gv_chain_label = " -> ".join(m.replace("gemini-", "") for m in _su.VISION_GEMINI_CHAIN)
        except Exception:
            pass
        agents.append({
            "id": "agent-gemini-vision",
            "name": "Gemini Vision Fallback Agent",
            "role": "Cloud Screen Perception Fallback",
            "provider": "Google Gemini Cloud",
            "model": gv_chain_label,
            "status": status,
            "task": task,
            "description": "Cloud fallback after primary Modal vision; hot Gemini models fall over to the next free model.",
            "health_desc": gv_desc,
        })

        # 7. OpenRouter Backup Vision Agent
        ov_health, ov_desc = get_pool_health("vision_openrouter")
        in_use = vision_active and gv_health != "available" and ov_health == "available"
        status = "in-use" if in_use else ov_health
        task = "Analyzing active screen capture (fallback)" if in_use else ("Standby (Ready)" if ov_health == "available" else ov_desc)
        agents.append({
            "id": "agent-openrouter-vision",
            "name": "OpenRouter Backup Vision Agent",
            "role": "Visual Perception Fallback",
            "provider": "OpenRouter Cloud",
            # Round N (2026-10-03): this used to be a HARDCODED
            # `meta-llama/llama-3.2-11b-vision-instruct:free`, which OpenRouter
            # has retired - so the HUD told Sir the fallback was one model while
            # the code used another. Read the real configured value instead, so
            # the display cannot drift from the request again.
            "model": _openrouter_vision_model_display(),
            "status": status,
            "task": task,
            "description": "Additional cloud visual fallback after Modal and Gemini vision.",
            "health_desc": ov_desc,
        })

        # 8. Groq Fast Ear STT Agent
        stt_health, stt_desc = get_pool_health("stt_groq")
        stt_active = state.get("active_stt_pool") == "active"
        in_use = stt_active and stt_health == "available"
        status = "in-use" if in_use else stt_health
        task = "Transcribing voice PCM audio stream" if in_use else ("Standby (Ready)" if stt_health == "available" else stt_desc)
        agents.append({
            "id": "agent-groq-stt",
            "name": "Groq Fast Ear STT Agent",
            "role": "Cloud Speech Recognition",
            "provider": "Groq Cloud",
            "model": "whisper-large-v3-turbo",
            "status": status,
            "task": task,
            "description": "Ultra low-latency cloud STT model for transcribing spoken user commands.",
            "health_desc": stt_desc,
        })

        # 9. Deepgram Emergency Ear STT Agent
        dg_health, dg_desc = get_pool_health("stt_deepgram")
        in_use = stt_active and stt_health != "available" and dg_health == "available"
        status = "in-use" if in_use else dg_health
        task = "Transcribing voice PCM audio stream (fallback)" if in_use else ("Standby (Ready)" if dg_health == "available" else dg_desc)
        agents.append({
            "id": "agent-deepgram-stt",
            "name": "Deepgram Emergency Ear Agent",
            "role": "Cloud STT Emergency Fallback",
            "provider": "Deepgram Cloud",
            "model": "nova-3",
            "status": status,
            "task": task,
            "description": "Secondary emergency cloud STT if Groq STT pool is exhausted.",
            "health_desc": dg_desc,
        })

        # 10. Local RealtimeSTT Ear
        in_use = stt_active and stt_health != "available" and dg_health != "available"
        status = "in-use" if in_use else "standby"
        task = "Local Whisper transcription fallback" if in_use else "Configured local recorder fallback"
        agents.append({
            "id": "agent-local-whisper",
            "name": "Local RealtimeSTT Ear Process",
            "role": "Offline Speech Recognition",
            "provider": "Local Faster-Whisper",
            "model": "whisper-base (RealtimeSTT)",
            "status": status,
            "task": task,
            "description": "Zero-quota local speech recorder running in a separate process.",
            "health_desc": "Configured local process; readiness depends on the recorder connection",
        })

        # 11. Gemini Transcribe Stage (reuses both Gemini accounts)
        gs_health, gs_desc = get_pool_health("gemini_stt")
        gs_status = gs_health if gs_health != "no-keys" else "available"
        agents.append({
            "id": "agent-gemini-stt",
            "name": "Gemini Transcribe Ear Agent",
            "role": "Cloud STT Third Stage",
            "provider": "Google Gemini Cloud",
            "model": "gemini-3.5-transcribe",
            "status": gs_status,
            "task": "Transcribing voice on both Gemini accounts (after Deepgram)",
            "description": "Per-account/per-model quota bucket: lives on the Brain+Vision Gemini accounts without touching their chat/vision usage.",
            "health_desc": gs_desc,
        })

        # 12/13. Cloud-first TTS: Gemini on both accounts, then local voice.
        tts_model = "gemini-3.1-flash-tts-preview"
        tts_voice = "Sulafat"
        try:
            from . import gemini_tts as _gt
            tts_model = _gt.GEMINI_TTS_MODEL_CHAIN[0]
            tts_voice = _gt.GEMINI_TTS_VOICE
            tts_stage = getattr(_gt, "LAST_STAGE", None)
        except Exception:
            tts_stage = None
        gt_health, gt_desc = get_pool_health("gemini_tts")
        gt_status = "in-use" if gt_health != "no-keys" and tts_stage == "gemini" else gt_health
        agents.append({
            "id": "agent-gemini-tts",
            "name": "Gemini Voice Agent",
            "role": "Cloud Text-to-Speech (Stages 1+2)",
            "provider": "Google Gemini Cloud",
            "model": tts_model,
            "status": gt_status,
            "task": "Speaking FRIDAY's reply" if gt_status == "in-use" else ("Standby (Ready)" if gt_health == "available" else gt_desc),
            "description": f"Cloud-first voice: both dedicated Gemini accounts speak the female '{tts_voice}' voice.",
            "health_desc": gt_desc,
        })
        agents.append({
            "id": "agent-local-edge-tts",
            "name": "Local Edge-TTS Voice",
            "role": "Offline Voice Fallback",
            "provider": "Local Edge-TTS Server",
            "model": "neural voice (localhost:5050)",
            "status": "in-use" if tts_stage == "local" else "standby",
            "task": "Speaking with the local neural voice" if tts_stage == "local" else "Standby (speaks when both Gemini TTS stages are hot)",
            "description": "The original FRIDAY voice; starts lazily the first time the cloud chain fails.",
            "health_desc": "Configured fallback; availability is verified when used",
        })

        # 14. Optional n8n workflow automation. OFF by default; when it is not
        # connected FRIDAY simply does the work with her own built-in resources,
        # so this panel reports "unconfigured" rather than a fault.
        n8n_status = "no-keys"
        n8n_desc = "Unconfigured (No n8n Workflows)"
        n8n_workflows = 0
        try:
            from . import n8n_runtime as _n8n

            _adapter = _n8n.get_adapter()
            _snap = _adapter.status()
            n8n_workflows = _snap.get("workflow_count", 0)
            if not _snap.get("enabled"):
                n8n_desc = "Not Connected (FRIDAY uses her own tools)"
            else:
                reachable = _adapter.available().get("connected")
                n8n_status = "available" if reachable else "exhausted"
                n8n_desc = (f"Connected - {n8n_workflows} workflow(s) available"
                            if reachable else "Configured but not answering")
        except Exception:
            pass
        agents.append({
            "id": "agent-n8n-workflows",
            "name": "n8n Workflow Automation",
            "role": "External Workflow Engine (Optional)",
            "provider": "n8n",
            "model": f"{n8n_workflows} workflow(s)" if n8n_workflows else "none configured",
            "status": n8n_status,
            "task": "Ready to hand over workflow tasks" if n8n_status == "available" else "Standby (FRIDAY handles the task herself)",
            "description": "Optional external automation server. Off until the owner connects it; FRIDAY never depends on it.",
            "health_desc": n8n_desc,
        })

        try:
            from . import lab_authority
            authority = lab_authority.shared()
            serving = _serving_summary(authority.snapshot() if authority else {})
        except Exception:
            serving = _serving_summary({})
        return {
            "agents": agents,
            "summary": {
                "total": len(agents),
                "active_turn": bool(is_busy),
                "active_model": active_model or last_model or "None (Standby)",
                "active_provider": active_pool or last_pool or "None (Standby)",
                # ROUND LAB-2B: served-by truth from the ONE authority. The
                # summary pills below used to be derived independently here;
                # now the badge identity and its revision come from the same
                # record get_core / get_roster / get_lab read.
                **serving,
                # Tiers were removed 2026-09-30: there is ONE chain, no tier.
                # The old pill read summary.active_tier and, with the key gone,
                # app.js fell back to a hard-coded "NORMAL" - i.e. the HUD lied
                # permanently. It now shows the real position in the chain,
                # which is what that pill was actually for.
                "chain_position": _chain_position(active_pool or last_pool),
                "last_switch": getattr(router, "last_switch", None) or "No provider switches yet.",
                # FIX (2026-09-29): this was gated on `"api_pools" in
            # str(type(router).__module__)`, but a BrainRouter's module is
            # "source.server.brain_router", so the condition was never true and
            # the per-model cooldown display was permanently empty. api_pools is
            # already imported defensively above, so call it directly.
            "exhaustion_ledger": api_pools.ledger() if api_pools is not None else {},
            }
        }


def _project_version():
    """Read the project version from pyproject without importing packaging."""
    root = Path(__file__).resolve().parents[2]
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _main_hud_window():
    """The HUD window by TITLE, never `windows[0]`.

    The floating globe is also a pywebview window, so index order is ambiguous:
    tray "Open App"/"Close"/"Exit" must act on the app, not on the globe.
    """
    try:
        import webview
        for window in (getattr(webview, "windows", None) or []):
            try:
                if (window.title or "").strip() == "FRIDAY":
                    return window
            except Exception:
                continue
    except Exception:
        pass
    return None


def _log_native_windows(delay=6.0):
    """Print which FRIDAY native windows really exist once the loop is up.

    Written for Sir's 2026-11-04 report: pywebview can hold a Window object
    that never becomes a native form, and only the native form can be shown,
    hidden, dragged or clicked. This makes that visible in the boot log.
    """
    import time as _t

    _t.sleep(delay)
    try:
        import win32gui
        rows = []

        def _cb(hwnd, _):
            try:
                title = win32gui.GetWindowText(hwnd)
            except Exception:
                return True
            if title.startswith("FRIDAY"):
                rows.append((hwnd, title,
                             bool(win32gui.IsWindowVisible(hwnd))))
            return True

        win32gui.EnumWindows(_cb, None)
        print("[status-ui] native windows after start: %r" % (rows,),
              flush=True)
    except Exception as error:
        print("[status-ui] window probe failed: %s" % error, flush=True)


# --- MAIN-window geometry memory (2026-11-05) --------------------------------
# LAB-3R7: the HUD's window title is a module constant so tests (and the
# isolated native harness) can retarget FindWindow — the geometry watchdog
# used to grab the LIVE production HUD's window while tests ran, writing
# production rects into a test fixture and failing 4 tests. Production value
# is unchanged: "FRIDAY".
HUD_WINDOW_TITLE = "FRIDAY"
# Retain the normal native resizable frame. window_integration removes only
# its caption after the exact WinForms form is ready, preserving resize
# edges and minimum dimensions. If adaptation fails, native chrome remains.
# No frameless/easy-drag regression: explicit header drag invokes OS movement,
# never a mismatched CSS selector or a whole-page draggable region.


def _hud_geometry_store_path():
    """Where the MAIN window's last geometry lives: ~/.friday/hud_window.json.

    Runtime-only. This file is NEVER touched at import time or by the test
    suite - the only callers run inside the live webview flow (window
    creation and the watchdog daemon thread below), guarded on every level.
    """
    import os as _os
    return _os.path.join(_os.path.expanduser("~"), ".friday", "hud_window.json")


def _sanitize_hud_geometry(data):
    """Validate a stored geometry dict; None means 'distrust it / ignore it'."""
    if not isinstance(data, dict):
        return None
    x, y, width, height = (data.get(k) for k in ("x", "y", "width", "height"))
    for value in (x, y, width, height):
        # bool is an int subclass - reject it explicitly so 'true' in JSON
        # can never become a coordinate.
        if (not isinstance(value, int) or isinstance(value, bool)
                or abs(value) > 100000):
            return None
    if width < 200 or height < 200 or width > 16000 or height > 16000:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _load_hud_geometry():
    try:
        import json as _json
        with open(_hud_geometry_store_path(), "r", encoding="utf-8") as _f:
            return _sanitize_hud_geometry(_json.load(_f))
    except Exception:
        return None


def _virtual_screen_bounds():
    """(x, y, width, height) of the whole virtual screen, or None if unknown."""
    try:
        import win32api as _a
        import win32con as _c
        x = _a.GetSystemMetrics(getattr(_c, "SM_XVIRTUALSCREEN", 76))
        y = _a.GetSystemMetrics(getattr(_c, "SM_YVIRTUALSCREEN", 77))
        width = _a.GetSystemMetrics(getattr(_c, "SM_CXVIRTUALSCREEN", 78))
        height = _a.GetSystemMetrics(getattr(_c, "SM_CYVIRTUALSCREEN", 79))
        if width > 0 and height > 0:
            return int(x), int(y), int(width), int(height)
    except Exception:
        pass
    return None


def _clamp_hud_geometry(geo):
    """Clamp a restored geometry into the CURRENT virtual screen.

    Covers monitor changes and docking: at least a strip of the window must
    stay on-screen on every axis, or the title bar could sit outside the
    visible desktop and Sir would have no way to drag the window back.
    """
    if not geo:
        return geo
    try:
        bounds = _virtual_screen_bounds()
        if not bounds:
            return geo
        bx, by, bw, bh = bounds
        width = max(760, min(geo["width"], max(bw, 760)))
        height = max(560, min(geo["height"], max(bh, 560)))
        # 80px minimum visible horizontally; vertically a bit more so the
        # title bar itself is on a monitor.
        x = min(max(geo["x"], bx + 80 - width), bx + bw - 80)
        y = min(max(geo["y"], by + 120 - height), by + bh - 120)
        return {"x": x, "y": y, "width": width, "height": height}
    except Exception:
        return geo


def _clamped_hud_geometry():
    return _clamp_hud_geometry(_load_hud_geometry())


def _hud_window_geometry_kwargs():
    """Geometry kwargs for the MAIN window (LAB-3R5).

    A missing or malformed hud_window.json is NORMAL on first boot: fall
    back to defaults instead of raising. The 2026-11-06 defect was
    `geo.get(...)` on a None geometry -> AttributeError -> 'HUD create
    failed' -> Tk fallback window.
    """
    geo = _clamped_hud_geometry() or {}
    return dict(
        x=geo.get("x"),
        y=geo.get("y"),
        width=geo.get("width") or 1100,
        height=geo.get("height") or 780,
        min_size=(760, 560),
    )


def _hud_window_state(hwnd):
    """'min' | 'max' | None using only APIs in the INSTALLED pywin32.

    `win32gui.IsZoomed` does NOT exist in this build (both the venv and the
    system interpreter were probed 2026-11-06) - calling it raised
    AttributeError every watchdog tick, swallowed by a bare except, so
    geometry persistence was silently dead. Minimized uses IsIconic;
    maximized uses GetWindowPlacement's showCmd with a WS_MAXIMIZE style
    fallback. Extremes are reported so the watchdog never persists them as
    normal geometry.
    """
    import win32gui as _w
    try:
        if _w.IsIconic(hwnd):
            return "min"
    except Exception:
        pass
    try:
        import win32con as _c
        placement = _w.GetWindowPlacement(hwnd)
        if placement and int(placement[1]) == int(
                getattr(_c, "SW_SHOWMAXIMIZED", 3)):
            return "max"
    except Exception:
        pass
    try:
        import win32con as _c
        style = _w.GetWindowLong(hwnd, _c.GWL_STYLE)
        if style & getattr(_c, "WS_MAXIMIZE", 0x1000000):
            return "max"
    except Exception:
        pass
    return None


def _save_hud_geometry(geo):
    try:
        import json as _json
        import os as _os
        import tempfile as _tf
        path = _hud_geometry_store_path()
        folder = _os.path.dirname(path)
        _os.makedirs(folder, exist_ok=True)
        handle, tmp = _tf.mkstemp(dir=folder, suffix=".tmp")
        try:
            with _os.fdopen(handle, "w", encoding="utf-8") as _f:
                _json.dump(geo, _f)
            _os.replace(tmp, path)
        except Exception:
            try:
                _os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def _hud_geometry_tick(hwnd, last):
    """One watchdog iteration; returns the new 'last saved' value.

    Extracted (LAB-3R5) so tests can drive it directly. Raises OSError when
    the write fails so the caller can surface a real diagnostic instead of
    the old silent `except: continue` that hid the IsZoomed death forever.
    """
    if not hwnd:
        return last
    import win32gui as _w
    state = _hud_window_state(hwnd)
    if state in ("min", "max"):
        # never persist minimized/maximized bounds as normal geometry
        return last
    left, top, right, bottom = _w.GetWindowRect(hwnd)
    cleaned = _sanitize_hud_geometry({
        "x": int(left), "y": int(top),
        "width": int(right - left), "height": int(bottom - top),
    })
    if not cleaned or cleaned["width"] < 760 or cleaned["height"] < 560:
        return last
    if cleaned == last:
        return last
    if not _save_hud_geometry(cleaned):
        raise OSError("writing hud_window.json failed")
    return cleaned


def _hud_geometry_watchdog(poll=2.0, stop=None):
    """Daemon: persist the MAIN window's geometry while FRIDAY runs.

    Reads the native hwnd only - Win32 GetWindowRect/FindWindow are safe
    from any thread; pywebview's Window methods are deliberately NOT used
    here because they must run on the GUI thread (the 2026-11-04 tray
    lesson). Minimized and maximized frames are skipped so the stored
    geometry is always a real window rect that can be restored as-is.

    Errors print ONCE per distinct message (LAB-3R5) instead of vanishing
    into a bare `except: continue`. `stop` is an optional threading.Event
    for tests; production passes nothing and runs forever.
    """
    import time as _t
    last = None
    last_error = None
    while True:
        _t.sleep(poll)
        if stop is not None and stop.is_set():
            return
        try:
            import win32gui as _w
            hwnd = _w.FindWindow(None, HUD_WINDOW_TITLE)
            last = _hud_geometry_tick(hwnd, last)
        except Exception as error:
            message = "%s: %s" % (type(error).__name__, error)
            if message != last_error:
                last_error = message
                print("[status-ui] geometry watchdog: %s" % message,
                      flush=True)


def run_status_window(status_bus, interpreter=None, start_hidden=False):
    """Run the modern webview UI on the process main thread.

    start_hidden=True (registry auto-start): the window is created hidden —
    FRIDAY boots in the system tray only; the tray 'Open App' shows it.
    """
    tray_controller = None
    companion_controller = None
    try:
        import webview
        from .window_integration import bind_window_controls, prepare_taskbar_identity

        if not UI_ENTRYPOINT.is_file():
            raise FileNotFoundError(UI_ENTRYPOINT)

        # Process-local shell grouping, before globe/HUD forms are created.
        # This does not edit the executable, registry or launcher/installer.
        prepare_taskbar_identity()

        tray_console = {"open": False, "proc": None}

        hud_window = None
        hud_geometry_thread = None

        def _hud_window_kwargs(*, hidden, bridge):
            """Shared creation kwargs for the MAIN window (boot + tray
            recreate), so both paths stay identical.

            Start with functional native chrome; the ready-form adapter
            removes only its caption. Native sizing edges remain enabled.
            Header controls/drag appear only when that adapter is ready.

            Geometry comes from the module-level helper (LAB-3R5) so a
            missing/malformed hud_window.json falls back to defaults
            instead of crashing window creation (the 2026-11-06 defect:
            geo was None -> AttributeError -> Tk fallback).
            """
            return dict(
                js_api=bridge,
                **_hud_window_geometry_kwargs(),
                background_color="#f6f5ee",
                frameless=False,
                easy_drag=False,
                hidden=hidden,
            )

        def _on_hud_closed():
            """Fallback after a genuinely destroyed HUD: hide its globe.

            The window integration vetoes ordinary system Close/Alt+F4 and
            hides the existing form. If destruction occurs independently,
            the existing tray recreation path still rebuilds the HUD.
            """
            try:
                from . import lab_state as _ls
                _ls.set_presence("away", visibility=True)
            except Exception:
                pass
            try:
                import win32gui as _w
                import win32con as _c
                globe = _w.FindWindow(None, "FRIDAY Globe")
                if globe:
                    _w.ShowWindow(globe, _c.SW_HIDE)
            except Exception:
                pass

        # -- app window control, by HWND -----------------------------------
        # Sir reported (2026-11-04) that tray "Open App" and "Close App" both
        # did nothing. Two causes, both reproduced:
        #   * the callbacks called pywebview's `window.show()/hide()`. Those
        #     must run on the GUI thread; from the tray/HTTP thread they block.
        #     Measured live: `GET /open_app` (same loop) hung for the full 8s
        #     timeout while `/ping` answered in 361ms - a healthy server with a
        #     wedged window call.
        #   * no native "FRIDAY" window existed at all, so even a non-blocking
        #     call had nothing to act on (see `_ensure_hud_window`).
        # Win32 ShowWindow/SetForegroundWindow are safe from any thread, so all
        # app-window control goes through the hwnd instead of pywebview.
        def _hud_hwnd():
            try:
                import win32gui
                return win32gui.FindWindow(None, HUD_WINDOW_TITLE)
            except Exception:
                return 0

        def _ensure_hud_window(timeout=15.0):
            """Return the HUD hwnd, creating the window on demand if missing.

            pywebview registers the Window object at `create_window` time but
            only makes the native form during `webview.start()`. If that form
            never appeared (exactly what a live window-map showed: the process
            owned "FRIDAY Globe" and nothing else), creating the window while
            the loop is already pumping still works - verified with a probe
            that created a window 4s after start() and found its native form.
            """
            nonlocal hud_window
            hwnd = _hud_hwnd()
            if hwnd:
                return hwnd
            try:
                import webview
                registered = [
                    (w.title or "").strip()
                    for w in (getattr(webview, "windows", None) or [])
                ]
                if "FRIDAY" not in registered:
                    bridge = WebviewBridge(status_bus, interpreter)
                    recreated = webview.create_window(
                        "FRIDAY",
                        UI_ENTRYPOINT.as_uri(),
                        **_hud_window_kwargs(hidden=False, bridge=bridge),
                    )
                    hud_window = recreated
                    bind_window_controls(recreated, bridge, _hide_app)
                    try:
                        recreated.events.closed += _on_hud_closed
                    except Exception:
                        pass
                import time as _t
                deadline = _t.time() + timeout
                while _t.time() < deadline:
                    hwnd = _hud_hwnd()
                    if hwnd:
                        break
                    _t.sleep(0.2)
            except Exception as error:
                print("[status-ui] HUD create failed: %s" % error, flush=True)
            return hwnd

        def _show_app():
            """Show + focus the HUD. Win32: callable from any thread."""
            # ROUND LAB-2C: presence wiring (real lifecycle caller). The
            # currently-visible state is `open`; `away`/`closed` is for the
            # tray/Town pane. The assistant is NEVER affected.
            try:
                from . import lab_state as _ls
                _ls.set_presence("open", visibility=True)
            except Exception:
                pass
            hwnd = _ensure_hud_window()
            if not hwnd:
                print("[status-ui] Open App: no HUD window", flush=True)
                return False
            try:
                import win32con
                import win32gui
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                note_window_open()
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    try:
                        win32gui.BringWindowToTop(hwnd)
                    except Exception:
                        pass
                return True
            except Exception as error:
                print("[status-ui] Open App failed: %s" % error, flush=True)
                return False

        def _hide_app(expected_hwnd=None):
            """Hide the HUD (and the globe - see the visibility rule below)."""
            # ROUND LAB-2C: leaving the HUD visible is a lifecycle event: the
            # bg-life tick pauses with ZERO model calls (never Exit FRIDAY).
            hwnd = _hud_hwnd()
            # A retained old bridge must not hide a replacement HUD or
            # change its presence. Tray callbacks intentionally pass None.
            if expected_hwnd is not None and hwnd != expected_hwnd:
                return False
            try:
                from . import lab_state as _ls
                _ls.set_presence("away", visibility=True)
            except Exception:
                pass
            hud_hidden = False
            if hwnd:
                try:
                    import win32con
                    import win32gui
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                    hud_hidden = True
                    if _WINDOW_OPENING is not None:
                        _WINDOW_OPENING.hidden()
                except Exception:
                    pass
            # VISIBILITY RULE (documented for Sir): the globe follows the app.
            # "Close App" clears FRIDAY's whole visible footprint - HUD and
            # globe - while voice/backend keep running; "Open App" brings both
            # back. The globe's own hotkey still restores it on its own.
            globe_hidden = False
            try:
                import win32gui as _w
                globe = _w.FindWindow(None, "FRIDAY Globe")
                if globe:
                    import win32con as _c
                    _w.ShowWindow(globe, _c.SW_HIDE)
                    globe_hidden = True
            except Exception:
                pass
            return hud_hidden if expected_hwnd is not None else hud_hidden or globe_hidden

        def _show_globe():
            try:
                import win32gui as _w
                globe = _w.FindWindow(None, "FRIDAY Globe")
                if globe:
                    import win32con as _c
                    _w.ShowWindow(globe, _c.SW_SHOW)
                    return True
            except Exception:
                pass
            return False

        def tray_on_open_app():
            import subprocess as _sub

            # Win32 show+focus; never pywebview's show() from this thread.
            _show_app()
            _show_globe()
            # The CMD panel is WELCOME on an explicit "Open App" — it appears
            # HERE and only here (never at silent background boot).
            if not tray_console["open"]:
                from .file_logger import log_file_path as _log_path

                try:
                    tray_console["proc"] = _sub.Popen(
                        [
                            "powershell", "-NoExit", "-Command",
                            "Get-Content -Encoding UTF8 -Path", f"'{str(_log_path())}'",
                            "-Wait", "-Tail", "60",
                        ],
                        creationflags=0,
                    )
                    tray_console["open"] = True
                except Exception:
                    pass

        def tray_on_close_app():
            # 'Close App (background)': hide the HUD AND the globe, and close
            # the console panel spawned at "Open App" — FRIDAY keeps running
            # in the tray (voice/backend untouched; this is NOT Exit).
            tray_console["open"] = False
            _hide_app()

        def tray_on_exit():
            # Tell the start_friday.ps1 supervisor this was an INTENTIONAL stop
            # so it disarms the auto-relaunch loop instead of bringing FRIDAY
            # right back. Then hard-exit (there is no graceful downs-instance).
            import os as _os

            try:
                _stop = _os.environ.get("FRIDAY_STOP_FILE") or _os.path.join(_os.path.expanduser("~"), ".friday", "stop_friday.flag")
                _os.makedirs(_os.path.dirname(_stop), exist_ok=True)
                with open(_stop, "w", encoding="utf-8") as _f:
                    _f.write("tray exit - intentional stop")
            except Exception:
                pass
            from .light_lifecycle import cleanup_light
            if companion_controller is not None:
                companion_controller.stop()
            cleanup_light(interpreter)
            _os._exit(0)

        from .startup_settings import start_tray

        tray_controller = start_tray(
            on_open_app=tray_on_open_app,
            on_close_app=tray_on_close_app,
            on_exit=tray_on_exit,
        )

        # ROUND ZB-C1: the floating desktop globe — created BEFORE the HUD, on
        # purpose. pywebview routes a second window's content through the HTTP
        # server it starts for the FIRST window, so with the HUD's file:// URL
        # registered first a live capture showed the globe serving pywebview's
        # own 404 page instead of its markup. Creating the globe first is what
        # makes its content resolve. `_main_hud_window()` picks the HUD by
        # title, so the tray still controls the app regardless of creation
        # order, and the globe is never focused (focus=False in open_globe).
        def _globe_open_app():
            # Double-click on the globe must open/focus the full app. The old
            # version used pywebview's restore()/show() from the js_api worker
            # thread - the same cross-thread call that wedged the tray menu.
            try:
                shown = _show_app()
                if shown:
                    _show_globe()
                return shown
            except Exception:
                return False

        def _globe_speech():
            """REAL playback acknowledgement for the globe's bounce.

            Same source the HUD captions use, so the two can never disagree.
            The server runs muted: `client_playback_observable` is true only
            because the output client reported writing audio to the device.
            """
            server_state = getattr(interpreter, "server_state", None) or {}
            try:
                from . import server as _srv
                payload = _srv.speech_snapshot(server_state)
                return payload if isinstance(payload, dict) else {
                    "client_playback_observable": False}
            except Exception:
                return {"client_playback_observable": False}

        try:
            from . import globe_window as _globe
            _globe_result = _globe.open_globe(controller=_globe.GlobeController(),
                                              on_open=_globe_open_app,
                                              speech_provider=_globe_speech)
            # Printed, not logged: the file log is not configured this early and
            # the chosen URL/markup is exactly what a blank globe needs to debug.
            print("[status-ui] globe: %s" % _globe_result, flush=True)
        except Exception as globe_error:
            print(f"[status-ui warning] globe unavailable: {globe_error}",
                  flush=True)

        try:
            bridge = WebviewBridge(status_bus, interpreter)
            hud_window = webview.create_window(
                "FRIDAY",
                UI_ENTRYPOINT.as_uri(),
                **_hud_window_kwargs(hidden=start_hidden, bridge=bridge),
            )
            bind_window_controls(hud_window, bridge, _hide_app)
            _bind_window_opening(hud_window, bridge)

            from .audio_startup import enabled as _diagnostic_audio
            if not _diagnostic_audio():
                try:
                    from .companion import for_runtime
                    from .companion_native import attach, runtime_activity
                    def _companion_hud_visible():
                        import win32gui
                        hwnd = _hud_hwnd()
                        return bool(hwnd and win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd))
                    companion_controller = attach(for_runtime(interpreter, create=True), webview,
                        on_open=_show_app, visible=_companion_hud_visible, start_hidden=start_hidden,
                        activity=lambda: runtime_activity(interpreter, status_bus))
                except Exception:
                    print("[companion] Optional native presentation unavailable.", flush=True)

            try:
                hud_window.events.closed += _on_hud_closed
            except Exception as closed_error:
                print("[status-ui] HUD closed-handler not attached: %s"
                      % closed_error, flush=True)
            else:
                # Diagnostic continuity with the registered-windows print:
                # record that the X-button harm-reduction hook is installed.
                print("[status-ui] HUD closed-handler attached (X = hide, "
                      "FRIDAY stays in the tray)", flush=True)

            # Diagnostic for Sir's 2026-11-04 report ("Open App does nothing"):
            # record what pywebview actually holds BEFORE the loop starts, so a
            # boot where the HUD form never appears can be told apart from one
            # where it appears and is later hidden.
            print("[status-ui] registered windows before start: %r" % (
                [(w.title or "") for w in
                 (getattr(webview, "windows", None) or [])],), flush=True)
        except Exception as hud_error:
            print("[status-ui] HUD create failed: %s" % hud_error, flush=True)
            hud_window = None

        # Geometry watchdog: remembers Sir's last window rect (win32-safe,
        # GUI-thread independence). Started once, regardless of whether the
        # create above succeeded - the hwnd lookup simply never fires on a
        # boot without an HUD form.
        if not hud_geometry_thread:
            try:
                hud_geometry_thread = threading.Thread(
                    target=_hud_geometry_watchdog, daemon=True,
                    name="hud-geometry")
                hud_geometry_thread.start()
            except Exception as watchdog_error:
                print("[status-ui] geometry watchdog not started: %s"
                      % watchdog_error, flush=True)

        # LAB-3R6: resume free NPC life at a visible boot unless the user
        # explicitly paused it (see _boot_resume_presence for the rationale).
        _boot_resume_presence(start_hidden)
        threading.Thread(target=_log_native_windows, daemon=True,
                         name="window-probe").start()
        webview.start(debug=False)
    except Exception as error:
        print(f"[status-ui warning] Modern webview unavailable: {error}", flush=True)
        status_bus.publish(STATUS_ERROR, f"Status window unavailable: {error}")
        FaceStatusWindow(status_bus, interpreter).run()
    finally:
        if companion_controller is not None:
            companion_controller.stop()
        if tray_controller is not None:
            stop_tray = tray_controller.get("stop")
            if callable(stop_tray):
                stop_tray()


def start_status_window(status_bus, interpreter=None):
    """Backward-compatible threaded fallback; prefer run_status_window()."""
    thread = threading.Thread(
        target=run_status_window,
        args=(status_bus, interpreter),
        name="friday-status-ui",
        daemon=True,
    )
    thread.start()
    return thread
