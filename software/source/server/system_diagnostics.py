"""Basic IT support for the PC: structured health, no guessing.

This is a HELPER MODULE the brain CHOOSES to call - it is never auto-fired by
phrase matching. Every answer about the machine is still authored and worded by
the model; these functions only SAVE it from re-typing psutil/socket/subprocess
probes for the questions a professional actually asks: "is my computer OK?",
"how much disk is left?", "is the print spooler running?", "am I online?".

Project policy (software/AGENTS.md): helpers are capabilities the model
discovers and invokes through its own reasoning, never auto-fired triggers.
So nothing here is registered in command_router - the live prompt
(profiles/default.py) is what advertises these to the brain.

Import form that WORKS from the model's kernel (CWD = software\\):

    from source.server.system_diagnostics import system_health

(A bare `from system_diagnostics import ...` does NOT resolve: the module lives
two levels down and the kernel's sys.path only has the project root.)

Conventions inherited from the sibling modules (home_assistant.py,
mcp_runtime.py, web_scrape.py):
  - public functions return plain dicts and NEVER raise for environmental
    problems; the model reads the returned text, so honesty beats exceptions
  - every external call is bounded by an explicit timeout
  - optional third-party deps are imported defensively
  - nothing here is destructive: read-only probes only
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import time

try:  # psutil is the only soft dependency; everything else degrades gracefully.
    import psutil
except Exception:  # pragma: no cover - exercised only on a broken venv
    psutil = None

# Windows-only probe. A no-op command keeps the timeout bounded and lets
# Get-Service / CIM run under the caller's own permissions.
POWERSHELL = "powershell.exe"
PS_TIMEOUT_SECONDS = 12.0
CMD_TIMEOUT_SECONDS = 12.0
SOCKET_TIMEOUT_SECONDS = 3.0
DISK_PATHS = ("C:\\", "D:\\")
DEFAULT_TOP_N = 8
MAX_TOP_N = 50
HEALTH_SAMPLE_SECONDS = 0.6
UPTIME_SAMPLE_SECONDS = 1.0

# A machine is "ok" while it is comfortably under these. Deliberately generous:
# this flags a genuinely sick PC, it does not grade performance.
CPU_WARN_PERCENT = 90.0
MEMORY_WARN_PERCENT = 90.0
DISK_WARN_PERCENT = 90.0

_NETWORK_TARGETS = (
    ("internet", "1.1.1.1", 443),
    ("internet", "8.8.8.8", 53),
    ("local_gateway", "192.168.1.1", 80),
)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _pct(part: float, whole: float) -> float:
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)


def _run(argv, timeout=PS_TIMEOUT_SECONDS):
    """Run a command, returning (ok, output). Never raises."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except FileNotFoundError:
        return False, f"{argv[0]} is not available on this machine"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:g}s"
    except Exception as error:  # pragma: no cover - defensive
        return False, f"failed: {error}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return False, detail or f"exit code {completed.returncode}"
    return True, (completed.stdout or "").strip()


# --------------------------------------------------------------------------- #
# host
# --------------------------------------------------------------------------- #
def host_info() -> dict:
    """Static facts about this machine: OS, CPU, RAM, uptime, Python."""
    info = {
        "computer_name": platform.node() or socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "os_caption": "",
        "architecture": platform.machine(),
        "cpu_cores_logical": os.cpu_count() or 0,
        "cpu_name": platform.processor() or "",
        "total_memory_gb": None,
        "uptime": "",
        "python": sys.version.split()[0],
        "collected_at": _now(),
    }
    if psutil is not None:
        try:
            info["total_memory_gb"] = round(psutil.virtual_memory().total / (1024**3), 1)
            up = int(time.time() - psutil.boot_time())
            days, rem = divmod(up, 86400)
            hours, rem = divmod(rem, 3600)
            minutes = rem // 60
            if days:
                info["uptime"] = f"{days}d {hours}h {minutes}m"
            else:
                info["uptime"] = f"{hours}h {minutes}m"
        except Exception:
            pass
    if not info["cpu_name"]:
        ok, out = _run([POWERSHELL, "-NoProfile", "-Command",
                        "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)"], CMD_TIMEOUT_SECONDS)
        if ok:
            info["cpu_name"] = out
    return info


