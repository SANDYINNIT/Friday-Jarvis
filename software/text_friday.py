"""Text-command bridge to a RUNNING FRIDAY - talk to her without voice.

POSTs the message to FRIDAY's /chat endpoint (server.py), which runs the same
dispatch_text chain as the HUD chat input and Telegram (command router, memory,
screen handling, brain with failover/retries). The final answer is delivered
back as the HTTP response, so the reply is exactly what FRIDAY would have
spoken - minus the voice.

Usage:
    python text_friday.py "your message"          # single shot
    echo "your message" | python text_friday.py    # from stdin
    python text_friday.py --chat                   # interactive REPL
    python text_friday.py --port 10102 "message"   # against another FRIDAY

Exit codes: 0 = got a reply, 1 = FRIDAY rejected it, 2 = transport error,
3 = could not connect, 4 = no reply within timeout.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_PORT = 10101


def chat(url, text, timeout):
    body = text.encode("utf-8")
    req = urllib.request.Request(
        url + "/chat",
        data=body,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip()
            return resp.status, data
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (e.read().decode("utf-8", errors="replace").strip() or "")
        except Exception:
            pass
        return e.code, detail
    except urllib.error.URLError:
        return 3, "FRIDAY is not reachable at this port (is main.py up?)"
    except (TimeoutError, urllib.error.ContentTooShortError):
        return 4, "(no reply within timeout)"


def main():
    ap = argparse.ArgumentParser(description="Text-command FRIDAY (no voice).")
    ap.add_argument("message", nargs="?", help="single message; omit for --chat or stdin")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"FRIDAY port (default {DEFAULT_PORT})")
    ap.add_argument("--host", default="localhost", help="FRIDAY host (default localhost)")
    ap.add_argument("--chat", action="store_true", help="interactive REPL")
    ap.add_argument("--timeout", type=int, default=240, help="seconds to wait for a reply (default 240; matches server FRIDAY_TEXT_TIMEOUT)")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"

    if args.chat:
        lines = []
        if not sys.stdin.isatty():
            lines = [l.strip() for l in sys.stdin if l.strip()]
        while True:
            try:
                if lines:
                    line = lines.pop(0)
                else:
                    line = input("you> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            code, reply = chat(url, line, args.timeout)
            print(f"friday> {reply}" if not reply.startswith("(") else f"[{code}] {reply}")
        return

    if args.message:
        req_text = args.message
    elif not sys.stdin.isatty():
        req_text = " ".join(l.strip() for l in sys.stdin if l.strip())
    else:
        ap.error("no message given")

    code, reply = chat(url, req_text, args.timeout)
    if code == 200:
        print(reply)
        sys.exit(0)
    if code in (1, 2, 3, 4):
        # transport-level sentinel codes from chat()
        print(f"friday> {reply}", file=sys.stderr)
        sys.exit(code)
    print(f"[HTTP {code}] {reply}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()