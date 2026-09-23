"""Mermaid output never uses a reserved word as a class name or node id (#585).

``end`` closes a ``subgraph`` in Mermaid, so ``classDef end ...`` /
``class __end__ end`` made every diagram with an END node fail to parse in
Mermaid 11. The guard here parses the generated source statement by statement
(every line must be a statement this exporter emits) and checks every
identifier it finds: ``classDef`` names, ``class`` assignment targets and class
names, node definitions, ``subgraph`` ids and edge endpoints.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from hypergraph import END, Graph, node, route
from hypergraph.materialization import SqliteTableStore
from hypergraph.viz._mermaid_core import _sanitize_class_name
from hypergraph.viz.mermaid import DEFAULT_COLORS
from tests.test_frozen_baselines.graph_cases import (
    build_container_entrypoint,
    build_gated,
    build_nested,
)

# The issue's list, owned by the test rather than imported from the code under test.
MERMAID_RESERVED = frozenset(
    word.lower()
    for word in (
        "end",
        "subgraph",
        "graph",
        "flowchart",
        "style",
        "class",
        "classDef",
        "click",
        "linkStyle",
        "direction",
    )
)


def _is_reserved(identifier: str) -> bool:
    return identifier.lower() in MERMAID_RESERVED


# =============================================================================
# Structural parser for the exporter's statement vocabulary
# =============================================================================

_HEADER = re.compile(r"^flowchart (TD|TB|BT|LR|RL)$")
_SUBGRAPH = re.compile(r'^subgraph (?P<id>\S+) \[".*"\]$')
_NODE_DEF = re.compile(r'^(?P<id>[^\s\[\](){}|]+)(?:\[\["|\[/"|\["|\(\["|\(\("|\{\{")')
_EDGE = re.compile(r"^(?P<src>\S+) (?:-->|-\.->)(?:\|[^|]*\|)? (?P<tgt>\S+)$")
_CLASSDEF = re.compile(r"^classDef (?P<name>\S+) (?P<props>\S+)$")
_CLASS = re.compile(r"^class (?P<ids>\S+) (?P<name>\S+)$")
_LINKSTYLE = re.compile(r"^linkStyle [\d,]+ \S+$")


@dataclass
class ParsedMermaid:
    node_ids: list[str] = field(default_factory=list)
    subgraph_ids: list[str] = field(default_factory=list)
    edge_endpoints: list[str] = field(default_factory=list)
    class_defs: dict[str, str] = field(default_factory=dict)
    class_assignments: dict[str, list[str]] = field(default_factory=dict)
    subgraph_closers: int = 0

    def identifiers(self) -> Iterator[tuple[str, str]]:
        """Every identifier in the source, tagged with the role it plays."""
        for node_id in self.node_ids:
            yield "node id", node_id
        for subgraph_id in self.subgraph_ids:
            yield "subgraph id", subgraph_id
        for endpoint in self.edge_endpoints:
            yield "edge endpoint", endpoint
        for name in self.class_defs:
            yield "classDef name", name
        for name, ids in self.class_assignments.items():
            yield "class name", name
            for node_id in ids:
                yield "class target", node_id


def parse_mermaid(source: str) -> ParsedMermaid:
    """Classify every line; an unknown statement fails rather than being skipped."""
    lines = [line.strip() for line in source.splitlines()]
    assert _HEADER.match(lines[0]), f"not a flowchart header: {lines[0]!r}"
    parsed = ParsedMermaid()
    for line in lines[1:]:
        if not line or line.startswith("%%"):
            continue
        if line == "end":
            parsed.subgraph_closers += 1
        elif m := _SUBGRAPH.match(line):
            parsed.subgraph_ids.append(m["id"])
        elif m := _CLASSDEF.match(line):
            assert m["name"] not in parsed.class_defs, f"duplicate classDef {m['name']!r}"
            parsed.class_defs[m["name"]] = m["props"]
        elif m := _CLASS.match(line):
            assert m["name"] not in parsed.class_assignments, f"duplicate class statement for {m['name']!r}"
            parsed.class_assignments[m["name"]] = m["ids"].split(",")
        elif _LINKSTYLE.match(line):
            continue
        elif m := _EDGE.match(line):
            parsed.edge_endpoints.extend((m["src"], m["tgt"]))
        elif m := _NODE_DEF.match(line):
            parsed.node_ids.append(m["id"])
        else:
            pytest.fail(f"unparsed Mermaid statement: {line!r}")
    return parsed


def assert_no_reserved_identifiers(source: str) -> ParsedMermaid:
    parsed = parse_mermaid(source)
    offenders = [(role, ident) for role, ident in parsed.identifiers() if _is_reserved(ident)]
    assert offenders == [], f"reserved Mermaid words used as identifiers: {offenders}\n\n{source}"

    # The statements still hang together: a bare ``end`` only ever closes a
    # subgraph, every class statement names a declared class, and every styled
    # or connected id is a declared node or subgraph.
    assert parsed.subgraph_closers == len(parsed.subgraph_ids)
    assert set(parsed.class_assignments) <= set(parsed.class_defs)
    declared = set(parsed.node_ids) | set(parsed.subgraph_ids)
    styled = {node_id for ids in parsed.class_assignments.values() for node_id in ids}
    assert styled <= set(parsed.node_ids)
    assert set(parsed.edge_endpoints) <= declared
    return parsed


# =============================================================================
# Graphs covering every class the exporter can emit
# =============================================================================


@node(output_name="total")
def combine(a: int, b: int) -> int:
    return a + b


@node(output_name="reported")
def report(total: int) -> str:
    return str(total)


def build_input_group_with_start() -> Graph:
    """Two params with one consumer set (INPUT_GROUP) and an explicit entrypoint (START)."""
    return Graph([combine, report], name="grouped").with_entrypoint("combine")


# Nodes named after every reserved word, including a route gate ending the
# run and a container whose subgraph id would otherwise be ``subgraph``.
@node(output_name="a_out")
def flowchart(x: int) -> int:
    return x


@node(output_name="b_out")
def style(a_out: int) -> int:
    return a_out


@node(output_name="c_out")
def click(b_out: int) -> int:
    return b_out


@route(targets=["linkStyle", END])
def direction(c_out: int) -> str:
    return "linkStyle"


@node(output_name="d_out")
def linkStyle(c_out: int) -> int:
    return c_out


@node(output_name="e_out")
def class_body(d_out: int) -> int:
    return d_out


@node(output_name="f_out")
def end(e_out: int) -> int:
    return e_out


@node(output_name="g_out")
def classDef(f_out: int) -> int:
    return f_out


@node(output_name="h_out")
def graph(g_out: int) -> int:
    return g_out


@node(output_name="i_out")
def Subgraph(g_out: int) -> int:  # mixed case must be caught too
    return g_out


def build_reserved_names() -> Graph:
    # ``class`` is a Python keyword, so only a container (graph-name rules) can carry it.
    klass = Graph([class_body], name="class")
    inner = Graph([classDef, graph, Subgraph], name="subgraph")
    return Graph(
        [flowchart, style, click, direction, linkStyle, klass.as_node(), end, inner.as_node()],
        name="reserved_names",
    )


@node(output_name=("doc_id", "body"))
def stage(source: str) -> tuple[str, str]:
    return source, source


@node(output_name="words")
def count_words(body: str) -> int:
    return len(body.split())


@node(output_name="published")
def publish(materialization: object) -> str:
    return "done"


@pytest.fixture
def table_store() -> Iterator[SqliteTableStore]:
    store = SqliteTableStore()
    yield store
    store.close()


def build_mounted_table(store: SqliteTableStore) -> Graph:
    """A mounted table's receipt has no inner producer: expanded, it draws an OUTPUT anchor."""
    table = Graph([count_words], name="docs_recipe").as_table(identity="doc_id", store=store)
    mounted = table.as_node(name="materialize", output_name="materialization")
    return Graph([stage, mounted, publish], name="ingest")


