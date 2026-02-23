"""Integration tests for hypergraph FunctionNode."""

import pytest

from hypergraph import FunctionNode, HyperNode, RenameError, node


class TestEndToEndWorkflow:
    """End-to-end workflow tests."""

    def test_create_rename_call(self):
        """Full workflow: create node, rename, call."""

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        # Original node
        assert double.name == "double"
        assert double.inputs == ("x",)
        assert double.outputs == ("doubled",)

        # Rename input
        renamed = double.with_inputs(x="value")
        assert renamed.inputs == ("value",)
        assert renamed.outputs == ("doubled",)

        # Call still works
        assert renamed(5) == 10

    def test_chain_multiple_renames(self):
        """Chain with_inputs, with_outputs, and with_name."""

        @node(output_name=("mean", "std"))
        def stats(data: list) -> tuple:
            mean = sum(data) / len(data)
            std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
            return mean, std

        # Chain renames
        renamed = stats.with_inputs(data="input_data").with_outputs(mean="average", std="standard_deviation").with_name("compute_statistics")

        assert renamed.name == "compute_statistics"
        assert renamed.inputs == ("input_data",)
        assert renamed.outputs == ("average", "standard_deviation")

        # Original unchanged
        assert stats.name == "stats"
        assert stats.inputs == ("data",)
        assert stats.outputs == ("mean", "std")

    def test_error_message_accuracy(self):
        """Verify error messages contain helpful context."""

        @node(output_name="result")
        def process(input_a, input_b):
            return input_a + input_b

        # Rename input_a to x
        renamed = process.with_inputs(input_a="x")

        # Try to rename input_a again (should fail with helpful message)
        with pytest.raises(RenameError) as exc_info:
            renamed.with_inputs(input_a="y")

        error_msg = str(exc_info.value)
        assert "'input_a' was renamed: input_a→x" in error_msg
        assert "Current inputs:" in error_msg

    def test_multiple_nodes_from_same_func(self):
        """Different configurations from same function work independently."""

        def transform(value):
            return value * 2

        node_a = FunctionNode(transform, name="doubler", output_name="doubled")
        node_b = FunctionNode(transform, name="multiplier", output_name="result")

        assert node_a.name == "doubler"
        assert node_a.outputs == ("doubled",)

        assert node_b.name == "multiplier"
        assert node_b.outputs == ("result",)

        # Both still call same underlying function
        assert node_a(5) == 10
        assert node_b(5) == 10


class TestAsyncFunctionNode:
    """Tests for async function nodes."""

    def test_async_function_detection(self):
        """Async function is properly detected."""

        @node(output_name="result")
        async def fetch_data(url: str) -> str:
            return f"fetched: {url}"

        assert fetch_data.is_async is True
        assert fetch_data.is_generator is False

    @pytest.mark.asyncio
    async def test_async_function_call(self):
        """Async function can be awaited."""

        @node(output_name="result")
        async def fetch_data(url: str) -> str:
            return f"fetched: {url}"

        result = await fetch_data("http://example.com")
        assert result == "fetched: http://example.com"


class TestGeneratorFunctionNode:
    """Tests for generator function nodes."""

    def test_generator_function_detection(self):
        """Generator function is properly detected."""

        @node(output_name="values")
        def generate_numbers(n: int):
            yield from range(n)

        assert generate_numbers.is_async is False
        assert generate_numbers.is_generator is True

    def test_generator_function_call(self):
        """Generator function yields values."""

        @node(output_name="values")
        def generate_numbers(n: int):
            yield from range(n)

        result = list(generate_numbers(3))
        assert result == [0, 1, 2]


class TestAsyncGeneratorFunctionNode:
    """Tests for async generator function nodes."""

    def test_async_generator_detection(self):
        """Async generator is properly detected."""

        @node(output_name="chunks")
        async def stream_data(count: int):
            for i in range(count):
                yield f"chunk_{i}"

        assert stream_data.is_async is True
        assert stream_data.is_generator is True

    @pytest.mark.asyncio
    async def test_async_generator_call(self):
        """Async generator yields values when awaited."""

        @node(output_name="chunks")
        async def stream_data(count: int):
            for i in range(count):
                yield f"chunk_{i}"

        result = []
        async for chunk in stream_data(3):
            result.append(chunk)
        assert result == ["chunk_0", "chunk_1", "chunk_2"]


class TestPublicAPIImports:
    """Test that public API imports work correctly."""

    def test_import_from_hypergraph(self):
        """All public symbols importable from hypergraph."""
        from hypergraph import FunctionNode, HyperNode, RenameError, node

        # All should be accessible
        assert node is not None
        assert FunctionNode is not None
        assert HyperNode is not None
        assert RenameError is not None

    def test_import_from_nodes_subpackage(self):
        """Symbols also available from nodes subpackage."""
        from hypergraph.nodes import (
            FunctionNode,
            HyperNode,
            RenameEntry,
            RenameError,
            node,
        )

        assert node is not None
        assert FunctionNode is not None
        assert HyperNode is not None
        assert RenameEntry is not None
        assert RenameError is not None


class TestImmutabilityPattern:
    """Tests verifying the immutability pattern."""

    def test_with_name_does_not_mutate(self):
        """with_name returns new instance, original unchanged."""

        @node(output_name="result")
        def foo():
            return 1

        original_name = foo.name
        renamed = foo.with_name("bar")

        assert foo.name == original_name
        assert renamed.name == "bar"

    def test_with_inputs_does_not_mutate(self):
        """with_inputs returns new instance, original unchanged."""

        @node(output_name="result")
        def foo(a, b):
            return a + b

        original_inputs = foo.inputs
        renamed = foo.with_inputs(a="x")

        assert foo.inputs == original_inputs
        assert renamed.inputs == ("x", "b")

    def test_with_outputs_does_not_mutate(self):
        """with_outputs returns new instance, original unchanged."""

        @node(output_name=("a", "b"))
        def foo():
            return 1, 2

        original_outputs = foo.outputs
        renamed = foo.with_outputs(a="x")

        assert foo.outputs == original_outputs
        assert renamed.outputs == ("x", "b")


class TestDefinitionHashConsistency:
    """Tests for definition_hash consistency."""

    def test_same_function_same_hash(self):
        """Same function always produces same hash."""

        @node(output_name="result")
        def foo():
            return 42

        hash1 = foo.definition_hash
        hash2 = foo.definition_hash

        assert hash1 == hash2

    def test_hash_persists_through_renames(self):
        """Hash remains consistent through renames."""

        @node(output_name="result")
        def foo(x):
            return x * 2

        original_hash = foo.definition_hash
        renamed = foo.with_name("bar").with_inputs(x="value")

        assert renamed.definition_hash == original_hash


class TestHyperNodeAbstract:
    """Tests confirming HyperNode is abstract."""

    def test_function_node_is_hyper_node(self):
        """FunctionNode is a HyperNode subclass."""

        @node(output_name="result")
        def foo():
            return 1

        assert isinstance(foo, HyperNode)

    def test_cannot_instantiate_hyper_node(self):
        """HyperNode cannot be instantiated directly."""
        with pytest.raises(TypeError):
            HyperNode()  # type: ignore[abstract]