# --------------------------------------------------------------------------- #
# resources
# --------------------------------------------------------------------------- #
def cpu_snapshot(interval: float = HEALTH_SAMPLE_SECONDS) -> dict:
    """CPU load. A short blocking sample is the only way to get a real number."""
    if psutil is None:
        return {"ok": False, "error": "psutil is not installed, cannot read CPU load"}
    try:
        first = psutil.cpu_percent(interval=interval)
    except Exception as error:
        return {"ok": False, "error": f"CPU read failed: {error}"}
    try:
        # percpu=True is required for a per-core LIST; without it psutil
        # returns a single float and iterating it raises.
        per_core = psutil.cpu_percent(interval=None, percpu=True)
    except Exception:
        per_core = []
    try:
        physical = psutil.cpu_count(logical=False)
    except Exception:
        physical = None
    try:
        freq = psutil.cpu_freq()
    except Exception:
        freq = None
    return {
        "ok": True,
        "percent": round(float(first), 1),
        "per_core_percent": [round(float(v), 1) for v in per_core],
        "logical_cores": psutil.cpu_count() or 0,
        "physical_cores": physical,
        "current_mhz": round(freq.current, 1) if freq else None,
        "max_mhz": round(freq.max, 1) if freq and freq.max else None,
    }


def memory_snapshot() -> dict:
    """Physical memory: used/total/available and the swap/committed figure."""
    if psutil is None:
        return {"ok": False, "error": "psutil is not installed, cannot read memory"}
    try:
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
    except Exception as error:
        return {"ok": False, "error": f"memory read failed: {error}"}
    gb = 1024 ** 3
    return {
        "ok": True,
        "percent": round(float(vm.percent), 1),
        "total_gb": round(vm.total / gb, 1),
        "used_gb": round(vm.used / gb, 1),
        "available_gb": round(vm.available / gb, 1),
        "swap_percent": round(float(swap.percent), 1),
        "swap_used_gb": round(swap.used / gb, 1),
        "swap_total_gb": round(swap.total / gb, 1),
    }


def disk_snapshot(paths=DISK_PATHS) -> dict:
    """Free/used space per volume. Defaults to C: and D: (the drives Sir uses)."""
    volumes = []
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
        except Exception as error:
            volumes.append({"path": path, "ok": False, "error": str(error)})
            continue
        gb = 1024 ** 3
        volumes.append({
            "path": path,
            "ok": True,
            "total_gb": round(usage.total / gb, 1),
            "used_gb": round(usage.used / gb, 1),
            "free_gb": round(usage.free / gb, 1),
            "percent_used": _pct(usage.used, usage.total),
        })
    usable = [v for v in volumes if v.get("ok")]
    return {
        "ok": bool(usable),
        "volumes": volumes,
        "lowest_free_path": min(usable, key=lambda v: v["free_gb"])["path"] if usable else "",
        "error": "" if usable else "no readable volume",
    }


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #
def top_processes(limit: int = DEFAULT_TOP_N, by: str = "memory") -> dict:
    """Busiest processes, by RAM (default) or CPU."""
    if psutil is None:
        return {"ok": False, "error": "psutil is not installed, cannot list processes"}
    limit = max(1, min(int(limit), MAX_TOP_N))
    key = "cpu" if str(by).lower().startswith("cpu") else "memory"
    rows = []
    try:
        for proc in psutil.process_iter(["name", "pid", "memory_info", "cpu_percent"]):
            try:
                info = proc.info.get("memory_info")
                rows.append({
                    "pid": proc.info.get("pid"),
                    "name": proc.info.get("name") or "?",
                    "memory_mb": round((info.rss / (1024 ** 2)), 1) if info else 0.0,
                    "cpu_percent": round(float(proc.info.get("cpu_percent") or 0.0), 1),
                })
            except Exception:
                continue
    except Exception as error:
        return {"ok": False, "error": f"process list failed: {error}"}
    rows.sort(key=lambda r: r["memory_mb"] if key == "memory" else r["cpu_percent"], reverse=True)
    return {
        "ok": True,
        "sorted_by": key,
        "count": len(rows),
        "processes": rows[:limit],
    }


