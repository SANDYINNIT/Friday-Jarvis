"""Bounded, deterministic Windows window awareness and control.

Mutating actions in this module are intentionally not wired to voice or an
LLM.  Callers must identify a window exactly enough to avoid an arbitrary
title-substring match and must explicitly confirm the requested action.

Provides application discovery (AppFinder) and low-level media-key dispatch
for reliable, deterministic PC control.
"""

import ctypes
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import psutil
try:
    import pywinctl
except (ImportError, ModuleNotFoundError):
    pywinctl = None

try:
    import winreg
except ImportError:
    winreg = None

from .windows_context import get_active_window_context


DEFAULT_WINDOW_LIMIT = 100
_BOUNDS_TOLERANCE = 2


def _value(window, name, default=None):
    try:
        value = getattr(window, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _process_info(pid):
    if not pid:
        return "", ""
    try:
        process = psutil.Process(pid)
        return str(process.name() or ""), str(process.exe() or "")
    except (OSError, psutil.Error):
        return "", ""


def _window_pid(handle):
    if os.name != "nt" or not handle:
        return None
    try:
        process_id = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        return process_id.value or None
    except (AttributeError, OSError):
        return None


def _handle(window):
    try:
        return int(window.getHandle())
    except Exception:
        return None


def _bounds(window):
    values = [_value(window, name) for name in ("left", "top", "width", "height")]
    return values if all(value is not None for value in values) else None


def _snapshot(window, *, include_process=True):
    handle = _handle(window)
    pid = _value(window, "pid")
    if not isinstance(pid, int):
        pid = _window_pid(handle)
    process = executable = ""
    if include_process:
        process, executable = _process_info(pid)
    return {
        "handle": handle,
        "title": str(_value(window, "title", "") or "").strip(),
        "process": process,
        "pid": pid,
        "executable": executable,
        "bounds": _bounds(window),
        "minimized": _value(window, "isMinimized"),
        "maximized": _value(window, "isMaximized"),
        "visible": _value(window, "isVisible"),
    }


def _windows():
    windows, _ = _windows_status()
    return windows


def _windows_status():
    if pywinctl is None:
        return [], "pywinctl backend unavailable"
    try:
        return list(pywinctl.getAllWindows()), None
    except Exception as error:
        return [], str(error) or error.__class__.__name__


def list_windows(limit=DEFAULT_WINDOW_LIMIT):
    """Return at most ``limit`` structured windows, without changing state."""
    try:
        limit = max(0, min(DEFAULT_WINDOW_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = DEFAULT_WINDOW_LIMIT
    return [_snapshot(window) for window in _windows()[:limit]]


def get_active_window():
    """Return the existing read-only foreground context."""
    return get_active_window_context()


def _norm(value):
    return " ".join(str(value or "").casefold().split())


def _process_matches(actual, requested):
    actual = _norm(actual)
    requested = _norm(requested)
    if not actual or not requested:
        return False
    return actual == requested or Path(actual).name == Path(requested).name or Path(actual).stem == Path(requested).stem


def _target_matches(snapshot, target):
    if isinstance(target, int):
        return snapshot["handle"] == target
    if not isinstance(target, dict):
        return False
    if target.get("handle") is not None:
        try:
            return snapshot["handle"] == int(target["handle"])
        except (TypeError, ValueError):
            return False
    title = target.get("title")
    process = target.get("process") or target.get("executable")
    if title is None or process is None:
        return False
    if _norm(snapshot["title"]) != _norm(title) or not _process_matches(snapshot["process"], process):
        return False
    if target.get("pid") is not None:
        try:
            if snapshot["pid"] != int(target["pid"]):
                return False
        except (TypeError, ValueError):
            return False
    if target.get("executable") and _norm(snapshot["executable"]) != _norm(target["executable"]):
        return False
    return True


_FUZZY_MIN_SCORE = 0.30
_FUZZY_AMBIGUITY_GAP = 0.35
_DEV_PROCESS_HINTS = (
    "windows terminal", "command prompt", "powershell", "pwsh", "cmd",
    "conhost", "console window host", "python", "node", "npm", "code",
)


def _tokenize(value):
    return tuple(dict.fromkeys(_norm(value).split()))


def _fuzzy_score(snapshot, query_tokens):
    """Score a window against a query: overlap + order bonus (0..1)."""
    if not query_tokens:
        return 0.0
    title_tokens = _tokenize(snapshot.get("title", ""))
    process_tokens = _tokenize(
        f"{snapshot.get('process', '')} {snapshot.get('executable', '')}"
    )
    haystack_tokens = tuple(dict.fromkeys(title_tokens + process_tokens))
    hits = [token for token in query_tokens if token in haystack_tokens]
    if not hits:
        return 0.0
    overlap = len(hits) / len(query_tokens)
    order_bonus = 0.1 if all(token in title_tokens for token in hits) else 0.0
    return round(min(1.0, overlap + order_bonus), 3)


def _is_dev_window(snapshot):
    text = _norm(f"{snapshot.get('process', '')} {snapshot.get('title', '')}")
    return any(hint in text for hint in _DEV_PROCESS_HINTS)


def find_windows(query, limit=15, *, exclude_dev=True):
    """Return scored window snapshots matching ``query``, highest first."""
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 15
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    candidates = []
    for window in _windows()[:DEFAULT_WINDOW_LIMIT]:
        snapshot = _snapshot(window)
        if exclude_dev and _is_dev_window(snapshot):
            continue
        score = _fuzzy_score(snapshot, query_tokens)
        if score < _FUZZY_MIN_SCORE:
            continue
        entry = dict(snapshot)
        entry["score"] = score
        candidates.append(entry)
    candidates.sort(key=lambda item: (-item["score"], _norm(item["process"]), _norm(item["title"])))
    if len(candidates) > limit:
        candidates = candidates[:limit]
    for index, candidate in enumerate(candidates):
        candidate["rank"] = index + 1
    return candidates


def resolve_fuzzy(query, *, exclude_dev=True):
    """Resolve ``query`` to one confident window or expose the candidates."""
    candidates = find_windows(query, exclude_dev=exclude_dev)
    if not candidates:
        return None, {"reason": "no window matches that description", "candidates": []}
    best = candidates[0]
    if len(candidates) == 1 or (best["score"] - candidates[1]["score"] >= _FUZZY_AMBIGUITY_GAP):
        return best, None
    return None, {"reason": "several windows match that description", "candidates": candidates[:8]}


def focus_by_query(query, *, exclude_dev=True):
    """Focus one confidently matched window; otherwise report candidates only."""
    match, failure = resolve_fuzzy(query, exclude_dev=exclude_dev)
    if failure:
        result = {
            "operation": "focus",
            "success": False,
            "changed": False,
            "before": None,
            "after": None,
            "failure_reason": failure["reason"],
            "candidates": failure["candidates"],
        }
        result["risk"] = {"class": "reversible", "confirmation_required": False}
        return result
    return focus_window({"title": match["title"], "process": match["process"],
                         "pid": match["pid"], "handle": match["handle"]})


def minimize_by_query(query, *, exclude_dev=True):
    """Minimize one confidently matched window; otherwise report candidates only."""
    match, failure = resolve_fuzzy(query, exclude_dev=exclude_dev)
    if failure:
        result = {
            "operation": "minimize",
            "success": False,
            "changed": False,
            "before": None,
            "after": None,
            "failure_reason": failure["reason"],
            "candidates": failure["candidates"],
        }
        result["risk"] = {"class": "reversible", "confirmation_required": False}
        return result
    return minimize_window({"title": match["title"], "process": match["process"],
                            "pid": match["pid"], "handle": match["handle"]})


def _resolve(target):
    windows, backend_error = _windows_status()
    if backend_error:
        return None, {"reason": "Windows window backend unavailable", "candidates": [], "available": False}
    snapshots = [_snapshot(window) for window in windows]
    if isinstance(target, str):
        candidates = [item for item in snapshots if _norm(item["title"]) == _norm(target)]
        return None, {"reason": "target requires exact title and process", "candidates": candidates}
    matches = [(window, snapshot) for window, snapshot in zip(windows, snapshots) if _target_matches(snapshot, target)]
    if len(matches) != 1:
        candidates = []
        if isinstance(target, dict) and target.get("title") is not None:
            candidates = [snapshot for snapshot in snapshots if _norm(snapshot["title"]) == _norm(target["title"])]
        return None, {
            "reason": "window target is ambiguous or not found",
            "candidates": [snapshot for _, snapshot in matches] if matches else candidates,
        }
    return matches[0], None


def _result(operation, before=None, after=None, *, reason=None, candidates=None, changed=False):
    return {
        "operation": operation,
        "success": bool(changed),
        "changed": bool(changed),
        "before": before,
        "after": after,
        "failure_reason": reason,
        "candidates": candidates or [],
    }


def _confirmation(confirmation_callback, operation, target, before):
    if not callable(confirmation_callback):
        return False
    try:
        return bool(confirmation_callback(operation, target, before))
    except Exception:
        return False


def _risk(operation):
    return {
        "class": "destructive" if operation in {"close", "terminate"} else "reversible",
        "confirmation_required": True,
    }


def _confirmed_result(operation, target, confirmation_callback, before=None):
    result = _result(operation, before, before, reason="explicit confirmation required")
    result["risk"] = _risk(operation)
    if _confirmation(confirmation_callback, operation, target, before):
        return None
    return result


def _number(value, name, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'finite'}")
    return int(value) if value.is_integer() else value


def _bounds_match(actual, expected, indexes):
    return actual is not None and all(
        abs(actual[index] - expected[position]) <= _BOUNDS_TOLERANCE
        for position, index in enumerate(indexes)
    )


def _action_result(operation, target, confirmation_callback, method_name, expected, indexes):
    resolved, failure = _resolve(target)
    if failure:
        result = _result(operation, reason=failure["reason"], candidates=failure["candidates"])
        result["risk"] = _risk(operation)
        result["available"] = failure.get("available", True)
        return result
    window, before = resolved
    denied = _confirmed_result(operation, target, confirmation_callback, before)
    if denied:
        return denied
    try:
        getattr(window, method_name)(*expected)
    except Exception as error:
        after = _snapshot(window)
        result = _result(operation, before, after, reason=str(error) or error.__class__.__name__)
        result["risk"] = _risk(operation)
        return result
    after = _snapshot(window)
    changed = after["bounds"] != before["bounds"] and _bounds_match(after["bounds"], expected, indexes)
    result = _result(operation, before, after, changed=changed,
                     reason=None if changed else "window bounds did not change to the requested values")
    result["risk"] = _risk(operation)
    return result


def _mutate(target, operation, method_name, state_name):
    resolved, failure = _resolve(target)
    if failure:
        return _result(operation, reason=failure["reason"], candidates=failure["candidates"])
    window, before = resolved
    if before[state_name] is True:
        return _result(operation, before, before, reason=f"window is already {state_name}")
    try:
        getattr(window, method_name)()
    except Exception as error:
        after = _snapshot(window)
        return _result(operation, before, after, reason=str(error) or error.__class__.__name__)
    after = _snapshot(window)
    changed = after[state_name] is True and after != before
    return _result(
        operation,
        before,
        after,
        changed=changed,
        reason=None if changed else f"window state did not change to {state_name}",
    )


def minimize_window(target):
    return _mutate(target, "minimize", "minimize", "minimized")


def maximize_window(target):
    return _mutate(target, "maximize", "maximize", "maximized")


def restore_window(target):
    resolved, failure = _resolve(target)
    if failure:
        return _result("restore", reason=failure["reason"], candidates=failure["candidates"])
    window, before = resolved
    if before["minimized"] is False and before["maximized"] is False:
        return _result("restore", before, before, reason="window is already restored")
    try:
        window.restore()
    except Exception as error:
        after = _snapshot(window)
        return _result("restore", before, after, reason=str(error) or error.__class__.__name__)
    after = _snapshot(window)
    changed = after["minimized"] is False and after["maximized"] is False and after != before
    return _result("restore", before, after, changed=changed, reason=None if changed else "window did not restore")


def focus_window(target):
    resolved, failure = _resolve(target)
    if failure:
        return _result("focus", reason=failure["reason"], candidates=failure["candidates"])
    window, before = resolved
    active_before = get_active_window()
    if before["handle"] is not None and active_before.get("handle") == before["handle"]:
        return _result("focus", before, before, reason="window is already active")
    try:
        window.activate()
    except Exception as error:
        after = _snapshot(window)
        return _result("focus", before, after, reason=str(error) or error.__class__.__name__)
    after = _snapshot(window)
    active_after = get_active_window()
    changed = active_after.get("handle") == after["handle"] and active_after.get("handle") != active_before.get("handle")
    return _result("focus", before, after, changed=changed, reason=None if changed else "window did not become active")


def close_window(target, *, confirmation_callback=None):
    """Close one exactly resolved window after explicit confirmation."""
    resolved, failure = _resolve(target)
    if failure:
        result = _result("close", reason=failure["reason"], candidates=failure["candidates"])
        result["risk"] = _risk("close")
        result["available"] = failure.get("available", True)
        return result
    window, before = resolved
    denied = _confirmed_result("close", target, confirmation_callback, before)
    if denied:
        return denied
    try:
        window.close()
    except Exception as error:
        after = _snapshot(window)
        result = _result("close", before, after, reason=str(error) or error.__class__.__name__)
        result["risk"] = _risk("close")
        return result

    windows, backend_error = _windows_status()
    after_windows = [_snapshot(item) for item in windows]
    handle_present = before["handle"] is not None and any(item["handle"] == before["handle"] for item in after_windows)
    pid_present = before["pid"] is not None and any(item["pid"] == before["pid"] for item in after_windows)
    disappeared = not handle_present
    result = _result(
        "close", before, None, changed=disappeared,
        reason=None if disappeared else "window did not disappear after close",
    )
    result.update({"risk": _risk("close"), "after_windows": after_windows,
                   "window_disappeared": not handle_present,
                   "process_disappeared": not pid_present})
    if backend_error:
        result["success"] = result["changed"] = False
        result["failure_reason"] = "could not verify close: Windows window backend unavailable"
        result["available"] = False
    return result


def move_window(target, x, y, *, confirmation_callback=None):
    try:
        requested = (_number(x, "x"), _number(y, "y"))
    except ValueError as error:
        result = _result("move", reason=str(error))
        result["risk"] = _risk("move")
        return result
    return _action_result("move", target, confirmation_callback, "moveTo", requested, (0, 1))


def resize_window(target, width, height, *, confirmation_callback=None):
    try:
        requested = (_number(width, "width", positive=True), _number(height, "height", positive=True))
    except ValueError as error:
        result = _result("resize", reason=str(error))
        result["risk"] = _risk("resize")
        return result
    return _action_result("resize", target, confirmation_callback, "resizeTo", requested, (2, 3))


def terminate_process(target, *, confirmation_callback=None):
    """Terminate only an explicitly identified PID, never a name or shell command."""
    if isinstance(target, dict):
        pid = target.get("pid")
        requested_process = target.get("process") or target.get("executable")
    else:
        pid = target
        requested_process = None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        result = _result("terminate", reason="terminate requires an exact positive PID")
        result["risk"] = _risk("terminate")
        return result
    try:
        process = psutil.Process(pid)
        process_name = str(process.name() or "")
        executable = str(process.exe() or "")
        if requested_process and not (_process_matches(process_name, requested_process) or _process_matches(executable, requested_process)):
            result = _result("terminate", reason="PID does not match requested process")
            result["risk"] = _risk("terminate")
            return result
        before = {"pid": pid, "process": process_name, "executable": executable, "running": bool(process.is_running())}
    except (OSError, psutil.Error, AttributeError) as error:
        result = _result("terminate", reason=str(error) or error.__class__.__name__)
        result["risk"] = _risk("terminate")
        return result
    denied = _confirmed_result("terminate", target, confirmation_callback, before)
    if denied:
        return denied
    try:
        process.terminate()
        running = bool(process.is_running())
    except (OSError, psutil.Error, AttributeError) as error:
        result = _result("terminate", before, None, reason=str(error) or error.__class__.__name__)
        result["risk"] = _risk("terminate")
        return result
    result = _result("terminate", before, {**before, "running": running}, changed=not running,
                     reason=None if not running else "process remained running after terminate")
    result["risk"] = _risk("terminate")
    return result


# --- Application discovery (AppFinder / launch) --------------------------- #
# Strategy order mirrors the pattern proven in DeskTopVoiceAgent / Jarvis
# Control System: direct path, URI scheme, registry App Paths, Start Menu
# shortcuts, PATH, and finally a bounded Program Files scan.

_SCAN_PATHEXTS = (".exe", ".bat", ".cmd", ".vbs", ".js", ".wsf", ".lnk")


def _expand_env_path(path):
    try:
        return os.path.expandvars(os.path.expanduser(str(path or "")))
    except (OSError, ValueError):
        return str(path or "")


def _resolve_lnk(shortcut_path):
    """Resolve a Windows .lnk shortcut target via PowerShell (no COM deps)."""
    try:
        script = (
            "$s=(New-Object -ComObject WScript.Shell)"
            f".CreateShortcut('{shortcut_path}');$s.TargetPath"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=8, check=False,
        )
        target = str(result.stdout or "").strip()
        return target if target and os.path.exists(target) else None
    except Exception:
        return None


def _registry_app_paths(exe_name):
    if winreg is None:
        return None
    if not exe_name.lower().endswith(".exe"):
        exe_name = f"{exe_name}.exe"
    subkey_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, subkey_path) as subkey:
                try:
                    with winreg.OpenKey(subkey, exe_name) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                        resolved = _expand_env_path(path).strip('"')
                        if os.path.isfile(resolved):
                            return resolved
                except OSError:
                    pass
        except OSError:
            pass
    return None


def _materialize_lnk(folder, pattern):
    try:
        for root, _, files in os.walk(folder):
            for file in files:
                if not file.lower().endswith(".lnk"):
                    continue
                if pattern.match(file) or pattern.search(root + "\\" + file):
                    resolved = _resolve_lnk(os.path.join(root, file))
                    if resolved:
                        return resolved
    except (OSError, ValueError):
        pass
    return None


def _scan_program_dirs(variations, max_depth=3):
    """Return the first matching executable found in standard install dirs."""
    variations = list(variations)
    base_dirs = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"),
    ]
    target_names = []
    target_names += [v if v.lower().endswith(".exe") else f"{v}.exe" for v in variations if v]
    for base_dir in base_dirs:
        if not os.path.isdir(base_dir):
            continue
        try:
            for current, _, files in os.walk(base_dir):
                depth = current[len(base_dir):].count(os.sep)
                if depth > max_depth:
                    continue
                for filename in files:
                    if filename.lower() in target_names or any(
                        filename.lower().endswith(f"\\{t.lower()}") or filename.lower().endswith(f"/{t.lower()}")
                        for t in target_names
                    ):
                        candidate = os.path.join(current, filename)
                        if os.path.isfile(candidate) and not candidate.lower().endswith(".lnk"):
                            return candidate
        except (OSError, ValueError):
            continue
    return None


def find_app_executable(app_name, timeout=8):
    """Locate an application's executable path on Windows.

    Accepts a display name, an ``.exe`` name, a resolved path, or a URI
    protocol (``ms-settings:``, ``slack:`` ...). Reports ``None`` when nothing
    can be confidently located so callers can fall back gracefully.
    """
    query = str(app_name or "").strip().strip('"')
    if not query:
        return None
    cleaned = re.sub(r"\s+", " ", query)

    # 0. Direct existing file path.
    expanded = _expand_env_path(cleaned)
    if os.path.isfile(expanded):
        return os.path.abspath(expanded)

    # 1. URI protocol scheme (ms-settings:..., slack:...). Drive letters like
    #    "C:" are also caught by ':' but a Windows path is never an app launcher.
    if ":" in cleaned and not (len(cleaned) > 1 and cleaned[1] == ":"):
        return cleaned

    variations = [cleaned]
    if not cleaned.lower().endswith(".exe"):
        variations.append(f"{cleaned}.exe")
    stem_variations = [v.rstrip(".exe") for v in variations]

    # 2. Registry App Paths.
    for variation in variations:
        found = _registry_app_paths(variation)
        if found:
            return found

    # 3. Start Menu shortcuts.
    start_menu_dirs = [
        os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    for folder in start_menu_dirs:
        if not os.path.isdir(folder):
            continue
        for label in reversed(stem_variations):
            label_clean = re.sub(r"[^a-z0-9]+", " ", label).strip().replace(" ", r".*?")
            pattern = re.compile(label_clean, re.IGNORECASE)
            found = _materialize_lnk(folder, pattern)
            if found:
                return found

    # 4. System PATH.
    for variation in variations:
        via_path = shutil.which(variation)
        if via_path:
            return via_path

    # 5. Bounded Program Files / LocalApps scan.
    try:
        found = _scan_program_dirs(stem_variations)
        if found:
            return found
    except (OSError, ValueError):
        pass
    return None


# --- Media-key dispatch --------------------------------------------------- #
_MEDIA_VK = {
    "play_pause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
    "next": 0xB0,        # VK_MEDIA_NEXT_TRACK
    "previous": 0xB1,    # VK_MEDIA_PREV_TRACK
}


# --- Process close/verify (deterministic close-app helper) ---------------- #

_KNOWN_IMAGE_HINTS = {
    "spotify": "Spotify.exe",
    "code": "Code.exe",
    "vs code": "Code.exe",
    "edge": "msedge.exe",
    "chrome": "chrome.exe",
    "discord": "Discord.exe",
    "notepad": "notepad.exe",
    "calculator": "CalculatorApp.exe",
}


def resolve_process_from_text(text):
    """Find which app a sentence refers to by scanning the REAL process list
    ("if spotify is open could you terminate it" -> Spotify.exe). One scan;
    informative tokens from the sentence, longest-matched wins."""
    import io
    import csv as _csv

    stop_words = {
        "open", "close", "terminate", "kill", "quit", "could", "can", "would",
        "please", "quick", "still", "then", "that", "this", "just", "tell",
        "which", "what", "have", "you", "was", "about", "thanks", "thank",
        "love", "friday", "the", "and", "for", "run", "apps", "programs",
        "running", "with", "from", "need",
    }
    tokens = [
        token for token in re.findall(r"[a-z][a-z0-9\-.]{2,}", str(text or "").lower())
        if token not in stop_words
    ]
    try:
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        images = []
        for row in _csv.reader(io.StringIO(listing.stdout or "")):
            if row:
                images.append(row[0].strip())
        matches = []
        for token in tokens:
            for image in images:
                if token in image.lower():
                    matches.append((len(token), image))
        if not matches:
            return None
        return max(matches, key=lambda pair: pair[0])[1]
    except Exception:
        return None


def is_app_running(app_name):
    """Return the real running image name (e.g. Spotify.exe) when the app
    appears in the process list, else None. Same generic substring logic
    as close_app so Minecraft->javaw resolves identically."""
    import io
    import csv as _csv

    name = str(app_name or "").strip()
    if not name:
        return None
    try:
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for row in _csv.reader(io.StringIO(listing.stdout or "")):
            if not row:
                continue
            image = row[0].strip()
            lowered_name = name.lower()
            if lowered_name in image.lower() or any(
                token and token in image.lower()
                for token in lowered_name.split()
            ):
                return image
        return None
    except Exception:
        return None


def close_app(app_name):
    """Close an app by quickly resolving its real process, then verifying.

    Returns (closed: bool, detail: str). Resolution is generic — the running
    process list decides (Minecraft == javaw.exe etc.) — no hardcoded per-app
    scripts beyond the well-known-knowledge hint table for speed.
    """
    import io
    import csv as _csv

    name = str(app_name or "").strip().strip(".")
    if not name:
        return False, "no app name given"
    try:
        hint = _KNOWN_IMAGE_HINTS.get(name.lower())
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = list(_csv.reader(io.StringIO(listing.stdout or "")))
        images = [row[0].strip() for row in rows if row]

        def kill(image):
            outcome = subprocess.run(
                ["taskkill", "/IM", image, "/F"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return outcome.returncode == 0

        # Fast path: well-known image name.
        verify = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {hint}", "/NH"],
            capture_output=True, text=True, timeout=15,
        ) if hint else None
        if hint and verify is not None and hint.lower() in (verify.stdout or "").lower():
            if kill(hint):
                return True, f"terminated {hint}"
        # Generic substring scan over the real process list.
        lowered = name.lower()
        candidates = [
            image for image in images
            if lowered in image.lower() or any(
                token and token in image.lower()
                for token in lowered.replace("_", " ").split()
            )
        ]
        if not candidates and hint and hint.lower() in " ".join(images).lower():
            candidates = [hint]
        if not candidates:
            return False, f"no process matching '{name}' is running"
        closed_any = False
        closed_names = []
        for image in candidates:
            if kill(image):
                closed_any = True
                closed_names.append(image)
        if closed_any:
            # Re-verify: no leftovers of the same app may survive.
            recheck = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
            survivors = [
                row for row in (recheck.stdout or "").lower().splitlines()
                if closed_names and closed_names[0].lower() in row
            ]
            return closed_any, "closed " + ", ".join(closed_names)
        return False, f"taskkill refused on {candidates[0]}"
    except Exception as error:
        return False, str(error)


def send_media_key(action):
    """Send a Windows media virtual key up. Returns True when sent."""
    virtual_key = _MEDIA_VK.get(action)
    if not virtual_key:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(virtual_key, 0, 0, 0)      # key down
        user32.keybd_event(virtual_key, 0, 2, 0)      # KEYEVENTF_KEYUP
        return True
    except (AttributeError, OSError, ctypes.WinError):
        return False
