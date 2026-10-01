"""Sealed API quota pools for cloud STT / Brain / Vision providers.

Each API account is assigned to exactly one function and is never shared:
STT keys only transcribe, Brain keys only chat, Vision keys only see, and
TTS stays fully local. Credentials are read from ~/.friday/api_credentials.json
(or a path in FRIDAY_CREDENTIALS_FILE) with environment variables as a second
source. Keys are never written to logs, never committed, and never printed.

The file schema is an object keyed by pool name, each value a list of keys:

    {
      "groq_stt":        ["gsk_...", "gsk_...", "gsk_...", "gsk_..."],
      "groq_brain":      ["gsk_...", "gsk_...", "gsk_...", "gsk_..."],
      "openrouter_brain": ["sk-or-v1-...", "sk-or-v1-...", "sk-or-v1-...", "sk-or-v1-..."],
      "gemini_brain":    ["AIza..."],
      "gemini_vision":   ["AIza..."],
      "openrouter_vision": ["sk-or-v1-...", "sk-or-v1-...", "sk-or-v1-...", "sk-or-v1-..."],
      "deepgram":        ["f953..."]
    }

See api_credentials.example.json for the ready-made template.
"""

import json
import os
import re
import hashlib
import threading
import time

CREDENTIALS_DEFAULT_PATH = os.path.join(
    os.path.expanduser("~"), ".friday", "api_credentials.json"
)

POOL_SCHEMA = {
    "stt_groq": {"file": "groq_stt", "envs": ("GROQ_STT_API_KEY", "GROQ_API_KEY")},
    "stt_deepgram": {"file": "deepgram", "envs": ("DEEPGRAM_API_KEY",)},
    # Modal serverless GPU endpoint (OpenAI-compatible). PRIMARY for both brain
    # and vision: it is a self-hosted GLM-5.3 Flash (NVFP4) endpoint, so it is
    # not a metered public API and does not share a free-tier quota with anyone.
    # The stored key is the COMBINED proxy token "wk-<id>.ws-<secret>", which is
    # exactly what an OpenAI-compatible client wants as its api_key. Pool auth is
    # therefore just a bearer token and needs no per-provider header handling.
    "brain_modal": {
        "file": "modal_brain",
        "envs": ("MODAL_BRAIN_API_KEY", "MODAL_API_KEY", "MODAL_PROXY_TOKEN"),
    },
    "vision_modal": {
        "file": "modal_vision",
        "envs": ("MODAL_VISION_API_KEY", "MODAL_API_KEY", "MODAL_PROXY_TOKEN"),
    },
    "brain_groq": {"file": "groq_brain", "envs": ("GROQ_BRAIN_API_KEY", "GROQ_API_KEY")},
    "brain_openrouter": {
        "file": "openrouter_brain",
        "envs": ("OPENROUTER_BRAIN_API_KEY", "OPENROUTER_API_KEY"),
    },
    "brain_gemini": {
        "file": "gemini_brain",
        "envs": ("GEMINI_BRAIN_API_KEY", "GEMINI_API_KEY"),
    },
    "vision_gemini": {
        "file": "gemini_vision",
        "envs": ("GEMINI_VISION_API_KEY", "GEMINI_API_KEY"),
    },
    "vision_openrouter": {
        "file": "openrouter_vision",
        "envs": ("OPENROUTER_VISION_API_KEY", "OPENROUTER_API_KEY"),
    },
    # Synthetic pool for the Gemini transcribe stage: reuses BOTH dedicated
    # Gemini accounts (Brain + Vision) for speech-to-text. Gemini quotas are
    # per-account AND per-model, so transcribing on these accounts consumes a
    # separate model bucket and never starves Brain/Vision usage.
    "gemini_stt": {"file": None, "envs": ()},
    # Same for the cloud TTS stage (Gemini 3.1 Flash TTS on both accounts,
    # local edge-tts voice as the final fallback).
    "gemini_tts": {"file": None, "envs": ()},
}

# Client-side request pacing (minimum inter-request spacing in seconds per key)
# Prevents free-tier API exhaustion and 429 burst errors ("AI overheating").
POOL_MIN_INTERVALS = {
    "stt_groq": 1.0,          # 20 RPM whisper limit
    "stt_deepgram": 0.2,      # High concurrency cap
    "brain_modal": 0.0,       # self-hosted endpoint: no public rate limit
    "vision_modal": 0.0,      # self-hosted endpoint: no public rate limit
    "brain_groq": 1.5,        # 30 RPM text limit
    "brain_openrouter": 2.5,  # 20 RPM free-model cap
    "brain_gemini": 3.5,      # 15 RPM free tier limit
    "vision_gemini": 3.5,     # 15 RPM free tier limit
    "vision_openrouter": 2.5, # 20 RPM free-model cap
    "gemini_stt": 3.5,        # Same per-account pacing as the Gemini tiers
    "gemini_tts": 2.0,        # Wordy pacing: speech happens between segments
}

