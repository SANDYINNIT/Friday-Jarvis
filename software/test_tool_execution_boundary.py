"""Real Open Interpreter input, confirmation and executor paths, with fake I/O."""

import asyncio
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import queue
import runpy
from threading import RLock
from types import SimpleNamespace

import pytest

from interpreter.core import async_core
from interpreter.core.computer.terminal.languages import jupyter_language, subprocess_language
from interpreter.core.friday_execution import (
    ExecutionDenied, ExecutionGate, bind_invocation, validate_runtime,
)


class DeferredWorker:
    def __init__(self, target, args):
        self.target = target
        self.args = args

    def start(self):
        pass

    def is_alive(self):
        return False

    def run(self):
        self.target(*self.args)


class ListenerStub:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        pass

    def is_alive(self):
        return False


@dataclass(frozen=True)
class Owner:
    name: str


class Model:
    supports_vision = False

    def __init__(self):
        self.tools = deque()

    def run(self, messages):
        if self.tools:
            language, code = self.tools.popleft()
            yield {"type": "code", "format": language, "content": code}
        else:
            yield {"type": "message", "content": "Done."}


class Fixture:
    def __init__(self, monkeypatch, *, limit=24):
        assert validate_runtime()
        self.interpreter = interpreter = async_core.AsyncInterpreter()
        interpreter.computer.import_computer_api = False
        interpreter.computer.import_skills = False
        interpreter.sync_computer = False
        interpreter.system_message = "fixture"
        interpreter.custom_instructions = ""
        interpreter.loop = False
        interpreter.auto_run = True
        interpreter.llm = Model()
        self.frames = []
        interpreter.output_queue = SimpleNamespace(sync_q=SimpleNamespace(put=self.frames.append))
        self.state = {}
        self.gate = ExecutionGate(interpreter, self.state, RLock(), limit)
        monkeypatch.setattr(async_core, "threading", SimpleNamespace(Thread=DeferredWorker))
        monkeypatch.setattr(jupyter_language, "threading", SimpleNamespace(Thread=ListenerStub))
        self.calls = []
        self.kernel = kernel = jupyter_language.JupyterLanguage.__new__(jupyter_language.JupyterLanguage)
        kernel.computer = interpreter.computer
        kernel.kc = SimpleNamespace(is_alive=lambda: True, execute=self.record)
        kernel.listener_thread = None
        kernel.preprocess_code = lambda code: code
        kernel._capture_output = lambda _: iter([
            {"type": "console", "format": "output", "content": "fixture output"},
        ])
        interpreter.computer.terminal._active_languages["python"] = kernel

    def record(self, code):
        self.calls.append((self.state["turn_ownership"], self.state["turn_attempt"], code))

    def schedule(self, owner, tools=(), *, approved=False, buffered=None):
        self.state.update(turn_ownership=owner, turn_attempt=object(), cancelled_owner=None,
                          tool_cap_terminated=False)
        i = self.interpreter
        i.messages = [{"role": "user", "type": "message", "content": "fixture request"}]
        if buffered is not None:
            i.messages.append({"role": "assistant", "type": "code", "format": "python",
                               "content": buffered})
        if approved:
            i.messages.append({"role": "user", "type": "command", "content": "go"})
        i.llm.tools = deque(tools)
        asyncio.run(i.input({"role": "user", "type": "message", "end": True}))
        return i.respond_thread, self.state["turn_attempt"]


def test_confirmation_stop_and_explicitly_authorized_recovery(monkeypatch):
    fixture = Fixture(monkeypatch)
    interpreter = fixture.interpreter
    interpreter.auto_run = False
    approve = async_core.approve_confirmation
    approvals = []

    def approve_then_stop(i, chunk):
        approve(i, chunk)
        approvals.append(chunk["content"]["content"])
        i.stop_event.set()
        i.stop_event.clear()  # Clearing the shared event must not re-arm this worker.

    monkeypatch.setattr(async_core, "approve_confirmation", approve_then_stop)
    worker, _ = fixture.schedule(Owner("stopped"), [("python", "stopped")], approved=True)
    worker.run()
    assert fixture.calls == []
    assert approvals == ["stopped"]

    monkeypatch.setattr(async_core, "approve_confirmation", approve)
    worker, _ = fixture.schedule(Owner("unapproved"), [("python", "unapproved")])
    worker.run()
    assert fixture.calls == []
    assert interpreter.messages[-1]["content"] == "unapproved"

    owner = Owner("approved next")
    worker, attempt = fixture.schedule(owner, [("python", "approved")], approved=True)
    worker.run()
    assert fixture.calls == [(owner, attempt, "approved")]


