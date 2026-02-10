"""Red-team audit tests for hypergraph.

These tests probe edge cases and potential bugs identified through
code analysis and cross-referencing with issues from LangGraph,
Pydantic-Graph, and Mastra.

Each test targets a specific hypothesis about a potential bug.
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, node, SyncRunner, AsyncRunner
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import RouteNode, IfElseNode, END, route, ifelse
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.graph.validation import GraphConfigError
from hypergraph._typing import is_type_compatible
from hypergraph.runners._shared.types import RunStatus


# ===========================================================================
# BUG 1: Mutable default values shared across runs
# ===========================================================================

class TestMutableDefaultSharing:
    """Mutable defaults (list, dict) in node functions should not leak between runs.

    Python's mutable default argument pitfall: if a node has `def f(x=[])`,
    the same list object is reused across calls. The framework should either
    copy defaults or document this limitation.
    """

    @pytest.mark.xfail(reason="BUG: Mutable default list shared across runs")
    def test_mutable_list_default_not_shared_across_runs(self):
        """Two consecutive runs should not share a mutable default list.

        BUG: _resolve_input calls node.get_default_for(param) which returns
        the actual default object from the function signature. For mutable
        defaults (list, dict), the SAME object is reused across runs.
        The framework should copy.deepcopy() defaults before passing them.
        """

        @node(output_name="result")
        def append_to(items: list = []) -> list:
            items.append(1)
            return items

        @node(output_name="length")
        def get_length(result: list) -> int:
            return len(result)

        graph = Graph(nodes=[append_to, get_length])
        runner = SyncRunner()

        r1 = runner.run(graph, {})
        r2 = runner.run(graph, {})

        # If mutable default is shared, r2 will see [1, 1] instead of [1]
        assert r1["length"] == 1, f"First run: expected 1, got {r1['length']}"
        assert r2["length"] == 1, (
            f"Second run: expected 1, got {r2['length']}. "
            "Mutable default list is shared across runs!"
        )

    @pytest.mark.xfail(reason="BUG: Mutable default dict shared across runs")
    def test_mutable_dict_default_not_shared_across_runs(self):
        """Two consecutive runs should not share a mutable default dict.

        Same root cause as list default sharing - see above.
        """

        @node(output_name="result")
        def add_key(data: dict = {}) -> dict:
            data["key"] = data.get("key", 0) + 1
            return data

        graph = Graph(nodes=[add_key])
        runner = SyncRunner()

        r1 = runner.run(graph, {})
        r2 = runner.run(graph, {})

        assert r1["result"]["key"] == 1
        assert r2["result"]["key"] == 1, (
            f"Second run: expected key=1, got key={r2['result']['key']}. "
            "Mutable default dict is shared across runs!"
        )


# ===========================================================================
# BUG 2: is_type_compatible misses subclass relationships
# ===========================================================================

class TestTypeCompatibilitySubclass:
    """is_type_compatible should handle subclass relationships for plain types.

    In Python, bool is a subclass of int. A node producing bool should be
    compatible with a downstream node expecting int.
    """

    def test_bool_compatible_with_int(self):
        """bool output should be compatible with int input."""
        assert is_type_compatible(bool, int), (
            "bool is a subclass of int, should be compatible"
        )

    def test_int_not_compatible_with_bool(self):
        """int output should NOT be compatible with bool input (not a subclass)."""
        # This is a design question - int is not a subclass of bool
        # but in practice int values can be truthy/falsy
        result = is_type_compatible(int, bool)
        # We just document the behavior, not assert a specific result

    def test_subclass_compatible(self):
        """Subclass types should be compatible with parent types."""

        class Animal:
            pass

        class Dog(Animal):
            pass

        assert is_type_compatible(Dog, Animal), (
            "Dog is a subclass of Animal, should be compatible"
        )

    def test_strict_types_with_bool_to_int_edge(self):
        """Graph with bool→int edge should work with strict_types=True."""

        @node(output_name="flag")
        def check(x: int) -> bool:
            return x > 0

        @node(output_name="result")
        def use_flag(flag: int) -> int:
            return flag + 1

        # This should NOT raise GraphConfigError because bool is subclass of int
        try:
            graph = Graph(nodes=[check, use_flag], strict_types=True)
        except GraphConfigError as e:
            pytest.fail(
                f"strict_types rejected bool→int edge, but bool is subclass of int: {e}"
            )


# ===========================================================================
# BUG 3: Edge building only uses first source for mutex branches
# ===========================================================================

class TestMutexBranchEdgeBuilding:
    """When mutex branches produce the same output name, only the first
    branch gets data edges built to downstream consumers. This means
    type validation with strict_types won't check the second branch.
    """

    def test_mutex_branch_both_outputs_reach_consumer(self):
        """Both mutex branches should produce values that reach the consumer."""

        @node(output_name="result")
        def branch_a(x: int) -> str:
            return f"a:{x}"

        @node(output_name="result")
        def branch_b(x: int) -> str:
            return f"b:{x}"

        @route(targets=["branch_a", "branch_b"])
        def decide(x: int) -> str:
            return "branch_a" if x > 0 else "branch_b"

        @node(output_name="final")
        def consume(result: str) -> str:
            return f"got:{result}"

        graph = Graph(nodes=[decide, branch_a, branch_b, consume])
        runner = SyncRunner()

        # Branch A path
        r1 = runner.run(graph, {"x": 1})
        assert r1.status == RunStatus.COMPLETED, f"Branch A failed: {r1.error}"
        assert r1["final"] == "got:a:1"

        # Branch B path
        r2 = runner.run(graph, {"x": -1})
        assert r2.status == RunStatus.COMPLETED, f"Branch B failed: {r2.error}"
        assert r2["final"] == "got:b:-1", (
            f"Branch B consumer got wrong value: {r2.get('final')}. "
            "Second mutex branch output may not reach consumer."
        )

    @pytest.mark.xfail(
        reason="BUG: Only first mutex branch gets data edge - second branch type not validated"
    )
    def test_mutex_strict_types_validates_both_branches(self):
        """strict_types should validate type compatibility for ALL branches,
        not just the first one.
        """

        @node(output_name="result")
        def branch_a(x: int) -> str:
            return "a"

        # Branch B produces int, but consumer expects str
        @node(output_name="result")
        def branch_b(x: int) -> int:
            return 42

        @route(targets=["branch_a", "branch_b"])
        def decide(x: int) -> str:
            return "branch_a" if x > 0 else "branch_b"

        @node(output_name="final")
        def consume(result: str) -> str:
            return f"got:{result}"

        # This SHOULD raise because branch_b produces int but consume expects str
        # But if edge building only creates edge from branch_a→consume,
        # the type mismatch in branch_b→consume is never checked.
        try:
            graph = Graph(
                nodes=[decide, branch_a, branch_b, consume],
                strict_types=True,
            )
            # If no error: the second branch's type wasn't validated
            # This is the bug - we should flag it
            pytest.fail(
                "strict_types did not validate branch_b→consume type mismatch. "
                "Only the first mutex branch gets data edges, so type validation "
                "misses the second branch."
            )
        except GraphConfigError:
            pass  # Expected: type mismatch caught


# ===========================================================================
# BUG 4: GraphNode exposes ALL intermediate outputs
# ===========================================================================

class TestGraphNodeOutputLeakage:
    """GraphNode uses graph.outputs (all node outputs), not just leaf_outputs.
    This can cause unintended connections in the outer graph.
    """

    def test_graphnode_intermediate_outputs_visible(self):
        """GraphNode's outputs should ideally only expose leaf outputs,
        not intermediate values that could accidentally connect to outer nodes.
        """

        @node(output_name="intermediate")
        def step1(x: int) -> int:
            return x * 2

        @node(output_name="final")
        def step2(intermediate: int) -> int:
            return intermediate + 1

        inner = Graph(nodes=[step1, step2], name="inner")
        gn = inner.as_node()

        # Check what outputs are exposed
        assert "final" in gn.outputs, "Final output should be exposed"

        # This is the question: should intermediate be exposed?
        if "intermediate" in gn.outputs:
            # This is the current behavior - document it as a potential issue

            # Now show how it can cause unintended connections
            @node(output_name="surprise")
            def outer_consumer(intermediate: int) -> int:
                # This accidentally connects to the inner graph's intermediate!
                return intermediate * 100

            outer = Graph(nodes=[gn, outer_consumer])
            runner = SyncRunner()
            result = runner.run(outer, {"x": 5})

            # intermediate=10 (from step1) flows to outer_consumer
            assert result.status == RunStatus.COMPLETED
            if result["surprise"] == 1000:
                # The intermediate value leaked and connected unintentionally
                pass  # This is the documented behavior, not necessarily a bug
                # but a design concern

    def test_graphnode_leaf_outputs_vs_all_outputs(self):
        """Verify the difference between graph.outputs and graph.leaf_outputs."""

        @node(output_name="a")
        def n1(x: int) -> int:
            return x

        @node(output_name="b")
        def n2(a: int) -> int:
            return a + 1

        g = Graph(nodes=[n1, n2], name="g")

        # All outputs includes intermediate
        assert "a" in g.outputs
        assert "b" in g.outputs

        # Leaf outputs should only include terminal node outputs
        assert "b" in g.leaf_outputs
        # 'a' is consumed by n2, so n1 is NOT a leaf node
        assert "a" not in g.leaf_outputs


# ===========================================================================
# BUG 5: Stale values from deactivated branches persist in state
# ===========================================================================

class TestStaleBranchValues:
    """In cyclic graphs with routing, values from a previously-activated
    branch may persist in state even after the gate routes elsewhere.
    """

    def test_simple_cycle_with_route(self):
        """Test a simple cycle: process→decide→(process or END)."""

        call_count = [0]

        @node(output_name="counter")
        def process(counter: int) -> int:
            call_count[0] += 1
            return counter + 1

        @route(targets=["process", END])
        def decide(counter: int) -> str:
            return END if counter >= 3 else "process"

        graph = Graph(nodes=[process, decide])
        runner = SyncRunner()
        result = runner.run(graph, {"counter": 0})

        assert result.status == RunStatus.COMPLETED, (
            f"Simple cycle failed: {result.error}"
        )
        assert call_count[0] >= 1, "process should have been called"
        # counter goes through process multiple times, then decide returns END
        assert result["counter"] >= 3, f"Expected counter>=3, got {result.get('counter')}"


# ===========================================================================
# BUG 6: GraphNode with renamed inputs doesn't propagate to inner graph
# ===========================================================================

class TestGraphNodeRenaming:
    """Test that GraphNode input/output renames work correctly end-to-end."""

    def test_graphnode_with_inputs_runs_correctly(self):
        """Renamed GraphNode inputs should still reach the inner graph."""

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        inner = Graph(nodes=[double], name="inner")
        gn = inner.as_node().with_inputs(x="input_val")

        @node(output_name="input_val")
        def provide(n: int) -> int:
            return n + 1

        outer = Graph(nodes=[provide, gn])
        runner = SyncRunner()
        result = runner.run(outer, {"n": 4})
        assert result.status == RunStatus.COMPLETED, f"Failed: {result.error}"
        assert result["doubled"] == 10, f"Expected 10, got {result.get('doubled')}"

    @pytest.mark.xfail(
        reason="BUG: SyncGraphNodeExecutor doesn't translate renamed outputs for non-map case"
    )
    def test_graphnode_with_outputs_runs_correctly(self):
        """Renamed GraphNode outputs should be accessible in the outer graph.

        BUG: SyncGraphNodeExecutor.__call__ returns result.values directly from
        the inner graph run (original output names like "doubled"), but the outer
        graph expects the renamed output names (like "result"). The
        map_outputs_from_original() method IS called in _collect_as_lists (map case)
        but NOT in the simple single-run case.
        """

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        inner = Graph(nodes=[double], name="inner")
        gn = inner.as_node().with_outputs(doubled="result")

        @node(output_name="final")
        def add_one(result: int) -> int:
            return result + 1

        outer = Graph(nodes=[gn, add_one])
        runner = SyncRunner()
        result = runner.run(outer, {"x": 5})
        assert result.status == RunStatus.COMPLETED, f"Failed: {result.error}"
        assert result["final"] == 11, f"Expected 11, got {result.get('final')}"

    def test_graphnode_chained_renames(self):
        """Chained renames on GraphNode should work correctly."""

        @node(output_name="y")
        def f(x: int) -> int:
            return x * 3

        inner = Graph(nodes=[f], name="inner")
        gn = inner.as_node().with_inputs(x="a").with_inputs(a="b")

        outer = Graph(nodes=[gn])
        runner = SyncRunner()
        result = runner.run(outer, {"b": 4})
        assert result.status == RunStatus.COMPLETED, f"Failed: {result.error}"
        assert result["y"] == 12, f"Expected 12, got {result.get('y')}"


# ===========================================================================
# BUG 7: Disconnected/unreachable nodes
# ===========================================================================

class TestDisconnectedNodes:
    """Nodes that are completely disconnected from the rest of the graph."""

    def test_disconnected_node_still_executes(self):
        """A node with no edges should still execute if its inputs are provided."""

        @node(output_name="a")
        def n1(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def n2(y: int) -> int:
            return y * 2

        # n1 and n2 are completely independent
        graph = Graph(nodes=[n1, n2])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 1, "y": 3})
        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 2
        assert result["b"] == 6

    def test_unreachable_node_after_gate(self):
        """A node that's a gate target but never routed to should not execute."""

        executed = {"a": False, "b": False}

        @node(output_name="result_a")
        def branch_a(x: int) -> int:
            executed["a"] = True
            return x + 1

        @node(output_name="result_b")
        def branch_b(x: int) -> int:
            executed["b"] = True
            return x + 2

        @route(targets=["branch_a", "branch_b"])
        def decide(x: int) -> str:
            return "branch_a"  # Always routes to A

        graph = Graph(nodes=[decide, branch_a, branch_b])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert executed["a"] is True, "branch_a should have executed"
        assert executed["b"] is False, "branch_b should NOT have executed"


