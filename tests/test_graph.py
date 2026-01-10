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
        assert edge_data["value_name"] == "a_out"

    def test_nx_graph_has_node_attributes(self):
        """Test NetworkX nodes contain hypernode references."""
        @node(output_name="result")
        def single(x: int) -> int:
            return x

        g = Graph([single])

        node_data = g.nx_graph.nodes["single"]
        assert node_data["hypernode"] is single

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
        with pytest.raises(RenameError, match="'a' was renamed to 'x'"):
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
