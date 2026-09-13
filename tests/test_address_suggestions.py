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

    def test_complete_miss_lists_valid_names_without_guessing(self):
        with pytest.warns(UserWarning), pytest.raises(MissingInputError) as exc:
            SyncRunner().run(_nested_outer(), {"quux": 1})

        message = str(exc.value)
        assert "Did you mean" not in message
        assert "Unrecognized inputs:\n  - 'quux'" in message
        assert "(address as 'embedder.indexer.docs')" in message
        assert "(address as 'embedder.indexer.overwrite')" in message


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

    def test_gate_target_typo_suggests_the_node_the_docs_promise(self):
        """README and docs/03-patterns/02-routing.md both advertise a
        ``Did you mean '<node>'?`` line here; before #90 none was emitted."""

        @route(targets=["retyr", END])
        def decide(x: int) -> str:
            return END

        @node(output_name="retried")
        def retry(x: int) -> int:
            return x + 1

        with pytest.raises(GraphConfigError) as exc:
            Graph([decide, retry])
        assert "Did you mean 'retry'?" in str(exc.value)
