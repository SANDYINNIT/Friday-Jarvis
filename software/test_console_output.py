"""Focused test for normal versus diagnostic Open Interpreter output."""

from types import SimpleNamespace

from source.server.server import configure_console_output


interpreter = SimpleNamespace(server=SimpleNamespace(), verbose=None, print=None)
configure_console_output(interpreter, False)
assert interpreter.verbose is False
assert interpreter.server.display is False
assert interpreter.print is False

configure_console_output(interpreter, True)
assert interpreter.verbose is True
assert interpreter.server.display is True
assert interpreter.print is True

print("PASS: generated/tool output is debug-only")
