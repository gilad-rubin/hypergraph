"""Contract for the path-aware address suggestion engine (issue #90).

Three independent copies of ``difflib.get_close_matches`` used to answer "did
you mean?" for input addresses, and none of them understood that an address is
a *path*. The worst case was the most common nested mistake — passing the leaf
parameter name you see in your own function:

    graph inputs: 'embedder.indexer.docs', 'embedder.indexer.overwrite'
    you passed:   'docs', 'overwrite'

    Did you mean:
      - 'embedder.indexer.docs' -> 'embedder.indexer.overwrite'?      # wrong
      - 'embedder.indexer.overwrite' -> 'embedder.indexer.docs'?      # wrong

Both suggestions crossed the two inputs and the real answer ("prefix the leaf
with 'embedder.indexer.'") was never offered. ``suggest_addresses`` now ranks
by path structure, so the leaf resolves to its own canonical address.
"""

from __future__ import annotations

import pytest

from hypergraph import END, Graph, SyncRunner, node, route
from hypergraph.exceptions import MissingInputError
from hypergraph.graph.addressing import format_did_you_mean, suggest_addresses
from hypergraph.graph.validation import GraphConfigError

CANONICAL = ("embedder.indexer.docs", "embedder.indexer.overwrite")


def _nested_outer() -> Graph:
    """A two-level nest whose only inputs are ``embedder.indexer.{docs,overwrite}``."""

    @node(output_name="index")
    def build_index(docs: list[str], overwrite: bool) -> str:
        return f"{len(docs)}:{overwrite}"

    indexer = Graph([build_index], name="indexer")
    embedder = Graph([indexer.as_node(namespaced=True)], name="embedder")
    return Graph([embedder.as_node(namespaced=True)], name="outer")


class TestLeafOnlyRegression:
    """The reproduction from the review, asserted as external behavior."""

    def test_graph_inputs_are_the_canonical_nested_addresses(self):
        assert tuple(sorted(_nested_outer().inputs.all)) == CANONICAL

    def test_leaf_names_suggest_their_own_canonical_address(self):
        """RED before the fix: this produced the two crossed suggestions above,
        pairing 'docs' with '...overwrite' and never offering the prefix."""
        with pytest.warns(UserWarning), pytest.raises(MissingInputError) as exc:
            SyncRunner().run(_nested_outer(), {"docs": ["a"], "overwrite": True})

        message = str(exc.value)
        assert "  - 'docs': Did you mean 'embedder.indexer.docs'?" in message
        assert "  - 'overwrite': Did you mean 'embedder.indexer.overwrite'?" in message
        assert "'embedder.indexer.docs' -> 'embedder.indexer.overwrite'?" not in message
        assert "'embedder.indexer.overwrite' -> 'embedder.indexer.docs'?" not in message

    def test_one_segment_typo_suggests_the_corrected_address(self):
        with pytest.warns(UserWarning), pytest.raises(MissingInputError) as exc:
            SyncRunner().run(
                _nested_outer(),
                {"embedder.indexer.docs": ["a"], "embeder.indexer.overwrite": True},
            )
        assert "  - 'embeder.indexer.overwrite': Did you mean 'embedder.indexer.overwrite'?" in str(exc.value)

    def test_providing_nothing_at_all_offers_no_guess(self):
        """The old matcher's "discoverability" fallback -- suggest some valid
        input nobody typed -- was the #90 bug. Nothing typed, nothing guessed."""
        with pytest.raises(MissingInputError) as exc:
            SyncRunner().run(_nested_outer())

        message = str(exc.value)
        assert "Did you mean" not in message
        assert "Unrecognized inputs" not in message
        assert "(address as 'embedder.indexer.docs')" in message

    def test_complete_miss_lists_valid_names_without_guessing(self):
        with pytest.warns(UserWarning), pytest.raises(MissingInputError) as exc:
            SyncRunner().run(_nested_outer(), {"quux": 1})

        message = str(exc.value)
        assert "Did you mean" not in message
        assert "Unrecognized inputs:\n  - 'quux'" in message
        assert "(address as 'embedder.indexer.docs')" in message
        assert "(address as 'embedder.indexer.overwrite')" in message


