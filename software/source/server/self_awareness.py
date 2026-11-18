"""Self-awareness: what FRIDAY knows about HERSELF, right now, truthfully.

The audit behind this module found the gap: FRIDAY's prompt described her
SKILLS but almost nothing about her own RUNTIME. She had no way to know which
model was answering her, whether Telegram was up, whether n8n was connected, or
that her own skills directory was empty. Everything about her state lived in the
HUD, which she cannot see.

This module gives her that, in one honest call, and - just as importantly - tells
her the things she must NOT overstate about herself.

    from source.server.self_awareness import who_am_i
    print(who_am_i())          # readable block she can print and read aloud
    self_state()               # the same facts as a dict
    model_report()             # WHICH models are serving, live, per subsystem
    runtime_models()           # the same as a dict (brain/stt/tts/vision)
    last_script()              # the last script she wrote and ran, and its result
    capabilities()             # what she can do, grouped, with import forms
    limitations()              # what she genuinely cannot do
    skill_status()             # her own skills, and the restart-latch truth

Design rules (same as the other helper modules): never raise, always return
plain data, bound every external call, and never let a secret leave. Every fact
here is read live at call time - nothing is cached into the prompt as a
guess, because a stale "n8n is not connected" is exactly the kind of confident
error that wastes a user's time.

Project policy: this is a CAPABILITY THE BRAIN CHOOSES. Nothing here is a
phrase-matched trigger; the deterministic router is deliberately not involved.
"""

from __future__ import annotations

import os
import platform
import sys
import time

DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".friday", "api_credentials.json")
SKILL_TIMEOUT_NOTE = "skills load once per process; a skill saved mid-session needs a restart"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _port_bound(port: int) -> bool:
    """Is something listening on this port? Read-only, no packets sent."""
    import socket

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _ollama_models() -> dict:
    """Which local models are actually pulled right now.

    ROUND ZB-LAT (2026-11-03): the URL defaults to 127.0.0.1, never
    `localhost`. On this machine `localhost` resolves to BOTH 127.0.0.1 and
    ::1, and the IPv6 loopback blackholes SYNs instead of refusing them - so
    `requests` burns its whole `timeout=2.0` on ::1 before falling back to
    IPv4. Measured: `http://localhost:11434/api/tags` **2.015s** vs
    `http://127.0.0.1:11434/api/tags` **0.015s**. This probe runs inside
    `self_state()` -> `self_knowledge()` -> one `{{}}` prompt block, i.e. it
    cost every single turn ~2 seconds of the ~7s prompt render.
    """
    import requests

    url = os.environ.get("OLLAMA_URL", os.environ.get("FRIDAY_OLLAMA_URL",
                                                      "http://127.0.0.1:11434")).rstrip("/")
    try:
        response = requests.get(f"{url}/api/tags", timeout=2.0)
        if response.status_code != 200:
            return {"ok": False, "url": url, "error": f"HTTP {response.status_code}"}
        names = [m.get("name", "?") for m in response.json().get("models", [])]
        return {"ok": True, "url": url, "count": len(names), "models": names[:20]}
    except Exception as error:
        return {"ok": False, "url": url, "error": f"{type(error).__name__}"}


def _n8n_state() -> dict:
    """Live n8n status - never an assumption."""
    try:
        from . import n8n_runtime

        adapter = n8n_runtime.get_adapter()
        available = adapter.available()
        status = adapter.status()
        return {
            "configured": bool(status.get("workflow_count")),
            "connected": bool(available.get("connected")),
            "workflows": status.get("workflows", []),
            "reason": available.get("reason") or status.get("reason", ""),
        }
    except Exception as error:
        return {"configured": False, "connected": False, "workflows": [],
                "reason": f"n8n probe failed: {type(error).__name__}"}


def _telegram_state() -> dict:
    """Is the phone channel configured? Never reads or returns the token."""
    try:
        from . import remote_telegram

        config = remote_telegram.load_config()
        token = (getattr(config, "token", "") or "")
        return {
            "configured": bool(token),
            "allowed_chats": len(getattr(config, "allowed_chat_ids", []) or []),
            "setup_mode": bool(getattr(config, "setup_mode", False)),
        }
    except Exception:
        return {"configured": False, "allowed_chats": 0, "setup_mode": False,
                "note": "telegram config unreadable"}


def safe_for_kernel(text, limit=None) -> str:
    """Make text safe to print from a Jupyter kernel prompt block.

    The prompt's {{ }} blocks are exec'd in the kernel and their stdout is
    captured with the console code page (cp1252 here). Any character outside
    that set raises UnicodeEncodeError INSIDE print(), which my own
    except-clause then swallowed into a fallback message - so the journal was
    silently rendering as "(journal unavailable)" for every turn, and I only
    caught it because a recorded answer contained an em-dash.

    Typographic characters are mapped to their ASCII equivalent so the text
    stays readable, then anything still unencodable becomes '?' rather than
    taking the whole block down.
    """
    text = str(text or "")
    for fancy, plain in (
        ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'),
        ("\u2013", "-"), ("\u2014", "-"), ("\u2212", "-"), ("\u2026", "..."),
        ("\u00a0", " "), ("\u2022", "*"), ("\u2192", "->"), ("\u2713", "ok"),
    ):
        text = text.replace(fancy, plain)
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        text = text.encode("ascii", "replace").decode("ascii")
    if limit:
        text = text[:limit]
    return text


