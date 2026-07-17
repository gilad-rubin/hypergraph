"""Tests for gate activation and scheduling rules.

Covers two key behaviors:
1. Pending routing decisions re-trigger gate-controlled nodes (even inputless ones)
2. Entrypoint-aware default_open prevents premature firing of gate targets
"""

import pytest

from hypergraph import END, AsyncRunner, Graph, interrupt, node, route
from hypergraph.runners._shared import readiness as readiness_module
from hypergraph.runners._shared.readiness import gate_permits_startup, get_ready_nodes
from hypergraph.runners._shared.scheduling import compute_execution_scope
from hypergraph.runners._shared.state import GraphState, NodeExecution
from tests._interrupt_questions import StringQuestion

# --- Fixtures ---


@interrupt(answer_name="user_input")
def wait_for_user() -> StringQuestion:
    return StringQuestion(prompt="What should the user say?")


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


class TestGatePermitsStartupTable:
    """The flat predicate is executable form of the shared gate table."""

    @pytest.mark.parametrize(
        (
            "gate_executed",
            "node_executed",
            "default_open",
            "entrypoints",
            "decision",
            "gate_activated",
            "expected",
        ),
        [
            pytest.param(False, False, True, None, None, True, True, id="never-never-default-open"),
            pytest.param(False, False, True, None, None, False, False, id="gate-itself-blocked-transitive"),
            pytest.param(False, False, True, ("target",), None, True, True, id="entrypoint-target-starts"),
            pytest.param(False, False, True, ("target",), None, False, True, id="entrypoint-target-exempt-from-transitive"),
            pytest.param(False, False, True, ("other",), None, True, False, id="non-entrypoint-waits"),
            pytest.param(False, False, False, None, None, True, False, id="default-closed-waits"),
            pytest.param(True, False, False, ("other",), "target", True, True, id="decision-target-wins"),
            pytest.param(True, False, False, ("other",), "target", False, False, id="orphaned-decision-blocks"),
            pytest.param(True, True, True, None, "other", True, False, id="decision-other-blocks"),
            pytest.param(True, False, True, None, None, True, False, id="stale-cleared-decision-blocks"),
        ],
    )
    def test_canonical_decision_table(
        self,
        gate_executed,
        node_executed,
        default_open,
        entrypoints,
        decision,
        gate_activated,
        expected,
    ):
        assert (
            gate_permits_startup(
                "target",
                decision=decision,
                gate_executed=gate_executed,
                node_executed=node_executed,
                default_open=default_open,
                entrypoints=entrypoints,
                gate_activated=gate_activated,
            )
            is expected
        )

    @pytest.mark.parametrize(
        ("decision", "expected"),
        [
            pytest.param("target", True, id="string-target"),
            pytest.param("other", False, id="string-other"),
            pytest.param(["other", "target"], True, id="list-includes-target"),
            pytest.param(["other"], False, id="list-excludes-target"),
            pytest.param(END, False, id="end-is-terminal"),
        ],
    )
    def test_executed_decision_shapes(self, decision, expected):
        assert (
            gate_permits_startup(
                "target",
                decision=decision,
                gate_executed=True,
                node_executed=True,
                default_open=False,
                entrypoints=("other",),
            )
            is expected
        )

    def test_missing_controlling_gate_is_ignored(self):
        @node(output_name="result")
        def target() -> int:
            return 1

        graph = Graph([target])
        graph.controlled_by[target.name] = ["missing_gate"]

        assert target.name not in readiness_module._get_activated_nodes(graph, GraphState())

    def test_multiple_controlling_gates_use_or_semantics(self):
        @node(output_name="result")
        def target() -> int:
            return 1

        @route(targets=["target"], default_open=False)
        def first_gate(seed: int) -> str:
            return "target"

        @route(targets=["target"], default_open=False)
        def second_gate(seed: int) -> str:
            return "target"

        graph = Graph([target, first_gate, second_gate])
        state = GraphState(
            values={"seed": 1},
            versions={"seed": 1},
            node_executions={
                second_gate.name: NodeExecution(
                    node_name=second_gate.name,
                    input_versions={"seed": 1},
                    outputs={},
                )
            },
            routing_decisions={second_gate.name: target.name},
        )

        assert target.name in readiness_module._get_activated_nodes(graph, state)

    @pytest.mark.parametrize(
        ("decision", "expected_present", "expected_activated"),
        [
            pytest.param("target", False, False, id="stale-routing-decision-cleared-first"),
            pytest.param(END, True, False, id="terminal-end-never-cleared"),
        ],
    )
    def test_stale_clearing_precedes_activation_and_preserves_end(
        self,
        decision,
        expected_present,
        expected_activated,
    ):
        @node(output_name="result")
        def target() -> int:
            return 1

        @route(targets=["target", END], default_open=False)
        def gate(seed: int):
            return decision

        graph = Graph([target, gate])
        state = GraphState()
        state.update_value("seed", 1)
        state.update_value("seed", 2)
        state.node_executions[gate.name] = NodeExecution(
            node_name=gate.name,
            input_versions={"seed": 1},
            outputs={},
        )
        state.routing_decisions[gate.name] = decision

        activated = readiness_module._get_activated_nodes(graph, state)

        assert (gate.name in state.routing_decisions) is expected_present
        assert (target.name in activated) is expected_activated


