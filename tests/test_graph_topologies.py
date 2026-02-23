"""Tests for complex graph topologies."""

from hypergraph.graph import Graph
from hypergraph.nodes.function import node


class TestDiamondPattern:
    r"""Test diamond dependency pattern (TOPO-01).

    Diamond pattern: A->B, A->C, B->D, C->D

        A (produces 'a')
       / \
      B   C  (both consume 'a', produce 'b' and 'c')
       \ /
        D    (consumes both 'b' and 'c')
    """

    def test_diamond_creates_correct_edges(self):
        """Diamond pattern creates exactly 4 edges: A->B, A->C, B->D, C->D."""

        @node(output_name="a")
        def node_a(x: int) -> int:
            return x

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(a: int) -> int:
            return a + 1

        @node(output_name="d")
        def node_d(b: int, c: int) -> int:
            return b + c

        g = Graph([node_a, node_b, node_c, node_d])

        # Check edges exist
        assert g.nx_graph.has_edge("node_a", "node_b")
        assert g.nx_graph.has_edge("node_a", "node_c")
        assert g.nx_graph.has_edge("node_b", "node_d")
        assert g.nx_graph.has_edge("node_c", "node_d")

        # Check total edge count
        assert g.nx_graph.number_of_edges() == 4

    def test_diamond_leaf_outputs(self):
        """Diamond pattern has only D as leaf node."""

        @node(output_name="a")
        def node_a(x: int) -> int:
            return x

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(a: int) -> int:
            return a + 1

        @node(output_name="d")
        def node_d(b: int, c: int) -> int:
            return b + c

        g = Graph([node_a, node_b, node_c, node_d])

        # Only D's output is a leaf (no downstream consumers)
        assert g.leaf_outputs == ("d",)

    def test_diamond_inputs(self):
        """Diamond pattern has only external input 'x'."""

        @node(output_name="a")
        def node_a(x: int) -> int:
            return x

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(a: int) -> int:
            return a + 1

        @node(output_name="d")
        def node_d(b: int, c: int) -> int:
            return b + c

        g = Graph([node_a, node_b, node_c, node_d])

        # Only 'x' is an external input
        assert g.inputs.required == ("x",)
        # All internal edges are not inputs
        assert "a" not in g.inputs.all
        assert "b" not in g.inputs.all
        assert "c" not in g.inputs.all

    def test_diamond_is_dag(self):
        """Diamond pattern is acyclic (DAG)."""

        @node(output_name="a")
        def node_a(x: int) -> int:
            return x

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(a: int) -> int:
            return a + 1

        @node(output_name="d")
        def node_d(b: int, c: int) -> int:
            return b + c

        g = Graph([node_a, node_b, node_c, node_d])

        assert g.has_cycles is False


class TestMultiNodeCycle:
    """Test multi-node cycles (TOPO-02).

    3-node cycle: A->B->C->A

        A (produces 'a', consumes 'c')
        |
        v
        B (produces 'b', consumes 'a')
        |
        v
        C (produces 'c', consumes 'b')
        |
        +---> back to A
    """

    def test_three_node_cycle_detected(self):
        """Three-node cycle A->B->C->A is detected as cyclic."""

        @node(output_name="a")
        def node_a(c: int) -> int:
            return c + 1

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(b: int) -> int:
            return b - 1

        g = Graph([node_a, node_b, node_c])

        assert g.has_cycles is True

    def test_three_node_cycle_entrypoints(self):
        """Three-node cycle computes entrypoints for cycle nodes."""

        @node(output_name="a")
        def node_a(c: int) -> int:
            return c + 1

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(b: int) -> int:
            return b - 1

        g = Graph([node_a, node_b, node_c])

        # Each non-gate cycle node is an entrypoint
        assert len(g.inputs.entrypoints) > 0
        # All cycle nodes should appear as entrypoints
        all_ep_params = {p for params in g.inputs.entrypoints.values() for p in params}
        cycle_params = {"a", "b", "c"}
        for param in cycle_params:
            assert param in all_ep_params or param not in g.inputs.all

    def test_three_node_cycle_no_required_inputs(self):
        """If all inputs are from cycle, required should be empty."""

        @node(output_name="a")
        def node_a(c: int) -> int:
            return c + 1

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(b: int) -> int:
            return b - 1

        g = Graph([node_a, node_b, node_c])

        # No external inputs needed (all come from cycle)
        assert g.inputs.required == ()

    def test_cycle_with_external_input(self):
        """Cycle with external input: x is required, cycle params are entrypoints."""

        @node(output_name="a")
        def node_a(c: int, x: int) -> int:
            return c + x

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="c")
        def node_c(b: int) -> int:
            return b - 1

        g = Graph([node_a, node_b, node_c])

        # 'x' is external input
        assert "x" in g.inputs.required
        # Cycle parameters appear in entrypoints
        assert len(g.inputs.entrypoints) > 0