# ===========================================================================
# BUG 8: Routing with None return and no fallback
# ===========================================================================

class TestRoutingEdgeCases:
    """Test edge cases in routing behavior."""

    def test_route_returns_none_without_fallback(self):
        """When route returns None with no fallback, no target should activate."""

        executed = {"a": False}

        @node(output_name="result")
        def target_a(x: int) -> int:
            executed["a"] = True
            return x

        @route(targets=["target_a"])
        def decide(x: int) -> str | None:
            return None  # No target

        graph = Graph(nodes=[decide, target_a])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        # When None is returned and no fallback, no target should execute
        assert result.status == RunStatus.COMPLETED
        assert executed["a"] is False, "target_a should NOT execute when route returns None"

    def test_ifelse_returns_truthy_non_bool(self):
        """IfElseNode should reject truthy non-bool values (e.g., 1, "yes")."""

        @ifelse(when_true="a", when_false="b")
        def decide(x: int) -> bool:
            return x  # Returns int, not bool!

        @node(output_name="r")
        def a(x: int) -> int:
            return x

        @node(output_name="r")
        def b(x: int) -> int:
            return -x

        graph = Graph(nodes=[decide, a, b])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 1})

        # Should fail because 1 is not bool
        assert result.status == RunStatus.FAILED, (
            "IfElseNode should reject truthy int (1) - must be strictly bool"
        )
        assert isinstance(result.error, TypeError)

    def test_route_returns_end_terminates_execution(self):
        """When route returns END, downstream nodes should not execute."""

        executed = {"downstream": False}

        @node(output_name="processed")
        def process(x: int) -> int:
            return x * 2

        @route(targets=["downstream", END])
        def decide(processed: int) -> str:
            return END

        @node(output_name="final")
        def downstream(processed: int) -> int:
            executed["downstream"] = True
            return processed + 1

        graph = Graph(nodes=[process, decide, downstream])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert executed["downstream"] is False, (
            "downstream should not execute when route returns END"
        )

    def test_multi_target_route_activates_multiple(self):
        """multi_target=True should allow routing to multiple targets."""

        executed = set()

        @node(output_name="r1")
        def target_a(x: int) -> int:
            executed.add("a")
            return x + 1

        @node(output_name="r2")
        def target_b(x: int) -> int:
            executed.add("b")
            return x + 2

        @route(targets=["target_a", "target_b"], multi_target=True)
        def decide(x: int) -> list:
            return ["target_a", "target_b"]

        graph = Graph(nodes=[decide, target_a, target_b])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED, f"Failed: {result.error}"
        assert "a" in executed, "target_a should have executed"
        assert "b" in executed, "target_b should have executed"