@pytest.mark.parametrize("revocation", ["stop", "authorization", "hard-cap"])
def test_revalidate_after_kernel_preparation(monkeypatch, revocation):
    fixture = Fixture(monkeypatch)
    i = fixture.interpreter

    def preprocess(code):
        if revocation == "stop":
            i.stop_event.set()
        elif revocation == "authorization":
            i.auto_run = False
        else:
            fixture.state["tool_cap_terminated"] = True
        return code

    fixture.kernel.preprocess_code = preprocess
    worker, _ = fixture.schedule(Owner("revoked"), [("python", "must not submit")])
    worker.run()
    assert fixture.calls == []
    assert fixture.kernel.listener_thread is None

    fixture.kernel.preprocess_code = lambda code: code
    i.auto_run = True
    owner = Owner("new")
    worker, attempt = fixture.schedule(owner, [("python", "new work")])
    worker.run()
    assert fixture.calls == [(owner, attempt, "new work")]


def test_buffered_code_from_another_generation_is_denied(monkeypatch):
    fixture = Fixture(monkeypatch)
    first = Owner("first")
    worker, _ = fixture.schedule(first, [("python", "would be stale")])
    fixture.interpreter.stop_event.set()
    next_owner = Owner("next")
    new_worker, attempt = fixture.schedule(next_owner, [("python", "next")])
    worker.run()  # The old scheduled callback starts after the next turn exists.
    assert fixture.calls == []
    new_worker.run()
    assert fixture.calls == [(next_owner, attempt, "next")]

    # A stale code row still present at worker launch cannot be approved afresh.
    owner = Owner("after buffered")
    worker, _ = fixture.schedule(owner, buffered="buffered old row")
    worker.run()
    assert fixture.calls == [(next_owner, attempt, "next")]


def test_confirmation_does_not_authorize_replaced_code(monkeypatch):
    fixture = Fixture(monkeypatch)
    fixture.interpreter.auto_run = False
    approve = async_core.approve_confirmation

    def swap_code(i, chunk):
        approve(i, chunk)
        i.messages[-1]["content"] = "different buffered code"

    monkeypatch.setattr(async_core, "approve_confirmation", swap_code)
    worker, _ = fixture.schedule(Owner("manual"), [("python", "approved text")], approved=True)
    worker.run()
    assert fixture.calls == []


def test_delayed_terminal_stream_keeps_the_old_generation(monkeypatch):
    fixture = Fixture(monkeypatch)
    interpreter = fixture.interpreter
    delayed = []

    def record_shell(code):
        fixture.record(code)
        yield {"type": "console", "format": "output", "content": "shell result"}

    interpreter.computer.terminal._active_languages["shell"] = SimpleNamespace(run=record_shell)

    def buffer_shell(code):
        delayed.append(interpreter.computer.run("shell", "stale shell code", stream=True))
        return code

    first = Owner("first")
    fixture.kernel.preprocess_code = buffer_shell
    worker, first_attempt = fixture.schedule(first, [("python", "first tool")])
    worker.run()
    assert fixture.calls == [(first, first_attempt, "first tool")]

    def consume_stale_stream(code):
        with pytest.raises(ExecutionDenied):
            next(delayed[0])
        return code

    second = Owner("second")
    fixture.kernel.preprocess_code = consume_stale_stream
    worker, second_attempt = fixture.schedule(second, [("python", "second tool")])
    worker.run()
    assert fixture.calls == [(first, first_attempt, "first tool"),
                             (second, second_attempt, "second tool")]