def skill_catalog(root=None) -> list:
    """Every saved skill with its APPLICABILITY, steps and usage count.

    Added 2026-10-01. The prompt used to receive only bare filenames
    (`['name()']` via computer.skills.list()), so a skill was a name with no
    indication of what it was FOR - she could not tell when a saved procedure
    applied to the task in front of her, which is why the skill library stayed
    empty in practice. Metadata is stored as `# when:` / `# verify:` / `# uses:`
    comment lines INSIDE the skill file by friday_save_skill, so there is exactly
    one source of truth and nothing to drift.

    Parsed as bounded syntax and literal data: this must be safe to call from the
    prompt on every turn.
    """
    from pathlib import Path
    from .skill_metadata import skill_directory

    project = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    try:
        rows = skill_directory(project / "skills")["rows"]
        return [{key: row[key] for key in ("name", "when_to_use", "verify", "steps", "uses")}
                for row in rows]
    except (OSError, ValueError):
        return []


def skills_for_prompt(max_chars=1400, root=None) -> str:
    """Render the skill library for the system prompt, applicability ONLY.

    Progressive disclosure (OpenCode Skills docs: metadata is loaded up front,
    the full procedure only when it is actually needed). Before this, every
    skill's steps and verify line were pasted into the prompt EVERY turn, so the
    prompt grew with every skill she ever saved and she paid for procedures she
    was never going to use. Now each skill costs two short lines - what it is
    called and when it applies - and `friday_load_skill(name)` pulls the real
    procedure on demand, with no restart needed.

    Empty library returns an explicit instruction rather than `[]`, because a
    bare empty list reads as "nothing here" and gives her no reason to ever save
    one in the first place.
    """
    catalog = skill_catalog(root=root)
    if not catalog:
        return ("(no skills saved yet - if you work out a genuinely reusable way "
                "to do a kind of task, save it with friday_save_skill)")
    lines = ["(metadata only - to use one, friday_load_skill(name) returns its steps)"]
    for entry in catalog:
        applies = entry["when_to_use"] or "(applicability not recorded)"
        lines.append("- %s [%d use(s)]: %s" % (entry["name"], entry["uses"], applies[:140]))
    return "\n".join(lines)[:max_chars]


def skill_procedure(name, max_chars=2000, root=None) -> str:
    """Return one skill's full procedure on demand (progressive disclosure).

    Read straight off disk by name; never exec's anything, so it is safe to
    call from the prompt path.
    """
    entry = None
    for candidate in skill_catalog(root=root):
        if candidate["name"] == name:
            entry = candidate
            break
    if entry is None:
        return "no saved skill named %r (see the list above)" % (name,)
    lines = ["skill: %s  (used %d time(s))" % (entry["name"], entry["uses"])]
    if entry["when_to_use"]:
        lines.append("use when: %s" % entry["when_to_use"])
    for index, step in enumerate(entry["steps"], 1):
        lines.append("%d. %s" % (index, step[:300]))
    if entry["verify"]:
        lines.append("verify: %s" % entry["verify"][:300])
    return "\n".join(lines)[:max_chars]


