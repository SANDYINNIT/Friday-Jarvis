import typer
import platform
import threading
import os
import importlib
import subprocess
import shutil
import json
import time
import signal
import warnings
import sys
from source.server.audio_startup import enabled as audio_startup_enabled

# Windows registry auto-start boots with cwd=C:\WINDOWS\system32 — force the
# project root so RELATIVE file writes (RealtimeSTT's realtimesst.log, etc.)
# never hit a permission wall and crash the voice pipeline at boot.
os.chdir(os.path.dirname(os.path.realpath(__file__)))
import requests

if not audio_startup_enabled() and not os.environ.get("FRIDAY_USER_ROUTES"):
    from dotenv import load_dotenv

    load_dotenv()
    if audio_startup_enabled():
        raise ValueError("Set FRIDAY_AUDIO_STARTUP_CHECK in the launcher environment, before startup")

# Sir's directive (2026-10-02): automatic fact-learning ON, but CONSERVATIVE.
#
# Audit finding: `_automatic_memory_enabled()` in server.py defaults to False and
# nothing in the tree ever set FRIDAY_AUTO_MEMORY or
# interpreter.friday_auto_memory_enabled, so the extractor NEVER ran on a live
# turn - the hardening in memory.py was exercised only by tests. Facts reached
# memory solely via an explicit "remember that X" or the brain's own
# friday_remember() helper.
#
# It is enabled here with setdefault so it applies however FRIDAY is started
# (autostart vbs, supervised launcher, or a manual `python main.py`) while still
# letting an operator turn it OFF by setting the variable to 0 in the
# environment. The extractor itself stays deliberately restrictive: questions,
# directives, sentence fragments, transient pronoun facts and anything carrying
# a secret shape are rejected before anything is written.
os.environ.setdefault("FRIDAY_AUTO_MEMORY", "1")

app = typer.Typer()

def warmup_services(interpreter, debug=False):
    """Local TTS services are NOT warmed at startup.

    FRIDAY's voice is cloud-first (Gemini 3.1 Flash TTS on both accounts).
    The local edge-tts voice is spun up lazily — only the moment both cloud
    accounts fail and the voice stage switches to local — see
    source/server/gemini_tts.py for the on-demand bootstrap.
    """
    return


