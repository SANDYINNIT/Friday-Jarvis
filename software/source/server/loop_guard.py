"""Robust agent loop guard and timeout guard for FRIDAY tool execution.

Ported and adapted from OpenJarvis-main's LoopGuard. Detects degenerate
tool-calling loops (identical calls, ping-pong cycles) and enforces safety limits
to prevent infinite loops and runaway execution.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
import logging

logger = logging.getLogger("friday.guard")


@dataclass(slots=True)
class LoopGuardConfig:
    enabled: bool = True
    max_identical_calls: int = 3
    ping_pong_window: int = 6
    max_tool_executions: int = 25
    warn_before_block: bool = False


@dataclass(slots=True)
class LoopVerdict:
    blocked: bool = False
    reason: str = ""
    warned: bool = False


class LoopGuard:
    """Detect and prevent degenerate agent and tool-execution loops."""

    def __init__(self, config: LoopGuardConfig | None = None) -> None:
        self._config = config or LoopGuardConfig()
        self._call_counts: dict[str, int] = {}
        self._tool_sequence: deque[str] = deque(maxlen=self._config.ping_pong_window * 2)
        self._total_executions: int = 0
        self._warned_cycles: set[str] = set()

    def reset(self) -> None:
        self._call_counts.clear()
        self._tool_sequence.clear()
        self._total_executions = 0
        self._warned_cycles.clear()

    def check_call(self, tool_name: str, arguments: str) -> LoopVerdict:
        if not self._config.enabled:
            return LoopVerdict()

        self._total_executions += 1
        if self._total_executions > self._config.max_tool_executions:
            return LoopVerdict(
                blocked=True,
                reason=f"Exceeded maximum tool execution budget ({self._config.max_tool_executions} calls per turn)."
            )

        # 1. Identical calls hash tracking
        call_hash = hashlib.sha256(f"{tool_name}:{arguments}".encode()).hexdigest()[:16]
        self._call_counts[call_hash] = self._call_counts.get(call_hash, 0) + 1
        if self._call_counts[call_hash] > self._config.max_identical_calls:
            reason = f"Degenerate loop detected: tool {tool_name!r} called identically {self._config.max_identical_calls} times."
            if self._config.warn_before_block and call_hash not in self._warned_cycles:
                self._warned_cycles.add(call_hash)
                return LoopVerdict(blocked=False, warned=True, reason=reason)
            return LoopVerdict(blocked=True, reason=reason)

        # 2. Ping-pong cycle detection
        self._tool_sequence.append(tool_name)
        seq = list(self._tool_sequence)
        w = self._config.ping_pong_window
        if len(seq) >= w and w % 2 == 0:
            half = w // 2
            first_half = seq[-w:-half]
            second_half = seq[-half:]
            if first_half == second_half and len(set(first_half)) > 1:
                reason = f"Ping-pong cycle detected between tools: {' -> '.join(first_half)}."
                return LoopVerdict(blocked=True, reason=reason)

        return LoopVerdict()
