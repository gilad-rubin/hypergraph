"""BoundaryProjection unit tests + GraphNode boundary regression coverage (issue #209).

Two layers:

1. ``BoundaryProjection`` in isolation — every translation direction exercised
   directly against synthetic construction state, no Graph required.
2. GraphNode-level regressions the consolidation must preserve: fan-in
   defaults, expose aliases, nested renames, mutex outputs, and the has/get
   default agreement property (red on pre-#209 master: ``get_default_for``
   succeeded for off-surface names where ``has_default_for`` said False).
"""

import functools

import pytest

from hypergraph import Graph, node
from hypergraph.nodes._boundary import BoundaryProjection
from hypergraph.nodes._rename import RenameEntry
from hypergraph.nodes.base import _EMIT_SENTINEL


def build_projection(
    *,
    node_name: str = "sub",
    namespaced: bool = False,
    exposed: dict[str, str] | None = None,
    local_inputs: tuple[str, ...] = (),
    local_outputs: tuple[str, ...] = (),
    local_data_outputs: tuple[str, ...] | None = None,
    rename_history: list[RenameEntry] | None = None,
) -> BoundaryProjection:
    return BoundaryProjection.build(
        node_name=node_name,
        namespaced=namespaced,
        exposed=exposed or {},
        local_inputs=local_inputs,
        local_outputs=local_outputs,
        local_data_outputs=local_data_outputs if local_data_outputs is not None else local_outputs,
        rename_history=rename_history or [],
    )


class TestFlatProjection:
    """Non-namespaced boundary: address == local == original."""

    def test_identity_surface(self):
        proj = build_projection(local_inputs=("x", "y"), local_outputs=("out",))
        assert proj.inputs == ("x", "y")
        assert proj.outputs == ("out",)
        assert proj.data_outputs == ("out",)
        assert proj.locals_for_input("x") == ("x",)
        assert proj.original_inputs_for_address("x") == ("x",)
        assert proj.original_output_for_address("out") == "out"
        assert proj.input_address_for_original("x") == "x"
        assert proj.output_address_for_original("out") == "out"

    def test_unknown_names_pass_through(self):
        proj = build_projection(local_inputs=("x",), local_outputs=("out",))
        assert proj.locals_for_input("ghost") == ("ghost",)
        assert proj.local_for_output("ghost") == "ghost"
        assert proj.original_input("ghost") == "ghost"
        assert proj.output_address_for_original("ghost") == "ghost"

    def test_translate_inputs_identity(self):
        proj = build_projection(local_inputs=("x", "y"), local_outputs=())
        assert proj.translate_inputs({"x": 1, "y": 2}) == {"x": 1, "y": 2}

    def test_name_maps_identity(self):
        proj = build_projection(local_inputs=("x",), local_outputs=("out",))
        assert proj.input_name_map == {"x": ("x",)}
        assert proj.output_name_map == {"out": "out"}


class TestNamespacedProjection:
    def test_prefixed_addresses(self):
        proj = build_projection(namespaced=True, local_inputs=("x",), local_outputs=("out",))
        assert proj.inputs == ("sub.x",)
        assert proj.outputs == ("sub.out",)
        assert proj.locals_for_input("sub.x") == ("x",)
        assert proj.local_for_output("sub.out") == "out"
        assert proj.input_address_for_original("x") == "sub.x"
        assert proj.output_address_for_original("out") == "sub.out"

    def test_exposed_alias_wins_over_prefix(self):
        proj = build_projection(
            namespaced=True,
            exposed={"x": "flat_x", "out": "result"},
            local_inputs=("x", "y"),
            local_outputs=("out",),
        )
        assert proj.inputs == ("flat_x", "sub.y")
        assert proj.outputs == ("result",)
        assert proj.locals_for_input("flat_x") == ("x",)
        assert proj.input_address_for_original("x") == "flat_x"
        assert proj.output_address_for_original("out") == "result"
        assert proj.input_name_map == {"flat_x": ("x",), "sub.y": ("y",)}
        assert proj.output_name_map == {"result": "out"}

    def test_translate_inputs_resolves_namespaced_addresses(self):
        proj = build_projection(namespaced=True, exposed={"x": "ex"}, local_inputs=("x", "y"), local_outputs=())
        assert proj.translate_inputs({"ex": 1, "sub.y": 2}) == {"x": 1, "y": 2}