# Exhaustion cooldown: 6 hours (Sir directive). A provider that runs dry
# stays off rotation for 6 hours — even across app restarts — and the next
# candidate inherits the rotation in the meantime.
_EXHAUSTION_COOLDOWN = float(os.environ.get("FRIDAY_API_COOLDOWN", "21600"))
_COOLDOWN_SECONDS = _EXHAUSTION_COOLDOWN

# Short cooldowns (2026-09-29). A free tier 429 is a per-minute sliding-window
# burst, not a dead key, so it gets a short cool-down and only escalates to the
# 6-hour window after the strike rule trips. A transient failure (timeout, 5xx)
# gets an even shorter one, so a permanently broken provider stops costing a
# request on every turn instead of being retried forever.
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
TRANSIENT_COOLDOWN_SECONDS = 45.0
_RETRY_AFTER_MIN = 5.0
_RETRY_AFTER_MAX = 300.0


def _retry_after_seconds(error_text):
    """Pull a provider's own "try again in Ns" hint out of an error string.

    Handles both common phrasings:
      * Groq / OpenAI style : "Please try again in 6.6s"
      * Google style        : "Please retry in 29.683900996s."
    Returns None when the provider gave no usable hint, so the caller can fall
    back to its own short cooldown instead of a 6-hour ban.
    """
    if not error_text:
        return None
    text = str(error_text).lower()
    for marker in ("try again in", "retry in"):
        start = text.find(marker)
        if start < 0:
            continue
        tail = text[start + len(marker):].lstrip(" :")
        digits = ""
        for char in tail:
            if char.isdigit() or char in ".,":
                digits += char
            else:
                break
        # normalise "29.683900996" / "29,68" style decimals
        try:
            seconds = float(digits.replace(",", "."))
        except ValueError:
            continue
        if seconds <= 0:
            continue
        return max(_RETRY_AFTER_MIN, min(seconds, _RETRY_AFTER_MAX))
    return None


_LEDGER_PATH = os.path.join(
    os.path.expanduser("~"), ".friday", "exhaustion_ledger.json"
)

# Persistent exhaustion ledger: bans survive app restarts. Loaded once at
# boot; every fail/fail_model updates it. Expired entries fall off naturally.
_ledger_lock = threading.Lock()
_ledger_restore_done = False


def _key_fingerprint(key):
    """Stable, non-reversible identifier for an API key.

    SECURITY (2026-09-29): the ledger used to persist the FULL key
    ("gsk_...", "sk-or-v1-...", "AIza...") in plaintext, rewritten on every
    success and every failure, which contradicted this module's own promise
    that keys are never written anywhere. Only a digest is stored now, so a
    stolen ledger is useless without the live credentials.
    """
    return hashlib.sha256(str(key).encode("utf-8", "replace")).hexdigest()[:16]


def _persist_ledger(path=None):
    """Write remaining cooldown minutes for keys and models (persisted so the
    6-hour exhaustion windows survive restarts). Keys are stored as digests,
    never in plaintext."""
    try:
        save_path = str(path or _LEDGER_PATH)
        now = time.monotonic()
        snapshot = {"keys": {}, "models": {}}
        pools = _pools or _ensure_pools()
        for name, quota_pool in pools.items():
            with quota_pool._lock:
                for index, until in quota_pool._banned_until.items():
                    if until > now and index < len(quota_pool._keys):
                        ident = f"{name}:{_key_fingerprint(quota_pool._keys[index])}"
                        snapshot["keys"][ident] = round((until - now) / 60.0, 1)
        with _model_ban_guard:
            for (pool_name, model), until in sorted(_model_bans.items()):
                if until > now:
                    snapshot["models"][f"{pool_name}:{model}"] = round((until - now) / 60.0, 1)
        with open(save_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=1)
    except Exception as error:
        print(f"[api_pools] ledger save failed: {error}", flush=True)


