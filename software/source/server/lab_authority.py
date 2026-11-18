"""One authoritative state for FRIDAY's lab: revisions, axes, serving identity.

WHY THIS EXISTS
---------------
Before this module the lab had five partial sources of truth (the world ring,
`roster_tasks`, `friday_tasks.db`, the hard-coded `get_ai_agents()` provider
cards and the globe's `globe.json`), no revision number anywhere, and no single
record of "who is FRIDAY right now". Rendering was therefore free to disagree
with the engine.

This module owns:

* **revisions** - a monotonic `rev` for the whole lab plus a per-entity `rev`,
  so a view can detect a gap and resync instead of silently showing stale data.
* **the mode axes** - background life (L), availability (A), duty (D),
  the user's manual pin (P) and emergency (E), persisted as `meta` rows.
* **serving identity** - exactly ONE record of which character currently holds
  the FRIDAY duty, written only by `install_serving()` (single writer, mirroring
  `server._install_ownership`). Identity (which character) is deliberately kept
  separate from channel (`TurnOwnership`) and from model truth
  (`brain_router._LIVE_ROUTER`) - conflating them is the class of bug that the
  telegram-mute work already fixed.
* **precedence** - the pure transition tables A-E, so "no provider", "manual
  NPC pin" and "background life" can never be resolved by accident.
* **cast derivation** - characters built from the ACTUAL configured
  provider/model inventory rather than four hard-coded generic specialists.

Storage is deliberately additive: only `CREATE TABLE IF NOT EXISTS` for the one
new table (`entity_rev`) and `INSERT OR REPLACE` rows in the pre-existing
`meta` table. No existing table is altered, no row is deleted - the production
world database lives OUTSIDE this working tree (`~/.friday/world.db`) and must
never be rewritten by a schema change here.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Axes vocabulary
# --------------------------------------------------------------------------
LIFE_OFF = "off"
LIFE_PAUSED = "paused"
LIFE_NPC = "npc"
LIFE_AI = "ai"
LIFE_STATES = (LIFE_OFF, LIFE_PAUSED, LIFE_NPC, LIFE_AI)

AVAIL_UP = "up"
AVAIL_DEGRADED = "degraded"
AVAIL_BANNED = "banned"
AVAIL_DISABLED = "disabled"
AVAIL_NONE = "none"
AVAIL_UNKNOWN = "unknown"
AVAIL_STATES = (AVAIL_UP, AVAIL_DEGRADED, AVAIL_BANNED, AVAIL_DISABLED,
                AVAIL_NONE, AVAIL_UNKNOWN)

DUTY_IDLE = "idle"
EMERGENCY_OFF = "off"
EMERGENCY_LOCAL_ONLY = "local_only"
EMERGENCY_NONE = "none"
EMERGENCY_STATES = (EMERGENCY_OFF, EMERGENCY_LOCAL_ONLY, EMERGENCY_NONE)

LANES = ("main_brain", "fallback_brain", "vision", "voice_out", "listen")

# Precedence, first match wins. Documented in TO_DO.md / MEMORY.md.
PRECEDENCE = ("emergency", "duty", "pin", "availability", "life")

# The boss is always this id; everything else is derived from real pools.
BOSS_CHAR_ID = "chr_friday"

_DDL = """
CREATE TABLE IF NOT EXISTS entity_rev (
    entity_id TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    rev       INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""

# Persistent, once-generated display names so characters keep their identity
# across restarts (spec: "random persistent names").
_NAME_POOL = (
    "Ada", "Bex", "Ciro", "Dela", "Eko", "Fenn", "Gita", "Hollis",
    "Ivo", "Juno", "Kade", "Lumi", "Mira", "Nix", "Oona", "Pell",
    "Quill", "Rho", "Sable", "Tamsin", "Uma", "Vero", "Wren", "Yara",
)


def _slug(value: str) -> str:
    out = []
    for ch in str(value or "").lower():
        out.append(ch if ch.isalnum() else "_")
    text = "".join(out).strip("_")
    while "__" in text:
        text = text.replace("__", "_")
    return text or "x"


# --------------------------------------------------------------------------
# Pure transition tables (no I/O - these are what the tests pin)
# --------------------------------------------------------------------------
def life_next(current: str, event: str) -> str:
    """Table A: background-life mode."""
    cur = current if current in LIFE_STATES else LIFE_OFF
    ev = str(event or "")
    if ev == "start":
        return LIFE_NPC
    if ev == "enable_ai" and cur == LIFE_NPC:
        return LIFE_AI
    if ev == "budget_exhausted" and cur == LIFE_AI:
        return LIFE_NPC
    if ev == "duty_admitted" and cur in (LIFE_NPC, LIFE_AI):
        return LIFE_PAUSED          # tick keeps running; planner is suppressed
    if ev == "duty_finished" and cur == LIFE_PAUSED:
        return LIFE_NPC             # caller re-applies enable_ai if it was AI
    if ev in ("pause", "away", "town_closed", "emergency"):
        return LIFE_PAUSED if cur != LIFE_OFF else LIFE_OFF
    if ev == "stop":
        return LIFE_OFF
    if ev == "resume" and cur == LIFE_PAUSED:
        return LIFE_NPC
    return cur


def availability_next(current: str, event: str) -> Tuple[str, str]:
    """Table B. Returns (state, reason). Reuses api_pools' real cooldowns."""
    cur = current if current in AVAIL_STATES else AVAIL_UP
    ev = str(event or "")
    if ev == "quota":
        return AVAIL_DEGRADED, "rate limit / quota"
    if ev == "transient":
        return AVAIL_DEGRADED, "transient upstream error"
    if ev == "three_strikes" or ev == "exhausted":
        return AVAIL_BANNED, "cooldown ban (persisted ledger)"
    if ev == "cooldown_expired" or ev == "success":
        return AVAIL_UP, "recovered"
    if ev == "model_error":
        return AVAIL_BANNED, "model-level ban"
    if ev == "disabled_by_config":
        return AVAIL_DISABLED, "FRIDAY_CLOUD_*=0"
    if ev == "chain_exhausted":
        return AVAIL_NONE, "no usable candidate"
    if ev == "available":
        return AVAIL_UP, ""
    return cur, ""


def emergency_next(current: str, event: str) -> str:
    """Table E. Mid-turn transitions are forbidden - the caller must only pass
    `cloud_down` / `cloud_up` / `local_down` at a turn boundary."""
    cur = current if current in EMERGENCY_STATES else EMERGENCY_OFF
    ev = str(event or "")
    if ev == "cloud_down":
        return EMERGENCY_LOCAL_ONLY
    if ev == "cloud_up" and cur == EMERGENCY_LOCAL_ONLY:
        return EMERGENCY_OFF
    if ev == "local_down":
        return EMERGENCY_NONE
    if ev == "reset":
        return EMERGENCY_OFF
    return cur


def precedence(*, emergency: str, duty: str, pin: Optional[str],
               availability: str, life: str) -> Dict[str, Any]:
    """First-match-wins evaluation of the five axes.

    Returns the decided outcome plus WHICH axis decided it, so the UI can show
    a truthful "why selected" instead of inventing a reason.

    Terminology (fixed 2026-11-05, replacing the old ambiguous wording):
    * `unavailable` means NO USABLE PROVIDER ANYWHERE - the emergency axis at
      NONE, i.e. not even the local fallback can serve. Nothing is "wip":
      duty simply cannot begin, and that is reported honestly.
    * `emergency` = LOCAL_ONLY means local is up and cloud is not - work
      continues on local, never silently (decided_by: emergency).
    * This function only DECIDES the projection. It never routes, never bans,
      never cooldowns: `brain_router` + `api_pools` stay the real authority.
    """
    if emergency == EMERGENCY_NONE:
        return {"outcome": "unavailable", "decided_by": "emergency",
                "detail": "no usable provider anywhere (not even local)"}
    if duty and duty != DUTY_IDLE:
        return {"outcome": "duty", "decided_by": "duty",
                "detail": "a live request owns the machine", "char_id": duty}
    if pin:
        return {"outcome": "pinned", "decided_by": "pin",
                "detail": "the user selected this character",
                "char_id": pin}
    if availability in (AVAIL_BANNED, AVAIL_NONE, AVAIL_DISABLED):
        return {"outcome": "degraded", "decided_by": "availability",
                "detail": availability}
    if life in (LIFE_NPC, LIFE_AI, LIFE_PAUSED, LIFE_OFF):
        return {"outcome": life, "decided_by": "life",
                "detail": "background life (yields to every axis above)"}
    return {"outcome": "unavailable", "decided_by": "availability",
            "detail": "unrecognised state"}


# --------------------------------------------------------------------------
# Authoritative store
# --------------------------------------------------------------------------
class Authority:
    """Single writer for revisions, axes and the serving-identity record."""

    def __init__(self, conn, lock: Optional[threading.RLock] = None):
        self._conn = conn
        self._lock = lock or threading.RLock()
        self._active_serving = None       # process-local duty, never restored from disk
        self._cast_cache = []             # cached cast projection (not truth)
        self._cast_at = 0.0
        self._eligibility_cache = None    # the user's user controls (cached)
        self._router_listener_registered = False
        self.init_schema()

    # -- schema ------------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            # `meta` also lives in the production store schema, but an
            # Authority may be constructed against a bare SQLite file (tests,
            # isolated harnesses): recreate the two-row contract here so every
            # axis/eligibility/serving write has a real table.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta "
                "(key TEXT PRIMARY KEY, value TEXT)")
            self._conn.execute(_DDL)
            self._conn.commit()

    # -- revisions ---------------------------------------------------------
    def rev(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='lab_rev'").fetchone()
        try:
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _bump_rev(self) -> int:
        nxt = self.rev() + 1
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('lab_rev',?)",
                (str(nxt),))
            self._conn.commit()
        return nxt

    def bump(self, kind: str, entity_id: str) -> int:
        """Record that an entity changed; returns the new lab revision."""
        entity_id = str(entity_id or "")
        if not entity_id:
            return self.rev()
        rev = self._bump_rev()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO entity_rev"
                "(entity_id,kind,rev,updated_at) VALUES(?,?,?,?)",
                (entity_id, str(kind or "entity"), rev, time.time()))
            self._conn.commit()
        return rev

    def entity_rev(self, entity_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT rev FROM entity_rev WHERE entity_id=?",
                (str(entity_id),)).fetchone()
        try:
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # -- meta helpers ------------------------------------------------------
    def _get(self, key: str, default: str = "") -> str:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return row[0] if row else default
        except Exception:
            return default

    def _set(self, key: str, value: str) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    (str(key), str(value)))
                self._conn.commit()
        except Exception:
            pass

    # -- axes --------------------------------------------------------------
    _AXIS_DEFAULTS = {
        "life": LIFE_OFF, "availability": AVAIL_UP,
        "emergency": EMERGENCY_OFF, "duty": DUTY_IDLE, "pin": "",
    }

    def axis(self, key: str, default: Optional[str] = None) -> str:
        """Read one axis. Unset axes report their documented resting value,
        never an empty string - a view must never have to guess whether
        `duty == ""` means idle or means 'not initialised'."""
        if key == "duty":
            current = self._active_serving or {}
            return current.get("char_id") or DUTY_IDLE
        if key == "duty_reason":
            return (self._active_serving or {}).get("reason") or ""
        if default is None:
            default = self._AXIS_DEFAULTS.get(str(key), "")
        return self._get("axis_" + str(key), default)

    def set_axis(self, key: str, value: str, reason: str = "") -> Dict[str, Any]:
        """Write one axis and record WHY. Refuses unrecognised states so a
        typo cannot silently become a mode."""
        key, value = str(key), str(value)
        allowed = {
            "life": LIFE_STATES, "availability": AVAIL_STATES,
            "emergency": EMERGENCY_STATES,
            "duty": None, "pin": None,
        }
        if key in allowed and allowed[key] is not None and value not in allowed[key]:
            return {"ok": False, "reason": "invalid state",
                    "key": key, "value": value,
                    "allowed": list(allowed[key])}
        self._set("axis_" + key, value)
        if reason:
            self._set("axis_" + key + "_reason", str(reason))
        self.bump("axis", key)
        return {"ok": True, "key": key, "value": value,
                "reason": str(reason or ""), "rev": self.rev()}

    def axes(self) -> Dict[str, Any]:
        out = {}
        for key in ("life", "availability", "duty", "pin", "emergency"):
            out[key] = self.axis(key, LIFE_OFF if key == "life" else
                                 EMERGENCY_OFF if key == "emergency" else
                                 DUTY_IDLE if key == "duty" else
                                 AVAIL_UP if key == "availability" else "")
            out[key + "_reason"] = self.axis(key + "_reason", "")
        return out

    # -- serving identity (SINGLE WRITER) ----------------------------------
    def serving(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._active_serving or self._last_serving())

    def _last_serving(self) -> Dict[str, Any]:
        raw = self._get("serving_identity", "")
        if not raw:
            return {"char_id": "", "duty": DUTY_IDLE, "served_by": "",
                    "since": 0.0, "reason": "", "active": False}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {**data, "active": False}
        except Exception:
            pass
        return {"char_id": "", "duty": DUTY_IDLE, "served_by": "",
                "since": 0.0, "reason": "", "active": False}

    def install_serving(self, char_id: str, *, served_by: str,
                        reason: str = "", duty: str = "brain") -> Dict[str, Any]:
        """The ONLY writer of the serving record. Called once per admission.

        ROUND LAB-2B: a character that the user has user-disabled can NEVER have
        the badge installed (a pin cannot resurrect a user-disable, and user
        controls cannot grant capabilities either). The refusal is recorded as
        a handover row so the view shows a truthful "asked to serve, refused:
        user-disabled" instead of a silently installed badge.
        """
        char_id = str(char_id or "")
        if self._eligibility_cache is None:
            self._load_eligibility()
        if self._pool_disabled_b(char_id):
            refusal = "refused: user-disabled; requested served_by was %s" \
                      % (served_by or "?")
            self.record_handover("user", char_id, refusal, served_by or "")
            self.bump("serving", char_id)
            return {"ok": False, "reason": "char %s is user-disabled" % char_id,
                    "handover_refusal": refusal, "rev": self.rev()}
        record = {"char_id": char_id, "duty": str(duty or "brain"), "active": True,
                  "served_by": str(served_by or ""),
                  "since": round(time.time(), 3),
                  "reason": str(reason or "")}
        with self._lock:
            self._set("serving_identity", json.dumps(record))
            self._active_serving = record
        self.bump("serving", record["char_id"] or "idle")
        return {"ok": True, "serving": record, "rev": self.rev()}

    def _pool_disabled_b(self, char_id: str) -> bool:
        record = (self._eligibility_cache or {}).get(str(char_id or ""))
        return bool(record is not None and record.get("eligible") is False)

    def clear_serving(self, reason: str = "") -> Dict[str, Any]:
        """Cleared ONLY at turn completion - never mid-turn (the Option-1
        lesson from the telegram-mute work)."""
        self._set("serving_identity", json.dumps(
            {"char_id": "", "duty": DUTY_IDLE, "served_by": "",
             "since": round(time.time(), 3), "reason": str(reason or "")}))
        self._active_serving = None
        self.bump("serving", "idle")
        return {"ok": True, "rev": self.rev()}

    def record_handover(self, frm: str, to: str, reason: str,
                        served_by: str) -> Dict[str, Any]:
        raw = self._get("handover_history", "[]")
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
        rows.append({"from": str(frm or ""), "to": str(to or ""),
                     "reason": str(reason or ""),
                     "served_by": str(served_by or ""),
                     "at": round(time.time(), 3)})
        del rows[:-50]                       # bounded history
        self._set("handover_history", json.dumps(rows))
        self.bump("handover", str(to or ""))
        return {"ok": True, "count": len(rows)}

    def handover_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        try:
            rows = json.loads(self._get("handover_history", "[]"))
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = []
        return rows[-max(1, min(int(limit), 50)):]

    # -- pins --------------------------------------------------------------
    def pin(self, char_id: str) -> Dict[str, Any]:
        """the user's explicit selection. Stored as the pin AXIS, never as a
        disguised duty: a pinned character that is not eligible still cannot
        serve (see `precedence`)."""
        char_id = str(char_id or "")
        if not char_id:
            return {"ok": False, "reason": "empty char_id"}
        self._set("axis_pin", char_id)
        self._set("axis_pin_reason", "the user selected this character")
        self.bump("pin", char_id)
        return {"ok": True, "pin": char_id, "rev": self.rev()}

    def unpin(self) -> Dict[str, Any]:
        self._set("axis_pin", "")
        self._set("axis_pin_reason", "")
        self.bump("pin", "")
        return {"ok": True, "rev": self.rev()}

    # -- individual life-mode overrides ------------------------------------
    def set_life_override(self, char_id: str, mode: str) -> Dict[str, Any]:
        """Persistent per-character override. `npc` is the safe default; the
        GLOBAL AI switch must never enable a local character, so callers pass
        `allow_local=False` when applying the global switch."""
        char_id, mode = str(char_id or ""), str(mode or "")
        if mode not in (LIFE_NPC, LIFE_AI, LIFE_OFF):
            return {"ok": False, "reason": "invalid mode",
                    "allowed": [LIFE_NPC, LIFE_AI, LIFE_OFF]}
        if not char_id:
            return {"ok": False, "reason": "empty char_id"}
        self._set("life_override_" + char_id, mode)
        self.bump("life_override", char_id)
        return {"ok": True, "char_id": char_id, "mode": mode}

    def life_override(self, char_id: str) -> str:
        return self._get("life_override_" + str(char_id), "")

    def apply_global_ai(self, enabled: bool, *, local_ids: Tuple[str, ...] = (),
                        is_local=None) -> Dict[str, Any]:
        """Global non-local AI life switch.

        Local characters are NEVER enabled by this switch (spec hard rule);
        they only change when the user sets them individually. Returns exactly
        which characters changed so the UI can show the truth.
        """
        changed, skipped = [], []
        ids = list(local_ids)
        if is_local is not None and not ids:
            ids = []
        for char_id in ids:
            if is_local is not None and is_local(char_id):
                skipped.append(char_id)
                continue
            if enabled:
                self.set_life_override(char_id, LIFE_AI)
                changed.append(char_id)
            else:
                self.set_life_override(char_id, LIFE_NPC)
                changed.append(char_id)
        # Characters not in `ids` keep their own override untouched.
        return {"ok": True, "enabled": bool(enabled), "changed": changed,
                "skipped_local": skipped, "rev": self.rev()}

    # -- serving projection (ROUND LAB-2B) ---------------------------------
    def turn_admitted(self, note: str = "") -> Dict[str, Any]:
        """A real turn took ownership (`server._install_ownership` with a
        non-LOCAL ownership). This is bookkeeping ONLY: it never routes or
        re-routes; the badge resolves when the router actually picks/keeps a
        provider (`observe_router`)."""
        self._set("last_turn_admitted", json.dumps(
            {"at": round(time.time(), 3), "note": str(note or "")[:120]}))
        self.bump("turn", "admitted")
        return {"ok": True, "rev": self.rev()}

    def turn_cleared(self, reason: str = "") -> Dict[str, Any]:
        """The turn is over (`server._clear_ownership`). Duty returns to idle
        while the serving record STAYS as last-known-served, so every view can
        still answer 'who served the last turn' without inventing state."""
        with self._lock:
            active = self._active_serving
            self._active_serving = None
            if active and active.get("char_id"):
                self.record_handover(active["char_id"], "idle",
                                     reason or "turn finished", active["served_by"])
        self.bump("turn", "finished")
        return {"ok": True, "rev": self.rev()}

    def observe_router(self, event: str, pool: str, model: str,
                       reason: str = "") -> Dict[str, Any]:
        """Project a brain selection, success or failure without routing.

        A selection is process-local duty. Only success updates durable
        serving history. Failures can clear duty without rewriting history.
        """
        pool, model = str(pool or ""), str(model or "")
        served_by = ("%s/%s" % (pool, model)) if pool else model
        with self._lock:
            if event in ("selected", "success"):
                char = self._char_for(pool, model)
                if char is None:
                    if event == "selected" and self._active_serving:
                        self._active_serving = None
                        self.bump("serving", "idle")
                    return {"ok": False, "reason": "provider/model has no cast binding"}
                if event == "selected":
                    self._active_serving = None
                    if self._pool_disabled(pool, model):
                        self.bump("serving", "refused")
                        return {"ok": False, "reason": "user-disabled"}
                    self._active_serving = {
                        "char_id": char, "duty": "brain", "active": True,
                        "served_by": served_by, "since": round(time.time(), 3),
                        "reason": "router selected"}
                    self.bump("serving", char)
                    return {"ok": True, "serving": self.serving(), "rev": self.rev()}

                previous = self._last_serving()
                active = self._active_serving or {}
                if (active.get("served_by") == served_by and active.get("char_id") == char
                        and self._pool_disabled(pool, model)):
                    # A user disable is enforced at the next admission. A
                    # previously admitted response still really completed.
                    self._set("serving_identity", json.dumps(active))
                    installed = {"ok": True}
                else:
                    installed = self.install_serving(
                        char, served_by=served_by, reason="router success")
                if not installed.get("ok"):
                    return installed
                state, why = availability_next(self.axis("availability", AVAIL_UP), "available")
                state, _ = availability_next(state, "success")
                self._set("axis_availability", state)
                self._set("axis_availability_reason", why or "router success")
                prior = previous.get("char_id", "")
                if prior != char:
                    note = ("provider switch to " + served_by if prior else
                            "installed (no previous badge); pool " + pool)
                    self.record_handover(prior, char, note, served_by)
                return {"ok": True, "char_id": char, "rev": self.rev()}

            if event != "failure":
                return {"ok": False, "reason": "unknown router event"}
            if (self._active_serving or {}).get("served_by") == served_by:
                self._active_serving = None
            state, _ = availability_next(self.axis("availability", AVAIL_UP), "transient")
            self._set("axis_availability", state)
            self._set("axis_availability_reason", "router failure: %s" % reason[:80])
            self.bump("availability", pool)
            return {"ok": True, "state": state, "rev": self.rev()}

    def _char_for(self, pool: str, model: str) -> Optional[str]:
        """Resolve a concrete binding, without borrowing a sibling model's ID."""
        if not self._refresh_cast():
            return None
        members = [item for item in self._cast_cache or [] if item.get("pool") == pool]
        for item in members:
            if str(item.get("model") or "") == model:
                return item.get("char_id")
        if len(members) == 1 and (not model or not members[0].get("model")):
            return members[0].get("char_id")
        return None

    def _refresh_cast(self, max_age: float = 60.0) -> bool:
        """Cache the derived cast for `max_age` seconds (pool/model truth is
        expensive to re-derive per event; the REAL router never depends on
        this cache - it is projection material only)."""
        now = time.time()
        if self._cast_cache and (now - self._cast_at) < max_age:
            return True
        try:
            payload = derive_cast()
            self._cast_cache = payload.get("cast") or []
            self._cast_at = now
            return True
        except Exception:
            return False

    def _pool_disabled(self, pool: str, model: str) -> bool:
        """True when the cast character that owns this brain (pool, model) is
        user-disabled by the user. Read from the in-memory eligibility cache (NO
        database access on the provider hot path); refreshed on eligibility
        changes and, defensively, on a cache miss."""
        if not self._refresh_cast():
            return False
        char_id = self._char_for(pool, model)
        if char_id is None:
            return False
        if self._eligibility_cache is None:
            self._load_eligibility()
        record = self._eligibility_cache.get(char_id)
        if record is None:
            # Refresh the cache once (persisted truth may have changed after a
            # restart); never raise.
            self._load_eligibility()
            record = self._eligibility_cache.get(char_id)
        return bool(record is not None and record.get("eligible") is False)

    def _load_eligibility(self) -> None:
        cache = {}
        try:
            raw = self._get("eligibility_map", "")
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    cache = {str(k): v for k, v in data.items()
                             if isinstance(v, dict)}
        except Exception:
            cache = {}
        self._eligibility_cache = cache

    def eligibility(self, char_id: str) -> Dict[str, Any]:
        if self._eligibility_cache is None:
            self._load_eligibility()
        record = self._eligibility_cache.get(str(char_id or ""))
        if not record:
            return {"char_id": str(char_id or ""), "eligible": True,
                    "reason": "default: eligible", "at": 0.0}
        return dict(record, char_id=str(char_id))

    def set_eligibility(self, char_id: str, eligible: bool,
                        reason: str = "") -> Dict[str, Any]:
        """the user's explicit user control on ONE cast character.

        `eligible=False` is a MANUAL DISABLE: the character cannot hold the
        FRIDAY badge and is skipped by worker lanes for its (pool, model).
        Pins NEVER override a disable, and a disable never turns a healthy
        main into a fake "sleeping" state - the handover reasons and the
        availability axis say exactly what happened. Enforced at the next
        serving boundary (see `observe_router`), never mid-flight.
        """
        char_id = str(char_id or "")
        if not char_id:
            return {"ok": False, "reason": "empty char_id"}
        record = {"char_id": char_id, "eligible": bool(eligible),
                  "reason": str(reason or "")[:160], "at": round(time.time(), 3)}
        if self._eligibility_cache is None:
            self._load_eligibility()
        self._eligibility_cache[char_id] = record
        self._set("eligibility_map", json.dumps(self._eligibility_cache))
        serving = self.serving()
        deferred = None
        if serving.get("active") and serving.get("char_id") == char_id and not eligible:
            deferred = ("serving character disabled; badge hands off at the "
                        "next serving boundary")
        if not eligible:
            # A VISIBLE row in the handover history: from this character to
            # the next eligible candidate OR empty (or no eligible) — the
            # disable decision itself must survive restarts and be shown.
            self.record_handover(char_id, "next eligible (at the next "
                                          "selection boundary)",
                                 "manual user-disable: %s"
                                 % str(reason or "")[:120],
                                 serving.get("served_by") or "")
        self.bump("eligibility", char_id)
        return {"ok": True, "char_id": char_id, "eligible": bool(eligible),
                "reason": record["reason"], "rev": self.rev(),
                "handover_pending": deferred}

    def record_task_serving(self, kind: str, data: Dict[str, Any]) -> None:
        """Persist truthful served-by/attempt facts the executor reports.

        Kernel: `task_completed`/`task_failed`/`task_retry`/`task_paused`
        events already carry the real (provider, model) from the provider
        chain; persisting them here (bounded) is what makes 'which model
        served this task' survive a restart - the old stats were in-memory
        only and `pool.succeed()` was never called by the queue path.
        """
        task_id = str((data or {}).get("task") or "")
        if not task_id:
            return
        entry = {
            "event": str(kind), "at": round(time.time(), 3),
            "provider": str((data or {}).get("provider") or ""),
            "model": str((data or {}).get("model") or ""),
            "reason": str((data or {}).get("reason") or "")[:160],
            "latency": (data or {}).get("latency"),
        }
        raw = self._get("task_serving", "{}")
        try:
            book = json.loads(raw) if raw else {}
            if not isinstance(book, dict):
                book = {}
        except Exception:
            book = {}
        rows = book.get(task_id)
        if not isinstance(rows, list):
            rows = []
        rows.append(entry)
        del rows[:-5]                      # last 5 attempts per task
        book[task_id] = rows
        while len(book) > 200:             # bounded: last 200 tasks overall
            oldest = next(iter(book))
            book.pop(oldest)
        self._set("task_serving", json.dumps(book))

    def task_serving(self, task_id: str) -> list:
        try:
            raw = self._get("task_serving", "{}")
            book = json.loads(raw) if raw else {}
            rows = (book or {}).get(str(task_id or ""))
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    def register_router_listener(self) -> bool:
        """Wire the REAL router's after-the-fact outcomes into this authority.

        `brain_router` stays the real selection authority; the listener only
        feeds the projection. Registered at most once per authority object,
        and only when the authority itself is constructed - importing tests
        that never build one never see it registered.

        ROUND LAB-2B2: also installs the PRE-SELECTION eligibility consumer so
        a user-disabled character's pool is excluded by the real selection
        functions themselves on the next turn boundary (the in-flight turn
        still finishes on its already-configured provider; served-by history
        keeps the truth of what actually answered).

        ROUND LAB-2D2: also installs the pool_disabled consult into
        `api_pools.run_chain` so the STT/any-run-chain lane honours a
        user-disable (a disabled `chr_stt_groq` skips the stt pool; the
        next chain candidate serves). Vision is enforced directly in
        `screen_understanding._cloud_vision` (the direct-call lane).
        """
        if self._router_listener_registered:
            return True
        try:
            from . import brain_router
            brain_router.set_eligibility_consult(self._consult_skip)
            registered = brain_router.add_serving_listener(self._on_router_event)
            self._router_listener_registered = bool(registered)
        except Exception:
            pass
        try:
            from . import api_pools
            api_pools.set_pool_disabled_consult(
                lambda pool_name, model="": self._consult_skip(pool_name,
                                                               model or ""))
        except Exception:
            pass
        return True

    def _consult_skip(self, pool: str, model: str = "") -> bool:
        """Cheap, never-raising pre-selection skip check."""
        try:
            return self._pool_disabled(pool, model)
        except Exception:
            return False

    def _on_router_event(self, event: str, ctx: Dict[str, Any]) -> None:
        try:
            self.observe_router(str(event),
                                str((ctx or {}).get("pool") or ""),
                                str((ctx or {}).get("model") or ""),
                                str((ctx or {}).get("reason") or ""))
        except Exception:
            pass

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """The one payload every view renders from."""
        with self._lock:
            return {
                "rev": self.rev(),
                "axes": self.axes(),
                "serving": self.serving(),
                "handover": self.handover_history(10),
                "precedence": precedence(
                    emergency=self.axis("emergency", EMERGENCY_OFF),
                    duty=self.axis("duty", DUTY_IDLE),
                    pin=self.axis("pin", "") or None,
                    availability=self.axis("availability", AVAIL_UP),
                    life=self.axis("life", LIFE_OFF)),
            }


