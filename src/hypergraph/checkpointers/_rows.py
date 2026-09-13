"""The codec between SQL rows and checkpointer records.

Rows are decoded BY NAME, against the same column lists the SELECTs
interpolate. That is the whole point of this module: a row and the record it
becomes agree because they are read from one list, not because a human kept
two orderings in step. Widening a table is therefore a one-line change here
plus a migration — and a list that drifts from its decoder fails on the first
row with :func:`named_row`'s message instead of silently shifting every field by
one.

Nothing here touches a connection. Given a row it returns a record; given a
record it returns the parameter tuple a statement binds. Both checkpointer
halves — async and sync — call exactly these functions, so a value one half
stores is the value the other half reads back.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from hypergraph.checkpointers.types import (
    AttemptError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    NodeBoundary,
    PauseSlot,
    PendingNode,
    Run,
    StepRecord,
    StepStatus,
    WorkflowStatus,
    derive_boundary_state,
)

# Explicit column lists for SELECT queries — avoids column-order bugs after migration
RUNS_COLS = (
    "id, graph_name, status, duration_ms, node_count, error_count, "
    "created_at, completed_at, parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config"
)
STEPS_COLS = (
    "id, run_id, step_index, superstep, node_name, node_type, status, duration_ms, cached, error, decision, "
    "input_versions, values_data, child_run_id, created_at, completed_at, partial, attempt_series_id, folded_producers"
)
PAUSE_SLOT_COLS = "pause_id, run_id, superstep, node_name, node_path, response_key, question, answer_schema, options, created_at, settled_at, answer"
ATTEMPT_SERIES_COLS = "id, run_id, node_name, policy_fingerprint, max_attempts, opened_at, deadline_at, committed_superstep, closed_at"
ATTEMPT_RECORD_COLS = (
    "series_id, attempt_number, scheduled_superstep, status, started_at, completed_at, error_type, error_message, "
    "retry_not_before, sampled_delay, deadline_elapsed, cancellation_requested"
)
#: The intent-joined-journal row behind a NodeBoundary: the pending_nodes row
#: plus the outer-joined step status. Not a table's column list, so it is
#: spelled here rather than derived from one.
NODE_BOUNDARY_COLS = "run_id, superstep, node_name, node_type, created_at, dispatched_at, step_status"


def _names(columns: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in columns.split(","))


_RUNS_NAMES = _names(RUNS_COLS)
_STEPS_NAMES = _names(STEPS_COLS)
_PAUSE_SLOT_NAMES = _names(PAUSE_SLOT_COLS)
_ATTEMPT_SERIES_NAMES = _names(ATTEMPT_SERIES_COLS)
_ATTEMPT_RECORD_NAMES = _names(ATTEMPT_RECORD_COLS)
_NODE_BOUNDARY_NAMES = _names(NODE_BOUNDARY_COLS)


def named_row(names: tuple[str, ...], row: Sequence[Any], *, cols: str) -> dict[str, Any]:
    """Pair a row with its column names, refusing a row of the wrong width.

    Raises:
        RuntimeError: The row has a different number of columns than ``cols``
            names. Every SELECT that feeds a decoder here interpolates that
            same constant, so a mismatch means the constant and the decoder
            were changed apart — a silent field shift if it were tolerated.
    """
    if len(row) != len(names):
        raise RuntimeError(
            f"A checkpointer row has {len(row)} column(s) but {cols} names {len(names)}: {', '.join(names)}.\n\n"
            f"Every SELECT feeding this decoder interpolates {cols}, so the row and the decoder "
            "can only disagree if the column list was changed without the decoder (or a caller "
            "passed a row it built by hand).\n\n"
            "How to fix:\n"
            f"  SELECT exactly {cols}; when adding a column, add it to {cols} AND to the decoder "
            "in hypergraph/checkpointers/_rows.py."
        )
    return dict(zip(names, row, strict=True))


def placeholders(items: Sequence[Any]) -> str:
    """``"?,?,?"`` for a bind list — one spelling, so every ``IN`` matches."""
    return ",".join("?" * len(items))


def parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO datetime string, normalising the UTC 'Z' suffix.

    ``datetime.fromisoformat`` only accepts 'Z' on Python 3.11+; SQLite always
    emits Z-suffixed timestamps, so we normalise to '+00:00' for 3.10 compat.
    """
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def encode_folded_producers(producers: tuple[str, ...] | None) -> str | None:
    """Store a carrier's producer provenance (#277) as a JSON name list.

    ``None`` stays SQL NULL, which is what every ordinary step row and every
    carrier written before the column existed reads back as.
    """
    return None if producers is None else json.dumps(list(producers))


