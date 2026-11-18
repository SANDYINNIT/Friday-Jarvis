"""Local presentation only: one preference authority, bounded inert teaching data.

No constructors start windows, capture, providers, threads or create stores.
Cards/notifications are session-only; only deliberate preferences are persisted.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
import time
from uuid import uuid4

DEFAULTS = {"cursor": "off", "style": "friday", "top_panel": True,
            "reduced_motion": False, "teaching": True,
            "private_banter": "off", "proactive_suggestions": False}
MAX_PAYLOAD = 48_000
PRESENTATION = object()
HIGHLIGHT = object()
_lock = RLock()


def settings_path():
    return Path(os.environ.get("FRIDAY_COMPANION_SETTINGS") or
                Path.home() / ".friday" / "companion_preferences.json")


def _checked(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError("Unknown companion preference")
    for key, value in values.items():
        choices = {"cursor": ("off", "when_asked", "always"), "style": ("friday", "pointer", "leaf", "bee"),
                   "private_banter": ("off", "light", "spicy")}
        if key in choices:
            if value not in choices[key]:
                raise ValueError("Invalid companion preference")
        elif type(value) is not bool:
            raise ValueError("A boolean preference is required")
    return values


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Text exceeds the presentation limit")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10_000:
        raise ValueError("Invalid scene coordinate")
    return value


def validate_card(raw):
    """Typed shapes/mesh data only; no HTML, SVG, code, expressions or URLs."""
    if (not isinstance(raw, dict) or set(raw) - {"id", "kind", "title", "text", "scene"}
            or len(json.dumps(raw, allow_nan=False).encode("utf-8")) > MAX_PAYLOAD):
        raise ValueError("Unsupported presentation")
    identifier = _text(raw.get("id"), 64)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        raise ValueError("A stable card identifier is required")
    kind = raw.get("kind")
    if kind not in ("lesson", "notification"):
        raise ValueError("Unknown presentation kind")
    result = dict(id=identifier, kind=kind, title=_text(raw.get("title"), 120),
                  text=_text(raw.get("text", ""), 8_000))
    scene = raw.get("scene")
    if scene is None:
        return result
    if kind != "lesson" or not isinstance(scene, dict):
        raise ValueError("Scenes belong to teaching cards")
    if scene.get("type") == "diagram" and set(scene) <= {"type", "shapes"}:
        shapes = scene.get("shapes")
        if not isinstance(shapes, list) or len(shapes) > 64:
            raise ValueError("Diagram limit exceeded")
        for shape in shapes:
            if (not isinstance(shape, dict) or shape.get("kind") not in ("rect", "ellipse", "line", "arrow", "text")
                    or set(shape) - {"kind", "x", "y", "w", "h", "text"}):
                raise ValueError("Unsupported diagram shape")
            for key in ("x", "y", "w", "h"):
                _number(shape.get(key, 0))
            _text(shape.get("text", ""), 160)
    elif scene.get("type") == "mesh" and set(scene) <= {"type", "vertices", "faces"}:
        vertices, faces = scene.get("vertices"), scene.get("faces")
        if (not isinstance(vertices, list) or not 3 <= len(vertices) <= 256
                or not isinstance(faces, list) or not 1 <= len(faces) <= 512):
            raise ValueError("3D scene limit exceeded")
        for vertex in vertices:
            if not isinstance(vertex, list) or len(vertex) != 3:
                raise ValueError("A vertex needs three coordinates")
            for coordinate in vertex:
                _number(coordinate)
        for face in faces:
            if (not isinstance(face, list) or not 3 <= len(face) <= 8
                    or any(type(index) is not int or not 0 <= index < len(vertices) for index in face)):
                raise ValueError("Invalid mesh face")
    else:
        raise ValueError("Only inert diagrams and indexed 3D meshes are supported")
    result["scene"] = deepcopy(scene)
    return result


class Companion:
    def __init__(self, path=None, *, clock=time.monotonic):
        self.path = Path(path) if path is not None else settings_path()
        self.lock = RLock()
        self.clock = clock
        self.cards = OrderedDict()
        self.native = {"available": False, "state": "not_started"}
        self.changed = None
        self.action = None
        self.navigation = None
        self.overlay = None
        self.pending_show = None
        self.epoch = uuid4().hex
        self.caption = None

    def observe_text(self, owner, generation, text, *, live):
        """Only already-admitted local visible output; never tools or model reasoning."""
        if not isinstance(text, str) or not text:
            return False
        with self.lock:
            if getattr(owner, "is_external", True) or not live():
                return False
            old = self.caption
            previous = old[2] if old and old[0] is owner and old[1] is generation else ""
            self.caption = (owner, generation, (previous + text)[-480:], self.clock())
            return True

    def visible_caption(self, owner, generation):
        with self.lock:
            value = self.caption
            if (value is None or value[0] is not owner or value[1] is not generation
                    or self.clock()-value[3] > 8):
                return ""
            return " ".join(value[2].split())[-280:]

    def preferences(self):
        try:
            with self.path.open("rb") as file:
                raw = file.read(4097)
            if len(raw) > 4096:
                raise ValueError("Settings limit")
            saved = _checked(json.loads(raw))
        except FileNotFoundError:
            raw, saved = b"", {}
        except (OSError, ValueError, TypeError):
            return {"available": False, "message": "Companion preferences could not be read. Nothing was changed."}
        return {"available": True, "values": {**DEFAULTS, **saved},
                "revision": hashlib.sha256(raw).hexdigest(), "scope": "saved_on_this_computer"}

    def save(self, changes, expected):
        try:
            _checked(changes)
            current=self.preferences()
            if not current['available'] or expected!=current.get('revision'):
                return {'ok':False,'conflict':True,'message':'Preferences changed or are unavailable. Refresh before saving.'}
            from .file_transaction import file_transaction
            with file_transaction(self.path):
                return self._save_locked(changes,expected)
        except (OSError,ValueError,TypeError):
            return {'ok':False,'message':'Preferences were not saved. Your edit is kept.'}

    def _save_locked(self, changes, expected):
        with self.lock:
            current = self.preferences()
            if not current["available"] or expected != current.get("revision"):
                return {"ok": False, "conflict": True, "message": "Preferences changed or are unavailable. Refresh before saving."}
            try:
                _checked(changes)
                values = {**current["values"], **changes}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                raw = json.dumps(values, sort_keys=True).encode("utf-8")
                name = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".companion-", delete=False) as file:
                        name = file.name
                        file.write(raw)
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(name, self.path)
                finally:
                    if name is not None and os.path.exists(name):
                        os.unlink(name)  # only this writer's uncommitted temporary file
            except (OSError, ValueError, TypeError):
                return {"ok": False, "message": "Preferences were not saved. Your edit is kept."}
            confirmed = self.preferences()
        if self.changed is not None:
            try:
                self.changed()
            except Exception:
                pass  # saved preference is distinct from native readiness
        return {"ok": confirmed.get("values") == values, "preferences": confirmed,
                "message": "Saved on this computer. Native state is reported separately."}

    def publish(self, owner, generation, payload, *, live):
        card = validate_card(payload)
        with self.lock:
            # Recheck immediately before publication, including queued/stopped work.
            prefs = self.preferences()
            if (not live() or not prefs["available"] or not prefs["values"]["teaching"]):
                return False
            now = self.clock()
            self._expire(now)
            key = (id(owner), id(generation), card["id"])
            if key in self.cards:
                return self.cards[key]["content"] == card
            self.cards[key] = {"id": uuid4().hex, "content": card,
                               "created": now, "seen": False,
                               "owner": owner, "generation": generation}
            while len(self.cards) > 32:
                self.cards.popitem(last=False)
            # Only an admitted lesson requests a teaching window. Notifications
            # update the inbox badge without stealing the user's app focus.
            if card["kind"] == "lesson":
                self.pending_show = self.cards[key]["id"]
            return True

    def take_presentation(self):
        with self.lock:
            identifier, self.pending_show = self.pending_show, None
            if identifier is None:
                return None
            return identifier if any(c["id"] == identifier and self.clock()-c["created"] <= 30
                                     for c in self.cards.values()) else None

    def _expire(self, now):
        for key, card in list(self.cards.items()):
            if now-card["created"] > 1800:
                self.cards.pop(key)

    def snapshot(self):
        with self.lock:
            # Expired records are hidden by a passive read; storage is not mutated.
            now = self.clock()
            rows = [{"id": c["id"], "content": deepcopy(c["content"]), "seen": c["seen"],
                     "age_seconds": max(0, now-c["created"])}
                    for c in self.cards.values() if now-c["created"] <= 1800]
            return {"available": True, "preferences": self.preferences(), "rows": rows,
                    "native": dict(self.native), "scope": "local_session", "epoch": self.epoch,
                    "navigation": deepcopy(self.navigation)}

    def open(self, tab):
        if tab not in ("lessons", "inbox", "settings", "home") or self.action is None:
            return {"ok": False, "message": "The main FRIDAY window is unavailable."}
        if self.action(tab) is not True:
            return {"ok": False, "message": "The main window could not be restored."}
        with self.lock:
            self.navigation = {"id": uuid4().hex, "tab": tab}
        return {"ok": True}

    def acknowledge(self, identifier):
        with self.lock:
            for card in self.cards.values():
                if card["id"] == identifier and self.clock()-card["created"] <= 1800:
                    card["seen"] = True
                    return {"ok": True}
        return {"ok": False, "message": "That notification is no longer available."}

    def clear_overlay(self, owner=None):
        # The exact annotation object fences old cleanup from a newer mark.
        current = self.overlay
        if current is None or owner is not None and current[0] is not owner:
            return
        self.overlay = None
        from . import screen_overlay
        screen_overlay.clear("turn_ended", expected=current[2])

    def discard_pending(self):
        with self.lock:
            self.pending_show = None
            self.caption = None


def for_runtime(interpreter, *, create=False):
    if interpreter is None:
        return None
    with _lock:
        result = getattr(interpreter, "companion", None)
        if result is None and create:
            result = interpreter.companion = Companion()
        return result