# --------------------------------------------------------------------------
# Cast derivation from the REAL configured inventory
# --------------------------------------------------------------------------
def _brain_candidates() -> List[Dict[str, str]]:
    """Ordered brain chain from brain_router (read-only, no key, no config)."""
    try:
        from . import brain_router
        out = []
        for item in (brain_router.resolve("") or []):
            try:
                pool, model, _base = item
            except Exception:
                continue
            if pool == "local" and not model:
                model = brain_router.LOCAL_MODEL
            out.append({"pool": str(pool or ""), "model": str(model or "")})
        return out
    except Exception:
        return []


def _lane_pools() -> Dict[str, List[Tuple[str, str]]]:
    """Which pools serve which non-brain capability lanes, from real config."""
    from . import api_pools
    schema = api_pools.POOL_SCHEMA
    vision = [p for p in schema if str(p).startswith("vision_")]
    stt = [p for p in schema if str(p).startswith("stt_")]
    tts = [p for p in schema if str(p).startswith("gemini_tts")
           or str(p).endswith("_tts")]
    return {
        "vision": [(p, "") for p in vision],
        "listen": [(p, "") for p in stt],
        "voice_out": [(p, "") for p in tts],
    }


def _display_names(count: int) -> List[str]:
    """Generate once, persist, then reuse so names never shuffle."""
    import sqlite3  # local: only needed if a caller hands us a raw path
    del sqlite3
    return list(_NAME_POOL[:max(0, count)])