class TestMultipleCycles:
    """Test multiple independent cycles (TOPO-03).

    Two separate cycles that don't interact:
      Cycle 1: X -> Y -> X
      Cycle 2: P -> Q -> R -> P
    """

    def test_two_independent_cycles_detected(self):
        """Graph with two independent cycles is detected as cyclic."""

        # Cycle 1: x_node and y_node
        @node(output_name="x")
        def x_node(y: int) -> int:
            return y + 1

        @node(output_name="y")
        def y_node(x: int) -> int:
            return x * 2

        # Cycle 2: p_node, q_node, r_node
        @node(output_name="p")
        def p_node(r: int) -> int:
            return r + 10

        @node(output_name="q")
        def q_node(p: int) -> int:
            return p * 3

        @node(output_name="r")
        def r_node(q: int) -> int:
            return q - 5

        g = Graph([x_node, y_node, p_node, q_node, r_node])

        assert g.has_cycles is True

    def test_two_independent_cycles_entrypoints(self):
        """Entry points from both cycles are detected."""

        @node(output_name="x")
        def x_node(y: int) -> int:
            return y + 1

        @node(output_name="y")
        def y_node(x: int) -> int:
            return x * 2

        @node(output_name="p")
        def p_node(r: int) -> int:
            return r + 10

        @node(output_name="q")
        def q_node(p: int) -> int:
            return p * 3

        @node(output_name="r")
        def r_node(q: int) -> int:
            return q - 5

        g = Graph([x_node, y_node, p_node, q_node, r_node])

        # Both cycles need entrypoints — at least one node per cycle
        assert len(g.inputs.entrypoints) >= 2

    def test_cycles_with_shared_external_input(self):
        """Two cycles sharing external input 'config'."""

        @node(output_name="x")
        def x_node(y: int, config: int) -> int:
            return y + config

        @node(output_name="y")
        def y_node(x: int) -> int:
            return x * 2

        @node(output_name="p")
        def p_node(r: int, config: int) -> int:
            return r + config

        @node(output_name="q")
        def q_node(p: int) -> int:
            return p * 3

        @node(output_name="r")
        def r_node(q: int) -> int:
            return q - 5

        g = Graph([x_node, y_node, p_node, q_node, r_node])

        # 'config' is required (external), cycle nodes have entrypoints
        assert "config" in g.inputs.required
        assert len(g.inputs.entrypoints) > 0


class TestIsolatedSubgraphs:
    """Test disconnected components (TOPO-04).

    Two unconnected chains:
      Component 1: A -> B
      Component 2: X -> Y (completely separate)
    """

    def test_disconnected_components_both_work(self):
        """Both disconnected components are included in the graph."""

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="y")
        def node_y(x: str) -> str:
            return x.upper()

        g = Graph([node_b, node_y])

        # Both nodes are in the graph
        assert "node_b" in g.nodes
        assert "node_y" in g.nodes

        # Both outputs are present
        assert "b" in g.outputs
        assert "y" in g.outputs

    def test_disconnected_components_separate_inputs(self):
        """Each component has its own inputs."""

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="y")
        def node_y(x: str) -> str:
            return x.upper()

        g = Graph([node_b, node_y])

        # Both inputs are required
        assert "a" in g.inputs.required
        assert "x" in g.inputs.required

    def test_disconnected_components_no_cross_edges(self):
        """No edges exist between disconnected components."""

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        @node(output_name="y")
        def node_y(x: str) -> str:
            return x.upper()

        g = Graph([node_b, node_y])

        # No edges between components
        assert not g.nx_graph.has_edge("node_b", "node_y")
        assert not g.nx_graph.has_edge("node_y", "node_b")

        # Total edges is 0 (no internal edges in either single-node "chain")
        assert g.nx_graph.number_of_edges() == 0

    def test_disconnected_all_are_leaves(self):
        """All terminal nodes in disconnected components are leaves."""

        # Chain 1: a_producer -> b_consumer
        @node(output_name="a")
        def a_producer(in1: int) -> int:
            return in1

        @node(output_name="b")
        def b_consumer(a: int) -> int:
            return a * 2

        # Chain 2: x_producer -> y_consumer
        @node(output_name="x")
        def x_producer(in2: str) -> str:
            return in2

        @node(output_name="y")
        def y_consumer(x: str) -> str:
            return x.upper()

        g = Graph([a_producer, b_consumer, x_producer, y_consumer])

        # Terminal nodes of both chains are leaves
        assert "b" in g.leaf_outputs
        assert "y" in g.leaf_outputs

        # Intermediate outputs are not leaves
        assert "a" not in g.leaf_outputs
        assert "x" not in g.leaf_outputs


