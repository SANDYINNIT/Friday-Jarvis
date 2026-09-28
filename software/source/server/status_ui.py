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

    def append(self, role, content, status="complete", timestamp=None):
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
        with self._lock:
            self._messages.append(message)
        return dict(message)

    def snapshot(self):
        with self._lock:
            return [dict(message) for message in self._messages]

    def merge_stream(self, content):
        """Append an incremental assistant chunk onto the last assistant row."""
        content = str(content or "")[:12000]
        if not content:
            return None
        with self._lock:
            if self._messages and self._messages[-1]["role"] == "assistant":
                message = self._messages[-1]
                message["content"] = (message["content"] + content)[:12000]
                message["status"] = "streaming"
                return dict(message)
        return self.append("assistant", content, status="streaming")

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
    vision_model = os.environ.get("FRIDAY_VISION_MODEL", "qwen2.5vl:3b")
    from .model_registry import MODEL_REGISTRY, VISION, select_model
    from .semantic_memory import semantic_memory_enabled
    selected_vision = select_model(VISION)
    tts_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:5050/v1")
    return {
        "model": model,
        "vision model": vision_model,
        "vision registry selection": selected_vision.name if selected_vision else "unavailable",
        "model registry": f"{len(MODEL_REGISTRY)} explicit models",
        "memory DB": str(_memory_db_path()),
        "semantic embeddings": "configured (explicit calls only)" if semantic_memory_enabled() else "disabled; keyword fallback",
        "TTS URL": tts_url,
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
            self._memory_message.set(f"Soft-deleted {count} record")
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

    def get_status(self):
        payload = status_payload(self._status_bus)
        server_state = getattr(self._interpreter, "server_state", None) or {}
        payload["paused"] = bool(server_state.get("is_paused"))
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

    def get_skills(self):
        root = Path(__file__).resolve().parents[2]
        server_dir = root / "source" / "server"
        try:
            files = [p.name for p in server_dir.glob("*.py")]
            return files
        except Exception:
            return ["server.py", "command_router.py", "memory.py"]

    def get_project_notes(self):
        notes_path = Path(__file__).resolve().parents[2] / "memory" / "project_notes.md"
        try:
            if notes_path.is_file():
                return notes_path.read_text(encoding="utf-8")
            return "No project notes recorded yet."
        except Exception as error:
            return f"Error reading notes: {error}"

    def get_memory(self):
        from .memory import MemoryStore
        return MemoryStore().list_active(limit=100)

    def delete_memory(self, memory_id):
        from .memory import MemoryStore
        return {"deleted": MemoryStore().forget(memory_id=int(memory_id))}

    def add_memory(self, key, content):
        from .memory import MemoryStore
        return MemoryStore().remember(key=key, content=content, source="user")

    def update_memory(self, memory_id, key, content):
        from .memory import MemoryStore
        record = MemoryStore().update(int(memory_id), key=key, content=content)
        return record if record is not None else {"error": "memory not found"}

    def get_startup(self):
        """Settings tab: startup-on-boot + tray availability."""
        from .startup_settings import tray_status, startup_enabled

        data = tray_status()
        data["startup_on"] = startup_enabled()
        return data

    def set_startup(self, enabled):
        """Toggle FRIDAY auto-start at Windows boot."""
        from .startup_settings import set_startup as apply_startup, startup_enabled

        ok = apply_startup(bool(enabled))
        return {"enabled": startup_enabled() and ok, "accepted": ok}

    def get_app_window(self):
        """Whether the HUD window is currently visible."""
        return {"visible": self._hud_visible}

    def set_app_window(self, visible):
        """Close App (background): hide/show the HUD window from the tray."""
        self._hud_visible = bool(visible)
        return {"visible": self._hud_visible}

    def set_paused(self, paused):
        """Internet toggle for the voice channel (mirrors the terminal Enter key)."""
        interpreter = self._interpreter
        server_state = getattr(interpreter, "server_state", None)
        if not isinstance(server_state, dict):
            return {"paused": False, "accepted": False, "message": "FRIDAY server is not ready yet."}
        server_state["is_paused"] = bool(paused)
        if paused:
            server_state["hot_window_active"] = False
            server_state["hot_window_expires_at"] = 0.0
        try:
            output_queue = getattr(interpreter, "output_queue", None)
            if output_queue is not None and hasattr(output_queue, "sync_q"):
                output_queue.sync_q.put({"voice_paused": bool(paused), "end": True})
        except Exception:
            pass
        message = "Voice mode paused." if paused else "Voice mode activated."
        try:
            tts = getattr(interpreter, "tts", None)
            if tts is not None and not paused:
                tts.feed("Voice mode activated.")
                callback = getattr(interpreter, "on_tts_chunk", None)
                tts.play_async(on_audio_chunk=callback, sentence_fragment_delimiters=".?!;,\n…)]}", minimum_sentence_length=1)
        except Exception:
            pass
        if self._status_bus is not None:
            self._status_bus.publish(STATUS_IDLE, message)
        return {"paused": bool(paused), "accepted": True, "message": message}

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
            "Diagnostics: watchdog reset, proactive welcome-back scan, system stats",
        ]
        return sections

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
        """Live mission schedule for the HUD Calendar pane."""
        state = getattr(self._interpreter, "server_state", {}) or {}
        store = state.get("schedule_store")
        if store is None:
            return {"events": [], "count": 0}
        from datetime import datetime as _dt, timedelta as _td

        now = _dt.now().astimezone()
        items = []
        for event in store.upcoming()[:40]:
            try:
                start = _dt.fromisoformat(event.get("start", ""))
            except (ValueError, TypeError):
                continue
            end = start + _td(minutes=event.get("duration_minutes", 60))
            items.append({
                "title": event.get("title", "Task"),
                "start_label": start.strftime("%a %d %b · %H:%M"),
                "start": event.get("start", ""),
                "done": end < now,
            })
        items.sort(key=lambda one: one.get("start", ""))
        return {"events": items, "count": len(items)}

    def get_ai_agents(self):
        """Return live status, active tasks, providers, and models of all AI agents."""
        state = getattr(self._interpreter, "server_state", {}) or {}
        respond_thread = getattr(self._interpreter, "respond_thread", None)
        is_busy = respond_thread is not None and respond_thread.is_alive()
        
        router = state.get("brain_router")
        active_pool = router.current_pool if router else None
        active_tier = router.current_tier if router else None
        active_model = router.current_model if router else None

        # Live "what is running / what ran last" for the HUD header, even
        # when the current turn has finished (current clears on success).
        if router is not None:
            last_pool = router.last_pool or active_pool
            last_model = router.last_model or active_model
            last_tier = router.last_tier or active_tier
        else:
            last_pool, last_model, last_tier = active_pool, active_model, active_tier
        
        try:
            from . import api_pools
            pools_stats = api_pools.describe_pools()
        except Exception:
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

        # 1. Groq Primary Conversation Agent
        p_health, p_desc = get_pool_health("brain_groq")
        in_use = is_busy and active_pool == "brain_groq" and active_tier == "normal"
        status = "in-use" if in_use else p_health
        task = "Conversing with user & executing tools" if in_use else ("Standby (Ready)" if p_health == "available" else p_desc)
        agents.append({
            "id": "agent-groq-conv",
            "name": "Groq Primary Dialogue Agent",
            "role": "Conversation & Tool Control",
            "provider": "Groq Cloud",
            "model": "openai/gpt-oss-20b",
            "status": status,
            "task": task,
            "description": "Fast primary dialogue engine & Python tool execution.",
            "health_desc": p_desc,
        })

        # 2. Groq Deep Reasoning Agent
        in_use = is_busy and active_pool == "brain_groq" and active_tier == "hard"
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
            "model": "deepseek/deepseek-chat-v3-0324:free",
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
        status = "in-use" if in_use else "available"
        task = "Executing local Qwen3 inference" if in_use else "Standby (Local Ollama Ready)"
        agents.append({
            "id": "agent-local-qwen",
            "name": "Local Qwen3 Offline Brain",
            "role": "Offline Dialogue & Tools",
            "provider": "Local Ollama Engine",
            "model": "ollama_chat/qwen3:8b",
            "status": status,
            "task": task,
            "description": "Zero-quota local baseline model running on CPU/RAM.",
            "health_desc": "Always Available (Local)",
        })

        # 6. Gemini Primary Vision Agent
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
            "name": "Gemini Visual Inspection Agent",
            "role": "Screen Perception & OCR Filter",
            "provider": "Google Gemini Cloud",
            "model": gv_chain_label,
            "status": status,
            "task": task,
            "description": "Primary visual chain on the dedicated vision account; hot models fall over to the next free model.",
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
            "model": "meta-llama/llama-3.2-11b-vision-instruct:free",
            "status": status,
            "task": task,
            "description": "Secondary visual perception model if primary Gemini vision fails.",
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
        status = "in-use" if in_use else "available"
        task = "Local Whisper transcription fallback" if in_use else "Standby (Local RealtimeSTT Process Ready)"
        agents.append({
            "id": "agent-local-whisper",
            "name": "Local RealtimeSTT Ear Process",
            "role": "Offline Speech Recognition",
            "provider": "Local Faster-Whisper",
            "model": "whisper-base (RealtimeSTT)",
            "status": status,
            "task": task,
            "description": "Zero-quota local speech recorder running in a separate process.",
            "health_desc": "Always Available (Local Process)",
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
            "status": "in-use" if tts_stage == "local" else "available",
            "task": "Speaking with the local neural voice" if tts_stage == "local" else "Standby (speaks when both Gemini TTS stages are hot)",
            "description": "The original FRIDAY voice; starts lazily the first time the cloud chain fails.",
            "health_desc": "Always Available (Local Server)",
        })

        return {
            "agents": agents,
            "summary": {
                "total": len(agents),
                "active_turn": bool(is_busy),
                "active_model": active_model or last_model or "None (Standby)",
                "active_provider": active_pool or last_pool or "None (Standby)",
                "active_tier": active_tier or last_tier or "normal",
                "last_switch": getattr(router, "last_switch", None) or "No provider switches yet.",
                "exhaustion_ledger": api_pools.ledger() if "api_pools" in str(type(router).__module__) else {},
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


def run_status_window(status_bus, interpreter=None, start_hidden=False):
    """Run the modern webview UI on the process main thread.

    start_hidden=True (registry auto-start): the window is created hidden —
    FRIDAY boots in the system tray only; the tray 'Open App' shows it.
    """
    try:
        import webview

        if not UI_ENTRYPOINT.is_file():
            raise FileNotFoundError(UI_ENTRYPOINT)

        tray_console = {"open": False, "proc": None}

        def tray_on_open_app():
            import subprocess as _sub

            window = webview.windows[0] if webview.windows else None
            if window is not None:
                try:
                    window.show()
                    window.restore()
                except Exception:
                    pass
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
            # 'Close App (background)': hide the HUD AND close the console
            # panel spawned at "Open App" — FRIDAY keeps running in the tray.
            tray_console["open"] = False
            window = webview.windows[0] if webview.windows else None
            window = webview.windows[0] if webview.windows else None
            if window is not None:
                try:
                    window.hide()
                except Exception:
                    pass

        def tray_on_exit():
            # Tell the start_friday.ps1 supervisor this was an INTENTIONAL stop
            # so it disarms the auto-relaunch loop instead of bringing FRIDAY
            # right back. Then hard-exit (there is no graceful downs-instance).
            import os as _os

            try:
                _stop = _os.path.join(_os.path.expanduser("~"), ".friday", "stop_friday.flag")
                _os.makedirs(_os.path.dirname(_stop), exist_ok=True)
                with open(_stop, "w", encoding="utf-8") as _f:
                    _f.write("tray exit - intentional stop")
            except Exception:
                pass
            _os._exit(0)

        from .startup_settings import start_tray

        start_tray(
            on_open_app=tray_on_open_app,
            on_close_app=tray_on_close_app,
            on_exit=tray_on_exit,
        )

        webview.create_window(
            "FRIDAY",
            UI_ENTRYPOINT.as_uri(),
            js_api=WebviewBridge(status_bus, interpreter),
            width=1100,
            height=780,
            min_size=(760, 560),
            background_color="#050b16",
            frameless=True,
            hidden=start_hidden,  # tray auto-start boots with no visible HUD
            # easy_drag swallows scroll-wheel events over scrollable panels
            # in frameless mode. Drag regions are marked with the
            # '__pywebview__draggable' CSS class instead (headers only), so
            # feeds/lists scroll freely while the title bar moves the window.
            easy_drag=False,
        )
        webview.start(debug=False)
    except Exception as error:
        print(f"[status-ui warning] Modern webview unavailable: {error}", flush=True)
        status_bus.publish(STATUS_ERROR, f"Status window unavailable: {error}")
        FaceStatusWindow(status_bus, interpreter).run()


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
