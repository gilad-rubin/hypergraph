"""OpenTelemetry export processor for Hypergraph execution events.

Hypergraph events remain the source of truth. This processor projects those
events into an OpenTelemetry span tree so runs can be exported to external
observability backends without replacing Hypergraph's native inspect/debug UX.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from threading import Lock
from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import TypedEventProcessor

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hypergraph.events.types import (
        CacheHitEvent,
        InterruptEvent,
        NodeAttemptEndEvent,
        NodeAttemptStartEvent,
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RouteDecisionEvent,
        RunEndEvent,
        RunStartEvent,
        StopRequestedEvent,
        SuperstepStartEvent,
    )


def _require_opentelemetry() -> None:
    """Raise a clear error if opentelemetry is not installed."""
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        raise ImportError(
            "The 'opentelemetry' package is required for OpenTelemetryProcessor. "
            "Install with: pip install 'hypergraph-ai[otel]' "
            "or: pip install opentelemetry-api opentelemetry-sdk"
        ) from None


def _set_attrs(span: Any, attributes: dict[str, Any]) -> None:
    """Set only bounded, non-null attributes on a span."""
    for key, value in attributes.items():
        if value is None:
            continue
        span.set_attribute(key, value)


def _clean_attrs(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop null attributes before sending them to the OTel SDK."""
    return {key: value for key, value in attributes.items() if value is not None}


def _base_attrs(event: Any) -> dict[str, Any]:
    return {
        "hypergraph.run_id": event.run_id,
        "hypergraph.workflow_id": event.workflow_id,
        "hypergraph.item_index": event.item_index,
    }


def _lineage_kind(event: RunStartEvent) -> str | None:
    if event.retry_of:
        return "retry"
    if event.forked_from:
        return "fork"
    if event.is_resume:
        return "resume"
    return None


_MAX_LINEAGE_CONTEXTS = 256
_MAX_TRACE_IDS = 1024
_DEFAULT_MAX_PAYLOAD_CHARS = 4096
_TRUNCATION_SUFFIX = "…[truncated]"


def _payload_attrs(prefix: str, payload: Any, max_chars: int) -> dict[str, Any]:
    """Serialize a trace payload into OpenInference ``input``/``output`` attrs.

    JSON first (so Phoenix renders it structurally), ``repr`` as the fallback
    for anything not JSON-serializable, and truncation past ``max_chars``. A
    value that cannot be rendered at all is omitted rather than exported
    half-formed; capture never raises and never fails the run.
    """
    try:
        text = json.dumps(payload, default=repr, ensure_ascii=False)
        mime = "application/json"
    except Exception:
        try:
            text = repr(payload)
            mime = "text/plain"
        except Exception:
            return {}
    if len(text) > max_chars:
        text = text[:max_chars] + _TRUNCATION_SUFFIX
        # A truncated JSON document is no longer valid JSON; say so honestly
        # rather than handing a backend something it will fail to parse.
        mime = "text/plain"
    return {f"{prefix}.value": text, f"{prefix}.mime_type": mime}


