"""What a HyperTable write derives, and what its receipt may claim.

This module plans writes; it does not shape rows (``_row_builder``) and does
not touch the store (``_commit``). What is left is the decision sequence: read
the stored row, classify what this write has to do to it, drive the runner for
exactly the nodes that cannot be reused, and report the physical truth of what
happened.

Two rules hold everywhere below:

- a write plan yields graph execution effects and nothing else, so a runner
  (sync, async, or a test double) can drive it;
- ``WriteOutcome`` is a claim about derivation, so ``SKIPPED`` is only ever
  reported by a path that derived no row and did not re-run a fan-out boundary
  to repair one. Re-stamping an unchanged child row at a newer generation, and
  retiring the row it replaces, is bookkeeping: it writes, but it derives
  nothing, and a plan that does only that still reports ``SKIPPED`` — even
  when reaching that conclusion required running the graph, as it does for a
  boundary that also produces a stored parent column.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Any

from hypergraph import Graph
from hypergraph.materialization._commit import (
    ChildGenerations,
    ChildWrites,
    TableCommitter,
    dedup_child_rows,
    dedup_rows,
)
from hypergraph.materialization._provenance import (
    DerivedChildren,
    Provenance,
    RebuildChildren,
    ReconcileComplete,
    ReconcileResult,
    ReconcileUnavailable,
    RunRoutedGraph,
    normalize_value,
    split_boundary_provenance,
)
from hypergraph.materialization._recipe_journal import RecipeJournal
from hypergraph.materialization._row_builder import RowBuilder
from hypergraph.materialization._schema import (
    PROVENANCE_PREFIX,
    RECIPE_COLUMN,
    TableSpec,
    input_names,
    is_internal_column,
)
from hypergraph.materialization._types import (
    ChangeReason,
    ColumnChange,
    RowReceipt,
    RowStatus,
    TableReceipt,
    WriteOutcome,
    deserialize_question,
)
from hypergraph.materialization._write_actions import RunGraph, RunOperations, WriteOperation, _Predicate
from hypergraph.runners import PauseInfo, RunStatus

__all__ = [
    "WritePlanner",
    "normalize_to_dict",
]


def normalize_to_dict(item: Any) -> dict[str, Any]:
    """Convert a mapped child item to a plain dict if it is not one already."""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="python")
    if hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(item)
    return dict(item)


# ---------------------------------------------------------------------------
# What one write has to do to one stored row
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkipWrite:
    """The stored row already answers this write; derive nothing.

    ``refresh_stamps`` marks the one write this arm may still make: a row
    stored before the table stamped recipes needs its stamp, which changes no
    value the reader can see.
    """

    row: dict[str, Any]
    refresh_stamps: bool = False


@dataclass(frozen=True, slots=True)
class ResumeAnswers:
    """Only answers arrived: run the interrupt slice, reusing stored columns."""

    row: dict[str, Any]
    answers: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReconcileColumns:
    """A stored row can be converged column by column, reusing what is fresh.

    Reconciliation can still turn out to be impossible once the planner walks
    the columns (a stored value the value chain cannot account for), in which
    case this arm falls through to ``FullDerive``.
    """

    row: dict[str, Any]
    parent_skipped: bool


@dataclass(frozen=True, slots=True)
class FullDerive:
    """Run the whole graph: there is no stored row worth reusing."""

    row: dict[str, Any] | None
    parent_skipped: bool


WriteClass = SkipWrite | ResumeAnswers | ReconcileColumns | FullDerive


@dataclass(frozen=True)
class _PausedConvergence:
    pause: PauseInfo
    outputs: dict[str, Any]
    provenances: dict[str, str]
    provenance: str


@dataclass(frozen=True)
class _Degradation:
    """What a failed degradable run left behind.

    ``values`` are the outputs the other nodes still produced; ``failures``
    maps each node the runner could blame to its error text. An empty
    ``failures`` means the run failed with nothing to attribute (a plan-level
    error), and the caller must fall back to a total-loss error row."""

    values: dict[str, Any]
    failures: dict[str, str]
    error: BaseException | None


@dataclass(frozen=True)
class _DegradedConvergence:
    """Column reconciliation stopped at a node that failed.

    ``outputs`` and ``provenances`` cover every column reconciliation settled
    BEFORE the failure — reused-fresh ones included. Columns after it were
    never reached, so the caller keeps whatever the stored row already had
    for them."""

    outputs: dict[str, Any]
    provenances: dict[str, str]
    failures: dict[str, str]
    error: BaseException | None


_ConvergenceResult = ReconcileResult | _PausedConvergence | _DegradedConvergence | ReconcileUnavailable


def _run_values(result: Any) -> dict[str, Any]:
    if hasattr(result, "values") and isinstance(result.values, dict):
        return result.values
    if isinstance(result, dict):
        return result
    return {}


def _run_pause(result: Any) -> PauseInfo | None:
    if getattr(result, "paused", False):
        return getattr(result, "pause", None)
    return None


def _as_stored(value: Any, arrow_type: Any) -> Any:
    """``value`` as a typed store reads it back, so a round trip is not a change.

    A typed store writes through the column's arrow type (``list[float]`` is
    float32), so a stored vector reads back rounded. Casting both sides the
    same way compares what a reader sees; a value the declared type cannot
    hold is compared as it is.
    """
    import pyarrow as pa

    value = normalize_value(value)
    if value is None or arrow_type is None:
        return value
    try:
        return pa.array([value], type=arrow_type).to_pylist()[0]
    except (pa.ArrowException, TypeError, ValueError):
        return value


def _pause_provenance(provenances: Mapping[str, str], pause: PauseInfo, *, routed: bool = False) -> str:
    """The provenance stamp an interrupt answer will be stored under.

    A waiting row is only re-openable if the answer carries the provenance of
    the inputs the question was asked from, so a missing stamp is a structural
    error, not a value this plan may invent.
    """
    provenance = provenances.get(pause.response_key) if pause.response_key is not None else None
    if provenance is None:
        raise RuntimeError(
            f"HyperTable could not compute provenance for {'a routed' if routed else 'an'} interrupt answer.\n\n"
            f"Answer column: {pause.response_key!r}\n\n"
            "How to fix: ensure every required interrupt input is a stored source or derived column."
        )
    return provenance


class WritePlanner:
    """Own physical row convergence and yield only graph execution effects."""

    def __init__(
        self,
        graph: Any,
        store: Any,
        spec: TableSpec,
        identity: str,
        components: Mapping[str, Any],
        on_error: str,
        provenance: Provenance,
        *,
        page_max_concurrency: int = 16,
    ):
        self._graph = graph
        self._spec = spec
        self._identity = identity
        self._components = dict(components)
        self._on_error = on_error
        self._provenance = provenance
        self._page_max_concurrency = page_max_concurrency
        self._commit = TableCommitter(store, spec, identity)
        self._rows = RowBuilder(self._commit, spec, identity, provenance)

    @property
    def journal(self) -> RecipeJournal:
        return self._rows.journal

    # -- inputs --------------------------------------------------------------

    def _graph_inputs(self, item: Mapping[str, Any], provided: set[str] | None = None) -> dict[str, Any]:
        required = input_names(self._graph.inputs.required)
        answers = {column.name for column in self._spec.columns if column.role == "answer"}
        accepted_answers = answers if provided is None else answers & provided
        accepted = required | accepted_answers
        return {key: value for key, value in item.items() if key != self._identity and key in accepted}

    def _answer_inputs(
        self,
        graph: Graph,
        item: Mapping[str, Any],
        existing: Mapping[str, Any],
        answer_names: set[str],
    ) -> dict[str, Any]:
        values = self._provenance.stored_values(existing)
        values.update(item)
        accepted = input_names(graph.inputs.required) | answer_names
        return {name: values[name] for name in accepted if name in values}

    def _source_inputs(self, item: Mapping[str, Any]) -> dict[str, Any]:
        sources = {column.name for column in self._spec.columns if column.role == "source"}
        return {key: value for key, value in item.items() if key in sources}

    @staticmethod
    def _executed_nodes(result: Any) -> set[str]:
        log = getattr(result, "log", None)
        return {step.node_name for step in getattr(log, "steps", ())}

    # -- classification ------------------------------------------------------

    def classify_write(self, item: Mapping[str, Any], provided_names: set[str]) -> WriteClass:
        """Decide what this write must do to the row it is about to touch.

        Pure apart from the single stored-row read: given the stored row it
        names one of four arms, so the write plan is a dispatch instead of a
        ladder of overlapping booleans.
        """
        existing = self._commit.read_one(self._spec.name, self._identity, item[self._identity])
        fingerprint = self._provenance.root_fingerprint(self._source_inputs(item))
        answer_names = {column.name for column in self._spec.columns if column.role == "answer"}
        provided_answers = answer_names & provided_names
        source_names = {column.name for column in self._spec.columns if column.role == "source"}
        source_provided = bool(source_names & provided_names)
        if existing is None:
            return FullDerive(None, parent_skipped=False)

        status = RowStatus.of_stored(existing)
        unchanged = existing.get("_row_fingerprint") == fingerprint
        if unchanged and status is RowStatus.WAITING and not provided_answers:
            return SkipWrite(existing)
        # An unchanged, complete parent is not re-derived. It is still not a
        # skip when it has children: they may be damaged, and only the reconcile
        # arm can tell (#204, #314).
        parent_skipped = unchanged and status is RowStatus.COMPLETE and not provided_answers
        if parent_skipped and not self._spec.children:
            return SkipWrite(existing, refresh_stamps=self._provenance.row_missing_stamp(existing, RECIPE_COLUMN))
        if provided_answers and not source_provided:
            return ResumeAnswers(existing, frozenset(provided_answers))
        if status is not RowStatus.ERROR:
            return ReconcileColumns(existing, parent_skipped)
        return FullDerive(existing, parent_skipped)

    # -- receipts ------------------------------------------------------------

    @staticmethod
    def _receipt_for_row(identity_value: Any, outcome: WriteOutcome, row: Mapping[str, Any]) -> RowReceipt:
        status = RowStatus.of_stored(row)
        if status is RowStatus.WAITING:
            pause, _provenance = deserialize_question(row["_question"])
            return RowReceipt(str(identity_value), outcome, status, pause=pause)
        if status in (RowStatus.ERROR, RowStatus.PARTIAL):
            return RowReceipt(str(identity_value), outcome, status, error=str(row.get("_error") or ""))
        return RowReceipt(str(identity_value), outcome, status)

    def _unchanged_parent_receipt(self, identity_value: Any, before: ChildWrites, *, rebuilt: bool = False) -> RowReceipt:
        """The receipt for a row whose parent this plan did not re-derive.

        ``SKIPPED`` claims this pass derived nothing that survived: no child
        row was derived, and no fan-out boundary was re-run to repair one.
        Re-stamping the unchanged child rows at a newer generation, and
        retiring the rows they replace, is bookkeeping and keeps the skip —
        and so does running the graph only to arrive there, which is what a
        boundary that also produces a stored parent column forces.

        Repair work is the other case — either the fan-out boundary re-ran to
        rebuild the child item list (``rebuilt``), or a child row was derived.
        A repair is ``HEALED`` when everything it derived landed healthy, and
        plain ``UPDATED`` when a child is still stored in error: a heal that
        did not heal is not a heal (#204, #314).
        """
        repair = self._commit.child_writes.since(before)
        if not repair.derived and not rebuilt:
            return RowReceipt(str(identity_value), WriteOutcome.SKIPPED, RowStatus.COMPLETE)
        outcome = WriteOutcome.UPDATED if repair.errored else WriteOutcome.HEALED
        return RowReceipt(str(identity_value), outcome, RowStatus.COMPLETE)

    # -- column reconciliation -----------------------------------------------

    def _reconcile(
        self,
        item: dict[str, Any],
        existing: dict[str, Any],
        spec: TableSpec | None = None,
        provided: set[str] | None = None,
    ) -> Generator[RunGraph, Any, _ConvergenceResult]:
        target = spec or self._spec
        target_graph = target.child_graph if spec is not None else self._graph
        if target_graph is None:
            return ReconcileUnavailable()
        # Only the parent table stores partial rows; a child row is whole or errored (#314).
        degrade = self._degrade() and spec is None
        boundary_counts: dict[str, int] = {}
        for child_spec in target.children:
            rows = self._commit.read_rows(
                child_spec.name,
                (("_parent_id", "eq", item[target.identity]),),
            )
            boundary_counts[child_spec.name] = len(dedup_child_rows(rows, child_spec.identity))
        provided_names = provided if provided is not None else set(item) - {target.identity}
        incoming = {key: value for key, value in item.items() if key in provided_names and key != target.identity}
        state = self._provenance.start_reconcile(target, existing, incoming, boundary_counts, graph=target_graph)
        while True:
            state, step = self._provenance.next_reconcile_step(state)
            if isinstance(step, ReconcileUnavailable):
                return step
            if isinstance(step, ReconcileComplete):
                return step.result

            if isinstance(step, RunRoutedGraph):
                result = yield RunGraph(step.graph, step.input_values())
                run_outputs = _run_values(result)
                executed = self._executed_nodes(result)
                pause = _run_pause(result)
                if pause is not None:
                    provenances = dict(state.provenances)
                    provenances.update(self._rows.provenances_for_values({**dict(state.values), **run_outputs}, pause, executed, target))
                    return _PausedConvergence(
                        pause=pause,
                        outputs={**dict(state.outputs), **run_outputs},
                        provenances=provenances,
                        provenance=_pause_provenance(provenances, pause, routed=True),
                    )
                state = self._provenance.apply_routed_result(state, step, run_outputs, executed)
                continue

            result = yield RunGraph(
                self._provenance.column_graph(step.node),
                step.input_values(),
                degrade=degrade,
            )
            if degrade:
                degradation = self._run_degradation(result)
                if degradation is not None:
                    return _DegradedConvergence(
                        outputs=dict(state.outputs),
                        provenances=dict(state.provenances),
                        failures=degradation.failures,
                        error=degradation.error,
                    )
            pause = _run_pause(result)
            if pause is not None:
                provenances = dict(state.provenances)
                provenances[pause.response_key] = step.provenance
                return _PausedConvergence(
                    pause=pause,
                    outputs=dict(state.outputs),
                    provenances=provenances,
                    provenance=step.provenance,
                )
            outputs = _run_values(result)
            state = self._provenance.apply_reconcile_result(state, step, outputs)

    # -- children ------------------------------------------------------------

    @staticmethod
    def _child_items(outputs: Mapping[str, Any], child_spec: TableSpec) -> list[Any] | None:
        if not child_spec.child_graph or child_spec.map_input is None:
            return None
        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return None
        return child_items

    def _insert_child_item(
        self,
        parent_id: Any,
        raw_item: Any,
        child_spec: TableSpec,
        bound_graph: Graph,
        write_gen: int,
    ) -> Generator[RunGraph, Any, None]:
        child_item = normalize_to_dict(raw_item)
        child_identity = child_item.get(child_spec.identity, "")
        child_inputs = {
            column.name: child_item[column.name]
            for column in child_spec.columns
            if column.role == "source" and column.content_key and column.name in child_item
        }
        fingerprint = self._provenance.child_fingerprint(child_inputs, child_spec)
        existing_rows = self._commit.read_rows(
            child_spec.name,
            (
                ("_parent_id", "eq", parent_id),
                (child_spec.identity, "eq", child_identity),
            ),
        )
        existing = max(existing_rows, key=lambda row: row.get("_write_gen", 0)) if existing_rows else None
        stored_status = RowStatus.of_stored(existing)
        if existing is not None and existing.get("_row_fingerprint") == fingerprint and stored_status is RowStatus.COMPLETE:
            if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                bumped = self._rows.stamp_existing_row(
                    child_spec.name,
                    existing,
                    write_gen,
                    child_spec,
                    normalize_values=False,
                )
            else:
                bumped = dict(existing)
                bumped["_write_gen"] = write_gen
            self._commit.restamp_child(child_spec.name, bumped)
            return

        def error_row(error: BaseException) -> dict[str, Any]:
            return self._rows.child_row(
                child_spec,
                child_item,
                child_identity,
                parent_id,
                fingerprint,
                write_gen,
                status=RowStatus.ERROR,
                error=f"{type(error).__name__}: {error}",
            )

        row: dict[str, Any] | None = None
        if existing is not None and stored_status is RowStatus.COMPLETE:
            try:
                reconciled = yield from self._reconcile(child_item, existing, child_spec)
            except Exception as error:
                if self._on_error == "raise":
                    raise
                self._commit.write_derived_child(child_spec.name, error_row(error), errored=True)
                return
            if isinstance(reconciled, ReconcileResult):
                row = self._rows.child_row(
                    child_spec,
                    child_item,
                    child_identity,
                    parent_id,
                    fingerprint,
                    write_gen,
                    status=RowStatus.COMPLETE,
                    error=None,
                    outputs=reconciled.output_values(),
                    provenances=reconciled.provenance_values(),
                )

        if row is None:
            try:
                child_outputs = _run_values((yield RunGraph(bound_graph, child_inputs)))
            except Exception as error:
                if self._on_error == "raise":
                    raise
                self._commit.write_derived_child(child_spec.name, error_row(error), errored=True)
                return
            row = self._rows.child_row(
                child_spec,
                child_item,
                child_identity,
                parent_id,
                fingerprint,
                write_gen,
                status=RowStatus.COMPLETE,
                error=None,
                outputs=child_outputs,
                provenances=self._rows.child_provenances(child_spec, {**child_item, **child_outputs}),
            )
        self._commit.write_derived_child(child_spec.name, row, errored=False)

    def _insert_children_items(
        self,
        parent_id: Any,
        child_items: list[Any],
        child_spec: TableSpec,
        child_gens: ChildGenerations,
    ) -> WriteOperation:
        if child_spec.child_graph is None:
            return
        # Allocated before any write in this batch: strictly greater than every
        # physical row currently in the child table, so cleanup (which deletes
        # older generations) removes every stale row and no tie can survive.
        write_gen = child_gens.for_table(child_spec.name)
        bound_graph = self._provenance.bind_child_components(child_spec.child_graph)
        operations = tuple(self._insert_child_item(parent_id, item, child_spec, bound_graph, write_gen) for item in child_items)
        if operations:
            yield RunOperations(operations, self._page_max_concurrency)

    def _insert_children(
        self,
        parent_id: Any,
        outputs: Mapping[str, Any],
        child_spec: TableSpec,
        child_gens: ChildGenerations,
    ) -> Generator[RunGraph, Any, None]:
        child_items = self._child_items(outputs, child_spec)
        if child_items is not None:
            yield from self._insert_children_items(parent_id, child_items, child_spec, child_gens)

    def _parent_stamps_stale(self, existing: Mapping[str, Any], provenances: Mapping[str, str | None]) -> bool:
        """Whether an unchanged parent must be rewritten to carry this pass's stamps.

        True when a provenance stamp moved — a column's, or a fan-out
        boundary's ``<provenance>#<count>`` — or when the stored row predates
        the recipe stamp. Both repair paths for an unchanged parent (the
        column-scoped reconcile and the whole-graph derive) ask this one
        question, so a stale recorded count is corrected by whichever runs.
        """
        provenance_changed = any(existing.get(f"{PROVENANCE_PREFIX}{name}") != provenance for name, provenance in provenances.items())
        return provenance_changed or self._provenance.row_missing_stamp(existing, RECIPE_COLUMN)

    def _parent_rewrite_derives(self, existing: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
        """Whether rewriting an unchanged parent as ``row`` changes what a reader sees (R13).

        It does when a fan-out boundary's stamp moved — the run derived a
        different item list than the one recorded — or when a derived column's
        value differs from the stored one, as a node whose output varies
        between runs can make it. Either is a rebuild, reported the way the
        plain shape reports one. A rewrite that only moves recipe or column
        stamps over identical values is bookkeeping and keeps the skip.
        """
        boundary_moved = any(
            existing.get(f"{PROVENANCE_PREFIX}{spec.map_input}") != row.get(f"{PROVENANCE_PREFIX}{spec.map_input}")
            for spec in self._spec.children
            if spec.map_input
        )
        return boundary_moved or any(
            _as_stored(existing.get(column.name), column.arrow_type) != _as_stored(row.get(column.name), column.arrow_type)
            for column in self._provenance.derived_columns()
        )

    def _apply_reconciled(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        existing: dict[str, Any],
        reconciled: ReconcileResult,
        parent_skipped: bool,
        write_gen: int,
        child_gens: ChildGenerations,
    ) -> Generator[RunGraph, Any, None]:
        outputs = reconciled.output_values()
        provenances = reconciled.provenance_values()
        identity_value = item[self._identity]
        for selection in reconciled.children:
            if isinstance(selection, RebuildChildren):
                rows = self._commit.read_rows(selection.spec.name, (("_parent_id", "eq", identity_value),))
                child_items = self._rows.rebuild_child_items(rows, selection.spec)
            elif isinstance(selection, DerivedChildren):
                child_items = list(selection.items)
            else:
                raise TypeError(f"unsupported child selection: {type(selection).__name__}")
            yield from self._insert_children_items(
                identity_value,
                child_items,
                selection.spec,
                child_gens,
            )
        if not parent_skipped or self._parent_stamps_stale(existing, provenances):
            self._rows.evolve_for_metadata(item)
            row = self._rows.parent_row(
                item,
                source_inputs,
                outputs,
                write_gen,
                RowStatus.COMPLETE,
                provenances=provenances,
            )
            self._commit.write_rows(self._spec.name, [row])
            self._commit.cleanup_parent(identity_value, write_gen)
        self._commit.cleanup_children(identity_value, child_gens)

    # -- parent rows ---------------------------------------------------------

    def _write_waiting_parent(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        outputs: Mapping[str, Any],
        provenances: Mapping[str, str],
        pause: PauseInfo,
        pause_provenance: str,
        write_gen: int,
        child_gens: ChildGenerations,
        existing: dict[str, Any] | None,
        outcome: WriteOutcome,
    ) -> RowReceipt:
        identity_value = item[self._identity]
        self._rows.evolve_for_metadata(item)
        row = self._rows.parent_row(
            item,
            source_inputs,
            outputs,
            write_gen,
            RowStatus.WAITING,
            provenances=provenances,
            pause=pause,
            pause_provenance=pause_provenance,
        )
        self._commit.write_rows(self._spec.name, [row])
        if existing is not None:
            self._commit.cleanup_parent(identity_value, write_gen)
            self._commit.cleanup_children(identity_value, child_gens)
        return RowReceipt(str(identity_value), outcome, RowStatus.WAITING, pause=pause)

    def _degrade(self) -> bool:
        """Whether a node failure should keep the columns that succeeded."""
        return self._on_error == "store"

    @staticmethod
    def _run_degradation(result: Any) -> _Degradation | None:
        """Read a degradable run's outcome; ``None`` when it did not fail."""
        if getattr(result, "status", None) is not RunStatus.FAILED:
            return None
        failures: dict[str, str] = {}
        first: BaseException | None = None
        for failure in getattr(result, "node_failures", ()):
            failures.setdefault(failure.node_name, f"{type(failure.error).__name__}: {failure.error}")
            first = first if first is not None else failure.error
        return _Degradation(
            values=_run_values(result),
            failures=failures,
            error=first if first is not None else getattr(result, "error", None),
        )

    @staticmethod
    def _blamed(producer: Any, failures: Mapping[str, str]) -> str | None:
        """The failure key blaming this producer, if one does.

        A failure raised inside a mounted graph is reported under the path of
        the node that owns it (``embed_stage/embed``), so a GraphNode column
        producer is blamed by its own prefix too."""
        name = getattr(producer, "name", None)
        if name is None:
            return None
        for failed_name in failures:
            if failed_name == name or failed_name.startswith(f"{name}/"):
                return failed_name
        return None

    def _downstream_of(self, failures: Mapping[str, str]) -> set[str]:
        """Node names the failed nodes can reach — the ones whose inputs the
        failure genuinely destroyed.

        A failure inside a mounted graph is reported under its path
        (``embed_stage/embed``); the graph knows only the node that owns it."""
        roots = {name.split("/", 1)[0] for name in failures} & set(self._graph.nodes)
        return self._provenance.node_names_downstream(roots) if roots else set()

    def _partial_columns(
        self,
        values: Mapping[str, Any],
        failures: Mapping[str, str],
        kept: Mapping[str, Any],
        downstream: set[str],
    ) -> tuple[dict[str, Any], tuple[ColumnChange, ...]]:
        """Split this table's derived columns into what survives a failed run and
        what it nulls, with one change entry per nulled column and the true
        reason it holds no value.

        A column the run produced is kept as it came — a node that ran and
        returned ``None`` returned a value, not a failure, so it gets no entry.
        A column the run never reached — its node comes after the failure and is
        not the one that failed — keeps whatever ``kept`` already stored for it,
        so a second failure never costs a column the first one saved.

        A failed fan-out boundary has no stored parent column to null — the
        parent keeps only its stamp — so it gets one ``NODE_ERROR`` entry naming
        the ``map_over`` input its child table could not be rebuilt from, however
        many child tables map over that input."""
        outputs: dict[str, Any] = {}
        changes: list[ColumnChange] = []
        for column in self._provenance.derived_columns():
            producers = self._provenance.column_producers(column)
            blamed = next((name for name in (self._blamed(producer, failures) for producer in producers) if name is not None), None)
            if column.name in values:
                outputs[column.name] = values[column.name]
            elif blamed is not None:
                changes.append(ColumnChange(column.name, ChangeReason.NODE_ERROR, blamed, failures[blamed]))
            elif not self._provenance.column_is_null(kept.get(column.name)):
                outputs[column.name] = kept[column.name]
            else:
                node = getattr(producers[0], "name", column.name)
                reason = (
                    ChangeReason.UPSTREAM_ERROR
                    if any(getattr(producer, "name", None) in downstream for producer in producers)
                    else ChangeReason.NOT_RUN
                )
                changes.append(ColumnChange(column.name, reason, node))
        named_fan_outs: set[str] = set()
        for child_spec in self._spec.children:
            blamed = self._blamed(self._provenance.boundary_node(child_spec), failures)
            if blamed is not None and child_spec.map_input and child_spec.map_input not in named_fan_outs:
                named_fan_outs.add(child_spec.map_input)
                changes.append(ColumnChange(child_spec.map_input, ChangeReason.NODE_ERROR, blamed, failures[blamed]))
        return outputs, tuple(changes)

    def _degraded_parent(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        write_gen: int,
        existing: dict[str, Any] | None,
        outcome: WriteOutcome,
        degradation: _Degradation,
        *,
        provenances: Mapping[str, str] | None = None,
        kept: Mapping[str, Any] | None = None,
    ) -> RowReceipt:
        """Store what a failed run still derived, as one PARTIAL row.

        A partial row names every node of this graph that failed with a
        ``NODE_ERROR`` entry: its column-scoped heal re-runs the nodes behind its
        entries, so a failure no entry names would never be retried. It falls
        back to the total-loss error row whenever that claim cannot hold: a
        failure the runner could not attribute to a node, one that left no
        derived column standing, or a failed top-level node of this graph that
        owns no stored column and is not a fan-out boundary — no entry can name
        it, and a column-scoped heal would never run it again. (A failure inside
        a mounted graph is covered by that graph's own entries: the heal re-runs
        it whole.) A failed fan-out boundary
        is named by its ``map_over`` input, so it stays PARTIAL."""
        kept_values = self._provenance.stored_values(kept) if kept is not None else {}
        outputs, changes = self._partial_columns(
            degradation.values,
            degradation.failures,
            kept_values,
            self._downstream_of(degradation.failures),
        )
        error = next(iter(degradation.failures.values()), None) or f"{type(degradation.error).__name__}: {degradation.error}"
        # Compared by the node that owns each failure in this graph: a mounted
        # graph whose inner nodes both raised is named once (``stage/a``) and
        # re-runs whole, so ``stage/b`` is covered too.
        failed = {name.split("/", 1)[0] for name in degradation.failures}
        named = {change.node.split("/", 1)[0] for change in changes if change.reason is ChangeReason.NODE_ERROR}
        if not outputs or not failed or not failed <= named:
            failure = degradation.error if degradation.error is not None else RuntimeError(error)
            self._error_parent(item, source_inputs, write_gen, failure, existing)
            return RowReceipt(str(item[self._identity]), outcome, RowStatus.ERROR, error=error)
        stamps = dict(provenances) if provenances is not None else self._rows.provenances_for_values({**item, **outputs})
        if kept is not None:
            for name in outputs:
                if name not in stamps and kept.get(f"_provenance_{name}") is not None:
                    stamps[name] = kept[f"_provenance_{name}"]
        self._rows.evolve_for_metadata(item)
        row = self._rows.parent_row(
            item,
            source_inputs,
            outputs,
            write_gen,
            RowStatus.PARTIAL,
            provenances={name: stamp for name, stamp in stamps.items() if stamp is not None and name in outputs},
            error=error,
            changes=changes,
        )
        self._commit.write_rows(self._spec.name, [row])
        if existing is not None:
            self._commit.cleanup_parent(item[self._identity], write_gen)
        return RowReceipt(str(item[self._identity]), outcome, RowStatus.PARTIAL, error=error)

    def _error_parent(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        write_gen: int,
        error: BaseException,
        existing: dict[str, Any] | None,
    ) -> None:
        self._rows.evolve_for_metadata(item)
        row = self._rows.parent_row(
            item,
            source_inputs,
            {},
            write_gen,
            RowStatus.ERROR,
            error=f"{type(error).__name__}: {error}",
        )
        self._commit.write_rows(self._spec.name, [row])
        if existing is not None:
            self._commit.cleanup_parent(item[self._identity], write_gen)

    def _resume_answer(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        existing: dict[str, Any],
        answer_names: set[str],
        write_gen: int,
        child_gens: ChildGenerations,
    ) -> Generator[RunGraph, Any, RowReceipt]:
        identity_value = item[self._identity]
        graph = self._provenance.answer_graph(answer_names)
        try:
            result = yield RunGraph(
                graph,
                self._answer_inputs(graph, item, existing, answer_names),
            )
        except Exception as error:
            if self._on_error == "raise":
                raise
            self._error_parent(item, source_inputs, write_gen, error, existing)
            return RowReceipt(
                str(identity_value),
                WriteOutcome.UPDATED,
                RowStatus.ERROR,
                error=f"{type(error).__name__}: {error}",
            )

        outputs = {
            column.name: normalize_value(existing[column.name])
            for column in self._provenance.derived_columns()
            if column.name in existing and not self._provenance.column_is_null(existing[column.name])
        }
        outputs.update(_run_values(result))
        pause = _run_pause(result)
        provenances = self._rows.provenances_for_values({**item, **outputs}, pause)
        if pause is not None:
            return self._write_waiting_parent(
                item,
                source_inputs,
                outputs,
                provenances,
                pause,
                _pause_provenance(provenances, pause),
                write_gen,
                child_gens,
                existing,
                WriteOutcome.UPDATED,
            )

        for child_spec in self._spec.children:
            yield from self._insert_children(identity_value, outputs, child_spec, child_gens)
        self._rows.evolve_for_metadata(item)
        row = self._rows.parent_row(
            item,
            source_inputs,
            outputs,
            write_gen,
            RowStatus.COMPLETE,
            provenances=provenances,
        )
        self._commit.write_rows(self._spec.name, [row])
        self._commit.cleanup_parent(identity_value, write_gen)
        self._commit.cleanup_children(identity_value, child_gens)
        return RowReceipt(str(identity_value), WriteOutcome.UPDATED, RowStatus.COMPLETE)

    # -- the four arms -------------------------------------------------------

    def _insert_one(
        self,
        item: dict[str, Any],
        write_gen: int,
        provided: set[str] | None = None,
    ) -> Generator[RunGraph, Any, RowReceipt]:
        identity_value = item[self._identity]
        child_gens = self._commit.child_generations(write_gen)
        provided_names = provided if provided is not None else set(item) - {self._identity}
        source_inputs = self._source_inputs(item)
        plan = self.classify_write(item, provided_names)
        outcome = WriteOutcome.UPDATED if plan.row is not None else WriteOutcome.INSERTED
        before = self._commit.child_writes

        if isinstance(plan, SkipWrite):
            if plan.refresh_stamps:
                self._commit.refresh_missing_stamps(plan.row, self._rows, self._provenance)
            return self._receipt_for_row(identity_value, WriteOutcome.SKIPPED, plan.row)

        if isinstance(plan, ResumeAnswers):
            return (
                yield from self._resume_answer(
                    item,
                    source_inputs,
                    plan.row,
                    set(plan.answers),
                    write_gen,
                    child_gens,
                )
            )

        if isinstance(plan, ReconcileColumns):
            receipt = yield from self._reconciled_parent(item, source_inputs, provided_names, plan, outcome, write_gen, child_gens, before)
            if receipt is not None:
                return receipt

        return (
            yield from self._derived_parent(
                item,
                source_inputs,
                provided_names,
                plan.row,
                plan.parent_skipped,
                outcome,
                write_gen,
                child_gens,
                before,
            )
        )

    def _reconciled_parent(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        provided_names: set[str],
        plan: ReconcileColumns,
        outcome: WriteOutcome,
        write_gen: int,
        child_gens: ChildGenerations,
        before: ChildWrites,
    ) -> Generator[RunGraph, Any, RowReceipt | None]:
        """Converge a stored row column by column.

        Returns ``None`` when the stored values cannot support column-scoped
        reconciliation, which is the caller's signal to derive the whole graph.
        """
        identity_value = item[self._identity]
        existing = plan.row
        try:
            reconciled = yield from self._reconcile(item, existing, provided=provided_names)
        except Exception as error:
            if self._on_error == "raise":
                raise
            if plan.parent_skipped:
                return self._unchanged_parent_receipt(identity_value, before)
            self._error_parent(item, source_inputs, write_gen, error, existing)
            return RowReceipt(str(identity_value), outcome, RowStatus.ERROR, error=f"{type(error).__name__}: {error}")

        if isinstance(reconciled, _PausedConvergence):
            return self._write_waiting_parent(
                item,
                source_inputs,
                reconciled.outputs,
                reconciled.provenances,
                reconciled.pause,
                reconciled.provenance,
                write_gen,
                child_gens,
                existing,
                outcome,
            )
        if isinstance(reconciled, _DegradedConvergence):
            if plan.parent_skipped:
                return self._unchanged_parent_receipt(identity_value, before)
            return self._degraded_parent(
                item,
                source_inputs,
                write_gen,
                existing,
                outcome,
                _Degradation(reconciled.outputs, reconciled.failures, reconciled.error),
                provenances=reconciled.provenances,
                kept=existing,
            )
        if isinstance(reconciled, ReconcileUnavailable):
            return None
        yield from self._apply_reconciled(
            item,
            source_inputs,
            existing,
            reconciled,
            plan.parent_skipped,
            write_gen,
            child_gens,
        )
        if plan.parent_skipped:
            # A DerivedChildren selection means the fan-out boundary re-ran to
            # regenerate the item list, so this pass DID derive — even when
            # every child row it then wrote was an unchanged re-stamp and the
            # only other effect was retiring a stale row (an extra child row
            # left behind by an interrupted write).
            rebuilt = any(isinstance(selection, DerivedChildren) for selection in reconciled.children)
            return self._unchanged_parent_receipt(identity_value, before, rebuilt=rebuilt)
        return RowReceipt(str(identity_value), outcome, RowStatus.COMPLETE)

    def _derived_parent(
        self,
        item: dict[str, Any],
        source_inputs: dict[str, Any],
        provided_names: set[str],
        existing: dict[str, Any] | None,
        parent_skipped: bool,
        outcome: WriteOutcome,
        write_gen: int,
        child_gens: ChildGenerations,
        before: ChildWrites,
    ) -> Generator[RunGraph, Any, RowReceipt]:
        """Run the whole graph for one row and store what it produced."""
        identity_value = item[self._identity]
        try:
            result = yield RunGraph(self._graph, self._graph_inputs(item, provided_names), degrade=self._degrade())
        except Exception as error:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                return self._unchanged_parent_receipt(identity_value, before)
            self._error_parent(item, source_inputs, write_gen, error, existing)
            return RowReceipt(str(identity_value), outcome, RowStatus.ERROR, error=f"{type(error).__name__}: {error}")

        degradation = self._run_degradation(result)
        if degradation is not None:
            if parent_skipped:
                return self._unchanged_parent_receipt(identity_value, before)
            return self._degraded_parent(item, source_inputs, write_gen, existing, outcome, degradation)

        outputs = _run_values(result)
        pause = _run_pause(result)
        if pause is not None:
            provenances = self._rows.provenances_for_values({**item, **outputs}, pause)
            return self._write_waiting_parent(
                item,
                source_inputs,
                outputs,
                provenances,
                pause,
                _pause_provenance(provenances, pause),
                write_gen,
                child_gens,
                existing,
                outcome,
            )

        for child_spec in self._spec.children:
            yield from self._insert_children(identity_value, outputs, child_spec, child_gens)
        if parent_skipped:
            # The boundary re-ran, so its recorded count may have moved (a
            # stale or legacy stamp, or an item list that changed length).
            # Leaving it would send every later sync() back through the graph.
            row = self._rows.parent_row(item, source_inputs, outputs, write_gen, RowStatus.COMPLETE)
            stamps = {key.removeprefix(PROVENANCE_PREFIX): value for key, value in row.items() if key.startswith(PROVENANCE_PREFIX)}
            rebuilt = False
            if existing is not None and self._parent_stamps_stale(existing, stamps):
                self._rows.evolve_for_metadata(item)
                self._commit.write_rows(self._spec.name, [row])
                self._commit.cleanup_parent(identity_value, write_gen)
                rebuilt = self._parent_rewrite_derives(existing, row)
            self._commit.cleanup_children(identity_value, child_gens)
            return self._unchanged_parent_receipt(identity_value, before, rebuilt=rebuilt)

        self._rows.evolve_for_metadata(item)
        row = self._rows.parent_row(item, source_inputs, outputs, write_gen, RowStatus.COMPLETE)
        self._commit.write_rows(self._spec.name, [row])
        if existing is not None:
            self._commit.cleanup_parent(identity_value, write_gen)
            self._commit.cleanup_children(identity_value, child_gens)
        return RowReceipt(str(identity_value), outcome, RowStatus.COMPLETE)

    # -- public write plans --------------------------------------------------

    def insert(self, items: list[dict[str, Any]]) -> WriteOperation:
        write_gen = self._commit.next_write_gen()
        receipts: list[RowReceipt] = []
        for item in items:
            receipts.append((yield from self._insert_one(item, write_gen)))
        return TableReceipt(tuple(receipts))

    def _prepare_update(
        self,
        identity_value: str,
        changes: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool, int]:
        existing = self._commit.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            raise KeyError(identity_value)
        item: dict[str, Any] = {self._identity: identity_value}
        for column in self._spec.columns:
            if column.role in ("source", "answer") and column.name in existing and not self._provenance.column_is_null(existing[column.name]):
                item[column.name] = normalize_value(existing[column.name])
        spec_columns = {column.name for column in self._spec.columns}
        for key, value in existing.items():
            if key not in spec_columns and not is_internal_column(key):
                item[key] = normalize_value(value)
        item.update(changes)
        derivation_inputs = {column.name for column in self._spec.columns if column.role in ("source", "answer")}
        needs_rederive = any(key in derivation_inputs for key in changes)
        write_gen = self._commit.next_write_gen()
        return item, existing, needs_rederive, write_gen

    def update(self, identity_value: str, changes: dict[str, Any]) -> WriteOperation:
        item, existing, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if not needs_rederive:
            # Metadata-only: no derivation, but a row IS written, so the receipt
            # reports the physical effect rather than claiming a skip (#248).
            self._rows.evolve_for_metadata({self._identity: identity_value, **changes})
            row = {key: normalize_value(value) for key, value in existing.items()}
            row.update(changes)
            row["_write_gen"] = write_gen
            self._commit.write_rows(self._spec.name, [row])
            self._commit.cleanup_parent(identity_value, write_gen)
            return self._receipt_for_row(identity_value, WriteOutcome.UPDATED, existing)

        return (yield from self._insert_one(item, write_gen, provided=set(changes)))

    def delete(self, identity_value: str) -> None:
        if self._commit.read_one(self._spec.name, self._identity, identity_value) is None:
            return
        self._commit.delete_row_and_children(identity_value)

    def _row_unchanged(self, item: dict[str, Any], existing: dict[str, Any]) -> bool:
        inputs = self._source_inputs(item)
        return existing.get("_row_fingerprint") == self._provenance.root_fingerprint(inputs)

    def _children_need_repair(self, existing: dict[str, Any]) -> bool:
        """Read-only damage probe for the unchanged-parent sync fast path (#204, #314).

        A child is damaged when it is physically absent or when it is stored as
        an error row: both are repaired by re-running only that child. The probe
        compares each child table's recorded fan-out count (the
        ``<provenance>#<count>`` value stamped on the parent row) against the
        physically present deduplicated child rows, and reads their ``_status``
        so a stored failure is not counted as a healthy child (#314). One
        ``read_rows`` per child table per parent row; never writes.

        A child spec whose boundary also produces a stored parent column is
        probed like any other — the probe is read-only either way. When it
        finds damage, the ordinary write plan reaches the same
        ``ReconcileUnavailable`` -> ``_derived_parent`` repair ``insert()``
        already takes for that shape (``Provenance.next_reconcile_step``), so
        both verbs give one damage one answer (#468). A child spec with no
        boundary node is skipped: no ``<provenance>#<count>`` stamp is written
        for it, so there is no recorded count to compare against.
        """
        identity_value = existing[self._identity]
        for child_spec in self._spec.children:
            if child_spec.child_graph is None or not child_spec.map_input:
                continue
            if self._provenance.boundary_node(child_spec) is None:
                continue
            _, expected = split_boundary_provenance(existing.get(f"_provenance_{child_spec.map_input}"))
            if expected is None:
                continue
            rows = self._commit.read_rows(
                child_spec.name,
                (("_parent_id", "eq", identity_value),),
                columns=(child_spec.identity, "_parent_id", "_write_gen", "_status"),
            )
            present = dedup_child_rows(rows, child_spec.identity)
            if len(present) != expected or any(RowStatus.of_stored(row) is RowStatus.ERROR for row in present):
                return True
        return False

    def sync(self, items: list[dict[str, Any]]) -> WriteOperation:
        rows = self._commit.read_rows(self._spec.name)
        existing_by_id = {str(row[self._identity]): row for row in dedup_rows(rows, self._identity) if row.get(self._identity) is not None}
        incoming_ids: set[str] = set()
        receipts: list[RowReceipt] = []
        write_gen = self._commit.next_write_gen()

        for item in items:
            identity_value = str(item[self._identity])
            incoming_ids.add(identity_value)
            existing = existing_by_id.get(identity_value)
            if existing is None:
                receipts.append((yield from self._insert_one(item, write_gen)))
            elif self._row_unchanged(item, existing) and RowStatus.of_stored(existing) is RowStatus.COMPLETE:
                if self._provenance.row_missing_stamp(existing, RECIPE_COLUMN):
                    self._commit.refresh_missing_stamps(existing, self._rows, self._provenance)
                if self._children_need_repair(existing):
                    # The ordinary write plan does the repair, and reports it:
                    # its receipt is HEALED when the rebuilt children landed
                    # healthy, never SKIPPED on a path that wrote rows.
                    receipts.append((yield from self._insert_one(item, write_gen)))
                else:
                    receipts.append(RowReceipt(identity_value, WriteOutcome.SKIPPED, RowStatus.COMPLETE))
            else:
                if self._row_unchanged(item, existing):
                    receipts.append((yield from self._insert_one(item, write_gen)))
                else:
                    changes = {key: value for key, value in item.items() if key != self._identity}
                    receipts.append((yield from self.update(identity_value, changes)))

        deleted = 0
        for identity_value in existing_by_id:
            if identity_value not in incoming_ids:
                self.delete(identity_value)
                deleted += 1
        return TableReceipt(tuple(receipts), deleted=deleted)

    def set_rows(self, where: _Predicate, fields: dict[str, Any]) -> int:
        content_keys = {column.name for column in self._spec.columns if column.content_key}
        blocked = sorted(content_keys.intersection(fields))
        if blocked:
            raise ValueError(
                "HyperTable.set() cannot update content-key fields.\n\n"
                f"Fields: {', '.join(blocked)}\n\n"
                "How to fix: update annotation metadata only; converge content changes with update()."
            )
        rows = dedup_rows(self._commit.read_rows(self._spec.name, where), self._identity)
        if not rows:
            return 0
        self._rows.evolve_for_metadata({self._identity: rows[0][self._identity], **fields})
        write_gen = self._commit.next_write_gen()
        updated = []
        for row in rows:
            new_row = {key: normalize_value(value) for key, value in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated.append(new_row)
        self._commit.write_rows(self._spec.name, updated)
        for row in rows:
            self._commit.cleanup_parent(row[self._identity], write_gen)
        return len(updated)

    def derive_column(self, column: str, *, backfill: bool) -> WriteOperation:
        if backfill:
            self._rows.evolve_for_backfill_column(column)
        node = self._provenance.producing_node(column)
        write_gen = self._commit.next_write_gen()
        rows = dedup_rows(self._commit.read_rows(self._spec.name), self._identity)
        receipts: list[RowReceipt] = []
        derived_columns = self._provenance.derived_columns()
        derived_names = {derived.name for derived in derived_columns}
        for existing in rows:
            child_gens = self._commit.child_generations(write_gen)
            if backfill and not self._provenance.column_is_null(existing.get(column)):
                receipts.append(
                    self._receipt_for_row(
                        existing[self._identity],
                        WriteOutcome.SKIPPED,
                        existing,
                    )
                )
                continue
            values = self._provenance.stored_values(existing)
            item = {name: value for name, value in values.items() if name == self._identity or name not in derived_names}
            source_inputs = self._source_inputs(item)
            stale_target_row = {key: normalize_value(value) for key, value in existing.items()}
            for target_column in self._provenance.node_columns(node):
                stale_target_row[f"_provenance_{target_column.name}"] = None
            try:
                reconciled = yield from self._reconcile(item, stale_target_row)
            except Exception as error:
                if self._on_error == "raise":
                    raise
                self._error_parent(item, source_inputs, write_gen, error, existing)
                receipts.append(
                    RowReceipt(
                        str(existing[self._identity]),
                        WriteOutcome.UPDATED,
                        RowStatus.ERROR,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
                continue
            if isinstance(reconciled, _DegradedConvergence):
                receipts.append(
                    self._degraded_parent(
                        item,
                        source_inputs,
                        write_gen,
                        existing,
                        WriteOutcome.UPDATED,
                        _Degradation(reconciled.outputs, reconciled.failures, reconciled.error),
                        provenances=reconciled.provenances,
                        kept=existing,
                    )
                )
                continue
            if isinstance(reconciled, _PausedConvergence):
                receipts.append(
                    self._write_waiting_parent(
                        item,
                        source_inputs,
                        reconciled.outputs,
                        reconciled.provenances,
                        reconciled.pause,
                        reconciled.provenance,
                        write_gen,
                        child_gens,
                        existing,
                        WriteOutcome.UPDATED,
                    )
                )
                continue
            if isinstance(reconciled, ReconcileUnavailable):
                raise RuntimeError(
                    "HyperTable could not converge a re-derived column.\n\n"
                    f"Column: {column!r}\n\n"
                    "How to fix: run insert() or sync() with the row's source columns so the full graph can derive it."
                )
            yield from self._apply_reconciled(
                item,
                source_inputs,
                existing,
                reconciled,
                False,
                write_gen,
                child_gens,
            )
            receipts.append(RowReceipt(str(existing[self._identity]), WriteOutcome.UPDATED, RowStatus.COMPLETE))
        return TableReceipt(tuple(receipts))