class TestRenameTranslation:
    def test_single_input_rename(self):
        history = [RenameEntry("inputs", "x", "items", batch_id=1)]
        proj = build_projection(local_inputs=("items",), local_outputs=(), rename_history=history)
        assert proj.original_input("items") == "x"
        assert proj.original_inputs_for_address("items") == ("x",)
        assert proj.input_address_for_original("x") == "items"
        assert proj.translate_inputs({"items": [1]}) == {"x": [1]}
        assert proj.input_name_map == {"items": ("x",)}

    def test_parallel_swap_same_batch(self):
        history = [
            RenameEntry("inputs", "x", "y", batch_id=7),
            RenameEntry("inputs", "y", "x", batch_id=7),
        ]
        proj = build_projection(local_inputs=("y", "x"), local_outputs=(), rename_history=history)
        assert proj.original_input("y") == "x"
        assert proj.original_input("x") == "y"
        assert proj.translate_inputs({"y": "was_x", "x": "was_y"}) == {"x": "was_x", "y": "was_y"}

    def test_chained_renames_across_batches(self):
        history = [
            RenameEntry("outputs", "a", "b", batch_id=1),
            RenameEntry("outputs", "b", "c", batch_id=2),
        ]
        proj = build_projection(local_inputs=(), local_outputs=("c",), rename_history=history)
        assert proj.original_output("c") == "a"
        assert proj.original_output_for_address("c") == "a"
        assert proj.output_address_for_original("a") == "c"
        assert proj.output_name_map == {"c": "a"}

    def test_rename_under_namespace(self):
        history = [RenameEntry("inputs", "x", "items", batch_id=1)]
        proj = build_projection(namespaced=True, local_inputs=("items",), local_outputs=(), rename_history=history)
        assert proj.inputs == ("sub.items",)
        assert proj.original_inputs_for_address("sub.items") == ("x",)
        assert proj.input_address_for_original("x") == "sub.items"
        assert proj.input_name_map == {"sub.items": ("x",)}

    def test_name_rename_history_ignored_for_ports(self):
        history = [RenameEntry("name", "old_sub", "sub", batch_id=1)]
        proj = build_projection(local_inputs=("x",), local_outputs=(), rename_history=history)
        assert proj.original_input("x") == "x"
        assert proj.former_names == ("old_sub",)


class TestTranslateOutputs:
    def test_renamed_output_values(self):
        history = [RenameEntry("outputs", "tripled", "final", batch_id=1)]
        proj = build_projection(local_inputs=(), local_outputs=("doubled", "final"), rename_history=history)
        assert proj.translate_outputs({"doubled": 4, "tripled": 12}) == {"doubled": 4, "final": 12}

    def test_off_surface_keys_pass_through(self):
        proj = build_projection(local_inputs=(), local_outputs=("out",))
        assert proj.translate_outputs({"out": 1, "internal": 2}) == {"out": 1, "internal": 2}

    def test_emit_only_outputs_backfilled_with_sentinel(self):
        proj = build_projection(
            local_outputs=("data_out", "signal"),
            local_data_outputs=("data_out",),
        )
        mapped = proj.translate_outputs({"data_out": 5})
        assert mapped["data_out"] == 5
        assert mapped["signal"] is _EMIT_SENTINEL

    def test_emit_backfill_does_not_override_produced_value(self):
        proj = build_projection(local_outputs=("data_out", "signal"), local_data_outputs=("data_out",))
        mapped = proj.translate_outputs({"data_out": 5, "signal": "fired"})
        assert mapped["signal"] == "fired"

    def test_data_outputs_filter_and_projection(self):
        proj = build_projection(
            namespaced=True,
            local_outputs=("data_out", "signal"),
            local_data_outputs=("data_out",),
        )
        assert proj.outputs == ("sub.data_out", "sub.signal")
        assert proj.data_outputs == ("sub.data_out",)


