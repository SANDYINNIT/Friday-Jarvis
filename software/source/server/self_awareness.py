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
    """Which local models are actually pulled right now."""
    import requests

    url = os.environ.get("OLLAMA_URL", os.environ.get("FRIDAY_OLLAMA_URL",
                                                      "http://localhost:11434")).rstrip("/")
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


def _skills_state() -> dict:
    """Her own skill directory, and whether the one-shot loader already ran.

    The loader in Open Interpreter latches on the FIRST python run of the
    process, so a skill written later is on disk but NOT callable until FRIDAY
    restarts. Reporting that honestly is the whole point of this function.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "skills")
    names = []
    try:
        names = sorted(n[:-3] for n in os.listdir(path) if n.endswith(".py"))
    except Exception:
        pass
    return {
        "path": path,
        "exists": os.path.isdir(path),
        "count": len(names),
        "skills": names,
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
    except Exception:
        stt_chain = ["stt_groq", "stt_deepgram", "gemini_stt"]

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

    return {
        "collected_at": _now(),
        "brain": {
            "serving": brain_desc,
            "model": brain_now or brain_last or serving.get("local_default"),
            "pool": serving.get("current_pool") or serving.get("last_pool") or "local",
            "tier": serving.get("current_tier") or serving.get("last_tier"),
            "local_default": serving.get("local_default"),
            "registry_general": general_model,
            "note": "cloud tiers are tried first; the local model is the guaranteed fallback",
        },
        "stt": {
            "local_engine": "RealtimeSTT / faster-whisper",
            "local_model": local_stt_model,
            "compute_type": os.environ.get("FRIDAY_STT_COMPUTE", "int8"),
            "cloud_chain_tried_first": stt_chain,
            "note": "cloud STT is tried first for speed; the local faster-whisper recorder is the fallback",
        },
        "tts": {
            "cloud_chain_tried_first": tts_chain,
            "last_stage": tts_last,
            "local_fallback": "edge-tts OpenAI-compatible server on localhost:5050",
            "note": "cloud TTS first; the local edge-tts server is the fallback",
        },
        "vision": {
            "local_model": vision_model,
            "note": "local vision model is tried first for screen understanding",
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
        f"- Listening to you: {stt['local_engine']} model '{stt['local_model']}' "
        f"({stt['compute_type']}) as the local fallback; cloud tried first: "
        f"{', '.join(stt['cloud_chain_tried_first']) or 'none configured'}"
    )
    tts = info["tts"]
    lines.append(
        f"- Speaking: cloud first ({', '.join(tts['cloud_chain_tried_first']) or 'none configured'}), "
        f"falling back to {tts['local_fallback']}"
        + (f"; last voice actually used: {tts['last_stage']}" if tts.get("last_stage") else "")
    )
    lines.append(f"- Vision/screens: local {info['vision']['local_model']} first")
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
    else:
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
                          "skill_status", "health_check"],
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
            "I cannot read the user's API keys - only pool counts are visible to me",
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
        "computer.skills.new_skill.create() -> set .name -> .add_step(text) -> .save()"
    )
    state["alternative"] = (
        "Write a normal helper module under source/server/ and import it as "
        "'from source.server.<name> import <fn>'; that path has no latch."
    )
    return state


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