def process_count() -> dict:
    """Cheap "how busy is it" number: total processes and thread count."""
    if psutil is None:
        return {"ok": False, "error": "psutil is not installed"}
    try:
        return {"ok": True, "processes": len(psutil.pids()),
                "threads": psutil.cpu_count() and sum(p.num_threads() for p in psutil.process_iter())}
    except Exception as error:
        return {"ok": False, "error": f"process count failed: {error}"}


# --------------------------------------------------------------------------- #
# services (Windows)
# --------------------------------------------------------------------------- #
def list_services(state: str = "running", limit: int = 200) -> dict:
    """Windows services filtered by state (running/stopped/paused/all).

    Read-only: Get-Service only. This never starts or stops anything.
    """
    ok, out = _run([POWERSHELL, "-NoProfile", "-Command",
                    "Get-Service | Select-Object Name,DisplayName,Status | ConvertTo-Csv -NoTypeInformation"],
                   PS_TIMEOUT_SECONDS)
    if not ok:
        return {"ok": False, "error": out, "services": []}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {"ok": False, "error": "Get-Service returned no rows", "services": []}
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    services = []
    wanted = str(state).strip().lower()
    for line in lines[1:]:
        cells = [c.strip().strip('"') for c in line.split(",")]
        row = dict(zip(header, cells))
        if wanted in ("", "all", "any") or row.get("Status", "").lower() == wanted:
            services.append({
                "name": row.get("Name", ""),
                "display_name": row.get("DisplayName", ""),
                "status": row.get("Status", ""),
            })
    services.sort(key=lambda s: s["name"].lower())
    return {"ok": True, "state": wanted or "all", "count": len(services),
            "services": services[: max(1, int(limit))]}


