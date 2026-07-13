"""Bounded, typed serialization for captured inspect values.

The runtime artifact keeps the original Python objects.  This module is the
single conversion seam from those objects to inert JSON-compatible data for
inspection renderers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, time
from itertools import islice
from typing import Literal, TypeAlias

SerializedKind: TypeAlias = Literal[
    "null",
    "boolean",
    "number",
    "text",
    "exception",
    "sequence",
    "mapping",
    "table",
    "placeholder",
]
SerializedScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = SerializedScalar | list["JSONValue"] | dict[str, "JSONValue"]

_MAX_TEXT_CHARACTERS = 20_000
_MAX_JSON_INTEGER_BITS = int(_MAX_TEXT_CHARACTERS * math.log2(10))
_MAX_MAPPING_ITEMS = 100
_MAX_SEQUENCE_ITEMS = 200
_MAX_TABLE_ROWS = 200
_MAX_TABLE_COLUMNS = 20
_MAX_DEPTH = 6


@dataclass(frozen=True, slots=True)
class SerializedValue:
    """One inert value node in the bounded inspect serialization tree."""

    kind: SerializedKind
    type_name: str
    value: SerializedScalar = None
    text: str | None = None
    entries: tuple[SerializedEntry, ...] = ()
    items: tuple[SerializedValue, ...] = ()
    table: SerializedTable | None = None
    original_size: int | None = None
    truncated: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SerializedEntry:
    """One typed key/value pair in a serialized mapping."""

    key: SerializedValue
    value: SerializedValue


@dataclass(frozen=True, slots=True)
class SerializedTableRow:
    """One row aligned with ``SerializedTable.columns``."""

    cells: tuple[SerializedValue, ...]


@dataclass(frozen=True, slots=True)
class SerializedTable:
    """Bounded tabular data with truthful source dimensions."""

    columns: tuple[SerializedValue, ...]
    rows: tuple[SerializedTableRow, ...]
    original_row_count: int
    original_column_count: int
    rows_truncated: bool
    columns_truncated: bool


def _safe_type_name(value: object) -> str:
    try:
        name = type(value).__name__
        name.encode("utf-8", errors="strict")
    except BaseException:
        return "unknown"
    return name


def _failed_value(
    value: object,
    *,
    operation: str,
    error: BaseException,
    original_size: int | None = None,
) -> SerializedValue:
    return SerializedValue(
        kind="placeholder",
        type_name=_safe_type_name(value),
        original_size=original_size,
        truncated=True,
        reason=f"{operation} failed ({_safe_type_name(error)})",
    )


def _placeholder(
    value: object,
    *,
    reason: str,
    original_size: int | None = None,
) -> SerializedValue:
    return SerializedValue(
        kind="placeholder",
        type_name=_safe_type_name(value),
        original_size=original_size,
        truncated=True,
        reason=reason,
    )


def _serialize_text(text: str, *, type_name: str) -> SerializedValue:
    original_size = len(text)
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return SerializedValue(
            kind="placeholder",
            type_name=type_name,
            original_size=original_size,
            truncated=True,
            reason="invalid Unicode",
        )
    return SerializedValue(
        kind="text",
        type_name=type_name,
        text=text[:_MAX_TEXT_CHARACTERS],
        original_size=original_size,
        truncated=original_size > _MAX_TEXT_CHARACTERS,
    )


def _serialize_row_table(
    value: object,
    *,
    source_rows: tuple[object, ...],
    original_row_count: int,
    depth: int,
    active_ids: set[int],
) -> SerializedValue:
    first_row = source_rows[0]
    assert isinstance(first_row, Mapping)
    try:
        original_column_count = len(first_row)
    except BaseException as error:
        return _failed_value(
            value,
            operation="len",
            error=error,
            original_size=original_row_count,
        )
    try:
        column_keys = tuple(islice(iter(first_row), _MAX_TABLE_COLUMNS))
    except BaseException as error:
        return _failed_value(
            value,
            operation="iteration",
            error=error,
            original_size=original_row_count,
        )

    columns = tuple(
        _serialize_value(
            key,
            depth=depth + 1,
            active_ids=active_ids,
        )
        for key in column_keys
    )
    rows: list[SerializedTableRow] = []
    for source_row in source_rows[:_MAX_TABLE_ROWS]:
        assert isinstance(source_row, Mapping)
        cells: list[SerializedValue] = []
        for key in column_keys:
            try:
                cell = source_row[key]
            except BaseException as error:
                cells.append(
                    _failed_value(
                        source_row,
                        operation="item access",
                        error=error,
                    )
                )
                continue
            cells.append(
                _serialize_value(
                    cell,
                    depth=depth + 1,
                    active_ids=active_ids,
                )
            )
        rows.append(SerializedTableRow(cells=tuple(cells)))

    rows_truncated = original_row_count > _MAX_TABLE_ROWS
    columns_truncated = original_column_count > _MAX_TABLE_COLUMNS
    return SerializedValue(
        kind="table",
        type_name=_safe_type_name(value),
        table=SerializedTable(
            columns=columns,
            rows=tuple(rows),
            original_row_count=original_row_count,
            original_column_count=original_column_count,
            rows_truncated=rows_truncated,
            columns_truncated=columns_truncated,
        ),
        original_size=original_row_count,
        truncated=rows_truncated or columns_truncated,
    )


def _serialize_matrix_table(
    value: object,
    *,
    source_columns: tuple[object, ...],
    source_rows: tuple[object, ...],
    original_row_count: int,
    original_column_count: int,
    depth: int,
    active_ids: set[int],
) -> SerializedValue:
    columns = tuple(
        _serialize_value(
            column,
            depth=depth + 1,
            active_ids=active_ids,
        )
        for column in source_columns
    )
    rows: list[SerializedTableRow] = []
    for source_row in source_rows:
        try:
            cells = tuple(islice(iter(source_row), len(columns)))  # type: ignore[arg-type]
        except BaseException as error:
            return _failed_value(
                value,
                operation="iteration",
                error=error,
                original_size=original_row_count,
            )
        serialized_cells = [
            _serialize_value(
                cell,
                depth=depth + 1,
                active_ids=active_ids,
            )
            for cell in cells
        ]
        while len(serialized_cells) < len(columns):
            serialized_cells.append(
                SerializedValue(
                    kind="placeholder",
                    type_name="missing",
                    truncated=True,
                    reason="missing table cell",
                )
            )
        rows.append(SerializedTableRow(cells=tuple(serialized_cells)))

    rows_truncated = original_row_count > _MAX_TABLE_ROWS
    columns_truncated = original_column_count > _MAX_TABLE_COLUMNS
    return SerializedValue(
        kind="table",
        type_name=_safe_type_name(value),
        table=SerializedTable(
            columns=columns,
            rows=tuple(rows),
            original_row_count=original_row_count,
            original_column_count=original_column_count,
            rows_truncated=rows_truncated,
            columns_truncated=columns_truncated,
        ),
        original_size=original_row_count,
        truncated=rows_truncated or columns_truncated,
    )


def _shape_of(value: object) -> tuple[tuple[int, ...] | None, SerializedValue | None]:
    try:
        shape = value.shape  # type: ignore[attr-defined]
    except AttributeError:
        return None, None
    except BaseException as error:
        return None, _failed_value(value, operation="getattr", error=error)
    if not isinstance(shape, Sequence) or isinstance(shape, (str, bytes, bytearray)):
        return None, None
    try:
        rank = len(shape)
        dimensions = tuple(islice(iter(shape), 3))
    except BaseException as error:
        return None, _failed_value(value, operation="shape", error=error)
    if rank not in {1, 2} or len(dimensions) != rank:
        return None, _placeholder(value, reason=f"array rank {rank} exceeds supported rank 2")
    if any(not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 0 for dimension in dimensions):
        return None, _placeholder(value, reason="invalid array shape")
    return dimensions, None


def _serialize_dataframe_duck(
    value: object,
    *,
    shape: tuple[int, int],
    columns_source: object,
    depth: int,
    active_ids: set[int],
) -> SerializedValue:
    try:
        columns = tuple(islice(iter(columns_source), _MAX_TABLE_COLUMNS))  # type: ignore[arg-type]
    except BaseException as error:
        return _failed_value(value, operation="iteration", error=error)
    try:
        iloc = value.iloc  # type: ignore[attr-defined]
    except BaseException as error:
        return _failed_value(value, operation="getattr", error=error)
    try:
        bounded_frame = iloc[:_MAX_TABLE_ROWS, :_MAX_TABLE_COLUMNS]
    except BaseException as error:
        return _failed_value(value, operation="slice", error=error)
    try:
        row_iterator = bounded_frame.itertuples(  # type: ignore[attr-defined]
            index=False,
            name=None,
        )
        rows = tuple(islice(row_iterator, _MAX_TABLE_ROWS))
    except BaseException as error:
        return _failed_value(value, operation="iteration", error=error)
    return _serialize_matrix_table(
        value,
        source_columns=columns,
        source_rows=rows,
        original_row_count=shape[0],
        original_column_count=shape[1],
        depth=depth,
        active_ids=active_ids,
    )


def _serialize_array_duck(
    value: object,
    *,
    shape: tuple[int, ...],
    depth: int,
    active_ids: set[int],
) -> SerializedValue:
    selection: object = slice(None, _MAX_SEQUENCE_ITEMS)
    if len(shape) == 2:
        selection = (
            slice(None, _MAX_TABLE_ROWS),
            slice(None, _MAX_TABLE_COLUMNS),
        )
    try:
        bounded_array = value[selection]  # type: ignore[index]
    except BaseException as error:
        return _failed_value(value, operation="slice", error=error)
    try:
        tolist = bounded_array.tolist  # type: ignore[attr-defined]
        materialized = tolist()
    except BaseException as error:
        return _failed_value(value, operation="tolist", error=error)
    if not isinstance(materialized, Sequence) or isinstance(materialized, (str, bytes, bytearray)):
        return _placeholder(value, reason="tolist returned a non-sequence value")
    try:
        source_items = tuple(islice(iter(materialized), _MAX_SEQUENCE_ITEMS))
    except BaseException as error:
        return _failed_value(value, operation="iteration", error=error)
    if len(shape) == 1:
        return SerializedValue(
            kind="sequence",
            type_name=_safe_type_name(value),
            items=tuple(
                _serialize_value(
                    item,
                    depth=depth + 1,
                    active_ids=active_ids,
                )
                for item in source_items
            ),
            original_size=shape[0],
            truncated=shape[0] > _MAX_SEQUENCE_ITEMS,
        )
    return _serialize_matrix_table(
        value,
        source_columns=tuple(range(min(shape[1], _MAX_TABLE_COLUMNS))),
        source_rows=source_items,
        original_row_count=shape[0],
        original_column_count=shape[1],
        depth=depth,
        active_ids=active_ids,
    )


def _serialize_value(
    value: object,
    *,
    depth: int,
    active_ids: set[int],
) -> SerializedValue:
    if depth > _MAX_DEPTH:
        try:
            original_size = len(value)  # type: ignore[arg-type]
        except BaseException:
            original_size = None
        return _placeholder(
            value,
            reason=f"depth limit {_MAX_DEPTH} exceeded",
            original_size=original_size,
        )
    if isinstance(value, bool):
        return SerializedValue(kind="boolean", type_name="bool", value=value)
    if isinstance(value, int):
        if int.bit_length(value) > _MAX_JSON_INTEGER_BITS:
            return _placeholder(
                value,
                reason=f"number exceeds {_MAX_TEXT_CHARACTERS}-character limit",
            )
        try:
            json.dumps(value, allow_nan=False)
        except BaseException as error:
            return _failed_value(value, operation="number encoding", error=error)
        return SerializedValue(kind="number", type_name="int", value=value)
    if isinstance(value, float):
        if math.isfinite(value):
            return SerializedValue(kind="number", type_name="float", value=value)
        text = "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
        return SerializedValue(
            kind="text",
            type_name="float",
            text=text,
            original_size=len(text),
        )
    if value is None:
        return SerializedValue(kind="null", type_name="NoneType")
    if isinstance(value, (datetime, date, time)):
        text = value.isoformat()
        return _serialize_text(text, type_name=type(value).__name__)
    if isinstance(value, str):
        return _serialize_text(value, type_name="str")
    if isinstance(value, BaseException):
        try:
            text = str(value)
        except BaseException as error:
            return _failed_value(value, operation="str", error=error)
        serialized = _serialize_text(text, type_name=_safe_type_name(value))
        if serialized.kind == "placeholder":
            return serialized
        return SerializedValue(
            kind="exception",
            type_name=serialized.type_name,
            text=serialized.text,
            original_size=serialized.original_size,
            truncated=serialized.truncated,
        )
    if is_dataclass(value) and not isinstance(value, type):
        try:
            value_fields = fields(value)
        except BaseException as error:
            return _failed_value(value, operation="fields", error=error)
        original_size = len(value_fields)
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(
                value,
                reason="recursive reference",
                original_size=original_size,
            )
        active_ids.add(object_id)
        try:
            entries: list[SerializedEntry] = []
            for value_field in value_fields[:_MAX_MAPPING_ITEMS]:
                try:
                    item = getattr(value, value_field.name)
                except BaseException as error:
                    return _failed_value(
                        value,
                        operation="getattr",
                        error=error,
                        original_size=original_size,
                    )
                entries.append(
                    SerializedEntry(
                        key=_serialize_text(value_field.name, type_name="str"),
                        value=_serialize_value(
                            item,
                            depth=depth + 1,
                            active_ids=active_ids,
                        ),
                    )
                )
            return SerializedValue(
                kind="mapping",
                type_name=_safe_type_name(value),
                entries=tuple(entries),
                original_size=original_size,
                truncated=original_size > _MAX_MAPPING_ITEMS,
            )
        finally:
            active_ids.discard(object_id)

    try:
        model_dump = value.model_dump  # type: ignore[attr-defined]
    except AttributeError:
        model_dump = None
    except BaseException as error:
        return _failed_value(value, operation="getattr", error=error)
    if callable(model_dump):
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(value, reason="recursive reference")
        active_ids.add(object_id)
        try:
            try:
                model_data = model_dump()
            except BaseException as error:
                return _failed_value(value, operation="model_dump", error=error)
            if not isinstance(model_data, Mapping):
                return _placeholder(
                    value,
                    reason="model_dump returned a non-mapping value",
                )
            serialized_model = _serialize_value(
                model_data,
                depth=depth,
                active_ids=active_ids,
            )
            return replace(serialized_model, type_name=_safe_type_name(value))
        finally:
            active_ids.discard(object_id)
    if isinstance(value, Mapping):
        try:
            original_size = len(value)
        except BaseException as error:
            return _failed_value(value, operation="len", error=error)
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(
                value,
                reason="recursive reference",
                original_size=original_size,
            )
        active_ids.add(object_id)
        try:
            try:
                keys = tuple(islice(iter(value), _MAX_MAPPING_ITEMS))
            except BaseException as error:
                return _failed_value(
                    value,
                    operation="iteration",
                    error=error,
                    original_size=original_size,
                )
            entries: list[SerializedEntry] = []
            for key in keys:
                try:
                    item = value[key]
                except BaseException as error:
                    return _failed_value(
                        value,
                        operation="item access",
                        error=error,
                        original_size=original_size,
                    )
                entries.append(
                    SerializedEntry(
                        key=_serialize_value(
                            key,
                            depth=depth + 1,
                            active_ids=active_ids,
                        ),
                        value=_serialize_value(
                            item,
                            depth=depth + 1,
                            active_ids=active_ids,
                        ),
                    )
                )
            return SerializedValue(
                kind="mapping",
                type_name=_safe_type_name(value),
                entries=tuple(entries),
                original_size=original_size,
                truncated=original_size > _MAX_MAPPING_ITEMS,
            )
        finally:
            active_ids.discard(object_id)

    shape, shape_failure = _shape_of(value)
    if shape_failure is not None:
        return shape_failure
    if shape is not None:
        try:
            columns_source = value.columns  # type: ignore[attr-defined]
        except AttributeError:
            columns_source = None
        except BaseException as error:
            return _failed_value(value, operation="getattr", error=error)
        try:
            tolist = value.tolist  # type: ignore[attr-defined]
        except AttributeError:
            tolist = None
        except BaseException as error:
            return _failed_value(value, operation="getattr", error=error)

        is_dataframe = len(shape) == 2 and columns_source is not None
        is_array = callable(tolist)
        if is_dataframe or is_array:
            object_id = id(value)
            if object_id in active_ids:
                return _placeholder(
                    value,
                    reason="recursive reference",
                    original_size=shape[0],
                )
            active_ids.add(object_id)
            try:
                if is_dataframe:
                    return _serialize_dataframe_duck(
                        value,
                        shape=(shape[0], shape[1]),
                        columns_source=columns_source,
                        depth=depth,
                        active_ids=active_ids,
                    )
                return _serialize_array_duck(
                    value,
                    shape=shape,
                    depth=depth,
                    active_ids=active_ids,
                )
            finally:
                active_ids.discard(object_id)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            original_size = len(value)
        except BaseException as error:
            return _failed_value(value, operation="len", error=error)
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(
                value,
                reason="recursive reference",
                original_size=original_size,
            )
        active_ids.add(object_id)
        try:
            try:
                source_items = tuple(islice(iter(value), _MAX_SEQUENCE_ITEMS))
            except BaseException as error:
                return _failed_value(
                    value,
                    operation="iteration",
                    error=error,
                    original_size=original_size,
                )
            if source_items and all(isinstance(item, Mapping) for item in source_items):
                return _serialize_row_table(
                    value,
                    source_rows=source_items,
                    original_row_count=original_size,
                    depth=depth,
                    active_ids=active_ids,
                )
            return SerializedValue(
                kind="sequence",
                type_name=_safe_type_name(value),
                items=tuple(
                    _serialize_value(
                        item,
                        depth=depth + 1,
                        active_ids=active_ids,
                    )
                    for item in source_items
                ),
                original_size=original_size,
                truncated=original_size > _MAX_SEQUENCE_ITEMS,
            )
        finally:
            active_ids.discard(object_id)
    try:
        text = repr(value)
    except BaseException as error:
        return _failed_value(value, operation="repr", error=error)
    return _serialize_text(text, type_name=_safe_type_name(value))


def serialize_value(value: object) -> SerializedValue:
    """Convert one Python value into an inert typed node."""

    try:
        return _serialize_value(value, depth=0, active_ids=set())
    except BaseException as error:
        return _failed_value(value, operation="serialization", error=error)


def serialized_value_to_wire(value: SerializedValue) -> dict[str, JSONValue]:
    """Cross the explicit boundary from typed nodes to JSON-compatible data."""

    wire: dict[str, JSONValue] = {
        "kind": value.kind,
        "type_name": value.type_name,
    }
    if value.kind in {"boolean", "number", "null"}:
        wire["value"] = value.value
    if value.text is not None:
        wire["text"] = value.text
    if value.original_size is not None:
        wire["original_size"] = value.original_size
    if value.truncated:
        wire["truncated"] = True
    if value.reason is not None:
        wire["reason"] = value.reason
    if value.kind == "mapping":
        wire["entries"] = [
            {
                "key": serialized_value_to_wire(entry.key),
                "value": serialized_value_to_wire(entry.value),
            }
            for entry in value.entries
        ]
    if value.kind == "sequence":
        wire["items"] = [serialized_value_to_wire(item) for item in value.items]
    if value.kind == "table" and value.table is not None:
        table: dict[str, JSONValue] = {
            "columns": [serialized_value_to_wire(column) for column in value.table.columns],
            "rows": [[serialized_value_to_wire(cell) for cell in row.cells] for row in value.table.rows],
            "original_row_count": value.table.original_row_count,
            "original_column_count": value.table.original_column_count,
            "rows_truncated": value.table.rows_truncated,
            "columns_truncated": value.table.columns_truncated,
        }
        wire["table"] = table
    return wire


def dump_serialized_value(value: SerializedValue) -> str:
    """Encode one typed value as script-safe strict JSON."""

    encoded = json.dumps(
        serialized_value_to_wire(value),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
