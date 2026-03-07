"""Tests for gate activation and scheduling rules.

Covers two key behaviors:
1. Pending routing decisions re-trigger gate-controlled nodes (even inputless ones)
2. Entrypoint-aware default_open prevents premature firing of gate targets
"""

import pytest

from hypergraph import END, AsyncRunner, Graph, interrupt, node, route
from hypergraph.runners._shared.helpers import (
    compute_execution_scope,
    get_ready_nodes,
)
from hypergraph.runners._shared.types import GraphState, NodeExecution

# --- Fixtures ---


@interrupt(output_name="user_input")
def wait_for_user() -> None:
    return None


@node(output_name="messages")
def add_user_message(messages: list, user_input: str) -> list:
    return [*messages, {"role": "user", "content": user_input}]


@node(output_name="assistant_text")
def llm_reply(messages: list) -> str:
    last = messages[-1]["content"] if messages else "nothing"
    return f"echo: {last}"


@node(output_name="messages")
def add_assistant(messages: list, assistant_text: str) -> list:
    return [*messages, {"role": "assistant", "content": assistant_text}]


@route(targets=["wait_for_user", END])
def check_done(messages: list, max_turns: int) -> str:
    turns = sum(1 for m in messages if m["role"] == "assistant")
    return END if turns >= max_turns else "wait_for_user"


CHAT_GRAPH = Graph(
    [wait_for_user, add_user_message, llm_reply, add_assistant, check_done],
    edges=[
        (add_user_message, llm_reply),
        (llm_reply, add_assistant),
        (add_assistant, check_done),
    ],
    name="chat",
    shared=["messages"],
    entrypoint="add_user_message",
)


class TestPendingActivationRetrigger:
    """A routing decision targeting a node is a re-execution signal.

    Without this, inputless gate targets (like interrupt nodes) that already
    executed would never be "stale" and would not re-fire on subsequent cycles.
    """

    def test_inputless_gate_target_refires_after_routing(self):
        """After check_done routes to wait_for_user, it should become ready."""
        scope = compute_execution_scope(CHAT_GRAPH)
        state = GraphState()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        state.update_value("messages", msgs)
        state.update_value("user_input", "hi")
        state.update_value("assistant_text", "hello")

        # Simulate: all nodes have executed once
        state.node_executions["wait_for_user"] = NodeExecution(
            node_name="wait_for_user",
            input_versions={},
            outputs={"user_input": "hi"},
            output_versions={"user_input": 1},
        )
        state.node_executions["add_user_message"] = NodeExecution(
            node_name="add_user_message",
            input_versions={"messages": 1, "user_input": 1},
            outputs={"messages": msgs},
            output_versions={"messages": state.get_version("messages")},
        )
        state.node_executions["llm_reply"] = NodeExecution(
            node_name="llm_reply",
            input_versions={"messages": state.get_version("messages")},
            outputs={"assistant_text": "hello"},
            output_versions={"assistant_text": state.get_version("assistant_text")},
        )
        state.node_executions["add_assistant"] = NodeExecution(
            node_name="add_assistant",
            input_versions={
                "messages": state.get_version("messages"),
                "assistant_text": state.get_version("assistant_text"),
            },
            outputs={"messages": msgs},
            output_versions={"messages": state.get_version("messages")},
        )
        state.node_executions["check_done"] = NodeExecution(
            node_name="check_done",
            input_versions={
                "messages": state.get_version("messages"),
                "max_turns": 0,
            },
            outputs={},
            output_versions={},
        )

        # check_done routes to wait_for_user
        state.routing_decisions["check_done"] = "wait_for_user"

        ready = get_ready_nodes(
            CHAT_GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]
        assert "wait_for_user" in ready_names, f"wait_for_user should be ready after routing decision. Got: {ready_names}"

    def test_no_routing_decision_no_refire(self):
        """Without a routing decision, an already-executed gate target stays idle."""
        scope = compute_execution_scope(CHAT_GRAPH)
        state = GraphState()
        state.update_value("messages", [])
        state.update_value("user_input", "hi")

        # wait_for_user already executed, no routing decision
        state.node_executions["wait_for_user"] = NodeExecution(
            node_name="wait_for_user",
            input_versions={},
            outputs={"user_input": "hi"},
            output_versions={"user_input": 1},
        )

        ready = get_ready_nodes(
            CHAT_GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]
        assert "wait_for_user" not in ready_names