def _skills_state() -> dict:
    """Her own skill directory, and whether the one-shot loader already ran.

    The loader in Open Interpreter latches on the FIRST python run of the
    process, so a skill written later is on disk but NOT callable until FRIDAY
    restarts. Reporting that honestly is the whole point of this function.
    (Note: friday_load_skill() bypasses the latch, so a saved skill IS callable
    immediately - the restart caveat only applies to OI's own importer.)
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "skills")
    catalog = skill_catalog()
    return {
        "path": path,
        "exists": os.path.isdir(path),
        "count": len(catalog),
        "skills": [entry["name"] for entry in catalog],
        "catalog": catalog,
        "loaded_this_process": None,  # unknown from outside the kernel
        "restart_note": SKILL_TIMEOUT_NOTE,
    }


def _api_pools() -> dict:
    """Which cloud pools have keys - counts only, never the keys themselves."""
    try:
        from . import api_pools

        described = api_pools.describe_pools()
        return {name: {"total": v.get("total", 0), "available": v.get("available", 0)}
                for name, v in described.items()}
    except Exception:
        return {}


def runtime_models() -> dict:
    """WHICH models are serving FRIDAY right now, per subsystem, read live.

    Sir asked for model self-awareness: "I hope its self aware with what models
    its using". Before this, the only model knowledge FRIDAY had was static
    prompt text, so a question like "what are you using to listen to my voice"
    produced a vague, generic answer instead of the real engine and model name.

    Every value here is read at call time. Volatile facts are reported honestly
    as unknown rather than guessed. Never raises; never returns keys.
    """
    from . import brain_router

    try:
        serving = brain_router.live_serving_state()
    except Exception:
        serving = {"ready": False}

    brain_now = serving.get("current_model")
    brain_last = serving.get("last_model")
    if brain_now:
        brain_desc = f"{brain_now} (pool {serving.get('current_pool') or 'local'})"
    elif brain_last:
        brain_desc = f"{brain_last} (last turn; pool {serving.get('last_pool') or 'local'})"
    else:
        brain_desc = f"{serving.get('local_default')} (local default; no turn served yet)"

    # Voice-in: the local RealtimeSTT recorder is the real ear; cloud STT is the
    # preferred fast path tried FIRST, with the local recorder as the fallback.
    local_stt_model = os.environ.get("FRIDAY_LOCAL_STT_MODEL", "base.en")
    try:
        from . import cloud_stt

        stt_chain = list(getattr(cloud_stt, "STT_CHAIN", ()) or ())
        stt_last = getattr(cloud_stt, "LAST_STAGE", None)
    except Exception:
        stt_chain = ["stt_groq", "stt_deepgram", "gemini_stt"]
        stt_last = None

    # Voice-out: cloud TTS first, local edge-tts server last.
    try:
        from . import gemini_tts

        tts_chain = list(getattr(gemini_tts, "GEMINI_TTS_MODEL_CHAIN", ()) or ())
        tts_last = getattr(gemini_tts, "LAST_STAGE", None)
    except Exception:
        tts_chain, tts_last = [], None

    try:
        from .model_registry import select_model, GENERAL, VISION

        def _model_name(value, fallback):
            # select_model() returns a ModelInfo dataclass, not a string.
            # Reporting the repr would be exactly the kind of wrong-but-
            # confident answer this function exists to prevent.
            name = getattr(value, "name", value)
            return str(name) if name else fallback

        vision_model = _model_name(
            select_model(VISION), os.environ.get("FRIDAY_VISION_MODEL", "qwen2.5vl:3b")
        )
        general_model = _model_name(
            select_model(GENERAL), os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "qwen3:8b")
        )
    except Exception:
        vision_model = os.environ.get("FRIDAY_VISION_MODEL", "qwen2.5vl:3b")
        general_model = os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "qwen3:8b")

    # The real fallback order, read from screen_understanding rather than
    # restated here, so this report cannot drift from the code that runs.
    local_vision_chain, vision_cloud_chain = [], []
    try:
        from . import screen_understanding as _su

        local_vision_chain = [str(m) for m in getattr(_su, "LOCAL_VISION_CHAIN", ()) or ()]
        vision_cloud_chain = [str(m) for m in getattr(_su, "VISION_CHAIN", ()) or ()]
    except Exception:
        pass

    return {
        "collected_at": _now(),
        "brain": {
            "serving": brain_desc,
            "model": brain_now or brain_last or serving.get("local_default"),
            "pool": serving.get("current_pool") or serving.get("last_pool") or "local",
            "local_default": serving.get("local_default"),
            "registry_general": general_model,
            # NOTE (2026-09-30): the "tier" key was removed from this report
            # along with the tier system. It always resolved to None, and
            # leaving it in let FRIDAY describe a routing level that does not
            # exist. If you see her claiming a "tier", this is why.
            "chain": "Modal -> Groq -> OpenRouter -> Gemini -> Local",
            "note": "one chain, no tiers; cloud is tried first and the local model is the guaranteed fallback",
        },
        "stt": {
            "local_engine": "RealtimeSTT / faster-whisper",
            "local_model": local_stt_model,
            "compute_type": os.environ.get("FRIDAY_STT_COMPUTE", "int8"),
            "cloud_chain_tried_first": stt_chain,
            # 2026-10-01: was missing entirely, so she could recite the chain
            # but never say which engine actually won. Now published by
            # cloud_stt.transcribe_wav, so she can answer the real question.
            "last_stage": stt_last,
            "last_stage_meaning": (
                "the engine that transcribed the last utterance; null means the "
                "cloud chain failed and the local faster-whisper fallback ran, "
                "or nothing has been transcribed since startup"
            ),
            "note": "cloud STT is tried first for speed; the local faster-whisper recorder is the fallback",
        },
        "tts": {
            "cloud_chain_tried_first": tts_chain,
            "last_stage": tts_last,
            "local_fallback": "edge-tts OpenAI-compatible server on localhost:5050",
            "note": "cloud TTS first; the local edge-tts server is the fallback",
        },
        "vision": {
            # 2026-10-01: this used to report the LOCAL model as the vision
            # engine and say "local vision model is tried first". Both were
            # false. Modal (self-hosted GLM-5.3 Flash NVFP4) has been the
            # PRIMARY vision engine since 2026-09-30 - it is self-hosted, so it
            # has no free-tier quota to exhaust, which local qwen2.5vl does have.
            # The local model is the guaranteed offline fallback.
            "primary": "Modal vision_modal (self-hosted GLM-5.3 Flash NVFP4)",
            "local_fallback_model": vision_model,
            "local_fallback_chain": local_vision_chain,
            "cloud_chain_after_modal": vision_cloud_chain,
            "note": ("Modal is tried FIRST for screen understanding; the local "
                     "vision model is the guaranteed offline fallback"),
        },
    }


def model_report() -> str:
    """Readable, speakable answer to 'what model are you using right now?'."""
    info = runtime_models()
    lines = ["My live model stack:"]
    brain = info["brain"]
    lines.append(f"- Thinking/replying: {brain['serving']}")
    stt = info["stt"]
    lines.append(
        f"- Listening to you: cloud tried first "
        f"({', '.join(stt['cloud_chain_tried_first']) or 'none configured'}); "
        f"local {stt['local_engine']} '{stt['local_model']}' ({stt['compute_type']}) is the fallback"
        + (f"; last voice actually heard by: {stt['last_stage']}" if stt.get("last_stage") else "")
    )
    tts = info["tts"]
    lines.append(
        f"- Speaking: cloud first ({', '.join(tts['cloud_chain_tried_first']) or 'none configured'}), "
        f"falling back to {tts['local_fallback']}"
        + (f"; last voice actually used: {tts['last_stage']}" if tts.get("last_stage") else "")
    )
    # Modal is PRIMARY vision (self-hosted, no free-tier quota); local is the
    # offline fallback. Saying "local first" here was simply wrong.
    lines.append(
        f"- Vision/screens: {info['vision']['primary']} first; local "
        f"{info['vision']['local_fallback_model']} is the offline fallback"
    )
    return "\n".join(lines)


def last_script(limit=2000):
    """The last script FRIDAY wrote and ran, plus its console output.

    Sir asked to be able to SEE which script is being used. The conversation
    stream shows it live, and this returns the same thing programmatically so
    she can also report it on request.
    """
    state = {}
    try:
        from . import server as _server

        state = getattr(_server, "LAST_TOOL_SCRIPT", None) or {}
    except Exception:
        state = {}
    if not state:
        return {"ran": False, "note": "no script recorded yet in this process"}
    code = str(state.get("code") or "")
    return {
        "ran": True,
        "tool_number": state.get("tool_number"),
        "language": state.get("language"),
        "success": state.get("success"),
        "at": state.get("at"),
        "code": code[:limit],
        "output": str(state.get("output") or "")[:1000],
        "note": "the same script is shown live in the conversation stream",
    }


def _brain_keepalive() -> dict:
    """Is the local brain being kept resident, and is the ping working?

    Worth exposing: a cold local model costs ~49s on this machine versus ~2.2s
    warm, so "am I slow?" and "is the brain being kept warm?" are the same
    question in practice.
    """
    try:
        from . import brain_keepalive

        return brain_keepalive.status()
    except Exception as error:
        return {"enabled": False, "running": False, "error": type(error).__name__}


def self_state() -> dict:
    """Everything about her current runtime, read live. No secrets."""
    server_port = int(os.environ.get("FRIDAY_SERVER_PORT", "10101") or 10101)
    return {
        "collected_at": _now(),
        "process": {
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            "uptime_note": "unknown from inside the kernel",
        },
        "machine": {
            "computer_name": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
        },
        "local_brain": _ollama_models(),
        "models": runtime_models(),
        "brain_keepalive": _brain_keepalive(),
        "cloud_pools": _api_pools(),
        "telegram": _telegram_state(),
        "n8n": _n8n_state(),
        "server": {
            "port": server_port,
            "listening": _port_bound(server_port),
        },
        "skills": _skills_state(),
    }


def who_am_i() -> str:
    """Readable self-description for FRIDAY to print and read aloud."""
    state = self_state()
    lines = [f"FRIDAY self-report ({state['collected_at']})"]

    machine = state["machine"]
    lines.append(f"Running on {machine['computer_name']} ({machine['os']}), python {state['process']['python']}")

    brain = state["local_brain"]
    if brain.get("ok"):
        primary = os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "qwen3:8b")
        present = primary in brain["models"]
        lines.append(f"Local brain (Ollama) is {'UP' if brain.get('ok') else 'DOWN'} with "
                     f"{brain['count']} model(s); my default '{primary}' is "
                     f"{'pulled' if present else 'NOT pulled'}")

    # Name the model that is REALLY serving, not just what is installed.
    try:
        models = state.get("models") or {}
        brain_info = models.get("brain") or {}
        if brain_info.get("serving"):
            lines.append(f"Model actually serving my replies: {brain_info['serving']}")
        stt_info = models.get("stt") or {}
        if stt_info.get("local_model"):
            lines.append(
                f"Listening: {stt_info.get('local_engine')} '{stt_info['local_model']}' "
                f"(local fallback), cloud tried first"
            )
        tts_info = models.get("tts") or {}
        if tts_info.get("last_stage"):
            lines.append(f"Last voice actually used: {tts_info['last_stage']}")
    except Exception:
        pass
    if not brain.get("ok"):
        lines.append(f"Local brain (Ollama at {brain.get('url')}) is NOT answering: {brain.get('error')}")

    pools = state["cloud_pools"]
    if pools:
        ready = [f"{name} {v['available']}/{v['total']}" for name, v in pools.items() if v["total"]]
        lines.append(f"Cloud keys configured: {', '.join(ready) if ready else 'none (I run fully local)'}")

    telegram = state["telegram"]
    lines.append(f"Telegram phone access: {'configured' if telegram['configured'] else 'not configured'}"
                 + (f" ({telegram['allowed_chats']} allowed chat(s))" if telegram["configured"] else ""))

    n8n = state["n8n"]
    if not n8n["configured"]:
        lines.append("n8n workflow automation: not connected - I use my own built-in resources instead")
    elif n8n["connected"]:
        lines.append(f"n8n: CONNECTED with workflow(s): {', '.join(n8n['workflows'])}")
    else:
        lines.append(f"n8n: configured but not answering ({n8n['reason']}) - falling back to my own resources")

    server = state["server"]
    lines.append(f"My own server: port {server['port']} "
                 f"{'is listening' if server['listening'] else 'is NOT listening'}")

    skills = state["skills"]
    lines.append(f"Skills I've saved: {skills['count']}"
                 + (f" ({', '.join(skills['skills'])})" if skills["skills"] else "")
                 + (f" - note: {skills['restart_note']}" if skills["count"] else ""))

    return "\n".join(lines)


def health_check(probe_brain: bool = False) -> dict:
    """Is she actually able to think right now? Delegates to the doctor.

    `probe_brain=False` is the fast path (no live model calls, ~1s): it reports
    whether Ollama is up, whether STT/TTS have anything to work with, which
    models are cooling down, and which ports are bound. `probe_brain=True`
    actually calls every fallback tier and is deliberately opt-in because it
    costs a little quota and up to ~45s on a slow local model.
    """
    try:
        from . import doctor

        # Compact by default: the full payload is large enough to flood the
        # model's context mid-conversation and leave it unable to answer.
        return doctor.run_doctor(probe_brain=probe_brain, full=False)
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def capabilities() -> dict:
    """What she can actually do, grouped, with the import forms that WORK.

    Kept in step with the live system prompt: this is the machine-readable
    version of what she is told, so she can check her own inventory rather than
    trusting memory of it.
    """
    return {
        "computer_health": {
            "import": "from source.server.system_diagnostics import ...",
            "functions": ["system_health", "health_report", "cpu_snapshot", "memory_snapshot",
                          "disk_snapshot", "top_processes", "service_status", "list_services",
                          "network_snapshot", "is_online", "host_info", "diagnose"],
        },
        "task_list": {
            "import": "from source.server.task_store import TaskStore; store = TaskStore()",
            "functions": ["add", "list_tasks", "complete", "reopen", "delete", "summary"],
        },
        "n8n_workflows": {
            "import": "from source.server.n8n_runtime import get_adapter; n8n = get_adapter()",
            "functions": ["list_workflows", "call", "available", "connection_help"],
            "note": "optional; absent n8n means I do the task with my own resources",
        },
        "self_awareness": {
            "import": "from source.server.self_awareness import who_am_i, self_state, health_check",
            "functions": ["who_am_i", "self_state", "capabilities", "limitations",
                          "skill_status", "health_check", "runtime_limits"],
        },
        "doctor": {
            "import": "from source.server.doctor import doctor_report, run_doctor",
            "functions": ["doctor_report", "run_doctor", "summary_line"],
            "note": "checks every AI fallback tier, STT/TTS and local services; "
                    "run_doctor(probe_brain=True) makes live model calls",
        },
        "windows_control": {
            "import": "from source.server.windows_control import ...",
            "functions": ["find_app_executable", "list_windows", "focus_by_query", "is_app_running"],
            "note": "author the launch yourself; never click an icon to open an app",
        },
        "self_extension": {
            "import": "computer.skills.new_skill.create()  (or write your own helper module)",
            "functions": ["create", "name", "add_step", "save"],
            "note": SKILL_TIMEOUT_NOTE,
        },
        "web": {
            "import": "silent_search(query) is already in the workspace; web_fetch(\"...\") for live docs",
            "functions": ["silent_search", "web_fetch"],
        },
    }


def runtime_limits() -> dict:
    """Her REAL time and safety limits, read from the environment.

    ROUND W (2026-10-04). This exists because she ANSWERED WRONG when asked
    "what's your time limit?". On 2026-10-04 13:54 she claimed a two-minute
    ceiling on "any single script or agent task".

    None of that was true. There is no two-minute inference ceiling: Modal and
    local Ollama both go through LiteLLM with a 6000 s client timeout and
    unlimited streaming. The only `MAX_TIMEOUT_SECONDS = 120.0` constants in
    FRIDAY are in delegation.py, mcp_runtime.py and n8n_runtime.py, and none of
    them governs model inference or generated code. She appears to have read one
    of those and generalised it into a global ceiling - 44 seconds after the
    real turn budget had just killed a turn.

    Everything below is read live from the same env vars the code uses, so it
    cannot drift from behaviour the way a remembered sentence did.
    """
    def _num(name, default):
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return float(default)

    inference_ceiling = None
    streaming_cap = None
    try:
        import litellm
        from litellm.constants import (
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            LITELLM_MAX_STREAMING_DURATION_SECONDS,
        )

        inference_ceiling = float(litellm.request_timeout)
        streaming_cap = LITELLM_MAX_STREAMING_DURATION_SECONDS
    except Exception:
        inference_ceiling = float(DEFAULT_REQUEST_TIMEOUT_SECONDS)

    return {
        # --- model inference: trusted endpoints are NOT rationed ---
        "model_inference": {
            "ceiling_seconds": inference_ceiling,
            "streaming_is_unbounded": streaming_cap is None,
            "note": (
                "There is no short cap on how long a model may think. Modal and "
                "local Ollama share the LiteLLM path and are bounded only by the "
                "client timeout above; streamed responses are never killed for "
                "taking a long time. Vision, STT and TTS have their own separate, "
                "shorter per-stage budgets."
            ),
        },
        # --- the overall turn ---
        "turn": {
            "budget_seconds": _num("FRIDAY_TURN_WALL_CLOCK", 600),
            "watchdog_seconds": max(10.0, _num("FRIDAY_WATCHDOG_TIMEOUT", 180)),
            "silent_hang_seconds": _num("FRIDAY_SILENT_HANG_LIMIT", 300),
            "note": (
                "A single turn has a finite overall budget. A turn that produces "
                "NO output at all is ended sooner than that budget. The watchdog "
                "can really stop the active turn - it interrupts the running "
                "kernel rather than only relabelling it."
            ),
        },
        # --- generated code / tool safety: these are hard, and deliberate ---
        "generated_code": {
            "hard_tool_cap": int(_num("FRIDAY_HARD_TOOL_CAP", 24)),
            "console_repeat_cap": int(_num("FRIDAY_TOOL_FLOOD_CAP", 300)),
            "advisory_tool_cap": int(_num("FRIDAY_TOOL_CAP", 12)),
            "note": (
                "Code I write is NOT allowed to run forever. A hard cap on tool "
                "executions and a cap on identical repeated output both end the "
                "turn, and the interpreter's interrupt is what actually stops the "
                "script."
            ),
        },
        "honest_answer": (
            "I have no short cap on how long a model may think - trusted "
            "inference (Modal, local Qwen) may take as long as it needs. What "
            "IS bounded is a whole turn (about %d s) and, more tightly, the "
            "code I generate - it cannot run forever."
            % int(_num("FRIDAY_TURN_WALL_CLOCK", 600))
        ),
    }


def limitations() -> dict:
    """What she genuinely cannot do. Stated so she never overclaims.

    Every entry here is a real, observed constraint on THIS machine, not a
    guess. The research (SMART, arXiv 2502.11435) is explicit that models
    overuse tools and overstate capability unless the boundary is written down.
    """
    return {
        "cannot": [
            "I cannot run wmic/wbem - not installed on Windows 11 24H2+; use PowerShell instead",
            "I must never kill my own python process or I stop existing",
            "I cannot see the HUD, so I do not know what my status panels display",
            "I have no memory of my own between restarts except lessons.md, the scratchpad and the memory DB",
              # CORRECTED (2026-10-01). The entry above was self-contradictory
              # ("no memory ... except ...") and it also listed the scratchpad as
              # durable, which is FALSE - the scratchpad is deleted at the start
              # of every turn. Sir moved a window, FRIDAY logged the lesson to the
              # scratchpad, and when he asked "will it be faster next time?" she
              # answered "I don't save or remember anything" - partly because of
              # this wording. Durable memory is lessons.md (re-read every turn)
              # plus the memory DB and skills; the scratchpad is per-turn only.
              "I do remember across turns: lessons.md is re-read every turn, and skills/ are durable - but the turn scratchpad is wiped at the start of every turn and is NOT long-term memory",
            "I cannot read the user's API keys - only pool counts are visible to me",
            # ROUND W (2026-10-04). She previously answered "what's your time
            # limit?" with a fabricated 120-second / two-minute ceiling. There is
            # no such model limit; the real bounds are the turn budget and the
            # generated-code caps, and they are stated in runtime_limits().
            "I have no short cap on how long a model may think - trusted inference is not rationed; what is bounded is a whole turn and the code I generate",
        ],
        "degrade_gracefully": [
            "No cloud keys? I run fully local on Ollama.",
            "No n8n? I do the task with my own reminders, tasks, calendar and code.",
            "Local brain down? Cloud providers answer, or I say plainly that I cannot.",
            "Telegram unset? I simply do not have phone access.",
        ],
        "honesty_rules": [
            "If a probe returns ok=False, say the check failed - never invent a number.",
            "Never claim a workflow, skill or app action succeeded without verifying it once.",
        ],
    }


def skill_status() -> dict:
    """Her own saved skills plus the one-shot loader caveat, spelled out."""
    state = _skills_state()
    state["how_to_create"] = (
        "friday_save_skill(name, steps, when_to_use=..., verify=...) - the "
        "supported path. computer.skills.new_skill.create() is LATCHED on first "
        "run in this project and silently does nothing; do not use it."
    )
    state["alternative"] = (
        "Write a normal helper module under source/server/ and import it as "
        "'from source.server.<name> import <fn>'; that path has no latch."
    )
    state["reuse_rule"] = (
        "A retrieved skill is rarely perfect for the task in front of you. Use it "
        "as-is if it fits, adapt the parts that do not, or skip it entirely. "
        "Choosing NOT to use a skill is a correct outcome, not a failure."
    )
    return state


_MODULE_PURPOSE = {
    "server.py": "main loop: dispatch, turn lifecycle, channels (voice/Telegram/text), watchdog",
    "brain_router.py": "model failover chain Modal -> Groq -> OpenRouter -> Gemini -> Local",
    "screen_understanding.py": "screen capture, vision, OCR, screen answers",
    "self_awareness.py": "this module - what she knows about herself",
    "self_improve.py": "scratchpad, lessons.md, durable turn journal",
    "memory.py": "SQLite long-term facts (remember/recall/forget)",
    "semantic_memory.py": "optional graph + embeddings over the same facts",
    "api_pools.py": "API keys, rate limiting, cooldowns, 6h bans",
    "command_router.py": "deterministic fast paths (time, open, media)",
    "status_ui.py": "desktop HUD + webview bridge",
    "tts_chain.py": "speech: cloud first, local edge-tts fallback",
    "self_improve.py ": "",
}


def code_map(limit=26) -> str:
    """Her own source layout, derived from disk so it cannot go stale.

    Modules with a documented purpose are listed FIRST - alphabetical order
    would bury brain_router.py and server.py behind a wall of filenames, which
    is exactly the opposite of useful self-knowledge.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    out = []
    try:
        names = sorted(
            n for n in os.listdir(root)
            if n.endswith(".py")
            and not n.startswith("_")
            and not n.startswith("test_")
            and not n.startswith("diagnose_")
        )
    except Exception:
        return "(source listing unavailable)"
    known = [n for n in names if n in _MODULE_PURPOSE and _MODULE_PURPOSE[n]]
    rest = [n for n in names if n not in known]
    for name in (known + rest)[:limit]:
        purpose = _MODULE_PURPOSE.get(name, "")
        out.append("- %s%s" % (name, (" - " + purpose) if purpose else ""))
    extra = len(known) + len(rest) - limit
    if extra > 0:
        out.append("- ... and %d more module(s)" % extra)
    return "\n".join(out)


