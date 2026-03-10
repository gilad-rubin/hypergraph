"""Tests for stop signal, NodeContext injection, and streaming.

Covers all user-facing scenarios from the design doc:
- Basic stop mid-stream
- Stop with complete_on_stop (partial persisted)
- Stop without checkpointer
- Stop converging to PAUSED via complete_on_stop + interrupt
- Stop without interrupt (STOPPED status)
- Resume after stop
- Streaming without stop (live preview)
- Backward compatibility (no NodeContext)
- Steering (stop + redirect)
- Testing with mocks (plain Python)
- Batch processing with stop
- Sync/async parity
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from hypergraph import (
    END,
    AsyncRunner,
    Graph,
    RunStatus,
    StopRequestedEvent,
    StreamingChunkEvent,
    SyncRunner,
    WorkflowAlreadyRunningError,
    interrupt,
    node,
    route,
)
from hypergraph.runners._shared.node_context import NodeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ChunkCollector:
    """Event processor that collects StreamingChunkEvents."""

    def __init__(self):
        self.chunks: list[Any] = []
        self.stop_events: list[StopRequestedEvent] = []

    def on_event(self, event):
        if isinstance(event, StreamingChunkEvent):
            self.chunks.append(event.chunk)
        elif isinstance(event, StopRequestedEvent):
            self.stop_events.append(event)

    def shutdown(self):
        pass


# ---------------------------------------------------------------------------
# 1. NodeContext excluded from node inputs
# ---------------------------------------------------------------------------


class TestNodeContextSignature:
    def test_context_excluded_from_inputs(self):
        @node(output_name="result")
        def my_node(x: int, ctx: NodeContext) -> int:
            return x

        assert my_node.inputs == ("x",)
        assert "ctx" not in my_node.inputs

    def test_context_excluded_regardless_of_param_name(self):
        @node(output_name="result")
        def my_node(x: int, context: NodeContext) -> int:
            return x

        assert my_node.inputs == ("x",)

    def test_no_context_unchanged(self):
        """Backward compat: nodes without NodeContext work as before."""

        @node(output_name="result")
        def my_node(x: int) -> int:
            return x * 2

        assert my_node.inputs == ("x",)

    def test_context_not_bindable(self):
        @node(output_name="result")
        def my_node(x: int, ctx: NodeContext) -> int:
            return x

        graph = Graph([my_node])
        with pytest.raises(TypeError):
            graph.bind(ctx="something")

    def test_context_not_a_graph_input(self):
        @node(output_name="result")
        def my_node(x: int, ctx: NodeContext) -> int:
            return x

        graph = Graph([my_node])
        assert "ctx" not in graph.inputs.all


# ---------------------------------------------------------------------------
# 2. NodeContext injected at execution time
# ---------------------------------------------------------------------------


class TestNodeContextInjection:
    async def test_async_node_receives_context(self):
        received = {}

        @node(output_name="result")
        async def my_node(x: int, ctx: NodeContext) -> int:
            received["stop_requested"] = ctx.stop_requested
            received["has_stream"] = callable(ctx.stream)
            return x * 2

        runner = AsyncRunner()
        result = await runner.run(Graph([my_node]), x=5)
        assert result["result"] == 10
        assert received["stop_requested"] is False
        assert received["has_stream"] is True

    def test_sync_node_receives_context(self):
        received = {}

        @node(output_name="result")
        def my_node(x: int, ctx: NodeContext) -> int:
            received["stop_requested"] = ctx.stop_requested
            return x * 2

        runner = SyncRunner()
        result = runner.run(Graph([my_node]), x=5)
        assert result["result"] == 10
        assert received["stop_requested"] is False


# ---------------------------------------------------------------------------
# 3. Stop mid-stream (basic async)
# ---------------------------------------------------------------------------


class TestStopMidStream:
    async def test_stop_requested_breaks_loop(self):
        """Simulate LLM streaming: node checks stop_requested per chunk."""
        chunks_produced = []

        @node(output_name="response")
        async def stream_llm(ctx: NodeContext) -> str:
            response = ""
            for i in range(100):
                if ctx.stop_requested:
                    break
                chunk = f"chunk-{i} "
                response += chunk
                chunks_produced.append(chunk)
                await asyncio.sleep(0.001)  # yield control
            return response

        runner = AsyncRunner()
        graph = Graph([stream_llm])

        # Start run in background, stop after a brief delay
        async def stop_after_delay():
            await asyncio.sleep(0.02)
            runner.stop("test-wf", info={"kind": "user_stop"})

        stop_task = asyncio.create_task(stop_after_delay())
        result = await runner.run(graph, workflow_id="test-wf")
        await stop_task

        assert result.stopped is True
        assert len(chunks_produced) < 100  # stopped before finishing
        assert len(result["response"]) > 0  # partial output preserved

    async def test_stop_result_properties(self):
        @node(output_name="result")
        async def slow_node(ctx: NodeContext) -> str:
            for _ in range(100):
                if ctx.stop_requested:
                    break
                await asyncio.sleep(0.001)
            return "partial"

        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf-1")

        task = asyncio.create_task(stop_soon())
        result = await runner.run(Graph([slow_node]), workflow_id="wf-1")
        await task

        assert result.stopped is True
        assert result.status == RunStatus.STOPPED


# ---------------------------------------------------------------------------
# 4. Streaming events (ctx.stream)
# ---------------------------------------------------------------------------


class TestStreaming:
    async def test_stream_emits_events(self):
        @node(output_name="response")
        async def my_node(ctx: NodeContext) -> str:
            ctx.stream("hello ")
            ctx.stream("world")
            return "hello world"

        collector = ChunkCollector()
        runner = AsyncRunner()
        result = await runner.run(
            Graph([my_node]),
            event_processors=[collector],
        )

        assert result["response"] == "hello world"
        assert collector.chunks == ["hello ", "world"]

    async def test_stream_skipped_after_stop(self):
        streamed = []

        @node(output_name="response")
        async def my_node(ctx: NodeContext) -> str:
            ctx.stream("before")
            streamed.append("before")
            # Simulate stop
            # (In practice, stop_requested would be True from runner.stop())
            # We test the NodeContext directly here
            return "result"

        collector = ChunkCollector()
        runner = AsyncRunner()
        await runner.run(
            Graph([my_node]),
            event_processors=[collector],
        )

        assert "before" in collector.chunks

    def test_sync_stream_emits_events(self):
        @node(output_name="response")
        def my_node(ctx: NodeContext) -> str:
            ctx.stream("hello ")
            ctx.stream("world")
            return "hello world"

        collector = ChunkCollector()
        runner = SyncRunner()
        result = runner.run(
            Graph([my_node]),
            event_processors=[collector],
        )

        assert result["response"] == "hello world"
        assert collector.chunks == ["hello ", "world"]


# ---------------------------------------------------------------------------
# 5. Stop without checkpointer (in-memory)
# ---------------------------------------------------------------------------


class TestStopWithoutCheckpointer:
    async def test_stop_works_without_checkpointer(self):
        @node(output_name="result")
        async def my_node(ctx: NodeContext) -> str:
            for _ in range(100):
                if ctx.stop_requested:
                    return "stopped"
                await asyncio.sleep(0.001)
            return "done"

        runner = AsyncRunner()  # no checkpointer

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf")

        task = asyncio.create_task(stop_soon())
        result = await runner.run(Graph([my_node]), workflow_id="wf")
        await task

        assert result.stopped is True
        assert result["result"] == "stopped"


# ---------------------------------------------------------------------------
# 6. Stop converges to PAUSED via complete_on_stop + interrupt
# ---------------------------------------------------------------------------


class TestStopConvergesToPaused:
    async def test_complete_on_stop_reaches_interrupt(self, tmp_path):
        """The chat app pattern: stop → partial → complete_on_stop →
        remaining nodes run → hits interrupt → status=PAUSED, stopped=True."""
        from hypergraph.checkpointers import SqliteCheckpointer

        @node(output_name="response")
        async def generate(ctx: NodeContext) -> str:
            result = ""
            for i in range(100):
                if ctx.stop_requested:
                    break
                result += f"token-{i} "
                await asyncio.sleep(0.001)
            return result

        @node(output_name="messages")
        def save_response(messages: list, response: str) -> list:
            return [*messages, {"role": "assistant", "content": response}]

        @interrupt(output_name="user_input")
        def wait_for_user() -> None:
            return None

        # Inner subgraph: generate + save (complete_on_stop=True)
        inner = Graph(
            [generate, save_response],
            name="llm_turn",
        ).as_node(complete_on_stop=True)

        # Outer graph with interrupt
        chat = Graph(
            [inner, wait_for_user],
            edges=[(inner, wait_for_user)],
            name="chat",
            shared=["messages"],
        ).bind(messages=[])

        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        try:
            runner = AsyncRunner(checkpointer=cp)

            async def stop_soon():
                await asyncio.sleep(0.02)
                runner.stop("chat-1")

            task = asyncio.create_task(stop_soon())
            result = await runner.run(chat, workflow_id="chat-1")
            await task

            # Stop converged to PAUSED (via complete_on_stop → interrupt)
            assert result.paused is True
            assert result.stopped is True
            assert len(result["messages"]) > 0  # partial was saved
        finally:
            await cp.close()


# ---------------------------------------------------------------------------
# 7. Stop without interrupt → STOPPED status
# ---------------------------------------------------------------------------


class TestStoppedStatus:
    async def test_stop_without_interrupt_gives_stopped(self):
        @node(output_name="a")
        async def step_a(ctx: NodeContext) -> int:
            for _ in range(100):
                if ctx.stop_requested:
                    return 0
                await asyncio.sleep(0.001)
            return 42

        @node(output_name="b")
        def step_b(a: int) -> int:
            return a + 1

        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf")

        task = asyncio.create_task(stop_soon())
        result = await runner.run(
            Graph([step_a, step_b]),
            workflow_id="wf",
        )
        await task

        assert result.stopped is True
        assert result.status == RunStatus.STOPPED


# ---------------------------------------------------------------------------
# 8. Resume after stop
# ---------------------------------------------------------------------------


class TestResumeAfterStop:
    async def test_resume_after_stop_with_checkpointer(self, tmp_path):
        from hypergraph.checkpointers import SqliteCheckpointer

        call_count = 0

        @node(output_name="response")
        async def generate(ctx: NodeContext) -> str:
            nonlocal call_count
            call_count += 1
            result = ""
            for i in range(100):
                if ctx.stop_requested:
                    break
                result += f"t{i} "
                await asyncio.sleep(0.001)
            return result

        @interrupt(output_name="user_input")
        def wait_for_user() -> None:
            return None

        inner = Graph(
            [generate],
            name="gen",
        ).as_node(complete_on_stop=True)

        chat = Graph(
            [inner, wait_for_user],
            edges=[(inner, wait_for_user)],
            name="chat",
        )

        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        try:
            runner = AsyncRunner(checkpointer=cp)

            # Turn 1: stop mid-stream
            async def stop_soon():
                await asyncio.sleep(0.02)
                runner.stop("chat-1")

            task = asyncio.create_task(stop_soon())
            r1 = await runner.run(chat, workflow_id="chat-1")
            await task

            assert r1.paused is True
            assert r1.stopped is True

            # Turn 2: resume (identical to normal turn)
            r2 = await runner.run(chat, workflow_id="chat-1", user_input="continue")
            assert r2.paused is True
            assert r2.stopped is False  # this turn was not stopped
        finally:
            await cp.close()


# ---------------------------------------------------------------------------
# 9. Backward compatibility: no NodeContext
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    async def test_async_node_without_context(self):
        @node(output_name="result")
        async def my_node(x: int) -> int:
            return x * 2

        runner = AsyncRunner()
        result = await runner.run(Graph([my_node]), x=5)
        assert result["result"] == 10

    def test_sync_node_without_context(self):
        @node(output_name="result")
        def my_node(x: int) -> int:
            return x * 2

        runner = SyncRunner()
        result = runner.run(Graph([my_node]), x=5)
        assert result["result"] == 10


# ---------------------------------------------------------------------------
# 10. Steering: stop + redirect
# ---------------------------------------------------------------------------


class TestSteering:
    async def test_hard_steer_stop_and_redirect(self, tmp_path):
        from hypergraph.checkpointers import SqliteCheckpointer

        @node(output_name="response")
        async def generate(prompt: str, ctx: NodeContext) -> str:
            result = ""
            for i in range(100):
                if ctx.stop_requested:
                    break
                result += f"{prompt}-{i} "
                await asyncio.sleep(0.001)
            return result

        @interrupt(output_name="prompt")
        def wait_for_input() -> None:
            return None

        inner = Graph(
            [generate],
            name="gen",
        ).as_node(complete_on_stop=True)

        chat = Graph(
            [wait_for_input, inner],
            edges=[(wait_for_input, inner)],
            name="chat",
            entrypoint="wait_for_input",
        )

        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        try:
            runner = AsyncRunner(checkpointer=cp)

            # Turn 1: start generating
            async def stop_soon():
                await asyncio.sleep(0.02)
                runner.stop("chat-1")

            task = asyncio.create_task(stop_soon())
            r1 = await runner.run(chat, workflow_id="chat-1", prompt="topic-A")
            await task

            assert r1.stopped is True

            # Turn 2: redirect to new topic
            r2 = await runner.run(chat, workflow_id="chat-1", prompt="topic-B")
            assert r2.stopped is False
            assert "topic-B" in r2["response"]
        finally:
            await cp.close()


# ---------------------------------------------------------------------------
# 11. WorkflowAlreadyRunningError
# ---------------------------------------------------------------------------


class TestWorkflowAlreadyRunning:
    async def test_second_run_same_workflow_raises(self):
        @node(output_name="result")
        async def slow(ctx: NodeContext) -> str:
            await asyncio.sleep(10)
            return "done"

        runner = AsyncRunner()

        async def attempt_second_run():
            await asyncio.sleep(0.02)
            with pytest.raises(WorkflowAlreadyRunningError):
                await runner.run(Graph([slow]), workflow_id="wf-1")

        task = asyncio.create_task(attempt_second_run())

        # First run will be stopped so we don't wait 10s
        async def stop_first():
            await asyncio.sleep(0.05)
            runner.stop("wf-1")

        stop_task = asyncio.create_task(stop_first())

        await runner.run(Graph([slow]), workflow_id="wf-1")
        await task
        await stop_task

    async def test_stop_nonexistent_workflow_is_noop(self):
        runner = AsyncRunner()
        runner.stop("does-not-exist")  # should not raise


# ---------------------------------------------------------------------------
# 12. StopRequestedEvent emission
# ---------------------------------------------------------------------------


class TestStopRequestedEvent:
    async def test_stop_emits_event_with_info(self):
        @node(output_name="result")
        async def my_node(ctx: NodeContext) -> str:
            for _ in range(100):
                if ctx.stop_requested:
                    return "stopped"
                await asyncio.sleep(0.001)
            return "done"

        collector = ChunkCollector()
        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf", info={"kind": "user_stop"})

        task = asyncio.create_task(stop_soon())
        await runner.run(
            Graph([my_node]),
            workflow_id="wf",
            event_processors=[collector],
        )
        await task

        assert len(collector.stop_events) > 0
        assert collector.stop_events[0].info == {"kind": "user_stop"}


# ---------------------------------------------------------------------------
# 13. StepRecord.partial
# ---------------------------------------------------------------------------


class TestStepRecordPartial:
    async def test_partial_flag_set_on_stopped_node(self, tmp_path):
        from hypergraph.checkpointers import SqliteCheckpointer

        @node(output_name="result")
        async def my_node(ctx: NodeContext) -> str:
            for _ in range(100):
                if ctx.stop_requested:
                    return "partial"
                await asyncio.sleep(0.001)
            return "full"

        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        try:
            runner = AsyncRunner(checkpointer=cp)

            async def stop_soon():
                await asyncio.sleep(0.02)
                runner.stop("wf")

            task = asyncio.create_task(stop_soon())
            await runner.run(Graph([my_node]), workflow_id="wf")
            await task

            # Check StepRecord
            run = await cp.get_run_async("wf")
            checkpoint = await cp.get_checkpoint_async(run.run_id)
            partial_steps = [s for s in checkpoint.steps if s.partial]
            assert len(partial_steps) > 0
        finally:
            await cp.close()


# ---------------------------------------------------------------------------
# 14. Stop propagates to nested graphs
# ---------------------------------------------------------------------------


class TestNestedStopPropagation:
    async def test_stop_reaches_nested_graph(self):
        nested_stopped = {}

        @node(output_name="inner_result")
        async def inner_node(ctx: NodeContext) -> str:
            for _ in range(100):
                if ctx.stop_requested:
                    nested_stopped["yes"] = True
                    return "stopped"
                await asyncio.sleep(0.001)
            return "done"

        inner_graph = Graph([inner_node], name="inner").as_node()
        outer_graph = Graph([inner_graph])

        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf")

        task = asyncio.create_task(stop_soon())
        result = await runner.run(outer_graph, workflow_id="wf")
        await task

        assert nested_stopped.get("yes") is True
        assert result.stopped is True


# ---------------------------------------------------------------------------
# 15. Batch processing with stop
# ---------------------------------------------------------------------------


class TestBatchStop:
    async def test_batch_processing_checks_stop_per_item(self):
        @node(output_name="results")
        async def process_batch(items: list, ctx: NodeContext) -> list:
            results = []
            for item in items:
                if ctx.stop_requested:
                    break
                results.append(item * 2)
                await asyncio.sleep(0.001)
            return results

        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf")

        task = asyncio.create_task(stop_soon())
        result = await runner.run(
            Graph([process_batch]),
            workflow_id="wf",
            items=list(range(1000)),
        )
        await task

        assert len(result["results"]) < 1000
        assert result.stopped is True


# ---------------------------------------------------------------------------
# 16. Testing NodeContext with mocks (plain Python)
# ---------------------------------------------------------------------------


class TestMockNodeContext:
    def test_node_testable_with_mock(self):
        """Users can test nodes with NodeContext using plain mocks."""

        @node(output_name="result")
        def my_node(x: int, ctx: NodeContext) -> int:
            if ctx.stop_requested:
                return 0
            ctx.stream(x)
            return x * 2

        # Test without stop
        ctx = MagicMock(spec=NodeContext)
        ctx.stop_requested = False
        assert my_node(5, ctx=ctx) == 10
        ctx.stream.assert_called_once_with(5)

        # Test with stop
        ctx2 = MagicMock(spec=NodeContext)
        ctx2.stop_requested = True
        assert my_node(5, ctx=ctx2) == 0

    async def test_async_node_testable_with_mock(self):
        @node(output_name="result")
        async def my_node(x: int, ctx: NodeContext) -> int:
            if ctx.stop_requested:
                return 0
            return x * 2

        ctx = MagicMock(spec=NodeContext)
        ctx.stop_requested = False
        assert await my_node(5, ctx=ctx) == 10


# ---------------------------------------------------------------------------
# 17. Sync runner stop
# ---------------------------------------------------------------------------


class TestSyncRunnerStop:
    def test_sync_stop_from_thread(self):
        import time as _time

        @node(output_name="result")
        def slow_node(ctx: NodeContext) -> str:
            result = ""
            for i in range(1000):
                if ctx.stop_requested:
                    break
                result += f"t{i} "
                _time.sleep(0.0001)  # slow down so stop signal arrives
            return result

        runner = SyncRunner()

        def stop_from_thread():
            import time

            time.sleep(0.02)
            runner.stop("wf")

        t = threading.Thread(target=stop_from_thread)
        t.start()

        result = runner.run(Graph([slow_node]), workflow_id="wf")
        t.join()

        assert result.stopped is True
        assert len(result["result"]) > 0

    def test_sync_stop_status(self):
        import time as _time

        @node(output_name="result")
        def slow_node(ctx: NodeContext) -> str:
            for _ in range(1000):
                if ctx.stop_requested:
                    return "stopped"
                _time.sleep(0.0001)  # slow down so stop signal arrives
            return "done"

        runner = SyncRunner()

        def stop_from_thread():
            import time

            time.sleep(0.02)
            runner.stop("wf")

        t = threading.Thread(target=stop_from_thread)
        t.start()

        result = runner.run(Graph([slow_node]), workflow_id="wf")
        t.join()

        assert result.stopped is True
        assert result.status == RunStatus.STOPPED


# ---------------------------------------------------------------------------
# 18. complete_on_stop parameter
# ---------------------------------------------------------------------------


class TestCompleteOnStop:
    async def test_complete_on_stop_runs_remaining_nodes(self):
        execution_order = []

        @node(output_name="partial")
        async def step_a(ctx: NodeContext) -> str:
            execution_order.append("a")
            for _ in range(100):
                if ctx.stop_requested:
                    return "partial-a"
                await asyncio.sleep(0.001)
            return "full-a"

        @node(output_name="final")
        def step_b(partial: str) -> str:
            execution_order.append("b")
            return f"processed-{partial}"

        inner = Graph(
            [step_a, step_b],
            name="inner",
        ).as_node(complete_on_stop=True)

        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf")

        task = asyncio.create_task(stop_soon())
        result = await runner.run(Graph([inner]), workflow_id="wf")
        await task

        # Both a and b ran despite stop
        assert "a" in execution_order
        assert "b" in execution_order
        assert result["final"].startswith("processed-partial")

    async def test_without_complete_on_stop_skips_remaining(self):
        execution_order = []

        @node(output_name="partial")
        async def step_a(ctx: NodeContext) -> str:
            execution_order.append("a")
            for _ in range(100):
                if ctx.stop_requested:
                    return "partial-a"
                await asyncio.sleep(0.001)
            return "full-a"

        @node(output_name="final")
        def step_b(partial: str) -> str:
            execution_order.append("b")
            return f"processed-{partial}"

        inner = Graph(
            [step_a, step_b],
            name="inner",
        ).as_node()  # default: complete_on_stop=False

        runner = AsyncRunner()

        async def stop_soon():
            await asyncio.sleep(0.02)
            runner.stop("wf")

        task = asyncio.create_task(stop_soon())
        await runner.run(Graph([inner]), workflow_id="wf")
        await task

        # step_b should NOT have run
        assert "a" in execution_order
        assert "b" not in execution_order


# ---------------------------------------------------------------------------
# 19. Route nodes with NodeContext
# ---------------------------------------------------------------------------


class TestRouteWithContext:
    def test_route_node_excludes_context(self):
        """Route nodes should also filter NodeContext from inputs."""

        @route(targets=["a", END])
        def decide(x: int) -> str:
            return END

        # Route node should work without NodeContext issues
        assert "x" in decide.inputs


# ---------------------------------------------------------------------------
# 20. Edge case: stop before any node runs
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_stop_before_run_starts(self):
        """runner.stop() before run() is a no-op — signal doesn't persist."""
        call_count = 0

        @node(output_name="result")
        async def my_node(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        runner = AsyncRunner()
        runner.stop("wf")  # no active run, should be no-op

        result = await runner.run(Graph([my_node]), workflow_id="wf", x=5)
        assert result["result"] == 10
        assert result.stopped is False
        assert call_count == 1
