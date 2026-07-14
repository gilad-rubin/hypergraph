"""Safe settled HTML presentation for typed inspection artifacts."""

from __future__ import annotations

import html
import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from hypergraph.runners._shared._inspect import MapInspection, NodeInspection, RunInspection
from hypergraph.runners._shared._inspect_serialization import (
    serialize_value,
    serialized_value_to_wire,
)
from hypergraph.runners._shared.results import FailureEvidence
from hypergraph.runners.inspection import InspectionDisplay as InspectionDisplay

_INSPECT_SCHEMA = "hypergraph.inspect/v1"
InspectionDeliveryState = Literal["live", "stale", "saved"]


def _serialized(value: object) -> dict[str, object]:
    return serialized_value_to_wire(serialize_value(value))


def _failure_wire(
    failure: FailureEvidence | None,
    *,
    failure_key: str | None,
) -> dict[str, object] | None:
    if failure is None:
        return None
    return {
        **({"failure_key": failure_key} if failure_key is not None else {}),
        "node_name": failure.node_name,
        "error": _serialized(failure.error),
        "inputs": _serialized(failure.inputs),
        "superstep": failure.superstep,
        "duration_ms": failure.duration_ms,
        "graph_name": failure.graph_name,
        "workflow_id": failure.workflow_id,
        "item_index": failure.item_index,
    }


def _node_wire(
    node: NodeInspection,
    *,
    failure_key: str | None,
) -> dict[str, object]:
    inputs = node.inputs
    outputs = node.outputs
    return {
        "run_id": node.run_id,
        "span_id": node.span_id,
        "node_name": node.node_name,
        "qualified_name": node.qualified_name,
        "graph_name": node.graph_name,
        "item_index": node.item_index,
        "superstep": node.superstep,
        "sequence": node.sequence,
        "status": node.status,
        "values_captured": node.values_captured,
        "inputs": _serialized(inputs) if inputs is not None else None,
        "outputs": _serialized(outputs) if outputs is not None else None,
        "failure": _failure_wire(node.failure, failure_key=failure_key),
        "started_at_ms": node.started_at_ms,
        "ended_at_ms": node.ended_at_ms,
        "duration_ms": node.duration_ms,
        "cached": node.cached,
    }


def _failures_share_raw_identity(
    left: FailureEvidence,
    right: FailureEvidence,
    *,
    containing_item_index: int | None,
) -> bool:
    metadata_fields = (
        "node_name",
        "superstep",
        "duration_ms",
        "graph_name",
        "workflow_id",
    )
    if any(getattr(left, field) != getattr(right, field) for field in metadata_fields):
        return False
    if left.error is not right.error or left.inputs.keys() != right.inputs.keys():
        return False
    if any(left.inputs[key] is not right.inputs[key] for key in left.inputs):
        return False
    if left.item_index == right.item_index:
        return True
    return containing_item_index is not None and containing_item_index in {
        left.item_index,
        right.item_index,
    }


def _failure_keys(
    artifact: RunInspection,
) -> tuple[tuple[str | None, ...], tuple[str, ...]]:
    """Correlate raw evidence once, then expose only opaque wire ordinals."""
    node_groups: list[tuple[FailureEvidence, list[int]]] = []
    for node_index, node in enumerate(artifact.nodes):
        if node.failure is None:
            continue
        matching_group = next(
            (
                node_indexes
                for representative, node_indexes in node_groups
                if _failures_share_raw_identity(
                    representative,
                    node.failure,
                    containing_item_index=artifact.item_index,
                )
            ),
            None,
        )
        if matching_group is None:
            node_groups.append((node.failure, [node_index]))
        else:
            matching_group.append(node_index)

    public_keys = tuple(f"failure-{index}" for index, _ in enumerate(artifact.failures))
    node_keys: list[str | None] = [None] * len(artifact.nodes)
    unclaimed_groups = set(range(len(node_groups)))
    for failure_index, failure in enumerate(artifact.failures):
        candidates = [
            group_index
            for group_index in sorted(unclaimed_groups)
            if any(
                (node_failure := artifact.nodes[node_index].failure) is not None
                and _failures_share_raw_identity(
                    node_failure,
                    failure,
                    containing_item_index=artifact.item_index,
                )
                for node_index in node_groups[group_index][1]
            )
        ]
        exact_candidates = [
            group_index
            for group_index in candidates
            if any(artifact.nodes[node_index].qualified_name == failure.node_name for node_index in node_groups[group_index][1])
        ]
        if not candidates:
            continue
        selected_group = (exact_candidates or candidates)[0]
        for node_index in node_groups[selected_group][1]:
            node_keys[node_index] = public_keys[failure_index]
        unclaimed_groups.remove(selected_group)

    next_key = len(public_keys)
    for group_index in sorted(unclaimed_groups):
        failure_key = f"failure-{next_key}"
        next_key += 1
        for node_index in node_groups[group_index][1]:
            node_keys[node_index] = failure_key
    return tuple(node_keys), public_keys


