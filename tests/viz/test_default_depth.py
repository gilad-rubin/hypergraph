"""Every visualize entry point starts COLLAPSED; expansion is a click away.

The interactive widget re-derives the scene client-side, so an initial
``depth=0`` costs nothing to expand later — but an initial ``depth=1`` costs
every reader a wall of internals before they have seen the shape of the
graph. One entry point (``HyperTable.visualize`` with mapped children) shipped
with its own ``depth=1`` default, so mounted tables opened noisy while plain
graphs opened calm.
"""

from __future__ import annotations

import json
import re
from typing import TypedDict

from hypergraph import Graph, SyncRunner, node


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


class Page(TypedDict):
    page_id: str
    text: str


@node(output_name="pages")
def segment(clean_text: str) -> list[Page]:
    return [Page(page_id="p0", text=clean_text)]


@node(output_name="enriched")
def enrich(text: str) -> str:
    return text.upper()


def _initial_expansion(html_path) -> dict[str, bool]:
    html = html_path.read_text()
    match = re.search(r'"initial_expansion"\s*:\s*(\{[^}]*\})', html)
    assert match, "widget payload must carry initial_expansion"
    return json.loads(match.group(1))


def test_graph_visualize_defaults_collapsed(tmp_path):
    inner = Graph([clean], name="inner")
    outer = Graph([inner.as_node(name="inner"), count_words], name="outer")

    outer.visualize(filepath=str(tmp_path / "graph.html"))

    assert _initial_expansion(tmp_path / "graph.html") == {"inner": False}


def test_hypertable_visualize_defaults_collapsed(tmp_path):
    """The regression: mapped children opened pre-expanded (depth=1)."""
    from tests.test_materialization_node_viz import MemoryStore

    table = Graph(
        [clean, segment, Graph([enrich], name="page").as_node(name="process_pages").map_over("pages", identity="page_id", schema=Page)],
        name="recipe",
    ).as_table(identity="doc_id", store=MemoryStore(), runner=SyncRunner())

    table.visualize(filepath=str(tmp_path / "table.html"))

    expansion = _initial_expansion(tmp_path / "table.html")
    assert expansion and all(value is False for value in expansion.values()), (
        f"HyperTable.visualize must start collapsed like every other entry point, got {expansion}"
    )


def test_hypertable_visualize_depth_still_expands(tmp_path):
    """``depth=`` keeps working; only the default moved."""
    from tests.test_materialization_node_viz import MemoryStore

    table = Graph(
        [clean, segment, Graph([enrich], name="page").as_node(name="process_pages").map_over("pages", identity="page_id", schema=Page)],
        name="recipe",
    ).as_table(identity="doc_id", store=MemoryStore(), runner=SyncRunner())

    table.visualize(depth=1, filepath=str(tmp_path / "table.html"))

    expansion = _initial_expansion(tmp_path / "table.html")
    assert any(expansion.values()), f"depth=1 must still expand the first level, got {expansion}"
