"""Attempt-ledger census policy: what the retry question asks, minus how it reads.

A node that retried BELOW the graph leaves its evidence in
``attempt_series``/``attempt_records`` and nowhere in the Run's status, so a
Run that looks merely slow reads exactly like one that has been failing and
retrying all along. The checkpointer's own attempt API answers one series at
a time (``get_attempt_series``, ``get_attempt_records``), which is the wrong
shape for an operator surface: a column on a sweep of a thousand Runs would
cost a thousand questions. The census asks ONE question per id window
instead.

Everything here is pure, exactly like ``_batch_store``. A function takes the
request the caller made, or the rows it already fetched, and returns a value
or raises; nothing touches a connection. ``RunHome`` still owns the read the
statement runs inside, so the sync and async mirrors there differ only in how
they fetch a row — the one difference that genuinely cannot be shared.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: The highest attempt each series reached, folded to the run that owns it.
#:
#: An INNER join on purpose. A Run appears only when a node of it was
#: attempt-managed — a declared retry policy or timeout — so an ABSENT Run
#: never opened a series, and a Run reported as ``1`` was managed and
#: succeeded on its first invocation. Reporting every Run as having tried
#: once would erase exactly that difference.
CENSUS_SELECT = "SELECT s.run_id, MAX(a.attempt_number) FROM attempt_series s JOIN attempt_records a ON a.series_id = s.id"

#: Narrowing by Definition joins the submission that pinned it. A nested
#: run has no submission of its own, so a Definition-narrowed census covers
#: Host Runs only — which is what naming a Definition asks for.
DEFINITION_JOIN = " JOIN host_submissions h ON h.workflow_id = s.run_id"


def validate_census_request(run_ids: Sequence[str] | None, definition: str | None) -> None:
    """Refuse a census the ledger cannot answer, before it reads."""
    if definition is not None and not isinstance(definition, str):
        raise TypeError(f"retry_census() definition must be a Definition name string or None, got {type(definition).__name__}.")
    if run_ids is None:
        return
    if isinstance(run_ids, str) or not isinstance(run_ids, Sequence):
        raise TypeError(f"retry_census() run_ids must be a sequence of run id strings or None, got {type(run_ids).__name__}.")
    for run_id in run_ids:
        if not isinstance(run_id, str):
            raise TypeError(f"retry_census() run_ids must contain run id strings, got {type(run_id).__name__}.")


def census_query(run_ids: Sequence[str] | None, definition: str | None) -> tuple[str, list[Any]]:
    """The census statement for one id window, and its binds.

    Stated once so the sync and async mirrors can never select different
    columns or narrow on different terms — a census that disagreed with
    itself across the two doors would be worse than no census.
    """
    conditions = []
    params: list[Any] = []
    join = ""
    if definition is not None:
        join = DEFINITION_JOIN
        conditions.append("h.definition_name = ?")
        params.append(definition)
    if run_ids is not None:
        conditions.append(f"s.run_id IN ({', '.join('?' for _ in run_ids)})")
        params.extend(run_ids)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"{CENSUS_SELECT}{join}{where} GROUP BY s.run_id", params


def folded_census(rows: Sequence[Any]) -> dict[str, int]:
    """One number per Run: the highest attempt its ledger recorded."""
    return {str(run_id): int(attempts) for run_id, attempts in rows}
