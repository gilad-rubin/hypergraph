"""Tests for SCC-based execution planning and local execution."""

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, node
from hypergraph.nodes.gate import END, route
from hypergraph.runners._shared.helpers import build_execution_plan, compute_execution_scope


class TestExecutionPlan:
    """Static SCC planning behavior."""

    def test_gate_driven_cycle_is_one_cyclic_component(self):
        @node(output_name="count")
        def counter(count: int) -> int:
            return count + 1

        @route(targets=["counter", END])
        def decide(count: int) -> str:
            return "counter" if count < 3 else END

        graph = Graph([counter, decide], entrypoint="counter")

        plan = build_execution_plan(graph)

        assert len(plan) == 1
        assert plan[0].node_names == ("counter", "decide")
        assert plan[0].is_cyclic is True

    def test_control_edge_orders_gate_before_target(self):
        @node(output_name="x")
        def start(seed: int) -> int:
            return seed

        @node(output_name="result")
        def target(x: int) -> int:
            return x * 2

        @route(targets=["target", END])
        def decide(x: int) -> str:
            return "target"

        graph = Graph([start, target, decide])

        plan = build_execution_plan(graph)

        assert [component.node_names for component in plan] == [
            ("start",),
            ("decide",),
            ("target",),
        ]


class TestExecutionPlanRunner:
    """Runtime behavior when walking SCCs to quiescence."""

    def test_gate_runs_before_target_even_when_target_declared_first(self):
        trace: list[str] = []

        @node(output_name="x")
        def start(seed: int) -> int:
            trace.append("start")
            return seed

        @node(output_name="result")
        def target(x: int) -> int:
            trace.append("target")
            return x * 2

        @route(targets=["target", END])
        def decide(x: int) -> str:
            trace.append("decide")
            return "target"

        graph = Graph([start, target, decide])
        result = SyncRunner().run(graph, {"seed": 4})

        assert result["result"] == 8
        assert trace == ["start", "decide", "target"]

    def test_downstream_node_waits_for_cyclic_component_to_quiesce(self):
        trace: list[str] = []

        @node(output_name="count")
        def counter(count: int) -> int:
            trace.append(f"counter:{count}")
            return count + 1

        @route(targets=["counter", END])
        def decide(count: int, limit: int = 3) -> str:
            trace.append(f"decide:{count}")
            return "counter" if count < limit else END

        @node(output_name="final")
        def finish(count: int) -> int:
            trace.append(f"finish:{count}")
            return count * 10

        graph = Graph([counter, decide, finish], entrypoint="counter")
        result = SyncRunner().run(graph, {"count": 0, "limit": 3})

        assert result["count"] == 3
        assert result["final"] == 30
        assert trace[-1] == "finish:3"
        assert "finish:1" not in trace
        assert "finish:2" not in trace

    def test_scope_reuses_planner_components(self):
        @node(output_name="count")
        def counter(count: int) -> int:
            return count + 1

        @route(targets=["counter", END])
        def decide(count: int) -> str:
            return END if count >= 1 else "counter"

        graph = Graph([counter, decide], entrypoint="counter")

        scope = compute_execution_scope(graph)

        assert len(scope.execution_plan) == 1
        assert scope.execution_plan[0].node_names == ("counter", "decide")

    def test_unrelated_downstream_work_finishes_while_cycle_iterates(self):
        @node(output_name="count")
        def cycle_node(count: int) -> int:
            return count + 1

        @route(targets=["cycle_node", END])
        def cycle_gate(count: int) -> str:
            return "cycle_node"

        @node(output_name="a")
        def branch_start(x: int) -> int:
            return x + 1

        @node(output_name="done")
        def branch_finish(a: int) -> int:
            return a * 10

        graph = Graph([cycle_node, cycle_gate, branch_start, branch_finish], entrypoint=["cycle_node", "branch_start"])
        result = SyncRunner().run(graph, {"count": 0, "x": 2}, max_iterations=3, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert result["a"] == 3
        assert result["done"] == 30

    def test_independent_cycles_use_independent_iteration_budgets(self):
        @node(output_name="count_a")
        def cycle_a(count_a: int) -> int:
            return count_a + 1

        @route(targets=["cycle_a", END])
        def gate_a(count_a: int) -> str:
            return "cycle_a"

        @node(output_name="count_b")
        def cycle_b(count_b: int) -> int:
            return count_b + 1

        @route(targets=["cycle_b", END])
        def gate_b(count_b: int) -> str:
            return "cycle_b"

        graph = Graph([cycle_a, gate_a, cycle_b, gate_b], entrypoint=["cycle_a", "cycle_b"])
        result = SyncRunner().run(graph, {"count_a": 0, "count_b": 10}, max_iterations=1, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert result["count_a"] == 1
        assert result["count_b"] == 11

    async def test_async_unrelated_downstream_work_finishes_while_cycle_iterates(self):
        @node(output_name="count")
        async def cycle_node(count: int) -> int:
            return count + 1

        @route(targets=["cycle_node", END])
        def cycle_gate(count: int) -> str:
            return "cycle_node"

        @node(output_name="a")
        async def branch_start(x: int) -> int:
            return x + 1

        @node(output_name="done")
        async def branch_finish(a: int) -> int:
            return a * 10

        graph = Graph([cycle_node, cycle_gate, branch_start, branch_finish], entrypoint=["cycle_node", "branch_start"])
        result = await AsyncRunner().run(graph, {"count": 0, "x": 2}, max_iterations=3, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert result["a"] == 3
        assert result["done"] == 30