def decode_folded_producers(raw: str | None) -> tuple[str, ...] | None:
    """Read back what :func:`encode_folded_producers` stored."""
    if raw is None:
        return None
    return tuple(json.loads(raw))


# === Rows to records ===


def row_to_run(row: Sequence[Any]) -> Run:
    """Build a :class:`Run` from a ``RUNS_COLS`` row."""
    values = named_row(_RUNS_NAMES, row, cols="RUNS_COLS")
    config_raw = values["config"]
    return Run(
        id=values["id"],
        graph_name=values["graph_name"] or None,
        status=WorkflowStatus(values["status"]),
        duration_ms=values["duration_ms"],
        node_count=values["node_count"] or 0,
        error_count=values["error_count"] or 0,
        created_at=parse_dt(values["created_at"]),  # type: ignore[arg-type]
        completed_at=parse_dt(values["completed_at"]),
        parent_run_id=values["parent_run_id"],
        forked_from=values["forked_from"],
        fork_superstep=values["fork_superstep"],
        retry_of=values["retry_of"],
        retry_index=values["retry_index"],
        config=json.loads(config_raw) if config_raw else None,
    )


def row_to_step(serializer: Any, row: Sequence[Any]) -> StepRecord:
    """Build a :class:`StepRecord` from a ``STEPS_COLS`` row."""
    values = named_row(_STEPS_NAMES, row, cols="STEPS_COLS")
    values_blob = values["values_data"]
    decision_raw = values["decision"]
    partial = values["partial"]
    return StepRecord(
        run_id=values["run_id"],
        superstep=values["superstep"],
        node_name=values["node_name"],
        index=values["step_index"],
        status=StepStatus(values["status"]),
        input_versions=json.loads(values["input_versions"]) if values["input_versions"] else {},
        values=serializer.deserialize(values_blob) if values_blob is not None else None,
        duration_ms=values["duration_ms"],
        cached=bool(values["cached"]),
        decision=json.loads(decision_raw) if decision_raw else None,
        error=values["error"],
        node_type=values["node_type"],
        created_at=parse_dt(values["created_at"]),  # type: ignore[arg-type]
        completed_at=parse_dt(values["completed_at"]),
        child_run_id=values["child_run_id"],
        partial=bool(partial) if partial is not None else False,
        attempt_series_id=values["attempt_series_id"],
        folded_producers=decode_folded_producers(values["folded_producers"]),
    )


def row_to_pause_slot(row: Sequence[Any]) -> PauseSlot:
    """Build a :class:`PauseSlot` from a ``PAUSE_SLOT_COLS`` row."""
    values = named_row(_PAUSE_SLOT_NAMES, row, cols="PAUSE_SLOT_COLS")
    options = json.loads(values["options"]) if values["options"] is not None else None
    settled_at = parse_dt(values["settled_at"])
    created_at = parse_dt(values["created_at"])
    answer = values["answer"]
    return PauseSlot(
        run_id=values["run_id"],
        superstep=int(values["superstep"]),
        node_name=values["node_name"],
        node_path=values["node_path"],
        response_key=values["response_key"],
        question=json.loads(values["question"]),
        answer_schema=json.loads(values["answer_schema"]),
        options=None if options is None else tuple(options),
        created_at=created_at if created_at is not None else datetime.now(timezone.utc),
        settled_at=settled_at,
        # The answer column is only meaningful once settled; an unsettled row
        # must never present a decoded value.
        answer=json.loads(answer) if settled_at is not None and answer is not None else None,
    )


def row_to_node_boundary(row: Sequence[Any]) -> NodeBoundary:
    """Build a :class:`NodeBoundary` from the intent-joined-journal row.

    This module only shapes the row; the state cascade itself lives in
    :func:`derive_boundary_state` so both backends cannot drift.
    """
    values = named_row(_NODE_BOUNDARY_NAMES, row, cols="NODE_BOUNDARY_COLS")
    dispatched_at = parse_dt(values["dispatched_at"])
    raw_status = values["step_status"]
    step_status = StepStatus(raw_status) if raw_status is not None else None
    return NodeBoundary(
        run_id=values["run_id"],
        superstep=values["superstep"],
        node_name=values["node_name"],
        state=derive_boundary_state(step_status, dispatched_at),
        node_type=values["node_type"],
        created_at=parse_dt(values["created_at"]),
        dispatched_at=dispatched_at,
        step_status=step_status,
    )


