"""Monitor the owned Light listener while the desktop event loop runs."""
import os
import json
import sys
import threading
import time

import requests


RECOVERY_EXIT = 75
STARTUP_SECONDS = 120
CHECK_SECONDS = 2
MAX_MISSES = 3


def server_thread_stack(thread):
    """Bounded frame locations only: no locals, arguments, source or paths."""
    try:
        frame = sys._current_frames().get(thread.ident)
        frames = []
        while frame is not None and len(frames) < 16:
            code = frame.f_code
            frames.append({"file": code.co_filename.replace('\\', '/').rsplit('/', 1)[-1],
                           "function": code.co_name, "line": frame.f_lineno})
            frame = frame.f_back
        return frames
    except Exception:
        return []


def listener_serving(interpreter, thread, host, port):
    server = interpreter.server.uvicorn_server
    return (thread.is_alive() and server.started and not server.should_exit
            and server.config.host == host and server.config.port == port
            and any(listener.is_serving() for listener in getattr(server, "servers", ())))


def cleanup_light(interpreter):
    """Request shutdown of owned services without waiting indefinitely."""
    if getattr(interpreter, "_light_shutdown_requested", False):
        return
    interpreter._light_shutdown_requested = True
    wrapper = getattr(interpreter, "server", None)
    server = getattr(wrapper, "uvicorn_server", None)
    if server is not None:
        server.should_exit = True
    callbacks = (getattr(interpreter, "close_mcp", None),
                 getattr(interpreter, "stop_reminders", None),
                 getattr(getattr(interpreter, "stt", None), "shutdown", None))

    def cleanup(callback):
        try:
            callback()
        except Exception:
            pass

    workers = [threading.Thread(target=cleanup, args=(callback,), daemon=True)
               for callback in callbacks if callable(callback)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 6
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))


def run_hud(interpreter, server_thread, host, port, show, report, *, terminate=None,
            startup_seconds=STARTUP_SECONDS, interval=CHECK_SECONDS):
    """Run the main-thread HUD and supervise its local ASGI server until exit."""
    terminate = os._exit if terminate is None else terminate
    stopped = threading.Event()
    ending = threading.Lock()

    def fail(reason):
        if (getattr(interpreter, "_light_shutdown_requested", False)
                or not ending.acquire(blocking=False)):
            return
        try:
            print("[light-lifecycle] server_stack=" + json.dumps(server_thread_stack(server_thread)), flush=True)
            report(reason)
        finally:
            try:
                cleanup_light(interpreter)
                server_thread.join(2)
            finally:
                terminate(RECOVERY_EXIT)

    def monitor():
        deadline = time.monotonic() + startup_seconds
        ready, misses = False, 0
        with requests.Session() as session:
            session.trust_env = False
            while not stopped.is_set() and not getattr(interpreter, "_light_shutdown_requested", False):
                if not server_thread.is_alive():
                    fail("Light server thread exited")
                    return
                healthy = False
                if listener_serving(interpreter, server_thread, host, port):
                    try:
                        response = session.get(f"http://{host}:{port}/ping", timeout=1,
                                               allow_redirects=False)
                        healthy = response.status_code == 200 and response.text.strip() == "pong"
                    except requests.RequestException:
                        pass
                if healthy:
                    if not ready:
                        print("[light-lifecycle] Owned listener ready", flush=True)
                    ready, misses = True, 0
                elif ready:
                    misses += 1
                    if misses >= MAX_MISSES:
                        fail("Light listener unresponsive after readiness"
                             if listener_serving(interpreter, server_thread, host, port)
                             else "Light listener lost after readiness")
                        return
                elif time.monotonic() >= deadline:
                    fail("Light listener startup deadline exceeded")
                    return
                stopped.wait(interval)

    def watch():
        try:
            monitor()
        except Exception:
            fail("Light listener watchdog failed")

    threading.Thread(target=watch, daemon=True, name="friday-listener-watchdog").start()
    try:
        show()
    except Exception:
        fail("HUD and fallback exited with an error")
    else:
        fail("HUD event loop exited")
    finally:
        stopped.set()