class TestTransitiveChainActivation:
    """Blocking propagates through chained gates via the activation fixpoint."""

    @staticmethod
    def _chain_graph():
        @node(output_name="result")
        def target(x: int) -> int:
            return x

        @route(targets=["target", END])
        def gate_b(x: int) -> str:
            return "target"

        @route(targets=["gate_b", END])
        def gate_a(x: int) -> str:
            return "gate_b"

        return Graph([gate_a, gate_b, target])

    def test_undecided_chain_is_fully_open_on_first_pass(self):
        graph = self._chain_graph()
        activated = readiness_module._get_activated_nodes(graph, GraphState())
        assert activated == {"gate_a", "gate_b", "target"}

    def test_end_at_head_deactivates_whole_chain(self):
        graph = self._chain_graph()
        state = GraphState(
            values={"x": 1},
            versions={"x": 1},
            node_executions={
                "gate_a": NodeExecution(
                    node_name="gate_a",
                    input_versions={"x": 1},
                    outputs={},
                )
            },
            routing_decisions={"gate_a": END},
        )

        activated = readiness_module._get_activated_nodes(graph, state)

        assert "gate_b" not in activated, "END at gate_a blocks gate_b"
        assert "target" not in activated, "blocking must propagate through gate_b"
        assert "gate_a" in activated

    def test_decision_for_mid_gate_keeps_chain_alive(self):
        graph = self._chain_graph()
        state = GraphState(
            values={"x": 1},
            versions={"x": 1},
            node_executions={
                "gate_a": NodeExecution(
                    node_name="gate_a",
                    input_versions={"x": 1},
                    outputs={},
                )
            },
            routing_decisions={"gate_a": "gate_b"},
        )

        activated = readiness_module._get_activated_nodes(graph, state)

        assert "gate_b" in activated, "gate_a's decision activates gate_b"
        assert "target" in activated, "default_open flows through the activated gate_b"