def derive_cast(*, names: Optional[Dict[str, str]] = None,
                boss_name: str = "FRIDAY") -> Dict[str, Any]:
    """Build the cast from the ACTUAL provider/model inventory.

    Nothing here is a hard-coded "Researcher/Coder" card: every character is a
    real `(pool, model)` binding with the lanes it genuinely serves, its real
    availability, and whether it is a local model (local characters stay NPC
    unless the user enables them one at a time).
    """
    from . import api_pools

    names = dict(names or {})
    health = {}
    try:
        health = api_pools.describe_pools()
    except Exception:
        health = {}

    def banned(pool: str, model: str) -> bool:
        try:
            return bool(api_pools.model_banned(pool, model))
        except Exception:
            return False

    def avail(pool: str, model: str) -> str:
        if pool in ("local", "ollama") or pool.startswith("local"):
            return AVAIL_UNKNOWN  # local readiness has no API-key quota pool
        if not pool:
            return AVAIL_NONE
        if banned(pool, model):
            return AVAIL_BANNED
        info = health.get(pool) or {}
        try:
            total, ban = int(info.get("total", 0)), int(info.get("banned", 0))
        except Exception:
            total, ban = 0, 0
        if total <= 0:
            return AVAIL_DISABLED
        if ban >= total:
            return AVAIL_BANNED
        if ban > 0:
            return AVAIL_DEGRADED
        return AVAIL_UP

    cast: List[Dict[str, Any]] = []
    used_names = set()

    def make(char_id: str, pool: str, model: str, lane: str,
             role: str, *, local: bool, persona: str) -> Dict[str, Any]:
        display = names.get(char_id)
        if not display:
            for candidate in _NAME_POOL:
                if candidate not in used_names:
                    display = candidate
                    break
            display = display or char_id
        used_names.add(display)
        return {
            "char_id": char_id,
            "display_name": display,
            "boss_name": boss_name,
            "persona": persona,
            "pool": pool,
            "model": model,
            "binding": "%s:%s" % (pool, model) if pool else "",
            "lane": lane,
            "duty_role": role,
            "local": bool(local),
            "availability": avail(pool, model),
            "life_mode": LIFE_NPC,
            "life_source": "individual" if local else "default",
        }

    # --- brain lanes (ordered, from brain_router.resolve) ------------------
    brain = _brain_candidates()
    for index, item in enumerate(brain):
        pool, model = item["pool"], item["model"]
        local = pool in ("local", "ollama", "") or pool.startswith("local")
        if local:
            # STABLE id for the local emergency lane: `chr_local` (must match
            # its projection pins and user controls, which use this lane name
            # and must survive model-name churn upstream).
            char_id = "chr_local"
        else:
            char_id = BOSS_CHAR_ID if index == 0 else (
                "chr_" + _slug(pool) + "_" + _slug(model))
        lane = "main_brain" if index == 0 else "fallback_brain"
        role = "boss" if index == 0 else "fallback"
        persona = ("FRIDAY - the boss. Warm, direct, technically precise, "
                   "never obsequious." if index == 0 else
                   "Off-duty personality of %s/%s. Speaks as FRIDAY only while "
                   "holding the duty badge, then returns to being themselves."
                   % (pool, model))
        cast.append(make(char_id, pool, model, lane, role,
                         local=local, persona=persona))

    # --- capability lanes (non-chat services are NOT fake personalities) ---
    for lane, entries in _lane_pools().items():
        for pool, _model in entries:
            char_id = "chr_" + _slug(pool)
            if any(c["char_id"] == char_id for c in cast):
                continue
            persona = ("Transport/service lane '%s'. This is a capability, "
                       "not a conversational persona - it never chats and "
                       "never invents a personality." % lane)
            cast.append(make(char_id, pool, "", lane, lane, local=False,
                             persona=persona))

    return {"cast": cast, "boss_char_id": BOSS_CHAR_ID,
            "lanes": list(LANES),
            "source": "brain_router.resolve + api_pools.POOL_SCHEMA",
            "count": len(cast)}


