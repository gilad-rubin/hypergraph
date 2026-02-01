"""Tests for InterruptNode — human-in-the-loop pause/resume."""

from __future__ import annotations

import pytest

from hypergraph import (
    AsyncRunner,
    Graph,
    InterruptNode,
    PauseInfo,
    RunResult,
    RunStatus,
    SyncRunner,
    node,
    route,
    END,
)
from hypergraph.exceptions import IncompatibleRunnerError


# ── Phase 1: Node class + Graph detection ──


class TestInterruptNodeConstruction:
    def test_basic_construction(self):
        n = InterruptNode(name="approval", input_param="draft", output_param="decision")
        assert n.name == "approval"
        assert n.inputs == ("draft",)
        assert n.outputs == ("decision",)
        assert n.input_param == "draft"
        assert n.output_param == "decision"
        assert n.response_type is None
        assert n.handler is None

    def test_with_response_type(self):
        n = InterruptNode(name="x", input_param="a", output_param="b", response_type=str)
        assert n.response_type is str

    def test_with_handler_constructor(self):
        handler = lambda x: "yes"
        n = InterruptNode(name="x", input_param="a", output_param="b", handler=handler)
        assert n.handler is handler

    def test_cache_always_false(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        assert n.cache is False

    def test_is_async_false(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        assert n.is_async is False

    def test_is_generator_false(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        assert n.is_generator is False

    def test_definition_hash_includes_response_type(self):
        n1 = InterruptNode(name="x", input_param="a", output_param="b")
        n2 = InterruptNode(name="x", input_param="a", output_param="b", response_type=str)
        assert n1.definition_hash != n2.definition_hash

    def test_definition_hash_excludes_handler(self):
        n1 = InterruptNode(name="x", input_param="a", output_param="b")
        n2 = InterruptNode(name="x", input_param="a", output_param="b", handler=lambda x: x)
        assert n1.definition_hash == n2.definition_hash


class TestInterruptNodeParameterValidation:
    def test_invalid_identifier_input_param(self):
        with pytest.raises(ValueError, match="input_param"):
            InterruptNode(name="x", input_param="123bad", output_param="b")

    def test_invalid_identifier_output_param(self):
        with pytest.raises(ValueError, match="output_param"):
            InterruptNode(name="x", input_param="a", output_param="not-valid")

    def test_keyword_input_param(self):
        with pytest.raises(ValueError, match="input_param"):
            InterruptNode(name="x", input_param="class", output_param="b")

    def test_keyword_output_param(self):
        with pytest.raises(ValueError, match="output_param"):
            InterruptNode(name="x", input_param="a", output_param="return")


class TestInterruptNodeRename:
    def test_with_name(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        renamed = n.with_name("y")
        assert renamed.name == "y"
        assert n.name == "x"  # original unchanged

    def test_with_inputs(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        renamed = n.with_inputs(a="c")
        assert renamed.inputs == ("c",)
        assert renamed.input_param == "c"

    def test_with_outputs(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        renamed = n.with_outputs(b="d")
        assert renamed.outputs == ("d",)
        assert renamed.output_param == "d"


class TestInterruptNodeWithHandler:
    def test_with_handler_returns_new_instance(self):
        n = InterruptNode(name="x", input_param="a", output_param="b")
        handler = lambda x: x
        n2 = n.with_handler(handler)
        assert n2.handler is handler
        assert n.handler is None  # original unchanged
        assert n2 is not n

    def test_with_handler_preserves_other_attrs(self):
        n = InterruptNode(name="x", input_param="a", output_param="b", response_type=str)
        n2 = n.with_handler(lambda x: x)
        assert n2.name == "x"
        assert n2.inputs == ("a",)
        assert n2.outputs == ("b",)
        assert n2.response_type is str


class TestGraphInterruptDetection:
    def test_has_interrupts_true(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return query

        interrupt = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        )
        graph = Graph([make_draft, interrupt])
        assert graph.has_interrupts is True

    def test_has_interrupts_false(self):
        @node(output_name="result")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        assert graph.has_interrupts is False

    def test_interrupt_nodes_property(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return query

        i1 = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        )
        graph = Graph([make_draft, i1])
        assert len(graph.interrupt_nodes) == 1
        assert graph.interrupt_nodes[0] is i1


# ── Phase 2: Runtime types ──


class TestPauseInfo:
    def test_construction(self):
        p = PauseInfo(node_name="approval", output_param="decision", value="draft text")
        assert p.node_name == "approval"
        assert p.output_param == "decision"
        assert p.value == "draft text"

    def test_response_key_top_level(self):
        p = PauseInfo(node_name="approval", output_param="decision", value=None)
        assert p.response_key == "decision"

    def test_response_key_nested(self):
        p = PauseInfo(node_name="review/approval", output_param="decision", value=None)
        assert p.response_key == "review.decision"

    def test_response_key_deeply_nested(self):
        p = PauseInfo(
            node_name="outer/review/approval", output_param="decision", value=None
        )
        assert p.response_key == "outer.review.decision"


class TestRunResultPaused:
    def test_paused_property(self):
        r = RunResult(values={}, status=RunStatus.PAUSED)
        assert r.paused is True

    def test_not_paused(self):
        r = RunResult(values={}, status=RunStatus.COMPLETED)
        assert r.paused is False

    def test_pause_field(self):
        info = PauseInfo(node_name="x", output_param="y", value=42)
        r = RunResult(values={}, status=RunStatus.PAUSED, pause=info)
        assert r.pause is info


# ── Phase 3: Validation ──


class TestInterruptValidation:
    def test_sync_runner_rejects_interrupts(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return query

        interrupt = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        )
        graph = Graph([make_draft, interrupt])
        runner = SyncRunner()
        with pytest.raises(IncompatibleRunnerError, match="InterruptNode"):
            runner.run(graph, {"query": "hello"})

    @pytest.mark.asyncio
    async def test_async_map_rejects_interrupts(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return query

        interrupt = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        )
        graph = Graph([make_draft, interrupt])
        runner = AsyncRunner()
        with pytest.raises(IncompatibleRunnerError, match="InterruptNode"):
            await runner.map(graph, {"query": ["a", "b"]}, map_over="query")


# ── Phase 4: AsyncRunner pause/resume ──


class TestAsyncRunnerPause:
    @pytest.mark.asyncio
    async def test_basic_pause(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        interrupt = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.PAUSED
        assert result.paused is True
        assert result.pause is not None
        assert result.pause.node_name == "approval"
        assert result.pause.output_param == "decision"
        assert result.pause.value == "Draft for: hello"
        assert result.pause.response_key == "decision"

    @pytest.mark.asyncio
    async def test_resume_with_response(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        interrupt = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        # First run: pauses
        result = await runner.run(graph, {"query": "hello"})
        assert result.paused

        # Resume with response
        result = await runner.run(
            graph, {"query": "hello", result.pause.response_key: "approved"}
        )
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: approved"

    @pytest.mark.asyncio
    async def test_handler_constructor_auto_resolves(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        interrupt = InterruptNode(
            name="approval",
            input_param="draft",
            output_param="decision",
            handler=lambda draft: "auto-approved",
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: auto-approved"

    @pytest.mark.asyncio
    async def test_async_handler_auto_resolves(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        async def async_handler(value):
            return "async-approved"

        interrupt = InterruptNode(
            name="approval",
            input_param="draft",
            output_param="decision",
            handler=async_handler,
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: async-approved"

    @pytest.mark.asyncio
    async def test_with_handler_auto_resolves(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        interrupt = InterruptNode(
            name="approval", input_param="draft", output_param="decision"
        ).with_handler(lambda v: "chain-approved")

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: chain-approved"

    @pytest.mark.asyncio
    async def test_multiple_sequential_interrupts(self):
        """Two interrupts in sequence: pauses at first, then second."""

        @node(output_name="step1")
        def produce(x: str) -> str:
            return x

        i1 = InterruptNode(
            name="interrupt1", input_param="step1", output_param="response1"
        )
        i2 = InterruptNode(
            name="interrupt2", input_param="response1", output_param="response2"
        )

        @node(output_name="result")
        def final(response2: str) -> str:
            return f"done: {response2}"

        graph = Graph([produce, i1, i2, final])
        runner = AsyncRunner()

        # First pause
        r1 = await runner.run(graph, {"x": "hello"})
        assert r1.paused
        assert r1.pause.node_name == "interrupt1"

        # Resume first, pause at second
        r2 = await runner.run(graph, {"x": "hello", "response1": "resp1"})
        assert r2.paused
        assert r2.pause.node_name == "interrupt2"
        assert r2.pause.value == "resp1"

        # Resume second
        r3 = await runner.run(
            graph, {"x": "hello", "response1": "resp1", "response2": "resp2"}
        )
        assert r3.status == RunStatus.COMPLETED
        assert r3["result"] == "done: resp2"


class TestHandlerFailure:
    @pytest.mark.asyncio
    async def test_handler_exception_wrapped_in_runtime_error(self):
        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        def bad_handler(value):
            raise ValueError("handler broke")

        interrupt = InterruptNode(
            name="approval",
            input_param="draft",
            output_param="decision",
            handler=bad_handler,
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return decision

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, RuntimeError)
        assert "handler broke" in str(result.error)


class TestNestedInterruptPropagation:
    @pytest.mark.asyncio
    async def test_nested_graph_pause_propagation(self):
        """InterruptNode in nested graph propagates with prefixed node_name."""
        interrupt = InterruptNode(name="approval", input_param="x", output_param="y")
        inner = Graph([interrupt], name="inner")

        @node(output_name="x")
        def produce(query: str) -> str:
            return query

        @node(output_name="result")
        def consume(y: str) -> str:
            return f"got: {y}"

        outer = Graph([produce, inner.as_node(), consume])
        runner = AsyncRunner()

        result = await runner.run(outer, {"query": "hello"})
        assert result.paused
        assert result.pause.node_name == "inner/approval"
        assert result.pause.response_key == "inner.y"
        assert result.pause.value == "hello"


# ── Phase 6: InterruptNode in cycles ──


class TestInterruptNodeInCycle:
    """InterruptNode inside a cycle should pause on every iteration."""

    def test_interrupt_output_not_classified_as_seed(self):
        """Interrupt output in a cycle should NOT be a seed input."""
        ask_user = InterruptNode(
            name="ask_user", input_param="messages", output_param="query"
        )

        @node(output_name="response")
        def process(query: str) -> str:
            return f"response to {query}"

        @node(output_name="messages")
        def accumulate(messages: list, response: str) -> list:
            return messages + [response]

        @route(targets=["ask_user", END])
        def decide(messages: list) -> str:
            return END if len(messages) > 2 else "ask_user"

        graph = Graph([ask_user, process, accumulate, decide])
        # query should NOT be in seeds since it's produced by an InterruptNode
        assert "query" not in graph.inputs.seeds
        assert "messages" in graph.inputs.seeds

    @pytest.mark.asyncio
    async def test_cycle_interrupt_pauses_first_run(self):
        """First run with no query should pause at the interrupt."""
        ask_user = InterruptNode(
            name="ask_user", input_param="messages", output_param="query"
        )

        @node(output_name="response")
        def process(query: str) -> str:
            return f"response to {query}"

        @node(output_name="messages")
        def accumulate(messages: list, response: str) -> list:
            return messages + [response]

        @route(targets=["ask_user", END])
        def decide(messages: list) -> str:
            return END if len(messages) > 2 else "ask_user"

        graph = Graph([ask_user, process, accumulate, decide])
        runner = AsyncRunner()

        result = await runner.run(graph, {"messages": []})
        assert result.paused
        assert result.pause.node_name == "ask_user"
        assert result.pause.value == []

    @pytest.mark.asyncio
    async def test_cycle_interrupt_resumes_then_pauses_again(self):
        """Resuming with a query should process it, then pause again on next iteration."""
        ask_user = InterruptNode(
            name="ask_user", input_param="messages", output_param="query"
        )

        @node(output_name="response")
        def process(query: str) -> str:
            return f"response to {query}"

        @node(output_name="messages")
        def accumulate(messages: list, response: str) -> list:
            return messages + [response]

        @route(targets=["ask_user", END])
        def decide(messages: list) -> str:
            return END if len(messages) > 2 else "ask_user"

        graph = Graph([ask_user, process, accumulate, decide])
        runner = AsyncRunner()

        # Run 1: pause immediately
        r1 = await runner.run(graph, {"messages": []})
        assert r1.paused

        # Run 2: provide query -> processes -> loops -> pauses again
        r2 = await runner.run(graph, {"messages": [], "query": "What is RAG?"})
        assert r2.paused
        assert r2.pause.node_name == "ask_user"
        # Messages now contain the response from first query
        assert r2.pause.value == ["response to What is RAG?"]

    @pytest.mark.asyncio
    async def test_cycle_interrupt_completes_after_enough_messages(self):
        """Cycle should complete when decide returns END."""
        ask_user = InterruptNode(
            name="ask_user", input_param="messages", output_param="query"
        )

        @node(output_name="response")
        def process(query: str) -> str:
            return f"response to {query}"

        @node(output_name="messages")
        def accumulate(messages: list, response: str) -> list:
            return messages + [response]

        @route(targets=["ask_user", END])
        def decide(messages: list) -> str:
            return END if len(messages) >= 1 else "ask_user"

        graph = Graph([ask_user, process, accumulate, decide])
        runner = AsyncRunner()

        # Provide query, process completes, decide returns END
        result = await runner.run(graph, {"messages": [], "query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["messages"] == ["response to hello"]