class TestOrphanedDecisionClearing:
    """A pending decision is live only while its gate is not cut off upstream."""

    @staticmethod
    def _executed(node_name: str, input_versions: dict | None = None) -> NodeExecution:
        return NodeExecution(node_name=node_name, input_versions=input_versions or {"x": 1}, outputs={})

    @staticmethod
    def _chain_graph():
        @node(output_name="result")
        def target(x: int) -> int:
            return x

        @route(targets=["target", END])
        def gate_b(x: int) -> str:
            return "target"

        @route(targets=["gate_b", END])
        def gate_a(x: int) -> str:
            return "gate_b"

        return Graph([gate_a, gate_b, target])

    def _state(self, decisions: dict, executed: tuple[str, ...]) -> GraphState:
        return GraphState(
            values={"x": 1},
            versions={"x": 1},
            node_executions={name: self._executed(name) for name in executed},
            routing_decisions=dict(decisions),
        )

    def test_explicit_upstream_end_orphans_pending_decision(self):
        graph = self._chain_graph()
        state = self._state({"gate_a": END, "gate_b": "target"}, executed=("gate_a", "gate_b"))

        activated = readiness_module._get_activated_nodes(graph, state)

        assert "gate_b" not in state.routing_decisions, "orphaned decision must be dropped"
        assert "target" not in activated
        assert state.routing_decisions.get("gate_a") is END, "END stays as terminal marker"

    def test_consumed_upstream_selection_keeps_pending_decision(self):
        graph = self._chain_graph()
        # gate_a executed and its selection was consumed (no current decision).
        state = self._state({"gate_b": "target"}, executed=("gate_a", "gate_b"))

        activated = readiness_module._get_activated_nodes(graph, state)

        assert state.routing_decisions.get("gate_b") == "target", "consumed upstream keeps decision live"
        assert "target" in activated

    def test_orphaning_is_transitive_across_gates(self):
        @node(output_name="result")
        def target(x: int) -> int:
            return x

        @route(targets=["target", END])
        def gate_c(x: int) -> str:
            return "target"

        @route(targets=["gate_c", END])
        def gate_b(x: int) -> str:
            return "gate_c"

        @route(targets=["gate_b", END])
        def gate_a(x: int) -> str:
            return "gate_b"

        graph = Graph([gate_a, gate_b, gate_c, target])
        state = self._state(
            {"gate_a": END, "gate_b": "gate_c", "gate_c": "target"},
            executed=("gate_a", "gate_b", "gate_c"),
        )

        activated = readiness_module._get_activated_nodes(graph, state)

        assert "gate_b" not in state.routing_decisions
        assert "gate_c" not in state.routing_decisions, "orphaning must cascade through the chain"
        assert "target" not in activated

    def test_cut_gate_keeps_its_end_decision(self):
        graph = self._chain_graph()
        state = self._state({"gate_a": END, "gate_b": END}, executed=("gate_a", "gate_b"))

        readiness_module._get_activated_nodes(graph, state)

        assert state.routing_decisions.get("gate_b") is END, "END activates nothing and is kept"


class TestActivationCostLinear:
    """C10: activation is a worklist — linear predicate calls on long chains."""

    N = 2000

    @classmethod
    def _chain_nodes(cls):
        from hypergraph.nodes.gate import RouteNode

        def make_router(next_name: str):
            def router(x: int) -> str:
                return next_name

            return router

        gates = [RouteNode(make_router(f"g{i + 1}"), targets=[f"g{i + 1}"], name=f"g{i}") for i in range(cls.N - 1)]

        @node(output_name="terminal_out")
        def terminal(x: int) -> int:
            return x

        gates.append(RouteNode(make_router("terminal"), targets=["terminal"], name=f"g{cls.N - 1}"))
        return [*gates, terminal]

    @pytest.mark.parametrize("order", ["forward", "reversed"])
    def test_end_at_head_costs_linear_predicate_calls(self, order, monkeypatch):
        nodes = self._chain_nodes()
        graph = Graph(nodes if order == "forward" else list(reversed(nodes)))
        state = GraphState(
            values={"x": 1},
            versions={"x": 1},
            node_executions={"g0": NodeExecution(node_name="g0", input_versions={"x": 1}, outputs={})},
            routing_decisions={"g0": END},
        )

        calls = {"count": 0}
        real_predicate = readiness_module.gate_permits_startup

        def counting_predicate(*args, **kwargs):
            calls["count"] += 1
            return real_predicate(*args, **kwargs)

        monkeypatch.setattr(readiness_module, "gate_permits_startup", counting_predicate)

        activated = readiness_module._get_activated_nodes(graph, state)

        assert activated == {"g0"}, "END at the head deactivates the whole chain"
        assert calls["count"] <= 5 * self.N, f"expected linear predicate calls, got {calls['count']}"


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
        try:
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
        finally:
            await cp.close()

    @pytest.mark.asyncio
    async def test_single_turn_terminates(self, tmp_path):
        from hypergraph.checkpointers import SqliteCheckpointer

        cp = SqliteCheckpointer(str(tmp_path / "chat.db"))
        try:
            runner = AsyncRunner(checkpointer=cp)
            graph = CHAT_GRAPH.bind(messages=[], max_turns=1)

            r = await runner.run(graph, workflow_id="t", user_input="one shot")
            assert not r.paused
            assert len(r["messages"]) == 2
        finally:
            await cp.close()
