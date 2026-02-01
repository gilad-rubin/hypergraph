"""Tests for graph.py - InputSpec and Graph classes."""

import pytest
from hypergraph import InputSpec
from hypergraph.graph import Graph, GraphConfigError
from hypergraph.nodes.function import node


class TestInputSpec:
    """Test InputSpec dataclass."""

    def test_input_spec_creation(self):
        """Test InputSpec can be created with all fields."""
        spec = InputSpec(
            required=("param1", "param2"),
            optional=("param3",),
            seeds=("param4",),
            bound={"param5": 42},
        )

        assert spec.required == ("param1", "param2")
        assert spec.optional == ("param3",)
        assert spec.seeds == ("param4",)
        assert spec.bound == {"param5": 42}

    def test_input_spec_all_property(self):
        """Test .all property returns combined tuple of all inputs."""
        spec = InputSpec(
            required=("a", "b"),
            optional=("c",),
            seeds=("d",),
            bound={},
        )

        assert spec.all == ("a", "b", "c", "d")

    def test_input_spec_empty_fields(self):
        """Test InputSpec works with empty tuples/dicts."""
        spec = InputSpec(
            required=(),
            optional=(),
            seeds=(),
            bound={},
        )

        assert spec.required == ()
        assert spec.optional == ()
        assert spec.seeds == ()
        assert spec.bound == {}
        assert spec.all == ()

    def test_input_spec_frozen(self):
        """Test InputSpec is immutable (frozen)."""
        spec = InputSpec(
            required=("param1",),
            optional=(),
            seeds=(),
            bound={},
        )

        with pytest.raises(AttributeError):
            spec.required = ("param2",)

    def test_input_spec_all_preserves_order(self):
        """Test .all property preserves order: required + optional + seeds."""
        spec = InputSpec(
            required=("r1", "r2"),
            optional=("o1", "o2"),
            seeds=("s1", "s2"),
            bound={},
        )

        assert spec.all == ("r1", "r2", "o1", "o2", "s1", "s2")


class TestGraphConstruction:
    """Test Graph class construction and basic properties."""

    def test_empty_graph(self):
        """Test creating an empty graph with no nodes."""
        g = Graph([])

        assert g.nodes == {}
        assert g.outputs == ()
        assert g.leaf_outputs == ()
        assert g.nx_graph.number_of_nodes() == 0
        assert g.nx_graph.number_of_edges() == 0

    def test_single_node_graph(self):
        """Test graph with a single node."""
        @node(output_name="result")
        def single(x: int) -> int:
            return x * 2

        g = Graph([single])

        assert "single" in g.nodes
        assert g.nodes["single"] is single
        assert g.outputs == ("result",)
        assert g.leaf_outputs == ("result",)
        assert g.nx_graph.number_of_nodes() == 1
        assert g.nx_graph.number_of_edges() == 0

    def test_linear_chain(self):
        """Test graph with linear dependency: a -> b."""
        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x + 1

        @node(output_name="b_out")
        def node_b(a_out: int) -> int:
            return a_out * 2

        g = Graph([node_a, node_b])

        assert set(g.nodes.keys()) == {"node_a", "node_b"}
        assert g.outputs == ("a_out", "b_out")
        assert g.leaf_outputs == ("b_out",)
        assert g.nx_graph.number_of_nodes() == 2
        assert g.nx_graph.number_of_edges() == 1
        assert g.nx_graph.has_edge("node_a", "node_b")

    def test_fan_out(self):
        """Test graph with fan-out: a -> b, a -> c."""
        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x + 1

        @node(output_name="b_out")
        def node_b(a_out: int) -> int:
            return a_out * 2

        @node(output_name="c_out")
        def node_c(a_out: int) -> int:
            return a_out * 3

        g = Graph([node_a, node_b, node_c])

        assert set(g.nodes.keys()) == {"node_a", "node_b", "node_c"}
        assert g.outputs == ("a_out", "b_out", "c_out")
        assert set(g.leaf_outputs) == {"b_out", "c_out"}
        assert g.nx_graph.number_of_nodes() == 3
        assert g.nx_graph.number_of_edges() == 2
        assert g.nx_graph.has_edge("node_a", "node_b")
        assert g.nx_graph.has_edge("node_a", "node_c")

    def test_fan_in(self):
        """Test graph with fan-in: a -> c, b -> c."""
        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x + 1

        @node(output_name="b_out")
        def node_b(y: int) -> int:
            return y + 2

        @node(output_name="c_out")
        def node_c(a_out: int, b_out: int) -> int:
            return a_out + b_out

        g = Graph([node_a, node_b, node_c])

        assert set(g.nodes.keys()) == {"node_a", "node_b", "node_c"}
        assert g.outputs == ("a_out", "b_out", "c_out")
        assert g.leaf_outputs == ("c_out",)
        assert g.nx_graph.number_of_nodes() == 3
        assert g.nx_graph.number_of_edges() == 2
        assert g.nx_graph.has_edge("node_a", "node_c")
        assert g.nx_graph.has_edge("node_b", "node_c")

    def test_duplicate_node_names_raises(self):
        """Test that duplicate node names raise GraphConfigError."""
        @node(output_name="out1")
        def process(x: int) -> int:
            return x + 1

        @node(output_name="out2")
        def process(x: int) -> int:  # noqa: F811 - intentional redefinition for test
            return x + 2

        with pytest.raises(GraphConfigError, match="Duplicate node name: 'process'"):
            Graph([process, process])

    def test_duplicate_outputs_raises(self):
        """Test that duplicate output names raise GraphConfigError."""
        @node(output_name="result")
        def node_a(x: int) -> int:
            return x + 1

        @node(output_name="result")  # Duplicate output name
        def node_b(y: int) -> int:
            return y + 2

        with pytest.raises(GraphConfigError, match="Multiple nodes produce 'result'"):
            Graph([node_a, node_b])

    def test_nodes_property_returns_dict_copy(self):
        """Test that nodes property returns a copy (prevents mutation)."""
        @node(output_name="result")
        def single(x: int) -> int:
            return x

        g = Graph([single])
        nodes_copy = g.nodes

        # Mutating the copy shouldn't affect the graph
        nodes_copy["fake"] = None

        assert "fake" not in g.nodes
        assert len(g.nodes) == 1

    def test_outputs_property(self):
        """Test outputs property returns all node outputs."""
        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x

        @node(output_name=("b_out1", "b_out2"))  # Multiple outputs
        def node_b(y: int) -> tuple:
            return y, y * 2

        g = Graph([node_a, node_b])

        assert g.outputs == ("a_out", "b_out1", "b_out2")

    def test_leaf_outputs_property(self):
        """Test leaf_outputs property returns only leaf node outputs."""
        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x

        @node(output_name="b_out")
        def node_b(a_out: int) -> int:
            return a_out * 2

        @node(output_name="c_out")
        def node_c(a_out: int) -> int:
            return a_out * 3

        g = Graph([node_a, node_b, node_c])

        # node_a is not a leaf (feeds b and c)
        # node_b and node_c are leaves
        assert set(g.leaf_outputs) == {"b_out", "c_out"}

    def test_nx_graph_has_correct_edges(self):
        """Test NetworkX graph contains correct edges with attributes."""
        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x

        @node(output_name="b_out")
        def node_b(a_out: int) -> int:
            return a_out * 2

        g = Graph([node_a, node_b])

        # Check edge exists
        assert g.nx_graph.has_edge("node_a", "node_b")

        # Check edge attributes
        edge_data = g.nx_graph.get_edge_data("node_a", "node_b")
        assert edge_data["edge_type"] == "data"
        assert edge_data["value_names"] == ["a_out"]

    def test_nx_graph_has_node_attributes(self):
        """Test NetworkX nodes contain flattened attributes."""
        @node(output_name="result")
        def single(x: int) -> int:
            return x

        g = Graph([single])

        node_data = g.nx_graph.nodes["single"]
        assert node_data["node_type"] == "FUNCTION"
        assert node_data["label"] == "single"
        assert node_data["inputs"] == ("x",)
        assert node_data["outputs"] == ("result",)
        assert node_data["parent"] is None

    def test_graph_with_optional_name(self):
        """Test Graph can be created with optional name."""
        @node(output_name="result")
        def single(x: int) -> int:
            return x

        g = Graph([single], name="test_graph")

        assert g.name == "test_graph"

    def test_graph_without_name(self):
        """Test Graph name defaults to None."""
        @node(output_name="result")
        def single(x: int) -> int:
            return x

        g = Graph([single])

        assert g.name is None


