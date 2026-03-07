"""Regression tests for shared-param staleness in cycles.

Two bugs fixed:
1. default_open re-activating a gated node that already executed (interrupt queue-jump)
2. Downstream shared-param updates triggering spurious upstream re-execution
"""

import pytest

from hypergraph import END, AsyncRunner, Graph, interrupt, node, route
from hypergraph.runners._shared.helpers import compute_execution_scope, get_ready_nodes
from hypergraph.runners._shared.types import GraphState

# --- Nodes (mirrors the notebook) ---


@interrupt(output_name="user_input")
def ask(messages: list) -> None:
    return None  # always pause


@node(output_name="messages")
def add_user(messages: list, user_input: str) -> list:
    return [*messages, f"user: {user_input}"]


@node(output_name="assistant_text")
def llm(messages: list) -> str:
    return f"reply to {messages[-1]}"


@node(output_name="messages")
def add_assistant(messages: list, assistant_text: str) -> list:
    return [*messages, f"assistant: {assistant_text}"]


@route(targets=["ask", END])
def decide(messages: list) -> str:
    return END if sum(1 for m in messages if m.startswith("assistant: ")) >= 2 else "ask"


GRAPH = Graph(
    [ask, add_user, llm, add_assistant, decide],
    edges=[(add_user, llm), (add_assistant, decide)],
    name="repro",
    shared=["messages"],
    entrypoint="ask",
)


class TestInterruptSharedCycleScheduling:
    """Low-level: trace which nodes become ready at each superstep."""

    def test_ask_does_not_become_ready_before_pipeline_completes(self):
        """After add_user updates messages, ask should NOT be ready.

        The pipeline (llm → add_assistant → decide) must complete before
        ask can fire again.
        """
        scope = compute_execution_scope(GRAPH)
        state = GraphState()

        # Initial state: messages=[], user_input provided (resume scenario)
        state.update_value("messages", [])
        state.update_value("user_input", "hello")

        # Superstep 1: ask should be ready (entrypoint, first execution)
        ready = get_ready_nodes(
            GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]
        assert "ask" in ready_names

        # Simulate ask executing (skip path: returns user_input from state)
        from hypergraph.runners._shared.types import NodeExecution

        state.node_executions["ask"] = NodeExecution(
            node_name="ask",
            input_versions={"messages": state.get_version("messages")},
            outputs={"user_input": "hello"},
            output_versions={"user_input": state.get_version("user_input")},
        )

        # Superstep 2: add_user should be ready
        ready = get_ready_nodes(
            GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]
        assert "add_user" in ready_names

        # Simulate add_user executing: updates messages (shared)
        new_messages = ["user: hello"]
        state.update_value("messages", new_messages)
        state.node_executions["add_user"] = NodeExecution(
            node_name="add_user",
            input_versions={
                "messages": 1,  # consumed version before update
                "user_input": state.get_version("user_input"),
            },
            outputs={"messages": new_messages},
            output_versions={"messages": state.get_version("messages")},
        )

        # Superstep 3: llm should be ready. ask should NOT be ready yet.
        ready = get_ready_nodes(
            GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]

        assert "llm" in ready_names, f"llm should be ready but got {ready_names}"
        assert "ask" not in ready_names, (
            f"BUG: ask became ready before pipeline completed. "
            f"Ready nodes: {ready_names}. "
            f"Expected only llm (and possibly others in pipeline), not ask."
        )

    def test_llm_not_stale_after_downstream_shared_update(self):
        """After add_assistant updates messages, llm should NOT re-fire.

        add_assistant is downstream of llm — only add_user (upstream via
        ordering edge) should trigger llm's staleness.
        """
        scope = compute_execution_scope(GRAPH)
        state = GraphState()
        from hypergraph.runners._shared.types import NodeExecution

        state.update_value("messages", [])
        state.update_value("user_input", "hello")

        # ask executes (skip)
        state.node_executions["ask"] = NodeExecution(
            node_name="ask",
            input_versions={"messages": state.get_version("messages")},
            outputs={"user_input": "hello"},
            output_versions={"user_input": state.get_version("user_input")},
        )

        # add_user executes
        v_before = state.get_version("messages")
        state.update_value("messages", ["user: hello"])
        state.node_executions["add_user"] = NodeExecution(
            node_name="add_user",
            input_versions={"messages": v_before, "user_input": state.get_version("user_input")},
            outputs={"messages": ["user: hello"]},
            output_versions={"messages": state.get_version("messages")},
        )

        # llm executes
        state.update_value("assistant_text", "reply")
        state.node_executions["llm"] = NodeExecution(
            node_name="llm",
            input_versions={"messages": state.get_version("messages")},
            outputs={"assistant_text": "reply"},
            output_versions={"assistant_text": state.get_version("assistant_text")},
        )

        # add_assistant executes — updates messages (downstream of llm)
        v_before_asst = state.get_version("messages")
        state.update_value("messages", ["user: hello", "assistant: reply"])
        state.node_executions["add_assistant"] = NodeExecution(
            node_name="add_assistant",
            input_versions={
                "messages": v_before_asst,
                "assistant_text": state.get_version("assistant_text"),
            },
            outputs={"messages": ["user: hello", "assistant: reply"]},
            output_versions={"messages": state.get_version("messages")},
        )

        # Now: llm should NOT be ready (downstream update shouldn't trigger it)
        ready = get_ready_nodes(
            GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]
        assert "llm" not in ready_names, f"BUG: llm re-triggered by downstream shared param update. Ready: {ready_names}"
        # decide should be ready (add_assistant is its ordering predecessor)
        assert "decide" in ready_names


class TestInterruptSharedCycleE2E:
    """End-to-end: stateless resume should produce assistant messages."""

    @pytest.mark.asyncio
    async def test_resume_completes_full_pipeline(self):
        """After providing user_input, the full pipeline should run before ask fires again."""
        runner = AsyncRunner()

        # Run 1: pauses at ask
        r1 = await runner.run(GRAPH, {"messages": []})
        assert r1.paused
        assert r1.pause.node_name == "ask"

        # Run 2: provide user_input — pipeline should complete, then ask fires again
        r2 = await runner.run(
            GRAPH,
            {"messages": [], "user_input": "hello"},
        )
        assert r2.paused, "Should pause at ask again after pipeline completes"

        # The critical check: messages should contain BOTH user and assistant entries
        pause_messages = r2.pause.value
        assert any("user:" in m for m in pause_messages), f"Expected user message in {pause_messages}"
        assert any("assistant:" in m for m in pause_messages), (
            f"BUG: No assistant message — pipeline didn't complete before ask re-fired. Messages: {pause_messages}"
        )
