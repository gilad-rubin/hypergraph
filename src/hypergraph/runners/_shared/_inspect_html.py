"""Safe settled HTML presentation for typed inspection artifacts."""

from __future__ import annotations

import html
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal

from hypergraph.runners._shared._inspect import MapInspection, NodeInspection, RunInspection
from hypergraph.runners._shared._inspect_serialization import (
    SerializedValue,
    serialize_value,
    serialized_value_to_wire,
)
from hypergraph.runners._shared._wire import sandboxed_child_document, script_safe_json
from hypergraph.runners._shared.results import FailureEvidence
from hypergraph.runners.inspection import InspectionDisplay as InspectionDisplay

_INSPECT_SCHEMA = "hypergraph.inspect/v1"
InspectionDeliveryState = Literal["live", "stale", "saved"]


_SERIALIZATION_PASS: ContextVar[dict[int, SerializedValue] | None] = ContextVar(
    "hypergraph_inspect_serialization_pass",
    default=None,
)


@contextmanager
def one_serialization_pass() -> Iterator[None]:
    """Serialize each captured value at most once for this render.

    The payload and the no-script summary show the same values.  A value whose
    ``repr`` changes every call would otherwise read differently in the two
    places, because each render would consume it twice.
    """
    token = _SERIALIZATION_PASS.set({})
    try:
        yield
    finally:
        _SERIALIZATION_PASS.reset(token)


def _serialize_once(value: object) -> SerializedValue:
    pass_cache = _SERIALIZATION_PASS.get()
    if pass_cache is None:
        return serialize_value(value)
    # The artifact holds every captured value for the whole pass, so identity
    # stays stable while this cache lives.
    cached = pass_cache.get(id(value))
    if cached is None:
        cached = serialize_value(value)
        pass_cache[id(value)] = cached
    return cached


def _serialized(value: object) -> dict[str, object]:
    return serialized_value_to_wire(_serialize_once(value))  # type: ignore[return-value]


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


def _run_wire(artifact: RunInspection) -> dict[str, object]:
    error = getattr(artifact, "error", None)
    public_failure_keys = artifact.failure_keys
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
        "nodes": [_node_wire(node, failure_key=node.failure_key) for node in artifact.nodes],
        "failures": [
            _failure_wire(
                failure,
                failure_key=(public_failure_keys[index] if index < len(public_failure_keys) else None),
            )
            for index, failure in enumerate(artifact.failures)
        ],
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
        f"{script_safe_json(payload)}</script>"
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


_DEBUG_WORKFLOWS_DOC = "docs/05-how-to/debug-workflows.md"
_NATIVE_WRAP_CHUNK_SIZE = 32
_NativeFailureSource = Literal["node", "run", "batch", "status", "start", "none"]


def _native_value_text(value: SerializedValue) -> str:
    """Format one already-bounded serialized value as inert text."""
    if value.kind == "null":
        return "None"
    if value.kind == "boolean":
        return "True" if value.value is True else "False"
    if value.kind == "number":
        return str(value.value)
    if value.kind in {"text", "exception"}:
        text = value.text or ""
        if value.truncated:
            return f"{text} … truncated from {_native_count(value.original_size)} characters"
        return text
    if value.kind == "placeholder":
        detail = value.text or value.reason or "value unavailable"
        return f"{value.type_name or 'value'}: {detail}"
    if value.kind == "mapping":
        parts = [f"{_native_value_text(entry.key)}={_native_value_text(entry.value)}" for entry in value.entries]
        if value.truncated:
            parts.append(f"… truncated from {_native_count(value.original_size)} entries")
        return "{" + ", ".join(parts) + "}"
    if value.kind == "sequence":
        parts = [_native_value_text(item) for item in value.items]
        if value.truncated:
            parts.append(f"… truncated from {_native_count(value.original_size)} items")
        return "[" + ", ".join(parts) + "]"
    if value.kind == "table" and value.table is not None:
        return f"{value.table.original_row_count} × {value.table.original_column_count} table"
    return value.text or str(value.value) or value.type_name or "value unavailable"


def _native_count(original_size: int | None) -> str:
    return "unknown" if original_size is None else str(original_size)


def _native_wrappable_markup(text: str) -> str:
    """Escape inert text and add copy-inert line-break opportunities."""
    return "<wbr>".join(html.escape(text[offset : offset + _NATIVE_WRAP_CHUNK_SIZE]) for offset in range(0, len(text), _NATIVE_WRAP_CHUNK_SIZE))


def _native_code_markup(text: str) -> str:
    """Preserve exact visible/copy text without allowing narrow-page overflow."""
    escaped = _native_wrappable_markup(text)
    if text == " ".join(text.split()):
        return f"<code>{escaped}</code>"
    return f"<pre><code>{escaped}</code></pre>"


