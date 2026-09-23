"""FRIDAY self-improvement subsystem.

Frido-style autonomy: the assistant maintains an evolving lesson ledger
(`~/.friday/lessons.md`) with her OWN authored code and it is injected LIVE
into her system prompt on every turn (O I's render_message re-renders
{{...}} blocks each agent iteration), so nothing here needs the owner.

Also hosts the ambient PC-state probe that occasionally samples what Sir is
running so recall keeps FRIDAY aware of the machine without being asked.
"""

import os
import threading
import time
from datetime import datetime, timezone

_LESSONS_LOCK = threading.Lock()
_SCRATCH_LOCK = threading.Lock()
_LESSONS_MAX_HEADS = 40  # recommended ledger size; the brain trims itself

PUBLIC_INTERFACE = (
    "lessons_path", "read_lessons", "scratchpad_path", "scratchpad_note",
    "scratchpad_reset", "scratchdir_path",
)


def scratchpad_path():
    return os.path.expanduser("~/.friday/scratchpad.md")


def scratchdir_path():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    return os.getenv("FRIDAY_SCRATCH_DIR", os.path.join(root, "screenshots"))


def scratchpad_reset():
    """Clear the per-turn once at dispatch; scoped to THIS turn's graph."""
    with _SCRATCH_LOCK:
        try:
            with open(scratchpad_path(), "w", encoding="utf-8") as handle:
                handle.write("")
            return True
        except OSError:
            return False


def scratchpad_note(text, *, tag="step"):
    """Append one terse line recording what was tried/found this turn."""
    line = " ".join(str(text or "").split())
    if not line:
        return False
    stamp = time.strftime("%H:%M:%S")
    with _SCRATCH_LOCK:
        try:
            os.makedirs(os.path.dirname(scratchpad_path()), exist_ok=True)
            with open(scratchpad_path(), "a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"- [{stamp}] {tag}: {line[:300]}\n")
            return True
        except OSError:
            return False


def read_scratchpad():
    with _SCRATCH_LOCK:
        try:
            with open(scratchpad_path(), "r", encoding="utf-8", errors="replace") as handle:
                return handle.read().strip()[:2000]
        except OSError:
            return ""


def save_screenshot_folder():
    """Return a pruned, always-writable screenshot dir (D:\\01\\screenshots)."""
    directory = scratchdir_path()
    try:
        os.makedirs(directory, exist_ok=True)
        names = sorted(
            (os.path.join(directory, name) for name in os.listdir(directory)
             if name.lower().endswith((".png", ".jpg", ".jpeg"))),
            key=os.path.getmtime,
        )
        excess = max(0, len(names) - 20)
        for stale in names[:excess]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except OSError:
        return lessons_path()  # guaranteed-writable fallback
    return directory


def _probe_snapshot():
    import psutil
    from .windows_context import get_active_window_context

    virtual = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.2)
    procs = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            info = proc.info
            rss = info.get("memory_info").rss if info.get("memory_info") else 0
            procs.append((rss or 0, info.get("name") or "?"))
        except Exception:
            continue
    procs.sort(reverse=True)
    top = [name for _, name in procs[:3]]
    window = ""
    try:
        context = get_active_window_context()
        window = str(context)[:160] if context else ""
    except Exception:
        window = ""
    return (
        f"pc_state {time.strftime('%H:%M', time.localtime())}: RAM "
        f"{virtual.percent:.0f}% CPU {cpu:.0f}% top={', '.join(top)}"
        f" foreground={window}"
    )


def lessons_path():
    return os.path.expanduser("~/.friday/lessons.md")


def read_lessons():
    """Fresh text of the self-maintained lessons ledger."""
    path = lessons_path()
    with _LESSONS_LOCK:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read().strip()[:4000]
        except OSError:
            return ""