class TestResumeKeys:
    def test_local_output_key_maps_whole_key(self):
        history = [RenameEntry("outputs", "decision", "verdict", batch_id=1)]
        proj = build_projection(local_inputs=(), local_outputs=("verdict",), rename_history=history)
        assert proj.resume_key_from_original("decision") == "verdict"

    def test_nested_key_maps_head_only(self):
        proj = build_projection(local_inputs=(), local_outputs=("out",))
        assert proj.resume_key_from_original("review.decision") == "review.decision"

    def test_nested_local_output_address_key(self):
        # A child namespaced GraphNode already produced "inner.decision" as a
        # local output name — the whole key is on the local surface.
        proj = build_projection(namespaced=True, local_inputs=(), local_outputs=("inner.decision",))
        assert proj.resume_key_from_original("inner.decision") == "sub.inner.decision"

    def test_namespaced_head_mapping(self):
        proj = build_projection(namespaced=True, local_inputs=(), local_outputs=("decision",))
        assert proj.resume_key_from_original("decision") == "sub.decision"


class TestStaleAddressReplacement:
    def test_renamed_local_input(self):
        history = [RenameEntry("inputs", "x", "items", batch_id=1)]
        proj = build_projection(namespaced=True, local_inputs=("items",), local_outputs=(), rename_history=history)
        assert proj.replacement_for_stale_input_address("sub.x") == "sub.items"

    def test_former_node_name_prefix(self):
        history = [RenameEntry("name", "sub", "worker", batch_id=1)]
        proj = build_projection(node_name="worker", namespaced=True, local_inputs=("x",), local_outputs=(), rename_history=history)
        assert proj.replacement_for_stale_input_address("sub.x") == "worker.x"

    def test_exposed_local(self):
        proj = build_projection(namespaced=True, exposed={"x": "flat_x"}, local_inputs=("x",), local_outputs=())
        assert proj.replacement_for_stale_input_address("sub.x") == "flat_x"

    def test_current_address_is_not_stale(self):
        proj = build_projection(namespaced=True, local_inputs=("x",), local_outputs=())
        assert proj.replacement_for_stale_input_address("sub.x") is None

    def test_unknown_prefix_and_unknown_local(self):
        proj = build_projection(namespaced=True, local_inputs=("x",), local_outputs=())
        assert proj.replacement_for_stale_input_address("other.x") is None
        assert proj.replacement_for_stale_input_address("sub.ghost") is None


class TestProjectionCollisions:
    def test_two_outputs_projecting_to_same_address(self):
        with pytest.raises(ValueError, match="projects multiple outputs to 'z'"):
            build_projection(
                namespaced=True,
                exposed={"a": "z", "b": "z"},
                local_outputs=("a", "b"),
            )

    def test_input_and_output_colliding_on_different_locals(self):
        with pytest.raises(ValueError, match="projects input\\(s\\) \\['a'\\] and output 'b'"):
            build_projection(
                namespaced=True,
                exposed={"a": "z", "b": "z"},
                local_inputs=("a",),
                local_outputs=("b",),
            )

    def test_cyclic_seed_port_shares_address_without_error(self):
        proj = build_projection(local_inputs=("state",), local_outputs=("state",))
        assert proj.inputs == ("state",)
        assert proj.outputs == ("state",)


# === GraphNode-level regression coverage ===


@node(output_name="doubled")
def double(x: int, factor: int = 2) -> int:
    return x * factor


@node(output_name="summed")
def sum_with_factor(doubled: int, factor: int = 2) -> int:
    return doubled + factor


def _get_succeeds(fn) -> bool:
    try:
        fn()
        return True
    except KeyError:
        return False


def _default_matrix_nodes():
    inner = Graph([double, sum_with_factor], name="inner")
    bound_inner = Graph([double, sum_with_factor], name="bound_inner").bind(factor=7)
    nested = Graph([inner.as_node(name="mid")], name="nest_outer")
    return {
        "flat": inner.as_node(name="flat"),
        "namespaced": inner.as_node(name="ns", namespaced=True),
        "exposed": inner.as_node(name="ex", namespaced=True).expose(x="flat_x", factor="shared_factor"),
        "renamed": inner.as_node(name="ren").rename_inputs(factor="mult").rename_outputs(summed="total"),
        "bound": bound_inner.as_node(name="bnd"),
        "bound_namespaced": bound_inner.as_node(name="bns", namespaced=True),
        "nested": nested.as_node(name="outer"),
    }


