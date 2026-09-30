"""Regression tests for preventing busy-state audio queue feedback."""

import importlib.util
from pathlib import Path


client_path = Path(__file__).parent / "source" / "clients" / "light-python" / "client.py"
spec = importlib.util.spec_from_file_location("light_client", client_path)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
can_listen_now = client.can_listen_now


assert not can_listen_now(False, True, False, 10.0, 0.0)
assert can_listen_now(False, True, True, 10.0, 0.0)
assert can_listen_now(False, False, False, 10.0, 0.0)
assert not can_listen_now(True, False, True, 10.0, 0.0)
print("PASS: client listens while speaking for barge-in, not while merely busy")
