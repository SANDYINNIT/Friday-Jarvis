"""Opt-in bounded coordination for independent delegation tasks.

This module is deliberately not imported by the voice server.  It runs the
existing synchronous delegation boundary in a bounded thread pool and returns
structured results; it does not merge answers with another model or emit UI,
voice, tool, or memory events.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time

from .delegation import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CONTEXT_CHARS,
    MAX_RESULT_CHARS,
    DelegationRequest,
    delegate,
)


DEFAULT_MAX_PARALLEL = 3
DEFAULT_GLOBAL_TIMEOUT = 120.0
DEFAULT_MAX_RESULTS = MAX_RESULT_CHARS


@dataclass(frozen=True)
class AgentTask:
    task_id: str
    target: str
    provider: str
    model: str
    task: str
    context: str = ""
    expected_output: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self):
        for field in ("task_id", "target", "provider", "model", "task", "context", "expected_output"):
            if not isinstance(getattr(self, field), str):
                raise TypeError(f"{field} must be a string")
        if not self.task_id.strip() or not self.target.strip():
            raise ValueError("task_id and target are required")
        if not self.provider.strip() or not self.model.strip() or not self.task.strip():
            raise ValueError("provider, model, and task are required")
        try:
            timeout = float(self.timeout)
        except (TypeError, ValueError) as error:
            raise ValueError("timeout must be a number") from error
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "target", self.target.strip())
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "task", self.task.strip())
        object.__setattr__(self, "context", self.context[:MAX_CONTEXT_CHARS])
        object.__setattr__(self, "expected_output", self.expected_output[:MAX_CONTEXT_CHARS])
        object.__setattr__(self, "timeout", timeout)


@dataclass(frozen=True)
class AgentResult:
    task_id: str
    target: str
    provider: str
    model: str
    success: bool
    text: str = ""
    error: str = ""
    latency: float = 0.0
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class CoordinatorResult:
    results: tuple
    aggregate: dict
    success: bool
    timed_out: bool = False
    cancelled: bool = False


def _failure(task, error, *, timed_out=False, cancelled=False, started=None):
    return AgentResult(
        task_id=task.task_id,
        target=task.target,
        provider=task.provider,
        model=task.model,
        success=False,
        error=str(error)[:MAX_RESULT_CHARS],
        latency=max(0.0, time.monotonic() - started) if started is not None else 0.0,
        timed_out=timed_out,
        cancelled=cancelled,
    )


class AgentCoordinator:
    """Run independent tasks with bounded concurrency and explicit shutdown."""

    def __init__(self, max_parallel=DEFAULT_MAX_PARALLEL, max_context_chars=MAX_CONTEXT_CHARS,
                 max_result_chars=DEFAULT_MAX_RESULTS):
        try:
            max_parallel = int(max_parallel)
            max_context_chars = int(max_context_chars)
            max_result_chars = int(max_result_chars)
        except (TypeError, ValueError) as error:
            raise ValueError("bounds must be integers") from error
        if max_parallel <= 0 or max_context_chars <= 0 or max_result_chars <= 0:
            raise ValueError("bounds must be greater than zero")
        self.max_parallel = max_parallel
        self.max_context_chars = max_context_chars
        self.max_result_chars = max_result_chars
        self._executor = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="friday-agent")
        self._closed = False
        self._running = set()

    async def run(self, tasks, global_timeout=DEFAULT_GLOBAL_TIMEOUT):
        if self._closed:
            raise RuntimeError("coordinator is shut down")
        tasks = tuple(tasks)
        if any(not isinstance(task, AgentTask) for task in tasks):
            raise TypeError("run() requires AgentTask values")
        ids = [task.task_id for task in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task IDs must be unique")
        try:
            global_timeout = float(global_timeout)
        except (TypeError, ValueError) as error:
            raise ValueError("global_timeout must be a number") from error
        if global_timeout <= 0:
            raise ValueError("global_timeout must be greater than zero")

        async def execute(task):
            started = time.monotonic()
            request = DelegationRequest(
                task.provider,
                task.model,
                task.task,
                task.context[:self.max_context_chars],
                task.expected_output[:self.max_context_chars],
                task.timeout,
            )
            future = asyncio.get_running_loop().run_in_executor(self._executor, delegate, request)
            self._running.add(future)
            try:
                delegation_result = await asyncio.wait_for(asyncio.shield(future), task.timeout)
                text = str(getattr(delegation_result, "text", ""))[:self.max_result_chars]
                error = str(getattr(delegation_result, "error", ""))[:self.max_result_chars]
                return AgentResult(task.task_id, task.target, task.provider, task.model,
                                   bool(getattr(delegation_result, "success", False)), text, error,
                                   max(0.0, time.monotonic() - started))
            except asyncio.TimeoutError:
                return _failure(task, "Agent task timed out"[:self.max_result_chars], timed_out=True, started=started)
            except asyncio.CancelledError:
                return _failure(task, "Agent task cancelled"[:self.max_result_chars], cancelled=True, started=started)
            except Exception as error:
                return _failure(task, f"Agent task failed: {error}"[:self.max_result_chars], started=started)
            finally:
                self._running.discard(future)

        async def bounded(task):
            async with semaphore:
                return await execute(task)

        semaphore = asyncio.Semaphore(self.max_parallel)
        workers = [asyncio.create_task(bounded(task)) for task in tasks]
        gathered = asyncio.gather(*workers)
        timed_out = False
        cancelled = False
        try:
            results = await asyncio.wait_for(gathered, global_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            try:
                results = await gathered
            except asyncio.CancelledError:
                results = await asyncio.gather(*workers, return_exceptions=True)
            results = [result if isinstance(result, AgentResult) else _failure(task, "Global coordinator timeout", timed_out=True)
                       for task, result in zip(tasks, results)]
            for index, result in enumerate(results):
                if result.cancelled:
                    results[index] = _failure(tasks[index], "Global coordinator timeout", timed_out=True)
                elif not result.success and not result.timed_out:
                    results[index] = _failure(tasks[index], "Global coordinator timeout", timed_out=True)
        except asyncio.CancelledError:
            cancelled = True
            for worker in workers:
                worker.cancel()
            try:
                await gathered
            except asyncio.CancelledError:
                await asyncio.gather(*workers, return_exceptions=True)
            raise
        return self._coordinator_result(results, timed_out=timed_out, cancelled=cancelled)

    def _coordinator_result(self, results, *, timed_out=False, cancelled=False):
        results = tuple(results)
        aggregate = {
            "task_count": len(results),
            "success_count": sum(result.success for result in results),
            "failure_count": sum(not result.success for result in results),
            "timeout_count": sum(result.timed_out for result in results),
            "cancelled_count": sum(result.cancelled for result in results),
            "task_ids": tuple(result.task_id for result in results),
        }
        return CoordinatorResult(results, aggregate, bool(results) and all(result.success for result in results),
                                 timed_out, cancelled)

    def shutdown(self, wait=False):
        """Stop accepting work; running blocking provider calls are not force-killed."""
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=wait, cancel_futures=True)

    async def aclose(self):
        self.shutdown(wait=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()


async def coordinate(tasks, max_parallel=DEFAULT_MAX_PARALLEL, global_timeout=DEFAULT_GLOBAL_TIMEOUT,
                     max_context_chars=MAX_CONTEXT_CHARS, max_result_chars=DEFAULT_MAX_RESULTS):
    """One-shot explicit API; the coordinator is always shut down afterwards."""
    coordinator = AgentCoordinator(max_parallel, max_context_chars, max_result_chars)
    try:
        return await coordinator.run(tasks, global_timeout)
    finally:
        coordinator.shutdown(wait=False)


def merge_text_results(results, max_chars=MAX_RESULT_CHARS):
    """Safely label and bound result text without making a model call."""
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError) as error:
        raise ValueError("max_chars must be an integer") from error
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    sections = []
    for result in results:
        text = str(getattr(result, "text", ""))
        if not text:
            continue
        label = f"[{getattr(result, 'task_id', 'unknown')} | {getattr(result, 'target', 'unknown')}]"
        sections.append(label + "\n" + text)
    return "\n\n".join(sections)[:max_chars]