class TestGraphInputs:
    """Test Graph.inputs property and InputSpec computation."""

    def test_all_required(self):
        """Test graph with all required parameters (no defaults, no edges)."""

        @node(output_name="result")
        def foo(a, b, c):
            return a + b + c

        g = Graph([foo])

        assert g.inputs.required == ("a", "b", "c")
        assert g.inputs.optional == ()
        assert g.inputs.seeds == ()
        assert g.inputs.bound == {}

    def test_with_defaults_become_optional(self):
        """Test parameters with defaults become optional."""

        @node(output_name="result")
        def foo(a, b=1, c=2):
            return a + b + c

        g = Graph([foo])

        assert g.inputs.required == ("a",)
        assert g.inputs.optional == ("b", "c")
        assert g.inputs.seeds == ()
        assert g.inputs.bound == {}

    def test_edge_connected_not_in_inputs(self):
        """Test edge-connected parameters are excluded (internal values)."""

        @node(output_name="x")
        def source(a):
            return a * 2

        @node(output_name="result")
        def destination(x, b):
            return x + b

        g = Graph([source, destination])

        # 'x' is edge-connected, so only 'a' and 'b' are external inputs
        assert "x" not in g.inputs.all
        assert "a" in g.inputs.all
        assert "b" in g.inputs.all

    def test_cycle_creates_seed(self):
        """Test parameter in cycle becomes a seed."""

        @node(output_name="count")
        def counter(count):
            return count + 1

        g = Graph([counter])

        # 'count' is both consumed and produced by same node -> cycle -> seed
        assert g.inputs.seeds == ("count",)
        assert g.inputs.required == ()
        assert g.inputs.optional == ()

    def test_complex_graph(self):
        """Test complex graph with mix of required, optional, and internal params."""

        @node(output_name="x")
        def step1(a, b=10):
            return a + b

        @node(output_name="y")
        def step2(x, c):
            return x * c

        @node(output_name="result")
        def step3(y, d=5):
            return y + d

        g = Graph([step1, step2, step3])

        # 'a' is required (no default)
        # 'b' is optional (has default)
        # 'c' is required (no default)
        # 'd' is optional (has default)
        # 'x' and 'y' are edge-connected (internal)
        assert set(g.inputs.required) == {"a", "c"}
        assert set(g.inputs.optional) == {"b", "d"}
        assert "x" not in g.inputs.all
        assert "y" not in g.inputs.all

    def test_multiple_destinations_same_param(self):
        """Test when multiple nodes receive the same external parameter."""

        @node(output_name="x")
        def node1(shared):
            return shared * 2

        @node(output_name="y")
        def node2(shared):
            return shared * 3

        g = Graph([node1, node2])

        # 'shared' appears only once in inputs even though 2 nodes use it
        assert g.inputs.required == ("shared",)

    def test_fan_out_internal_value(self):
        """Test internal value fanning out to multiple destinations."""

        @node(output_name="base")
        def source(a):
            return a * 2

        @node(output_name="x")
        def destination1(base):
            return base + 1

        @node(output_name="y")
        def destination2(base):
            return base + 2

        g = Graph([source, destination1, destination2])

        # 'base' is edge-connected, should not appear in inputs
        assert "base" not in g.inputs.all
        assert g.inputs.required == ("a",)