class TestDefaultLookupAgreement:
    """has_*/get_* cannot disagree for ANY address (issue #209 fan-in fix).

    Red on pre-#209 master: for off-surface addresses (local or original
    names hidden behind namespacing/renames) has_default_for returned False
    while get_default_for still resolved a value through the first local.
    """

    @pytest.mark.parametrize("case", sorted(_default_matrix_nodes()))
    def test_has_get_agreement_over_address_matrix(self, case):
        gn = _default_matrix_nodes()[case]
        probe_addresses = {
            *gn.inputs,
            *gn.local_inputs,
            "x",
            "factor",
            "doubled",
            "nonexistent",
            f"{gn.name}.factor",
        }
        for address in sorted(probe_addresses):
            has = gn.has_default_for(address)
            got = _get_succeeds(functools.partial(gn.get_default_for, address))
            assert has == got, f"{case}: has_default_for({address!r})={has} but get success={got}"

            has_sig = gn.has_signature_default_for(address)
            got_sig = _get_succeeds(functools.partial(gn.get_signature_default_for, address))
            assert has_sig == got_sig, f"{case}: has_signature_default_for({address!r})={has_sig} but get success={got_sig}"

    def test_fan_in_consumers_agree_with_value(self):
        """Two inner consumers of 'factor' (consistent defaults): has=True, get=value."""
        gn = _default_matrix_nodes()["flat"]
        assert gn.has_default_for("factor") is True
        assert gn.get_default_for("factor") == 2
        assert gn.has_signature_default_for("factor") is True
        assert gn.get_signature_default_for("factor") == 2

    def test_bound_fan_in_agrees_with_bound_value(self):
        gn = _default_matrix_nodes()["bound"]
        assert gn.has_default_for("factor") is True
        assert gn.get_default_for("factor") == 7
        assert gn.has_signature_default_for("factor") is False
        with pytest.raises(KeyError, match="bound, not a signature default"):
            gn.get_signature_default_for("factor")

    def test_exposed_alias_defaults_resolve_through_alias(self):
        gn = _default_matrix_nodes()["exposed"]
        assert gn.has_default_for("shared_factor") is True
        assert gn.get_default_for("shared_factor") == 2

    def test_off_surface_get_now_raises(self):
        """The concrete pre-#209 asymmetry: off-surface name resolved a value."""
        gn = _default_matrix_nodes()["namespaced"]
        assert gn.has_default_for("factor") is False
        with pytest.raises(KeyError):
            gn.get_default_for("factor")


def _fan_in_boundary(*, consumer_has_value: bool):
    """One fan-in address 'shared' feeding two inner consumers.

    Toggling ``consumer_has_value`` gives the first consumer (a nested graph)
    its value via an inner-graph bind — the only way one consumer can gain a
    value while the graph stays constructible, because build validation
    rejects mixed signature defaults (asserted separately below).
    """

    @node(output_name="left")
    def use_left(shared: int) -> int:
        return shared

    @node(output_name="right")
    def use_right(shared: int) -> int:
        return -shared

    left_inner = Graph([use_left], name="left_inner")
    if consumer_has_value:
        left_inner = left_inner.bind(shared=11)
    g1 = left_inner.as_node(name="left_box")
    g2 = Graph([use_right], name="right_inner").as_node(name="right_box")
    return Graph([g1, g2], name="mid").as_node(name="box")


def _signature_fan_in_boundary(*, consumers_have_default: bool):
    """Fan-in address 'shared' with two function-node consumers."""
    if consumers_have_default:

        @node(output_name="left")
        def use_left(shared: int = 11) -> int:
            return shared

        @node(output_name="right")
        def use_right(shared: int = 11) -> int:
            return -shared
    else:

        @node(output_name="left")
        def use_left(shared: int) -> int:
            return shared

        @node(output_name="right")
        def use_right(shared: int) -> int:
            return -shared

    return Graph([use_left, use_right], name="mid").as_node(name="box")