@app.command()
def run(
    server: str = typer.Option(
        None,
        "--server",
        help="Run server (accepts `livekit` or `light`)",
    ),
    server_host: str = typer.Option(
        "127.0.0.1",
        "--server-host",
        help="Local bind address. FRIDAY servers are restricted to IPv4 loopback.",
    ),
    server_port: int = typer.Option(
        10101,
        "--server-port",
        help="Specify the server port where the server will deploy",
    ),
    expose: bool = typer.Option(False, "--expose", help="Expose only the authenticated Light n8n webhook"),
    domain: str = typer.Option(None, "--domain", help="Use `--expose` with a custom ngrok domain"),
    client: str = typer.Option(None, "--client", help="Run client of a particular type. Accepts `light-python`, defaults to `light-python`"),
    server_url: str = typer.Option(
        None,
        "--server-url",
        help="Specify the server URL that the --client should expect. Defaults to server-host and server-port",
    ),
    qr: bool = typer.Option(
        False, "--qr", help="Display the local server connection information as a QR code"
    ),
    profiles: bool = typer.Option(
        False,
        "--profiles",
        help="Opens the folder where profiles are contained",
    ),
    profile: str = typer.Option(
        "default.py",
        "--profile",
        help="Specify the path to the profile, or the name of the file if it's in the `profiles` directory (run `--profiles` to open the profiles directory)",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Print latency measurements and save microphone recordings locally for manual playback",
    ),
    status_ui: bool = typer.Option(
        False,
        "--status-ui",
        help="Open the optional FRIDAY face/status window",
    ),
    background: bool = typer.Option(
        False,
        "--background",
        help="Boot FRIDAY hidden in the system tray (registry auto-start): the brain + telegram run, the HUD stays hidden until Open App.",
    ),
    multimodal: bool = typer.Option(
        False,
        "--multimodal",
        help="Run the multimodal agent",
    ),
):

    from source.server.audio_startup import enabled, run as run_audio_startup

    if enabled():
        if server not in (None, "light") or client not in (None, "light-python") or profiles or multimodal:
            raise typer.BadParameter("Audio startup check supports only the local Light server and client")
        return run_audio_startup(server_port, debug)

    threads = []

    if server_host not in ("127.0.0.1", "localhost"):
        print("FRIDAY server bind is restricted to loopback (127.0.0.1).")
    server_host = "127.0.0.1"
    url = f"http://{server_host}:{server_port}"

    if not server:
        # Single-instance guard: when FRIDAY is already running (the hidden
        # tray boot), a second desktop launch must NOT start another instance
        # (uvicorn bind fails 10048). The tray instance needs ~10-20s to load
        # its voice brain and raise /ping, so when the PC JUST booted also
        # grant the booting instance a grace window before declaring nobody
        # home; otherwise (desktop click, still off) FRIDAY boots normally.
        import urllib.request as _ur

        def _friday_already_up():
            try:
                _ur.urlopen(f"http://localhost:{server_port}/ping", timeout=2)
                return True
            except Exception:
                return False

        already = False
        for _probe in range(10):
            if _friday_already_up():
                already = True
                break
            if background:
                time.sleep(2.0)  # the tray instance may still be loading
            else:
                break
        if already:
            print("FRIDAY is already running - raising the app window.")
            try:
                _ur.urlopen(f"http://localhost:{server_port}/open_app", timeout=3)
            except Exception:
                pass
            time.sleep(0.5)
            os._exit(0)

    if not server:
        # Default desktop boot AND client-only auto-start both need the
        # light server; --status-ui alone also implies the default client.
        server = "light"
        if not client:
            client = "light-python"

    profiles_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "source", "server", "profiles")

    if profiles:
        if platform.system() == "Windows":
            subprocess.Popen(['explorer', profiles_dir])
        elif platform.system() == "Darwin":
            subprocess.Popen(['open', profiles_dir])
        elif platform.system() == "Linux":
            subprocess.Popen(['xdg-open', profiles_dir])
        else:
            subprocess.Popen(['open', profiles_dir])
        exit(0)

    from source.server.catalog_startup import CONFIG_ERROR_EXIT, CatalogStartupError, prepare

    try:
        installed_catalog = prepare()
        startup_identity = installed_catalog.attach() if installed_catalog is not None else None
    except CatalogStartupError as error:
        print(f"FRIDAY catalog startup: {error}", file=sys.stderr, flush=True)
        raise typer.Exit(CONFIG_ERROR_EXIT) from None

    if installed_catalog is not None:
        from source.server import lab_state

        lab_state.select_installed_world(installed_catalog.world)
        os.environ.update(FRIDAY_IDENTITY_INTEGRATION="1", FRIDAY_IDENTITY_READER="1")

    # Cloud clients must inherit Windows trust before the active profile loads.
    from source.server.tls_trust import bootstrap_windows_tls
    bootstrap_windows_tls()
    from source.server.server import start_server
    from source.server.status_ui import StatusBus, STATUS_THINKING, attach_log_capture, run_status_window, start_status_window

    if profile:
        if not os.path.isfile(profile):
            profile = os.path.join(profiles_dir, profile)
            if not os.path.isfile(profile):
                profile += ".py"
                if not os.path.isfile(profile):
                    print(f"Invalid profile path: {profile}")
                    exit(1)

    spec = importlib.util.spec_from_file_location("profile", profile)
    profile_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile_module)

    interpreter = profile_module.interpreter
    if startup_identity is not None:
        interpreter.friday_startup_identity = startup_identity

    status_bus = None
    if status_ui:
        status_bus = StatusBus(STATUS_THINKING, "Starting FRIDAY services")
        attach_log_capture(status_bus)

    if server or client:
        warmup_services(interpreter, debug)

    if not server_url:
        server_url = f"{server_host}:{server_port}"

    if server:

        if server == "light":
            light_server_port = server_port
            light_server_host = server_host
            voice = True
        elif server == "livekit":
            if shutil.which("livekit-server") is None:
                print(
                    "The `livekit-server` executable was not found on PATH. FRIDAY "
                    "needs it for `--server livekit` (it is NOT installed by pip). "
                    "Download it from https://github.com/livekit/livekit/releases "
                    "(e.g. livekit_1.13.7_windows_amd64.zip on Windows), extract it, "
                    "and put it on PATH. Or start FRIDAY with `--server light`."
                )
                exit(1)
            print(f"Starting light server (required for livekit server) on localhost, on the port before `--server-port` (port {server_port-1}), unless the `AN_OPEN_PORT` env var is set.")
            print(f"The livekit server will be started on port {server_port}.")
            light_server_port = os.getenv('AN_OPEN_PORT', server_port-1)
            light_server_host = server_host
            voice = False

        server_thread = threading.Thread(
            target=start_server,
            args=(
                light_server_host,
                light_server_port,
                interpreter,
                voice,
                debug,
                status_bus,
            ),
        )
        print("Starting server...")
        server_thread.start()
        threads.append(server_thread)

        if server == "livekit":

            def run_command(command):
                subprocess.run(command, shell=True, check=True)

            if debug:
                command = f'livekit-server --dev --bind "{server_host}" --port {server_port}'
            else:
                null_target = "> NUL 2>&1" if os.name == "nt" else "> /dev/null 2>&1"
                command = f'livekit-server --dev --bind "{server_host}" --port {server_port} {null_target}'
            livekit_thread = threading.Thread(
                target=run_command, args=(command,)
            )
            time.sleep(7)
            livekit_thread.start()
            threads.append(livekit_thread)

            local_livekit_url = f"ws://{server_host}:{server_port}"

        if server == "livekit":
            print("Livekit server will run at:", url)

    if client:
        
        module = importlib.import_module(
            f".clients.{client}.client", package="source"
        )

        client_thread = threading.Thread(target=module.run, args=[server_url, debug])
        print("Starting client...")
        client_thread.start()
        threads.append(client_thread)

    if expose:
        if server == "light":
            def light_listener_ready():
                # A different service may already own the selected port. Gate
                # credential forwarding on this interpreter's actual listener.
                local = interpreter.server.uvicorn_server
                return (server_thread.is_alive() and local.started and not local.should_exit
                        and local.config.host == light_server_host
                        and local.config.port == light_server_port)

            try:
                from source.server.public_ingress import start_public_ingress

                start_public_ingress(light_server_port, domain, upstream_ready=light_listener_ready)
            except Exception as error:
                print(f"Public n8n ingress unavailable: startup error ({type(error).__name__}).")
        else:
            print("Public ingress is available only for the Light server's n8n webhook. "
                  "LiveKit and its QR address are local.")

    def signal_handler(sig, frame):
        print("Termination signal received. Shutting down...")
        from source.server.light_lifecycle import cleanup_light
        cleanup_light(interpreter)
        # Graceful exit ONLY. Root-caused 2026-09-28: this handler previously
        # ran `taskkill /PID <self> /T /F` whenever any thread was alive, which
        # fire-bombed FRIDAY's ENTIRE process tree (supervising cmd too) with
        # zero traceback, exit -1 and no OS events -- the "mystery killer" that
        # deleted FRIDAY repeatedly and silently. Signals CAN arrive spuriously
        # during boot, so we just exit; the supervised launcher restarts her.
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:

        # Show the HUD IMMEDIATELY (or keep it hidden for tray auto-start):
        # the window must appear while the brain still loads, not 10-20s later.
        def readiness_poll():
            ping_url = f"http://{light_server_host}:{light_server_port}/ping"
            with requests.Session() as session:
                session.trust_env = False
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    try:
                        response = session.get(ping_url, timeout=2, allow_redirects=False)
                        if response.status_code == 200 and response.text.strip() == "pong":
                            return
                    except requests.RequestException:
                        pass
                    time.sleep(1)
            raise RuntimeError("Local FRIDAY server did not become ready within 120 seconds")

        if status_ui:
            if server == "light":
                # pywebview must own the process main thread on Windows; the
                # readiness poll moves to a background thread so the window
                # opens instantly while the brain loads behind it. With
                # --background the window is created HIDDEN (tray auto-start)
                # and 'Open App' from the tray shows it on the same thread.
                from source.server.light_lifecycle import run_hud

                def report_listener_failure(reason):
                    print(f"[light-lifecycle] {reason}", flush=True)

                run_hud(interpreter, server_thread, light_server_host, light_server_port,
                        lambda: run_status_window(status_bus, interpreter, start_hidden=background),
                        report_listener_failure)
                return
            else:
                start_status_window(status_bus, interpreter)

        readiness_poll()

        if qr:
            import segno

            def display_qr_code():
                time.sleep(10)
                content = json.dumps({"livekit_server": url})
                qr_code = segno.make(content)
                qr_code.terminal(compact=True)

            qr_thread = threading.Thread(target=display_qr_code)
            qr_thread.start()
            threads.append(qr_thread)

        if server == "livekit":
            from livekit import api
            from source.server.livekit.worker import main as worker_main
            from source.server.livekit.multimodal import main as multimodal_main

            time.sleep(1)
            os.environ['INTERPRETER_SERVER_HOST'] = light_server_host
            os.environ['INTERPRETER_SERVER_PORT'] = str(light_server_port)
            os.environ['01_TTS'] = interpreter.tts
            os.environ['01_STT'] = interpreter.stt

            token = str(api.AccessToken('devkey', 'secret') \
                .with_identity("identity") \
                .with_name("my name") \
                .with_grants(api.VideoGrants(
                    room_join=True,
                    room="my-room",
            )).to_jwt())

            meet_url = f'https://meet.livekit.io/custom?liveKitUrl={url.replace("http", "ws")}&token={token}\n\n'
            print("\n")
            print("For debugging, you can join a video call with your assistant. Click the link below, then send a chat message that says {CONTEXT_MODE_OFF}, then begin speaking:")
            print(meet_url)

            for attempt in range(30):
                try:
                    if multimodal:
                        multimodal_main(local_livekit_url)
                    else:
                        worker_main(local_livekit_url)
                except KeyboardInterrupt:
                    print("Exiting.")
                    raise
                except Exception as e:
                    print(f"Error occurred: {e}")
                print("Retrying...")
                time.sleep(1)

        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        os.kill(os.getpid(), signal.SIGINT)

if __name__ == "__main__":
    app()