def _restore_ledger():
    """Re-apply stored exhaustion bans (6-hour windows run even if the app
    was closed mid-window)."""
    try:
        with open(_LEDGER_PATH, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError):
        return 0
    now = time.monotonic()
    restored = 0
    pools = _pools or {}
    for ident, minutes_left in (snapshot.get("keys") or {}).items():
        try:
            pool_name, key_ref = ident.split(":", 1)
            quota_pool = pools.get(pool_name)
            if quota_pool is None:
                continue
            # Accept both the new digest form and a legacy plaintext entry so an
            # existing ledger keeps working; the next write re-hashes it.
            index = None
            for candidate, raw in enumerate(quota_pool._keys):
                if _key_fingerprint(raw) == key_ref or raw == key_ref:
                    index = candidate
                    break
            if index is None:
                continue
            if minutes_left > 0:
                quota_pool._banned_until[index] = now + minutes_left * 60.0
                restored += 1
        except (ValueError, TypeError):
            continue
    for ident, minutes_left in (snapshot.get("models") or {}).items():
        try:
            pool_name, model = ident.split(":", 1)
            if minutes_left > 0:
                _model_bans[(pool_name, model)] = time.monotonic() + minutes_left * 60.0
                restored += 1
        except (ValueError, TypeError):
            continue
    if restored:
        print(f"[api_pools] exhaustion ledger restored: {restored} cooling windows", flush=True)
    return restored


def ledger():
    """Expose the current exhaustion ledger (identifiers + minutes left).

    Key entries are digests, never any part of a live key.
    """
    now = time.monotonic()
    out = {"keys": {}, "models": {}}
    pools = _ensure_pools()
    for name, quota_pool in pools.items():
        with quota_pool._lock:
            for index, until in quota_pool._banned_until.items():
                if until > now and index < len(quota_pool._keys):
                    out["keys"][f"{name}:{_key_fingerprint(quota_pool._keys[index])}"] = round((until - now) / 60.0, 1)
    with _model_ban_guard:
        for (pool_name, model), until in sorted(_model_bans.items()):
            if until > now:
                out["models"][f"{pool_name}:{model}"] = round((until - now) / 60.0, 1)
    return out

