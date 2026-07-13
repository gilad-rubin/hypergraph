"""Executable contract and deterministic reference for the inspect-mode example."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from hypergraph import InspectionDisplay, MapResult

# Generator-only internals: the public example exercised by this test never
# imports or exposes artifact or renderer implementation types.
from hypergraph.runners._shared._inspect import MapInspection, MapItemInspection, NodeInspection, RunInspection
from hypergraph.runners._shared._inspect_html import render_map_inspection
from hypergraph.runners._shared.results import FailureEvidence

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "inspect_mode.py"
REFERENCE = ROOT / "examples" / "inspect-mode-reference.html"
SOURCE_COMMAND = "uv run python tests/inspect/test_inspect_example.py --write-reference"
FAILURE_TEXT = "Customer maya-23 requires manual review"


def _load_example() -> ModuleType:
    assert EXAMPLE.exists(), f"Missing public example: {EXAMPLE}"
    spec = importlib.util.spec_from_file_location("hypergraph_inspect_mode_example", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalized_failure(failure: FailureEvidence) -> FailureEvidence:
    return replace(failure, duration_ms=0.0)


def _normalized_node(node: NodeInspection, *, item_index: int) -> NodeInspection:
    return replace(
        node,
        run_id=f"reference-item-{item_index}",
        span_id=f"reference-item-{item_index}-span-{node.sequence}",
        failure=_normalized_failure(node.failure) if node.failure is not None else None,
        started_at_ms=0.0 if node.started_at_ms is not None else None,
        ended_at_ms=0.0 if node.ended_at_ms is not None else None,
        duration_ms=0.0,
    )


def _normalized_run(run: RunInspection, *, item_index: int) -> RunInspection:
    return replace(
        run,
        run_id=f"reference-item-{item_index}",
        nodes=tuple(_normalized_node(node, item_index=item_index) for node in run.nodes),
        failures=tuple(_normalized_failure(failure) for failure in run.failures),
        total_duration_ms=0.0,
    )


def _normalized_item(item: MapItemInspection) -> MapItemInspection:
    assert item.run is not None
    return replace(item, run=_normalized_run(item.run, item_index=item.item_index))


def _reference_artifact() -> MapInspection:
    module = _load_example()
    batch = module.run_customer_review()
    assert isinstance(batch, MapResult)
    artifact: Any = batch._inspection
    assert isinstance(artifact, MapInspection)
    return replace(
        artifact,
        run_id="reference-batch",
        items=tuple(_normalized_item(item) for item in artifact.items),
        total_duration_ms=0.0,
    )


def _generate_reference_html() -> str:
    rendered = render_map_inspection(_reference_artifact())
    return f"""<!doctype html>
<!-- GENERATED FILE — DO NOT EDIT
Source: {SOURCE_COMMAND}
Normalization: volatile run/span IDs and timings only; statuses, original indexes, inputs, outputs, and exceptions come from a real run.
-->
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Hypergraph inspect mode reference</title><style data-generated-reference-style>body{{margin:16px}}[data-generated-reference]{{box-sizing:border-box;margin:0 0 12px;padding:12px;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc;color:#334155;font:13px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}}</style></head>
<body>
<aside data-generated-reference="true"><strong>GENERATED FILE — DO NOT EDIT</strong><br>Source: <code>{SOURCE_COMMAND}</code><br>Normalization: volatile run/span IDs and timings only. All statuses, original indexes, inputs, outputs, and exceptions below came from a real failing <code>SyncRunner.map(...)</code> execution.</aside>
{rendered}
</body>
</html>
"""


def test_public_example_runs_real_failure_and_uses_public_inspect_surface() -> None:
    module = _load_example()
    batch = module.run_customer_review()

    assert isinstance(batch, MapResult)
    assert batch.requested_count == 3
    assert len(batch) == 3
    assert [result.status.value for result in batch] == ["completed", "failed", "completed"]
    assert batch[0]["review_action"] == "approve"
    assert batch[2]["review_action"] == "approve"

    failed = next(result for result in batch.failures if result.failure is not None and result.failure.item_index == 1)
    assert failed.failure is not None
    assert failed.failure.inputs == {"customer_id": "maya-23", "lifetime_value": 1200}
    assert str(failed.failure.error) == FAILURE_TEXT
    assert isinstance(batch.inspect(), InspectionDisplay)

    async_batch = asyncio.run(module.run_customer_review_async())
    assert isinstance(async_batch, MapResult)
    assert [result.status.value for result in async_batch] == ["completed", "failed", "completed"]
    assert isinstance(async_batch.inspect(), InspectionDisplay)

    source = EXAMPLE.read_text()
    assert "batch.inspect()  # Keep this as the final expression." in source
    assert not source.rstrip().endswith("batch.inspect()")
    for private_surface in ("._repr_html_(", "._inspection", "._shared", ".artifact", ".to_html(", ".save("):
        assert private_surface not in source


def test_generated_reference_is_byte_stable_offline_and_truthful() -> None:
    first = _generate_reference_html()
    second = _generate_reference_html()

    assert first == second
    assert REFERENCE.read_text() == first
    assert first.count("GENERATED FILE — DO NOT EDIT") >= 2
    assert SOURCE_COMMAND in first
    assert "volatile run/span IDs and timings only" in first
    assert 'data-hypergraph-inspect="map"' in first
    assert '"status":"partial"' in first
    assert '"item_index":1' in first
    assert '"completed":2' in first
    assert '"failed":1' in first
    assert "maya-23" in first
    assert FAILURE_TEXT in first
    assert "reference-batch" in first
    assert "https://" not in first
    assert "http://" not in first
    assert re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", first, re.IGNORECASE) is None


if __name__ == "__main__":
    if sys.argv[1:] != ["--write-reference"]:
        raise SystemExit(f"Usage: {SOURCE_COMMAND}")
    REFERENCE.write_text(_generate_reference_html(), encoding="utf-8")