class TestGraphBind:
    """Test Graph.bind() and unbind() methods."""

    def test_basic_bind(self):
        """Test basic bind creates new graph with binding."""

        @node(output_name="result")
        def foo(x, y):
            return x + y

        g = Graph([foo])
        g2 = g.bind(x=10)

        # New graph has binding
        assert g2.inputs.bound == {"x": 10}

        # Original unchanged
        assert g.inputs.bound == {}

        # x should now be optional (has binding)
        assert "x" not in g2.inputs.required
        assert "x" in g2.inputs.optional

    def test_merge_bindings(self):
        """Test chaining bind calls merges bindings."""

        @node(output_name="result")
        def foo(x, y, z):
            return x + y + z

        g = Graph([foo])
        g2 = g.bind(x=1).bind(y=2)

        assert g2.inputs.bound == {"x": 1, "y": 2}
        assert set(g2.inputs.optional) == {"x", "y"}
        assert g2.inputs.required == ("z",)

    def test_override_binding(self):
        """Test binding same key twice overwrites."""

        @node(output_name="result")
        def foo(x):
            return x

        g = Graph([foo])
        g2 = g.bind(x=1).bind(x=2)

        assert g2.inputs.bound == {"x": 2}

    def test_bind_edge_produced_raises(self):
        """Test binding an edge-produced value raises ValueError."""

        @node(output_name="x")
        def source(a):
            return a * 2

        @node(output_name="result")
        def destination(x):
            return x + 1

        g = Graph([source, destination])

        # 'x' is produced by source node, cannot bind it
        with pytest.raises(ValueError, match="Cannot bind 'x': output of node 'source'"):
            g.bind(x=10)

    def test_bind_unknown_key_raises(self):
        """Test binding a key not in graph.inputs.all raises ValueError."""

        @node(output_name="result")
        def foo(x):
            return x

        g = Graph([foo])

        # 'unknown' is not a graph input
        with pytest.raises(ValueError, match="Cannot bind 'unknown': not a graph input"):
            g.bind(unknown=42)

    def test_original_unchanged_after_bind(self):
        """Test original graph is not mutated by bind."""

        @node(output_name="result")
        def foo(x, y):
            return x + y

        g = Graph([foo])
        original_bound = dict(g.inputs.bound)
        original_required = g.inputs.required

        g.bind(x=10)  # Create new graph, ignore result

        # Original graph unchanged
        assert g.inputs.bound == original_bound
        assert g.inputs.required == original_required

    def test_unbind_removes_binding(self):
        """Test unbind removes specific bindings."""

        @node(output_name="result")
        def foo(x, y, z):
            return x + y + z

        g = Graph([foo])
        g2 = g.bind(x=1, y=2, z=3)

        # Unbind specific keys
        g3 = g2.unbind("x", "z")

        assert g3.inputs.bound == {"y": 2}
        assert "x" in g3.inputs.required
        assert "y" in g3.inputs.optional
        assert "z" in g3.inputs.required

    def test_unbind_nonexistent_key_noop(self):
        """Test unbinding a non-bound key is a no-op."""

        @node(output_name="result")
        def foo(x):
            return x

        g = Graph([foo])
        g2 = g.bind(x=1)
        g3 = g2.unbind("y")  # 'y' not bound

        # No error, binding unchanged
        assert g3.inputs.bound == {"x": 1}

    def test_unbind_all_keys(self):
        """Test unbinding all keys."""

        @node(output_name="result")
        def foo(x, y):
            return x + y

        g = Graph([foo])
        g2 = g.bind(x=1, y=2)
        g3 = g2.unbind("x", "y")

        assert g3.inputs.bound == {}
        assert set(g3.inputs.required) == {"x", "y"}

    def test_bind_with_existing_binding(self):
        """Test binding when graph already has bindings."""

        @node(output_name="result")
        def foo(x, y, z):
            return x + y + z

        g = Graph([foo])
        g2 = g.bind(x=1)
        g3 = g2.bind(y=2)  # Add to existing binding

        assert g3.inputs.bound == {"x": 1, "y": 2}
        assert g2.inputs.bound == {"x": 1}  # g2 unchanged


class TestGraphFeatureProperties:
    """Test Graph has_* properties for feature detection."""

    def test_has_cycles_false(self):
        """Test has_cycles is False for DAG."""

        @node(output_name="x")
        def node_a(a):
            return a + 1

        @node(output_name="y")
        def node_b(x):
            return x * 2

        g = Graph([node_a, node_b])

        assert g.has_cycles is False

    def test_has_cycles_true(self):
        """Test has_cycles is True for cyclic graph."""

        @node(output_name="count")
        def counter(count):
            return count + 1

        g = Graph([counter])

        assert g.has_cycles is True

    def test_has_async_nodes_false(self):
        """Test has_async_nodes is False when all nodes are sync."""

        @node(output_name="x")
        def sync_node_a(a):
            return a + 1

        @node(output_name="y")
        def sync_node_b(x):
            return x * 2

        g = Graph([sync_node_a, sync_node_b])

        assert g.has_async_nodes is False

    def test_has_async_nodes_true(self):
        """Test has_async_nodes is True when at least one node is async."""

        @node(output_name="x")
        def sync_node(a):
            return a + 1

        @node(output_name="y")
        async def async_node(x):
            return x * 2

        g = Graph([sync_node, async_node])

        assert g.has_async_nodes is True

    def test_has_async_nodes_all_async(self):
        """Test has_async_nodes is True when all nodes are async."""

        @node(output_name="x")
        async def async_node_a(a):
            return a + 1

        @node(output_name="y")
        async def async_node_b(x):
            return x * 2

        g = Graph([async_node_a, async_node_b])

        assert g.has_async_nodes is True


