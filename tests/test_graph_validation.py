"""Tests for graph validation (build-time checks)."""

import pytest
from hypergraph.graph import Graph, GraphConfigError
from hypergraph.nodes.function import node


class TestGraphNameValidation:
    """Test graph name validation (reserved characters)."""

    def test_valid_graph_name(self):
        """Test graph names with valid characters work."""
        @node(output_name="result")
        def add(x: int, y: int) -> int:
            return x + y

        # Valid names
        g1 = Graph([add], name="my_graph")
        assert g1.name == "my_graph"

        g2 = Graph([add], name="my-graph")
        assert g2.name == "my-graph"

        g3 = Graph([add], name="MyGraph123")
        assert g3.name == "MyGraph123"

    def test_graph_name_with_dot_raises(self):
        """Test graph name with dot raises GraphConfigError."""
        @node(output_name="result")
        def add(x: int, y: int) -> int:
            return x + y

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([add], name="my.graph")

        assert "Invalid graph name" in str(exc_info.value)
        assert "my.graph" in str(exc_info.value)
        assert "cannot contain '.'" in str(exc_info.value)

    def test_graph_name_with_slash_raises(self):
        """Test graph name with slash raises GraphConfigError."""
        @node(output_name="result")
        def add(x: int, y: int) -> int:
            return x + y

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([add], name="my/graph")

        assert "Invalid graph name" in str(exc_info.value)
        assert "my/graph" in str(exc_info.value)
        assert "cannot contain '/'" in str(exc_info.value)

    def test_graph_name_none_allowed(self):
        """Test graph with name=None is valid."""
        @node(output_name="result")
        def add(x: int, y: int) -> int:
            return x + y

        g = Graph([add], name=None)
        assert g.name is None


class TestNodeAndOutputNameValidation:
    """Test node and output names must be valid identifiers."""

    def test_valid_node_name(self):
        """Test valid identifier node names work."""
        @node(output_name="result")
        def my_node(x: int) -> int:
            return x

        g = Graph([my_node])
        assert "my_node" in g.nodes

    def test_invalid_node_name_raises(self):
        """Test non-identifier node name raises GraphConfigError."""
        @node(output_name="result")
        def bad_func(x: int) -> int:
            return x

        # Rename to invalid identifier
        bad_func.name = "bad-name"  # Hyphen not valid in identifier

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([bad_func])

        assert "Invalid node name" in str(exc_info.value)
        assert "bad-name" in str(exc_info.value)
        assert "valid Python identifiers" in str(exc_info.value)

    def test_invalid_output_name_raises(self):
        """Test non-identifier output name raises GraphConfigError."""
        @node(output_name="bad-output")  # Hyphen not valid
        def my_func(x: int) -> int:
            return x

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([my_func])

        assert "Invalid output name" in str(exc_info.value)
        assert "bad-output" in str(exc_info.value)
        assert "valid Python identifiers" in str(exc_info.value)

    def test_node_name_with_number_start_raises(self):
        """Test node name starting with number raises error."""
        @node(output_name="result")
        def func(x: int) -> int:
            return x

        func.name = "123func"  # Invalid identifier

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([func])

        assert "Invalid node name" in str(exc_info.value)
        assert "123func" in str(exc_info.value)


class TestConsistentDefaultsValidation:
    """Test consistent defaults validation across shared parameters."""

    def test_consistent_defaults_ok(self):
        """Test shared param with same default in all nodes works."""
        @node(output_name="r1")
        def node1(x: int = 5) -> int:
            return x

        @node(output_name="r2")
        def node2(x: int = 5) -> int:
            return x * 2

        # Both have x with default=5
        g = Graph([node1, node2])
        assert g.inputs.optional == ("x",)

    def test_inconsistent_defaults_some_have_some_not(self):
        """Test shared param where some nodes have default and some don't raises error."""
        @node(output_name="r1")
        def node1(x: int = 5) -> int:
            return x

        @node(output_name="r2")
        def node2(x: int) -> int:  # No default
            return x * 2

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([node1, node2])

        assert "Inconsistent defaults for 'x'" in str(exc_info.value)
        assert "node1" in str(exc_info.value)
        assert "node2" in str(exc_info.value)
        assert "without default" in str(exc_info.value)

    def test_inconsistent_defaults_different_values(self):
        """Test shared param with different default values raises error."""
        @node(output_name="r1")
        def node1(x: int = 5) -> int:
            return x

        @node(output_name="r2")
        def node2(x: int = 10) -> int:
            return x * 2

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([node1, node2])

        assert "Inconsistent defaults for 'x'" in str(exc_info.value)
        assert "node1" in str(exc_info.value)
        assert "node2" in str(exc_info.value)
        assert "5" in str(exc_info.value)
        assert "10" in str(exc_info.value)

    def test_param_used_by_only_one_node_ok(self):
        """Test param used by single node doesn't trigger validation."""
        @node(output_name="r1")
        def node1(x: int = 5) -> int:
            return x

        @node(output_name="r2")
        def node2(y: int) -> int:
            return y * 2

        # x only in node1, y only in node2 - no sharing, no validation
        g = Graph([node1, node2])
        assert "x" in g.inputs.all
        assert "y" in g.inputs.all

    def test_consistent_defaults_with_bind_ok(self):
        """Test bind() doesn't interfere with default validation."""
        @node(output_name="r1")
        def node1(x: int = 5) -> int:
            return x

        @node(output_name="r2")
        def node2(x: int = 5) -> int:
            return x * 2

        g = Graph([node1, node2])
        g2 = g.bind(x=100)

        # Bind changes the value but doesn't affect structure validation
        assert g2.inputs.bound == {"x": 100}
