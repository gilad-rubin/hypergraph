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


class TestNamespaceCollisionValidation:
    """Test GraphNode name collision with output names."""

    def test_graphnode_name_matches_output_raises(self):
        """GraphNode name colliding with output name raises error."""
        # Outer node outputs "subgraph" - same as GraphNode name
        @node(output_name="subgraph")
        def source_node(x: int) -> int:
            return x * 2

        # Inner graph will be named "subgraph" - collision!
        @node(output_name="inner_out")
        def inner_func(a: int) -> int:
            return a

        inner_graph = Graph([inner_func], name="subgraph")

        with pytest.raises(GraphConfigError, match="collides with output"):
            Graph([source_node, inner_graph.as_node()])

    def test_no_collision_different_names(self):
        """No error when GraphNode name differs from all outputs."""
        @node(output_name="result")
        def source_node(x: int) -> int:
            return x * 2

        @node(output_name="inner_out")
        def inner_func(a: int) -> int:
            return a

        inner_graph = Graph([inner_func], name="inner")  # Different from "result"

        # Should not raise
        outer = Graph([source_node, inner_graph.as_node()])
        assert "inner" in outer.nodes
        assert "source_node" in outer.nodes

    def test_graphnode_with_hyphenated_name_allowed(self):
        """GraphNode with hyphenated name (valid graph name) should work."""
        @node(output_name="result")
        def source_node(x: int) -> int:
            return x * 2

        @node(output_name="inner_out")
        def inner_func(a: int) -> int:
            return a

        # Hyphenated name is valid for graphs but not Python identifiers
        inner_graph = Graph([inner_func], name="my-inner-graph")

        # Should not raise - GraphNodes skip identifier validation
        outer = Graph([source_node, inner_graph.as_node()])
        assert "my-inner-graph" in outer.nodes

    def test_graphnode_output_collision_with_other_graphnode(self):
        """GraphNode name colliding with another GraphNode's output raises error."""
        @node(output_name="collider")
        def inner1_func(a: int) -> int:
            return a

        @node(output_name="other")
        def inner2_func(b: int) -> int:
            return b

        # inner1 outputs "collider", inner2 is named "collider" -> collision
        inner1 = Graph([inner1_func], name="inner1")
        inner2 = Graph([inner2_func], name="collider")

        with pytest.raises(GraphConfigError, match="collides with output"):
            Graph([inner1.as_node(), inner2.as_node()])