def _native_repr_with_type(error_type: str, error_repr: str) -> str:
    """Keep an anchored repr type once without erasing opaque repr content."""
    if error_repr.startswith(error_type):
        remainder = error_repr[len(error_type) :]
        if not remainder or remainder[0] in " \t\r\n([{<:":
            return error_repr
    return f"{error_type}: {error_repr}"


def _native_inputs_markup(inputs: Mapping[str, Any]) -> str:
    serialized = _serialize_once(inputs)
    if serialized.kind != "mapping":
        return f"<div>{_native_code_markup(_native_value_text(serialized))}</div>"
    if not serialized.entries:
        return f"<div>{_native_code_markup('{}')}</div>"
    rows = [f"<li>{_native_code_markup(f'{_native_value_text(entry.key)}={_native_value_text(entry.value)}')}</li>" for entry in serialized.entries]
    if serialized.truncated:
        rows.append(f"<li>Inputs truncated from {html.escape(_native_count(serialized.original_size))} entries.</li>")
    return "<ul>" + "".join(rows) + "</ul>"


def _native_exception_markup(error: SerializedValue, *, exact_label: str) -> str:
    error_type = error.type_name or "Error"
    if error.kind == "placeholder" or (error.kind in {"text", "exception"} and error.text is None):
        detail = error.reason or "serialized exception text unavailable"
        return f"<div>Exception details unavailable: {_native_code_markup(f'{error_type} — {detail}')}</div>"
    if error.kind == "text":
        label = "Exception preview (bounded repr)"
        if error.truncated:
            label = f"Exception preview (bounded repr; truncated from {_native_count(error.original_size)} characters)"
        error_repr = _native_repr_with_type(error_type, error.text or "")
        return f"<div>{html.escape(label)}: {_native_code_markup(error_repr)}</div>"
    if error.kind == "exception":
        label = exact_label
        if error.truncated:
            label = f"Exception preview (truncated from {_native_count(error.original_size)} characters)"
        return f"<div>{html.escape(label)}: {_native_code_markup(f'{error_type}: {error.text}')}</div>"
    return f"<div>Exception preview (serialized value): {_native_code_markup(f'{error_type}: {_native_value_text(error)}')}</div>"


def _run_has_failure_evidence(run: RunInspection) -> bool:
    return bool(run.failures) or any(node.failure is not None for node in run.nodes)


def _run_is_failed(run: RunInspection, *, status_failed: bool = False) -> bool:
    return status_failed or run.status == "failed" or any(node.status == "failed" for node in run.nodes)


def _failed_run_and_item(
    artifact: RunInspection | MapInspection,
) -> tuple[RunInspection | None, int | None, _NativeFailureSource]:
    """Pick the one execution the native fallback speaks about."""
    if isinstance(artifact, RunInspection):
        if _run_has_failure_evidence(artifact):
            return artifact, artifact.item_index, "node"
        if artifact.error is not None:
            return artifact, artifact.item_index, "run"
        if _run_is_failed(artifact):
            return artifact, artifact.item_index, "status"
        return artifact, artifact.item_index, "none"

    item_runs = [(item, item.run) for item in artifact.items if item.run is not None]
    for item, run in item_runs:
        if _run_has_failure_evidence(run):
            return run, item.item_index, "node"
    for item, run in item_runs:
        if run.error is not None:
            return run, item.item_index, "run"
    if artifact.error is not None:
        return None, None, "batch"
    for item, run in item_runs:
        if _run_is_failed(run, status_failed=item.status == "failed"):
            return run, item.item_index, "status"
    return None, None, "none"


def _first_failure_and_node(
    run: RunInspection,
) -> tuple[FailureEvidence | None, NodeInspection | None]:
    """Pair the run's first published failure with the execution that recorded it."""
    failure = run.failures[0] if run.failures else None
    if failure is not None:
        failure_key = run.failure_keys[0] if run.failure_keys else None
        return failure, next(
            (
                node
                for node in run.nodes
                if node.failure_key is not None and node.failure_key == failure_key and node.qualified_name == failure.node_name
            ),
            None,
        )
    nodes_with_failure = [node for node in run.nodes if node.failure is not None]
    named = next(
        (node for node in nodes_with_failure if node.failure is not None and node.qualified_name == node.failure.node_name),
        None,
    )
    if named is not None:
        return named.failure, named
    if nodes_with_failure:
        return nodes_with_failure[0].failure, None
    return None, next((node for node in run.nodes if node.status == "failed"), None)


def _run_failure_count(run: RunInspection, *, status_failed: bool = False) -> int:
    if run.failures:
        return len(run.failures)
    embedded = sum(node.failure is not None for node in run.nodes)
    if embedded:
        return embedded
    if run.error is not None:
        return 1
    return 1 if _run_is_failed(run, status_failed=status_failed) else 0