class TestEntrypointAwareDefaultOpen:
    """With entrypoints configured, non-entrypoint gate targets should not
    fire on first pass via default_open.

    Without this fix, an inputless gate target (e.g. an interrupt) fires
    before the entrypoint node, because default_open=True allows it through
    and it has no data predecessors to block it.
    """

    def test_gate_target_blocked_before_gate_fires(self):
        """wait_for_user should NOT be ready on first pass (entrypoint is add_user_message)."""
        scope = compute_execution_scope(CHAT_GRAPH)
        state = GraphState()
        state.update_value("messages", [])
        state.update_value("user_input", "hi")

        ready = get_ready_nodes(
            CHAT_GRAPH,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]

        assert "add_user_message" in ready_names, f"Entrypoint should be ready. Got: {ready_names}"
        assert "wait_for_user" not in ready_names, f"Gate target should not fire before gate on first pass. Got: {ready_names}"

    def test_entrypoint_node_that_is_gate_target_still_fires(self):
        """A gate target that IS the entrypoint should fire on first pass."""

        @node(output_name="x")
        def step_a() -> int:
            return 1

        @route(targets=["step_a", END])
        def gate(x: int) -> str:
            return END

        # step_a is both entrypoint and gate target
        g = Graph([step_a, gate], entrypoint="step_a")
        scope = compute_execution_scope(g)
        state = GraphState()

        ready = get_ready_nodes(
            g,
            state,
            active_nodes=scope.active_nodes,
            startup_predecessors=scope.startup_predecessors,
        )
        ready_names = [n.name for n in ready]
        assert "step_a" in ready_names, f"Entrypoint gate target should fire on first pass. Got: {ready_names}"


aiosqlite = pytest.importorskip("aiosqlite")


class TestChatAppE2E:
    """End-to-end: multi-turn chat with interrupt pause/resume.

    Uses a checkpointer because multi-turn requires durable state:
    each .run() call is a separate HTTP-request-like invocation.
    """

    @pytest.mark.asyncio
    async def test_multi_turn_pause_resume(self, tmp_path):
        from hypergraph.checkpointers import SqliteCheckpointer

        cp = SqliteCheckpointer(str(tmp_path / "chat.db"))
        runner = AsyncRunner(checkpointer=cp)
        graph = CHAT_GRAPH.bind(messages=[], max_turns=3)

        r1 = await runner.run(graph, workflow_id="t", user_input="hello")
        assert r1.paused
        msgs = r1["messages"]
        assert len(msgs) == 2  # user + assistant
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

        r2 = await runner.run(graph, workflow_id="t", user_input="more")
        assert r2.paused
        assert len(r2["messages"]) == 4

        r3 = await runner.run(graph, workflow_id="t", user_input="last")
        assert not r3.paused  # max_turns=3 reached
        assert len(r3["messages"]) == 6

    @pytest.mark.asyncio
    async def test_single_turn_terminates(self, tmp_path):
        from hypergraph.checkpointers import SqliteCheckpointer

        cp = SqliteCheckpointer(str(tmp_path / "chat.db"))
        runner = AsyncRunner(checkpointer=cp)
        graph = CHAT_GRAPH.bind(messages=[], max_turns=1)

        r = await runner.run(graph, workflow_id="t", user_input="one shot")
        assert not r.paused
        assert len(r["messages"]) == 2
