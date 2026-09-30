"""Unit tests for FRIDAY's ported LoopGuard."""

import pytest
from source.server.loop_guard import LoopGuard, LoopGuardConfig


def test_loop_guard_allows_normal_calls():
    guard = LoopGuard(LoopGuardConfig(max_identical_calls=2))
    v1 = guard.check_call("python", "print(1)")
    assert not v1.blocked
    v2 = guard.check_call("python", "print(2)")
    assert not v2.blocked


def test_loop_guard_blocks_identical_calls():
    config = LoopGuardConfig(max_identical_calls=2, warn_before_block=False)
    guard = LoopGuard(config)
    assert not guard.check_call("python", "print(1)").blocked
    assert not guard.check_call("python", "print(1)").blocked
    verdict = guard.check_call("python", "print(1)")
    assert verdict.blocked
    assert "Degenerate loop" in verdict.reason


def test_loop_guard_detects_ping_pong():
    config = LoopGuardConfig(ping_pong_window=4, max_identical_calls=10)
    guard = LoopGuard(config)
    assert not guard.check_call("toolA", "arg").blocked
    assert not guard.check_call("toolB", "arg").blocked
    assert not guard.check_call("toolA", "arg").blocked
    verdict = guard.check_call("toolB", "arg")
    assert verdict.blocked
    assert "Ping-pong cycle" in verdict.reason