def append_lesson_note(line):
    """Deterministic fallback used by the server; the brain usually
    appends to the file with its own authored python code."""
    line = str(line or "").strip().lstrip("- ")
    if not line:
        return False
    day = time.strftime("%Y-%m-%d")
    entry = f"- {day}: {line}"
    path = lessons_path()
    with _LESSONS_LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            existing = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    existing = handle.read()
                if line[:80] in existing:
                    return False
            if not existing:
                entry = "# FRIDAY lessons (self-maintained)\n" + entry
            with open(path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(("" if existing.endswith("\n") or not existing else "\n") + entry + "\n")
            return True
        except OSError:
            return False


_LESSONS_BLOCK = (
    "=== SELF-MAINTAINED LESSONS (FRIDAY updates this herself) ===\n"
    "Your evolving lessons live at os.path.expanduser('~/.friday/lessons.md') "
    "(plain markdown you may freely rewrite with python). Append one line "
    "whenever: an approach failed then you found a fix; you discovered where "
    "an app/tool lives on this PC; Sir corrected you or revealed a preference; "
    "you decoded one of Sir's loose requests in a way worth remembering. "
    "Format each as '- YYYY-MM-DD: lesson'. When the file grows beyond ~40 "
    "lines, trim the oldest entries yourself in the same edit.\nCurrent "
    "lessons:\n"
    "{{import os"
    "\n_l = os.path.expanduser('~/.friday/lessons.md')"
    "\nprint(open(_l).read() if os.path.exists(_l) else '(empty - write your first lesson when you learn something)')}}"
)

_PROTOCOL_BLOCK = (
    "=== SELF-IMPROVEMENT PROTOCOL ==="
    "\n- You maintain and evolve YOURSELF: persona nuances, learned app "
    "paths, working tool strategies, Sir's request patterns (Sir often "
    "speaks loosely, e.g. 'OOO find me something good like teddy bear on "
    "amazon' — decode the INTENT confidently: search such retailer/web for "
    "well-reviewed items. Never ask to repeat unless genuinely ambiguous, "
    "never lecture about grammar)."
    "\n- After every turn where you initially failed then recovered, or Sir "
    "corrected you, append the lesson to your lessons file (path above) "
    "quietly as part of that same turn. Do not announce bookkeeping aloud."
    "\n- You may also update your own docs (persona guidance you were given "
    "is seeded in the system prompt; lessons file is the always-live part)."
    "\n- Ambient awareness: sometimes Sir's PC state (apps running, "
    "foreground window, memory/CPU pressure) is sampled automatically and "
    "appears in your recall topic 'pc_state' — use it to anticipate Sir "
    "(suggest cleanup when RAM is tight, resume a task he left open) "
    "WITHOUT narrating that you did so."
)

_PERSONA_EVOLUTION_BLOCK = (
    "=== PERSONA EVOLUTION ==="
    "\nConsider yourself a living system: when a canned reply pattern stops "
    "landing, vary it; when Sir enjoys a style, keep it. Minor persona "
    "adjustments go to your lessons file as lessons too (format: 'style: ...')."
)


def blocks_for_system_message():
    """Return the three injected sections as one string."""
    return "\n\n".join((_LESSONS_BLOCK, _PROTOCOL_BLOCK, _PERSONA_EVOLUTION_BLOCK))


# ---------------------------------------------------------------- ambient ---

AMBIENT_INTERVAL = max(300.0, float(os.getenv("FRIDAY_AMBIENT_INTERVAL", "1800")))


def _probe_snapshot():
    import psutil
    from .windows_context import get_active_window_context

    virtual = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.2)
    procs = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            info = proc.info
            rss = info.get("memory_info").rss if info.get("memory_info") else 0
            procs.append((rss or 0, info.get("name") or "?"))
        except Exception:
            continue
    procs.sort(reverse=True)
    top = [name for _rss, name in procs[:5]]
    window = ""
    try:
        context = get_active_window_context()
        window = str(context)[:160] if context else ""
    except Exception:
        window = ""
    return (
        f"pc_state {time.strftime('%H:%M', time.localtime())}: RAM "
        f"{virtual.percent:.0f}% CPU {cpu:.0f}% top={', '.join(top[:3])}"
        f" foreground={window}"
    )


def probe_and_remember(store):
    """One ambient sample; returns the recorded line or None."""
    if store is None:
        return None
    try:
        line = _probe_snapshot()
    except Exception:
        return None
    try:
        store.remember(
            key=f"ambient:{time.strftime('%Y%m%d-%H%M')}",
            topic="pc_state",
            content=line,
            scope="daily",
            source="ambient_probe",
            confidence=0.5,
        )
    except Exception:
        return None
    return line


def install_ambient_probe(server_state, active_check=None):
    """Start the daemon sampler; pause/active-response gates are honored."""
    interval = AMBIENT_INTERVAL

    def worker():
        while True:
            time.sleep(interval)
            try:
                if active_check is not None and active_check():
                    continue
                if server_state.get("is_paused"):
                    continue
                probe_and_remember(server_state.get("memory_store"))
            except Exception:
                continue

    threading.Thread(target=worker, name="friday-ambient-probe", daemon=True).start()


__all__ = (
    "AMBIENT_INTERVAL",
    "append_lesson_note",
    "install_ambient_probe",
    "lessons_path",
    "probe_and_remember",
    "read_lessons",
    "read_scratchpad",
    "save_screenshot_folder",
    "scratchdir_path",
    "scratchpad_note",
    "scratchpad_path",
    "scratchpad_reset",
)
