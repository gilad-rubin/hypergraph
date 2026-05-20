"""Unit tests for gate nodes (END, GateNode, RouteNode).

Tests cover:
- END sentinel behavior
- RouteNode construction and validation
- @route decorator usage
"""

import pytest

from hypergraph.nodes.gate import END, GateNode, route

# =============================================================================
# END Sentinel Tests
# =============================================================================


class TestENDSentinel:
    """Tests for the END sentinel singleton."""

    def test_end_is_str_subtype(self):
        """END is a str instance so it's accepted by `-> str` annotations."""
        assert isinstance(END, str)

    def test_end_repr_is_clean(self):
        """END should have clean string representation."""
        assert repr(END) == "END"
        assert str(END) == "END"

    def test_end_not_equal_to_literal_end_string(self):
        """END's underlying value is hidden, so it doesn't equal the string 'END'."""
        assert END != "END"

    def test_end_identity(self):
        """Assigning END to a variable should preserve identity."""
        target = END
        assert target is END

    def test_end_usable_in_targets(self):
        """END should be usable in a targets list."""
        targets = ["process", END]
        assert END in targets
        assert "process" in targets

    def test_external_string_collision_rejected_during_graph_run(self):
        """A gate returning the raw underlying value (e.g. from an LLM)
        must raise during graph execution, not silently terminate.

        This guards the runner path (execute_route -> validate_routing_decision),
        which calls node.func directly and bypasses RouteNode.__call__.
        Set membership in valid_targets would otherwise accept the collision
        because hash and __eq__ both match END.
        """
        from hypergraph import Graph, SyncRunner, node

        @node(output_name="x")
        def start() -> int:
            return 1

        @route(targets=["a", END])
        def collision_gate(x: int) -> str:
            return "__hg_end__"  # collides with END's underlying value

        @node(output_name="result")
        def a(x: int) -> int:
            return x

        graph = Graph([start, collision_gate, a])
        with pytest.raises(ValueError, match="not the END sentinel"):
            SyncRunner().run(graph)

    def test_external_string_collision_rejected_in_multi_target_list(self):
        """Collision detection must recurse into multi_target lists."""
        from hypergraph import Graph, SyncRunner, node

        @node(output_name="x")
        def start() -> int:
            return 1

        @route(targets=["a", "b", END], multi_target=True)
        def multi_gate(x: int) -> list[str]:
            return ["a", "__hg_end__"]  # one good item, one collision

        @node(output_name="ra")
        def a(x: int) -> int:
            return x

        @node(output_name="rb")
        def b(x: int) -> int:
            return x

        graph = Graph([start, multi_gate, a, b])
        with pytest.raises(ValueError, match="not the END sentinel"):
            SyncRunner().run(graph)

    def test_end_pickle_round_trip_preserves_singleton(self):
        """END must remain the same singleton across pickle/deepcopy.

        Without __reduce__, pickle.loads would call _End("__hg_end__")
        — but _End.__new__ takes no args, so it would raise TypeError.
        And even if reconstruction succeeded the result would be a fresh
        instance, breaking `decision is END` checks that the persistent
        DiskCache (which uses pickle.dumps under the hood) and any other
        pickle-based round-trip rely on.
        """
        import copy
        import pickle

        restored = pickle.loads(pickle.dumps(END))
        assert restored is END

        deep = copy.deepcopy(END)
        assert deep is END


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
        assert decide.outputs == ("_decide",)
        assert decide.data_outputs == ("_decide",)
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
        with pytest.raises(ValueError, match="reserved END-like string target"):

            @route(targets=["END", "process"])
            def decide(x):
                return "process"

    def test_reserved_underlying_value_target_raises(self):
        """END's hidden underlying value must be rejected at registration.

        If a user accidentally types the raw value as a target, it would
        otherwise reach the runtime collision check — but rejecting at
        configuration time gives a clearer, earlier error.
        """
        with pytest.raises(ValueError, match="reserved END-like string target"):

            @route(targets=["__hg_end__", "process"])
            def decide(x):
                return "process"

    def test_string_end_fallback_raises(self):
        """Fallback must also be validated at registration, not just targets."""
        with pytest.raises(ValueError, match="reserved END-like string target"):

            @route(targets=["a"], fallback="END")
            def decide(x):
                return None

    def test_reserved_underlying_value_fallback_raises(self):
        """Fallback must also reject the raw underlying value."""
        with pytest.raises(ValueError, match="reserved END-like string target"):

            @route(targets=["a"], fallback="__hg_end__")
            def decide(x):
                return None

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

    def test_rename_inputs_returns_new_instance(self):
        """rename_inputs should return a new instance."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        renamed = decide.rename_inputs(x="value")
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

    def test_outputs_include_internal_gate_value(self):
        """RouteNode outputs include the internal gate value."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert decide.outputs == ("_decide",)

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

    def test_gatenode_default_open_defaults_true(self):
        """GateNode default_open should default to True."""

        @route(targets=["a"])
        def decide(x):
            return "a"

        assert decide.default_open is True

    def test_gatenode_default_open_can_be_set(self):
        """GateNode default_open should be configurable."""

        @route(targets=["a"], default_open=False)
        def decide(x):
            return "a"

        assert decide.default_open is False


# =============================================================================
# GateNode Type Annotation Tests
# =============================================================================


class TestGateNodeTypeAnnotations:
    """Tests for get_input_type on gate nodes."""

    def test_route_get_input_type_returns_annotation(self):
        """RouteNode.get_input_type returns the type annotation."""

        @route(targets=["a", "b"])
        def gate(x: int) -> str:
            return "a"

        assert gate.get_input_type("x") is int

    def test_route_get_input_type_returns_none_when_untyped(self):
        """RouteNode.get_input_type returns None for untyped parameters."""

        @route(targets=["a", "b"])
        def gate(x) -> str:
            return "a"

        assert gate.get_input_type("x") is None

    def test_ifelse_get_input_type_returns_annotation(self):
        """IfElseNode.get_input_type returns the type annotation."""
        from hypergraph.nodes.gate import ifelse

        @ifelse(when_true="yes", when_false="no")
        def gate(decision: bool) -> bool:
            return decision

        assert gate.get_input_type("decision") is bool

    def test_route_get_input_type_handles_renamed_inputs(self):
        """RouteNode.get_input_type works with renamed inputs."""

        @route(targets=["a", "b"], rename_inputs={"x": "value"})
        def gate(x: int) -> str:
            return "a"

        assert gate.get_input_type("value") is int
        assert gate.get_input_type("x") is None  # Original name no longer valid

    def test_route_with_strict_types_succeeds(self):
        """Route nodes work correctly with strict_types=True."""
        from dataclasses import dataclass

        from hypergraph import END, Graph, node, route

        @dataclass(frozen=True)
        class ReviewDecision:
            status: str

        @node(output_name="decision")
        def make_decision() -> ReviewDecision:
            return ReviewDecision(status="accept")

        @route(targets=["done", END])
        def gate(decision: ReviewDecision) -> str:
            return "done" if decision.status == "accept" else END

        @node(output_name="done")
        def done(decision: ReviewDecision) -> str:
            return "ok"

        # This should NOT raise GraphConfigError
        graph = Graph([make_decision, gate, done], strict_types=True)
        assert graph.strict_types is True