class TestDeeplyNestedGraphs:
    """Test deeply nested graphs using GraphNode (TOPO-05).

    3-level nesting:
      Level 3 (innermost): inner_inner_graph (single node)
      Level 2: inner_graph (contains inner_inner as GraphNode)
      Level 1 (outermost): outer_graph (contains inner as GraphNode)
    """

    def test_three_level_nesting_works(self):
        """Three-level nested graph constructs without error."""

        @node(output_name="z")
        def innermost(x: int) -> int:
            return x * 2

        # Level 3: innermost graph
        inner_inner = Graph([innermost], name="inner_inner")

        # Level 2: middle graph containing innermost
        middle = Graph([inner_inner.as_node()], name="middle")

        # Level 1: outer graph containing middle
        outer = Graph([middle.as_node()], name="outer")

        # No exception raised
        assert "middle" in outer.nodes
        assert outer.inputs.required == ("x",)
        assert outer.outputs == ("z",)

    def test_three_level_inputs_propagate(self):
        """Inputs from all levels propagate to outermost graph."""

        @node(output_name="z1")
        def level3_node(x: int) -> int:
            return x * 2

        @node(output_name="z2")
        def level2_node(y: int) -> int:
            return y + 1

        @node(output_name="z3")
        def level1_node(z: int) -> int:
            return z - 1

        # Level 3
        inner_inner = Graph([level3_node], name="inner_inner")

        # Level 2: contains level 3 + its own node
        middle = Graph([inner_inner.as_node(), level2_node], name="middle")

        # Level 1: contains level 2 + its own node
        outer = Graph([middle.as_node(), level1_node], name="outer")

        # All external inputs propagate
        all_inputs = outer.inputs.all
        assert "x" in all_inputs  # from level 3
        assert "y" in all_inputs  # from level 2
        assert "z" in all_inputs  # from level 1

    def test_three_level_outputs_visible(self):
        """Outputs from all levels are visible from outermost graph."""

        @node(output_name="out3")
        def level3_node(x: int) -> int:
            return x * 2

        @node(output_name="out2")
        def level2_node(y: int) -> int:
            return y + 1

        @node(output_name="out1")
        def level1_node(z: int) -> int:
            return z - 1

        # Level 3
        inner_inner = Graph([level3_node], name="inner_inner")

        # Level 2
        middle = Graph([inner_inner.as_node(), level2_node], name="middle")

        # Level 1
        outer = Graph([middle.as_node(), level1_node], name="outer")

        # All outputs visible
        assert "out3" in outer.outputs
        assert "out2" in outer.outputs
        assert "out1" in outer.outputs

    def test_nested_with_strict_types(self):
        """Three-level nesting with strict_types=True works correctly."""

        @node(output_name="val")
        def level3_node(x: int) -> int:
            return x * 2

        @node(output_name="result")
        def level2_node(val: int) -> int:  # Consumes level3 output
            return val + 1

        # Level 3
        inner_inner = Graph([level3_node], name="inner_inner", strict_types=True)

        # Level 2: connects level3 output to level2 input
        middle = Graph([inner_inner.as_node(), level2_node], name="middle", strict_types=True)

        # Level 1: outer wrapper
        outer = Graph([middle.as_node()], name="outer", strict_types=True)

        # No GraphConfigError raised - types flow correctly
        assert outer.strict_types is True
        assert "result" in outer.outputs

    def test_deeply_nested_definition_hash(self):
        """Definition hash is computed correctly for deeply nested graphs."""

        @node(output_name="z")
        def innermost(x: int) -> int:
            return x * 2

        # Build 3-level nesting
        inner_inner = Graph([innermost], name="inner_inner")
        middle = Graph([inner_inner.as_node()], name="middle")
        outer = Graph([middle.as_node()], name="outer")

        # Hash exists and is deterministic
        hash1 = outer.definition_hash
        assert hash1 is not None
        assert len(hash1) > 0

        # Build identical structure
        inner_inner2 = Graph([innermost], name="inner_inner")
        middle2 = Graph([inner_inner2.as_node()], name="middle")
        outer2 = Graph([middle2.as_node()], name="outer")

        hash2 = outer2.definition_hash

        # Same structure produces same hash
        assert hash1 == hash2
