"""Unit tests for gate nodes (END, GateNode, RouteNode).

Tests cover:
- END sentinel behavior
- RouteNode construction and validation
- @route decorator usage
"""

import pytest

from hypergraph.nodes.gate import END, GateNode, RouteNode, route


# =============================================================================
# END Sentinel Tests
# =============================================================================


class TestENDSentinel:
    """Tests for the END sentinel class."""

    def test_end_cannot_be_instantiated(self):
        """END() should raise TypeError."""
        with pytest.raises(TypeError, match="cannot be instantiated"):
            END()

    def test_end_repr_is_clean(self):
        """END should have clean string representation."""
        assert repr(END) == "END"
        assert str(END) == "END"

    def test_end_is_class_not_instance(self):
        """END should be a class (type), not an instance."""
        assert isinstance(END, type)

    def test_end_not_equal_to_string(self):
        """END should not be equal to the string 'END'."""
        assert END != "END"
        assert "END" != END

    def test_end_identity(self):
        """Assigning END to a variable should preserve identity."""
        target = END
        assert target is END

    def test_end_usable_in_targets(self):
        """END should be usable in a targets list."""
        targets = ["process", END]
        assert END in targets
        assert "process" in targets


# =============================================================================
# RouteNode Construction Tests
# =============================================================================