def _run_wire(artifact: RunInspection) -> dict[str, object]:
    error = getattr(artifact, "error", None)
    node_failure_keys, public_failure_keys = _failure_keys(artifact)
    return {
        "run_id": artifact.run_id,
        "graph_name": artifact.graph_name,
        "workflow_id": artifact.workflow_id,
        "item_index": artifact.item_index,
        "status": artifact.status,
        "total_duration_ms": artifact.total_duration_ms,
        "captured": artifact.captured,
        "terminal": artifact.terminal,
        **({"runner_kind": artifact._runner_kind} if artifact._runner_kind is not None else {}),
        "error": _serialized(error) if error is not None else None,
        "nodes": [_node_wire(node, failure_key=node_failure_keys[index]) for index, node in enumerate(artifact.nodes)],
        "failures": [_failure_wire(failure, failure_key=public_failure_keys[index]) for index, failure in enumerate(artifact.failures)],
    }


def _map_wire(artifact: MapInspection) -> dict[str, object]:
    error = getattr(artifact, "error", None)
    items = [
        {
            "item_index": item.item_index,
            "status": item.status,
            "requested_inputs": (_serialized(item.requested_inputs) if item.requested_inputs is not None else None),
            "run": _run_wire(item.run) if item.run is not None else None,
            "restored": item.restored,
        }
        for item in artifact.items
    ]
    statuses = [item.status for item in artifact.items]
    pending = max(
        0,
        artifact.requested_count - len(artifact.items) - len(artifact.unstarted_item_indexes),
    )
    return {
        "run_id": artifact.run_id,
        "graph_name": artifact.graph_name,
        "workflow_id": artifact.workflow_id,
        "status": artifact.status,
        "map_over": list(artifact.map_over),
        "map_mode": artifact.map_mode,
        "requested_count": artifact.requested_count,
        "total_duration_ms": artifact.total_duration_ms,
        "captured": artifact.captured,
        "terminal": artifact.terminal,
        **({"runner_kind": artifact._runner_kind} if artifact._runner_kind is not None else {}),
        "error": _serialized(error) if error is not None else None,
        "counts": {
            "requested": artifact.requested_count,
            "claimed": len(artifact.items),
            "completed": artifact.completed_count,
            "failed": artifact.failed_count,
            "running": statuses.count("running"),
            "paused": statuses.count("paused"),
            "stopped": statuses.count("stopped"),
            "restored": artifact.restored_count,
            "unstarted": artifact.unstarted_count,
            "pending": pending,
        },
        "items": items,
        "unstarted_item_indexes": list(artifact.unstarted_item_indexes),
    }


def _script_safe_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@lru_cache(maxsize=2)
def _read_asset(name: str) -> str:
    """Read one packaged renderer asset without a network or runtime dependency."""
    return files("hypergraph.runners._shared.assets").joinpath(name).read_text(encoding="utf-8")