def public_state(authority: Authority, cast: Optional[Dict[str, Any]] = None
                 ) -> Dict[str, Any]:
    """Everything a view needs, from the ONE authority."""
    payload = authority.snapshot()
    payload["cast"] = cast if cast is not None else derive_cast()
    payload["authority"] = "lab_authority.Authority"
    # LAB-3R5: ONE authoritative per-character life view every badge reads.
    # The persistent override always wins; otherwise the cast default (a
    # model defaults to NPC until explicitly enabled). Brain-lane cast
    # members only - capability lanes are services, not residents.
    life: Dict[str, Any] = {}
    try:
        for member in (payload.get("cast") or {}).get("cast", []):
            if member.get("lane") not in ("main_brain", "fallback_brain"):
                continue
            cid = str(member.get("char_id") or "")
            if not cid:
                continue
            override = authority.life_override(cid)
            default = member.get("life_mode") or LIFE_NPC
            life[cid] = {
                "mode": override or default,
                "override": bool(override),
                "local": bool(member.get("local")),
                "source": "override" if override else
                          (member.get("life_source") or "default"),
            }
    except Exception:
        life = {}
    payload["life"] = life
    return payload


# ---------------------------------------------------------------------------
# Process-wide singleton (ROUND LAB-2B). One authority per process: the same
# record every projection (get_lab, get_core, get_roster, get_ai_agents,
# worker serving) reads from. Never opened at import time - construction is
# explicit and lazy, so importing tests never touch any database.
# ---------------------------------------------------------------------------
_shared: Optional[Authority] = None
_shared_path = None
_shared_lock = threading.RLock()