class TestRouteNodeConstruction:
    """Tests for RouteNode creation and initialization."""

    def test_basic_construction_with_list(self):
        """Basic RouteNode construction with list targets."""

        @route(targets=["a", "b"])
        def decide(x: int) -> str:
            return "a"

        assert decide.targets == ["a", "b"]
        assert decide.descriptions == {}
        assert decide.inputs == ("x",)
        assert decide.outputs == ()  # Gates produce no data
        assert decide.name == "decide"

    def test_construction_with_descriptions(self):
        """RouteNode construction with dict targets (descriptions)."""

        @route(
            targets={
                "process": "Handle normal case",
                END: "Finished processing",
            }
        )
        def decide(x: int) -> str:
            return "process"

        assert set(decide.targets) == {"process", END}
        assert decide.descriptions["process"] == "Handle normal case"
        assert decide.descriptions[END] == "Finished processing"

    def test_construction_with_end_in_list(self):
        """END can be included in targets list."""

        @route(targets=["process", END])
        def decide(x: int):
            return END

        assert END in decide.targets
        assert "process" in decide.targets

    def test_empty_targets_raises(self):
        """Empty targets list should raise ValueError."""
        with pytest.raises(ValueError, match="at least one target"):

            @route(targets=[])
            def decide(x):
                return "a"

    def test_string_end_target_raises(self):
        """String 'END' as target should raise (too confusing with END sentinel)."""
        with pytest.raises(ValueError, match="'END' as a string target"):

            @route(targets=["END", "process"])
            def decide(x):
                return "process"

    def test_duplicate_targets_deduplicated(self):
        """Duplicate targets should be deduplicated (preserve order)."""

        @route(targets=["a", "a", "b"])
        def decide(x):
            return "a"

        assert decide.targets == ["a", "b"]

    def test_fallback_added_to_targets(self):
        """Fallback should be added to targets if not already present."""

        @route(targets=["a"], fallback="default")
        def decide(x):
            return None

        assert "default" in decide.targets
        assert decide.fallback == "default"

    def test_fallback_already_in_targets(self):
        """Fallback already in targets should not be duplicated."""

        @route(targets=["a", "default"], fallback="default")
        def decide(x):
            return None

        assert decide.targets.count("default") == 1

    def test_multi_target_incompatible_with_fallback(self):
        """multi_target=True is incompatible with fallback."""
        with pytest.raises(ValueError, match="cannot have both fallback and multi_target"):

            @route(targets=["a", "b"], multi_target=True, fallback="c")
            def decide(x):
                return []

    def test_custom_name(self):
        """Custom name should override function name."""

        @route(targets=["a"], name="my_gate")
        def decide(x):
            return "a"

        assert decide.name == "my_gate"

    def test_rename_inputs(self):
        """rename_inputs should rename input parameters."""

        @route(targets=["a"], rename_inputs={"x": "input_value"})
        def decide(x):
            return "a"

        assert decide.inputs == ("input_value",)

    def test_with_name_returns_new_instance(self):
        """with_name should return a new instance."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        renamed = decide.with_name("new_name")
        assert renamed.name == "new_name"
        assert decide.name == "decide"  # Original unchanged

    def test_with_inputs_returns_new_instance(self):
        """with_inputs should return a new instance."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        renamed = decide.with_inputs(x="value")
        assert renamed.inputs == ("value",)
        assert decide.inputs == ("x",)  # Original unchanged

    def test_async_routing_function_raises(self):
        """Async routing functions should be rejected at decoration time."""
        with pytest.raises(TypeError, match="cannot be async"):

            @route(targets=["a", "b"])
            async def decide(x):
                return "a"

    def test_generator_routing_function_raises(self):
        """Generator routing functions should be rejected."""
        with pytest.raises(TypeError, match="cannot be.*generator"):

            @route(targets=["a", "b"])
            def decide(x):
                yield "a"

    def test_callable_directly(self):
        """RouteNode should be directly callable."""

        @route(targets=["a", "b"])
        def decide(x):
            return "a" if x > 0 else "b"

        assert decide(5) == "a"
        assert decide(-5) == "b"

    def test_repr_without_rename(self):
        """RouteNode repr without renaming."""

        @route(targets=["a", "b"])
        def decide(x):
            return "a"

        assert "RouteNode(decide" in repr(decide)
        assert "targets=" in repr(decide)

    def test_repr_with_rename(self):
        """RouteNode repr with renaming."""

        @route(targets=["a", "b"], name="my_gate")
        def decide(x):
            return "a"

        assert "decide as 'my_gate'" in repr(decide)

    def test_is_async_always_false(self):
        """RouteNode.is_async should always be False."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert decide.is_async is False

    def test_is_generator_always_false(self):
        """RouteNode.is_generator should always be False."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert decide.is_generator is False

    def test_outputs_always_empty(self):
        """RouteNode.outputs should always be empty tuple."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert decide.outputs == ()

    def test_definition_hash_exists(self):
        """RouteNode should have a definition_hash."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert isinstance(decide.definition_hash, str)
        assert len(decide.definition_hash) == 64  # SHA256 hex

    def test_cache_default_false(self):
        """Cache should default to False."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert decide.cache is False

    def test_cache_can_be_true(self):
        """Cache can be set to True."""

        @route(targets=["a"], cache=True)
        def decide(x):
            return "a"

        assert decide.cache is True


# =============================================================================
# RouteNode Default Handling Tests
# =============================================================================


class TestRouteNodeDefaults:
    """Tests for RouteNode default value handling."""

    def test_has_default_for_with_default(self):
        """has_default_for should return True for params with defaults."""

        @route(targets=["a"])
        def decide(x, y=10):
            return "a"

        assert decide.has_default_for("y") is True

    def test_has_default_for_without_default(self):
        """has_default_for should return False for params without defaults."""

        @route(targets=["a"])
        def decide(x, y=10):
            return "a"

        assert decide.has_default_for("x") is False

    def test_get_default_for_with_default(self):
        """get_default_for should return the default value."""

        @route(targets=["a"])
        def decide(x, y=10):
            return "a"

        assert decide.get_default_for("y") == 10

    def test_get_default_for_without_default_raises(self):
        """get_default_for should raise KeyError for params without defaults."""

        @route(targets=["a"])
        def decide(x, y=10):
            return "a"

        with pytest.raises(KeyError, match="No default"):
            decide.get_default_for("x")

    def test_defaults_with_renamed_inputs(self):
        """Defaults should work with renamed inputs."""

        @route(targets=["a"], rename_inputs={"y": "threshold"})
        def decide(x, y=10):
            return "a"

        assert decide.has_default_for("threshold") is True
        assert decide.get_default_for("threshold") == 10


# =============================================================================
# RouteNode Multi-Target Tests
# =============================================================================


class TestRouteNodeMultiTarget:
    """Tests for multi_target RouteNode functionality."""

    def test_multi_target_basic(self):
        """multi_target=True should allow list returns."""

        @route(targets=["a", "b", "c"], multi_target=True)
        def decide(x):
            return ["a", "c"]

        assert decide.multi_target is True
        assert decide(1) == ["a", "c"]

    def test_multi_target_empty_list(self):
        """multi_target can return empty list."""

        @route(targets=["a", "b"], multi_target=True)
        def decide(x):
            return []

        assert decide(1) == []

    def test_single_target_default(self):
        """multi_target defaults to False."""

        @route(targets=["a", "b"])
        def decide(x):
            return "a"

        assert decide.multi_target is False


# =============================================================================
# GateNode Base Class Tests
# =============================================================================


class TestGateNodeBase:
    """Tests for GateNode base class behavior."""

    def test_routenode_is_gatenode(self):
        """RouteNode should be a subclass of GateNode."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert isinstance(decide, GateNode)

    def test_gatenode_has_targets(self):
        """GateNode should have targets attribute."""

        @route(targets=["a", "b"])
        def decide(x):
            return "a"

        assert hasattr(decide, "targets")
        assert decide.targets == ["a", "b"]

    def test_gatenode_has_descriptions(self):
        """GateNode should have descriptions attribute."""

        @route(targets={"a": "Option A"})
        def decide(x):
            return "a"

        assert hasattr(decide, "descriptions")
        assert decide.descriptions["a"] == "Option A"