_GENERIC_SECRETS = (
    re.compile(r"gsk_[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"sk-or-v1-[0-9a-f]{60,80}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[0-9a-f]{40}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    # Modal proxy tokens. The combined form "wk-<id>.ws-<secret>" is what an
    # OpenAI-compatible client carries, and a ticket tuple contains it verbatim
    # - so redact the combined form AND either half on its own. Without these,
    # any accidental `print(ticket)` puts the endpoint credential in the log.
    re.compile(r"wk-[A-Za-z0-9_-]{8,}\.ws-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bwk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bws-[A-Za-z0-9_-]{12,}"),
)


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() not in ("0", "false", "off", "no")


def cloud_stt_enabled():
    return _env_flag("FRIDAY_CLOUD_STT")


def cloud_brain_enabled():
    return _env_flag("FRIDAY_CLOUD_BRAIN")


def cloud_vision_enabled():
    return _env_flag("FRIDAY_CLOUD_VISION")


def extra_credential(name, default=""):
    """Read a SCALAR (non-list) value from the credentials JSON.

    Used for things that are configuration rather than a key - e.g. the Modal
    endpoint URL. Keeping the endpoint next to the key means a user configures
    one provider in one place, and no developer's personal endpoint has to be
    hard-coded in shipped code. Env vars win, so a launcher can override it.
    """
    for var in (f"FRIDAY_{name.upper()}", name.upper()):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    for candidate in (
        os.environ.get("FRIDAY_CREDENTIALS_FILE", ""),
        CREDENTIALS_DEFAULT_PATH,
    ):
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
    return default


class CredentialStore:
    """Loads and caches credentials once, tolerating a missing file."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = None
        self._file_candidates = [
            os.environ.get("FRIDAY_CREDENTIALS_FILE", ""),
            CREDENTIALS_DEFAULT_PATH,
        ]

    def _discovered_path(self):
        for candidate in self._file_candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def _load(self):
        path = self._discovered_path()
        merged = {}
        if path:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    file_data = json.load(handle)
                if isinstance(file_data, dict):
                    for pool_name, entries in file_data.items():
                        if not isinstance(entries, list):
                            continue
                        keys = [str(k).strip() for k in entries if isinstance(k, str) and k.strip()]
                        if keys:
                            merged.setdefault(pool_name, []).extend(keys)
            except (OSError, ValueError) as error:
                print(f"[api_pools] credentials file unreadable: {error}", flush=True)
        for pool_name, spec in POOL_SCHEMA.items():
            merged.setdefault(pool_name, [])
            file_pool = spec.get("file")
            if file_pool:
                for key in list(merged.get(file_pool, [])):
                    if key not in merged[pool_name]:
                        merged[pool_name].append(key)
            for env_name in spec.get("envs", ()):
                key = os.environ.get(env_name, "").strip()
                if key and key not in merged[pool_name]:
                    merged[pool_name].append(key)
        # Synthetic stage pools reuse dedicated accounts for a different model.
        # Gemini quotas are per-account AND per-model, so transcribing on the
        # Brain+Vision accounts consumes separate model buckets and never
        # starves the parents' own usage.
        for synthetic in ("gemini_stt", "gemini_tts"):
            merged[synthetic] = list(merged.get(synthetic, []))
            for parent in ("gemini_brain", "gemini_vision"):
                for key in merged.get(parent, []):
                    if key not in merged[synthetic]:
                        merged[synthetic].append(key)
        return merged

    def pools(self):
        with self._lock:
            if self._data is None:
                self._data = self._load()
            return dict(self._data)


class Pool:
    """An ordered, per-function set of credentials with ban cooldowns and pacing."""

    def __init__(self, name, keys, cooldown=_COOLDOWN_SECONDS, min_interval=None):
        self.name = name
        self._keys = list(keys)
        self._cooldown = cooldown
        self._min_interval = min_interval if min_interval is not None else POOL_MIN_INTERVALS.get(name, 0.0)
        self._cursor = 0
        self._banned_until = {}
        self._last_used = {}
        self._strikes = {}
        self._lock = threading.Lock()

    @property
    def empty(self):
        return not self._keys

    def next_key(self, pace=True):
        """Return the next usable credential (round-robin, skipping banned).
        
        When pace is True, enforces the client-side minimum spacing delay for
        free API tiers to prevent rate-limit bursts ('AI overheating').
        """
        with self._lock:
            if not self._keys:
                return None
            size = len(self._keys)
            now = time.monotonic()
            for step in range(size):
                index = (self._cursor + step) % size
                if self._banned_until.get(index, 0.0) <= now:
                    self._cursor = (index + 1) % size
                    selected_key = self._keys[index]
                    if pace and self._min_interval > 0:
                        last = self._last_used.get(index, 0.0)
                        elapsed = now - last
                        if elapsed < self._min_interval:
                            sleep_time = self._min_interval - elapsed
                            time.sleep(sleep_time)
                            now = time.monotonic()
                    self._last_used[index] = now
                    return selected_key
            return None

    def total(self):
        return len(self._keys)

    def banned(self):
        now = time.monotonic()
        with self._lock:
            return sum(1 for until in self._banned_until.values() if until > now)

    def succeed(self, key):
        with self._lock:
            try:
                index = self._keys.index(key)
            except ValueError:
                index = None
            if index is not None:
                self._banned_until.pop(index, None)
            # FIX (2026-09-29): a real success RESETS the strike counter. It used
            # to be left untouched, so three unlucky failures spread over days
            # (each with a success in between) promoted to a 6-hour ban and
            # contradicted this class's own "3rd failure IN A ROW" contract.
            self._strikes.pop(key, None)
        _persist_ledger()

    def fail(self, key, cooldown=None):
        """Cooldown a key. Strike counter: the 3rd failure IN A ROW (no
        successful call since the last failure) promotes the cooldown to the
        full 6-hour exhaustion window (persisted), so flaky keys cannot
        ping-pong the chain with 20-120s turtle-hops forever, and a genuinely
        dead key eventually leaves the rotation instead of costing a request
        on every turn. `succeed()` resets the counter, so a key that recovers
        is trusted again immediately."""
        with self._lock:
            try:
                index = self._keys.index(key)
            except ValueError:
                return
            effective = cooldown or self._cooldown
            strikes = self._strikes.get(key, 0) + 1
            short_window = effective < self._cooldown
            if short_window and strikes >= 3:
                # Three short-window failures in a row = chronic exhaustion.
                effective = max(effective, self._cooldown)
                strikes = 0
            self._strikes[key] = strikes if effective < self._cooldown else 0
            self._banned_until[index] = time.monotonic() + effective
        _persist_ledger()


_store = CredentialStore()
_pools = None
_pools_guard = threading.Lock()


def _ensure_pools():
    global _pools
    with _pools_guard:
        if _pools is None:
            data = _store.pools()
            _pools = {
                name: Pool(name, data.get(name, []))
                for name in POOL_SCHEMA
            }
            # Persisted exhaustion bans (6-hour windows survive restarts).
            _restore_ledger()
        return _pools


def _ensure_pools_or_none():
    """Return the pools registry without forcing a boot-crash spiral: builds
    pools the first time (with ledger restore), never returns raw None."""
    try:
        return _ensure_pools()
    except Exception as error:
        print(f"[api_pools] pools build failed: {error}", flush=True)
        return {}


def pool(name):
    return _ensure_pools_or_none().get(name)


def sanitize(text):
    """Redact every known credential and common key shapes from a string."""
    output = str(text)
    for key in _all_known_keys():
        if key and len(key) >= 12:
            output = output.replace(key, "[REDACTED]")
    for pattern in _GENERIC_SECRETS:
        output = pattern.sub("[REDACTED]", output)
    return output


def _all_known_keys():
    keys = []
    for spec in POOL_SCHEMA.values():
        keys.extend(_store.pools().get(spec["file"], []))
    return keys


def describe_pools():
    """Non-secret snapshot of pool health for logging/UI."""
    current = _ensure_pools()
    summary = {}
    for name, item in current.items():
        summary[name] = {
            "total": item.total(),
            "banned": item.banned(),
            "available": max(0, item.total() - item.banned()),
        }
    return summary


def run_chain(pool_names, call, on_result=None):
    """Walk ordered quota pools and their keys until one succeeds.

    call(key, pool_name) must return (ok, payload, error, quota_failure);
    quota_failure True bans the key for the cooldown window, transient errors
    move on without banning so a flaky network can retry next time.

    Returns (ok, payload, provider_name, last_error).
    """
    current = _ensure_pools()
    last_error = ""
    for pool_name in pool_names:
        quota_pool = current.get(pool_name)
        if quota_pool is None or quota_pool.empty:
            continue
        attempted = 0
        for _ in range(quota_pool.total()):
            if attempted > 0:
                time.sleep(0.35)  # Polite backoff delay between fallback key attempts
            key = quota_pool.next_key()
            if key is None:
                if attempted == 0:
                    last_error = f"quota exhausted on {pool_name}"
                break
            attempted += 1
            try:
                ok, payload, error, quota_failure = call(key, pool_name)
            except Exception as exc:
                ok, payload, quota_failure = False, None, False
                error = str(exc)
            if on_result is not None:
                on_result(pool_name, key, ok, error)
            if ok:
                quota_pool.succeed(key)
                return True, payload, pool_name, ""
            if quota_failure:
                # FIX (2026-09-29): this used to call fail(key) with no cooldown,
                # which is a FULL 6-hour ban, persisted across restarts. On a free
                # tier a 429 is almost always a per-minute sliding-window burst -
                # the exact thing POOL_MIN_INTERVALS exists to soften - so one
                # burst could take a whole pool offline for six hours. Now:
                #   * honour the provider's own retry hint when it offers one
                #   * otherwise use a short window cooldown, and let the
                #     three-strikes rule escalate to 6h only for a key that
                #     keeps doing it (see Pool.fail)
                cooldown = _retry_after_seconds(error) or RATE_LIMIT_COOLDOWN_SECONDS
                quota_pool.fail(key, cooldown=cooldown)
                last_error = sanitize(f"{error}") if error else f"quota exhausted on {pool_name}"
            else:
                # Transient failure (timeout, 5xx, bad JSON). Previously NOTHING
                # was recorded, so a provider that is permanently broken cost a
                # full failing request on EVERY subsequent turn, forever. Now the
                # strike counter escalates it out of rotation after a few tries.
                quota_pool.fail(key, cooldown=TRANSIENT_COOLDOWN_SECONDS)
                last_error = sanitize(f"{error}") if error else f"failed on {pool_name}"
    return False, None, "", last_error


# --- Per-model quota tracking ------------------------------------------------
# Gemini's free tier enforces limits per account AND per model, so one key can
# serve an ordered CHAIN of models — each with its own private quota bucket.
# Bans here track (pool, model) pairs rather than keys.

_model_bans = {}
_model_ban_guard = threading.Lock()


def model_banned(pool_name, model):
    with _model_ban_guard:
        until = _model_bans.get((pool_name, model), 0.0)
    return until > time.monotonic()


def fail_model(pool_name, model, cooldown=None):
    until = time.monotonic() + (cooldown or _EXHAUSTION_COOLDOWN)
    with _model_ban_guard:
        _model_bans[(pool_name, model)] = until
    _persist_ledger()
    print(f"[api_pools] model {pool_name}/{model} cooling down {cooldown or _EXHAUSTION_COOLDOWN:.0f}s", flush=True)


def model_succeed(pool_name, model):
    with _model_ban_guard:
        _model_bans.pop((pool_name, model), None)