def service_status(name: str) -> dict:
    """Is one specific Windows service running? e.g. service_status("spooler").

    Read-only status check. Starting/stopping a service stays the brain's own
    authored code (Start-Service / Stop-Service), never an auto-fired trigger.
    """
    wanted = str(name or "").strip()
    if not wanted:
        return {"ok": False, "error": "no service name given"}
    ok, out = _run([POWERSHELL, "-NoProfile", "-Command",
                    f"Get-Service -Name '{wanted}' | Select-Object Name,DisplayName,Status "
                    f"| ConvertTo-Csv -NoTypeInformation"], PS_TIMEOUT_SECONDS)
    if not ok:
        return {"ok": False, "error": out, "name": wanted}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {"ok": False, "error": f"service '{wanted}' not found", "name": wanted}
    cells = [c.strip().strip('"') for c in lines[1].split(",")]
    return {"ok": True, "name": cells[0], "display_name": cells[1] if len(cells) > 1 else "",
            "status": cells[2] if len(cells) > 2 else ""}


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #
def network_snapshot(targets=_NETWORK_TARGETS) -> dict:
    """Connectivity check. Online is decided by an INTERNET host only.

    A raw TCP connect: fast, no ICMP needed, and it proves the route works
    (unlike ping, which Windows often blocks).
    """
    checks = []
    online = False
    for label, host, port in targets:
        row = {"label": label, "host": host, "port": port, "reachable": False,
               "latency_ms": None, "error": ""}
        started = time.monotonic()
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT_SECONDS)
            row["reachable"] = True
            row["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            if label == "internet":
                online = True
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        checks.append(row)
    return {
        "ok": True,
        "online": online,
        "hostname": socket.gethostname(),
        "local_ip": local_ip(),
        "checks": checks,
    }


def local_ip() -> str:
    """This machine's LAN address, without sending anything over the network."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.connect(("1.1.1.1", 80))  # no packets sent; just picks the route
        return sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# rollup
# --------------------------------------------------------------------------- #
def _verdict(value, warn_at) -> str:
    if value is None:
        return "unknown"
    if value >= warn_at:
        return "high"
    if value >= warn_at * 0.75:
        return "elevated"
    return "ok"


def health_report() -> dict:
    """ONE-CALL summary for "is my computer OK?".

    Returns {"ok": bool, "verdict": str, "summary": str, "findings": [...],
             "checks": {...}}. `findings` is empty when nothing is wrong, so the
    brain can say "all good" without re-deriving thresholds.
    """
    host = host_info()
    cpu = cpu_snapshot()
    memory = memory_snapshot()
    disk = disk_snapshot()
    network = network_snapshot()

    findings = []
    checks = {"host": host, "cpu": cpu, "memory": memory, "disk": disk, "network": network}

    if cpu.get("ok") and cpu["percent"] >= CPU_WARN_PERCENT:
        findings.append(f"CPU at {cpu['percent']}% — unusually high, something is chewing it up")
    if memory.get("ok") and memory["percent"] >= MEMORY_WARN_PERCENT:
        findings.append(f"Memory at {memory['percent']}% — {memory['available_gb']} GB still free")
    if memory.get("ok") and memory.get("swap_percent", 0) >= 90:
        findings.append(f"Page file at {memory['swap_percent']}% — Windows is paging hard, the machine feels slow")
    for volume in disk.get("volumes", []):
        if not volume.get("ok"):
            continue
        if volume["percent_used"] >= DISK_WARN_PERCENT:
            findings.append(f"Disk {volume['path']} is {volume['percent_used']}% full — only {volume['free_gb']} GB free")
    if not network.get("online"):
        findings.append("No internet route from this machine (checked 1.1.1.1 and 8.8.8.8)")

    if findings:
        verdict = "attention"
        summary = "; ".join(findings)
    else:
        verdict = "ok"
        bits = []
        if cpu.get("ok"):
            bits.append(f"CPU {cpu['percent']}%")
        if memory.get("ok"):
            bits.append(f"RAM {memory['percent']}%")
        for volume in disk.get("volumes", []):
            if volume.get("ok"):
                bits.append(f"{volume['path']} {volume['free_gb']} GB free")
        if network.get("online"):
            bits.append("online")
        summary = "All clear: " + ", ".join(bits) if bits else "All clear"

    return {
        "ok": True,
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "checks": checks,
        "collected_at": _now(),
    }


def is_online() -> dict:
    """Tiny connectivity answer for "am I online?"."""
    snap = network_snapshot()
    return {"ok": True, "online": snap["online"], "local_ip": snap["local_ip"],
            "checks": snap["checks"]}


def diagnose(areas=None) -> dict:
    """Wider sweep: health + services + processes, for "why is my PC weird?".

    `areas` may narrow it to any of: health, services, processes, network.
    """
    wanted = [a.strip().lower() for a in (areas or ["health", "services", "processes", "network"]) if a.strip()]
    result = {"ok": True, "areas": wanted, "collected_at": _now()}
    if "health" in wanted:
        result["health"] = health_report()
    if "services" in wanted:
        result["services"] = list_services(state="stopped", limit=40)
    if "processes" in wanted:
        result["processes"] = top_processes(limit=10)
    if "network" in wanted:
        result["network"] = network_snapshot()
    return result


def system_health(brief: bool = False) -> str:
    """Readable one-screen health report, ready to speak.

    The model can print this straight to the console and then phrase the answer
    in FRIDAY's own voice. `brief=True` trims it to the headline numbers.
    """
    report = health_report()
    lines = [f"FRIDAY health check ({report['collected_at']}) — verdict: {report['verdict'].upper()}"]
    host = report["checks"]["host"]
    lines.append(f"Computer: {host.get('computer_name')} | {host.get('os')} | up {host.get('uptime')}")
    cpu = report["checks"]["cpu"]
    mem = report["checks"]["memory"]
    if cpu.get("ok"):
        lines.append(f"CPU: {cpu['percent']}% of {cpu.get('logical_cores')} logical cores ({_verdict(cpu['percent'], CPU_WARN_PERCENT)})")
    if mem.get("ok"):
        lines.append(f"RAM: {mem['percent']}% used — {mem['used_gb']} GB used / {mem['total_gb']} GB, {mem['available_gb']} GB free")
    for volume in report["checks"]["disk"].get("volumes", []):
        if volume.get("ok"):
            lines.append(f"Disk {volume['path']}: {volume['free_gb']} GB free of {volume['total_gb']} GB ({volume['percent_used']}% used)")
    net = report["checks"]["network"]
    lines.append(f"Network: {'ONLINE' if net.get('online') else 'OFFLINE'} — local IP {net.get('local_ip') or 'unknown'}")
    if report["findings"]:
        lines.append("Findings:")
        lines.extend(f"  - {f}" for f in report["findings"])
    if brief:
        lines = lines[:4]
    return "\n".join(lines)
