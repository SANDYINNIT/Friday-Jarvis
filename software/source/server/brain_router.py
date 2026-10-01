"""Per-turn Brain routing: tier classification + model/pool selection.

Cloud-first routing for every tier (2026-09-28, Sir's decision after the
hybrid's local-for-tools leg backfired: slow local qwen3 prefill + the
wrong-app GUI-truthfulness bug):
  - normal/casual chat AND hard/task-tool work share one cloud-first chain:
    groq fast (gpt-oss-20b) -> groq strong -> gemini chain -> openrouter
    chain -> local qwen3:8b as the COMPLETE fallback.
  - deep  (explicit "use gemini"/strongest): gemini chain first ->
    groq strong -> openrouter -> local.
Mid-turn storm guard: once a tool row exists in interpreter.messages this
turn, failover only ever lands back on local (no cross-cloud hop that
forgets what already ran) - this is what keeps cloud-held tool turns safe.
A spoken override ("use your strongest model") forces the deep tier.
"""

import os
import re
import threading

from . import api_pools
from .file_logger import friday_logger as _friday_logger

_flog = _friday_logger()

GROQ_BASE = os.environ.get("FRIDAY_GROQ_BASE", "https://api.groq.com/openai/v1")
OPENROUTER_BASE = os.environ.get("FRIDAY_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
GEMINI_BASE = os.environ.get(
    "FRIDAY_GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
LOCAL_BASE = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434")
LOCAL_MODEL = os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "ollama_chat/qwen3:8b")

# Modal serverless GPU endpoint - PRIMARY brain (Sir directive 2026-09-30).
# It is a self-hosted GLM-5.3 Flash (NVFP4) server exposing an OpenAI-compatible
# /v1, so it behaves exactly like the other cloud candidates and needs no custom
# auth: the pool key is already the combined "wk-<id>.ws-<secret>" bearer token.
def _modal_api_base():
    """Modal's OpenAI-compatible base, normalised to end in /v1.

    Litellm appends /chat/completions itself, so the base MUST include /v1.
    Live-verified 2026-09-30: without it the endpoint answers
    404 {"error": "route not found"} and every Modal turn fails over.
    """
    raw = (
        os.environ.get("FRIDAY_MODAL_BASE", "").strip()
        or api_pools.extra_credential("modal_endpoint")
    ).rstrip("/")
    if not raw:
        return None
    if not raw.endswith("/v1"):
        raw = raw + "/v1"
    return raw


MODAL_BASE = _modal_api_base()
# Model id read from the live endpoint's /v1/models (2026-09-30):
#   nvidia/GLM-5.3-Flash-NVFP4
# Do not guess this - Modal serves the API under the exact served-model-name.
MODAL_MODEL = os.environ.get(
    "FRIDAY_MODAL_MODEL", "nvidia/GLM-5.3-Flash-NVFP4"
)

GROQ_FAST_MODEL = os.environ.get("FRIDAY_GROQ_FAST_MODEL", "openai/gpt-oss-20b")
GROQ_STRONG_MODEL = os.environ.get("FRIDAY_GROQ_STRONG_MODEL", "openai/gpt-oss-120b")
# OpenRouter free-model inventory CHANGES and stale slugs return 404
# "unavailable for free" (live-verified Sep 2026: deepseek-chat-v3-0324:free
# is gone). Chain today's healthy free tool-calling models, strongest first:
OPENROUTER_BRAIN_MODEL = os.environ.get(
    "FRIDAY_OPENROUTER_BRAIN_MODEL", "nvidia/nemotron-3.5-lightning:free"
)
OPENROUTER_MODEL_CHAIN = [
    name.strip()
    for name in os.environ.get(
        "FRIDAY_OPENROUTER_MODEL_CHAIN",
        "nvidia/nemotron-3.5-lightning:free,"
        "nvidia/nemotron-3-ultra-550b-a55b:free,"
        "google/gemma-4-31b-it:free",
    ).split(",")
    if name.strip()
]
if OPENROUTER_BRAIN_MODEL not in OPENROUTER_MODEL_CHAIN:
    OPENROUTER_MODEL_CHAIN.append(OPENROUTER_BRAIN_MODEL)
# Gemini free-tier quotas are per-account AND per-model, so the Gemini stage
# is an ordered chain of models on the SAME account â€” each model has its own
# quota bucket. Strongest first, newest first; env-overridable.
GEMINI_BRAIN_MODEL = os.environ.get("FRIDAY_GEMINI_BRAIN_MODEL", "gemini-3.1-flash-lite")
GEMINI_MODEL_CHAIN = [
    name.strip()
    for name in os.environ.get(
        "FRIDAY_GEMINI_CHAIN",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",")
    if name.strip()
]
if GEMINI_BRAIN_MODEL not in GEMINI_MODEL_CHAIN:
    GEMINI_MODEL_CHAIN.append(GEMINI_BRAIN_MODEL)

DEEP_TRIGGERS = [
    "use your strongest",
    "use the strongest",
    "strongest model",
    "use gemini",
    "high-stakes",
    "high stakes",
    "most important",
    "once in a lifetime",
]
HARD_TRIGGERS = [
    r"\bresearch\b",
    r"\bresearcher\b",
    r"\bscientific\b",
    r"\bpaper\b",
    r"\banaly[sz]",
    r"\bdebug",
    r"\brefactor",
    r"\bsecurity\b",
    r"\bvulnerab",
    r"\barchitecture\b",
    r"\bdesign a\b",
    r"\bplan this\b",
    r"\breview this code\b",
    r"\bexplain why\b",
    r"\broot cause\b",
    r"\bproduction\b",
    r"\bdeploy\b",
    r"\bmigration\b",
    r"\boptimize\b",
    r"\bperformance\b",
    r"\bdifficult\b",
    r"\bcomplex\b",
    r"\bcomplicated\b",
    r"\bdeep reasoning\b",
    r"\bstep[- ]by[- ]step plan\b",
    r"\blarge[\s-]context\b",
    r"\blong document\b",
    r"https?://",
    r"github\.com/",
]

# Imperative action/tool requests route LOCAL-first (deterministic offline
# tool path; free-tier cloud 429 mid-turn hops historically re-ran tools to
# the loop cap). Conservative on purpose: clear app/system/command verbs and
# explicit "do this now" phrasing only, so casual chat stays cloud-first.
TOOL_TASK_TRIGGERS = [
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|go\s+ahead\s+and\s+)?(?:open|close|launch|start|stop|kill|terminate|shutdown|restart|click|install|uninstall|set up|play|pause|download|delete|move|copy|rename|create|save|take a screenshot|scan|type|press|mute|unmute)\b",
    r"\binstall\b",
    r"\buninstall\b",
    r"\b(?:open|launch|start|close|stop|kill|terminate)\s+(?:the\s+)?[a-z0-9_.-]+\s+(?:app|application|program|window|file|folder|tab|site|website|url|browser)\b",
    r"\bclick\s+(?:on\s+)?(?:the\s+)?[a-z0-9_-]+\s+(?:button|icon|link|tab|menu)\b",
    r"\btake\s+a\s+screenshot\b",
    r"\bset\s+(?:the\s+)?volume",
    r"\bwrite\s+(?:a|an|the|this)\s+(?:python|script|program|file|function|class)\b",
]

