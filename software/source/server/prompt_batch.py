"""Render trusted profile blocks in one existing kernel round trip.

No model call, subprocess runner or added tool authority. The same blocks that
Open Interpreter already executes run in order in its existing kernel namespace.
Malformed output fails rather than executing the blocks a second time.
"""
import json
import re
from uuid import uuid4

MAX_RENDER_BYTES = 256_000


def render(interpreter, message):
    parts = re.split(r"({{.*?}})", message, flags=re.DOTALL)
    positions = [i for i, p in enumerate(parts) if p.startswith("{{") and p.endswith("}}")]
    if not positions:
        return message.strip()
    blocks = [parts[i][2:-2].strip() for i in positions]
    marker = "FRIDAY_RENDER_" + uuid4().hex + ":"
    # Reserved names avoid consuming the profile's ordinary globals. exec uses
    # the existing shared namespace, matching sequential computer.run calls.
    program = (
        "import contextlib as _friday_render_context, io as _friday_render_io, json as _friday_render_json\n"
        "_friday_render_results = []\n"
        f"for _friday_render_code in {blocks!r}:\n"
        "    _friday_render_capture = _friday_render_io.StringIO()\n"
        "    try:\n"
        "        with _friday_render_context.redirect_stdout(_friday_render_capture):\n"
        "            exec(_friday_render_code, globals())\n"
        "    except Exception:\n"
        "        pass\n"
        "    _friday_render_results.append(_friday_render_capture.getvalue())\n"
        f"print({marker!r} + _friday_render_json.dumps(_friday_render_results))\n"
    )
    computer = interpreter.computer
    previous = computer.save_skills
    try:
        computer.save_skills = False
        output = computer.run("python", program, display=interpreter.verbose)
        text = "".join(row.get("content", "") for row in output
                       if row.get("format") == "output" and isinstance(row.get("content"), str))
        if len(text.encode("utf-8")) > MAX_RENDER_BYTES:
            raise ValueError("Profile render exceeds its bound")
        matches = [line[len(marker):] for line in text.splitlines() if line.startswith(marker)]
        if len(matches) != 1:
            raise ValueError("Profile render did not return a complete frame")
        rendered = json.loads(matches[0])
        if not isinstance(rendered, list) or len(rendered) != len(blocks) or not all(isinstance(s, str) for s in rendered):
            raise ValueError("Invalid profile render frame")
        for i, value in zip(positions, rendered):
            # OI's kernel traces executed source lines through stdout. The
            # ordinary renderer receives these as separate active_line frames;
            # redirected nested exec captures them inside our JSON instead.
            value = re.sub(r"##active_line\d+##\n?", "", value)
            # Preserve the original renderer's special output filtering.
            parts[i] = value if "IGNORE_ALL_ABOVE_THIS_LINE" not in value else ""
        return "".join(parts).strip()
    finally:
        computer.save_skills = previous