class TestGraphDefinitionHash:
    """Test Graph.definition_hash property for versioning."""

    def test_definition_hash_deterministic(self):
        """Test same graph produces same hash (deterministic)."""

        @node(output_name="x")
        def node_a(a):
            return a + 1

        @node(output_name="y")
        def node_b(x):
            return x * 2

        g1 = Graph([node_a, node_b])
        g2 = Graph([node_a, node_b])

        assert g1.definition_hash == g2.definition_hash

    def test_definition_hash_node_order_independent(self):
        """Test hash is same regardless of node list order."""

        @node(output_name="x")
        def node_a(a):
            return a + 1

        @node(output_name="y")
        def node_b(x):
            return x * 2

        g1 = Graph([node_a, node_b])
        g2 = Graph([node_b, node_a])  # Reversed order

        assert g1.definition_hash == g2.definition_hash

    def test_definition_hash_bindings_not_included(self):
        """Test bindings don't affect hash (not part of structure)."""

        @node(output_name="result")
        def foo(x, y):
            return x + y

        g1 = Graph([foo])
        g2 = g1.bind(x=10)
        g3 = g2.bind(y=20)

        # All should have same hash - bindings don't change structure
        assert g1.definition_hash == g2.definition_hash
        assert g2.definition_hash == g3.definition_hash

    def test_definition_hash_implementation_change(self):
        """Test hash changes when node implementation changes."""

        @node(output_name="result")
        def version1(x):
            return x + 1

        @node(output_name="result")
        def version2(x):
            return x + 2  # Different implementation

        g1 = Graph([version1])
        g2 = Graph([version2])

        # Different implementations -> different hashes
        assert g1.definition_hash != g2.definition_hash

    def test_definition_hash_is_sha256(self):
        """Test hash is SHA256 (64 hex characters)."""

        @node(output_name="result")
        def foo(x):
            return x

        g = Graph([foo])

        # SHA256 produces 64 hex characters
        assert len(g.definition_hash) == 64
        assert all(c in "0123456789abcdef" for c in g.definition_hash)

    def test_definition_hash_cached(self):
        """Test hash is cached (same object instance returns same hash)."""

        @node(output_name="result")
        def foo(x):
            return x

        g = Graph([foo])

        # Call twice, should be same object (cached)
        hash1 = g.definition_hash
        hash2 = g.definition_hash

        assert hash1 is hash2  # Same string object (cached)

    def test_definition_hash_structure_change(self):
        """Test hash changes when graph structure changes."""

        @node(output_name="x")
        def node_a(a):
            return a + 1

        @node(output_name="y")
        def node_b(x):
            return x * 2

        @node(output_name="z")
        def node_c(y):
            return y * 3

        g1 = Graph([node_a, node_b])
        g2 = Graph([node_a, node_b, node_c])  # Added node

        # Different structure -> different hash
        assert g1.definition_hash != g2.definition_hash


