"""Behavioral contract for bounded inspect-value serialization."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, time

import pytest

from hypergraph.runners._shared._inspect_serialization import (
    SerializedValue,
    dump_serialized_value,
    serialize_value,
    serialized_value_to_wire,
)


def test_scalar_values_are_frozen_and_cross_only_the_explicit_wire() -> None:
    serialized = serialize_value(42)

    assert serialized == SerializedValue(
        kind="number",
        type_name="int",
        value=42,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        serialized.kind = "text"  # type: ignore[misc]

    wire = serialized_value_to_wire(serialized)
    assert wire == {"kind": "number", "type_name": "int", "value": 42}
    assert json.loads(dump_serialized_value(serialized)) == wire


def test_common_scalars_and_text_keep_truth_without_executable_markup() -> None:
    marker = '</script><img src="https://attacker.invalid/pixel" onerror="steal()">\n**markdown**'

    assert serialize_value(None) == SerializedValue(kind="null", type_name="NoneType")
    assert serialize_value(False) == SerializedValue(kind="boolean", type_name="bool", value=False)
    assert serialize_value(3.5) == SerializedValue(kind="number", type_name="float", value=3.5)
    assert serialize_value(float("nan")) == SerializedValue(kind="text", type_name="float", text="nan", original_size=3)
    assert serialize_value(float("inf")) == SerializedValue(kind="text", type_name="float", text="inf", original_size=3)
    assert serialize_value(float("-inf")) == SerializedValue(kind="text", type_name="float", text="-inf", original_size=4)
    assert serialize_value(datetime(2026, 7, 13, 12, 30, 45)) == SerializedValue(
        kind="text",
        type_name="datetime",
        text="2026-07-13T12:30:45",
        original_size=19,
    )
    assert serialize_value(date(2026, 7, 13)).text == "2026-07-13"
    assert serialize_value(time(12, 30, 45)).text == "12:30:45"
    assert serialize_value(marker) == SerializedValue(
        kind="text",
        type_name="str",
        text=marker,
        original_size=len(marker),
    )
    assert math.isfinite(serialize_value(3.5).value)  # type: ignore[arg-type]


def test_text_limit_invalid_unicode_and_script_escaping_are_bounded() -> None:
    one_over = "x" * 20_001
    serialized = serialize_value(one_over)

    assert serialized.kind == "text"
    assert serialized.text == "x" * 20_000
    assert serialized.original_size == 20_001
    assert serialized.truncated is True

    invalid = serialize_value("valid\ud800invalid")
    assert invalid.kind == "placeholder"
    assert invalid.type_name == "str"
    assert invalid.original_size == 13
    assert invalid.truncated is True
    assert invalid.reason == "invalid Unicode"

    inert = serialize_value("</script>&>\u2028\u2029")
    encoded = dump_serialized_value(inert)
    assert "<" not in encoded
    assert ">" not in encoded
    assert "&" not in encoded
    assert "\u2028" not in encoded
    assert "\u2029" not in encoded
    assert json.loads(encoded)["text"] == "</script>&>\u2028\u2029"


def test_exceptions_and_hostile_stringification_become_typed_placeholders() -> None:
    class HostileRepr:
        def __repr__(self) -> str:
            raise RuntimeError("repr must not escape")

    class HostileError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str must not escape")

    ordinary = serialize_value(ValueError("customer missing"))
    assert ordinary == SerializedValue(
        kind="exception",
        type_name="ValueError",
        text="customer missing",
        original_size=16,
    )

    hostile_error = serialize_value(HostileError())
    assert hostile_error.kind == "placeholder"
    assert hostile_error.type_name == "HostileError"
    assert hostile_error.reason == "str failed (RuntimeError)"

    hostile_repr = serialize_value(HostileRepr())
    assert hostile_repr.kind == "placeholder"
    assert hostile_repr.type_name == "HostileRepr"
    assert hostile_repr.reason == "repr failed (RuntimeError)"
    assert hostile_repr.text is None


def test_mapping_limit_recursion_and_iteration_failures_are_truthful() -> None:
    one_over = {f"key-{index}": index for index in range(101)}
    serialized = serialize_value(one_over)

    assert serialized.kind == "mapping"
    assert serialized.original_size == 101
    assert serialized.truncated is True
    assert len(serialized.entries) == 100
    assert serialized.entries[0].key.text == "key-0"
    assert serialized.entries[99].value.value == 99

    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    recursive_value = serialize_value(recursive).entries[0].value
    assert recursive_value.kind == "placeholder"
    assert recursive_value.type_name == "dict"
    assert recursive_value.original_size == 1
    assert recursive_value.reason == "recursive reference"

    class BoundedMapping(Mapping[str, int]):
        def __len__(self) -> int:
            return 1_000_000

        def __iter__(self) -> Iterator[str]:
            for index in range(101):
                if index == 100:
                    raise AssertionError("serializer iterated beyond the mapping cap")
                yield str(index)

        def __getitem__(self, key: str) -> int:
            return int(key)

    bounded = serialize_value(BoundedMapping())
    assert bounded.kind == "mapping"
    assert bounded.original_size == 1_000_000
    assert len(bounded.entries) == 100

    class HostileLength(BoundedMapping):
        def __len__(self) -> int:
            raise RuntimeError("len failed")

    failed = serialize_value(HostileLength())
    assert failed.kind == "placeholder"
    assert failed.reason == "len failed (RuntimeError)"

    wire = serialized_value_to_wire(serialized)
    assert len(wire["entries"]) == 100  # type: ignore[arg-type]


def test_sequence_limit_recursion_and_depth_limit_are_truthful() -> None:
    one_over = serialize_value(list(range(201)))
    assert one_over.kind == "sequence"
    assert one_over.type_name == "list"
    assert one_over.original_size == 201
    assert one_over.truncated is True
    assert len(one_over.items) == 200
    assert one_over.items[-1].value == 199

    recursive: list[object] = []
    recursive.append(recursive)
    recursive_value = serialize_value(recursive).items[0]
    assert recursive_value.kind == "placeholder"
    assert recursive_value.type_name == "list"
    assert recursive_value.original_size == 1
    assert recursive_value.reason == "recursive reference"

    nested: object = "leaf"
    for _ in range(7):
        nested = [nested]
    depth_limited = serialize_value(nested)
    for _ in range(7):
        depth_limited = depth_limited.items[0]
    assert depth_limited.kind == "placeholder"
    assert depth_limited.type_name == "str"
    assert depth_limited.original_size == 4
    assert depth_limited.reason == "depth limit 6 exceeded"

    class BoundedSequence(Sequence[int]):
        def __len__(self) -> int:
            return 1_000_000

        def __getitem__(self, index: int) -> int:
            if index >= 200:
                raise AssertionError("serializer iterated beyond the sequence cap")
            return index

    bounded = serialize_value(BoundedSequence())
    assert bounded.kind == "sequence"
    assert bounded.original_size == 1_000_000
    assert len(bounded.items) == 200


def test_dataclasses_and_model_dump_values_use_bounded_mapping_shape() -> None:
    @dataclasses.dataclass
    class Customer:
        customer_id: str
        active: bool

    class Model:
        def model_dump(self) -> dict[str, object]:
            return {"customer_id": "maya-23", "score": 0.9}

    customer = serialize_value(Customer(customer_id="maya-23", active=True))
    assert customer.kind == "mapping"
    assert customer.type_name == "Customer"
    assert customer.original_size == 2
    assert [entry.key.text for entry in customer.entries] == ["customer_id", "active"]
    assert customer.entries[0].value.text == "maya-23"

    model = serialize_value(Model())
    assert model.kind == "mapping"
    assert model.type_name == "Model"
    assert model.original_size == 2
    assert model.entries[1].value.value == 0.9

    class HostileModel:
        def model_dump(self) -> dict[str, object]:
            raise RuntimeError("dump failed")

    failed_model = serialize_value(HostileModel())
    assert failed_model.kind == "placeholder"
    assert failed_model.reason == "model_dump failed (RuntimeError)"

    @dataclasses.dataclass
    class HostileField:
        secret: str = "never read"

        def __getattribute__(self, name: str) -> object:
            if name == "secret":
                raise RuntimeError("getattr failed")
            return super().__getattribute__(name)

    failed_field = serialize_value(HostileField())
    assert failed_field.kind == "placeholder"
    assert failed_field.reason == "getattr failed (RuntimeError)"


def test_row_table_limits_are_bounded_and_keep_proven_dimensions() -> None:
    rows = [{f"column-{column}": row * 100 + column for column in range(21)} for row in range(201)]

    serialized = serialize_value(rows)
    assert serialized.kind == "table"
    assert serialized.type_name == "list"
    assert serialized.original_size == 201
    assert serialized.truncated is True
    assert serialized.table is not None
    assert serialized.table.original_row_count == 201
    assert serialized.table.original_column_count == 21
    assert serialized.table.original_column_count_exact is False
    assert serialized.table.rows_truncated is True
    assert serialized.table.columns_truncated is True
    assert len(serialized.table.rows) == 200
    assert len(serialized.table.columns) == 20
    assert serialized.table.columns[-1].text == "column-19"
    assert serialized.table.rows[-1].cells[-1].value == 19_919

    wire = serialized_value_to_wire(serialized)
    table = wire["table"]
    assert isinstance(table, dict)
    assert table["original_row_count"] == 201
    assert table["original_column_count"] == 21
    assert table["original_column_count_exact"] is False
    assert len(table["rows"]) == 200  # type: ignore[arg-type]


def test_row_table_uses_heterogeneous_union_and_explicit_missing_cells() -> None:
    rows = [
        {},
        {"customer_id": "maya-23", "risk": 0.9},
        {"risk": 0.3, "decision": "review"},
    ]

    serialized = serialize_value(rows)

    assert serialized.kind == "table"
    assert serialized.truncated is False
    assert serialized.table is not None
    assert [column.text for column in serialized.table.columns] == [
        "customer_id",
        "risk",
        "decision",
    ]
    assert serialized.table.original_column_count == 3
    assert serialized.table.original_column_count_exact is True
    assert serialized.table.columns_truncated is False

    first_row = serialized.table.rows[0]
    assert [cell.kind for cell in first_row.cells] == [
        "placeholder",
        "placeholder",
        "placeholder",
    ]
    assert {cell.reason for cell in first_row.cells} == {"missing table cell"}
    assert first_row.cells[0].type_name == "missing"

    second_row = serialized.table.rows[1]
    assert second_row.cells[0].text == "maya-23"
    assert second_row.cells[1].value == 0.9
    assert second_row.cells[2].reason == "missing table cell"

    third_row = serialized.table.rows[2]
    assert third_row.cells[0].reason == "missing table cell"
    assert third_row.cells[1].value == 0.3
    assert third_row.cells[2].text == "review"


def test_row_table_union_over_column_cap_is_exact_when_safely_bounded() -> None:
    rows = [
        {f"column-{index}": index for index in range(20)},
        {"column-20": 20},
    ]

    serialized = serialize_value(rows)

    assert serialized.kind == "table"
    assert serialized.truncated is True
    assert serialized.table is not None
    assert [column.text for column in serialized.table.columns] == [f"column-{index}" for index in range(20)]
    assert serialized.table.original_column_count == 21
    assert serialized.table.original_column_count_exact is True
    assert serialized.table.columns_truncated is True
    assert len(serialized.table.rows) == 2
    assert len(serialized.table.rows[0].cells) == 20
    assert len(serialized.table.rows[1].cells) == 20
    assert all(cell.reason == "missing table cell" for cell in serialized.table.rows[1].cells)


def test_row_table_wide_mapping_count_is_lower_bound_without_over_iteration() -> None:
    class WideRow(Mapping[str, int]):
        def __init__(self, prefix: str) -> None:
            self.prefix = prefix

        def __len__(self) -> int:
            return 1_000_000

        def __iter__(self) -> Iterator[str]:
            for index in range(21):
                if index == 20:
                    raise AssertionError("serializer iterated a 21st wide-row key")
                yield f"{self.prefix}-{index}"

        def __getitem__(self, key: str) -> int:
            return int(key.rsplit("-", 1)[1])

    serialized = serialize_value([WideRow("first"), WideRow("second")])

    assert serialized.kind == "table"
    assert serialized.truncated is True
    assert serialized.table is not None
    assert len(serialized.table.columns) == 20
    assert serialized.table.original_column_count == 1_000_000
    assert serialized.table.original_column_count_exact is False
    assert serialized.table.columns_truncated is True


def test_row_table_schema_does_not_compare_invalid_unhashable_keys() -> None:
    equality_calls = 0

    class HostileKey:
        __hash__ = None  # type: ignore[assignment]

        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: object) -> bool:
            nonlocal equality_calls
            equality_calls += 1
            raise AssertionError("row-schema discovery compared hostile keys")

        def __repr__(self) -> str:
            return self.name

    class IdentityRow(Mapping[object, int]):
        def __init__(self, keys: list[object]) -> None:
            self.keys = keys

        def __len__(self) -> int:
            return len(self.keys)

        def __iter__(self) -> Iterator[object]:
            yield from self.keys

        def __getitem__(self, key: object) -> int:
            for index, candidate in enumerate(self.keys):
                if candidate is key:
                    return index
            raise KeyError(key)

    keys = [HostileKey(f"key-{index}") for index in range(20)]
    serialized = serialize_value([IdentityRow(keys), IdentityRow(keys)])

    assert serialized.kind == "table"
    assert serialized.table is not None
    assert len(serialized.table.columns) == 20
    assert serialized.table.original_column_count == 20
    assert serialized.table.original_column_count_exact is False
    assert serialized.table.columns_truncated is True
    assert equality_calls == 0


def test_dataframe_and_array_ducks_are_sliced_before_materialization() -> None:
    class FrameSlice:
        def __init__(self, frame: FakeFrame) -> None:
            self._frame = frame

        def __getitem__(self, key: tuple[slice, slice]) -> FakeFrame:
            self._frame.requested_slice = key
            row_slice, column_slice = key
            return FakeFrame(
                [row[column_slice] for row in self._frame.rows[row_slice]],
                self._frame.columns[column_slice],
            )

    class FakeFrame:
        def __init__(self, rows: list[list[int]], columns: list[str]) -> None:
            self.rows = rows
            self.columns = columns
            self.shape = (len(rows), len(columns))
            self.requested_slice: tuple[slice, slice] | None = None

        @property
        def iloc(self) -> FrameSlice:
            return FrameSlice(self)

        def itertuples(self, *, index: bool, name: object) -> Iterator[tuple[int, ...]]:
            assert index is False
            assert name is None
            yield from (tuple(row) for row in self.rows)

    frame = FakeFrame(
        [[row * 100 + column for column in range(21)] for row in range(201)],
        [f"field-{column}" for column in range(21)],
    )
    serialized_frame = serialize_value(frame)
    assert frame.requested_slice == (slice(None, 200), slice(None, 20))
    assert serialized_frame.kind == "table"
    assert serialized_frame.type_name == "FakeFrame"
    assert serialized_frame.table is not None
    assert serialized_frame.table.original_row_count == 201
    assert serialized_frame.table.original_column_count == 21
    assert len(serialized_frame.table.rows) == 200
    assert len(serialized_frame.table.columns) == 20

    class FakeArray:
        def __init__(self, values: list[int]) -> None:
            self.values = values
            self.shape = (len(values),)
            self.requested_slice: slice | None = None

        def __getitem__(self, key: slice) -> FakeArray:
            self.requested_slice = key
            return FakeArray(self.values[key])

        def tolist(self) -> list[int]:
            return list(self.values)

    array = FakeArray(list(range(201)))
    serialized_array = serialize_value(array)
    assert array.requested_slice == slice(None, 200)
    assert serialized_array.kind == "sequence"
    assert serialized_array.type_name == "FakeArray"
    assert serialized_array.original_size == 201
    assert len(serialized_array.items) == 200

    class HostileShape:
        @property
        def shape(self) -> tuple[int]:
            raise RuntimeError("shape failed")

    failed = serialize_value(HostileShape())
    assert failed.kind == "placeholder"
    assert failed.reason == "getattr failed (RuntimeError)"


def test_hostile_container_observation_is_local_and_never_mutates_inputs() -> None:
    class HostileDatetime(datetime):
        def isoformat(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("isoformat failed")

    class HostileIterationMapping(Mapping[str, int]):
        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("iteration failed")

        def __getitem__(self, key: str) -> int:
            return 1

    class HostileItemMapping(Mapping[str, int]):
        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[str]:
            yield "answer"

        def __getitem__(self, key: str) -> int:
            raise RuntimeError("item failed")

    class HostileIterationSequence(Sequence[int]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> int:
            return 1

        def __iter__(self) -> Iterator[int]:
            raise RuntimeError("iteration failed")

    assert serialize_value(HostileIterationMapping()).reason == "iteration failed (RuntimeError)"
    assert serialize_value(HostileItemMapping()).reason == "item access failed (RuntimeError)"
    assert serialize_value(HostileIterationSequence()).reason == "iteration failed (RuntimeError)"
    hostile_datetime = serialize_value(HostileDatetime(2026, 7, 13))
    assert hostile_datetime.reason == "serialization failed (RuntimeError)"

    captured = {"customer": {"id": "maya-23"}}
    snapshot = serialize_value(captured)
    assert captured == {"customer": {"id": "maya-23"}}
    assert snapshot.entries[0].value.entries[0].value.text == "maya-23"


def test_numbers_that_cannot_cross_strict_json_become_placeholders() -> None:
    too_large_for_bounded_json = 10**20_000

    serialized = serialize_value(too_large_for_bounded_json)

    assert serialized.kind == "placeholder"
    assert serialized.type_name == "int"
    assert serialized.reason == "number exceeds 20000-character limit"
    assert json.loads(dump_serialized_value(serialized))["kind"] == "placeholder"


def test_global_serialization_budget_bounds_alias_expansion() -> None:
    leaf: object = {"value": "maya-23"}
    for _ in range(6):
        leaf = {f"k{index}": leaf for index in range(5)}

    serialized = serialize_value(leaf)
    encoded = dump_serialized_value(serialized)

    assert serialized.kind == "mapping"
    assert serialized.truncated is True
    assert "serialization budget exhausted" in encoded
    assert len(encoded.encode("utf-8")) < 500_000


def test_global_text_budget_is_explicit_and_marks_ancestors_truncated() -> None:
    serialized = serialize_value(["x" * 12_000, "y" * 12_000])

    assert serialized.kind == "sequence"
    assert serialized.truncated is True
    assert serialized.items[0].kind == "text"
    assert serialized.items[0].text == "x" * 12_000
    assert serialized.items[1].kind == "placeholder"
    assert serialized.items[1].reason == "serialization budget exhausted"


def test_global_budget_charges_many_large_json_numbers() -> None:
    serialized = serialize_value([10**1_000 for _ in range(200)])
    encoded = dump_serialized_value(serialized)

    assert serialized.kind == "sequence"
    assert serialized.truncated is True
    assert any(item.reason == "serialization budget exhausted" for item in serialized.items)
    assert len(encoded.encode("utf-8")) < 500_000


def test_type_names_cannot_bypass_the_global_payload_bound() -> None:
    huge_name_mapping = type("X" * 4_096, (dict,), {})
    leaf: object = huge_name_mapping({"value": "maya-23"})
    for _ in range(5):
        leaf = huge_name_mapping({f"k{index}": leaf for index in range(5)})

    serialized = serialize_value(leaf)
    encoded = dump_serialized_value(serialized)

    assert len(serialized.type_name) <= 48
    assert serialized.type_name.endswith("... (truncated)")
    assert len(encoded.encode("utf-8")) < 500_000


def test_datetime_subclass_type_name_uses_the_same_hard_bound() -> None:
    huge_name_datetime = type("D" * 4_096, (datetime,), {})

    serialized = serialize_value(huge_name_datetime(2026, 7, 14, 12, 30))

    assert serialized.kind == "text"
    assert len(serialized.type_name) <= 48
    assert serialized.type_name.endswith("... (truncated)")
    assert len(dump_serialized_value(serialized).encode("utf-8")) < 1_000


def test_array_shape_dimensions_cannot_inject_unbounded_json_numbers() -> None:
    class HugeShapeArray:
        shape = (10**20_000,)

        def __getitem__(self, key: object) -> object:
            raise AssertionError("invalid huge shape must fail before slicing")

        def tolist(self) -> list[object]:
            raise AssertionError("invalid huge shape must fail before materialization")

    serialized = serialize_value(HugeShapeArray())
    encoded = dump_serialized_value(serialized)

    assert serialized.kind == "placeholder"
    assert serialized.reason == "array dimension exceeds platform container size"
    assert len(encoded.encode("utf-8")) < 1_000
