"""FRIDAY doctor: one command that says what is actually working right now.

WHY THIS EXISTS (2026-09-29)
---------------------------
Probing every fallback tier by hand showed only 4 of 11 brain tiers healthy, a
dead Gemini STT stage, and a local TTS server that was down - and NONE of that
was visible anywhere. Provider health reached the HUD only as key counts, the
per-model cooldown panel was permanently empty (a broken module check), and
FRIDAY herself had no way to check.

So degradation was discovered by the user, mid-conversation, as slowness or
silence. This module makes it discoverable up front:

    from source.server.doctor import run_doctor, doctor_report
    run_doctor()          # structured dict, no exceptions
    doctor_report()       # readable block FRIDAY can print and read aloud

Design rules (same as every other helper): never raise, always return data,
bound every network call, and NEVER print a key. A doctor that leaks
credentials while diagnosing them would be worse than no doctor.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
import time

DEFAULT_TIMEOUT = 6.0
PROMPT = "Reply with exactly the word: OK"
STT_CHAIN = ("stt_groq", "stt_deepgram", "gemini_stt")
BRAIN_STAGES = (
    ("groq-fast", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b", "brain_groq"),
    ("groq-strong", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "brain_groq"),
    ("gemini-3.5-flash", "gemini", "gemini-3.5-flash", "brain_gemini"),
    ("gemini-3.5-flash-lite", "gemini", "gemini-3.5-flash-lite", "brain_gemini"),
    ("gemini-3.1-flash-lite", "gemini", "gemini-3.1-flash-lite", "brain_gemini"),
    ("openrouter-nemotron-ultra", "https://openrouter.ai/api/v1",
     "nvidia/nemotron-3-ultra-550b-a55b:free", "brain_openrouter"),
    ("openrouter-gemma-4", "https://openrouter.ai/api/v1",
     "google/gemma-4-31b-it:free", "brain_openrouter"),
    ("openrouter-nemotron-lightning", "https://openrouter.ai/api/v1",
     "nvidia/nemotron-3.5-lightning:free", "brain_openrouter"),
)
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
LOCAL_MODEL = "ollama_chat/qwen3:8b"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _pool_key(pool_name, index=0):
    try:
        from . import api_pools

        pool = api_pools.pool(pool_name)
        if pool is None or pool.empty or index >= pool.total():
            return ""
        return pool._keys[index]
    except Exception:
        return ""


def _short(text, limit=110):
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def _probe_openai_style(label, base, model, key, timeout=DEFAULT_TIMEOUT):
    import requests

    if not key:
        return {"stage": label, "ok": False, "ms": 0, "detail": "no API key configured"}
    started = time.monotonic()
    try:
        response = requests.post(
            base.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": PROMPT}],
                  "max_tokens": 8, "temperature": 0},
            timeout=timeout,
        )
        elapsed = round((time.monotonic() - started) * 1000)
        if response.status_code == 200:
            try:
                data = response.json()
                message = data.get("choices", [{}])[0].get("message", {})
                # Reasoning models can answer inside `reasoning` while content is
                # still empty, so accept either as proof the tier is alive.
                text = (message.get("content") or message.get("reasoning") or "").strip()
            except Exception:
                text = ""
            return {"stage": label, "ok": True, "ms": elapsed,
                    "detail": f"responded in {elapsed} ms" + ("" if text else " (empty body)")}
        return {"stage": label, "ok": False, "ms": elapsed,
                "detail": f"HTTP {response.status_code} {_short(response.text, 70)}"}
    except Exception as error:
        return {"stage": label, "ok": False,
                "ms": round((time.monotonic() - started) * 1000),
                "detail": f"{type(error).__name__}"}


def _probe_gemini(label, model, key, timeout=DEFAULT_TIMEOUT):
    import requests

    if not key:
        return {"stage": label, "ok": False, "ms": 0, "detail": "no API key configured"}
    started = time.monotonic()
    try:
        response = requests.post(
            f"{GEMINI_BASE}/{model}:generateContent",
            params={"key": key},
            json={"contents": [{"parts": [{"text": PROMPT}]}],
                  "generationConfig": {"maxOutputTokens": 16, "temperature": 0}},
            timeout=timeout,
        )
        elapsed = round((time.monotonic() - started) * 1000)
        if response.status_code == 200:
            return {"stage": label, "ok": True, "ms": elapsed, "detail": f"responded in {elapsed} ms"}
        return {"stage": label, "ok": False, "ms": elapsed,
                "detail": f"HTTP {response.status_code} {_short(response.text, 70)}"}
    except Exception as error:
        return {"stage": label, "ok": False,
                "ms": round((time.monotonic() - started) * 1000),
                "detail": f"{type(error).__name__}"}


def _probe_local_brain(timeout=90.0):
    import requests

    url = os.environ.get("FRIDAY_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    started = time.monotonic()
    try:
        response = requests.post(
            f"{url}/api/chat",
            json={"model": "qwen3:8b", "messages": [{"role": "user", "content": PROMPT}],
                  "stream": False, "think": False},
            timeout=timeout,
        )
        elapsed = round((time.monotonic() - started) * 1000)
        if response.status_code == 200:
            return {"stage": "local qwen3:8b", "ok": True, "ms": elapsed,
                    "detail": f"responded in {elapsed} ms"}
        return {"stage": "local qwen3:8b", "ok": False, "ms": elapsed,
                "detail": f"HTTP {response.status_code}"}
    except Exception as error:
        return {"stage": "local qwen3:8b", "ok": False,
                "ms": round((time.monotonic() - started) * 1000),
                "detail": f"{type(error).__name__}"}


def _probe_ollama_reachable(timeout=3.0):
    import requests

    url = os.environ.get("FRIDAY_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    try:
        response = requests.get(f"{url}/api/tags", timeout=timeout)
        if response.status_code == 200:
            names = [m.get("name", "") for m in response.json().get("models", [])]
            return {"reachable": True, "count": len(names),
                    "default_pulled": "qwen3:8b" in names, "models": names[:15]}
        return {"reachable": False, "error": f"HTTP {response.status_code}"}
    except Exception as error:
        return {"reachable": False, "error": f"{type(error).__name__}"}


def _probe_port(port, timeout=0.5):
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def run_doctor(probe_brain: bool = True, full: bool = True) -> dict:
    """Full health report. `probe_brain=False` skips live model calls (fast check).

    `full=False` returns only the compact verdict fields, which is what FRIDAY
    should use mid-conversation: the complete payload is large enough to flood
    the model's context and leave it unable to answer.
    """
    report = {
        "checked_at": _now(),
        "machine": {
            "computer_name": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "python": sys.version.split()[0],
        },
        "brain_tiers": [],
        "ollama": _probe_ollama_reachable(),
        "local_brain": None,
        "stt_chain": [],
        "tts": {},
        "ports": {},
        "keys": {},
        "notes": [],
    }

    try:
        from . import api_pools

        for name, stat in api_pools.describe_pools().items():
            report["keys"][name] = f"{stat.get('available', 0)}/{stat.get('total', 0)}"
        ledger = api_pools.ledger()
        report["cooling_keys"] = ledger.get("keys", {})
        report["cooling_models"] = ledger.get("models", {})
    except Exception as error:
        report["notes"].append(f"api_pools unreadable: {type(error).__name__}")

    for pool_name in STT_CHAIN:
        has_key = bool(_pool_key(pool_name))
        report["stt_chain"].append({
            "pool": pool_name,
            "configured": has_key,
            "note": "" if has_key else "no key - this stage will be skipped",
        })

    try:
        from . import tts_bootstrap

        report["tts"]["local_edge_server"] = tts_bootstrap.probe()
    except Exception as error:
        report["tts"]["local_edge_server"] = False
        report["notes"].append(f"tts probe errored: {type(error).__name__}")
    try:
        from . import gemini_tts

        report["tts"]["cloud_chain"] = list(gemini_tts.GEMINI_TTS_MODEL_CHAIN)
    except Exception:
        report["tts"]["cloud_chain"] = []
    report["tts"]["cloud_keys"] = bool(_pool_key("gemini_tts"))

    server_port = int(os.environ.get("FRIDAY_SERVER_PORT", "10101") or 10101)
    report["ports"] = {
        f"{server_port} (FRIDAY)": _probe_port(server_port),
        "5050 (local TTS)": _probe_port(5050),
        "11434 (Ollama)": _probe_port(11434),
    }

    if probe_brain:
        groq_key = _pool_key("brain_groq")
        gemini_key = _pool_key("brain_gemini")
        openrouter_key = _pool_key("brain_openrouter")
        for label, base, model, pool in BRAIN_STAGES:
            if base == "gemini":
                report["brain_tiers"].append(_probe_gemini(label, model, gemini_key))
            else:
                key = groq_key if pool == "brain_groq" else openrouter_key
                report["brain_tiers"].append(_probe_openai_style(label, base, model, key))
        report["local_brain"] = _probe_local_brain()

    healthy = [tier["stage"] for tier in report["brain_tiers"] if tier.get("ok")]
    report["brain_healthy"] = healthy
    report["brain_healthy_count"] = len(healthy)
    report["brain_degraded"] = bool(
        report["brain_tiers"] and len(healthy) < 2
    )
    if not report.get("tts", {}).get("cloud_keys") and not report.get("tts", {}).get("local_edge_server"):
        report["notes"].append(
            "NO WORKING TEXT-TO-SPEECH: no cloud TTS key and no local voice server - FRIDAY cannot speak."
        )
    if not report["ollama"].get("reachable"):
        report["notes"].append("Ollama is not answering on 11434 - the local brain is unavailable.")
    elif not report["ollama"].get("default_pulled"):
        report["notes"].append("Ollama is up but qwen3:8b is not pulled.")

    if not full:
        # Compact verdict only - safe to print inside a conversation turn.
        stt_ready = [entry["pool"] for entry in report.get("stt_chain", []) if entry.get("configured")]
        return {
            "checked_at": report["checked_at"],
            "brain_healthy_count": report.get("brain_healthy_count", 0),
            "brain_degraded": report.get("brain_degraded", False),
            "down_tiers": [t["stage"] for t in report.get("brain_tiers", []) if not t.get("ok")],
            "local_brain_ok": (report.get("local_brain") or {}).get("ok"),
            "ollama_reachable": report["ollama"].get("reachable"),
            "stt_ready": stt_ready,
            "tts_ok": bool(report["tts"].get("cloud_keys") or report["tts"].get("local_edge_server")),
            "cooling_models": list((report.get("cooling_models") or {}).keys())[:5],
            "notes": report["notes"],
        }
    return report


def doctor_report() -> str:
    """Readable health report, ready for FRIDAY to print and read aloud."""
    report = run_doctor()
    lines = [f"FRIDAY doctor ({report['checked_at']})"]
    machine = report["machine"]
    lines.append(f"Machine: {machine['computer_name']} ({machine['os']}), python {machine['python']}")

    tiers = report.get("brain_tiers") or []
    if tiers:
        good = report["brain_healthy_count"]
        lines.append(f"Brain tiers working: {good}/{len(tiers)}")
        for tier in tiers:
            mark = "ok  " if tier.get("ok") else "DOWN"
            lines.append(f"  [{mark}] {tier['stage']:<32} {tier.get('detail', '')[:52]}")
    local = report.get("local_brain")
    if local:
        mark = "ok  " if local.get("ok") else "DOWN"
        lines.append(f"  [{mark}] {'local qwen3:8b':<32} {local.get('detail', '')[:52]}")

    stt = [entry for entry in report.get("stt_chain", []) if entry.get("configured")]
    lines.append("Speech-to-text chain: "
                 + (" -> ".join(entry["pool"] for entry in stt) or "NO KEYS CONFIGURED"))

    tts = report.get("tts", {})
    lines.append(
        "Text-to-speech: "
        + ("cloud " + ", ".join(tts.get("cloud_chain", [])) if tts.get("cloud_keys")
           else "no cloud key")
        + f" | local voice server: {'up' if tts.get('local_edge_server') else 'down (starts on demand)'}"
    )

    cooling = report.get("cooling_models") or {}
    if cooling:
        lines.append("Cooling down right now: "
                     + ", ".join(f"{name} ({mins}m)" for name, mins in list(cooling.items())[:6]))

    for note in report.get("notes", []):
        lines.append(f"NOTE: {note}")
    return "\n".join(lines)


def summary_line() -> str:
    """One-line verdict for a status bar or a quick spoken check."""
    report = run_doctor(probe_brain=False, full=False)
    parts = []
    if report.get("ollama_reachable"):
        parts.append("local brain up")
    else:
        parts.append("local brain DOWN")
    parts.append("STT " + ("ready" if report.get("stt_ready") else "no keys"))
    parts.append("TTS " + ("ready" if report.get("tts_ok") else "NONE"))
    if report.get("cooling_models"):
        parts.append(f"{len(report['cooling_models'])} model(s) cooling")
    return "FRIDAY health: " + ", ".join(parts)