class TestGraphAsNode:
    """Test Graph.as_node() method for composition."""

    def test_as_node_uses_graph_name(self):
        """Test as_node() uses graph.name when present."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        gnode = g.as_node()

        assert gnode.name == "my_graph"

    def test_as_node_override_name(self):
        """Test as_node(name=...) overrides graph.name."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        gnode = g.as_node(name="override")

        assert gnode.name == "override"

    def test_as_node_no_name_raises(self):
        """Test as_node() raises when graph has no name and no override."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo])  # No name

        with pytest.raises(ValueError, match="GraphNode requires a name"):
            g.as_node()

    def test_graphnode_inputs_match_graph(self):
        """Test GraphNode.inputs matches graph.inputs.all."""
        @node(output_name="x")
        def step1(a: int, b: int = 10) -> int:
            return a + b

        @node(output_name="result")
        def step2(x: int, c: int) -> int:
            return x * c

        g = Graph([step1, step2], name="my_graph")
        gnode = g.as_node()

        # Graph inputs: required=(a, c), optional=(b,)
        assert set(gnode.inputs) == {"a", "b", "c"}
        assert set(gnode.inputs) == set(g.inputs.all)

    def test_graphnode_outputs_match_graph(self):
        """Test GraphNode.outputs matches graph.outputs."""
        @node(output_name="x")
        def step1(a: int) -> int:
            return a + 1

        @node(output_name="y")
        def step2(x: int) -> int:
            return x * 2

        g = Graph([step1, step2], name="my_graph")
        gnode = g.as_node()

        assert gnode.outputs == g.outputs
        assert gnode.outputs == ("x", "y")

    def test_graphnode_definition_hash(self):
        """Test GraphNode.definition_hash returns graph's hash."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        gnode = g.as_node()

        assert gnode.definition_hash == g.definition_hash

    def test_graphnode_graph_property(self):
        """Test GraphNode.graph property returns wrapped graph."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        gnode = g.as_node()

        assert gnode.graph is g


class TestGraphNodeOutputAnnotation:
    """Test GraphNode.output_annotation property for type extraction."""

    def test_single_typed_output(self):
        """Test GraphNode exposes output type from typed function."""
        @node(output_name="x")
        def inner_func(_a: int) -> str:
            return "hello"

        inner_graph = Graph([inner_func], name="inner")
        gn = inner_graph.as_node()

        assert gn.output_annotation == {"x": str}

    def test_multiple_outputs(self):
        """Test GraphNode exposes multiple output types."""
        @node(output_name="x")
        def func_a(_a: int) -> str:
            return ""

        @node(output_name="y")
        def func_b(_x: str) -> float:
            return 0.0

        g = Graph([func_a, func_b], name="multi")
        gn = g.as_node()

        assert gn.output_annotation == {"x": str, "y": float}

    def test_untyped_outputs(self):
        """Test GraphNode returns None for untyped outputs."""
        @node(output_name="x")
        def untyped(a):
            return a

        g = Graph([untyped], name="untyped")
        gn = g.as_node()

        assert gn.output_annotation == {"x": None}

    def test_mixed_typed_untyped(self):
        """Test GraphNode includes all outputs, None for untyped."""
        @node(output_name="x")
        def typed(_a: int) -> str:
            return ""

        @node(output_name="y")
        def untyped(x):
            return x

        g = Graph([typed, untyped], name="mixed")
        gn = g.as_node()

        # 'x' has type annotation, 'y' is None (untyped)
        assert gn.output_annotation == {"x": str, "y": None}

    def test_nested_graphnode(self):
        """Test output_annotation works with nested GraphNode."""
        @node(output_name="x")
        def inner(_a: int) -> str:
            return ""

        inner_graph = Graph([inner], name="inner")
        inner_gn = inner_graph.as_node()

        @node(output_name="y")
        def outer(_x: str) -> float:
            return 0.0

        outer_graph = Graph([inner_gn, outer], name="outer")
        outer_gn = outer_graph.as_node()

        # Both inner and outer produce typed outputs
        assert outer_gn.output_annotation == {"x": str, "y": float}


class TestGraphStrictTypes:
    """Test Graph strict_types parameter for type validation."""

    def test_strict_types_defaults_false(self):
        """Test strict_types defaults to False."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        g = Graph([foo])

        assert g.strict_types is False

    def test_strict_types_true(self):
        """Test strict_types can be set to True."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        g = Graph([foo], strict_types=True)

        assert g.strict_types is True

    def test_strict_types_preserved_through_bind(self):
        """Test strict_types is preserved through bind operation."""
        @node(output_name="result")
        def foo(x: int, y: int) -> int:
            return x + y

        g = Graph([foo], strict_types=True)
        g2 = g.bind(x=10)

        assert g2.strict_types is True

    def test_strict_types_preserved_through_unbind(self):
        """Test strict_types is preserved through unbind operation."""
        @node(output_name="result")
        def foo(x: int, y: int) -> int:
            return x + y

        g = Graph([foo], strict_types=True)
        g2 = g.bind(x=10)
        g3 = g2.unbind("x")

        assert g3.strict_types is True

    def test_strict_types_with_name(self):
        """Test strict_types works with name parameter."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        g = Graph([foo], name="my_graph", strict_types=True)

        assert g.name == "my_graph"
        assert g.strict_types is True

    def test_strict_types_independent_per_graph(self):
        """Test each graph has its own strict_types setting."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        g1 = Graph([foo], strict_types=True)
        g2 = Graph([foo], strict_types=False)

        assert g1.strict_types is True
        assert g2.strict_types is False


class TestStrictTypesValidation:
    """Test type validation when strict_types=True."""

    def test_strict_types_missing_input_annotation(self):
        """Test missing input annotation raises GraphConfigError."""
        @node(output_name="result")
        def producer() -> int:
            return 42

        @node(output_name="final")
        def consumer(result):  # Missing type annotation
            return result

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([producer, consumer], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Missing type annotation" in error_msg
        assert "consumer" in error_msg
        assert "result" in error_msg
        assert "How to fix" in error_msg

    def test_strict_types_missing_output_annotation(self):
        """Test missing output annotation raises GraphConfigError."""
        @node(output_name="result")
        def producer():  # Missing return type annotation
            return 42

        @node(output_name="final")
        def consumer(result: int) -> int:
            return result

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([producer, consumer], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Missing type annotation" in error_msg
        assert "producer" in error_msg
        assert "result" in error_msg
        assert "How to fix" in error_msg

    def test_strict_types_type_mismatch(self):
        """Test type mismatch raises GraphConfigError."""
        @node(output_name="result")
        def producer() -> int:
            return 42

        @node(output_name="final")
        def consumer(result: str) -> str:
            return result

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([producer, consumer], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg
        assert "producer" in error_msg
        assert "consumer" in error_msg
        assert "result" in error_msg
        assert "How to fix" in error_msg

    def test_strict_types_compatible_types_pass(self):
        """Test compatible types pass validation."""
        @node(output_name="result")
        def producer() -> int:
            return 42

        @node(output_name="final")
        def consumer(result: int) -> int:
            return result

        # Should not raise
        g = Graph([producer, consumer], strict_types=True)

        assert g.strict_types is True
        assert g.nx_graph.has_edge("producer", "consumer")

    def test_strict_types_union_compatible(self):
        """Test int is compatible with int | str."""
        @node(output_name="result")
        def producer() -> int:
            return 42

        @node(output_name="final")
        def consumer(result: int | str) -> str:
            return str(result)

        # Should not raise - int is compatible with int | str
        g = Graph([producer, consumer], strict_types=True)

        assert g.strict_types is True

    def test_strict_types_disabled_skips_validation(self):
        """Test type mismatch is ignored when strict_types=False."""
        @node(output_name="result")
        def producer() -> int:
            return 42

        @node(output_name="final")
        def consumer(result: str) -> str:
            return result

        # Should not raise - validation disabled
        g = Graph([producer, consumer], strict_types=False)

        assert g.strict_types is False
        assert g.nx_graph.has_edge("producer", "consumer")

    def test_strict_types_graphnode_output_compatible(self):
        """Test GraphNode output type validates correctly."""
        @node(output_name="x")
        def inner_func(_a: int) -> str:
            return "hello"

        inner_graph = Graph([inner_func], name="inner")
        inner_gn = inner_graph.as_node()

        @node(output_name="final")
        def outer_consumer(x: str) -> str:
            return x.upper()

        # Should not raise - GraphNode output str is compatible with str input
        g = Graph([inner_gn, outer_consumer], strict_types=True)

        assert g.strict_types is True
        assert g.nx_graph.has_edge("inner", "outer_consumer")

    def test_strict_types_graphnode_output_incompatible(self):
        """Test GraphNode output type mismatch raises error."""
        @node(output_name="x")
        def inner_func(_a: int) -> str:
            return "hello"

        inner_graph = Graph([inner_func], name="inner")
        inner_gn = inner_graph.as_node()

        @node(output_name="final")
        def outer_consumer(x: int) -> int:
            return x + 1

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([inner_gn, outer_consumer], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg
        assert "inner" in error_msg
        assert "outer_consumer" in error_msg
        assert "x" in error_msg

    def test_strict_types_chain_validation(self):
        """Test type validation works through a chain of nodes."""
        @node(output_name="a")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def step2(a: int) -> str:
            return str(a)

        @node(output_name="c")
        def step3(b: str) -> float:
            return float(b)

        # Should not raise - all types are compatible
        g = Graph([step1, step2, step3], strict_types=True)

        assert g.strict_types is True
        assert g.nx_graph.number_of_edges() == 2

    def test_strict_types_chain_mismatch_detected(self):
        """Test type mismatch in middle of chain is detected."""
        @node(output_name="a")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def step2(a: str) -> str:  # Expects str but a is int
            return a.upper()

        @node(output_name="c")
        def step3(b: str) -> float:
            return float(b)

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([step1, step2, step3], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg
        assert "step1" in error_msg
        assert "step2" in error_msg


class TestGraphNodeRename:
    """Test GraphNode rename operations (with_name, with_inputs, with_outputs)."""

    def test_with_name_returns_new_instance(self):
        """with_name returns new GraphNode with different name."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        original = g.as_node()
        renamed = original.with_name("new_name")

        assert original.name == "my_graph"
        assert renamed.name == "new_name"
        assert original is not renamed

    def test_with_name_preserves_graph_reference(self):
        """Renamed GraphNode shares same underlying Graph (immutable)."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        original = g.as_node()
        renamed = original.with_name("new_name")

        assert renamed.graph is original.graph
        assert renamed.definition_hash == original.definition_hash

    def test_with_inputs_renames_inputs(self):
        """with_inputs renames inputs in returned GraphNode."""
        @node(output_name="result")
        def foo(a: int, b: int) -> int:
            return a + b

        g = Graph([foo], name="my_graph")
        original = g.as_node()
        renamed = original.with_inputs(a="x")

        assert original.inputs == ("a", "b")
        assert renamed.inputs == ("x", "b")

    def test_with_outputs_renames_outputs(self):
        """with_outputs renames outputs in returned GraphNode."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        original = g.as_node()
        renamed = original.with_outputs(result="output")

        assert original.outputs == ("result",)
        assert renamed.outputs == ("output",)

    def test_rename_preserves_inputs_outputs_types(self):
        """Renamed GraphNode has same tuple types for inputs/outputs."""
        @node(output_name="result")
        def foo(a: int, b: int = 10) -> int:
            return a + b

        g = Graph([foo], name="my_graph")
        gn = g.as_node().with_inputs(a="x")

        assert isinstance(gn.inputs, tuple)
        assert isinstance(gn.outputs, tuple)

    def test_with_inputs_nonexistent_raises(self):
        """Renaming non-existent input raises RenameError."""
        from hypergraph.nodes._rename import RenameError

        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        gn = g.as_node()

        with pytest.raises(RenameError, match="'nonexistent' not found"):
            gn.with_inputs(nonexistent="y")

    def test_with_outputs_nonexistent_raises(self):
        """Renaming non-existent output raises RenameError."""
        from hypergraph.nodes._rename import RenameError

        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        g = Graph([foo], name="my_graph")
        gn = g.as_node()

        with pytest.raises(RenameError, match="'nonexistent' not found"):
            gn.with_outputs(nonexistent="y")

    def test_rename_history_tracked(self):
        """Rename history is tracked for error messages."""
        from hypergraph.nodes._rename import RenameError

        @node(output_name="result")
        def foo(a: int) -> int:
            return a * 2

        g = Graph([foo], name="my_graph")
        gn = g.as_node()
        renamed = gn.with_inputs(a="x")

        # Try to rename 'a' again - should show history
        with pytest.raises(RenameError, match="'a' was renamed: a→x"):
            renamed.with_inputs(a="y")

    def test_original_unchanged_after_rename(self):
        """Original GraphNode is not mutated by rename operations."""
        @node(output_name="result")
        def foo(a: int, b: int) -> int:
            return a + b

        g = Graph([foo], name="my_graph")
        original = g.as_node()
        original_inputs = original.inputs
        original_outputs = original.outputs
        original_name = original.name

        # Do multiple renames
        original.with_name("new_name")
        original.with_inputs(a="x", b="y")
        original.with_outputs(result="out")

        # Original unchanged
        assert original.name == original_name
        assert original.inputs == original_inputs
        assert original.outputs == original_outputs