def project_paths() -> dict:
    """VERIFIED absolute paths to her own project - resolved, never guessed.

    WHY (2026-11-06): asked to "check your own specs", the model guessed
    absolute source paths that do not exist and burned four tool calls on
    `FileNotFoundError: [WinError 3]`. Nothing told it the real root:
    `code_map()` prints a RELATIVE `source/server/` layout and the prompt
    carried no absolute paths at all. Every path below is derived from THIS
    module's own file location (or from self_improve's resolver for the
    screenshot dir, which honours FRIDAY_SCRATCH_DIR), and only paths that
    actually EXIST on disk are returned - so a caller can never be sent to a
    location that is not there. Read this instead of guessing.
    """
    server_dir = os.path.dirname(os.path.abspath(__file__))   # source/server
    source_root = os.path.dirname(server_dir)                 # source
    project_root = os.path.dirname(source_root)               # project root
    shots = None
    try:
        from .self_improve import scratchdir_path
        shots = scratchdir_path()
    except Exception:
        shots = None
    if not (shots and os.path.isdir(shots)):
        shots = os.path.join(project_root, "screenshots")
    candidates = {
        "project_root": project_root,
        "source": source_root,
        "server": server_dir,
        "profiles": os.path.join(server_dir, "profiles"),
        "skills": os.path.join(project_root, "skills"),
        "screenshots": shots,
    }
    return {
        name: path for name, path in candidates.items()
        if path and os.path.exists(path)
    }


