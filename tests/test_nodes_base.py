"""Tests for hypergraph.nodes.base."""

import pytest

from hypergraph.nodes._rename import RenameEntry, RenameError, _apply_renames
from hypergraph.nodes.base import HyperNode


class TestRenameEntry:
    """Tests for RenameEntry dataclass."""

    def test_create_entry(self):
        """Can create a RenameEntry."""
        entry = RenameEntry("inputs", "a", "b")
        assert entry.kind == "inputs"
        assert entry.old == "a"
        assert entry.new == "b"

    def test_is_frozen(self):
        """RenameEntry is immutable (frozen)."""
        entry = RenameEntry("inputs", "a", "b")
        with pytest.raises(AttributeError):
            entry.old = "c"  # type: ignore[misc]

    def test_equality(self):
        """Two entries with same values are equal."""
        entry1 = RenameEntry("inputs", "a", "b")
        entry2 = RenameEntry("inputs", "a", "b")
        assert entry1 == entry2

    def test_hashable(self):
        """RenameEntry can be used in sets and as dict keys."""
        entry = RenameEntry("inputs", "a", "b")
        # Can add to set
        s = {entry}
        assert entry in s
        # Can use as dict key
        d = {entry: "value"}
        assert d[entry] == "value"


class TestRenameError:
    """Tests for RenameError exception."""

    def test_is_exception_subclass(self):
        """RenameError is an Exception subclass."""
        assert issubclass(RenameError, Exception)

    def test_message_preserved(self):
        """Error message is preserved."""
        error = RenameError("test message")
        assert str(error) == "test message"


class TestApplyRenames:
    """Tests for _apply_renames function."""

    def test_none_mapping(self):
        """None mapping returns original values and empty history."""
        values = ("a", "b")
        new_values, history = _apply_renames(values, None, "inputs")
        assert new_values == ("a", "b")
        assert history == []

    def test_empty_mapping(self):
        """Empty mapping returns original values and empty history."""
        values = ("a", "b")
        new_values, history = _apply_renames(values, {}, "inputs")
        assert new_values == ("a", "b")
        assert history == []

    def test_single_rename(self):
        """Single rename is applied correctly."""
        values = ("a", "b")
        new_values, history = _apply_renames(values, {"a": "x"}, "inputs")
        assert new_values == ("x", "b")
        assert len(history) == 1
        assert history[0] == RenameEntry("inputs", "a", "x")

    def test_multiple_renames(self):
        """Multiple renames are applied correctly."""
        values = ("a", "b")
        new_values, history = _apply_renames(values, {"a": "x", "b": "y"}, "inputs")
        assert new_values == ("x", "y")
        assert len(history) == 2
        # Check both entries exist (order may vary due to dict iteration)
        assert RenameEntry("inputs", "a", "x") in history
        assert RenameEntry("inputs", "b", "y") in history

    def test_rename_nonexistent_raises(self):
        """Renaming non-existent name raises RenameError."""
        values = ("a", "b")
        # "c" doesn't exist in values, validation should catch this
        with pytest.raises(RenameError, match="Cannot rename unknown inputs: 'c'"):
            _apply_renames(values, {"c": "x"}, "inputs")

    def test_outputs_kind(self):
        """Kind is correctly set for outputs."""
        values = ("a",)
        new_values, history = _apply_renames(values, {"a": "x"}, "outputs")
        assert new_values == ("x",)
        assert history[0].kind == "outputs"


class TestHyperNode:
    """Tests for HyperNode abstract base class."""

    def test_cannot_instantiate_directly(self):
        """HyperNode cannot be instantiated directly."""
        with pytest.raises(TypeError):
            HyperNode()  # type: ignore[abstract]

    def test_with_name_returns_new_instance(self):
        """with_name returns new instance, original unchanged."""
        # Use FunctionNode to test HyperNode methods
        from hypergraph.nodes.function import FunctionNode

        def foo(x):
            pass

        original = FunctionNode(foo, output_name="result")
        renamed = original.with_name("bar")

        assert original.name == "foo"
        assert renamed.name == "bar"
        assert original is not renamed

    def test_with_inputs_kwargs(self):
        """with_inputs with kwargs renames inputs."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a, b):
            pass

        node = FunctionNode(foo, output_name="result")
        renamed = node.with_inputs(a="x")

        assert node.inputs == ("a", "b")
        assert renamed.inputs == ("x", "b")

    def test_with_inputs_dict(self):
        """with_inputs with dict renames inputs."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a, b):
            pass

        node = FunctionNode(foo, output_name="result")
        renamed = node.with_inputs({"a": "x"})

        assert renamed.inputs == ("x", "b")

    def test_with_inputs_combined(self):
        """with_inputs with dict and kwargs combines them."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a, b):
            pass

        node = FunctionNode(foo, output_name="result")
        renamed = node.with_inputs({"a": "x"}, b="y")

        assert renamed.inputs == ("x", "y")

    def test_with_outputs_same_patterns(self):
        """with_outputs follows same patterns as with_inputs."""
        from hypergraph.nodes.function import FunctionNode

        def foo(x):
            pass

        node = FunctionNode(foo, output_name=("a", "b"))
        renamed = node.with_outputs(a="x")

        assert node.outputs == ("a", "b")
        assert renamed.outputs == ("x", "b")

    def test_rename_nonexistent_raises(self):
        """Renaming non-existent name raises RenameError."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a, b):
            pass

        node = FunctionNode(foo, output_name="result")
        with pytest.raises(RenameError, match="'nonexistent' not found"):
            node.with_inputs(nonexistent="x")

    def test_error_shows_history(self):
        """Error message shows rename history when applicable."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a, b):
            pass

        node = FunctionNode(foo, output_name="result")
        renamed = node.with_inputs(a="x")

        # Now try to rename 'a' again (it was renamed to 'x')
        with pytest.raises(RenameError, match="'a' was renamed to 'x'"):
            renamed.with_inputs(a="y")

    def test_chained_renames_track_history(self):
        """Chained renames accumulate in history."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a, b, c):
            pass

        node = FunctionNode(foo, output_name="result")
        step1 = node.with_inputs(a="x")
        step2 = step1.with_inputs(b="y")

        assert len(step2._rename_history) == 2
        assert RenameEntry("inputs", "a", "x") in step2._rename_history
        assert RenameEntry("inputs", "b", "y") in step2._rename_history

    def test_copy_independent_history(self):
        """_copy creates independent history list."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a):
            pass

        node = FunctionNode(foo, output_name="result")
        renamed = node.with_inputs(a="x")

        # Original history should be empty
        assert len(node._rename_history) == 0
        # Renamed history should have the entry
        assert len(renamed._rename_history) == 1

    def test_with_inputs_empty_returns_copy(self):
        """with_inputs with no arguments returns a copy."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a):
            pass

        node = FunctionNode(foo, output_name="result")
        copy_node = node.with_inputs()

        assert node is not copy_node
        assert node.inputs == copy_node.inputs

    def test_with_outputs_empty_returns_copy(self):
        """with_outputs with no arguments returns a copy."""
        from hypergraph.nodes.function import FunctionNode

        def foo(a):
            pass

        node = FunctionNode(foo, output_name="result")
        copy_node = node.with_outputs()

        assert node is not copy_node
        assert node.outputs == copy_node.outputs
