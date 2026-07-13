"""Behavioral contract for the semantic inspect renderer."""

from __future__ import annotations

import json
import re
from importlib.resources import files

from hypergraph.runners._shared._inspect import (
    MapInspection,
    MapItemInspection,
    NodeInspection,
    RunInspection,
)
from hypergraph.runners._shared._inspect_html import (
    render_map_inspection,
    render_run_inspection,
)


def _payload_from_html(rendered: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/json" data-hg-inspect-payload>(.*?)</script>',
        rendered,
        flags=re.DOTALL,
    )
    assert match is not None, "renderer must embed one semantic payload"
    payload = json.loads(match.group(1))
    assert isinstance(payload, dict)
    return payload


def test_run_renderer_embeds_the_versioned_artifact_wire_and_opens_on_timeline() -> None:
    node = NodeInspection(
        run_id="child-run-7",
        span_id="span-lookup-3",
        node_name="lookup",
        qualified_name="research/lookup",
        graph_name="research",
        item_index=None,
        superstep=2,
        sequence=3,
        status="completed",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        outputs={"decision": "manual-review"},
        started_at_ms=1_250.0,
        ended_at_ms=1_500.0,
        duration_ms=250.0,
        cached=False,
    )
    artifact = RunInspection(
        run_id="run-customer-23",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
        status="completed",
        nodes=(node,),
        failures=(),
        total_duration_ms=500.0,
        captured=True,
        terminal=True,
    )

    rendered = render_run_inspection(artifact)
    payload = _payload_from_html(rendered)

    assert payload == {
        "schema": "hypergraph.inspect/v1",
        "kind": "run",
        "default_view": "timeline",
        "delivery": {"state": "saved", "label": "Saved snapshot"},
        "run": {
            "run_id": "run-customer-23",
            "graph_name": "customer_enrichment",
            "workflow_id": "workflow-customers",
            "item_index": None,
            "status": "completed",
            "total_duration_ms": 500.0,
            "captured": True,
            "terminal": True,
            "error": None,
            "nodes": [
                {
                    "run_id": "child-run-7",
                    "span_id": "span-lookup-3",
                    "node_name": "lookup",
                    "qualified_name": "research/lookup",
                    "graph_name": "research",
                    "item_index": None,
                    "superstep": 2,
                    "sequence": 3,
                    "status": "completed",
                    "values_captured": True,
                    "inputs": {
                        "kind": "mapping",
                        "type_name": "mappingproxy",
                        "original_size": 1,
                        "entries": [
                            {
                                "key": {
                                    "kind": "text",
                                    "type_name": "str",
                                    "text": "customer_id",
                                    "original_size": 11,
                                },
                                "value": {
                                    "kind": "text",
                                    "type_name": "str",
                                    "text": "maya-23",
                                    "original_size": 7,
                                },
                            }
                        ],
                    },
                    "outputs": {
                        "kind": "mapping",
                        "type_name": "mappingproxy",
                        "original_size": 1,
                        "entries": [
                            {
                                "key": {
                                    "kind": "text",
                                    "type_name": "str",
                                    "text": "decision",
                                    "original_size": 8,
                                },
                                "value": {
                                    "kind": "text",
                                    "type_name": "str",
                                    "text": "manual-review",
                                    "original_size": 13,
                                },
                            }
                        ],
                    },
                    "failure": None,
                    "started_at_ms": 1_250.0,
                    "ended_at_ms": 1_500.0,
                    "duration_ms": 250.0,
                    "cached": False,
                }
            ],
            "failures": [],
        },
    }


def test_map_renderer_preserves_original_indexes_and_opens_on_items() -> None:
    completed_run = RunInspection(
        run_id="run-item-1",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=1,
        status="completed",
        nodes=(),
        failures=(),
        total_duration_ms=10.0,
        captured=True,
        terminal=True,
    )
    failed_run = RunInspection(
        run_id="run-item-3",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=3,
        status="failed",
        nodes=(),
        failures=(),
        total_duration_ms=20.0,
        captured=True,
        terminal=True,
    )
    artifact = MapInspection(
        run_id="batch-customers",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        status="partial",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=4,
        items=(
            MapItemInspection(
                item_index=1,
                status="completed",
                requested_inputs={"customer_id": "ari-2"},
                run=completed_run,
            ),
            MapItemInspection(
                item_index=3,
                status="failed",
                requested_inputs={"customer_id": "maya-23"},
                run=failed_run,
            ),
        ),
        unstarted_item_indexes=(0, 2),
        total_duration_ms=30.0,
        captured=True,
        terminal=True,
    )

    payload = _payload_from_html(render_map_inspection(artifact))
    map_wire = payload["map"]
    assert isinstance(map_wire, dict)

    assert payload["schema"] == "hypergraph.inspect/v1"
    assert payload["kind"] == "map"
    assert payload["default_view"] == "items"
    assert map_wire["counts"] == {
        "requested": 4,
        "claimed": 2,
        "completed": 1,
        "failed": 1,
        "running": 0,
        "paused": 0,
        "stopped": 0,
        "restored": 0,
        "unstarted": 2,
        "pending": 0,
    }
    items = map_wire["items"]
    assert isinstance(items, list)
    assert [item["item_index"] for item in items] == [1, 3]
    assert items[1]["requested_inputs"]["entries"][0]["value"]["text"] == "maya-23"
    assert items[1]["run"]["run_id"] == "run-item-3"
    assert map_wire["unstarted_item_indexes"] == [0, 2]


def test_packaged_shell_uses_only_local_dom_assets() -> None:
    assets = files("hypergraph.runners._shared.assets")
    javascript = assets.joinpath("inspect.js").read_text(encoding="utf-8")
    stylesheet = assets.joinpath("inspect.css").read_text(encoding="utf-8")

    assert "__hypergraphInspect" in javascript
    assert "updatePayload" in javascript
    assert "replaceChildren" in javascript
    forbidden = {
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "postMessage",
    }
    assert [token for token in forbidden if token in javascript] == []
    assert "@import" not in stylesheet
    assert "url(" not in stylesheet