class TestFanInDefaultFalsifier:
    """C4 falsifier: one fan-in consumer gains its default and the public
    outcome flips from False/KeyError to True/value."""

    def test_consumer_without_value_fails_then_gaining_it_resolves(self):
        missing = _fan_in_boundary(consumer_has_value=False)
        assert missing.has_default_for("shared") is False
        with pytest.raises(KeyError, match="No default value for parameter 'shared'"):
            missing.get_default_for("shared")

        having = _fan_in_boundary(consumer_has_value=True)
        assert having.has_default_for("shared") is True
        assert having.get_default_for("shared") == 11

    def test_gained_value_reaches_execution(self):
        """The resolved default is a real public outcome, not a map stamp."""
        from hypergraph import SyncRunner

        having = _fan_in_boundary(consumer_has_value=True)
        result = SyncRunner().run(Graph([having], name="root"), {})
        assert result["left"] == 11
        assert result["right"] == -11

    def test_signature_agreement_repeats_the_flip(self):
        missing = _signature_fan_in_boundary(consumers_have_default=False)
        assert missing.has_signature_default_for("shared") is False
        with pytest.raises(KeyError, match="No signature default for parameter 'shared'"):
            missing.get_signature_default_for("shared")
        assert missing.has_default_for("shared") is False
        with pytest.raises(KeyError):
            missing.get_default_for("shared")

        having = _signature_fan_in_boundary(consumers_have_default=True)
        assert having.has_signature_default_for("shared") is True
        assert having.get_signature_default_for("shared") == 11
        assert having.has_default_for("shared") is True
        assert having.get_default_for("shared") == 11

    def test_bound_consumer_fails_loudly_as_signature_default(self):
        having = _fan_in_boundary(consumer_has_value=True)
        assert having.has_signature_default_for("shared") is False
        with pytest.raises(KeyError, match="bound, not a signature default"):
            having.get_signature_default_for("shared")

    def test_lone_signature_default_still_rejected_at_build(self):
        """Why the signature flip changes both consumers: build validation
        (unchanged by #209) rejects the mixed state with its exact diagnostic."""
        from hypergraph.graph.validation import GraphConfigError

        @node(output_name="left")
        def use_left(shared: int = 11) -> int:
            return shared

        @node(output_name="right")
        def use_right(shared: int) -> int:
            return -shared

        with pytest.raises(GraphConfigError, match="Inconsistent defaults for 'shared'"):
            Graph([use_left, use_right], name="mid")


class TestBoundaryRegressionScenarios:
    def test_nested_rename_chain_translates_both_boundaries(self):
        """GraphNode inside GraphNode, renames at both boundaries."""
        inner = Graph([double], name="inner")
        mid = inner.as_node(name="mid").rename_outputs(doubled="mid_out")

        @node(output_name="final")
        def consume(mid_out: int) -> int:
            return mid_out + 1

        outer = Graph([mid, consume], name="outer")
        outer_gn = outer.as_node(name="top").rename_outputs(final="top_final")

        assert outer_gn._projection.output_address_for_original("final") == "top_final"
        assert mid._projection.output_address_for_original("doubled") == "mid_out"
        assert outer_gn._projection.original_output_for_address("top_final") == "final"

        from hypergraph import SyncRunner

        result = SyncRunner().run(Graph([outer_gn], name="root"), {"x": 3})
        assert result["top_final"] == 7

    def test_mutex_outputs_translate_through_renamed_boundary(self):
        """Exclusive branches producing the same output stay addressable after rename."""
        from hypergraph import SyncRunner
        from hypergraph.nodes.gate import ifelse

        @node(output_name="result")
        def process(a: int) -> int:
            return a * 2

        @node(output_name="result")
        def skip(a: int) -> int:
            return a

        @ifelse(when_true="process", when_false="skip")
        def is_positive(a: int) -> bool:
            return a > 0

        inner = Graph([is_positive, process, skip], name="mutex_inner")
        gn = inner.as_node(name="mutex").rename_outputs(result="branch_result")
        assert "branch_result" in gn.outputs
        assert gn._projection.output_name_map["branch_result"] == "result"

        runner = SyncRunner()
        assert runner.run(Graph([gn], name="root_pos"), {"a": 3})["branch_result"] == 6
        assert runner.run(Graph([gn], name="root_neg"), {"a": -3})["branch_result"] == -3

    def test_nx_attrs_name_maps_come_from_projection(self):
        inner = Graph([double], name="inner")
        gn = inner.as_node(name="cx", namespaced=True).expose(x="ex").rename_inputs(factor="mult")
        attrs = gn.nx_attrs
        assert attrs["input_name_map"] == gn._projection.input_name_map
        assert attrs["output_name_map"] == gn._projection.output_name_map
        assert attrs["input_name_map"] == {"ex": ("x",), "cx.mult": ("factor",)}
        assert attrs["output_name_map"] == {"cx.doubled": "doubled"}
