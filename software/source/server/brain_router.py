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

GROQ_BASE = os.environ.get("FRIDAY_GROQ_BASE", "https://api.groq.com/openai/v1")
OPENROUTER_BASE = os.environ.get("FRIDAY_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
GEMINI_BASE = os.environ.get(
    "FRIDAY_GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
LOCAL_BASE = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434")
LOCAL_MODEL = os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "ollama_chat/qwen3:8b")

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
    """Groq/OpenRouter 429s often carry 'Please try again in 17.3s'. Honor a
    bounded version of that hint (min 20s, max 120s) so short TPM blips
    recover quickly instead of locking the key away for 5 minutes."""
    match = _RETRY_HINT_RE.search(str(reason_text or ""))
    if not match:
        return None  # caller falls back to the pool default
    seconds = float(match.group(1))
    return max(20.0, min(120.0, seconds + 3.0))


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

    Cloud-first for every tier (see module docstring): chat and task/tool work
    both start on the fast cloud chain with local qwen3:8b as the complete
    fallback (safe: apply_for() storm guard locks any post-tool failover onto
    LOCAL, so mid-turn hops cannot re-run tools cross-provider). Explicit
    strongest-model requests start on the Gemini chain.
    """
    tier = classify(user_text)
    gemini_candidates = [
        ("brain_gemini", model, GEMINI_BASE) for model in GEMINI_MODEL_CHAIN
    ]
    openrouter_candidates = [
        ("brain_openrouter", model, OPENROUTER_BASE) for model in OPENROUTER_MODEL_CHAIN
    ]
    chosen = []
    if tier == "deep":
        # Explicit strongest-model request: Gemini chain first, then Groq
        # strong, local as the dependable fallback.
        chosen = [
            *gemini_candidates,
            ("brain_groq", GROQ_STRONG_MODEL, GROQ_BASE),
        ]
    # else normal / hard / task-tool: nothing pre-chosen -> cloud chain fills
    # in below (groq fast first) and "local" is appended as the complete
    # fallback, for ALL turns. Tool work is no longer local-first: the
    # storm guard in apply_for() keeps cross-provider tool re-runs bounded.
    for candidate in [
        ("brain_groq", GROQ_FAST_MODEL, GROQ_BASE),
        ("brain_groq", GROQ_STRONG_MODEL, GROQ_BASE),
        *gemini_candidates,
        *openrouter_candidates,
    ]:
        if candidate not in chosen:
            chosen.append(candidate)
    chosen.append(("local", None, None))
    return chosen


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
            self._configure(interpreter, None, LOCAL_BASE, None)
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
        if new_ticket:
            new_pool, new_key, new_model = new_ticket
            self._last_switch = (
                f"{old_label} exhausted ({api_pools.sanitize(reason)[:60]}) "
                f"-> switched to {new_pool}/{new_model}"
            )
        else:
            self._last_switch = f"{old_label} exhausted everything -> local qwen3:8b"
        print(f"[brain switch] {self._last_switch}", flush=True)
        return new_ticket is not None

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
        model_level = pool_name == "brain_gemini" or (
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
                # Exhaustion (429/TPM/RPD/etc.): 6-HOUR cooldown on that key
                # (Sir directive) â€” persisted so restarts honor the window.
                # The provider's own short retry hint ("try again in 6.6s")
                # is still honored for tiny blips; big hints/daily quotas
                # collapse into the 6-hour exhaustion window either way.
                hint = _retry_after_seconds(reason_text)
                cooldown = hint if (hint is not None and hint <= 120.0) else None
                quota_pool.fail(key, cooldown=cooldown)
            api_pools.model_succeed(pool_name, model)
        else:  # safe default: park the key only briefly; never hang the turn
            quota_pool = api_pools.pool(pool_name)
            if quota_pool is not None:
                quota_pool.fail(key, cooldown=_retry_after_seconds(reason_text))
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