def row_to_attempt_series(row: Sequence[Any]) -> AttemptSeries:
    """Build an :class:`AttemptSeries` from an ``ATTEMPT_SERIES_COLS`` row."""
    values = named_row(_ATTEMPT_SERIES_NAMES, row, cols="ATTEMPT_SERIES_COLS")
    return AttemptSeries(
        id=values["id"],
        run_id=values["run_id"],
        node_name=values["node_name"],
        policy_fingerprint=values["policy_fingerprint"],
        max_attempts=int(values["max_attempts"]),
        opened_at=parse_dt(values["opened_at"]),  # type: ignore[arg-type]
        deadline_at=parse_dt(values["deadline_at"]),
        committed_superstep=values["committed_superstep"],
        closed_at=parse_dt(values["closed_at"]),
    )


def row_to_attempt_record(row: Sequence[Any]) -> AttemptRecord:
    """Build an :class:`AttemptRecord` from an ``ATTEMPT_RECORD_COLS`` row."""
    values = named_row(_ATTEMPT_RECORD_NAMES, row, cols="ATTEMPT_RECORD_COLS")
    error_type = values["error_type"]
    return AttemptRecord(
        series_id=values["series_id"],
        attempt_number=int(values["attempt_number"]),
        scheduled_superstep=int(values["scheduled_superstep"]),
        status=AttemptStatus(values["status"]),
        started_at=parse_dt(values["started_at"]),  # type: ignore[arg-type]
        completed_at=parse_dt(values["completed_at"]),
        error=AttemptError(type_name=error_type, message=values["error_message"] or "") if error_type is not None else None,
        retry_not_before=parse_dt(values["retry_not_before"]),
        sampled_delay=values["sampled_delay"],
        deadline_elapsed=bool(values["deadline_elapsed"]),
        cancellation_requested=bool(values["cancellation_requested"]),
    )


# === Records to statement parameters ===


def step_upsert_params(serializer: Any, record: StepRecord) -> tuple[Any, ...]:
    """Build the parameter tuple for the steps upsert."""
    return (
        record.run_id,
        record.superstep,
        record.node_name,
        record.index,
        record.status.value,
        json.dumps(record.input_versions),
        serializer.serialize(record.values) if record.values is not None else None,
        record.duration_ms,
        int(record.cached),
        json.dumps(record.decision) if record.decision is not None else None,
        record.error,
        record.node_type,
        record.created_at.isoformat(),
        iso_or_none(record.completed_at),
        record.child_run_id,
        int(record.partial),
        record.attempt_series_id,
        encode_folded_producers(record.folded_producers),
    )


def pending_node_params(boundary: PendingNode) -> tuple[Any, ...]:
    return (
        boundary.run_id,
        boundary.superstep,
        boundary.node_name,
        boundary.node_type,
        boundary.created_at.isoformat(),
        iso_or_none(boundary.dispatched_at),
    )


def pause_slot_insert_params(slot: PauseSlot) -> tuple[Any, ...]:
    return (
        slot.pause_id,
        slot.run_id,
        slot.superstep,
        slot.node_name,
        slot.node_path,
        slot.response_key,
        json.dumps(slot.question),
        json.dumps(slot.answer_schema),
        None if slot.options is None else json.dumps(list(slot.options)),
        slot.created_at.isoformat(),
        iso_or_none(slot.settled_at),
        None if slot.settled_at is None else json.dumps(slot.answer),
    )


def attempt_series_insert_params(series: AttemptSeries) -> tuple[Any, ...]:
    return (
        series.id,
        series.run_id,
        series.node_name,
        series.policy_fingerprint,
        series.max_attempts,
        series.opened_at.isoformat(),
        iso_or_none(series.deadline_at),
        series.committed_superstep,
        iso_or_none(series.closed_at),
    )


def attempt_record_insert_params(record: AttemptRecord) -> tuple[Any, ...]:
    return (
        record.series_id,
        record.attempt_number,
        record.scheduled_superstep,
        record.status.value,
        record.started_at.isoformat(),
        iso_or_none(record.completed_at),
        record.error.type_name if record.error else None,
        record.error.message if record.error else None,
        iso_or_none(record.retry_not_before),
        record.sampled_delay,
        int(record.deadline_elapsed),
        int(record.cancellation_requested),
    )


def attempt_outcome_params(
    series_id: str,
    attempt_number: int,
    status: AttemptStatus,
    *,
    now: datetime,
    error: AttemptError | None,
    retry_not_before: datetime | None,
    sampled_delay: float | None,
) -> tuple[Any, ...]:
    """Params for the compare-and-set that records one attempt's outcome."""
    return (
        status.value,
        now.isoformat(),
        error.type_name if error else None,
        error.message if error else None,
        iso_or_none(retry_not_before),
        sampled_delay,
        series_id,
        attempt_number,
    )