# (builder, to_mermaid kwargs); builders taking the store get it from the fixture.
CASES: dict[str, tuple[Callable[..., Graph], dict]] = {
    "gated": (build_gated, {}),
    "gated_separate_outputs": (build_gated, {"separate_outputs": True}),
    "container_entrypoint_expanded": (build_container_entrypoint, {"depth": 1}),
    "nested_collapsed": (build_nested, {"depth": 0}),
    "nested_expanded": (build_nested, {"depth": 1}),
    "input_group_with_start": (build_input_group_with_start, {}),
    "reserved_names_collapsed": (build_reserved_names, {"depth": 0}),
    "reserved_names_expanded": (build_reserved_names, {"depth": 1}),
    "reserved_names_separate_outputs": (build_reserved_names, {"depth": 1, "separate_outputs": True}),
    "mounted_table_expanded": (build_mounted_table, {"depth": 2}),
}


def _render(case: str, store: SqliteTableStore) -> str:
    builder, kwargs = CASES[case]
    graph_ = builder(store) if builder is build_mounted_table else builder()
    return graph_.to_mermaid(**kwargs).source


@pytest.mark.parametrize("case", sorted(CASES))
def test_no_reserved_word_is_an_identifier(case: str, table_store: SqliteTableStore) -> None:
    assert_no_reserved_identifiers(_render(case, table_store))


