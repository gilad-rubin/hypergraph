"""Immutable action algebra shared by HyperTable write planning and apply."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from hypergraph.materialization._schema import TableSpec

_Items = tuple[tuple[str, Any], ...]
_Rows = tuple[_Items, ...]
_Predicate = tuple[tuple[str, str, Any], ...]


def _freeze(values: Mapping[str, Any]) -> _Items:
    return tuple(values.items())


def _thaw(values: _Items) -> dict[str, Any]:
    return dict(values)


def _freeze_rows(rows: list[dict[str, Any]]) -> _Rows:
    return tuple(_freeze(row) for row in rows)


def _thaw_rows(rows: _Rows) -> list[dict[str, Any]]:
    return [_thaw(row) for row in rows]


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


def dedup_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest write generation for each root identity."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity_value = str(row.get(identity, ""))
        existing = best.get(identity_value)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[identity_value] = row
    return list(best.values())


def dedup_child_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest write generation for each parent/child identity."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("_parent_id", "")), str(row.get(identity, "")))
        existing = best.get(key)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[key] = row
    return list(best.values())


@dataclass(frozen=True, slots=True)
class RunGraph:
    """The only colored action: execute this graph with these inputs."""

    graph: Any
    inputs: _Items

    def input_values(self) -> dict[str, Any]:
        return _thaw(self.inputs)


@dataclass(frozen=True, slots=True)
class ReadOne:
    table: str
    identity: str
    value: Any


@dataclass(frozen=True, slots=True)
class ReadRows:
    table: str
    where: _Predicate | None = None
    limit: int | None = None
    columns: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class MaxWriteGen:
    table: str


@dataclass(frozen=True, slots=True)
class WriteRows:
    table: str
    rows: _Rows

    @classmethod
    def from_rows(cls, table: str, rows: list[dict[str, Any]]) -> WriteRows:
        return cls(table, _freeze_rows(rows))


@dataclass(frozen=True, slots=True)
class DeleteRows:
    table: str
    where: _Predicate


@dataclass(frozen=True, slots=True)
class EvolveMetadata:
    item: _Items
    table: str | None = None
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class BuildParentRow:
    item: _Items
    graph_inputs: _Items
    outputs: _Items
    write_gen: int
    mode: Literal["complete", "update", "error"]
    provenances: _Items | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BuildChildRow:
    spec: TableSpec
    item: _Items
    identity: Any
    parent_id: Any
    fingerprint: str
    write_gen: int
    status: Literal["complete", "error"]
    error: str | None
    outputs: _Items = ()
    provenances: _Items = ()


@dataclass(frozen=True, slots=True)
class StampExistingRow:
    table: str
    row: _Items
    write_gen: int
    child_spec: TableSpec | None = None
    normalize_values: bool = True


@dataclass(frozen=True, slots=True)
class BuildNodeRow:
    existing: _Items
    node: Any
    outputs: _Items
    write_gen: int


@dataclass(frozen=True, slots=True)
class EvolveBackfillColumn:
    column: str


WriteAction = (
    RunGraph
    | ReadOne
    | ReadRows
    | MaxWriteGen
    | WriteRows
    | DeleteRows
    | EvolveMetadata
    | BuildParentRow
    | BuildChildRow
    | StampExistingRow
    | BuildNodeRow
    | EvolveBackfillColumn
)
WriteOperation = Generator[WriteAction, Any, Any]