def configure(path) -> Authority:
    """Bind the singleton to an EXPLICIT path. Tests and isolated harnesses
    MUST call this before `shared()` - under pytest, an unconfigured `shared()`
    returns None rather than silently opening the production store."""
    global _shared, _shared_path
    with _shared_lock:
        _shared_path = str(path)
        _shared = None
    return shared()


def shared(path=None) -> Optional[Authority]:
    """The one Authority for this process, opened lazily.

    `path=None` derives the database from `lab_state.db_path()`, which honours
    `FRIDAY_WORLD_DB`. Under pytest with no explicit `configure()`, this
    returns **None**: running the suite must never open the real store, and a
    projection that is unavailable is reported honestly instead of guessed.
    """
    global _shared, _shared_path
    if path is not None:
        with _shared_lock:
            key = str(path)
            if _shared is None or _shared_path != key:
                _shared = None
                _shared_path = key
    if _shared is None:
        import sys as _sys
        if "pytest" in _sys.modules and not _shared_path:
            return None                      # never open production in tests
        target = _shared_path
        if target is None:
            try:
                from . import lab_state
                target = lab_state.db_path()
            except Exception:
                return None
        try:
            import sqlite3
            conn = sqlite3.connect(str(target), check_same_thread=False,
                                   timeout=5.0)
            authority = Authority(conn)
        except Exception:
            return None
        with _shared_lock:
            # Re-check under the lock: another thread may have built it first.
            if _shared is None:
                _shared = authority
                authority.register_router_listener()   # once per singleton
    return _shared