def test_cases_cover_every_class_the_exporter_emits(table_store: SqliteTableStore) -> None:
    """The matrix above exercises all of ``DEFAULT_COLORS`` plus the shapes that share a class."""
    emitted_classes: set[str] = set()
    ids: set[str] = set()
    for case in CASES:
        parsed = parse_mermaid(_render(case, table_store))
        emitted_classes |= set(parsed.class_assignments)
        ids |= set(parsed.node_ids) | set(parsed.subgraph_ids)

    assert emitted_classes == {_sanitize_class_name(cls) for cls in DEFAULT_COLORS}
    assert any(i.startswith("input_group_") for i in ids)  # INPUT_GROUP shares the input class
    assert any("__output__" in i for i in ids)  # OUTPUT anchor
    assert any(i.startswith("data_") for i in ids)  # DATA pill
    assert {"n_subgraph", "n_end", "n_class", "n_style"} <= ids  # reserved node names were emitted, prefixed


# =============================================================================
# colors= keeps its public keys; emitted class names stay safe
# =============================================================================


def _end_node_class(parsed: ParsedMermaid) -> str:
    (name,) = [cls for cls, ids in parsed.class_assignments.items() if "__end__" in ids]
    return name


def test_end_color_override_still_styles_the_end_node() -> None:
    source = build_gated().to_mermaid(colors={"end": {"fill": "#000"}}).source
    parsed = assert_no_reserved_identifiers(source)

    props = dict(prop.split(":", 1) for prop in parsed.class_defs[_end_node_class(parsed)].split(","))
    assert props["fill"] == "#000"
    assert props["stroke"] == DEFAULT_COLORS["end"]["stroke"]  # unmentioned defaults survive the override


def test_reserved_color_keys_are_never_emitted_verbatim() -> None:
    colors = {word: {"fill": "#123456"} for word in ("style", "class", "classDef", "subgraph", "end", "End")}
    for kwargs in ({}, {"separate_outputs": True}):
        assert_no_reserved_identifiers(build_gated().to_mermaid(colors=colors, **kwargs).source)


# =============================================================================
# The class-name sanitizer
# =============================================================================


@pytest.mark.parametrize("word", sorted(MERMAID_RESERVED | {"classDef", "linkStyle", "End", "SUBGRAPH"}))
def test_sanitizer_never_returns_a_reserved_word(word: str) -> None:
    assert not _is_reserved(_sanitize_class_name(word))


def test_sanitizer_names_the_end_class_end_node() -> None:
    assert _sanitize_class_name("end") == "endNode"


@pytest.mark.parametrize("name", sorted(set(DEFAULT_COLORS) - {"end"}))
def test_sanitizer_keeps_non_reserved_names_byte_identical(name: str) -> None:
    assert _sanitize_class_name(name) == name