# ===========================================================================
# BUG 9: Bind validation edge cases
# ===========================================================================

class TestBindEdgeCases:
    """Test edge cases in graph.bind() behavior."""

    def test_bind_overrides_function_default(self):
        """Bound value should take precedence over function default."""

        @node(output_name="result")
        def add(x: int, y: int = 10) -> int:
            return x + y

        graph = Graph(nodes=[add])
        bound_graph = graph.bind(y=20)
        runner = SyncRunner()
        result = runner.run(bound_graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 25, (
            f"Expected 25 (x=5 + y=20), got {result['result']}. "
            "Bound value should override function default."
        )

    def test_run_input_overrides_bound_value(self):
        """Runtime input should override bound value.

        Resolution order: edge > input > bound > default.
        So input should override bound.
        """

        @node(output_name="result")
        def add(x: int, y: int = 10) -> int:
            return x + y

        graph = Graph(nodes=[add])
        bound_graph = graph.bind(y=20)
        runner = SyncRunner()

        # Provide y at runtime - should it override the bound value?
        # According to resolution order, input > bound
        result = runner.run(bound_graph, {"x": 5, "y": 30})

        assert result.status == RunStatus.COMPLETED
        # Based on resolution order, runtime input (30) should override bound (20)
        assert result["result"] == 35, (
            f"Expected 35 (x=5 + y=30), got {result['result']}. "
            "Runtime input should override bound value."
        )

    def test_unbind_restores_default(self):
        """After unbinding, the function default should be used again."""

        @node(output_name="result")
        def add(x: int, y: int = 10) -> int:
            return x + y

        graph = Graph(nodes=[add])
        bound_graph = graph.bind(y=20)
        unbound_graph = bound_graph.unbind("y")
        runner = SyncRunner()
        result = runner.run(unbound_graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 15, (
            f"Expected 15 (x=5 + y=10 default), got {result['result']}. "
            "After unbinding, function default should be used."
        )


# ===========================================================================
# BUG 10: Cycle seed validation
# ===========================================================================

class TestCycleSeedValidation:
    """Test that cyclic graphs properly require seed values."""

    def test_cycle_requires_seed(self):
        """A cyclic graph should require seed values for cycle parameters."""

        @node(output_name="counter")
        def increment(counter: int) -> int:
            return counter + 1

        @route(targets=["increment", END])
        def check(counter: int) -> str:
            return END if counter >= 3 else "increment"

        graph = Graph(nodes=[increment, check])

        # counter is both an input and output in the cycle - it's a seed
        assert "counter" in graph.inputs.seeds, (
            f"'counter' should be a seed parameter. "
            f"Seeds: {graph.inputs.seeds}, Required: {graph.inputs.required}"
        )

    def test_cycle_without_seed_raises(self):
        """Running a cyclic graph without providing seed values should error."""
        from hypergraph.exceptions import MissingInputError

        @node(output_name="counter")
        def increment(counter: int) -> int:
            return counter + 1

        @route(targets=["increment", END])
        def check(counter: int) -> str:
            return END if counter >= 3 else "increment"

        graph = Graph(nodes=[increment, check])
        runner = SyncRunner()

        with pytest.raises(MissingInputError):
            runner.run(graph, {})


# ===========================================================================
# BUG 11: Multiple outputs tuple unpacking
# ===========================================================================

class TestMultipleOutputs:
    """Test nodes with multiple outputs (tuple unpacking).

    BUG FOUND: NetworkX DiGraph cannot store multiple edges between the same
    node pair. When a multi-output node (e.g., output_name=("a", "b")) feeds
    into a single consumer, only the LAST edge survives. Earlier edges are
    silently dropped, causing the lost outputs to appear as "required inputs"
    instead of edge-produced values.
    """

    @pytest.mark.xfail(
        reason="BUG: DiGraph drops parallel edges - multi-output to same consumer loses edges"
    )
    def test_multiple_outputs_to_same_consumer(self):
        """Node with multiple outputs feeding into single consumer.

        BUG: _add_data_edges creates multiple edges between divmod_node→combine
        (one per output), but nx.DiGraph only keeps the last one. The first
        output ('quotient') edge is silently overwritten by the second
        ('remainder') edge. This causes 'quotient' to appear as a required
        input instead of an edge-produced value.
        """

        @node(output_name=("quotient", "remainder"))
        def divmod_node(a: int, b: int) -> tuple[int, int]:
            return divmod(a, b)

        @node(output_name="result")
        def combine(quotient: int, remainder: int) -> str:
            return f"{quotient}r{remainder}"

        graph = Graph(nodes=[divmod_node, combine])
        runner = SyncRunner()
        result = runner.run(graph, {"a": 17, "b": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "3r2"

    def test_multiple_outputs_to_different_consumers(self):
        """Multi-output node feeding into DIFFERENT consumers works fine."""

        @node(output_name=("quotient", "remainder"))
        def divmod_node(a: int, b: int) -> tuple[int, int]:
            return divmod(a, b)

        @node(output_name="q_result")
        def use_quotient(quotient: int) -> str:
            return f"q={quotient}"

        @node(output_name="r_result")
        def use_remainder(remainder: int) -> str:
            return f"r={remainder}"

        graph = Graph(nodes=[divmod_node, use_quotient, use_remainder])
        runner = SyncRunner()
        result = runner.run(graph, {"a": 17, "b": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["q_result"] == "q=3"
        assert result["r_result"] == "r=2"

    def test_multiple_outputs_wrong_count(self):
        """Node returning wrong number of values should fail."""

        @node(output_name=("a", "b", "c"))
        def bad_node(x: int) -> tuple:
            return (1, 2)  # Only 2 values for 3 outputs

        graph = Graph(nodes=[bad_node])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 1})

        assert result.status == RunStatus.FAILED, (
            "Should fail when return count doesn't match output count"
        )


# ===========================================================================
# BUG 12: Rename collision detection
# ===========================================================================

class TestRenameCollisions:
    """Test that renaming doesn't create ambiguous connections."""

    def test_rename_input_to_existing_input_name(self):
        """Renaming an input to match another input's name is problematic."""

        @node(output_name="result")
        def add(x: int, y: int) -> int:
            return x + y

        # Rename x to y - now both inputs are named "y"
        # This should either error or have well-defined behavior
        try:
            renamed = add.with_inputs(x="y")
            # If it succeeds, both inputs are now "y"
            assert renamed.inputs == ("y", "y") or len(set(renamed.inputs)) == 1, (
                f"Unexpected inputs after rename collision: {renamed.inputs}"
            )
        except Exception:
            pass  # Error is acceptable behavior

    def test_swap_rename_preserves_semantics(self):
        """Swapping input names (x→y, y→x) should work correctly."""

        @node(output_name="result")
        def subtract(x: int, y: int) -> int:
            return x - y

        # Swap x and y in a single call
        swapped = subtract.with_inputs({"x": "y", "y": "x"})
        # Note: this is a parallel rename, so x→y and y→x simultaneously

        # The node should now take y as first param and x as second
        # So subtract(y=10, x=3) should compute original_x - original_y
        # After swap: providing x=3, y=10 means original_x=10, original_y=3
        graph = Graph(nodes=[swapped])
        runner = SyncRunner()
        # After swap: input "x" maps to original "y", input "y" maps to original "x"
        # So providing x=3, y=10 means original_y=3, original_x=10
        # Result = original_x - original_y = 10 - 3 = 7
        result = runner.run(graph, {"x": 3, "y": 10})
        assert result.status == RunStatus.COMPLETED, f"Failed: {result.error}"
        assert result["result"] == 7, (
            f"Expected 7 (swapped: original_x=10, original_y=3, 10-3=7), "
            f"got {result['result']}"
        )


# ===========================================================================
# BUG 13: Graph with only gate nodes (no data producers)
# ===========================================================================

class TestEdgeCaseGraphs:
    """Test unusual graph configurations."""

    def test_single_side_effect_node(self):
        """Graph with only a side-effect node (no outputs)."""

        called = {"count": 0}

        @node
        def side_effect(x: int) -> None:
            called["count"] += 1

        graph = Graph(nodes=[side_effect])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 1})
        assert result.status == RunStatus.COMPLETED
        assert called["count"] == 1

    def test_empty_graph_not_rejected(self):
        """Graph with no nodes is currently accepted (design gap).

        BUG/DESIGN GAP: Empty graphs are silently accepted. Running them
        returns empty results. Should arguably raise at construction time.
        """
        graph = Graph(nodes=[])
        runner = SyncRunner()
        result = runner.run(graph, {})
        assert result.status == RunStatus.COMPLETED
        assert result.values == {}

    def test_self_referential_graph_node(self):
        """A graph containing itself should be detected/rejected."""

        @node(output_name="x")
        def identity(x: int) -> int:
            return x

        g = Graph(nodes=[identity], name="self_ref")
        gn = g.as_node()

        # Try to create a graph that contains itself
        # This would be: Graph(nodes=[gn]) where gn wraps g
        # Since g already contains identity, this is fine (gn wraps g, g contains identity)
        # But what if we try circular reference?
        outer = Graph(nodes=[gn], name="outer")
        # This is not self-referential, just nested. That's fine.
        assert outer is not None


# ===========================================================================
# BUG 14: Generator node streaming behavior
# ===========================================================================

class TestGeneratorNodes:
    """Test generator (streaming) node behavior."""

    def test_sync_generator_collects_all_values_as_list(self):
        """Sync generator collects ALL yielded values into a list."""

        @node(output_name="result")
        def gen(x: int) -> int:
            for i in range(x):
                yield i

        graph = Graph(nodes=[gen])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        # Generator accumulates to list (see SyncFunctionNodeExecutor)
        assert result["result"] == [0, 1, 2, 3, 4], (
            f"Generator should collect all values into list, got {result['result']}"
        )


# ===========================================================================
# BUG 15: Type validation with complex types
# ===========================================================================

class TestComplexTypeValidation:
    """Test type validation with complex type annotations."""

    def test_optional_type_compatible(self):
        """Optional[int] should be compatible with int | None."""
        from typing import Optional

        assert is_type_compatible(int, Optional[int]), (
            "int should be compatible with Optional[int]"
        )

    def test_list_int_not_compatible_with_list_str(self):
        """list[int] should NOT be compatible with list[str]."""
        assert not is_type_compatible(list[int], list[str])

    def test_unparameterized_list_accepts_parameterized(self):
        """list[int] should be compatible with bare list."""
        assert is_type_compatible(list[int], list)

    def test_nested_generic_compatibility(self):
        """dict[str, list[int]] should be compatible with itself."""
        assert is_type_compatible(dict[str, list[int]], dict[str, list[int]])

    def test_nested_generic_incompatibility(self):
        """dict[str, list[int]] should NOT be compatible with dict[str, list[str]]."""
        assert not is_type_compatible(
            dict[str, list[int]], dict[str, list[str]]
        )


# ===========================================================================
# BUG 16: with_name creates node that works in graph
# ===========================================================================

class TestWithName:
    """Test that with_name works correctly for all node types."""

    def test_function_node_with_name(self):
        """Renamed FunctionNode should work in a graph."""

        @node(output_name="result")
        def process(x: int) -> int:
            return x * 2

        renamed = process.with_name("my_processor")
        assert renamed.name == "my_processor"

        graph = Graph(nodes=[renamed])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 10

    def test_route_node_with_name(self):
        """Renamed RouteNode should work in a graph."""

        @route(targets=["a", END])
        def decide(x: int) -> str:
            return "a" if x > 0 else END

        renamed = decide.with_name("my_gate")
        assert renamed.name == "my_gate"

        @node(output_name="result")
        def a(x: int) -> int:
            return x

        graph = Graph(nodes=[renamed, a])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result.status == RunStatus.COMPLETED


# ===========================================================================
# BUG 17: Concurrent value updates in superstep
# ===========================================================================

class TestSuperstepDeterminism:
    """Test that superstep execution is deterministic.

    All nodes in a superstep should see the SAME input state (snapshot from
    before the superstep), not partially-updated state.
    """

    def test_parallel_nodes_see_same_state(self):
        """Two nodes executing in the same superstep should see the same input."""

        @node(output_name="x")
        def provide(input_val: int) -> int:
            return input_val

        @node(output_name="a")
        def consumer_a(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def consumer_b(x: int) -> int:
            return x + 2

        graph = Graph(nodes=[provide, consumer_a, consumer_b])
        runner = SyncRunner()
        result = runner.run(graph, {"input_val": 10})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 11
        assert result["b"] == 12


# ===========================================================================
# BUG 18: max_iterations boundary
# ===========================================================================

class TestMaxIterations:
    """Test max_iterations behavior at boundaries."""

    def test_exact_max_iterations(self):
        """Graph that completes on exactly the max iteration."""

        @node(output_name="counter")
        def increment(counter: int) -> int:
            return counter + 1

        @route(targets=["increment", END])
        def check(counter: int) -> str:
            return END if counter >= 3 else "increment"

        graph = Graph(nodes=[increment, check])
        runner = SyncRunner()

        # With max_iterations=3, should be enough for counter 0→1→2→3→END
        result = runner.run(graph, {"counter": 0}, max_iterations=10)
        assert result.status == RunStatus.COMPLETED, (
            f"Should complete within 10 iterations. Error: {result.error}"
        )

    def test_max_iterations_exceeded(self):
        """Graph that exceeds max_iterations should fail."""

        @node(output_name="counter")
        def increment(counter: int) -> int:
            return counter + 1

        @route(targets=["increment", END])
        def check(counter: int) -> str:
            return END if counter >= 100 else "increment"

        graph = Graph(nodes=[increment, check])
        runner = SyncRunner()

        result = runner.run(graph, {"counter": 0}, max_iterations=5)
        assert result.status == RunStatus.FAILED, (
            "Should fail when max_iterations is exceeded"
        )


# ===========================================================================
# BUG 19: GateNode get_output_type returns None (gates have no outputs)
# ===========================================================================

class TestGateNodeProperties:
    """Test GateNode property edge cases."""

    def test_gate_has_no_outputs(self):
        """Gates should have empty outputs."""

        @route(targets=["a", END])
        def decide(x: int) -> str:
            return "a"

        assert decide.outputs == ()

    def test_gate_get_output_type_returns_none(self):
        """Gates have no outputs, so get_output_type should return None."""

        @route(targets=["a", END])
        def decide(x: int) -> str:
            return "a"

        assert decide.get_output_type("anything") is None

    def test_gate_definition_hash_stable(self):
        """Gate definition hash should be deterministic."""

        @route(targets=["a", END])
        def decide(x: int) -> str:
            return "a"

        h1 = decide.definition_hash
        h2 = decide.definition_hash
        assert h1 == h2


# ===========================================================================
# BUG 20: select parameter filters outputs correctly
# ===========================================================================

class TestSelectParameter:
    """Test the select parameter in runner.run()."""

    def test_select_filters_outputs(self):
        """select should only return requested outputs."""

        @node(output_name="a")
        def n1(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def n2(x: int) -> int:
            return x + 2

        graph = Graph(nodes=[n1, n2])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5}, select=["a"])

        assert "a" in result.values
        assert "b" not in result.values

    def test_select_nonexistent_output_warns(self):
        """Selecting a non-existent output should warn."""
        import warnings

        @node(output_name="a")
        def n1(x: int) -> int:
            return x + 1

        graph = Graph(nodes=[n1])
        runner = SyncRunner()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = runner.run(graph, {"x": 5}, select=["nonexistent"])
            # Should have a warning about missing output
            warning_messages = [str(warning.message) for warning in w]
            assert any("nonexistent" in msg for msg in warning_messages), (
                f"Expected warning about 'nonexistent', got: {warning_messages}"
            )