def attempt_final_params(
    series_id: str,
    attempt_number: int,
    status: AttemptStatus,
    *,
    now: datetime,
    error: AttemptError | None,
) -> tuple[Any, ...]:
    """Params for the compare-and-set that settles a closing attempt."""
    return (
        status.value,
        now.isoformat(),
        error.type_name if error else None,
        error.message if error else None,
        series_id,
        attempt_number,
    )


def run_upsert(
    run_id: str,
    *,
    graph_name: str | None,
    created_at: datetime,
    parent_run_id: str | None,
    forked_from: str | None,
    fork_superstep: int | None,
    retry_of: str | None,
    retry_index: int | None,
    config: dict[str, Any] | None,
    inputs_blob: Any,
) -> tuple[tuple[Any, ...], Run]:
    """The runs-upsert params and the :class:`Run` the caller returns.

    Both come from here because they are the same facts: what a create writes
    and what it reports must not be two readings of the arguments. The
    statement names every column twice — once to insert, once for the
    ``ON CONFLICT`` branch that resets a re-run — so the params are those
    facts twice, minus the two the reset must not touch (``id`` and
    ``created_at``: a re-run keeps the id it was asked for and the moment the
    run first existed).
    """
    config_json = json.dumps(config) if config is not None else None
    lineage = (parent_run_id, forked_from, fork_superstep, retry_of, retry_index, config_json, inputs_blob)
    params = (
        run_id,
        WorkflowStatus.ACTIVE.value,
        graph_name or "",
        created_at.isoformat(),
        *lineage,
        WorkflowStatus.ACTIVE.value,
        graph_name or "",
        *lineage,
    )
    run = Run(
        id=run_id,
        status=WorkflowStatus.ACTIVE,
        graph_name=graph_name,
        parent_run_id=parent_run_id,
        forked_from=forked_from,
        fork_superstep=fork_superstep,
        retry_of=retry_of,
        retry_index=retry_index,
        config=config,
        created_at=created_at,
    )
    return params, run


# === Run inputs ===


def deserialize_run_inputs(serializer: Any, row: Sequence[Any] | None) -> dict[str, Any]:
    """Decode a stored ``runs.inputs_data`` blob; ``{}`` when absent."""
    if row is None or row[0] is None:
        return {}
    return dict(serializer.deserialize(row[0]) or {})


def serialize_run_inputs(serializer: Any, run_id: str, inputs: dict[str, Any] | None) -> Any:
    """Encode a run's graph-boundary inputs, naming what refused to encode.

    Run inputs became durable so a checkpoint can restore them, which turns
    them into a persisted record rather than a live call argument. The bare
    ``TypeError`` the serializer raises for one ("Object of type Client is
    not JSON serializable") names neither the run, nor the input, nor the
    rule, so a caller cannot tell that the value was refused for being a
    graph INPUT — node outputs have always had to serialize, graph inputs
    did not.

    The failure is re-raised as the same type with the offending names, the
    run they belong to, and the two ways out. Re-encoding key by key to find
    them runs only on the failing path.
    """
    if not inputs:
        return None
    try:
        return serializer.serialize(inputs)
    except TypeError as error:
        raise TypeError(_run_inputs_type_error(serializer, run_id, inputs, error)) from error


def _run_inputs_type_error(serializer: Any, run_id: str, inputs: dict[str, Any], error: TypeError) -> str:
    """The message for graph inputs the checkpointer cannot store."""
    offenders = [f"{name} ({type(value).__name__})" for name, value in inputs.items() if not _encodes(serializer, name, value)]
    return (
        f"Run {run_id!r} cannot start: a checkpointed run stores its graph inputs, and "
        f"{', '.join(offenders) if offenders else str(error)} cannot be stored by this checkpointer's serializer.\n\n"
        "Hypergraph persists a run's graph-boundary inputs so a checkpoint can restore them — a node placed after an "
        "interrupt has no other way to read a raw graph input when the run resumes.\n\n"
        "How to fix:\n"
        "  Pass a storable stand-in as the graph input (an id, a config dict) and build the live object inside a node; or\n"
        "  give the checkpointer a serializer that accepts it, e.g. SqliteCheckpointer(..., serializer=JsonSerializer(lossy=True))."
    )


def _encodes(serializer: Any, name: str, value: Any) -> bool:
    """Whether this one input survives the serializer on its own."""
    try:
        serializer.serialize({name: value})
    except TypeError:
        return False
    return True