def reset_shared() -> None:
    """Drop the singleton (tests/harnesses only)."""
    global _shared, _shared_path
    with _shared_lock:
        if _shared is not None and _shared._router_listener_registered:
            from . import api_pools, brain_router
            brain_router.remove_serving_listener(_shared._on_router_event)
            brain_router.set_eligibility_consult(None)
            api_pools.set_pool_disabled_consult(None)
        _shared = None
        _shared_path = None


def pool_user_disabled(pool: str, model: str = "") -> bool:
    """Hot-path check for worker lanes: is this brain (pool, model) disabled
    by an explicit the user user-disable? Never raises and NEVER touches the
    database on the provider thread - reads the singleton's in-memory cache.
    """
    try:
        authority = _shared
        if authority is None:
            return False
        result = authority._pool_disabled(str(pool or ""), str(model or ""))
        return bool(result)
    except Exception:
        return False


def cached_route_eligibility(pool, model):
    """Read an exact, already-loaded legacy policy binding without a refresh."""
    authority = _shared
    if authority is None:
        return None
    cache, cast = authority._eligibility_cache, authority._cast_cache
    if cache is None or not cast or time.time() - authority._cast_at >= 60:
        return None
    members = [item for item in cast if item.get("pool") == pool]
    matches = [item for item in members if str(item.get("model") or "") == model]
    if not matches and not model and len(members) == 1:
        matches = members
    if len(matches) != 1 or not matches[0].get("char_id"):
        return None
    return (cache.get(matches[0]["char_id"]) or {}).get("eligible") is not False


def user_disabled(char_id: str) -> bool:
    """Was ONE cast character explicitly disabled by the user?"""
    try:
        authority = _shared
        if authority is None:
            return False
        record = authority.eligibility(str(char_id or ""))
        return bool(record and record.get("eligible") is False)
    except Exception:
        return False
