"""Retention: which step rows a store keeps, and the carrier that replaces the rest.

Two layers live here, and only the first is backend-neutral:

- :func:`plan_retention` is THE policy. Given a run's step rows in execution
  order it answers "keep these, drop those, and date the carrier at this
  superstep" — and nothing else. Memory and SQLite both call it, so a
  ``retention="latest"`` run cannot mean one thing in a test and another in
  production.
- Everything below :data:`RETENTION_ROW_COLS` is the SQL-backed half: the
  narrow row a compaction pass reads, and the statements that carry it out.
  Both checkpointer halves (async and sync) drive the same statement stream,
  so a delete that one half performs cannot be one the other half forgets.

The carrier itself (``__retained_state__``) is a state-carrying record, not a
node execution: it holds the folded values of the rows that were dropped, and
:func:`hypergraph.checkpointers.types.fold_producers` decides whose completion
it may claim.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Generic, Literal, Protocol, TypeVar

from hypergraph.checkpointers._rows import (
    decode_folded_producers,
    encode_folded_producers,
    named_row,
    parse_dt,
    placeholders,
)
from hypergraph.checkpointers.types import StepStatus, fold_producers

#: The carrier's node name and type. Sentinels rather than a flag column
#: because every backend, every reader and every public-step filter has to
#: agree on what a carrier looks like; a second spelling would make one of
#: them silently treat a carrier as an ordinary node execution.
BASELINE_NODE_NAME = "__retained_state__"
BASELINE_NODE_TYPE = "RetentionBaseline"

Retention = Literal["full", "latest", "windowed"]


class RetainableRecord(Protocol):
    """What :func:`plan_retention` needs of a row to decide its fate."""

    @property
    def node_name(self) -> str: ...

    @property
    def superstep(self) -> int: ...


RecordT = TypeVar("RecordT", bound=RetainableRecord)


@dataclass(frozen=True)
class RetentionPlan(Generic[RecordT]):
    """One compaction pass, decided but not yet performed."""

    kept_rows: tuple[RecordT, ...]
    dropped_rows: tuple[RecordT, ...]
    baseline_superstep: int


def plan_retention(
    rows: Sequence[RecordT],
    retention: Retention,
    window: int | None,
) -> RetentionPlan[RecordT] | None:
    """Decide what this run keeps, given its rows in execution order.

    ``None`` means "nothing to do": ``retention="full"``, a window that has
    not been filled yet, or a run with no ordinary steps to keep.

    Args:
        rows: The run's step rows, oldest first. Carrier rows may be present;
            they are never kept as themselves — a new carrier folds them.
        retention: The configured policy.
        window: How many trailing supersteps ``"windowed"`` keeps.

    Returns:
        The plan, or ``None`` when this pass would change nothing.

    Note:
        Fates are decided by POSITION, never by row equality: two rows of one
        run can compare equal without being the same occurrence.
    """
    if retention == "latest":
        latest_index_by_node: dict[str, int] = {}
        for index, row in enumerate(rows):
            if row.node_name != BASELINE_NODE_NAME:
                latest_index_by_node[row.node_name] = index
        kept_indices = set(latest_index_by_node.values())
        kept_rows = tuple(rows[index] for index in latest_index_by_node.values())
        dropped_rows = tuple(row for index, row in enumerate(rows) if index not in kept_indices)
        return RetentionPlan(
            kept_rows=kept_rows,
            dropped_rows=dropped_rows,
            baseline_superstep=min((row.superstep for row in kept_rows), default=0) - 1,
        )

    if retention == "windowed" and window is not None:
        ordinary_rows = tuple(row for row in rows if row.node_name != BASELINE_NODE_NAME)
        if not ordinary_rows:
            return None
        cutoff = max(row.superstep for row in ordinary_rows) - window + 1
        if cutoff <= 0:
            return None
        return RetentionPlan(
            kept_rows=tuple(row for row in ordinary_rows if row.superstep >= cutoff),
            dropped_rows=tuple(row for row in rows if row.node_name == BASELINE_NODE_NAME or row.superstep < cutoff),
            baseline_superstep=cutoff - 1,
        )

    return None


# === The SQL-backed half ===

#: Compaction reads status and folded_producers too: the carrier's provenance
#: (#277) is derived from which folded rows COMPLETED, and a previous carrier
#: contributes the producers it already recorded.
RETENTION_ROW_COLS = "id, step_index, superstep, node_name, values_data, created_at, completed_at, attempt_series_id, status, folded_producers"
DELETE_BATCH_SIZE = 500
#: Two binds per row plus the run id — stays under the 999-variable floor.
PENDING_DELETE_BATCH_SIZE = 400
_RETENTION_ROW_NAMES = tuple(name.strip() for name in RETENTION_ROW_COLS.split(","))


@dataclass(frozen=True, slots=True)
class RetentionRow:
    """One step row, in the narrow shape a compaction pass reads."""

    id: int
    step_index: int
    superstep: int
    node_name: str
    values_data: bytes | None
    created_at: str | None
    completed_at: str | None
    attempt_series_id: str | None
    status: StepStatus
    folded_producers: tuple[str, ...] | None


def decode_retention_rows(rows: Sequence[Sequence[Any]]) -> tuple[RetentionRow, ...]:
    """Decode ``RETENTION_ROW_COLS`` rows into :class:`RetentionRow`."""
    decoded: list[RetentionRow] = []
    for row in rows:
        values = named_row(_RETENTION_ROW_NAMES, row, cols="RETENTION_ROW_COLS")
        decoded.append(
            RetentionRow(
                id=int(values["id"]),
                step_index=int(values["step_index"]),
                superstep=int(values["superstep"]),
                node_name=str(values["node_name"]),
                values_data=values["values_data"],
                created_at=values["created_at"],
                completed_at=values["completed_at"],
                attempt_series_id=values["attempt_series_id"],
                status=StepStatus(values["status"]),
                folded_producers=decode_folded_producers(values["folded_producers"]),
            )
        )
    return tuple(decoded)


def merge_retained_state(serializer: Any, rows: Sequence[RetentionRow]) -> dict[str, Any]:
    """Fold the dropped rows' values the way a state read would."""
    state: dict[str, Any] = {}
    for row in rows:
        if row.values_data is None:
            continue
        values = serializer.deserialize(row.values_data)
        if values:
            state.update(values)
    return state