class TestGraphNodeCapabilities:
    """Test GraphNode forwarding methods for universal capabilities.

    These tests verify that GraphNode correctly delegates has_default_for,
    get_default_for, get_input_type, and get_output_type to the inner graph.

    Note: Some tests may fail until GraphNode implements forwarding for
    has_default_for and get_default_for (documents expected behavior).
    """

    def test_has_default_for_with_default(self):
        """Inner graph has node with default, GraphNode.has_default_for returns True."""
        @node(output_name="result")
        def foo(x: int, y: int = 10) -> int:
            return x + y

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.has_default_for("y") is True

    def test_has_default_for_without_default(self):
        """Inner graph node has no default, returns False."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.has_default_for("x") is False

    def test_has_default_for_nonexistent_param(self):
        """Param not in inputs, returns False."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.has_default_for("nonexistent") is False

    def test_get_default_for_retrieves_value(self):
        """Get actual default value from inner graph."""
        @node(output_name="result")
        def foo(x: int, y: int = 42) -> int:
            return x + y

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.get_default_for("y") == 42

    def test_get_default_for_raises_on_no_default(self):
        """KeyError when param has no default."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x * 2

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        with pytest.raises(KeyError, match="x"):
            gn.get_default_for("x")

    def test_get_input_type_returns_type(self):
        """Returns type annotation from inner graph node."""
        @node(output_name="result")
        def foo(x: int, y: str) -> str:
            return f"{x}: {y}"

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.get_input_type("x") == int
        assert gn.get_input_type("y") == str

    def test_get_input_type_untyped_returns_none(self):
        """Returns None for untyped params."""
        @node(output_name="result")
        def foo(x):
            return x

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.get_input_type("x") is None

    def test_get_input_type_nonexistent_returns_none(self):
        """Returns None for nonexistent param."""
        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.get_input_type("nonexistent") is None

    def test_get_output_type_returns_type(self):
        """Returns output type from inner graph node."""
        @node(output_name="result")
        def foo(x: int) -> str:
            return str(x)

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.get_output_type("result") == str

    def test_get_output_type_untyped_returns_none(self):
        """Returns None for untyped output."""
        @node(output_name="result")
        def foo(x):
            return x

        inner_graph = Graph([foo], name="inner")
        gn = inner_graph.as_node()

        assert gn.get_output_type("result") is None

    # --- Tests for bound inner graph values (GNODE-05) ---

    def test_bound_inner_graph_includes_bound_in_inputs(self):
        """Bound values remain in GraphNode.inputs (as optional)."""
        @node(output_name="result")
        def foo(x: int, y: int) -> int:
            return x + y

        inner_graph = Graph([foo], name="inner")
        bound_inner = inner_graph.bind(y=10)
        gn = bound_inner.as_node()

        # y is bound but still appears in inputs (can be overridden)
        assert "y" in gn.inputs

    def test_bound_inner_graph_preserves_unbound_inputs(self):
        """Unbound inputs still in GraphNode.inputs."""
        @node(output_name="result")
        def foo(x: int, y: int) -> int:
            return x + y

        inner_graph = Graph([foo], name="inner")
        bound_inner = inner_graph.bind(y=10)
        gn = bound_inner.as_node()

        # x is not bound, should still appear in inputs
        assert "x" in gn.inputs

    def test_bound_value_accessible_via_has_default(self):
        """Bound values act as defaults - has_default_for returns True."""
        @node(output_name="result")
        def foo(x: int, y: int) -> int:
            return x + y

        inner_graph = Graph([foo], name="inner")
        bound_inner = inner_graph.bind(y=10)
        gn = bound_inner.as_node()

        # y is bound, so has_default_for returns True (bound acts as default)
        assert gn.has_default_for("y") is True
        # get_default_for returns the bound value
        assert gn.get_default_for("y") == 10

    def test_nested_graphnode_with_bound_inner(self):
        """GraphNode of GraphNode with bound values - types flow correctly."""
        @node(output_name="intermediate")
        def inner_func(a: int, b: int = 5) -> str:
            return str(a + b)

        inner_graph = Graph([inner_func], name="inner")
        bound_inner = inner_graph.bind(b=10)
        inner_gn = bound_inner.as_node()

        @node(output_name="final")
        def outer_func(intermediate: str) -> int:
            return len(intermediate)

        outer_graph = Graph([inner_gn, outer_func], name="outer", strict_types=True)
        outer_gn = outer_graph.as_node()

        # Types should flow correctly: inner produces str, outer consumes str
        assert outer_gn.get_input_type("a") == int
        assert outer_gn.get_output_type("intermediate") == str
        assert outer_gn.get_output_type("final") == int

        # Both a and b appear in outer's inputs (b is optional due to binding)
        assert "a" in outer_gn.inputs
        assert "b" in outer_gn.inputs
        # b has a default (the bound value)
        assert outer_gn.has_default_for("b") is True
        assert outer_gn.get_default_for("b") == 10


class TestStrictTypesWithNestedGraphNode:
    """Tests for strict_types with nested GraphNode (GAP-04)."""

    def test_strict_types_propagates_to_inner_graph(self):
        """Inner graph respects strict_types from outer graph construction."""
        @node(output_name="x")
        def inner_typed(a: int) -> str:
            return str(a)

        inner_graph = Graph([inner_typed], name="inner")
        inner_gn = inner_graph.as_node()

        @node(output_name="y")
        def outer_typed(x: str) -> int:
            return len(x)

        # Outer graph with strict_types should validate inner's output type
        outer = Graph([inner_gn, outer_typed], strict_types=True)

        # Should pass validation - inner outputs str, outer expects str
        assert outer.strict_types is True

    def test_strict_types_detects_inner_outer_mismatch(self):
        """Type mismatch between inner output and outer input is detected."""
        @node(output_name="x")
        def inner_typed(a: int) -> int:
            return a * 2

        inner_graph = Graph([inner_typed], name="inner")
        inner_gn = inner_graph.as_node()

        @node(output_name="y")
        def outer_typed(x: str) -> str:  # Expects str, inner produces int
            return x.upper()

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([inner_gn, outer_typed], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg
        assert "inner" in error_msg
        assert "outer_typed" in error_msg

    def test_type_mismatch_at_graphnode_boundary(self):
        """Type error is detected at GraphNode input boundary."""
        @node(output_name="x")
        def producer() -> int:
            return 42

        @node(output_name="inner_result")
        def inner_consumer(a: str) -> str:  # Expects str
            return a

        inner_graph = Graph([inner_consumer], name="inner")
        inner_gn = inner_graph.as_node().with_inputs(a="x")

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([producer, inner_gn], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg

    def test_nested_graphnode_chain_type_checking(self):
        """Multiple nested levels are type checked."""
        @node(output_name="a")
        def step1(x: int) -> str:
            return str(x)

        @node(output_name="b")
        def step2(a: str) -> float:
            return float(a)

        @node(output_name="c")
        def step3(b: float) -> int:
            return int(b)

        # Inner graph: int -> str -> float
        inner = Graph([step1, step2], name="inner")
        inner_gn = inner.as_node()

        # Outer: add step3 which expects float
        outer = Graph([inner_gn, step3], strict_types=True)

        # All types should match
        assert outer.strict_types is True
        # Should have edges from inner to step3
        assert outer.nx_graph.has_edge("inner", "step3")

    def test_nested_graphnode_chain_type_mismatch(self):
        """Type mismatch in nested chain is detected."""
        @node(output_name="a")
        def step1(x: int) -> str:
            return str(x)

        inner = Graph([step1], name="inner")
        inner_gn = inner.as_node()

        @node(output_name="b")
        def step2(a: int) -> int:  # Expects int, inner produces str
            return a * 2

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([inner_gn, step2], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg

    def test_deeply_nested_graphnode_type_checking(self):
        """Three levels of GraphNode nesting with type checking."""
        @node(output_name="x")
        def level3_node(a: int) -> str:
            return str(a)

        level3 = Graph([level3_node], name="level3")

        @node(output_name="y")
        def level2_node(x: str) -> float:
            return float(x)

        level2 = Graph([level3.as_node(), level2_node], name="level2")

        @node(output_name="z")
        def level1_node(y: float) -> int:
            return int(y)

        # All types should match through the chain
        level1 = Graph([level2.as_node(), level1_node], strict_types=True)

        assert level1.strict_types is True
        # Verify the chain exists
        assert "level2" in level1.nodes
        assert "level1_node" in level1.nodes

    def test_graphnode_with_multiple_outputs_type_checked(self):
        """GraphNode with multiple outputs has all outputs type checked."""
        @node(output_name=("a", "b"))
        def multi_output(x: int) -> tuple[str, float]:
            return str(x), float(x)

        inner = Graph([multi_output], name="inner")
        inner_gn = inner.as_node()

        @node(output_name="c")
        def consumer_a(a: str) -> int:
            return len(a)

        @node(output_name="d")
        def consumer_b(b: float) -> int:
            return int(b)

        # Both outputs should match their consumers
        outer = Graph([inner_gn, consumer_a, consumer_b], strict_types=True)

        assert outer.strict_types is True
        assert outer.nx_graph.has_edge("inner", "consumer_a")
        assert outer.nx_graph.has_edge("inner", "consumer_b")

    def test_graphnode_with_multiple_outputs_mismatch(self):
        """Type mismatch detected when one of multiple outputs doesn't match."""
        @node(output_name=("a", "b"))
        def multi_output(x: int) -> tuple[str, float]:
            return str(x), float(x)

        inner = Graph([multi_output], name="inner")
        inner_gn = inner.as_node()

        @node(output_name="c")
        def consumer_a(a: str) -> int:  # Correct type
            return len(a)

        @node(output_name="d")
        def consumer_b(b: str) -> int:  # Wrong type - expects str, gets float
            return len(b)

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([inner_gn, consumer_a, consumer_b], strict_types=True)

        error_msg = str(exc_info.value)
        assert "Type mismatch" in error_msg
        assert "b" in error_msg