def _failure_count(
    artifact: RunInspection | MapInspection,
    *,
    has_message: bool,
) -> int:
    if isinstance(artifact, RunInspection):
        return _run_failure_count(artifact) + int(has_message)
    total = 0
    for item in artifact.items:
        if item.run is not None:
            total += _run_failure_count(item.run, status_failed=item.status == "failed")
        elif item.status == "failed":
            total += 1
    return total + int(artifact.error is not None) + int(has_message)


def _native_failure_markup(
    artifact: RunInspection | MapInspection,
    *,
    message: SerializedValue | None = None,
) -> str:
    """Render the one failure the settled artifact already knows about, facts only."""
    run, item_index, source = _failed_run_and_item(artifact)
    if message is not None and source in {"none", "status"}:
        run, item_index, source = None, None, "start"
    failure, node = _first_failure_and_node(run) if run is not None and source in {"node", "status"} else (None, None)
    node_failure = node.failure if node is not None else None
    if source == "node":
        error_value = (failure.error if failure is not None else None) or (node_failure.error if node_failure is not None else None)
        error = _serialize(error_value)
    elif source == "run":
        error = _serialize(run.error if run is not None else None)
    elif source == "batch":
        error = _serialize(artifact.error)
    elif source == "start":
        error = message
    else:
        error = None
    inputs: Mapping[str, Any] | None = None
    qualified_name: str | None = None
    if source in {"node", "status"}:
        for candidate in (
            failure.inputs if failure is not None else None,
            node_failure.inputs if node_failure is not None else None,
            node.inputs if node is not None else None,
        ):
            if candidate:
                inputs = candidate
                break
        qualified_name = (node.qualified_name if node is not None else None) or (failure.node_name if failure is not None else None)
    if error is None and inputs is None and qualified_name is None:
        return ""

    if item_index is not None:
        title = f"Item {item_index} failure"
    elif source == "start":
        title = "Start failure"
    elif source == "batch":
        title = "Batch failure"
    else:
        title = "Run failure"
    title = f"{title} — First failure of {max(1, _failure_count(artifact, has_message=message is not None))}"

    facts: list[str] = []
    if item_index is not None:
        facts.append(f"<p>Original item: <code>{html.escape(str(item_index))}</code></p>")
    if qualified_name is not None:
        facts.append(f"<p>Qualified node: <code>{html.escape(qualified_name)}</code></p>")
    if inputs is not None:
        facts.append("<p>Captured inputs:</p>")
        facts.append(_native_inputs_markup(inputs))
    if error is not None:
        exact_label = {
            "run": "Exact run exception",
            "batch": "Exact batch exception",
        }.get(source, "Exact exception")
        facts.append(_native_exception_markup(error, exact_label=exact_label))
    facts.append(f"<p>Debugging guide: <code>{_DEBUG_WORKFLOWS_DOC}</code></p>")
    return f"<details data-hg-inspect-native-failure><summary>{html.escape(title)}</summary>{''.join(facts)}</details>"


def _serialize(error: BaseException | None) -> SerializedValue | None:
    return None if error is None else _serialize_once(error)


def render_native_summary(
    artifact: RunInspection | MapInspection,
    *,
    widget_id: str,
    delivery_label: str,
    message: SerializedValue | None = None,
) -> str:
    """Render the no-script settled view: the same facts, no renderer needed."""
    if isinstance(artifact, MapInspection):
        counts_markup = (
            f"<dt>Completed</dt><dd data-hg-inspect-native-completed>{artifact.completed_count}</dd>"
            f"<dt>Failed</dt><dd data-hg-inspect-native-failed>{artifact.failed_count}</dd>"
            f"<dt>Unstarted</dt><dd>{artifact.unstarted_count}</dd>"
        )
    else:
        completed = sum(node.status == "completed" for node in artifact.nodes)
        failed = sum(node.status == "failed" for node in artifact.nodes)
        counts_markup = (
            f"<dt>Completed nodes</dt><dd data-hg-inspect-native-completed>{completed}</dd>"
            f"<dt>Failed nodes</dt><dd data-hg-inspect-native-failed>{failed}</dd>"
        )
    return (
        f'<section data-hg-inspect-native-summary="{html.escape(widget_id, quote=True)}" '
        'aria-label="Saved execution summary">'
        f"<p><strong>{html.escape(delivery_label)}</strong> — {html.escape(artifact.graph_name or 'Hypergraph execution')}</p>"
        f"<dl><dt>Status</dt><dd><code>{html.escape(artifact.status or 'unknown')}</code></dd>{counts_markup}</dl>"
        f"{_native_failure_markup(artifact, message=message)}"
        "</section>"
    )


def render_inspection_frame(child_html: str) -> str:
    """Isolate one saved inspection renderer from its notebook host."""
    child_document = sandboxed_child_document(child_html)
    return (
        '<iframe title="Hypergraph execution inspection" sandbox="allow-scripts" '
        'style="display:block;box-sizing:border-box;width:100%;min-width:0;'
        'height:720px;border:0" '
        f'srcdoc="{html.escape(child_document, quote=True)}"></iframe>'
    )