def baseline_timestamp(
    kept_rows: Sequence[RetentionRow],
    dropped_rows: Sequence[RetentionRow],
) -> datetime:
    """When the carrier claims to have happened.

    Just before the oldest row it precedes, so every time-ordered read folds
    the carrier's values first. With nothing kept there is nothing to precede,
    so it inherits the newest dropped row's time instead.
    """
    if kept_rows:
        kept_times = [parse_dt(row.completed_at) or parse_dt(row.created_at) for row in kept_rows]
        anchor = min(time for time in kept_times if time is not None)
        try:
            return anchor - timedelta(microseconds=1)
        except OverflowError:
            return anchor

    dropped_times = [parse_dt(row.completed_at) or parse_dt(row.created_at) for row in dropped_rows]
    return max((time for time in dropped_times if time is not None), default=datetime.now(timezone.utc))


def baseline_step_params(
    serializer: Any,
    run_id: str,
    *,
    dropped_rows: Sequence[RetentionRow],
    kept_rows: Sequence[RetentionRow],
    baseline_superstep: int,
) -> tuple[Any, ...] | None:
    """The step-upsert params for this pass's carrier, or ``None``.

    ``None`` when the dropped rows carried no values at all: a carrier that
    restores nothing is a row claiming work happened, with no state to show
    for it.
    """
    values = merge_retained_state(serializer, dropped_rows)
    if not values:
        return None

    producers = fold_producers(
        ((row.node_name, row.status, row.folded_producers) for row in dropped_rows),
        carrier_node_name=BASELINE_NODE_NAME,
    )
    baseline_at = baseline_timestamp(kept_rows, dropped_rows).isoformat()
    return (
        run_id,
        baseline_superstep,
        BASELINE_NODE_NAME,
        min(row.step_index for row in dropped_rows),
        StepStatus.COMPLETED.value,
        "{}",
        serializer.serialize(values),
        0.0,
        0,
        None,
        None,
        BASELINE_NODE_TYPE,
        baseline_at,
        baseline_at,
        None,
        0,
        None,
        encode_folded_producers(producers),
    )


def compaction_deletes(run_id: str, dropped_rows: Sequence[RetentionRow]) -> Iterator[tuple[str, list[Any]]]:
    """Every DELETE this pass performs, in order, as ``(sql, params)``.

    One stream for both checkpointer halves, so a table one half prunes can
    never be one the other half leaves behind. Three things follow a dropped
    step: the step row, its pending-node boundary (whose COMMITTED state is
    derived from that step, so keeping it would re-classify settled work as
    pending), and the CLOSED attempt series it was linked to. An OPEN series
    is never pruned — ``closed_at IS NOT NULL`` is the enforcement point.

    Batching is not an optimisation here: SQLite's oldest builds cap a
    statement at 999 bound variables, so an unbatched prune of a long run
    would fail on the host that needed it most.
    """
    step_ids = [row.id for row in dropped_rows]
    for batch in _batched(step_ids, DELETE_BATCH_SIZE):
        yield f"DELETE FROM steps WHERE id IN ({placeholders(batch)})", batch

    # Row-value ``IN`` is avoided for old-sqlite portability.
    for start in range(0, len(dropped_rows), PENDING_DELETE_BATCH_SIZE):
        boundary_batch = dropped_rows[start : start + PENDING_DELETE_BATCH_SIZE]
        predicate = " OR ".join("(superstep = ? AND node_name = ?)" for _ in boundary_batch)
        params: list[Any] = [run_id]
        for row in boundary_batch:
            params.extend((row.superstep, row.node_name))
        yield f"DELETE FROM pending_nodes WHERE run_id = ? AND ({predicate})", params

    series_ids = sorted({row.attempt_series_id for row in dropped_rows if row.attempt_series_id is not None})
    for batch in _batched(series_ids, DELETE_BATCH_SIZE):
        binds = placeholders(batch)
        yield (
            f"DELETE FROM attempt_records WHERE series_id IN ({binds}) AND series_id IN (SELECT id FROM attempt_series WHERE closed_at IS NOT NULL)",
            batch,
        )
        yield f"DELETE FROM attempt_series WHERE id IN ({binds}) AND closed_at IS NOT NULL", batch


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
