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
    interrupt,
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


class TestInterruptNodeMultiParam:
    """Tests for multi-input/output InterruptNode construction."""

    def test_multi_input_construction(self):
        n = InterruptNode(
            name="review",
            input_param=("draft", "metadata"),
            output_param="decision",
        )
        assert n.inputs == ("draft", "metadata")
        assert n.outputs == ("decision",)
        assert n.input_param == "draft"  # backward compat: first input
        assert n.is_multi_input is True
        assert n.is_multi_output is False

    def test_multi_output_construction(self):
        n = InterruptNode(
            name="review",
            input_param="draft",
            output_param=("decision", "notes"),
        )
        assert n.inputs == ("draft",)
        assert n.outputs == ("decision", "notes")
        assert n.output_param == "decision"  # backward compat: first output
        assert n.is_multi_input is False
        assert n.is_multi_output is True

    def test_multi_both_construction(self):
        n = InterruptNode(
            name="review",
            input_param=("draft", "metadata"),
            output_param=("decision", "notes"),
        )
        assert n.inputs == ("draft", "metadata")
        assert n.outputs == ("decision", "notes")
        assert n.is_multi_input is True
        assert n.is_multi_output is True

    def test_multi_output_with_dict_response_type(self):
        n = InterruptNode(
            name="review",
            input_param="draft",
            output_param=("decision", "notes"),
            response_type={"decision": bool, "notes": str},
        )
        assert n.get_output_type("decision") is bool
        assert n.get_output_type("notes") is str
        assert n.get_output_type("unknown") is None

    def test_single_output_get_output_type_unchanged(self):
        n = InterruptNode(
            name="x", input_param="a", output_param="b", response_type=str
        )
        assert n.get_output_type("b") is str
        assert n.get_output_type("unknown") is None

    def test_definition_hash_includes_dict_response_type(self):
        n1 = InterruptNode(
            name="x",
            input_param="a",
            output_param=("b", "c"),
        )
        n2 = InterruptNode(
            name="x",
            input_param="a",
            output_param=("b", "c"),
            response_type={"b": str, "c": int},
        )
        assert n1.definition_hash != n2.definition_hash

    def test_multi_input_validation(self):
        with pytest.raises(ValueError, match="input_param"):
            InterruptNode(
                name="x",
                input_param=("valid", "123bad"),
                output_param="out",
            )

    def test_multi_output_validation(self):
        with pytest.raises(ValueError, match="output_param"):
            InterruptNode(
                name="x",
                input_param="inp",
                output_param=("valid", "class"),
            )


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

    def test_with_inputs_multi(self):
        n = InterruptNode(
            name="x", input_param=("a", "b"), output_param="out"
        )
        renamed = n.with_inputs(a="c", b="d")
        assert renamed.inputs == ("c", "d")

    def test_with_outputs_multi(self):
        n = InterruptNode(
            name="x", input_param="inp", output_param=("a", "b")
        )
        renamed = n.with_outputs(a="c", b="d")
        assert renamed.outputs == ("c", "d")


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

    def test_response_keys_single_output(self):
        p = PauseInfo(node_name="approval", output_param="decision", value=None)
        assert p.response_keys == {"decision": "decision"}

    def test_response_keys_multi_output(self):
        p = PauseInfo(
            node_name="approval",
            output_param="decision",
            value=None,
            output_params=("decision", "notes"),
        )
        assert p.response_keys == {"decision": "decision", "notes": "notes"}

    def test_response_keys_nested_multi_output(self):
        p = PauseInfo(
            node_name="review/approval",
            output_param="decision",
            value=None,
            output_params=("decision", "notes"),
        )
        assert p.response_keys == {
            "decision": "review.decision",
            "notes": "review.notes",
        }

    def test_multi_input_values(self):
        p = PauseInfo(
            node_name="review",
            output_param="decision",
            value="draft text",
            values={"draft": "draft text", "metadata": {"author": "me"}},
        )
        assert p.value == "draft text"  # backward compat
        assert p.values == {"draft": "draft text", "metadata": {"author": "me"}}


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


