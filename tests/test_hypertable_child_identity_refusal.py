"""A colliding child identity is refused, not silently merged (#499, #519).

A child row is keyed by ``(parent identity, child identity)``, so two mapped
items under one parent that produce the same child identity used to occupy ONE
child row: both child graphs ran and one item's derived values were silently
lost. The write is now refused with ``DuplicateChildIdentityError`` before any
child graph runs and before any child row of that parent is written, restamped
or retired — for every child table of the row, checked together:

- ``on_error="raise"``: the write raises and nothing for that row is written;
- ``on_error="store"``: the row is stored as a total-loss ERROR row whose error
  names the collision, and the other rows of the same call proceed.

Every site that turns a fan-out's item list into child rows refuses: the
whole-graph derive (fresh insert and sync), the column-scoped reconcile that
re-runs a boundary, the whole-graph repair of a boundary that also produces a
parent column, the resume of an answered interrupt, and ``rederive()``. The
Materialization Branch path is pinned in ``test_materialization_branches.py``
and the legacy-row path in ``test_hypertable_receipt_truth.py``.

Two fan-outs whose child tables would share one name (#519), and a fan-out
whose child table would take the root table's name, are refused at table
analysis with ``GraphConfigError``.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, ClassVar, TypedDict

import pytest

import hypergraph
import hypergraph.exceptions
from hypergraph import AsyncRunner, DuplicateChildIdentityError, Graph, GraphConfigError, SyncRunner, interrupt, node
from hypergraph.materialization import RowStatus, SqliteTableStore, WriteOutcome
from hypergraph.materialization._provenance import split_boundary_provenance
from hypergraph.materialization._recipe_journal import JOURNAL_TABLE

RUNNERS = [pytest.param(SyncRunner, id="sync"), pytest.param(AsyncRunner, id="async")]

executions = {"split": 0, "shout": 0, "label": 0}
clean_suffix = {"value": ""}


@pytest.fixture(autouse=True)
def _reset():
    for name in executions:
        executions[name] = 0
    clean_suffix["value"] = ""


@pytest.fixture
def store():
    s = SqliteTableStore()
    yield s
    s.close()


def _settle(result: Any) -> Any:
    """Run a write under either runner family: an AsyncRunner table returns a coroutine."""
    return asyncio.run(result) if inspect.isawaitable(result) else result


class Word(TypedDict):
    word_id: str
    text: str


class Tag(TypedDict):
    tag_id: str
    text: str


@node(output_name="words")
def split_by_content(text: str) -> list[Word]:
    """The child identity is the word itself, so a repeated word collides."""
    executions["split"] += 1
    return [Word(word_id=word, text=word) for word in text.split()]


@node(output_name=("words", "word_count"))
def split_by_content_counting(text: str) -> tuple[list[Word], str]:
    """The fan-out boundary ALSO produces the stored parent column ``word_count``."""
    executions["split"] += 1
    words = text.split()
    return [Word(word_id=word, text=word) for word in words], str(len(words))


@node(output_name="words")
def split_by_position(text: str) -> list[Word]:
    """The child identity is the position: unique per item."""
    executions["split"] += 1
    return [Word(word_id=f"w{index}", text=word) for index, word in enumerate(text.split())]


@node(output_name="words")
def split_without_identity(text: str) -> list[dict[str, str]]:
    """No item carries the child identity field."""
    executions["split"] += 1
    return [{"text": word} for word in text.split()]


@node(output_name="tags")
def tag_by_content(text: str) -> list[Tag]:
    return [Tag(tag_id=word, text=word) for word in text.split()]


@node(output_name="upper")
def shout(text: str) -> str:
    executions["shout"] += 1
    return text.upper()


@node(output_name="label")
def label(text: str) -> str:
    executions["label"] += 1
    return f"#{text}"


shout_word = Graph([shout], name="shout_word")
label_tag = Graph([label], name="label_tag")


def _table(store, *, on_error: str = "raise", runner: Any = SyncRunner, splitter: Any = split_by_content):
    return Graph(
        [splitter, shout_word.as_node(name="shout_words").map_over("words", identity="word_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=runner())


def _latest(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row[key] not in latest or row["_write_gen"] > latest[row[key]]["_write_gen"]:
            latest[row[key]] = row
    return latest


def _physical(store, table: str) -> list[tuple[Any, ...]]:
    """Every physical row, in a stable order, as the store holds it."""
    return sorted(tuple(sorted((key, repr(value)) for key, value in row.items())) for row in store.read_rows(table))


# ---------------------------------------------------------------------------
# The error
# ---------------------------------------------------------------------------


def test_the_error_is_a_public_value_error_with_its_parts():
    assert hypergraph.DuplicateChildIdentityError is hypergraph.exceptions.DuplicateChildIdentityError
    assert "DuplicateChildIdentityError" in hypergraph.__all__
    assert issubclass(DuplicateChildIdentityError, ValueError)

    error = DuplicateChildIdentityError(table="word", identity="word_id", value="alpha", parent="d1")

    assert (error.table, error.identity, error.value, error.parent) == ("word", "word_id", "alpha", "d1")
    message = str(error)
    assert message.startswith("Child table 'word' got two items with word_id='alpha' under parent 'd1'.")
    assert "No child graph ran and no child row of this parent was written." in message
    assert "How to fix: derive word_id from something unique per item" in message


# ---------------------------------------------------------------------------
# Fresh insert and sync: the whole-graph derive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("verb", ["insert", "sync"])
def test_a_colliding_write_raises_before_any_child_graph_runs(store, runner, verb):
    table = _table(store, runner=runner)

    with pytest.raises(DuplicateChildIdentityError) as caught:
        _settle(getattr(table, verb)([{"doc_id": "d1", "text": "alpha beta alpha"}]))

    error = caught.value
    assert (error.table, error.identity, error.value, error.parent) == ("word", "word_id", "alpha", "d1")
    assert isinstance(error, ValueError)
    assert executions == {"split": 1, "shout": 0, "label": 0}, "the boundary ran; no child graph did"
    assert store.read_rows("doc") == [], "nothing for the row was written"
    assert store.read_rows("word") == []


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("verb", ["insert", "sync"])
def test_under_store_the_colliding_row_is_an_error_row_and_its_siblings_proceed(store, runner, verb):
    table = _table(store, on_error="store", runner=runner)

    receipt = _settle(
        getattr(table, verb)(
            [
                {"doc_id": "d1", "text": "alpha beta alpha"},
                {"doc_id": "d2", "text": "gamma delta"},
            ]
        )
    )

    assert [(row.id, row.outcome, row.status) for row in receipt.receipts] == [
        ("d1", WriteOutcome.INSERTED, RowStatus.ERROR),
        ("d2", WriteOutcome.INSERTED, RowStatus.COMPLETE),
    ]
    assert receipt.receipts[0].error.startswith("DuplicateChildIdentityError: Child table 'word' got two items with word_id='alpha'")
    assert [(row.id, row.error) for row in table.errors()] == [("d1", receipt.receipts[0].error)]
    assert executions["shout"] == 2, "only d2's two words ran the child graph"
    assert {row["_parent_id"] for row in store.read_rows("word")} == {"d2"}, "no child row of d1 was written"
    assert table.get("d2")["doc_id"] == "d2"


def test_items_missing_the_identity_field_collide_on_the_empty_identity(store):
    table = _table(store, splitter=split_without_identity)

    with pytest.raises(DuplicateChildIdentityError) as caught:
        table.insert(doc_id="d1", text="alpha beta")

    assert caught.value.value == ""
    assert "got two items with no word_id under parent 'd1': the field is missing or empty on both" in str(caught.value)
    assert "How to fix: set word_id on every mapped item" in str(caught.value)
    assert executions["shout"] == 0
    assert store.read_rows("word") == []


@pytest.mark.parametrize("runner", RUNNERS)
def test_falsifier_distinct_identities_insert_and_settle_as_before(store, runner):
    """A repeated word under a POSITIONAL identity, and distinct words under a
    content identity, are both accepted; the next sync is a zero-work skip."""
    table = _table(store, runner=runner)
    receipt = _settle(table.insert([{"doc_id": "d1", "text": "alpha beta gamma"}]))

    assert [(row.outcome, row.status) for row in receipt.receipts] == [(WriteOutcome.INSERTED, RowStatus.COMPLETE)]
    parent = _latest(store.read_rows("doc"), "doc_id")["d1"]
    assert split_boundary_provenance(parent["_provenance_words"])[1] == 3
    assert sorted(row["word_id"] for row in table.child("word").rows()) == ["alpha", "beta", "gamma"]

    for name in executions:
        executions[name] = 0
    again = _settle(_table(store, runner=runner).sync([{"doc_id": "d1", "text": "alpha beta gamma"}]))

    assert [(row.outcome, row.status) for row in again.receipts] == [(WriteOutcome.SKIPPED, RowStatus.COMPLETE)]
    assert executions == {"split": 0, "shout": 0, "label": 0}

    positional = _table(SqliteTableStore(), runner=runner, splitter=split_by_position)
    receipt = _settle(positional.insert([{"doc_id": "d1", "text": "alpha beta alpha"}]))
    assert [(row.outcome, row.status) for row in receipt.receipts] == [(WriteOutcome.INSERTED, RowStatus.COMPLETE)]
    assert positional.child("word").count() == 3


# ---------------------------------------------------------------------------
# A stored row whose boundary re-runs: the column-scoped reconcile
# (DerivedChildren) and the whole-graph repair of a counting boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize(
    ("splitter", "boundary_runs"),
    [
        # The column-scoped reconcile re-runs the boundary once (DerivedChildren).
        pytest.param(split_by_content, 1, id="reconcile-boundary"),
        # The reconcile runs the counting node for ``word_count``, cannot scope
        # a boundary that produces a stored column, and repairs through the
        # whole graph, which runs it again.
        pytest.param(split_by_content_counting, 2, id="whole-graph-boundary"),
    ],
)
@pytest.mark.parametrize("on_error", ["raise", "store"])
def test_an_update_that_re_runs_the_boundary_into_a_collision_touches_no_child_row(store, runner, splitter, boundary_runs, on_error):
    table = _table(store, on_error=on_error, runner=runner, splitter=splitter)
    _settle(table.insert([{"doc_id": "d1", "text": "alpha beta gamma"}]))
    children_before = _physical(store, "word")
    parent_before = _physical(store, "doc")
    for name in executions:
        executions[name] = 0

    if on_error == "raise":
        with pytest.raises(DuplicateChildIdentityError, match="word_id='alpha' under parent 'd1'"):
            _settle(table.update("d1", text="alpha beta alpha"))
        assert _physical(store, "doc") == parent_before, "nothing for the row was written"
    else:
        receipt = _settle(table.update("d1", text="alpha beta alpha"))
        assert (receipt.outcome, receipt.status) == (WriteOutcome.UPDATED, RowStatus.ERROR)
        assert "DuplicateChildIdentityError" in receipt.error
        stored = _latest(store.read_rows("doc"), "doc_id")["d1"]
        assert (stored["_status"], stored["text"]) == ("error", "alpha beta alpha")

    assert executions == {"split": boundary_runs, "shout": 0, "label": 0}, "the boundary re-ran; no child graph did"
    assert _physical(store, "word") == children_before, "no child row was written, restamped or retired"


# ---------------------------------------------------------------------------
# Every child table of the row is checked together
# ---------------------------------------------------------------------------


def _two_child_table(store, *, on_error: str, tags_first: bool):
    words = shout_word.as_node(name="shout_words").map_over("words", identity="word_id")
    tags = label_tag.as_node(name="label_tags").map_over("tags", identity="tag_id")
    fan_outs = [tags, words] if tags_first else [words, tags]
    return Graph([split_by_position, tag_by_content, *fan_outs], name="doc").as_table(
        identity="doc_id", store=store, on_error=on_error, runner=SyncRunner()
    )


@pytest.mark.parametrize("tags_first", [False, True], ids=["collision-second", "collision-first"])
@pytest.mark.parametrize("on_error", ["raise", "store"])
def test_a_collision_in_one_child_table_leaves_every_child_table_untouched(store, tags_first, on_error):
    """``words`` is keyed by position (never collides), ``tags`` by content. A
    collision in ``tags`` — whichever order the child tables are analysed in —
    leaves the ``word`` table exactly as it was: no child graph ran for it and
    none of its rows was written, restamped or retired."""
    table = _two_child_table(store, on_error=on_error, tags_first=tags_first)

    if on_error == "raise":
        with pytest.raises(DuplicateChildIdentityError, match="Child table 'tag'"):
            table.insert(doc_id="fresh", text="alpha beta alpha")
    else:
        assert table.insert(doc_id="fresh", text="alpha beta alpha").status is RowStatus.ERROR
    assert executions["shout"] == executions["label"] == 0
    assert store.read_rows("word") == [] and store.read_rows("tag") == []

    table.insert(doc_id="d1", text="alpha beta gamma")
    words_before, tags_before = _physical(store, "word"), _physical(store, "tag")
    for name in executions:
        executions[name] = 0

    if on_error == "raise":
        with pytest.raises(DuplicateChildIdentityError, match="Child table 'tag'"):
            table.update("d1", text="alpha beta alpha")
    else:
        assert table.update("d1", text="alpha beta alpha").status is RowStatus.ERROR
    assert executions["shout"] == executions["label"] == 0
    assert _physical(store, "word") == words_before
    assert _physical(store, "tag") == tags_before


# ---------------------------------------------------------------------------
# The other item-list sites: an answered interrupt, and rederive()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WordsQuestion:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@interrupt(answer_name="answer")
def ask_for_words(text: str) -> WordsQuestion:
    return WordsQuestion(prompt=f"Which words for {text}?")


@node(output_name="words")
def split_answer(answer: str) -> list[Word]:
    executions["split"] += 1
    return [Word(word_id=word, text=word) for word in answer.split()]


@pytest.mark.parametrize("on_error", ["raise", "store"])
def test_an_answer_whose_resume_produces_a_collision_is_refused(store, on_error):
    table = Graph(
        [ask_for_words, split_answer, shout_word.as_node(name="shout_words").map_over("words", identity="word_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=AsyncRunner())
    assert _settle(table.insert(doc_id="d1", text="intro")).status is RowStatus.WAITING

    if on_error == "raise":
        with pytest.raises(DuplicateChildIdentityError, match="word_id='x' under parent 'd1'"):
            _settle(table.update("d1", answer="x y x"))
        assert [row.id for row in table.waiting()] == ["d1"], "the waiting row was not touched"
    else:
        receipt = _settle(table.update("d1", answer="x y x"))
        assert (receipt.outcome, receipt.status) == (WriteOutcome.UPDATED, RowStatus.ERROR)
        assert [row.id for row in table.errors()] == ["d1"]
    assert executions["shout"] == 0
    assert store.read_rows("word") == []


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text + clean_suffix["value"]


@node(output_name="words")
def split_clean(clean_text: str) -> list[Word]:
    executions["split"] += 1
    return [Word(word_id=word, text=word) for word in clean_text.split()]


@pytest.mark.parametrize("on_error", ["raise", "store"])
def test_a_rederive_that_re_runs_the_boundary_into_a_collision_is_refused(store, on_error):
    table = Graph(
        [clean, split_clean, shout_word.as_node(name="shout_words").map_over("words", identity="word_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=SyncRunner())
    table.insert(doc_id="d1", text="alpha beta")
    children_before = _physical(store, "word")
    for name in executions:
        executions[name] = 0
    clean_suffix["value"] = " alpha"

    if on_error == "raise":
        with pytest.raises(DuplicateChildIdentityError, match="word_id='alpha' under parent 'd1'"):
            table.rederive("clean_text")
    else:
        receipt = table.rederive("clean_text")
        assert [(row.outcome, row.status) for row in receipt.receipts] == [(WriteOutcome.UPDATED, RowStatus.ERROR)]
        assert "DuplicateChildIdentityError" in receipt.receipts[0].error
    assert executions == {"split": 1, "shout": 0, "label": 0}
    assert _physical(store, "word") == children_before


# ---------------------------------------------------------------------------
# #519: two fan-outs whose child tables would share one name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second_input", "second_identity"),
    [
        pytest.param("words", "word_id", id="one-input-same-identity"),
        pytest.param("tags", "word_id", id="two-inputs-same-identity"),
        pytest.param("tags", "word", id="two-identities-one-table-name"),
    ],
)
def test_two_fan_outs_sharing_one_child_table_are_refused_at_analysis(store, second_input, second_identity):
    table = Graph(
        [
            split_by_position,
            tag_by_content,
            shout_word.as_node(name="shout_words").map_over("words", identity="word_id"),
            label_tag.as_node(name="label_tags").map_over(second_input, identity=second_identity),
        ],
        name="doc",
    ).as_table(identity="doc_id", store=store, runner=SyncRunner())

    with pytest.raises(GraphConfigError) as caught:
        table.insert(doc_id="d1", text="alpha beta")

    message = str(caught.value)
    assert message.startswith("Two fan-outs would write one child table 'word'.")
    assert f"'shout_words' (identity 'word_id') and 'label_tags' (identity '{second_identity}')" in message
    assert "How to fix: give each child graph its own identity" in message
    assert executions["split"] == 0, "refused at analysis, before anything ran"


class Token(TypedDict):
    word_id: str
    token_id: str
    text: str


@node(output_name="tokens")
def split_tokens(text: str) -> list[Token]:
    executions["split"] += 1
    return [Token(word_id=f"w{index}", token_id=f"t{index}", text=word) for index, word in enumerate(text.split())]


def test_falsifier_distinct_identities_over_one_input_work_and_settle(store):
    """Two fan-outs over ONE input, each with its own identity, get two child
    tables; the second write is a zero-execution SKIPPED."""

    def build():
        return Graph(
            [
                split_tokens,
                shout_word.as_node(name="shout_words").map_over("tokens", identity="word_id"),
                label_tag.as_node(name="label_tokens").map_over("tokens", identity="token_id"),
            ],
            name="doc",
        ).as_table(identity="doc_id", store=store, runner=SyncRunner())

    build().insert(doc_id="d1", text="alpha beta")
    table = build()
    assert sorted(row["upper"] for row in table.child("word").rows()) == ["ALPHA", "BETA"]
    assert sorted(row["label"] for row in table.child("token").rows()) == ["#alpha", "#beta"]
    for name in executions:
        executions[name] = 0

    receipt = table.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [(row.outcome, row.status) for row in receipt.receipts] == [(WriteOutcome.SKIPPED, RowStatus.COMPLETE)]
    assert executions == {"split": 0, "shout": 0, "label": 0}


# ---------------------------------------------------------------------------
# #499: a child table named like the root table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table_name", "child_identity"),
    [
        pytest.param(None, "doc", id="derived-root-name"),
        pytest.param("documents", "documents_id", id="explicit-root-name"),
    ],
)
def test_a_child_table_named_like_the_root_table_is_refused_at_analysis(store, table_name, child_identity):
    """The root table is ``doc`` (from ``doc_id``) or the ``name=`` passed; a
    child identity that resolves to the same name used to write child rows
    into the ROOT table: ``rows()`` showed a phantom root row and
    ``child(...)`` raised ``KeyError: '_parent_id'``."""

    @node(output_name="parts")
    def split(text: str) -> list[dict[str, str]]:
        executions["split"] += 1
        return [{child_identity: f"p{index}", "text": word} for index, word in enumerate(text.split())]

    table = Graph(
        [split, shout_word.as_node(name="shout_parts").map_over("parts", identity=child_identity)],
        name="doc",
    ).as_table(identity="doc_id", store=store, runner=SyncRunner(), name=table_name)
    root = table_name or "doc"

    with pytest.raises(GraphConfigError) as caught:
        table.insert(doc_id="d1", text="alpha beta")

    message = str(caught.value)
    assert message.startswith(f"Fan-out 'shout_parts' would write its child rows into the root table {root!r}.")
    assert f"(identity {child_identity!r})" in message
    assert "How to fix: give the child graph a different identity" in message
    assert executions == {"split": 0, "shout": 0, "label": 0}, "refused at analysis, before anything ran"


# ---------------------------------------------------------------------------
# The refusal keys an identity the way the child table stores it: str(value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(None, "None", id="None-vs-str"),
        pytest.param(1, "1", id="int-vs-str"),
    ],
)
def test_identities_that_differ_only_in_type_collide_as_the_store_keys_them(store, first, second):
    """A child row is keyed by ``str`` of its identity, so ``None`` and
    ``"None"`` (or ``1`` and ``"1"``) name ONE child row. Keyed by the raw
    value they would look distinct: ``None``/``"None"`` then folded into one
    readable row, and ``1``/``"1"`` failed in the store instead of here."""

    @node(output_name="words")
    def split(text: str) -> list[dict[str, Any]]:
        executions["split"] += 1
        return [{"word_id": first, "text": "a"}, {"word_id": second, "text": "b"}]

    table = Graph(
        [split, shout_word.as_node(name="shout_words").map_over("words", identity="word_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, runner=SyncRunner())

    with pytest.raises(DuplicateChildIdentityError) as caught:
        table.insert(doc_id="d1", text="x")

    assert (caught.value.identity, caught.value.value, caught.value.parent) == ("word_id", str(second), "d1")
    assert executions["shout"] == 0
    assert store.read_rows("word") == []


# ---------------------------------------------------------------------------
# #499: the recipe journal's table name is reserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identity", "table_name"),
    [
        pytest.param(f"{JOURNAL_TABLE}_id", None, id="root-identity"),
        pytest.param("doc_id", JOURNAL_TABLE, id="root-name"),
    ],
)
def test_a_root_table_named_like_the_recipe_journal_is_refused_at_analysis(store, identity, table_name):
    """The store keeps its recipe journal in a table of its own; a root table
    resolving to that name used to read journal rows back as phantom rows."""
    table = Graph([shout], name="doc").as_table(identity=identity, store=store, runner=SyncRunner(), name=table_name)

    with pytest.raises(GraphConfigError) as caught:
        table.insert(**{identity: "d1", "text": "alpha"})

    message = str(caught.value)
    assert message.startswith(f"HyperTable table name {JOURNAL_TABLE!r} is reserved.")
    assert "How to fix: pass a different name= or identity" in message
    assert executions["shout"] == 0


def test_a_child_table_named_like_the_recipe_journal_is_refused_at_analysis(store):
    """A child identity resolving to the journal's table name used to write
    child rows into the journal: 3 rows for 2 items, one of them a journal row."""

    @node(output_name="parts")
    def split(text: str) -> list[dict[str, str]]:
        executions["split"] += 1
        return [{f"{JOURNAL_TABLE}_id": f"p{index}", "text": word} for index, word in enumerate(text.split())]

    table = Graph(
        [split, shout_word.as_node(name="shout_parts").map_over("parts", identity=f"{JOURNAL_TABLE}_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, runner=SyncRunner())

    with pytest.raises(GraphConfigError) as caught:
        table.insert(doc_id="d1", text="alpha beta")

    message = str(caught.value)
    assert message.startswith(f"Fan-out 'shout_parts' would write its child rows into the reserved table {JOURNAL_TABLE!r}.")
    assert f"(identity '{JOURNAL_TABLE}_id')" in message
    assert "How to fix: give the child graph a different identity" in message
    assert executions == {"split": 0, "shout": 0, "label": 0}