def recent_changes(hours=72, limit=8) -> str:
    """What is being modified on her right now, from real file mtimes.

    The live tree has NO git history, so this uses filesystem mtimes rather than
    inventing a changelog. Truthful beats pretty.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    cutoff = time.time() - hours * 3600
    rows = []
    for folder in (root, os.path.join(root, "profiles")):
        try:
            names = os.listdir(folder)
        except Exception:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            full = os.path.join(folder, name)
            try:
                stamp = os.path.getmtime(full)
            except Exception:
                continue
            if stamp >= cutoff:
                rows.append((stamp, name))
    rows.sort(reverse=True)
    if not rows:
        return "(nothing modified in her source in the last %d hours)" % hours
    out = []
    for stamp, name in rows[:limit]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
        out.append("- %s  (last changed %s)" % (name, when))
    return "\n".join(out)


def _limits_for_prompt() -> str:
    """The limits, rendered short enough to sit in the system prompt.

    Round W: runtime_limits() existed but nothing injected it, so FRIDAY had the
    truth available and still answered "120 seconds" from memory. A fact nobody
    is shown is a fact she cannot use.
    """
    try:
        limits = runtime_limits()
    except Exception as error:
        return "- (could not read my own limits: %s - say so, do not guess)" % error
    inference = limits["model_inference"]
    turn = limits["turn"]
    code = limits["generated_code"]
    return "\n".join([
        "- Thinking is NOT rationed: no short cap on how long a model may think "
        "(client timeout %s s, streaming unbounded). Never tell me I have a "
        "two-minute or 120-second ceiling on thinking."
        % ("unknown" if inference["ceiling_seconds"] is None
           else int(inference["ceiling_seconds"])),
        "- A whole turn has a finite budget: %d s. A turn producing NO output at "
        "all is ended sooner, after %d s. The watchdog really stops the turn."
        % (int(turn["budget_seconds"]), int(turn["silent_hang_seconds"])),
        "- Code I generate cannot run forever: hard cap %d tool calls, %d "
        "identical repeated outputs, and the kernel interrupt that stops them."
        % (int(code["hard_tool_cap"]), int(code["console_repeat_cap"])),
    ])


def self_knowledge(max_chars=4200) -> str:
    """Deep, always-true awareness of her own system.

    Added 2026-10-01 at Sir's request. Deliberately NOT a claim of sentience:
    this is a factual inventory of the software she runs, the memory she keeps,
    the skills she has saved, and what is currently being changed in her - so
    she can reason about her own capabilities instead of guessing.
    """
    try:
        state = self_state()
        catalog = skill_catalog()
        runtime = runtime_models()
        brain = runtime.get("brain", {})
        try:
            _verified_paths = "; ".join(
                "%s=%s" % (name, path)
                for name, path in sorted(project_paths().items())
            )
        except Exception:
            _verified_paths = "(path listing unavailable)"
        lines = [
            "=== YOUR OWN SYSTEM (live facts about yourself) ===",
            "You are FRIDAY: a Python program on %s, pid %s, serving port %s."
            % (
                state.get("machine", {}).get("computer_name", "?"),
                state.get("process", {}).get("pid", "?"),
                state.get("server", {}).get("port", "?"),
            ),
            "YOUR BRAIN right now: %s via %s. The failover chain is: %s"
            % (brain.get("model", "?"), brain.get("pool", "?"),
               brain.get("chain", "?")),
            "YOUR PROVIDERS (counts only, you never see keys): %s"
            % ", ".join(
                "%s %d/%d" % (name, info.get("available", 0), info.get("total", 0))
                for name, info in sorted(state.get("cloud_pools", {}).items())
                if info.get("total")
            ),
            "YOUR MEMORY: %d stored fact(s) you can recall; lessons in lessons.md; "
            "a durable journal of what you actually did." % _memory_count(),
            "YOUR SKILLS: %d saved. Use friday_skills() to see them, "
            "friday_load_skill(name) to run one, friday_use_skill(name) after a "
            "successful use, friday_save_skill(...) to keep a reusable procedure. "
            "Check your saved skills BEFORE solving a similar task from scratch."
            % len(catalog),
            "YOUR REAL TIME AND SAFETY LIMITS (read live, Round W):",
            _limits_for_prompt(),
            "YOUR SOURCE LAYOUT (source/server/):",
            code_map(),
            # VERIFIED PATHS (2026-11-06): absolute, existence-checked, derived
            # from module locations. The model used to GUESS absolute paths for
            # "her own specs" and hit FileNotFoundError four times in one turn.
            "YOUR FILES (absolute and verified on disk - READ these instead of "
            "guessing a path; a guessed path does not exist): " + _verified_paths,
            "BEING CHANGED IN YOU RIGHT NOW:",
            recent_changes(),
            "Ask yourself: who_am_i(), self_state(), capabilities(), "
            "limitations(), skill_status(), last_script(), model_report(). "
            "Never guess about your own system - read it.",
        ]
        return "\n".join(lines)[:max_chars]
    except Exception as error:
        return "=== YOUR OWN SYSTEM === (self-inspection failed: %s)" % error


def trim_conversation_history(interpreter, keep=200):
    """Bound the conversation FRIDAY carries, keeping the newest `keep` messages.

    2026-10-01 (Sir: "at least the last 100 messages... 200 gives us a little
    safety"). Measured before this: `interpreter.messages` was an UNBOUNDED
    list, reset only by a process restart - it reached 66 messages and grew
    from there. The only bound was tokentrim's ~95,879-token budget deep inside
    the LLM call, which is neither a message count nor ours. ChatBuffer(100) is
    the UI transcript and never reached the model, so the "100 messages"
    requirement was met only by accident.

    Two rules make this safe:
      * it is called at TURN BOUNDARIES only, never mid-loop, so an in-flight
        tool call is never severed;
      * the cut lands on a `user` message, so the window never begins with an
        orphaned `tool`/`computer` result that has no matching call.
    Returns (trimmed_count, final_length).
    """
    default_keep = int(os.environ.get("FRIDAY_HISTORY_MESSAGES", "200") or 200)
    try:
        keep = max(20, int(keep))
    except (TypeError, ValueError):
        keep = default_keep
    messages = getattr(interpreter, "messages", None)
    if not isinstance(messages, list) or len(messages) <= keep:
        return 0, len(messages or [])
    tail = messages[-keep:]
    # Never start the window on a tool/computer result: find the first user
    # turn and drop everything before it.
    start = 0
    for index, message in enumerate(tail):
        role = (message or {}).get("role") if isinstance(message, dict) else None
        if role == "user":
            start = index
            break
    trimmed = tail[start:] if tail else []
    try:
        interpreter.messages = trimmed
    except Exception:
        return 0, len(messages)
    return len(messages) - len(trimmed), len(trimmed)


def _memory_count() -> int:
    try:
        from .memory import MemoryStore

        return len(MemoryStore().list_active(limit=500))
    except Exception:
        return 0


def awareness_brief() -> str:
    """A compact always-true self-summary for the system prompt.

    Deliberately short: it states the IDENTITY and the BOUNDARIES, not the
    volatile numbers (those belong in who_am_i(), called on demand). A prompt
    that goes stale teaches the model to trust stale things.
    """
    skills = _skills_state()
    lines = [
        "=== WHO YOU ARE (live, checked at boot) ===",
        f"You are FRIDAY running on {platform.node()} as a Python process (pid {os.getpid()}).",
        "You are not just a chatbot: you execute real code on this PC, you can read your own"
        " health, keep a task list, see your own lessons and scratchpad, and you can save NEW"
        " reusable skills so you never solve the same hard task the same way twice.",
        "You do NOT know your own live state from this prompt - call"
        " `from source.server.self_awareness import who_am_i` when you are asked how you are,"
        " what you can do, what you are connected to, or what you cannot do. Never guess it.",
        f"Skills saved so far: {skills['count']}.",
    ]
    return "\n".join(lines)