_REDUCED = re.compile(r"(redacted)", re.IGNORECASE)

_KEY_ERROR_MARKERS = (
    "rate limit", "429", "401", "unauthorized", "402", "payment required",
    "403", "forbidden", "tpm", "rpm", "quota", "insufficient",
)


def _is_key_error(reason_text):
    lowered = str(reason_text or "").lower()
    return any(marker in lowered for marker in _KEY_ERROR_MARKERS)


_MODEL_ERROR_MARKERS = (
    "notfound", "404", "unavailable for free", "unavailable",
    "failed to parse tool call", "does not exist", "no such model",
)


def _reason_kills_model(reason_text):
    lowered = str(reason_text or "").lower()
    return any(marker in lowered for marker in _MODEL_ERROR_MARKERS)


_RETRY_HINT_RE = re.compile(r"try\s+again\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)


def throttle_wait_seconds(reason_text, max_wait=30.0):
    """Return how long to WAIT-and-retry THE SAME candidate (provider+key)
    before giving up and hopping providers. Groq 429s carry
    'Please try again in 2.63s' style hints; a short window (< 30s) means
    waiting is cheaper than burning the next provider / re-running tools.
    Returns the hint (bounded) or None (not a short-window throttle)."""
    lowered = str(reason_text or "").lower()
    if not any(marker in lowered for marker in ("rate limit", "tpm", "rpm", "quota", "429")):
        return None
    match = _RETRY_HINT_RE.search(lowered)
    if not match:
        # Payload without the numeric hint ("TPM" style) — still a throttle:
        # give the brain a fixed breather instead of hopping instantly.
        return 12.0
    seconds = float(match.group(1))
    if seconds > max_wait:
        return None  # long window: hop providers instead of waiting
    return max(2.5, min(max_wait, seconds + 1.5))


def _retry_after_seconds(reason_text, default_cooldown=None):
    """Honour a provider's own "try again in Ns" / "retry in Ns" hint.

    FIX (2026-09-30): this used to be a SECOND, divergent parser that only
    matched Groq/OpenRouter's "try again in" wording and clamped to 20-120s, so
    Google's "Please retry in 29.68s" was ignored and the key got a flat 60s
    instead of the time Google actually asked for. One parser now wins:
    api_pools._retry_after_seconds, which handles both phrasings. The extra
    3s padding is kept so we never come back a second too early.
    """
    hint = api_pools._retry_after_seconds(reason_text)
    if hint is None:
        return None  # caller falls back to the pool default
    return min(120.0, hint + 3.0)


def classify(user_text):
    """Return 'deep', 'hard' or 'normal' for the given user message."""
    text = _REDUCED.sub("", user_text or "")
    lowered = text.lower()
    if any(trigger.lower() in lowered for trigger in DEEP_TRIGGERS):
        return "deep"
    if any(re.search(pattern, lowered) for pattern in HARD_TRIGGERS):
        return "hard"
    if any(re.search(pattern, lowered) for pattern in TOOL_TASK_TRIGGERS):
        return "hard"
    return "normal"


def resolve(user_text):
    """Ordered candidates (pool_name, model, api_base) for a message.

    ORDER (Sir directive 2026-09-30), identical for every tier:

        1. Modal  - self-hosted GLM-5.3 Flash (NVFP4), the PRIMARY brain
        2. Groq   - gpt-oss-20b then gpt-oss-120b
        3. OpenRouter - free tool-calling models
        4. Gemini - flash chain
        5. Local  - qwen3:8b, the complete final fallback

    Modal leads because it is a serverless endpoint Sir owns: it has no public
    free-tier quota to exhaust and no competitor sharing it, so it does not
    429 the way Groq/Gemini do. `apply_for()` also skips any candidate whose
    pool has no key, so a user who configures only ONE provider simply gets a
    one-entry chain - they never have to fill in every pool.
    """
    tier = classify(user_text)
    modal_candidates = []
    if MODAL_BASE:
        modal_candidates = [("brain_modal", MODAL_MODEL, MODAL_BASE)]
    groq_candidates = [
        ("brain_groq", GROQ_FAST_MODEL, GROQ_BASE),
        ("brain_groq", GROQ_STRONG_MODEL, GROQ_BASE),
    ]
    openrouter_candidates = [
        ("brain_openrouter", model, OPENROUTER_BASE) for model in OPENROUTER_MODEL_CHAIN
    ]
    gemini_candidates = [
        ("brain_gemini", model, GEMINI_BASE) for model in GEMINI_MODEL_CHAIN
    ]
    chosen = []
    # Modal is PRIMARY for every tier, including "use your strongest" (Sir
    # directive): GLM-5.3 Flash on a self-hosted endpoint is both the fastest
    # and the most dependable option available, so the deep tier must not jump
    # ahead of it. Gemini remains in the chain, just later.
    for candidate in [
        *modal_candidates,
        *groq_candidates,
        *openrouter_candidates,
        *gemini_candidates,
    ]:
        if candidate not in chosen:
            chosen.append(candidate)
    chosen.append(("local", None, None))
    return chosen


# Live serving state, published by BrainRouter and read by self_awareness so
# FRIDAY can name the model that is REALLY answering (model self-awareness).
_LIVE_ROUTER = {"router": None}


def live_serving_state() -> dict:
    """What model/pool/tier is serving turns RIGHT NOW (and what ran last).

    Read-only and defensive: never raises, so self-reporting can never break a
    turn. Returns counts/names only - never keys.
    """
    router = _LIVE_ROUTER.get("router")
    if router is None:
        return {"ready": False, "current_model": None, "current_pool": None,
                "current_tier": None, "last_model": None, "last_pool": None,
                "last_tier": None, "local_default": LOCAL_MODEL}

    def _get(name, default=None):
        try:
            value = getattr(router, name, default)
        except Exception:
            return default
        return value

    return {
        "ready": True,
        "current_model": _get("current_model"),
        "current_pool": _get("current_pool"),
        "current_tier": _get("current_tier"),
        "last_model": _get("last_model"),
        "last_pool": _get("last_pool"),
        "last_tier": _get("last_tier"),
        "local_default": LOCAL_MODEL,
    }


class BrainRouter:
    """Applies the best current Brain candidate to an interpreter.llm."""
    def __init__(self, enabled=True):
        self._enabled = enabled
        self._current = None
        self._current_tier = None
        self._current_model = None
        self._current_pool = None
        # Persisted last-serving state so the UI can show WHAT last ran even
        # after the turn completes (current clears on mark_success).
        self._last_model = None
        self._last_pool = None
        self._last_tier = None
        # Publish to a module-level slot so self_awareness can report the model
        # that is REALLY serving turns, without importing server.py (which
        # would be circular). Sir asked for model self-awareness: she must be
        # able to name the live model, not a guess from a static prompt.
        _LIVE_ROUTER["router"] = self
        self._last_switch = "No provider switches yet."
        # THIS TURN's blacklist (pool_name, model): every candidate that
        # already failed once this turn is skipped — no bouncing back to
        # keys that "healed" via a 20s cooldown mid-turn. Strict forward
        # only: next, next, next, until local. Cleared by new_turn().
        self._turn_skipped = set()
        self._lock = threading.Lock()

    def new_turn(self):
        with self._lock:
            self._turn_skipped.clear()

    def _turn_skip(self, pool_name, model):
        with self._lock:
            return (pool_name, model) in self._turn_skipped

    def _turn_mark(self, pool_name, model):
        with self._lock:
            self._turn_skipped.add((pool_name, model))

    @property
    def current(self):
        with self._lock:
            return self._current

    @property
    def current_tier(self):
        with self._lock:
            return self._current_tier

    @property
    def current_model(self):
        with self._lock:
            return self._current_model

    @property
    def current_pool(self):
        with self._lock:
            return self._current_pool

    def apply_for(self, user_text, interpreter):
        ticket = None
        tier = classify(user_text)
        model_name = None
        pool_name_selected = None
        if self._enabled:
            # Mid-turn storm guard: if a tool/computer row already appeared
            # AFTER the turn's user message, refuse to hop across cloud
            # providers (a mid-turn cloud 429 used to send the hop target
            # re-running already-done tools until the loop cap). Failover can
            # only land back on local, which sees the same completed work.
            _tool_this_turn = False
            try:
                for _m in reversed(interpreter.messages):
                    if _m.get("role") == "user":
                        break
                    if _m.get("role") in ("computer", "tool"):
                        _tool_this_turn = True
                        break
            except Exception:
                _tool_this_turn = False
            for pool_name, model, base in resolve(user_text):
                if _tool_this_turn and pool_name != "local":
                    continue
                if pool_name == "local":
                    # Local is the primary but must remain FAILOVER-ABLE: once
                    # it failed this turn it is skipped so the cloud chain can
                    # take over instead of re-sticking to a dead local brain.
                    if self._turn_skip("local", None):
                        continue
                    self._configure(interpreter, None, LOCAL_BASE, None)
                    model_name = LOCAL_MODEL
                    pool_name_selected = "local"
                    ticket = ("local", None, LOCAL_MODEL)
                    break
                # Per-account-per-model quotas: a model-banned gemini stage
                # entry means "this model's bucket is hot" â€” try the next
                # model on the same account, not the next provider.
                if api_pools.model_banned(pool_name, model):
                    continue
                # This-turn blacklist: a candidate that failed once in THIS
                # turn is done for the turn (next, next, next — no bouncing
                # back to briefly-cooled keys).
                if self._turn_skip(pool_name, model):
                    continue
                quota_pool = api_pools.pool(pool_name)
                key = None
                if quota_pool is not None and not quota_pool.empty:
                    key = quota_pool.next_key()
                if not key:
                    continue
                self._configure(interpreter, model, base, key)
                ticket = (pool_name, key, model)
                model_name = model
                pool_name_selected = pool_name
                break
        if ticket is None:
            # FIX (2026-09-29): the local fallback left `ticket` as None, so
            # `self._current` was None and mark_failure() short-circuited on
            # `if not ticket: return` — meaning a LOCAL brain failure was never
            # recorded anywhere (no ledger, no console, no HUD). That silence is
            # what let a fully dead chain spin unnoticed. The local candidate is
            # now a real ticket like any other, so it is visible and honest, and
            # failover_to_next() reports the chain is exhausted.
            self._configure(interpreter, None, LOCAL_BASE, None)
            ticket = ("local", None, LOCAL_MODEL)
            model_name = LOCAL_MODEL
            pool_name_selected = "local"
        with self._lock:
            self._current = ticket
            self._current_tier = tier
            self._current_model = model_name
            self._current_pool = pool_name_selected
            if ticket is not None:
                self._last_model = model_name
                self._last_pool = pool_name_selected
                self._last_tier = tier
        return ticket

    @property
    def last_model(self):
        with self._lock:
            return self._last_model

    @property
    def last_pool(self):
        with self._lock:
            return self._last_pool

    @property
    def last_tier(self):
        with self._lock:
            return self._last_tier

    @property
    def last_switch(self):
        with self._lock:
            return self._last_switch

    def failover_to_next(self, interpreter, user_text, reason=""):
        ticket = self.current
        if not ticket:
            return False

        old_label = f"{self.current_pool}/{self.current_model}"
        pool_name, key, model = ticket
        self._turn_mark(pool_name, model)
        self.mark_failure(reason=reason)
        # Apply for the same text to get the next candidate (next model on
        # the same Gemini account, next key in a pool, or next provider)
        new_ticket = self.apply_for(user_text, interpreter)
        if not new_ticket:
            # DIAG (2026-09-30): observed live - Groq 429 twice, then
            # "brain chain exhausted ... no candidate left" even though local
            # qwen3:8b was never tried. Log exactly why the walk produced
            # nothing, because the isolated repro returns local fine.
            try:
                remaining = [
                    (pool, model)
                    for pool, model, _base in resolve(user_text)
                ]
                reasons = []
                for pool, model in remaining:
                    if self._turn_skip(pool, model):
                        reasons.append(f"{pool}/{model}: turn-skip")
                    elif pool != "local" and api_pools.model_banned(pool, model):
                        reasons.append(f"{pool}/{model}: model-banned")
                    elif pool != "local":
                        quota = api_pools.pool(pool)
                        if quota is None or quota.empty:
                            reasons.append(f"{pool}/{model}: no key")
                _flog.warning(
                    "failover found no candidate. turn_skips=%s skip_reasons=%s",
                    sorted(self._turn_skipped),
                    reasons or ["<none - all candidates looked usable>"],
                )
            except Exception as diag_error:
                _flog.warning("failover diagnostic failed: %s", diag_error)
        if new_ticket:
            new_pool, new_key, new_model = new_ticket
            self._last_switch = (
                f"{old_label} exhausted ({api_pools.sanitize(reason)[:60]}) "
                f"-> switched to {new_pool}/{new_model}"
            )
        else:
            self._last_switch = f"{old_label} exhausted everything -> local qwen3:8b"
        print(f"[brain switch] {self._last_switch}", flush=True)
        # FIX (2026-09-29): report exhaustion truthfully. Falling back to LOCAL is
        # the end of the cloud chain, not "another candidate found", so returning
        # True here would make the caller keep hopping a turn that has nowhere
        # left to go. Only a real cloud candidate counts as a successful hop.
        return bool(new_ticket) and new_ticket[0] != "local"

    def mark_success(self):
        ticket = self.current
        if not ticket:
            return
        pool_name, key, model = ticket
        with self._lock:
            self._current = None
            self._current_pool = None
            self._current_model = None
            self._current_tier = None
        if pool_name == "local":
            return  # no quota pool/cooldown bookkeeping for the local brain
        quota_pool = api_pools.pool(pool_name)
        if quota_pool is not None:
            quota_pool.succeed(key)
        api_pools.model_succeed(pool_name, model)

    def mark_failure(self, reason=""):
        ticket = self.current
        if not ticket:
            return
        pool_name, key, model = ticket
        with self._lock:
            self._current = None
            self._current_pool = None
            self._current_model = None
            self._current_tier = None

        reason_text = str(reason or "")
        lowered_reason = reason_text.lower()
        # The local candidate has no key pool and no model quota to cool. Record
        # it honestly (console + scratchpad) and let the chain be exhausted -
        # api_pools bookkeeping below would otherwise write meaningless
        # "local:qwen3:8b" model-ban entries into the persisted ledger.
        if pool_name == "local":
            print(
                f"[brain failover] local brain {model} failed: "
                f"{api_pools.sanitize(reason_text)[:200]}",
                flush=True,
            )
            return
        # Gemini 3's thought_signature validator hard-400s when the tool-call
        # history was generated by ANOTHER provider (Groq) — a history-format
        # problem, not a Gemini model death. Sanity-sanitized turns retry
        # fine moments later, so use a SHORT cooldown instead of the 6h ban.
        if "thought_signature" in lowered_reason:
            api_pools.fail_model(pool_name, model, cooldown=120.0)
            return
        # Provider-side capacity blips (503 "high demand" / overloaded /
        # spike errors) are TRANSIENT — minutes, never the 6h window.
        if any(marker in lowered_reason for marker in (
            "503", "high demand", "service unavailable",
            "overloaded", "try again later",
        )):
            api_pools.fail_model(pool_name, model, cooldown=300.0)
            return
        # Model-level ban. FIX (2026-09-29): this used to fire for EVERY Gemini
        # error, so five unrelated network blips could take the whole Gemini
        # stage offline for the day. A model ban is now reserved for errors that
        # genuinely indict the MODEL (quota/permission on that model id, or a
        # stale slug); everything else falls through to the key-level paths
        # below, which use short cooldowns.
        gemini_model_dead = pool_name == "brain_gemini" and _reason_kills_model(reason_text)
        model_level = gemini_model_dead or (
            pool_name == "brain_openrouter" and _reason_kills_model(reason_text)
        ) or (pool_name == "brain_groq" and "parse tool call" in reason_text.lower())
        if model_level:
            # Gemini quotas AND stale OpenRouter slugs (404 unavailable-for-
            # free) are per-MODEL problems. Banning the account would waste
            # healthy siblings: cool the model, keep the account hot.
            api_pools.fail_model(pool_name, model)
        elif _is_key_error(reason_text):
            quota_pool = api_pools.pool(pool_name)
            if quota_pool is not None:
                # A real quota signal is a short sliding-window burst far more
                # often than a dead key, so start with the provider's hint (or a
                # 60s window) and let the three-strikes rule escalate to the
                # 6-hour window only if the key keeps doing it.
                hint = _retry_after_seconds(reason_text)
                quota_pool.fail(key, cooldown=hint or api_pools.RATE_LIMIT_COOLDOWN_SECONDS)
            api_pools.model_succeed(pool_name, model)
        else:  # safe default: park the key only briefly; never hang the turn
            quota_pool = api_pools.pool(pool_name)
            if quota_pool is not None:
                # FIX (2026-09-29): this branch is commented "park the key only
                # briefly" but passed None whenever the provider gave no retry
                # hint, and Pool.fail maps None to the FULL 6-hour window - so the
                # documented safe default was the main source of 6h bans. An
                # uncategorised error now gets a short cooldown.
                quota_pool.fail(key, cooldown=_retry_after_seconds(reason_text)
                                or api_pools.TRANSIENT_COOLDOWN_SECONDS)
            api_pools.model_succeed(pool_name, model)
        if reason:
            print(f"[brain failover] {pool_name}/{model} marked failed: {api_pools.sanitize(reason)[:200]}", flush=True)

    @staticmethod
    def _configure(interpreter, model, base, key):
        try:
            llm = interpreter.llm
        except Exception:
            return
        
        # Explicitly reset tool-calling state to prevent leakage
        llm.supports_functions = False
        llm.supports_function_calling = False
        llm.tool_choice = None
        
        local = model is None
        try:
            llm.model = LOCAL_MODEL if local else "openai/" + model
            llm.api_base = base
            llm.api_key = key
            
            # OI dispatches tool calling on llm.supports_functions (llm.py:320)
            # — NOT supports_function_calling — so tool-capable candidates must
            # run through run_tool_calling_llm, which sends `tools` +
            # `tool_choice=auto`. Cloud AND local qwen3:8b (natively
            # tool-calling) both use it; the old markdown code-block path let
            # qwen3 REPLY about actions instead of executing them (fabricated
            # "I opened Notepad" with zero code run — live-verified 2026-09-28).
            llm.supports_functions = True
            llm.supports_function_calling = True
            llm.tool_choice = "auto"
            print(
                f"[brain router] configured "
                f"{LOCAL_MODEL if local else model} with tool_choice=auto",
                flush=True,
            )
        except Exception as error:
            print(f"[brain router] configure error: {api_pools.sanitize(error)}", flush=True)
