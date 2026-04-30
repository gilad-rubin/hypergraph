"""Payload-size tripwires (PR #88, Stage 5).

These tests fence three regression classes:

1. **Vendor bloat**: someone adds a new bundled library and the per-cell
   HTML balloons. Per-cell ceiling catches this.
2. **2^N resurrection**: a precompute pass quietly comes back. The
   exponential-growth assertion on deeply-nested fixtures catches this.
3. **IR bloat**: the IR shape grows new fields that don't pull their
   weight. Per-expandable-container budget catches this.

When a ceiling fails, **don't reflexively raise it**. First check that
the change is intentional and bounded; then bump with a comment.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from tests.viz.conftest import (
    make_chain_graph,
    make_outer,
    make_simple_graph,
    make_workflow,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from benchmark_deep_nested import make_parametric_graph

# Vendor JS+CSS dominates per-cell payload (~720 KB on 2026-04-30).
# Headroom is ~75 KB for IR + viz JS growth before this trips. Bumping
# this means new vendor was added — review whether that vendor earns
# its bytes before raising the ceiling.
PER_CELL_HTML_CEILING_BYTES = 800 * 1024  # 800 KB

# IR shape is small and stable. ~5 KB per expandable container is the
# rough fixture profile today (see scripts/benchmark_deep_nested.py).
# A 50% headroom budget catches accidental field-of-fields growth.
IR_BYTES_PER_EXPANDABLE_BUDGET = 8 * 1024  # 8 KB

# Linearity-of-nesting tripwire: every additional expandable container
# should cost a small, bounded slice of HTML. If this slope exceeds the
# budget the most likely cause is a 2^N precompute coming back.
HTML_GROWTH_PER_EXPANDABLE_CEILING_BYTES = 4 * 1024  # 4 KB


def _generate_html(graph) -> str:
    flat = graph.to_flat_graph()
    ir = build_graph_ir(flat)
    data = {
        "nodes": [],
        "edges": [],
        "meta": {
            "ir": asdict(ir),
            "initial_expansion": {},
            "theme_preference": "auto",
            "show_types": True,
            "separate_outputs": False,
            "show_inputs": True,
            "show_bounded_inputs": False,
            "debug_overlays": False,
        },
    }
    return generate_widget_html(data)


@pytest.mark.parametrize(
    "fixture_name,factory",
    [
        ("simple", make_simple_graph),
        ("chain", make_chain_graph),
        ("workflow", make_workflow),
        ("outer", make_outer),
    ],
)
def test_per_cell_html_under_ceiling(fixture_name: str, factory) -> None:
    """No fixture should exceed the per-cell HTML ceiling."""
    html = _generate_html(factory())
    assert len(html) < PER_CELL_HTML_CEILING_BYTES, (
        f"{fixture_name}: {len(html):,} bytes exceeds {PER_CELL_HTML_CEILING_BYTES:,} byte ceiling. "
        "Did vendor JS get bigger, or did precompute resurrect?"
    )


@pytest.mark.parametrize("k", [3, 6, 10])
def test_deeply_nested_payload_is_linear_not_exponential(k: int) -> None:
    """The whole point of Stage 1 was deleting the 2^N precompute.

    We assert k=10 (10 expandable containers, would be 2^10 = 1024
    precomputed states) stays under the same per-cell ceiling as a
    flat fixture. If it doesn't, something is precomputing again."""
    graph = make_parametric_graph(k, width=2)
    html = _generate_html(graph)
    assert len(html) < PER_CELL_HTML_CEILING_BYTES, (
        f"k={k}: {len(html):,} bytes exceeds {PER_CELL_HTML_CEILING_BYTES:,} byte ceiling. Suspect: 2^N expansion-state precompute resurrected."
    )


def test_html_growth_per_expandable_is_bounded() -> None:
    """The slope of HTML(k) over k must be small and bounded."""
    sizes_by_k = {}
    for k in (3, 6, 10):
        sizes_by_k[k] = len(_generate_html(make_parametric_graph(k, width=2)))

    growth_per_extra_expandable = (sizes_by_k[10] - sizes_by_k[3]) / (10 - 3)
    assert growth_per_extra_expandable < HTML_GROWTH_PER_EXPANDABLE_CEILING_BYTES, (
        f"HTML grows {growth_per_extra_expandable:.0f} bytes per added expandable container "
        f"(measured between k=3 and k=10). Ceiling is {HTML_GROWTH_PER_EXPANDABLE_CEILING_BYTES:,}. "
        "Suspect: a precompute scaling super-linearly with nesting depth."
    )


@pytest.mark.parametrize("k", [3, 6, 10])
def test_ir_size_is_small_and_linear_in_expandables(k: int) -> None:
    """Independent check on the IR itself, ignoring vendor JS noise.

    The IR is what frontends consume; if it bloats, every consumer
    pays. Treat per-expandable-container budget as the contract."""
    graph = make_parametric_graph(k, width=2)
    flat = graph.to_flat_graph()
    ir = build_graph_ir(flat)
    ir_size = len(json.dumps(asdict(ir)))
    expandable_count = max(len(ir.expandable_nodes), 1)
    bytes_per_expandable = ir_size / expandable_count
    assert bytes_per_expandable < IR_BYTES_PER_EXPANDABLE_BUDGET, (
        f"k={k}: IR is {ir_size:,} bytes for {expandable_count} expandables "
        f"= {bytes_per_expandable:.0f} B/expandable, exceeds budget "
        f"{IR_BYTES_PER_EXPANDABLE_BUDGET:,}. Suspect: a new IR field that scales with state."
    )