class TestAsyncRunnerMultiParam:
    """Tests for multi-input/output InterruptNode execution."""

    @pytest.mark.asyncio
    async def test_multi_input_pause(self):
        """InterruptNode with multiple inputs receives all values in pause."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        @node(output_name="metadata")
        def make_meta(query: str) -> dict:
            return {"query": query}

        interrupt = InterruptNode(
            name="review",
            input_param=("draft", "metadata"),
            output_param="decision",
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, make_meta, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.paused
        assert result.pause.node_name == "review"
        assert result.pause.value == "Draft for: hello"  # first input
        assert result.pause.values == {
            "draft": "Draft for: hello",
            "metadata": {"query": "hello"},
        }

    @pytest.mark.asyncio
    async def test_multi_output_pause(self):
        """InterruptNode with multiple outputs exposes all output keys."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        interrupt = InterruptNode(
            name="review",
            input_param="draft",
            output_param=("decision", "notes"),
        )

        @node(output_name="result")
        def finalize(decision: str, notes: str) -> str:
            return f"{decision}: {notes}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.paused
        assert result.pause.output_param == "decision"  # first output
        assert result.pause.output_params == ("decision", "notes")
        assert result.pause.response_keys == {
            "decision": "decision",
            "notes": "notes",
        }

    @pytest.mark.asyncio
    async def test_multi_output_resume(self):
        """Resuming multi-output interrupt with all values completes."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        interrupt = InterruptNode(
            name="review",
            input_param="draft",
            output_param=("decision", "notes"),
        )

        @node(output_name="result")
        def finalize(decision: str, notes: str) -> str:
            return f"{decision}: {notes}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        # Pause
        r1 = await runner.run(graph, {"query": "hello"})
        assert r1.paused

        # Resume with all outputs
        r2 = await runner.run(
            graph, {"query": "hello", "decision": "approved", "notes": "looks good"}
        )
        assert r2.status == RunStatus.COMPLETED
        assert r2["result"] == "approved: looks good"

    @pytest.mark.asyncio
    async def test_multi_input_handler_receives_dict(self):
        """Handler for multi-input InterruptNode receives dict of values."""
        received = {}

        def handler(inputs_dict):
            received.update(inputs_dict)
            return "handled"

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        @node(output_name="metadata")
        def make_meta(query: str) -> dict:
            return {"key": "value"}

        interrupt = InterruptNode(
            name="review",
            input_param=("draft", "metadata"),
            output_param="decision",
            handler=handler,
        )

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return decision

        graph = Graph([make_draft, make_meta, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert received == {"draft": "draft", "metadata": {"key": "value"}}

    @pytest.mark.asyncio
    async def test_multi_output_handler_returns_dict(self):
        """Handler for multi-output InterruptNode can return dict."""

        def handler(value):
            return {"decision": "approved", "notes": f"for: {value}"}

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        interrupt = InterruptNode(
            name="review",
            input_param="draft",
            output_param=("decision", "notes"),
            handler=handler,
        )

        @node(output_name="result")
        def finalize(decision: str, notes: str) -> str:
            return f"{decision}: {notes}"

        graph = Graph([make_draft, interrupt, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "approved: for: draft"

    @pytest.mark.asyncio
    async def test_multi_output_handler_single_value_goes_to_first(self):
        """Handler returning single value for multi-output assigns to first."""

        def handler(value):
            return "only_decision"

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        interrupt = InterruptNode(
            name="review",
            input_param="draft",
            output_param=("decision", "notes"),
            handler=handler,
        )

        graph = Graph([make_draft, interrupt])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["decision"] == "only_decision"
        assert "notes" not in result.values


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


# ── @interrupt decorator ──


class TestInterruptDecorator:
    """Tests for the @interrupt decorator."""

    def test_basic_construction(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "approved"

        assert isinstance(approval, InterruptNode)
        assert approval.name == "approval"
        assert approval.inputs == ("draft",)
        assert approval.outputs == ("decision",)

    def test_func_accessible(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "approved"

        assert approval.func is not None
        assert approval.func("anything") == "approved"

    def test_callable_directly(self):
        """InterruptNode created via decorator is callable for testing."""

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "approved"

        assert approval("test") == "approved"

    def test_handler_backward_compat(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "approved"

        assert approval.handler is approval.func

    def test_ellipsis_body_returns_none(self):
        """Function with ... body returns None → pause."""

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        assert approval("test") is None

    def test_multi_input(self):
        @interrupt(output_name="decision")
        def review(draft: str, metadata: dict) -> str:
            return "ok"

        assert review.inputs == ("draft", "metadata")
        assert review.is_multi_input is True

    def test_multi_output(self):
        @interrupt(output_name=("decision", "notes"))
        def review(draft: str) -> tuple[str, str]:
            return ("approved", "looks good")

        assert review.outputs == ("decision", "notes")
        assert review.is_multi_output is True

    def test_type_inference_from_annotations(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> bool:
            ...

        assert approval.get_input_type("draft") is str
        assert approval.get_output_type("decision") is bool

    def test_defaults_from_signature(self):
        @interrupt(output_name="decision")
        def approval(draft: str, threshold: float = 0.8) -> str:
            ...

        assert approval.has_default_for("threshold") is True
        assert approval.get_default_for("threshold") == 0.8
        assert approval.has_default_for("draft") is False

    def test_rename_inputs(self):
        @interrupt(output_name="decision", rename_inputs={"draft": "document"})
        def approval(draft: str) -> str:
            return "ok"

        assert approval.inputs == ("document",)
        assert approval.get_input_type("document") is str

    def test_with_name(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        renamed = approval.with_name("review_step")
        assert renamed.name == "review_step"
        assert approval.name == "approval"  # original unchanged

    def test_with_inputs(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        renamed = approval.with_inputs(draft="document")
        assert renamed.inputs == ("document",)

    def test_with_outputs(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        renamed = approval.with_outputs(decision="verdict")
        assert renamed.outputs == ("verdict",)

    def test_definition_hash_changes_with_code(self):
        @interrupt(output_name="a")
        def v1(x: str) -> str:
            return "one"

        @interrupt(output_name="a")
        def v2(x: str) -> str:
            return "two"

        # Different function bodies → different hashes
        assert v1.definition_hash != v2.definition_hash

    def test_cache_always_false(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        assert approval.cache is False

    def test_repr(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        assert "InterruptNode" in repr(approval)
        assert "approval" in repr(approval)


class TestInterruptDecoratorExecution:
    """Tests for @interrupt decorator in graph execution."""

    @pytest.mark.asyncio
    async def test_auto_resolve(self):
        """Decorator handler that returns a value auto-resolves."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "auto-approved"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, approval, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: auto-approved"

    @pytest.mark.asyncio
    async def test_pause_on_none_return(self):
        """Decorator handler that returns None pauses."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...  # returns None → pause

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, approval, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.paused
        assert result.pause.node_name == "approval"
        assert result.pause.value == "Draft for: hello"

    @pytest.mark.asyncio
    async def test_pause_then_resume(self):
        """Pause at decorator node, then resume with user value."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, approval, finalize])
        runner = AsyncRunner()

        r1 = await runner.run(graph, {"query": "hello"})
        assert r1.paused

        r2 = await runner.run(
            graph, {"query": "hello", r1.pause.response_key: "user-approved"}
        )
        assert r2.status == RunStatus.COMPLETED
        assert r2["result"] == "Final: user-approved"

    @pytest.mark.asyncio
    async def test_conditional_handler(self):
        """Handler that conditionally returns or pauses."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return query

        @interrupt(output_name="decision")
        def approval(draft: str) -> str | None:
            if "LGTM" in draft:
                return "auto-approved"
            return None  # pause for human review

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, approval, finalize])
        runner = AsyncRunner()

        # "LGTM" → auto-resolves
        r1 = await runner.run(graph, {"query": "LGTM looks great"})
        assert r1.status == RunStatus.COMPLETED
        assert r1["result"] == "Final: auto-approved"

        # No "LGTM" → pauses
        r2 = await runner.run(graph, {"query": "needs work"})
        assert r2.paused

    @pytest.mark.asyncio
    async def test_async_handler(self):
        """Async decorator handler."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        @interrupt(output_name="decision")
        async def approval(draft: str) -> str:
            return "async-approved"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, approval, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: async-approved"

    @pytest.mark.asyncio
    async def test_multi_input_kwargs(self):
        """Decorator handler with multiple inputs receives kwargs."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "the draft"

        @node(output_name="metadata")
        def make_meta(query: str) -> dict:
            return {"author": "test"}

        @interrupt(output_name="decision")
        def review(draft: str, metadata: dict) -> str:
            return f"reviewed:{draft}:{metadata['author']}"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return decision

        graph = Graph([make_draft, make_meta, review, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "reviewed:the draft:test"

    @pytest.mark.asyncio
    async def test_multi_output_dict_return(self):
        """Decorator handler returning dict for multi-output."""

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        @interrupt(output_name=("decision", "notes"))
        def review(draft: str) -> dict:
            return {"decision": "approved", "notes": f"for: {draft}"}

        @node(output_name="result")
        def finalize(decision: str, notes: str) -> str:
            return f"{decision}: {notes}"

        graph = Graph([make_draft, review, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "approved: for: draft"

    @pytest.mark.asyncio
    async def test_with_handler_on_decorator_node(self):
        """with_handler replaces the function on a decorator-created node."""

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...  # pause

        replaced = approval.with_handler(lambda draft: "replaced-approved")

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "draft"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        graph = Graph([make_draft, replaced, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "Final: replaced-approved"

    @pytest.mark.asyncio
    async def test_rename_inputs_in_execution(self):
        """Renamed inputs work correctly during execution."""

        @node(output_name="document")
        def produce(query: str) -> str:
            return "the doc"

        @interrupt(output_name="decision", rename_inputs={"draft": "document"})
        def approval(draft: str) -> str:
            return f"approved:{draft}"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return decision

        graph = Graph([produce, approval, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "approved:the doc"

    @pytest.mark.asyncio
    async def test_defaults_in_execution(self):
        """Function defaults work as node defaults in graph execution."""

        @interrupt(output_name="decision")
        def approval(draft: str, mode: str = "auto") -> str:
            return f"{mode}:{draft}"

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return "the draft"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return decision

        graph = Graph([make_draft, approval, finalize])
        runner = AsyncRunner()

        result = await runner.run(graph, {"query": "hello"})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "auto:the draft"

    @pytest.mark.asyncio
    async def test_nested_graph_decorator_interrupt(self):
        """Decorator interrupt in nested graph propagates with prefix."""

        @interrupt(output_name="decision")
        def approval(x: str) -> str:
            ...

        inner = Graph([approval], name="inner")

        @node(output_name="x")
        def produce(query: str) -> str:
            return query

        @node(output_name="result")
        def consume(decision: str) -> str:
            return f"got: {decision}"

        outer = Graph([produce, inner.as_node(), consume])
        runner = AsyncRunner()

        result = await runner.run(outer, {"query": "hello"})
        assert result.paused
        assert result.pause.node_name == "inner/approval"
        assert result.pause.response_key == "inner.decision"