class OpenTelemetryProcessor(TypedEventProcessor):
    """Convert Hypergraph events into OTel spans and span events."""

    def __init__(
        self,
        tracer_name: str = "hypergraph",
        *,
        extra_attributes: Mapping[str, str | int | float | bool] | None = None,
        tracer_provider: Any | None = None,
        set_success_status: bool = False,
        enrich_openinference: bool = False,
        redact_errors: bool = False,
        redact_payloads: bool = False,
        max_payload_chars: int = _DEFAULT_MAX_PAYLOAD_CHARS,
    ) -> None:
        """Create an OTel export processor.

        Args:
            tracer_name: Name passed to ``get_tracer`` on the resolved provider.
            extra_attributes: Attributes merged onto **every** span this
                processor creates — graph, map, mapped-item, and node spans
                alike. All spans rather than the root only: it is cheap, and
                lets backends filter on any span. Hypergraph's own span
                attributes win on key collisions.
            tracer_provider: OTel ``TracerProvider`` to write spans to. When
                ``None`` (default), the tracer is looked up on the global
                provider exactly as before. When provided, spans go only to
                this provider — the global tracer provider is neither
                consulted nor modified.
            set_success_status: Set ``StatusCode.OK`` for genuinely completed
                runs and nodes. Disabled by default, as recommended for OTel
                instrumentation libraries.
            enrich_openinference: Add OpenInference ``graph.node.*``
                containment attributes and ``openinference.span.kind=CHAIN``.
                Disabled by default because not every Hypergraph workflow is
                an AI chain.
            redact_errors: Export only the privacy-safe error projection
                (``NodeErrorEvent.error`` / ``RunEndEvent.error``) instead of
                the real exception message and traceback. **Off by default**:
                a trace whose ``exception.message`` reads "Node 'x' raised
                ValueError." cannot be debugged, and standard OTel
                ``record_exception`` carries the real message. Turn it on for
                regulated deployments where exception text may contain
                regulated data; the export is then byte-identical to the
                pre-``redact_errors`` behavior. Durable Hypergraph records
                (RunLog, StepRecord, checkpoints, attempt ledger) always keep
                the safe projection and are unaffected by this flag.
            redact_payloads: Refuse ALL node input/output payloads regardless
                of any ``trace_io=True`` on a node or graph. The
                regulated-deployment kill switch, symmetric with
                ``redact_errors``; precedence is kill switch > node explicit >
                graph default > off.
            max_payload_chars: Per-value cap for an exported payload. Longer
                values are truncated (and marked ``text/plain``, since a cut
                JSON document is no longer parseable).
        """
        _require_opentelemetry()
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace import Link, Status, StatusCode

        self._trace = trace
        self._context_api = otel_context
        if tracer_provider is not None:
            self._tracer = tracer_provider.get_tracer(tracer_name)
        else:
            self._tracer = trace.get_tracer(tracer_name)
        self._extra_attributes: dict[str, Any] = {
            key: value
            for key, value in (extra_attributes or {}).items()
            if key not in {"hypergraph.span.role", "hypergraph.node_name"} and not key.startswith("hypergraph.nested.")
        }
        self._Link = Link
        self._Status = Status
        self._StatusCode = StatusCode
        self._set_success_status = set_success_status
        self._enrich_openinference = enrich_openinference
        self._redact_errors = redact_errors
        self._redact_payloads = redact_payloads
        self._max_payload_chars = max_payload_chars
        self._spans: dict[str, Any] = {}
        self._contexts: dict[str, Any] = {}
        self._tokens: dict[str, Any] = {}
        # Absorbed run ids are aliases, never owners of spans, contexts, or
        # ambient tokens.  Owner metadata is keyed only by physical span id.
        self._aliases: dict[str, str] = {}
        self._absorbed_run: dict[str, str] = {}
        self._nested_outcome: dict[str, str] = {}
        self._logical_ids: dict[str, str] = {}
        self._collapse_candidates: set[str] = set()
        self._workflow_span_contexts: OrderedDict[str, Any] = OrderedDict()
        self._workflow_span_contexts_lock = Lock()
        self._trace_ids: OrderedDict[str, str] = OrderedDict()
        self._trace_ids_lock = Lock()

    def _owner_id(self, span_id: str | None) -> str | None:
        if span_id is None:
            return None
        return self._aliases.get(span_id, span_id)

    def _span_for(self, span_id: str | None) -> Any | None:
        owner_id = self._owner_id(span_id)
        return self._spans.get(owner_id) if owner_id is not None else None

    def _context_for(self, span_id: str | None) -> Any | None:
        owner_id = self._owner_id(span_id)
        return self._contexts.get(owner_id) if owner_id is not None else None

    def _oi_attrs(self, logical_id: str, parent_span_id: str | None) -> dict[str, Any]:
        if not self._enrich_openinference:
            return {}
        parent_id = self._owner_id(parent_span_id)
        return {
            "openinference.span.kind": "CHAIN",
            "graph.node.id": logical_id,
            "graph.node.name": logical_id,
            "graph.node.parent_id": self._logical_ids.get(parent_id) if parent_id else None,
        }

    def _release_aliases(self, owner_id: str) -> None:
        run_id = self._absorbed_run.pop(owner_id, None)
        if run_id is not None:
            self._aliases.pop(run_id, None)

    # -- Ambient context activation -------------------------------------------
    #
    # Beyond projecting events into spans, this processor makes each span the
    # AMBIENT OTel context for the code it covers: the run root around the run,
    # each node span around that node's body. Third-party instrumentation
    # (openinference, agent SDKs) that starts spans inside a node body then
    # parents them under the node span automatically — zero coupling.
    #
    # This works because event dispatch brackets execution IN the executing
    # context: the sync runner emits node start/end synchronously around the
    # executor call in one thread, and the async runner emits them inside the
    # same per-node asyncio task that awaits the executor (each task owns a
    # contextvars copy, so concurrent nodes and concurrent map items cannot
    # see each other's attach). Span parentage bookkeeping (explicit
    # ``context=parent_ctx`` at span creation) is unchanged — activation only
    # changes what ``opentelemetry.context.get_current()`` returns inside the
    # bracketed code.

    def _attach_ambient(self, span_id: str) -> None:
        """Make the span's context ambient; remember the token for detach."""
        ctx = self._contexts.get(span_id)
        if ctx is None:
            return
        self._tokens[span_id] = self._context_api.attach(ctx)

    def _detach_ambient(self, span_id: str) -> None:
        """Restore the previous ambient context.

        Only called from event handlers that run in the same context that
        attached (run end, node end, node error — guaranteed by the runner
        templates). If that invariant ever breaks, ``opentelemetry.context``
        logs a loud "Failed to detach context" error rather than raising.
        """
        token = self._tokens.pop(span_id, None)
        if token is None:
            return
        self._context_api.detach(token)

    def _detach_ambient_if_current(self, span_id: str) -> None:
        """Detach only when this execution unit provably owns the attach.

        Used where cross-context arrival is NORMAL: an async pause raises out
        of the node's asyncio task without a node end/error event, so the
        InterruptEvent (and shutdown) observe the token from a different
        context whose contextvars copy is already gone. Detaching there would
        make OTel log "Failed to detach context" on a healthy path; dropping
        the token is safe because the attach only ever mutated the dead task's
        context copy.
        """
        token = self._tokens.pop(span_id, None)
        if token is None:
            return
        if self._context_api.get_current() is self._contexts.get(span_id):
            self._context_api.detach(token)

    def _get_linked_workflow_context(self, workflow_id: str | None) -> Any | None:
        """Return a recent workflow span context, or None if evicted/missing."""
        if workflow_id is None:
            return None
        with self._workflow_span_contexts_lock:
            span_context = self._workflow_span_contexts.pop(workflow_id, None)
            if span_context is None:
                return None
            self._workflow_span_contexts[workflow_id] = span_context
            return span_context

    def _remember_workflow_context(self, workflow_id: str | None, span_context: Any) -> None:
        """Remember the most recent span context for a workflow in a bounded cache."""
        if workflow_id is None:
            return
        with self._workflow_span_contexts_lock:
            self._workflow_span_contexts.pop(workflow_id, None)
            self._workflow_span_contexts[workflow_id] = span_context
            if len(self._workflow_span_contexts) > _MAX_LINEAGE_CONTEXTS:
                self._workflow_span_contexts.popitem(last=False)

    # -- Run <-> trace correlation --------------------------------------------

    def _remember_trace_id(self, run_id: str, span: Any) -> None:
        """Record the hex trace id a run's spans landed in (bounded, LRU)."""
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return
        trace_id = format(span_context.trace_id, "032x")
        with self._trace_ids_lock:
            self._trace_ids.pop(run_id, None)
            self._trace_ids[run_id] = trace_id
            if len(self._trace_ids) > _MAX_TRACE_IDS:
                self._trace_ids.popitem(last=False)

    def trace_id_for(self, run_id: str) -> str | None:
        """The 32-char hex OTel trace id this run's spans were exported under.

        Populated when the run's span starts, and — unlike the span
        bookkeeping — deliberately NOT cleared by :meth:`shutdown`: the runner
        shuts the processor down before ``runner.run()`` returns, so clearing
        here would destroy the mapping exactly when the caller wants to link
        its ``RunResult.run_id`` to a trace URL.

        Recorded for every run that gets a span — top-level runs, nested graph
        runs, and mapped items alike — in an LRU bounded to the last
        ``1024`` runs, so a long-lived process cannot grow without limit. A
        run evicted by that bound (or one that never produced a valid span
        context, e.g. under a no-op tracer provider) returns ``None``.

        Args:
            run_id: The ``RunResult.run_id`` / ``event.run_id`` to look up.

        Returns:
            The lowercase hex trace id, or ``None`` if unknown.

        Example:
            >>> result = runner.run(graph, event_processors=[processor])  # doctest: +SKIP
            >>> processor.trace_id_for(result.run_id)  # doctest: +SKIP
            '4bf92f3577b34da6a3ce929d0e0e4736'
        """
        with self._trace_ids_lock:
            return self._trace_ids.get(run_id)

    def on_run_start(self, event: RunStartEvent) -> None:
        parent_owner = self._owner_id(event.parent_span_id)
        parent_ctx = self._context_for(event.parent_span_id)
        links = []
        lineage_kind = _lineage_kind(event)
        source_workflow_id = event.retry_of or event.forked_from or (event.workflow_id if event.is_resume else None)
        if lineage_kind is not None and source_workflow_id is not None:
            source_ctx = self._get_linked_workflow_context(source_workflow_id)
            if source_ctx is not None:
                links.append(
                    self._Link(
                        source_ctx,
                        attributes={"hypergraph.lineage.relationship": lineage_kind},
                    )
                )

        # Structurally collapse the first child run of each live ordinary node.
        if parent_owner is not None and parent_owner in self._collapse_candidates and parent_owner not in self._absorbed_run:
            span = self._spans[parent_owner]
            self._collapse_candidates.discard(parent_owner)
            self._aliases[event.span_id] = parent_owner
            self._absorbed_run[parent_owner] = event.span_id
            role = "map" if event.is_map else "graph"
            _set_attrs(
                span,
                {
                    "hypergraph.span.role": role,
                    "hypergraph.nested.graph_name": event.graph_name,
                    "hypergraph.nested.run_id": event.run_id,
                    "hypergraph.nested.workflow_id": event.workflow_id,
                    "hypergraph.is_map": event.is_map,
                    "hypergraph.map_size": event.map_size,
                    "hypergraph.parent_workflow_id": event.parent_workflow_id,
                    "hypergraph.forked_from": event.forked_from,
                    "hypergraph.fork_superstep": event.fork_superstep,
                    "hypergraph.retry_of": event.retry_of,
                    "hypergraph.retry_index": event.retry_index,
                    "hypergraph.is_resume": event.is_resume,
                },
            )
            for link in links:
                span.add_link(link.context, link.attributes)
            self._add_lineage_events(span, event)
            self._remember_trace_id(event.run_id, span)
            return

        graph_name = event.graph_name or "graph"
        name = f"{graph_name}.item" if event.item_index is not None else graph_name
        role = "map" if event.is_map and event.item_index is None else "graph"
        attributes = {
            **self._extra_attributes,
            **_base_attrs(event),
            **self._oi_attrs(name, event.parent_span_id),
            "hypergraph.span.role": role,
            "hypergraph.graph_name": event.graph_name,
            "hypergraph.is_map": event.is_map,
            "hypergraph.map_size": event.map_size,
            "hypergraph.parent_workflow_id": event.parent_workflow_id,
            "hypergraph.forked_from": event.forked_from,
            "hypergraph.fork_superstep": event.fork_superstep,
            "hypergraph.retry_of": event.retry_of,
            "hypergraph.retry_index": event.retry_index,
            "hypergraph.is_resume": event.is_resume,
        }
        span = self._tracer.start_span(
            name=name,
            context=parent_ctx,
            attributes=_clean_attrs(attributes),
            links=links,
        )
        self._spans[event.span_id] = span
        self._logical_ids[event.span_id] = name
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)
        self._attach_ambient(event.span_id)
        self._remember_trace_id(event.run_id, span)

        self._add_lineage_events(span, event)

    def _add_lineage_events(self, span: Any, event: RunStartEvent) -> None:
        if event.is_resume:
            span.add_event(
                "hypergraph.resume",
                attributes=_clean_attrs({"hypergraph.source_workflow_id": event.workflow_id}),
            )
        if event.forked_from is not None:
            span.add_event(
                "hypergraph.fork",
                attributes=_clean_attrs(
                    {
                        "hypergraph.source_workflow_id": event.forked_from,
                        "hypergraph.source_superstep": event.fork_superstep,
                    }
                ),
            )
        if event.retry_of is not None:
            span.add_event(
                "hypergraph.retry",
                attributes=_clean_attrs(
                    {
                        "hypergraph.source_workflow_id": event.retry_of,
                        "hypergraph.retry_index": event.retry_index,
                    }
                ),
            )

    def on_run_end(self, event: RunEndEvent) -> None:
        owner_id = self._aliases.pop(event.span_id, None)
        if owner_id is not None:
            span = self._spans.get(owner_id)
            if span is None:
                return
            outcome = event.status.value
            self._nested_outcome[owner_id] = outcome
            _set_attrs(
                span,
                {
                    "hypergraph.nested.duration_ms": event.duration_ms,
                    "hypergraph.nested.outcome": outcome,
                    "hypergraph.run.outcome": outcome,
                    "hypergraph.batch.total_items": event.batch_total_items,
                    "hypergraph.batch.completed_items": event.batch_completed_items,
                    "hypergraph.batch.failed_items": event.batch_failed_items,
                    "hypergraph.batch.paused_items": event.batch_paused_items,
                    "hypergraph.batch.stopped_items": event.batch_stopped_items,
                    "hypergraph.batch.restored_items": event.batch_restored_items,
                    "hypergraph.batch.outcome": event.batch_outcome,
                },
            )
            if event.workflow_id is not None:
                self._remember_workflow_context(event.workflow_id, span.get_span_context())
            return
        self._detach_ambient(event.span_id)
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        self._logical_ids.pop(event.span_id, None)
        if span is None:
            return

        _set_attrs(
            span,
            {
                "hypergraph.graph_name": event.graph_name,
                "hypergraph.run.outcome": event.status.value,
                "hypergraph.duration_ms": event.duration_ms,
                "hypergraph.batch.total_items": event.batch_total_items,
                "hypergraph.batch.completed_items": event.batch_completed_items,
                "hypergraph.batch.failed_items": event.batch_failed_items,
                "hypergraph.batch.paused_items": event.batch_paused_items,
                "hypergraph.batch.stopped_items": event.batch_stopped_items,
                "hypergraph.batch.restored_items": event.batch_restored_items,
                "hypergraph.batch.outcome": event.batch_outcome,
            },
        )
        if event.status.value == "failed" or event.error:
            span.set_status(self._Status(self._StatusCode.ERROR, self._status_description(event)))
        if event.error:
            span.add_event("exception", attributes=_clean_attrs(self._exception_attrs(event)))
        elif self._set_success_status and event.status.value == "completed":
            span.set_status(self._Status(self._StatusCode.OK))
        if event.workflow_id is not None:
            self._remember_workflow_context(event.workflow_id, span.get_span_context())
        span.end()

    def _trace_payload_attrs(self, prefix: str, payload: Any) -> dict[str, Any]:
        """Payload attrs for an opted-in node, or nothing at all."""
        if payload is None or self._redact_payloads:
            return {}
        return _payload_attrs(prefix, payload, self._max_payload_chars)

    def on_node_start(self, event: NodeStartEvent) -> None:
        parent_ctx = self._context_for(event.parent_span_id)
        span = self._tracer.start_span(
            name=event.node_name,
            context=parent_ctx,
            attributes={
                k: v
                for k, v in {
                    **self._extra_attributes,
                    **_base_attrs(event),
                    **self._oi_attrs(event.node_name, event.parent_span_id),
                    "hypergraph.span.role": "node",
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.superstep": event.superstep,
                    **self._trace_payload_attrs("input", getattr(event, "trace_inputs", None)),
                }.items()
                if v is not None
            },
        )
        self._spans[event.span_id] = span
        self._logical_ids[event.span_id] = event.node_name
        self._collapse_candidates.add(event.span_id)
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)
        self._attach_ambient(event.span_id)

    def on_node_attempt_start(self, event: NodeAttemptStartEvent) -> None:
        """Attempt start = span event on the single logical node span."""
        span = self._span_for(event.parent_span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.attempt.start",
            attributes=_clean_attrs(
                {
                    "hypergraph.attempt.series_id": event.attempt_series_id,
                    "hypergraph.attempt.number": event.attempt_number,
                    "hypergraph.attempt.max_attempts": event.max_attempts,
                    "hypergraph.attempt.timeout_seconds": event.timeout_seconds,
                    "hypergraph.attempt.deadline_at": (event.attempt_deadline_at.isoformat() if event.attempt_deadline_at else None),
                    "hypergraph.attempt.series_deadline_at": (event.series_deadline_at.isoformat() if event.series_deadline_at else None),
                    "hypergraph.node_name": event.node_name,
                }
            ),
        )

    def on_node_attempt_end(self, event: NodeAttemptEndEvent) -> None:
        """Attempt end = span event; it NEVER marks the node span as error.

        Only the terminal escaping failure (``NodeErrorEvent``) sets error
        status on the logical node span.
        """
        span = self._span_for(event.parent_span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.attempt.end",
            attributes=_clean_attrs(
                {
                    "hypergraph.attempt.series_id": event.attempt_series_id,
                    "hypergraph.attempt.number": event.attempt_number,
                    "hypergraph.attempt.outcome": event.outcome,
                    "hypergraph.attempt.settlement": event.settlement,
                    "hypergraph.attempt.deadline_scope": event.deadline_scope,
                    "hypergraph.attempt.deadline_elapsed": event.deadline_elapsed,
                    "hypergraph.attempt.cancellation_requested": event.cancellation_requested,
                    "hypergraph.attempt.duration_ms": event.duration_ms,
                    "hypergraph.attempt.error_type": event.error_type,
                    "hypergraph.attempt.retry_scheduled": event.retry_scheduled,
                    "hypergraph.attempt.retry_not_before": (event.retry_not_before.isoformat() if event.retry_not_before else None),
                    "hypergraph.node_name": event.node_name,
                }
            ),
        )

    def on_node_end(self, event: NodeEndEvent) -> None:
        self._detach_ambient(event.span_id)
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        self._collapse_candidates.discard(event.span_id)
        if span is None:
            return
        absorbed_run = event.span_id in self._absorbed_run
        self._release_aliases(event.span_id)
        _set_attrs(
            span,
            {
                "hypergraph.duration_ms": event.duration_ms,
                "hypergraph.cached": event.cached,
                "hypergraph.superstep": event.superstep,
                **self._trace_payload_attrs("output", getattr(event, "trace_outputs", None)),
            },
        )
        if self._set_success_status and (not absorbed_run or self._nested_outcome.get(event.span_id) == "completed"):
            span.set_status(self._Status(self._StatusCode.OK))
        self._nested_outcome.pop(event.span_id, None)
        self._logical_ids.pop(event.span_id, None)
        span.end()

    # -- Failure projection ---------------------------------------------------
    #
    # Two shapes travel on every failure event: ``error`` is the privacy-safe
    # projection durable Hypergraph records store, and ``error_detail`` is the
    # real exception (message, type, traceback).  By DEFAULT the export
    # carries the real detail, because the OTel ecosystem norm
    # (``Span.record_exception``) does and a scrubbed trace cannot be
    # debugged.  ``redact_errors=True`` falls back to the safe projection for
    # deployments where exception text may carry regulated data.

    def _status_description(self, event: Any) -> str | None:
        detail = None if self._redact_errors else getattr(event, "error_detail", None)
        if detail is None or not detail.message:
            return event.error or None
        return f"{detail.type_name}: {detail.message}"

    def _exception_attrs(self, event: Any) -> dict[str, Any]:
        # ``exception.type`` never depends on the flag: only message text and
        # the stacktrace are redacted.
        error_type = getattr(event, "error_type", None) or None
        detail = None if self._redact_errors else getattr(event, "error_detail", None)
        if detail is None:
            return {"exception.type": error_type, "exception.message": event.error}
        return {
            "exception.type": error_type or detail.type_name,
            "exception.message": detail.message,
            "exception.stacktrace": detail.traceback,
            "exception.escaped": False,
        }

    def on_node_error(self, event: NodeErrorEvent) -> None:
        # Only the terminal escaping failure reaches this handler and marks
        # the logical node span as error; intermediate attempts never do.
        self._detach_ambient(event.span_id)
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        self._collapse_candidates.discard(event.span_id)
        if span is None:
            return
        self._release_aliases(event.span_id)
        self._nested_outcome.pop(event.span_id, None)
        self._logical_ids.pop(event.span_id, None)
        _set_attrs(
            span,
            {
                "hypergraph.error_type": event.error_type,
                "hypergraph.superstep": event.superstep,
                "hypergraph.diagnostic.code": (event.diagnostic.code if event.diagnostic is not None else None),
                "hypergraph.diagnostic.docs_ref": (event.diagnostic.docs_ref if event.diagnostic is not None else None),
            },
        )
        span.set_status(self._Status(self._StatusCode.ERROR, self._status_description(event)))
        span.add_event("exception", attributes=_clean_attrs(self._exception_attrs(event)))
        span.end()

    def on_superstep_start(self, event: SuperstepStartEvent) -> None:
        span = self._span_for(event.parent_span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.superstep.start",
            attributes=_clean_attrs(
                {
                    "hypergraph.superstep": event.superstep,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        target_span = self._span_for(event.node_span_id) or self._span_for(event.parent_span_id)  # type: ignore[arg-type]
        if target_span is None:
            return
        decision = event.decision if isinstance(event.decision, str) else ",".join(event.decision)
        target_span.add_event(
            "hypergraph.route.decision",
            attributes=_clean_attrs(
                {
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.decision": decision,
                    "hypergraph.superstep": event.superstep,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )

    def on_cache_hit(self, event: CacheHitEvent) -> None:
        span = self._span_for(event.span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.cache.hit",
            attributes=_clean_attrs(
                {
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.superstep": event.superstep,
                }
            ),
        )

    def on_interrupt(self, event: InterruptEvent) -> None:
        span = self._span_for(event.span_id) or self._span_for(event.parent_span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.pause",
            attributes=_clean_attrs(
                {
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.response_param": event.response_param,
                    "hypergraph.superstep": event.superstep,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )
        # InterruptEvent should normally reuse the paused node span id.
        # If a fallback run span id slips through, don't end the parent span early.
        if event.span_id in self._spans and event.span_id != event.parent_span_id:
            # An async pause raises out of the node's task without a node
            # end/error event, so this event arrives in the run's context —
            # the node's context copy died with its task (drop the token).
            self._detach_ambient_if_current(event.span_id)
            paused_span = self._spans.pop(event.span_id)
            self._collapse_candidates.discard(event.span_id)
            self._release_aliases(event.span_id)
            self._nested_outcome.pop(event.span_id, None)
            self._logical_ids.pop(event.span_id, None)
            self._contexts.pop(event.span_id, None)
            paused_span.end()

    def on_stop_requested(self, event: StopRequestedEvent) -> None:
        span = self._span_for(event.span_id) or self._span_for(event.parent_span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.stop.requested",
            attributes=_clean_attrs(
                {
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )

    def shutdown(self) -> None:
        """End any remaining spans while preserving bounded lineage history."""
        # Leftover tokens exist only on abnormal exits (e.g. BaseException
        # escaping the run template). Detach newest-first, and only where this
        # execution unit owns the attach — cross-context leftovers died with
        # their task's context copy and are dropped.
        self._aliases.clear()
        self._absorbed_run.clear()
        for span_id in reversed(list(self._tokens)):
            self._detach_ambient_if_current(span_id)
        self._tokens.clear()
        for span in self._spans.values():
            span.end()
        self._spans.clear()
        self._contexts.clear()
        self._collapse_candidates.clear()
        self._nested_outcome.clear()
        self._logical_ids.clear()