def test_retry_generation_cannot_use_stale_callbacks_or_reset_cap(monkeypatch):
    fixture = Fixture(monkeypatch, limit=1)
    delayed = []

    def preprocess(code):
        delayed.append(bind_invocation(fixture.kernel.kc.execute))
        return code

    fixture.kernel.preprocess_code = preprocess
    owner = Owner("retry owner")
    worker, attempt = fixture.schedule(owner, [("python", "allowed"), ("python", "over cap")])
    worker.run()
    assert fixture.calls == [(owner, attempt, "allowed")]
    retry, retry_attempt = fixture.schedule(owner, [("python", "retry over cap")])
    assert retry_attempt is not attempt
    with pytest.raises(ExecutionDenied):
        delayed[0]("stale delayed callback")
    retry.run()
    assert fixture.calls == [(owner, attempt, "allowed")]
    next_owner = Owner("new budget")
    worker, new_attempt = fixture.schedule(next_owner, [("python", "works again")])
    worker.run()
    assert fixture.calls == [(owner, attempt, "allowed"), (next_owner, new_attempt, "works again")]


def test_persistent_shell_rechecks_at_stdin_and_denies_unowned_run(monkeypatch):
    fixture = Fixture(monkeypatch)
    interpreter = fixture.interpreter
    shell = subprocess_language.SubprocessLanguage()
    shell.process = SimpleNamespace(stdin=SimpleNamespace(
        write=fixture.record, flush=shell.done.set,
    ))

    def empty_queue(**kwargs):
        raise queue.Empty

    shell.output_queue = SimpleNamespace(empty=lambda: True, get=empty_queue)
    monkeypatch.setattr(subprocess_language, "time", SimpleNamespace(sleep=lambda _: None))
    interpreter.computer.terminal._active_languages["shell"] = shell

    with pytest.raises(ExecutionDenied):
        interpreter.computer.run("shell", "outside a turn")

    def stop_during_preprocess(code):
        interpreter.stop_event.set()
        return code

    shell.preprocess_code = stop_during_preprocess
    worker, _ = fixture.schedule(Owner("stopped shell"), [("shell", "blocked")])
    worker.run()
    assert fixture.calls == []
    shell.preprocess_code = lambda code: code
    owner = Owner("fresh shell")
    worker, attempt = fixture.schedule(owner, [("shell", "allowed")])
    worker.run()
    assert fixture.calls == [(owner, attempt, "allowed\n")]


def test_pending_subprocess_launch_is_denied_and_fresh_launch_recovers(monkeypatch):
    fixture = Fixture(monkeypatch)
    interpreter = fixture.interpreter
    shell = subprocess_language.SubprocessLanguage()
    shell.start_cmd = ["synthetic-shell"]

    def launch(argv, **kwargs):
        fixture.record(tuple(argv))
        return SimpleNamespace(stdin=SimpleNamespace(write=fixture.record, flush=shell.done.set),
                               stdout=object(), stderr=object())

    def empty_queue(**kwargs):
        raise queue.Empty

    shell.output_queue = SimpleNamespace(empty=lambda: True, get=empty_queue)
    monkeypatch.setattr(subprocess_language, "subprocess", SimpleNamespace(Popen=launch, PIPE=-1))
    monkeypatch.setattr(subprocess_language, "threading", SimpleNamespace(Thread=ListenerStub))
    monkeypatch.setattr(subprocess_language, "time", SimpleNamespace(sleep=lambda _: None))
    interpreter.computer.terminal._active_languages["shell"] = shell

    def stop(code):
        interpreter.stop_event.set()
        return code

    shell.preprocess_code = stop
    worker, _ = fixture.schedule(Owner("old"), [("shell", "no launch")])
    worker.run()
    assert fixture.calls == []
    shell.preprocess_code = lambda code: code
    owner = Owner("new")
    worker, attempt = fixture.schedule(owner, [("shell", "fresh")])
    worker.run()
    assert fixture.calls == [(owner, attempt, ("synthetic-shell",)), (owner, attempt, "fresh\n")]


def test_trusted_profile_render_is_scoped_and_does_not_spend_tool_budget(monkeypatch):
    fixture = Fixture(monkeypatch, limit=1)
    fixture.interpreter.system_message = "{{profile setup}}"
    owner = Owner("with profile setup")
    worker, attempt = fixture.schedule(owner, [("python", "one tool")])
    worker.run()
    assert fixture.calls[:2] == [(owner, attempt, "profile setup"), (owner, attempt, "one tool")]
    assert fixture.gate.used == 1
    assert all(call[:2] == (owner, attempt) for call in fixture.calls)


def test_wheel_matches_vendored_sources_and_record():
    root = Path(__file__).resolve().parent
    tooling = runpy.run_path(str(root / "tmp-oi" / "rebuild_friday_execution_wheel.py"))
    tooling["verify"]()
