"""Tests for bind/unbind edge cases."""

import pytest
from hypergraph.graph import Graph
from hypergraph.nodes.function import node


class TestBindNone:
    """Test bind(x=None) correctly binds None as a value (BIND-01).

    The key distinction: bind(x=None) should store None as a bound value,
    making x optional with value None. This is different from unbind("x")
    which would remove the binding entirely.
    """

    def test_bind_none_makes_param_optional(self):
        """bind(x=None) moves param from required to optional."""

        @node(output_name="result")
        def foo(x: int) -> int:
            return x if x is not None else 0

        g = Graph([foo])

        # Before bind: x is required
        assert "x" in g.inputs.required

        # After bind(x=None): x is optional (bound)
        g2 = g.bind(x=None)
        assert "x" not in g2.inputs.required
        assert "x" in g2.inputs.optional

    def test_bind_none_preserved_in_bound_dict(self):
        """None is preserved as the actual bound value."""

        @node(output_name="result")
        def foo(x: int) -> int:
            return x if x is not None else 0

        g = Graph([foo])
        g2 = g.bind(x=None)

        # None is the actual value, not removed
        assert "x" in g2.inputs.bound
        assert g2.inputs.bound["x"] is None

    def test_bind_none_vs_unbind(self):
        """bind(x=None) is NOT the same as unbind."""

        @node(output_name="result")
        def foo(x: int) -> int:
            return x if x is not None else 0

        g = Graph([foo])

        # Bind with value, then bind with None
        g2 = g.bind(x=10)
        g3 = g2.bind(x=None)

        # x is still bound (to None), not unbound
        assert "x" in g3.inputs.bound
        assert g3.inputs.bound["x"] is None
        assert "x" in g3.inputs.optional

        # Compare with actual unbind
        g4 = g2.unbind("x")
        assert "x" not in g4.inputs.bound
        assert "x" in g4.inputs.required  # Returns to required

    def test_bind_none_multiple_params(self):
        """Bind multiple params where some are None."""

        @node(output_name="result")
        def foo(x: int, y: int, z: int) -> int:
            return (x or 0) + (y or 0) + (z or 0)

        g = Graph([foo])
        g2 = g.bind(x=None, y=10, z=None)

        # All three are now optional
        assert "x" in g2.inputs.optional
        assert "y" in g2.inputs.optional
        assert "z" in g2.inputs.optional

        # Bound dict has correct values
        assert g2.inputs.bound["x"] is None
        assert g2.inputs.bound["y"] == 10
        assert g2.inputs.bound["z"] is None


class TestBindMultiple:
    """Test bind() with multiple values at once (BIND-02)."""

    def test_bind_multiple_all_become_optional(self):
        """All bound params move to optional in single call."""

        @node(output_name="result")
        def foo(a: int, b: int, c: int, d: int) -> int:
            return a + b + c + d

        g = Graph([foo])

        # Bind three at once
        g2 = g.bind(a=1, b=2, c=3)

        # a, b, c are optional (bound)
        assert "a" in g2.inputs.optional
        assert "b" in g2.inputs.optional
        assert "c" in g2.inputs.optional

        # d remains required
        assert "d" in g2.inputs.required

        # Bound dict has all three
        assert g2.inputs.bound == {"a": 1, "b": 2, "c": 3}

    def test_bind_multiple_preserves_existing_bindings(self):
        """Chained bind calls preserve prior bindings."""

        @node(output_name="result")
        def foo(a: int, b: int, c: int) -> int:
            return a + b + c

        g = Graph([foo])
        g2 = g.bind(a=1)
        g3 = g2.bind(b=2, c=3)

        # All three are bound
        assert g3.inputs.bound == {"a": 1, "b": 2, "c": 3}

    def test_bind_multiple_override_partial(self):
        """Override some keys while adding others."""

        @node(output_name="result")
        def foo(a: int, b: int, c: int) -> int:
            return a + b + c

        g = Graph([foo])
        g2 = g.bind(a=1, b=2)
        g3 = g2.bind(b=20, c=3)

        # a stays at 1, b overridden to 20, c added
        assert g3.inputs.bound == {"a": 1, "b": 20, "c": 3}

    def test_bind_multiple_empty_call(self):
        """bind() with no args is valid no-op."""

        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        g = Graph([foo])
        g2 = g.bind()

        # No changes
        assert g2.inputs.bound == {}
        assert "x" in g2.inputs.required