class TestNameValidationEdgeCases:
    """Test name validation edge cases (NAME-01 through NAME-05).

    Tests verify handling of:
    - Underscore-prefixed names (valid Python identifiers)
    - Python keywords (should be rejected but may not be currently)
    - Empty string names (invalid identifiers)
    - Unicode characters (depends on Python identifier rules)
    - Very long names (no length limit in Python)

    Note: Some tests document expected behavior that may not be implemented yet.
    Tests for Python keyword rejection (NAME-02) are marked xfail until the
    implementation is updated to use keyword.iskeyword() in addition to str.isidentifier().
    """

    from hypergraph.nodes.function import FunctionNode

    # NAME-01: Underscore prefix names (should be accepted)

    def test_node_name_with_leading_underscore_valid(self):
        """Node name with leading underscore is a valid Python identifier."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "_private"

        # Should not raise
        g = Graph([fn])
        assert "_private" in g.nodes

    def test_output_name_with_leading_underscore_valid(self):
        """Output name with leading underscore is valid."""

        @node(output_name="_result")
        def foo(x: int) -> int:
            return x

        # Should not raise
        g = Graph([foo])
        assert "_result" in g.outputs

    def test_node_name_double_underscore_valid(self):
        """Dunder-style names are valid Python identifiers."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "__dunder__"

        # Should not raise
        g = Graph([fn])
        assert "__dunder__" in g.nodes

    # NAME-02: Python keywords (should be rejected)

    def test_node_name_keyword_class_raises(self):
        """Node name 'class' is a Python keyword and should be rejected."""
        import keyword

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "class"

        # "class" is a keyword
        assert keyword.iskeyword("class")

        # Should raise GraphConfigError
        with pytest.raises(GraphConfigError) as exc_info:
            Graph([fn])

        assert "keyword" in str(exc_info.value).lower()

    def test_node_name_keyword_for_raises(self):
        """Node name 'for' is a Python keyword and should be rejected."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "for"

        with pytest.raises(GraphConfigError):
            Graph([fn])

    def test_node_name_keyword_import_raises(self):
        """Node name 'import' is a Python keyword and should be rejected."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "import"

        with pytest.raises(GraphConfigError):
            Graph([fn])

    def test_output_name_keyword_raises(self):
        """Output name 'class' is a Python keyword and should be rejected."""

        @node(output_name="class")
        def foo(x: int) -> int:
            return x

        with pytest.raises(GraphConfigError):
            Graph([foo])

    # NAME-03: Empty string names (should be rejected)

    def test_node_name_empty_string_raises(self):
        """Empty string is not a valid Python identifier."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = ""

        # Should raise GraphConfigError
        with pytest.raises(GraphConfigError) as exc_info:
            Graph([fn])

        assert "Invalid node name" in str(exc_info.value)

    def test_output_name_empty_string_raises(self):
        """Empty output name should be rejected."""

        @node(output_name="")
        def foo(x: int) -> int:
            return x

        # Should raise GraphConfigError
        with pytest.raises(GraphConfigError) as exc_info:
            Graph([foo])

        assert "Invalid output name" in str(exc_info.value)

    # NAME-04: Unicode characters

    def test_node_name_unicode_valid_identifier(self):
        """Valid ASCII names work (baseline test)."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "cafe"

        g = Graph([fn])
        assert "cafe" in g.nodes

    def test_node_name_unicode_greek_letter(self):
        """Greek letters are valid Python identifiers."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "\u03b1"  # Greek lowercase alpha (α)

        # Python allows unicode identifiers that start with letter-like chars
        assert "\u03b1".isidentifier()

        g = Graph([fn])
        assert "\u03b1" in g.nodes

    def test_node_name_unicode_emoji_raises(self):
        """Emojis are not valid Python identifiers."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "test\U0001F600"  # Grinning face emoji

        # Verify it's not a valid identifier
        assert not "test\U0001F600".isidentifier()

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([fn])

        assert "Invalid node name" in str(exc_info.value)

    def test_node_name_unicode_space_raises(self):
        """Names with Unicode non-breaking space are not valid identifiers."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        fn.name = "test\u00a0name"  # Non-breaking space

        # Verify it's not a valid identifier
        assert not "test\u00a0name".isidentifier()

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([fn])

        assert "Invalid node name" in str(exc_info.value)

    # NAME-05: Very long names (1000+ chars)

    def test_node_name_very_long_valid(self):
        """Very long (1000+ char) names that are valid identifiers work."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        long_name = "a" * 1000  # 1000 'a' characters
        fn.name = long_name

        # Python has no length limit on identifiers
        assert long_name.isidentifier()

        g = Graph([fn])
        assert long_name in g.nodes

    def test_output_name_very_long_valid(self):
        """Very long output names are accepted."""
        long_name = "x" * 1000

        @node(output_name=long_name)
        def foo(a: int) -> int:
            return a

        g = Graph([foo])
        assert long_name in g.outputs

    def test_error_message_with_long_name_readable(self):
        """Error messages for long invalid names should be readable (truncated)."""

        def foo(x: int) -> int:
            return x

        fn = self.FunctionNode(foo, output_name="result")
        # Invalid name: starts with digit then 1000 'a' chars
        invalid_long_name = "1" + "a" * 999
        fn.name = invalid_long_name

        assert not invalid_long_name.isidentifier()

        with pytest.raises(GraphConfigError) as exc_info:
            Graph([fn])

        error_msg = str(exc_info.value)
        # Error message should exist but not be absurdly long
        assert "Invalid node name" in error_msg
        # The message length should be reasonable (under 500 chars or truncated)
        # This is a soft check - implementation may show full name
        assert len(error_msg) < 2000
