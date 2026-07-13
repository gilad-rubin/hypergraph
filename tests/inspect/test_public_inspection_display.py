"""Public typing and import-order contract for inspect displays."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from hypergraph import Graph, MapResult, RunResult, SyncRunner, node

ROOT = Path(__file__).resolve().parents[2]


def test_inspection_display_is_public_and_resolves_result_annotations() -> None:
    from hypergraph import InspectionDisplay
    from hypergraph.runners import InspectionDisplay as RunnerInspectionDisplay
    from hypergraph.runners._shared._inspect_html import InspectionDisplay as LegacyInspectionDisplay

    assert InspectionDisplay is RunnerInspectionDisplay
    assert InspectionDisplay is LegacyInspectionDisplay

    for result_type in (RunResult, MapResult):
        return_type = get_type_hints(result_type.inspect)["return"]
        assert get_origin(return_type) is InspectionDisplay
        assert get_args(return_type) == (Any,)


def test_real_run_and_map_inspection_return_the_public_display_type() -> None:
    from hypergraph import InspectionDisplay

    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    runner = SyncRunner()
    graph = Graph([double], name="public-inspection-display")

    run = runner.run(graph, {"value": 3}, inspect=True)
    batch = runner.map(
        graph,
        {"value": [3, 5]},
        map_over="value",
        inspect=True,
    )

    assert isinstance(run.inspect(), InspectionDisplay)
    assert isinstance(batch.inspect(), InspectionDisplay)


def test_inspection_display_import_orders_are_cycle_free_in_clean_interpreters() -> None:
    scripts = (
        """
from typing import get_type_hints
from hypergraph.runners._shared import _inspect_html
import hypergraph
assert _inspect_html.InspectionDisplay is hypergraph.InspectionDisplay
assert get_type_hints(hypergraph.RunResult.inspect)["return"].__origin__ is hypergraph.InspectionDisplay
assert get_type_hints(hypergraph.MapResult.inspect)["return"].__origin__ is hypergraph.InspectionDisplay
""",
        """
from typing import get_type_hints
import hypergraph
from hypergraph.runners._shared import _inspect_html
assert _inspect_html.InspectionDisplay is hypergraph.InspectionDisplay
assert get_type_hints(hypergraph.RunResult.inspect)["return"].__origin__ is hypergraph.InspectionDisplay
assert get_type_hints(hypergraph.MapResult.inspect)["return"].__origin__ is hypergraph.InspectionDisplay
""",
    )

    for script in scripts:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
