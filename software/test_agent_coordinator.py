import asyncio
import time
import unittest
from unittest.mock import patch

from source.server.agent_coordinator import (
    AgentCoordinator,
    AgentResult,
    AgentTask,
    merge_text_results,
)
from source.server.delegation import DelegationResult


class AgentCoordinatorTests(unittest.TestCase):
    def task(self, task_id, **changes):
        values = dict(task_id=task_id, target="specialist", provider="ollama", model="test", task="review")
        values.update(changes)
        return AgentTask(**values)

    def execute(self, awaitable):
        return asyncio.run(awaitable)

    def test_parallel_limit(self):
        active = [0]
        maximum = [0]

        def fake_delegate(request):
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            time.sleep(0.02)
            active[0] -= 1
            return DelegationResult(request.provider, request.model, True, request.task)

        async def scenario():
            coordinator = AgentCoordinator(max_parallel=2)
            try:
                return await coordinator.run([self.task(str(index)) for index in range(5)])
            finally:
                coordinator.shutdown()

        with patch("source.server.agent_coordinator.delegate", side_effect=fake_delegate):
            result = self.execute(scenario())
        self.assertEqual(maximum[0], 2)
        self.assertEqual(result.aggregate["success_count"], 5)

    def test_timeout_and_failure_are_isolated(self):
        def fake_delegate(request):
            if request.task == "slow":
                time.sleep(0.05)
            if request.task == "bad":
                raise RuntimeError("broken")
            return DelegationResult(request.provider, request.model, True, "ok")

        async def scenario():
            coordinator = AgentCoordinator()
            try:
                return await coordinator.run([
                    self.task("slow", task="slow", timeout=0.01),
                    self.task("bad", task="bad"),
                    self.task("ok"),
                ])
            finally:
                coordinator.shutdown()

        with patch("source.server.agent_coordinator.delegate", side_effect=fake_delegate):
            result = self.execute(scenario())
        self.assertTrue(result.results[0].timed_out)
        self.assertIn("failed", result.results[1].error)
        self.assertTrue(result.results[2].success)

    def test_bounds_and_deterministic_aggregation(self):
        def fake_delegate(request):
            return DelegationResult(request.provider, request.model, True, "z" * 20)

        async def scenario():
            coordinator = AgentCoordinator(max_parallel=1, max_context_chars=5, max_result_chars=7)
            try:
                return await coordinator.run([self.task("b", context="x" * 20), self.task("a")])
            finally:
                coordinator.shutdown()

        with patch("source.server.agent_coordinator.delegate", side_effect=fake_delegate) as delegate:
            result = self.execute(scenario())
        self.assertEqual(delegate.call_args_list[0].args[0].context, "xxxxx")
        self.assertEqual([item.task_id for item in result.results], ["b", "a"])
        self.assertTrue(all(len(item.text) <= 7 for item in result.results))
        self.assertEqual(result.aggregate["task_ids"], ("b", "a"))
        merged = merge_text_results(result.results, max_chars=20)
        self.assertLessEqual(len(merged), 20)
        self.assertIn("[b | specialist]", merged)

    def test_duplicate_ids_rejected_without_calls(self):
        async def scenario():
            coordinator = AgentCoordinator()
            try:
                with self.assertRaises(ValueError):
                    await coordinator.run([self.task("same"), self.task("same")])
            finally:
                coordinator.shutdown()

        with patch("source.server.agent_coordinator.delegate") as delegate:
            self.execute(scenario())
            delegate.assert_not_called()

    def test_shutdown_and_cancellation(self):
        async def scenario():
            coordinator = AgentCoordinator()
            coordinator.shutdown()
            with self.assertRaises(RuntimeError):
                await coordinator.run([self.task("closed")])

            coordinator = AgentCoordinator()
            try:
                run_task = asyncio.create_task(coordinator.run([self.task("cancel", timeout=10)], 10))
                await asyncio.sleep(0)
                run_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await run_task
            finally:
                coordinator.shutdown()

        with patch("source.server.agent_coordinator.delegate", side_effect=lambda request: time.sleep(0.05)):
            self.execute(scenario())


if __name__ == "__main__":
    unittest.main()