class TestOptionalInputsAreCandidatesToo:
    """Review of #90: drawing candidates from the missing *required* set only
    answers a typo'd optional name with a different parameter, and taking that
    advice completes the run while silently dropping the value."""

    @staticmethod
    def _optional_graph() -> Graph:
        @node(output_name="total")
        def add(alpha: int, alpha_two: int = 7) -> int:
            return alpha + alpha_two

        return Graph([Graph([add], name="b").as_node(namespaced=True)], name="a")

    def test_a_typod_optional_input_is_offered_its_own_address(self):
        graph = self._optional_graph()
        assert sorted(graph.inputs.all) == ["b.alpha", "b.alpha_two"]
        assert sorted(graph.inputs.required) == ["b.alpha"]

        with pytest.warns(UserWarning), pytest.raises(MissingInputError) as exc:
            SyncRunner().run(graph, {"a.b.alpha_tw": 3})

        message = str(exc.value)
        assert "  - 'a.b.alpha_tw': Did you mean 'b.alpha_two'?" in message
        assert "'b.alpha'?" not in message

    def test_the_warning_that_precedes_a_silent_drop_carries_the_suggestion(self):
        """No required input is missing here, so nothing raises: the run
        COMPLETES and 'alpha_two' falls back to its default while the value the
        user passed is thrown away. The warning is the only signal there is."""
        with pytest.warns(UserWarning) as caught:
            result = SyncRunner().run(self._optional_graph(), {"b.alpha": 1, "alpha_two": 99})

        assert result.values == {"b.total": 8}  # not 100 -- the value was dropped
        assert "'alpha_two': Did you mean 'b.alpha_two'?" in str(caught[0].message)

    def test_taking_the_suggestion_makes_the_value_count(self):
        result = SyncRunner().run(self._optional_graph(), {"b.alpha": 1, "b.alpha_two": 99})
        assert result.values == {"b.total": 100}


class TestRanking:
    """The tiers the engine ranks by, each as a direct engine call."""

    def test_exact_leaf_suffix_beats_string_distance(self):
        assert suggest_addresses("docs", CANONICAL) == ("embedder.indexer.docs",)

    def test_exact_multi_segment_suffix_fills_the_missing_prefix(self):
        assert suggest_addresses("indexer.docs", CANONICAL) == ("embedder.indexer.docs",)

    def test_one_segment_typo_at_equal_depth(self):
        assert suggest_addresses("embeder.indexer.overwrite", CANONICAL) == ("embedder.indexer.overwrite",)

    def test_typo_in_a_leaf_that_is_also_missing_its_prefix(self):
        assert suggest_addresses("dcos", CANONICAL) == ("embedder.indexer.docs",)

    def test_missing_middle_segment_falls_back_to_whole_path_distance(self):
        assert suggest_addresses("embedder.docs", CANONICAL) == ("embedder.indexer.docs",)

    def test_ambiguous_leaf_lists_every_boundary_deterministically(self):
        candidates = ["retrieval.query", "generation.query", "answer"]
        assert suggest_addresses("query", candidates) == ("generation.query", "retrieval.query")

    def test_complete_miss_suggests_nothing(self):
        assert suggest_addresses("quux", CANONICAL) == ()

    def test_a_suggestion_is_always_an_addressable_candidate(self):
        for name in ("docs", "indexer.docs", "dcos", "embeder.indexer.docs", "embedder.docs"):
            assert set(suggest_addresses(name, CANONICAL)) <= set(CANONICAL), name

    def test_the_name_itself_is_never_suggested_back(self):
        assert suggest_addresses("docs", ["docs", "embedder.indexer.docs"]) == ("embedder.indexer.docs",)

    def test_flat_typo_still_works_for_flat_graphs(self):
        assert suggest_addresses("treshold", ["threshold", "documents"]) == ("threshold",)


class TestFormatter:
    """One formatter renders every 'did you mean' clause in the codebase."""

    def test_no_match_renders_nothing(self):
        assert format_did_you_mean("quux", CANONICAL) == ""

    def test_single_match(self):
        assert format_did_you_mean("docs", CANONICAL) == "Did you mean 'embedder.indexer.docs'?"

    def test_two_matches_read_as_a_choice(self):
        assert format_did_you_mean("query", ["a.query", "b.query"]) == "Did you mean 'a.query' or 'b.query'?"

    def test_three_or_more_matches_are_comma_separated(self):
        clause = format_did_you_mean("query", ["a.query", "b.query", "c.query"])
        assert clause == "Did you mean 'a.query', 'b.query', or 'c.query'?"


