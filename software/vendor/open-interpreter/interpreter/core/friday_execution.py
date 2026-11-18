"""Opt-in invocation hooks for FRIDAY's admitted Light turns."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import sys
import weakref


class ExecutionDenied(RuntimeError):
    """The tool lost its execution authority before invocation."""


@dataclass(eq=False)
class _Worker:
    gate: object
    owner: object
    attempt: object
    previous_code: list
    stopped: bool = False
    finished: bool = False
    approval: tuple | None = None


@dataclass(frozen=True)
class _Permit:
    worker: _Worker
    automatic: bool

    def check(self):
        self.worker.gate.check(self.worker)
        if self.automatic and not self.worker.gate.interpreter.auto_run:
            raise ExecutionDenied("Automatic execution is disabled")


_worker = ContextVar("friday_execution_worker", default=None)
_permit = ContextVar("friday_execution_permit", default=None)
_gates = weakref.WeakKeyDictionary()


def gate_for(interpreter):
    try:
        reference = _gates.get(interpreter)
    except TypeError:
        return None
    return reference() if reference is not None else None


class _StopEvent:
    """Linearize the existing Stop signal with a short executor submission."""

    def __init__(self, event, gate):
        self.event = event
        self.gate = gate

    def set(self):
        with self.gate.lock:
            if self.gate.bound is not None:
                self.gate.bound.stopped = True
            self.event.set()

    def clear(self):
        with self.gate.lock:
            self.event.clear()

    def is_set(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        return self.event.wait(timeout)


class ExecutionGate:
    def __init__(self, interpreter, state, lock, limit):
        self.interpreter = interpreter
        self.state = state
        self.lock = lock
        self.limit = limit
        self.bound = None
        self.budget_owner = None
        self.used = 0
        interpreter.stop_event = _StopEvent(interpreter.stop_event, self)
        _gates[interpreter] = weakref.ref(self)

    def bind_response(self, respond):
        """Snapshot the producer before the worker is scheduled."""
        with self.lock:
            worker = _Worker(self, self.state.get("turn_ownership"),
                             self.state.get("turn_attempt"),
                             [row for row in self.interpreter.messages if row.get("type") == "code"])
            self.bound = worker

        def run(*args, **kwargs):
            token = _worker.set(worker)
            permit_token = _permit.set(None)
            try:
                return respond(*args, **kwargs)
            finally:
                with self.lock:
                    worker.finished = True
                _permit.reset(permit_token)
                _worker.reset(token)

        return run

    def output_attempt(self):
        worker = _worker.get()
        return worker.attempt if worker is not None and worker.gate is self else None

    def owns(self, worker):
        return (worker is not None and worker.gate is self and worker is self.bound
                and not worker.finished and worker.attempt is not None
                and worker.attempt is self.state.get("turn_attempt")
                and worker.owner is self.state.get("turn_ownership"))

    def check(self, worker):
        if (not self.owns(worker) or worker.owner is self.state.get("cancelled_owner")
                or worker.stopped or self.interpreter.stop_event.is_set()
                or self.state.get("tool_cap_terminated")):
            raise ExecutionDenied("Turn no longer owns tool execution")

    def grant(self, language, code, row):
        with self.lock:
            worker = _worker.get()
            self.check(worker)
            if any(row is earlier for earlier in worker.previous_code):
                raise ExecutionDenied("Buffered code belongs to a different tool submission")
            explicit = worker.approval == (language, code)
            worker.approval = None
            automatic = not explicit
            if automatic and not self.interpreter.auto_run:
                raise ExecutionDenied("Tool execution needs confirmation")
            if self.budget_owner is not worker.owner:
                self.budget_owner, self.used = worker.owner, 0
            if self.used >= self.limit:
                worker.stopped = True
                raise ExecutionDenied("Turn tool limit reached")
            worker.previous_code.append(row)
            self.used += 1
            return _Permit(worker, automatic)


def bind_response(interpreter, respond):
    gate = gate_for(interpreter)
    return gate.bind_response(respond) if gate is not None else respond


def approve_confirmation(interpreter, chunk):
    gate = gate_for(interpreter)
    if gate is not None:
        with gate.lock:
            worker = _worker.get()
            gate.check(worker)
            worker.approval = (chunk["content"]["format"], chunk["content"]["content"])


@contextmanager
def execution_scope(permit):
    token = _permit.set(permit)
    try:
        yield
    finally:
        _permit.reset(token)


def approved_tool(interpreter, language, code, row):
    gate = gate_for(interpreter)
    return execution_scope(gate.grant(language, code, row) if gate is not None else None)


def profile_render(interpreter):
    """Permit locally configured prompt setup without spending a model tool slot."""
    gate = gate_for(interpreter)
    if gate is None:
        return execution_scope(None)
    with gate.lock:
        worker = _worker.get()
        gate.check(worker)
    return execution_scope(_Permit(worker, automatic=False))


def bind_invocation(executor):
    """Retain the original permit for callbacks on a different thread."""
    permit = _permit.get()

    def run(*args, **kwargs):
        with execution_scope(permit):
            return invoke(executor, *args, **kwargs)

    return run


def require_permit(interpreter):
    gate = gate_for(interpreter)
    permit = _permit.get()
    if gate is not None:
        with gate.lock:
            if permit is None or permit.worker.gate is not gate:
                raise ExecutionDenied("Tool execution requires an admitted turn")
            permit.check()
    return permit


def guarded_chunks(chunks, permit):
    if permit is None:
        yield from chunks
        return
    try:
        while True:
            with execution_scope(permit):
                try:
                    chunk = next(chunks)
                except StopIteration:
                    return
            yield chunk
    finally:
        with execution_scope(permit):
            chunks.close()


def check_current():
    permit = _permit.get()
    if permit is not None:
        with permit.worker.gate.lock:
            permit.check()


def cleanup_current(interpreter, cleanup):
    gate = gate_for(interpreter)
    if gate is None:
        return cleanup()
    permit = _permit.get()
    with gate.lock:
        # A cancelled current worker may still stop its running tool.
        if permit is not None and gate.owns(permit.worker):
            return cleanup()


def invoke(executor, *args, **kwargs):
    """Check and submit as one operation. Output collection stays outside."""
    permit = _permit.get()
    if permit is None:
        return executor(*args, **kwargs)
    with permit.worker.gate.lock:
        permit.check()
        return executor(*args, **kwargs)


def _check_external_launch(event, _args):
    # Optional language adapters may delegate spawning to a library.
    if event == "subprocess.Popen":
        check_current()


def validate_runtime():
    """An older fallback installation may have only some of the hooks."""
    from .async_core import AsyncInterpreter
    from .computer.terminal.languages.java import Java
    from .computer.terminal.languages.jupyter_language import JupyterLanguage
    from .computer.terminal.languages.subprocess_language import SubprocessLanguage
    from .computer.terminal.terminal import Terminal
    from .respond import respond

    hooks = (
        (AsyncInterpreter.input, "bind_response"),
        (AsyncInterpreter.respond, "approve_confirmation"),
        (respond, "approved_tool"),
        (respond, "profile_render"),
        (Terminal.run, "guarded_chunks"),
        (Terminal._streaming_run, "require_permit"),
        (JupyterLanguage.__init__, "invoke"),
        (JupyterLanguage._execute_code, "invoke"),
        (SubprocessLanguage.start_process, "invoke"),
        (SubprocessLanguage.run, "invoke"),
        (Java.run, "invoke"),
    )
    return all(name in function.__code__.co_names or name in function.__code__.co_consts
               for function, name in hooks)


sys.addaudithook(_check_external_launch)