class TestCycleSameOutput:
    """Test that multiple nodes can produce the same output when in the same cycle."""

    def test_same_cycle_same_output_allowed(self):
        """Two nodes producing 'messages' in a cycle should not raise."""
        from hypergraph.nodes.gate import route, END

        @node(output_name="messages")
        def accumulate_query(messages: list, query: str) -> list:
            return messages + [{"role": "user", "content": query}]

        @node(output_name="messages")
        def accumulate_response(messages: list, response: str) -> list:
            return messages + [{"role": "assistant", "content": response}]

        @route(targets=["accumulate_query", END])
        def should_continue(messages: list) -> str:
            return END if len(messages) >= 4 else "accumulate_query"

        # Should NOT raise GraphConfigError
        graph = Graph([accumulate_query, accumulate_response, should_continue])
        assert graph is not None

    def test_same_cycle_same_output_runs(self):
        """Same-output cycle nodes should execute end-to-end."""
        from hypergraph.nodes.gate import route, END
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="messages")
        def accumulate_query(messages: list, query: str) -> list:
            return messages + [{"role": "user", "content": query}]

        @node(output_name="messages")
        def accumulate_response(messages: list, response: str) -> list:
            return messages + [{"role": "assistant", "content": response}]

        @route(targets=["accumulate_query", END])
        def should_continue(messages: list) -> str:
            return END if len(messages) >= 4 else "accumulate_query"

        graph = Graph([accumulate_query, accumulate_response, should_continue])
        runner = SyncRunner()
        result = runner.run(graph, {"messages": [], "query": "hi", "response": "hello"})
        assert "messages" in result
        assert len(result["messages"]) >= 2

    def test_non_cycle_same_output_still_raises(self):
        """Two nodes producing 'result' in a plain DAG should still raise."""

        @node(output_name="result")
        def producer_a(x: int) -> int:
            return x + 1

        @node(output_name="result")
        def producer_b(y: int) -> int:
            return y + 2

        with pytest.raises(GraphConfigError):
            Graph([producer_a, producer_b])

    def test_self_producers_property(self):
        """After the fix, graph should expose self_producers mapping output to producer sets."""
        from hypergraph.nodes.gate import route, END

        @node(output_name="messages")
        def accumulate_query(messages: list, query: str) -> list:
            return messages + [{"role": "user", "content": query}]

        @node(output_name="messages")
        def accumulate_response(messages: list, response: str) -> list:
            return messages + [{"role": "assistant", "content": response}]

        @route(targets=["accumulate_query", END])
        def should_continue(messages: list) -> str:
            return END if len(messages) >= 4 else "accumulate_query"

        graph = Graph([accumulate_query, accumulate_response, should_continue])

        # self_producers should map output names to sets of producer node names
        assert hasattr(graph, "self_producers")
        producers = graph.self_producers
        assert producers["messages"] == {"accumulate_query", "accumulate_response"}