def render_inspection_payload(payload: dict[str, object]) -> str:
    """Render one already-built payload into the immutable offline shell."""
    kind = str(payload["kind"])
    return (
        f"<style data-hg-inspect-style>{_read_asset('inspect.css')}</style>"
        f'<section class="hg-inspect{" hg-inspect-map" if kind == "map" else ""}" '
        f'data-hypergraph-inspect="{kind}" data-inspect-schema="{_INSPECT_SCHEMA}">'
        '<header class="hg-inspect-header">'
        '<div><div class="hg-inspect-eyebrow">Hypergraph inspect</div>'
        '<h2 class="hg-inspect-title" data-hg-title>Loading inspection…</h2>'
        '<code class="hg-inspect-run-id" data-hg-run-id></code></div>'
        '<div class="hg-inspect-header-meta">'
        '<span class="hg-inspect-delivery" data-hg-delivery>'
        '<span class="hg-inspect-dot" aria-hidden="true"></span>'
        "<span data-hg-delivery-label>Saved snapshot</span></span>"
        '<span class="hg-inspect-badge" data-hg-sequence></span>'
        "</div></header>"
        '<div class="hg-inspect-summary" data-hg-summary '
        'aria-label="Execution summary"></div>'
        '<div class="hg-inspect-alert" data-hg-alert role="status" '
        'aria-live="polite" hidden>'
        "<span data-hg-alert-text></span>"
        '<button type="button" class="hg-inspect-button" '
        'data-action="show-failure" data-hg-show-failure>Show failure</button>'
        "</div>"
        f'<div class="hg-inspect-body" data-hg-body data-kind="{kind}">'
        '<aside class="hg-inspect-items" data-hg-items aria-label="Map items">'
        '<div class="hg-inspect-section-title">Items</div>'
        '<label class="hg-inspect-field-label">Status'
        '<select class="hg-inspect-select" data-hg-filter>'
        '<option value="all">All</option><option value="failed">Failed</option>'
        '<option value="running">Running</option>'
        '<option value="completed">Completed</option>'
        '<option value="restored">Restored</option>'
        '<option value="unstarted">Unstarted</option>'
        "</select></label>"
        '<div class="hg-inspect-item-list" data-hg-item-list></div>'
        '<div class="hg-inspect-pager">'
        '<button type="button" class="hg-inspect-button" '
        'data-action="prev-page" data-hg-prev-page>Prev</button>'
        "<span data-hg-page-label></span>"
        '<button type="button" class="hg-inspect-button" '
        'data-action="next-page" data-hg-next-page>Next</button>'
        "</div></aside>"
        '<main class="hg-inspect-main" data-hg-main>'
        '<nav class="hg-inspect-tabs" role="tablist" aria-label="Inspect view">'
        '<button type="button" class="hg-inspect-tab" role="tab" '
        'data-action="view" data-hg-view="items">Items</button>'
        '<button type="button" class="hg-inspect-tab" role="tab" '
        'data-action="view" data-hg-view="timeline">Timeline</button>'
        '<button type="button" class="hg-inspect-tab" role="tab" '
        'data-action="view" data-hg-view="graph">Graph</button>'
        "</nav>"
        '<section class="hg-inspect-panel" role="tabpanel" '
        'data-hg-panel="items"></section>'
        '<section class="hg-inspect-panel" role="tabpanel" '
        'data-hg-panel="timeline"></section>'
        '<section class="hg-inspect-panel" role="tabpanel" '
        'data-hg-panel="graph"></section>'
        "</main>"
        '<aside class="hg-inspect-detail" data-hg-detail '
        'aria-label="Selected execution details"></aside>'
        "</div>"
        '<footer class="hg-inspect-footer">'
        "<span data-hg-state-proof>View state is local to this snapshot.</span>"
        "<span data-hg-delivery-note>"
        "Saved output is locally interactive without a kernel or network."
        "</span></footer>"
        "<noscript>JavaScript is required for local drill-down; the semantic "
        "payload remains embedded in this saved output.</noscript>"
        f'<script type="application/json" data-hg-inspect-payload>'
        f"{_script_safe_json(payload)}</script>"
        f"<script data-hg-inspect-runtime>{_read_asset('inspect.js')}</script>"
        "</section>"
    )


def build_inspection_payload(
    artifact: RunInspection | MapInspection,
    *,
    delivery_state: InspectionDeliveryState,
    delivery_label: str,
) -> dict[str, object]:
    """Build the one semantic wire shared by saved and live delivery."""
    if isinstance(artifact, MapInspection):
        return {
            "schema": _INSPECT_SCHEMA,
            "kind": "map",
            "default_view": "items",
            "delivery": {"state": delivery_state, "label": delivery_label},
            "map": _map_wire(artifact),
        }
    return {
        "schema": _INSPECT_SCHEMA,
        "kind": "run",
        "default_view": "timeline",
        "delivery": {"state": delivery_state, "label": delivery_label},
        "run": _run_wire(artifact),
    }


def render_run_inspection(artifact: RunInspection) -> str:
    """Render one run artifact as a semantic, versioned saved snapshot."""
    payload = build_inspection_payload(
        artifact,
        delivery_state="saved",
        delivery_label="Saved snapshot",
    )
    return render_inspection_payload(payload)


def render_map_inspection(artifact: MapInspection) -> str:
    """Render one map artifact as a semantic original-index snapshot."""
    payload = build_inspection_payload(
        artifact,
        delivery_state="saved",
        delivery_label="Saved snapshot",
    )
    return render_inspection_payload(payload)


def render_inspection_frame(child_html: str) -> str:
    """Isolate one saved inspection renderer from its notebook host."""
    child_document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head><body style="margin:0">'
        f"{child_html}"
        "</body></html>"
    )
    return (
        '<iframe title="Hypergraph execution inspection" sandbox="allow-scripts" '
        'style="display:block;box-sizing:border-box;width:100%;min-width:0;'
        'height:720px;border:0" '
        f'srcdoc="{html.escape(child_document, quote=True)}"></iframe>'
    )