class TestBindCycleSeeds:
    """Test bind() interaction with cycle seeds (BIND-03).

    Edge-produced values (including cycle seeds) cannot be bound because
    they are outputs of nodes, not external inputs.
    """

    def test_bind_seed_param_rejected(self):
        """Binding a seed param raises ValueError because it's edge-produced."""

        @node(output_name="count")
        def counter(count: int) -> int:
            return count + 1

        g = Graph([counter])

        # count is a seed (self-loop cycle)
        assert "count" in g.inputs.seeds

        # Attempting to bind raises ValueError
        with pytest.raises(ValueError) as exc_info:
            g.bind(count=0)

        assert "output of node" in str(exc_info.value)

    def test_seed_not_bindable_multi_node_cycle(self):
        """Multi-node cycle seed also not bindable."""

        @node(output_name="a")
        def node_a(b: int) -> int:
            return b + 1

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a * 2

        g = Graph([node_a, node_b])

        # Both a and b are cycle-related
        assert g.has_cycles

        # Attempting to bind either should fail (they're edge-produced)
        with pytest.raises(ValueError):
            g.bind(a=0)

        with pytest.raises(ValueError):
            g.bind(b=0)

    def test_non_seed_edge_produced_not_bindable(self):
        """Regular edge-produced values (not seeds) also not bindable."""

        @node(output_name="x")
        def producer(input_val: int) -> int:
            return input_val * 2

        @node(output_name="y")
        def consumer(x: int) -> int:
            return x + 1

        g = Graph([producer, consumer])

        # x is produced by producer, consumed by consumer
        # Only input_val is a valid input
        assert "input_val" in g.inputs.required
        assert "x" not in g.inputs.all

        # Can't bind x (it's an output)
        with pytest.raises(ValueError):
            g.bind(x=10)


class TestUnbindRestoresStatus:
    """Test unbind() restores correct required vs optional status (BIND-04).

    After unbind, a parameter's status depends on whether it had a default
    in the original function.
    """

    def test_unbind_restores_required(self):
        """Param without default returns to required after unbind."""

        @node(output_name="result")
        def foo(x: int) -> int:
            return x

        g = Graph([foo])
        assert "x" in g.inputs.required

        # Bind makes it optional
        g2 = g.bind(x=10)
        assert "x" in g2.inputs.optional

        # Unbind restores to required (no function default)
        g3 = g2.unbind("x")
        assert "x" in g3.inputs.required
        assert "x" not in g3.inputs.bound

    def test_unbind_restores_optional_with_default(self):
        """Param with default stays optional after unbind."""

        @node(output_name="result")
        def foo(y: int = 10) -> int:
            return y

        g = Graph([foo])
        # y has default, so it's optional
        assert "y" in g.inputs.optional

        # Bind overrides default
        g2 = g.bind(y=20)
        assert g2.inputs.bound["y"] == 20

        # Unbind removes binding, but y stays optional (has function default)
        g3 = g2.unbind("y")
        assert "y" not in g3.inputs.bound
        assert "y" in g3.inputs.optional  # Still optional due to function default

    def test_unbind_multiple_mixed(self):
        """Unbind multiple params with different statuses."""

        @node(output_name="result")
        def foo(a: int, b: int = 1, c: int = None, d: int = 2) -> int:
            return (a or 0) + (b or 0) + (c or 0) + (d or 0)

        g = Graph([foo])

        # Bind all four
        g2 = g.bind(a=10, b=20, c=30, d=40)
        assert all(k in g2.inputs.bound for k in ["a", "b", "c", "d"])

        # Unbind a and b only
        g3 = g2.unbind("a", "b")

        # a returns to required (no default)
        assert "a" in g3.inputs.required

        # b returns to optional (has default 1)
        assert "b" in g3.inputs.optional
        assert "b" not in g3.inputs.bound

        # c and d remain bound
        assert "c" in g3.inputs.bound
        assert "d" in g3.inputs.bound

    def test_unbind_preserves_other_bindings(self):
        """Unbind is selective - other bindings preserved."""

        @node(output_name="result")
        def foo(a: int, b: int, c: int) -> int:
            return a + b + c

        g = Graph([foo])
        g2 = g.bind(a=1, b=2, c=3)

        # Unbind only b
        g3 = g2.unbind("b")

        # a and c remain bound
        assert g3.inputs.bound == {"a": 1, "c": 3}
        # b is now required again
        assert "b" in g3.inputs.required

    def test_unbind_chained(self):
        """Multiple unbind calls work correctly when chained."""

        @node(output_name="result")
        def foo(a: int, b: int, c: int) -> int:
            return a + b + c

        g = Graph([foo])
        g2 = g.bind(a=1, b=2, c=3)

        # Chain unbind calls
        g3 = g2.unbind("a").unbind("b")

        # Only c remains bound
        assert g3.inputs.bound == {"c": 3}
        assert "a" in g3.inputs.required
        assert "b" in g3.inputs.required
