import typer
import ngrok
import platform
import threading
import os
import importlib
from source.server.server import start_server
import subprocess
import webview
import socket
import json
import segno
from livekit import api
import time
from dotenv import load_dotenv
import signal
from source.server.livekit.worker import main as worker_main
from source.server.livekit.multimodal import main as multimodal_main
from source.server.status_ui import StatusBus, STATUS_THINKING, attach_log_capture, run_status_window, start_status_window
import warnings

# Windows registry auto-start boots with cwd=C:\WINDOWS\system32 — force the
# project root so RELATIVE file writes (RealtimeSTT's realtimesst.log, etc.)
# never hit a permission wall and crash the voice pipeline at boot.
os.chdir(os.path.dirname(os.path.realpath(__file__)))
import requests

load_dotenv()

system_type = platform.system()

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
        "0.0.0.0",
        "--server-host",
        help="Specify the server host where the server will deploy",
    ),
    server_port: int = typer.Option(
        10101,
        "--server-port",
        help="Specify the server port where the server will deploy",
    ),
    expose: bool = typer.Option(False, "--expose", help="Expose server over the internet"),
    domain: str = typer.Option(None, "--domain", help="Use `--expose` with a custom ngrok domain"),
    client: str = typer.Option(None, "--client", help="Run client of a particular type. Accepts `light-python`, defaults to `light-python`"),
    server_url: str = typer.Option(
        None,
        "--server-url",
        help="Specify the server URL that the --client should expect. Defaults to server-host and server-port",
    ),
    qr: bool = typer.Option(
        False, "--qr", help="Display QR code containing the server connection information (will be ngrok url if `--expose` is used)"
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

    threads = []

    # The readiness-poll URL must exist even when --client is passed without
    # --server (the registry auto-start uses that exact combination).
    url = f"http://{server_host if server_host != '0.0.0.0' else 'localhost'}:{server_port}"

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

    status_bus = None
    if status_ui:
        status_bus = StatusBus(STATUS_THINKING, "Starting FRIDAY services")
        attach_log_capture(status_bus)

    if server or client:
        warmup_services(interpreter, debug)

    if system_type == "Windows":
        server_host = "localhost"

    if not server_url:
        server_url = f"{server_host}:{server_port}"

    if server:

        if server == "light":
            light_server_port = server_port
            light_server_host = server_host
            voice = True
        elif server == "livekit":
            print(f"Starting light server (required for livekit server) on localhost, on the port before `--server-port` (port {server_port-1}), unless the `AN_OPEN_PORT` env var is set.")
            print(f"The livekit server will be started on port {server_port}.")
            light_server_port = os.getenv('AN_OPEN_PORT', server_port-1)
            light_server_host = "localhost"
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
                command = f'livekit-server --dev --bind "{server_host}" --port {server_port} > /dev/null 2>&1'
            livekit_thread = threading.Thread(
                target=run_command, args=(command,)
            )
            time.sleep(7)
            livekit_thread.start()
            threads.append(livekit_thread)

            local_livekit_url = f"ws://{server_host}:{server_port}"

        if expose:

            listener = ngrok.forward(f"{server_host}:{server_port}", authtoken_from_env=True, domain=domain)
            url = listener.url()

        else:

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_address = s.getsockname()[0]
            s.close()
            url = f"http://localhost:{server_port}"

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

    def signal_handler(sig, frame):
        print("Termination signal received. Shutting down...")
        stop_reminders = getattr(interpreter, "stop_reminders", None)
        if stop_reminders is not None:
            stop_reminders()
        for thread in threads:
            if thread.is_alive():
                subprocess.run(f"pkill -P {os.getpid()}", shell=True)
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:

        # Show the HUD IMMEDIATELY (or keep it hidden for tray auto-start):
        # the window must appear while the brain still loads, not 10-20s later.
        def readiness_poll():
            for attempt in range(120):
                try:
                    response = requests.get(url)
                    if response.status_code == 200:
                        return
                except requests.RequestException:
                    pass
                time.sleep(1)
            raise Exception(f"Server at {url} failed to respond after 120 attempts")

        if status_ui:
            if server == "light":
                # pywebview must own the process main thread on Windows; the
                # readiness poll moves to a background thread so the window
                # opens instantly while the brain loads behind it. With
                # --background the window is created HIDDEN (tray auto-start)
                # and 'Open App' from the tray shows it on the same thread.
                threading.Thread(target=readiness_poll, daemon=True, name="friday-readiness").start()
                run_status_window(status_bus, interpreter, start_hidden=background)
                return
            else:
                start_status_window(status_bus, interpreter)

        readiness_poll()

        if qr:
            def display_qr_code():
                time.sleep(10)
                content = json.dumps({"livekit_server": url})
                qr_code = segno.make(content)
                qr_code.terminal(compact=True)

            qr_thread = threading.Thread(target=display_qr_code)
            qr_thread.start()
            threads.append(qr_thread)

        if server == "livekit":
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
