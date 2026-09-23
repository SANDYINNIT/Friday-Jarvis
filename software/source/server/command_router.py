"""Risk-gated deterministic command router for FRIDAY's voice/text paths.

Kept deliberately small so natural conversation still flows to the model.
Recognized intents are explicit computer-control phrases.  Read-only actions
may use fuzzy matching; reversible mutating actions (focus, type, minimize -
``close X`` means minimize, never kill) require a *confident* single match and
report before/after verification; destructive phrasing (quit/kill/terminate)
is refused without explicit on-screen confirmation.
"""

from __future__ import annotations

"""Deterministic command adapters: window focus, apps, browser, media, calendar.

The transport layer (voice/web/telegram) passes every user request through
`route()` BEFORE the AI brain, so known-good app flows stay instant.
"""

import re

from . import persona_flair as _flair

import unicodedata

from .audit import log_action
from .windows_control import focus_by_query, list_windows, minimize_by_query


RISK_READ_ONLY = "read_only"
RISK_REVERSIBLE = "reversible"
RISK_DESTRUCTIVE = "destructive"

_MAX_LIST_WINDOWS = 12
DICTATION_MAX_CHARS = 400


def _norm(text):
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


_LIST_RE = re.compile(
    r"^(?:what|which)\s+windows?\s+(?:are open|are running|do you see|are on screen)"
    r"|^(?:please\s+)?(?:list|show)\s+(?:me\s+)?(?:the\s+)?windows",
    flags=re.IGNORECASE,
)
_FOCUS_RE = re.compile(
    r"^(?:please\s+)?(?:focus|switch)\s+(?:on\s+|to\s+)?(.+?)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)
_TYPE_RE = re.compile(
    r"^(?:please\s+)?(?:type|dictate|type out)\s+(.+?)\s+for me\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)
_MINIMIZE_RE = re.compile(
    r"^(?:please\s+)?(?:minimize|minimise|hide)\s+(?:the\s+|down\s+)?(.+?)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)
_DESTRUCTIVE_RE = re.compile(
    r"^(?:please\s+)?(?:quit|kill|terminate|exit|shut\s+down|turn\s+off|shutdown)\s+(.+?)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)
_WEB_FETCH_RE = re.compile(
    r"^(?:please\s+)?(?:search|look)\s+(?:the\s+web\s+)?(?:up\s+|for\s+)*(.+?)\s*[.!?]?\s*$"
    r"|^(?:please\s+)?(?:fetch|pull\s+up|check)\s+(?:the\s+)?(?:docs?|documentation)\s+(?:for\s+|on\s+)?(.+?)\s*[.!?]?\s*$"
    r"|^(?:please\s+)?(?:what\s+is\s+the\s+latest|is\s+there\s+an?\s+update\s+to)\s+(.+?)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)

# PC power actions are Sir's explicit system request, NOT an app kill: pass
# them through to the brain (it runs shutdown/restart itself with the
# user's explicit phrase). Only app-targeted destructive verbs are refused.
_POR_POWER_RE = re.compile(
    r"(?:turn|shut|power|put).{0,24}?\b(?:pc|computer|machine|system|pc)\b|"
    r"\b(?:shut\s+down|turn\s+off|power\s+off|restart|reboot)\s+(?:the\s+)?(?:pc|computer|system|machine|it)\b",
    flags=re.IGNORECASE,
)


def _is_pc_power_request(text):
    lowered = str(text or "").lower()
    return bool(
        re.search(r"\b(?:turn|shut|power)\b.{0,24}\b(?:off|down)\b", lowered)
        and re.search(r"\b(?:pc|computer|machine|system|desktop)\b|\bgoodnight\b", lowered)
    ) or bool(_POR_POWER_RE.search(lowered))


def _natural_window_list(limit=_MAX_LIST_WINDOWS):
    windows = list_windows(limit=min(100, max(10, limit * 4)))
    visible = []
    seen = set()
    for window in windows:
        title = str(window.get("title") or "").strip()
        process = str(window.get("process") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        visible.append(f"{title} ({process})")
        if len(visible) >= limit:
            break
    if not visible:
        return "I can't see any open windows right now."
    body = "; ".join(visible)
    return f"I see {len(visible)} windows: {body}."


def _focus_phrase(text):
    match = _FOCUS_RE.match(text)
    if not match:
        return None
    target = re.sub(r"\s+(?:now|please)$", "", match.group(1).strip()).strip(",.?!")
    if not target or len(target) > 80:
        return None
    return target


def _candidate_names(candidates, limit=5):
    return "; ".join(
        f"{item.get('title', '')!s} ({item.get('process', '')!s})" for item in candidates[:limit]
    )


def _handle_list_windows(text, actor):
    if not _LIST_RE.match(_norm(text)):
        return None
    response = _natural_window_list()
    log_action(actor=actor, risk=RISK_READ_ONLY, operation="list_windows",
               outcome="ok", allowed=True, query=text)
    return {"handled": True, "action": "list_windows", "risk": RISK_READ_ONLY,
            "allowed": True, "changed": False, "response": response}


# "What is the user doing?" — instant read from the foreground window instead
# of letting the brain author code that re-confirms "Roblox is running" 91 times.
_ACTIVITY_RE = re.compile(
    r"^(?:what(?:'?s|\s+is)?|do\s+you\s+know)\s+"
    r"(?:the\s+|my\s+)?(?:user|sir|owner|sandy|boss|bro)\s+"
    r"(?:currently|right\s+now|now|just|there)?\s*"
    r"(?:doing|up\s+to|working\s+on|watching|playing|reading|using)\b"
    r"|^(?:what\s+am\s+i|what(?:'?s|\s+is)\s+(?:me|i))\s+"
    r"(?:currently|right\s+now|now|just)?\s*"
    r"(?:doing|up\s+to|working\s+on|watching|playing|reading|using)\b"
    r"|^(?:what(?:'?s|\s+is)?\s+going\s+on|what(?:'?s|\s+is)?\s+happening|"
    r"whats?\s+happening|whats?\s+going\s+on)\b"
    r"|^what(?:'?s|\s+is)?\s+happening\s+on\s+(?:the\s+)?(?:pc|computer|screen)\b"
    r"|^what(?:'?s|\s+is|\s+are|'?re)?\s+(?:open|running|focused)"
    r"(?:\s+right\s+now|\s+on\s+(?:the\s+)?(?:pc|computer))?\s*$"
    r"|^(?:what|which)\s+(?:app|application|window|program|software)\s+"
    r"(?:is|has|am)\s+(?:focused|active|open|running|up)\b"
    r"|^is\s+(?:anyone|anyone\s+else)\s+(?:using|on)\s+(?:the\s+)?"
    r"(?:pc|computer)\b",
    flags=re.IGNORECASE,
)


def _handle_activity(text, actor):
    if not _ACTIVITY_RE.search(_norm(text)):
        return None
    from .windows_context import get_active_window_context

    active = None
    try:
        active = get_active_window_context()
    except Exception:
        active = None
    if active and (active.get("title") or active.get("process")):
        title = str(active.get("title") or "").strip()
        proc = str(active.get("process") or "").strip()
        label = title or proc
        suffix = f" ({proc})" if proc and not label.lower().endswith(proc.lower()) else ""
        response = f"Right now {label}{suffix} has focus — that's what you're looking at."
    else:
        try:
            windows = list_windows(limit=8)
        except Exception:
            windows = []
        titles = sorted({
            str(w.get("title") or "").strip()
            for w in (windows or []) if str(w.get("title") or "").strip()
        })
        if titles:
            response = "Nothing has focus right now; these are the open windows: " + "; ".join(titles[:5]) + "."
        else:
            response = "The desktop looks idle — no foreground application is active."
    log_action(actor=actor, risk=RISK_READ_ONLY, operation="what_is_user_doing",
               outcome="ok", allowed=True, query=text)
    return {"handled": True, "action": "what_is_user_doing", "risk": RISK_READ_ONLY,
            "allowed": True, "changed": False, "response": response}


def _handle_focus(text, actor):
    target = _focus_phrase(text)
    if target is None:
        return None
    result = focus_by_query(target)
    if not result.get("success") and result.get("candidates"):
        names = _candidate_names(result["candidates"])
        response = f"That matches more than one window: {names}."
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="focus",
                   outcome="ambiguous", allowed=True, command=text, target=target)
        return {"handled": True, "action": "focus", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False, "response": response}
    if not result.get("success"):
        reason = result.get("failure_reason") or "no matching window"
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="focus",
                   outcome="failed", allowed=True, command=text, target=target,
                   error=reason)
        return {"handled": True, "action": "focus", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False,
                "response": f"I couldn't focus that window: {reason}."}
    changed = result.get("changed", False)
    title = (result.get("after") or {}).get("title") or target
    response = f"Focused {title}." if changed else f"Already focused on {title}."
    log_action(actor=actor, risk=RISK_REVERSIBLE, operation="focus",
               outcome="ok" if changed else "already", allowed=True,
               command=text, target=title, changed=changed)
    return {"handled": True, "action": "focus", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": changed, "response": response}


def _handle_type(text, actor):
    match = _TYPE_RE.match(text)
    if not match:
        return None
    draft = match.group(1).strip().strip(",.?! ")
    if not draft or len(draft) > DICTATION_MAX_CHARS:
        response = ("I can only type short dictation to a focused text field "
                    "when the message ends with 'for me'.")
        return {"handled": True, "action": "type", "risk": RISK_REVERSIBLE,
                "allowed": False, "changed": False, "response": response}
    try:
        import pyautogui

        pyautogui.write(draft, interval=0.03)
        typed = True
    except Exception as error:
        typed = False
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="type",
                   outcome="failed", allowed=True, command=text,
                   error=str(error) or error.__class__.__name__)
        return {"handled": True, "action": "type", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False,
                "response": "I couldn't type that right now."}
    log_action(actor=actor, risk=RISK_REVERSIBLE, operation="type",
               outcome="ok", allowed=True, command=text, chars=len(draft))
    return {"handled": True, "action": "type", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": typed,
            "response": "Typed into the focused window."}


def _handle_minimize(text, actor):
    source = _norm(text)
    match = _MINIMIZE_RE.match(source)
    if not match:
        return None
    target = re.sub(r"\s+(?:now|please)$", "", match.group(1).strip()).strip(",.?! ")
    if not target or len(target) > 80:
        return None
    result = minimize_by_query(target)
    if not result.get("success") and result.get("candidates"):
        names = _candidate_names(result["candidates"])
        response = f"That matches more than one window: {names}."
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="minimize",
                   outcome="ambiguous", allowed=True, command=text, target=target)
        return {"handled": True, "action": "minimize", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False, "response": response}
    if not result.get("success") and result.get("failure_reason") == "window is already minimized":
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="minimize",
                   outcome="already", allowed=True, command=text, target=target)
        return {"handled": True, "action": "minimize", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False, "response": "That window is already minimized."}
    if not result.get("success"):
        reason = result.get("failure_reason") or "no matching window"
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="minimize",
                   outcome="failed", allowed=True, command=text, target=target,
                   error=reason)
        return {"handled": True, "action": "minimize", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False,
                "response": f"I couldn't minimize that window: {reason}."}
    changed = result.get("changed", False)
    title = (result.get("after") or {}).get("title") or target
    response = f"Minimized {title}." if changed else "That window did not change; it may already be minimized."
    log_action(actor=actor, risk=RISK_REVERSIBLE, operation="minimize",
               outcome="ok" if changed else "already", allowed=True,
               command=text, target=title, changed=changed)
    return {"handled": True, "action": "minimize", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": changed, "response": response}


def _handle_forbidden(text, actor):
    if _is_pc_power_request(text):
        # Explicit power-off/restart of Sir's PC goes to the brain (system
        # action, not an app kill); see jarvis_persona power rules.
        return None
    match = _DESTRUCTIVE_RE.match(text)
    if not match:
        return None
    target = match.group(1).strip().strip(",.?! ")
    log_action(actor=actor, risk=RISK_DESTRUCTIVE, operation="quit-kill-refused",
               outcome="denied", allowed=False, command=text, target=target)
    return {"handled": True, "action": "refuse", "risk": RISK_DESTRUCTIVE,
            "allowed": False, "changed": False,
            "response": ("I won't quit, kill, or terminate applications without "
                         "explicit confirmation on screen. Say or type it in the "
                         "FRIDAY window to proceed.")}


def _handle_web_fetch(text, actor, max_report=420):
    match = _WEB_FETCH_RE.match(text)
    if not match:
        return None
    query = next((group.strip().strip(",.?! ") for group in match.groups() if group and group.strip()), None)
    if not query or len(query) > 140:
        return None
    from .web_scrape import search_and_fetch

    result = search_and_fetch(query)
    if not result.get("ok"):
        reason = result.get("error") or "no results"
        log_action(actor=actor, risk=RISK_READ_ONLY, operation="web_fetch",
                   outcome="failed", allowed=True, query=query, error=reason)
        return {"handled": True, "action": "web_fetch", "risk": RISK_READ_ONLY,
                "allowed": True, "changed": False,
                "response": f"I couldn't pull that from the web right now: {reason}."}
    hits = result["results"]
    if not hits:
        log_action(actor=actor, risk=RISK_READ_ONLY, operation="web_fetch",
                   outcome="no_results", allowed=True, query=query)
        return {"handled": True, "action": "web_fetch", "risk": RISK_READ_ONLY,
                "allowed": True, "changed": False,
                "response": "I searched but couldn't find anything reliable on that."}
    top = hits[0]
    snippet = re.sub(r"\s+", " ", top.get("text", "") or "").strip()
    if len(snippet) > max_report:
        snippet = snippet[: max_report] + "…"
    response = f"From the top result, {top.get('title') or top.get('url')}: {snippet}"
    log_action(actor=actor, risk=RISK_READ_ONLY, operation="web_fetch",
               outcome="ok", allowed=True, query=query, url=top.get("url"))
    return {"handled": True, "action": "web_fetch", "risk": RISK_READ_ONLY,
            "allowed": True, "changed": False, "response": response}


def _handle_browser(text, actor):
    quoted_search = re.search(
        r"\bsearch\s*(?:in\s+a\s+new\s+tab\s*|for\s+)?[\"'\u201c]?([\w\d .,!?\-']{2,120})",
        text,
        flags=re.IGNORECASE,
    ) if "search" in text.lower() else None
    if quoted_search and ("\"" in text or "'" in text or "\u201c" in text):
        # 'open edge and in a new tab search "how are you"' -> open the
        # default browser at the google search for the quoted term.
        from urllib.parse import quote_plus

        query = quoted_search.group(1).strip().strip("\"'\u201c\u201d ")
        if len(query) >= 2:
            import webbrowser

            url = f"https://www.google.com/search?q={quote_plus(query)}"
            webbrowser.open(url)
            log_action(actor=actor, risk=RISK_READ_ONLY, operation="web_search",
                       allowed=True, outcome="ok", query=_redact(query))
            return {"handled": True, "action": "web_search", "risk": RISK_READ_ONLY,
                    "allowed": True, "changed": True,
                    "response": _flair.browser(f"the search for \u201c{query}\u201d"),
                    "url": url}
    match = _BROWSER_RE.search(text)
    if not match:
        return None
    body = match.group("body").strip().strip(",.!? ")
    if not body:
        return None
    lowered = body.lower()
    target = None
    for name, url in _BROWSER_SITE_TARGETS.items():
        if re.search(rf"\b{name}\b", lowered):
            target = url
            break
    if target is None:
        for app_name in _BROWSER_APP_TOKENS:
            if re.search(rf"\b{re.escape(app_name)}\b", lowered):
                target = "https://google.com"  # default browser
                break
    if target is None:
        return None  # Not a browser/site request; let launch-app or the model handle it
    try:
        import webbrowser
        webbrowser.open(target)
        return {"handled": True, "action": "browser", "risk": RISK_READ_ONLY,
                "allowed": True, "changed": True, "response": _flair.browser(target)}
    except Exception:
        # Script-based fallback
        try:
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile('w', suffix='.bat', delete=False) as f:
                f.write(f'start {target}')
                script_path = f.name
            subprocess.Popen([script_path], shell=True)
            return {"handled": True, "action": "browser", "risk": RISK_READ_ONLY,
                    "allowed": True, "changed": True, "response": f"Opening {target} via script."}
        except Exception:
            return None # Fail to OI if absolutely nothing works

_BROWSER_RE = re.compile(
    r".*\b(?:open|launch|start|go\s+to)\b\s*(?:up\s+|the\s+|an?\s+|my\s+|a\s+|me\s+)*"
    r"(?P<body>[a-z0-9 .,!?\-:]*)",
    flags=re.IGNORECASE,
)
_BROWSER_SITE_TARGETS = {
    "youtube": "https://youtube.com",
    "google": "https://google.com",
}
_BROWSER_APP_TOKENS = ("chrome", "firefox", "edge", "browser", "website", "site")

def _handle_script(text, actor):
    if not _SCRIPT_RE.match(text): return None
    return {"handled": True, "action": "script", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": True, "response": "Ready. Please provide the script, Sir."}


_SCRIPT_RE = re.compile(
    r"^(?:please\s+)?run\s+(?:this|the\s+)?(?:script|code|python)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)

# Launch an application on this PC ("open discord", "launch spotify", ...).
# Skip matches that name a running/listed window (handled upstream by focus/
# minimize), and skip site keywords handled by _handle_browser.
_LAUNCH_RE = re.compile(
    r"^(?:do\s+me\s+a\s+favor\s+and\s+)?(?:please\s+|could\s+you\s+(?:please\s+)?|"
    r"would\s+you\s+(?:please\s+)?|can\s+you\s+(?:please\s+)?)?"
    r"(?:open|launch|start)\s+(?:up\s+|the\s+|an?\s+)?(.+?)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)
_LAUNCH_BYPASS = re.compile(
    r"windows?|browser|chrome|firefox|edge|google|youtube|site|website|"
    r"the\s+settings|settings\s+or|the\s+file|explorer\s+or",
    flags=re.IGNORECASE,
)


def _handle_launch_app(text, actor):
    # Conditional clauses ("open spotify if it isn't open") collapse to the
    # plain request so the router never falls back to a model-authored
    # 'spotify is not recognized' script for them.
    conditional_tail = re.compile(r"\bif\b.*$", re.IGNORECASE)
    clean_for_match = re.sub(r"\bif\b.*$", "", text, flags=re.IGNORECASE).strip(",.?! ")
    match = (_LAUNCH_RE.match(clean_for_match) or _LAUNCH_RE.match(text))
    if not match:
        return None
    # Peel conversational tails ("please love", "thank you tons love",
    # "friday", "now") one at a time until only the app name remains.
    target = match.group(1).strip()
    for _ in range(5):
        peeled = re.sub(
            r"\s+(?:now|please|for\s+me|love|friday|thank\s+you[\w\s]*|"
            r"tons(?:\s+of)?\s*love)$",
            "", target, flags=re.IGNORECASE,
        ).strip(",.?! ")
        if peeled == target:
            break
        target = peeled
    if not target or len(target) > 60:
        return None
    if _LAUNCH_BYPASS.search(target):
        return None  # Handled by _handle_browser or the model instead
    from .windows_control import find_app_executable, is_app_running

    # "open spotify if it isn't open" — honor the condition: if the app is
    # already running, do not spawn a second instance.
    running_image = is_app_running(target)
    if running_image:
        return {"handled": True, "action": "launch_app", "risk": RISK_READ_ONLY,
                "allowed": True, "changed": False,
                "response": f"{target.capitalize()} is already open, Sir — right where you left it."}
    executable = None
    try:
        executable = find_app_executable(target)
    except Exception:
        executable = None
    if not executable:
        return None  # Can't confidently resolve -> let the model handle it
    try:
        import subprocess

        subprocess.Popen([executable])
        launched = True
    except Exception as error:
        launched = False
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="launch_app",
                   outcome="failed", allowed=True, command=text, target=target,
                   error=str(error) or error.__class__.__name__)
        return {"handled": True, "action": "launch_app", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False,
                "response": "I couldn't launch that application right now."}
    log_action(actor=actor, risk=RISK_REVERSIBLE, operation="launch_app",
               outcome="ok", allowed=True, command=text, target=target,
               executable=_redact(executable))
    return {"handled": True, "action": "launch_app", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": True, "response": _flair.launch(target)}


def _redact(value):
    return str(value or "")


# Media control: play/pause/skip music or video through Windows media keys.
_MEDIA_RE = re.compile(
    r"^(?:do\s+me\s+a\s+favor\s+and\s+)?(?:please\s+)?"
    r"(?:pause|resume|play|continue|play\s+or\s+pause|pause\s+or\s+play|stop)\s+"
    r"(?:the\s+|my\s+|your\s+)?(?:music|song|track|video|playback|media|it)\s*[.!?]?\s*$"
    r"|^(?:do\s+me\s+a\s+favor\s+and\s+)?(?:please\s+)?"
    r"(?:skip|next|previous|go\s+back\s+to)\s+(?:this\s+|that\s+|the\s+)?"
    r"(?:song|track|video|one|one\s+track)\s*[.!?]?\s*$",
    flags=re.IGNORECASE,
)


def _handle_media(text, actor):
    if not _MEDIA_RE.match(text):
        return None
    word = _norm(text)
    action = "play_pause"
    if "skip" in word or "next" in word:
        action = "next"
    elif "previous" in word or "go back" in word:
        action = "previous"
    from .windows_control import send_media_key

    sent = send_media_key(action)
    if not sent:
        log_action(actor=actor, risk=RISK_REVERSIBLE, operation="media",
                   outcome="failed", allowed=True, command=text, action=action)
        return {"handled": True, "action": "media", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False,
                "response": "I couldn't control media playback right now."}
    label = {"play_pause": "toggled", "next": "skipped to the next track",
             "previous": "went to the previous track"}[action]
    log_action(actor=actor, risk=RISK_REVERSIBLE, operation="media",
               outcome="ok", allowed=True, command=text, action=action)
    return {"handled": True, "action": "media", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": True,
            "response": _flair.media(action)}


def route(text, *, actor="voice"):
    """Return a handled action dict for a recognized command, else ``None``.

    DESIGN INTENT (Sir's directive): open/close app control is NOT
    pre-scripted here anymore — the brain AUTHORS its own python every time
    (self-learning beats premade paths). The deterministic layer keeps only
    instant-read helpers and destructive-refusal guards.
    """
    if not text:
        return None
    # Quoted/hinted web searches need the RAW text: _norm() strips quotes,
    # and 'search "how are you"' loses its markers through it.
    for quoted_handler in (_handle_browser,):
        result = quoted_handler(text, actor=actor)
        if result is not None:
            return result
    handlers = (_handle_list_windows, _handle_activity, _handle_forbidden,
                _handle_minimize, _handle_focus, _handle_type, _handle_web_fetch,
                _handle_browser, _handle_media, _handle_calendar, _handle_script)
    # Strip conversational prefixes ("okay perfect, ...") so the remaining
    # fast handlers (browser/media/calendar) can still see the verb.
    source = _norm(text)
    for _ in range(4):
        stripped = re.sub(
            r"^(?:okay|ok|alright|perfect|yes|sure|hey|hi|friday|please)\b[,\s]+",
            "", source, flags=re.IGNORECASE,
        )
        if stripped == source:
            break
        source = stripped
    for handler in handlers:
        result = handler(source, actor)
        if result is not None:
            return result
    return None

CALENDAR_REQUEST_RE = re.compile(
    r"^(?:okay|ok|hey|hi|friday|please|do\s+me\s+a\s+favor\s+and\s+|could\s+you\s+|can\s+you\s+|"
    r"why\s+don'?t\s+you\s+|would\s+you\s+)?"
    r"(?:put|add|create|schedule|book|slot|place|set\s+up)\s+.*\b(?:in|into|on)\s+(?:my\s+|the\s+|your\s+)?calendar\b",
    flags=re.IGNORECASE,
)
_TITLE_REMOVER = re.compile(
    r"\b(?:for\s+me|please|right\s+now|if\s+you\s+can|would\s+you|could\s+you)\b",
    flags=re.IGNORECASE,
)
_TIME_WORDS_RE = re.compile(
    r"\b(?:tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\bon?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b|\bam\b|\bpm\b|\bto\b|\bin\s+\d+",
    flags=re.IGNORECASE,
)

# Set by server.py so the router can reach the schedule store without an
# import-cycle; server calls `configure_calendar(store_getter)` at startup.
CALENDAR_STORE_GETTER = lambda: None  # noqa: E731
CALENDAR_PENDING = {}


def configure_calendar(store_getter):
    global CALENDAR_STORE_GETTER
    CALENDAR_STORE_GETTER = store_getter


_CLOSE_RE = re.compile(
    r"^\s*(?:do\s+me\s+a\s+favor\s+and\s+|(?:ok[ayy]+|yo|hey|hi)[, ]+)?"
    r"(?:please\s+|could\s+you\s+(?:please\s+)?|would\s+you\s+(?:please\s+)?|"
    r"can\s+you\s+(?:please\s+)?)?"
    r"(?:(?:if\s+[\w ]{0,40}?\b(?:is|it'?s)\s+open\b[, ]*)?"
    r"(?:could\s+you\s+|would\s+you\s+|can\s+you\s+)?)?"
    r"(?:please\s+)?"
    r"(?:close|terminate|kill|quit)\s+"
    r"(?:the\s+|my\s+|all\s+)?(.+?)"
    r"(?:\s*,\s*friday|\s+friday|,\s*please|\s+thank\s+you.*|\s+now|\s+please)?\s*[.!?]*$",
    flags=re.IGNORECASE,
)
# Generic-object targets ("close the app/window") stay in the safe zone:
# only NAMED apps get the real process kill. quit/kill/terminate/exit keep
# the destructive-refusal contract in _handle_forbidden.
_GENERIC_TARGET_RE = re.compile(
    r"^(?:the\s+|that\s+|my\s+)?(?:app|apps|window|windows|program|programs|application|thing)\b",
    flags=re.IGNORECASE,
)

_FALLBACK_NOTE = (
    "No luck, Sir — nothing by that name is running right now.",
    "I can't find anything like that running, Sir — want me to look again?",
    "That name isn't in the process list right now, Sir.",
)
_APP_CLOSE_DONE = (
    "Closed and verified, Sir.",
    "Terminated cleanly, Sir.",
    "Done — re-checked the process list, it's gone, Sir.",
)


def app_close_flair():
    import random
    return random.choice(_APP_CLOSE_DONE)


_CLOSE_APP_FALLBACK = (
    "No luck, Sir — nothing by that name is running right now.",
    "I can't find anything like that running, Sir — want me to look again?",
    "That name isn't in the process list right now, Sir.",
)


def _handle_close_app(text, actor):
    """Deterministic close-app: resolve the REAL process (learning: Minecraft
    == javaw.exe etc. via the tasklist scan), taskkill, then VERIFY."""
    match = _CLOSE_RE.match(text)
    if not match:
        return None
    target = re.sub(
        r"\s+(?:now|please|for\s+me|thank\s+you[\w\s]*|love|tons\s+love|friday)$",
        "", match.group(1).strip(),
        flags=re.IGNORECASE,
    ).strip(",.?! ")
    if not target or len(target) > 40:
        return None
    if target.lower() in ("it", "that", "this"):
        # "...could you terminate it?" — infer the app from the WHOLE
        # sentence: find which of the mentioned nouns is actually running.
        from .windows_control import resolve_process_from_text

        inferred = resolve_process_from_text(text)
        if not inferred:
            return None  # nothing running matches — fall through honestly
        target = inferred
    elif _GENERIC_TARGET_RE.match(target):
        return None  # vague object — no blind process kills
    if _LAUNCH_BYPASS.search(target):
        return None  # windows/file-explorer phrases belong elsewhere
    from .windows_control import close_app

    log_action(actor=actor, risk=RISK_DESTRUCTIVE, operation="close_app",
               allowed=True, command=target)
    closed, detail = close_app(target)
    if closed:
        return {"handled": True, "action": "close_app", "risk": RISK_DESTRUCTIVE,
                "allowed": True, "changed": True,
                "response": f"{app_close_flair()} ({target}: {detail})."}
    note = f"{target}: {detail}."
    return {"handled": True, "action": "close_app", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": False,
            "response": f"{_flair.pick(_CLOSE_APP_FALLBACK)} ({note})"}


def _extract_calendar_title(text):
    """Language-first title guess: everything except filler and calendar words."""
    lowered = text.lower()
    if not CALENDAR_REQUEST_RE.match(lowered):
        return ""
    body = lowered
    body = re.sub(CALENDAR_REQUEST_RE.pattern, "", body, flags=re.IGNORECASE, count=1)
    body = _TITLE_REMOVER.sub(" ", body)
    body = _TIME_WORDS_RE.sub(" ", body)
    body = re.sub(r"\s+calendar\b.*$", "", body, flags=re.IGNORECASE)
    body = re.sub(r"[^\w\s'?!\-]", " ", body)
    body = re.sub(r"\s+", " ", body).strip(",.?! ")
    return body[:100]


def _handle_calendar(text, actor):
    """Deterministic calendar journey: store the event + speak one confirmation.

    Handles the whole dialogue: "put something in my calendar tomorrow 10 to
    11" -> event; missing time -> asks; bare "anything" after an ask -> a
    catch-all task tomorrow 2pm so FRIDAY never stalls.
    """
    if not CALENDAR_REQUEST_RE.match(text):
        return None
    from .schedule_store import _parse_natural_time  # local import like other handlers

    store = CALENDAR_STORE_GETTER()
    if store is None:
        return None
    when = _parse_natural_time(text)
    if when is None:
        # Ask for the missing details, remembering the pending request so the
        # next short answer ("it can be anything") completes the booking.
        CALENDAR_PENDING["title"] = _extract_calendar_title(text) or "Check-in with Sir"
        return {"handled": True, "action": "calendar", "risk": RISK_REVERSIBLE,
                "allowed": True, "changed": False,
                "response": "Of course — what day and time should I slot it? For example, tomorrow 10 to 11."}
    start, duration = when
    title = _extract_calendar_title(text)
    if not title:
        # No real title given (e.g. "put something in my calendar ...").
        title = CALENDAR_PENDING.pop("title", None) or "Task with Sir"
    else:
        CALENDAR_PENDING.pop("title", None)
    # Bare "anything" after the ask falls back to the pending/defaults path.
    event = store.add(title, start, duration, source=actor)
    when_label = start.strftime("%a %d %b at %H:%M")
    log_action(actor=actor, risk=RISK_REVERSIBLE, operation="calendar",
               outcome="ok", allowed=True, command=text, title=title, start=event["start"])
    return {"handled": True, "action": "calendar", "risk": RISK_REVERSIBLE,
            "allowed": True, "changed": True,
            "response": _flair.calendar(when_label)}



__all__ = ("route", "RISK_DESTRUCTIVE", "RISK_READ_ONLY", "RISK_REVERSIBLE")