class TestEveryNameTakingSurfaceUsesTheEngine:
    """bind(), select(), wait_for, gate targets and the runner share one formatter."""

    def test_bind_suggests_the_canonical_address_for_a_leaf_name(self):
        with pytest.raises(ValueError) as exc:
            _nested_outer().bind(docs=["a"])
        assert "Did you mean 'embedder.indexer.docs'?" in str(exc.value)

    def test_bind_on_a_complete_miss_still_lists_valid_inputs(self):
        with pytest.raises(ValueError) as exc:
            _nested_outer().bind(quux=1)
        message = str(exc.value)
        assert "Did you mean" not in message
        assert "Valid inputs: ['embedder.indexer.docs', 'embedder.indexer.overwrite']" in message

    def test_select_suggests_the_canonical_output_address(self):
        with pytest.raises(ValueError) as exc:
            _nested_outer().select("index")
        assert "Did you mean 'embedder.indexer.index'?" in str(exc.value)

    def test_select_keys_each_suggestion_by_the_name_that_was_rejected(self):
        """select() reports every invalid name at once, so an unkeyed clause
        would read as the answer for all of them."""
        with pytest.raises(ValueError) as exc:
            _nested_outer().select("index", "indexer.indx", "quux")

        message = str(exc.value)
        assert "  - 'index': Did you mean 'embedder.indexer.index'?" in message
        assert "  - 'indexer.indx': Did you mean 'embedder.indexer.index'?" in message
        assert "'quux':" not in message  # no close output -- no invented clause

    def test_select_on_a_complete_miss_still_lists_valid_outputs(self):
        with pytest.raises(ValueError) as exc:
            _nested_outer().select("quux")
        assert "Did you mean" not in str(exc.value)
        assert "not graph outputs" in str(exc.value)

    def test_wait_for_typo_uses_the_shared_clause(self):
        @node(output_name="done_signal")
        def producer() -> str:
            return "done"

        @node(output_name="result", wait_for=["done_signl"])
        def consumer() -> str:
            return "ok"

        with pytest.raises(GraphConfigError) as exc:
            Graph([producer, consumer])
        assert "Did you mean 'done_signal'?" in str(exc.value)

    def test_gate_target_typo_suggests_the_node_at_build_time(self):
        """A gate's *declared* target is checked when the graph is built."""

        @route(targets=["retyr", END])
        def decide(x: int) -> str:
            return END

        @node(output_name="retried")
        def retry(x: int) -> int:
            return x + 1

        with pytest.raises(GraphConfigError) as exc:
            Graph([decide, retry])
        assert "Did you mean 'retry'?" in str(exc.value)

    def test_gate_target_typo_suggests_the_node_with_two_declared_targets(self):
        """The twin of the test above, in the shape users actually write.

        A single declared target plus ``END`` used to be the only shape that
        reached this message: with two or more non-END targets the mutex
        expansion inside ``_build_graph`` walked the missing name first and
        raised a raw ``networkx.exception.NetworkXError`` (issue #450). Both
        the with-END and the no-END spellings are pinned here.
        """

        @node(output_name="a")
        def step_a(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def step_b(x: int) -> int:
            return x + 2

        @route(targets=["step_a", "step_c", END])
        def decide(x: int) -> str:
            return "step_a"

        @route(targets=["step_a", "step_c"])
        def decide_no_end(x: int) -> str:
            return "step_a"

        for gate, gate_name in ((decide, "decide"), (decide_no_end, "decide_no_end")):
            with pytest.raises(GraphConfigError) as exc:
                Graph([gate, step_a, step_b])
            message = str(exc.value)
            assert f"Gate '{gate_name}' targets unknown node 'step_c'" in message
            assert f"  -> Available nodes: ['{gate_name}', 'step_a', 'step_b']" in message
            assert "Did you mean 'step_a' or 'step_b'?" in message

    def test_returned_target_typo_suggests_a_declared_target_at_runtime(self):
        """What a routing function *returns* is only checkable when it runs.
        README, docs/01-introduction/what-is-hypergraph.md,
        docs/03-patterns/02-routing.md and docs/06-api-reference/gates.md all
        quote this message; before the #90 repair it carried no suggestion and
        rendered its target list as ``["'step_a'", 'END']``."""

        @node(output_name="a")
        def step_a(x: int) -> str:
            return "a"

        @node(output_name="b")
        def step_b(x: int) -> str:
            return "b"

        @route(targets=["step_a", "step_b", END])
        def decide(x: int) -> str:
            return "step_c"

        result = SyncRunner().run(Graph([decide, step_a, step_b]), {"x": 5}, error_handling="continue")
        message = str(result.error)
        assert "  -> Valid targets: ['END', 'step_a', 'step_b']" in message
        assert "Did you mean 'step_a' or 'step_b'?" in message
