"""Behavioral contract for bounded inspect-value serialization."""

from __future__ import annotations

import dataclasses
import json
import math
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

import numpy as np
import pandas as pd
import pydantic
import pytest
from pandas.api.extensions import ExtensionArray, ExtensionDtype, take
from pydantic import BaseModel, computed_field

from hypergraph.runners._shared._inspect_serialization import (
    SerializedTable,
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


def test_exceptions_never_invoke_custom_stringification() -> None:
    calls = {"repr": 0, "str": 0}

    class HostileRepr:
        def __repr__(self) -> str:
            calls["repr"] += 1
            raise RuntimeError("repr must not escape")

    class HostileError(Exception):
        def __str__(self) -> str:
            calls["str"] += 1
            return "secret from custom str"

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "HostileError(<redacted>)"

    ordinary = serialize_value(ValueError("customer missing"))
    assert ordinary == SerializedValue(
        kind="exception",
        type_name="ValueError",
        text="customer missing",
        original_size=16,
    )

    hostile_error = serialize_value(HostileError("customer missing"))
    assert hostile_error.kind == "text"
    assert hostile_error.type_name == "HostileError"
    assert hostile_error.text == "HostileError(<redacted>)"
    assert calls == {"repr": 1, "str": 0}

    hostile_repr = serialize_value(HostileRepr())
    assert hostile_repr.kind == "placeholder"
    assert hostile_repr.type_name == "HostileRepr"
    assert hostile_repr.reason == "repr failed (RuntimeError)"
    assert hostile_repr.text is None
    assert calls == {"repr": 2, "str": 0}


def test_exact_builtin_exceptions_do_not_format_hostile_arguments() -> None:
    calls = {"repr": 0, "str": 0}

    class HostileArgument:
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("exception argument stringification must not run")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("exception argument repr must not run")

    serialized = serialize_value(ValueError(HostileArgument()))

    assert serialized.kind == "placeholder"
    assert serialized.type_name == "ValueError"
    assert serialized.reason == "exception contains unsupported arguments"
    assert calls == {"repr": 0, "str": 0}


def test_exact_builtin_exception_text_uses_only_bounded_inert_arguments() -> None:
    calls = {"repr": 0, "str": 0}

    class HostileFilename(Exception):
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("mutable exception attributes must not be formatted")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("mutable exception attributes must not be formatted")

    error = OSError(2, "missing", "/tmp/original")
    hostile_filename = HostileFilename()
    error.__cause__ = hostile_filename
    error.filename = hostile_filename
    metadata_error = ValueError("bad input")
    metadata_error.ticket = HostileFilename()

    try:
        raise KeyError("customer_id")
    except KeyError as raised_key_error:
        key_error = serialize_value(raised_key_error)

    serialized = serialize_value(error)
    benign_metadata = serialize_value(metadata_error)
    file_error = serialize_value(FileNotFoundError(2, "missing", "/tmp/x"))
    too_many_items = serialize_value(ValueError(tuple(range(201))))
    oversized_bytes = serialize_value(ValueError(b"x" * 20_001))
    long_message = serialize_value(ValueError("x" * 20_001))

    assert serialized.kind == "placeholder"
    assert serialized.reason == "exception contains unsupported arguments"
    assert calls == {"repr": 0, "str": 0}
    assert benign_metadata.kind == "exception"
    assert benign_metadata.text == "bad input"
    assert calls == {"repr": 0, "str": 0}
    assert key_error.kind == "exception"
    assert key_error.text == "'customer_id'"
    assert file_error.kind == "exception"
    assert file_error.text == "[Errno 2] missing: '/tmp/x'"
    assert too_many_items.kind == "placeholder"
    assert too_many_items.reason == "exception contains unsupported arguments"
    assert oversized_bytes.kind == "placeholder"
    assert oversized_bytes.reason == "exception contains unsupported arguments"
    assert long_message.kind == "exception"
    assert long_message.text == "x" * 20_000
    assert long_message.original_size == 20_001
    assert long_message.truncated is True


def test_exact_mapping_limit_and_recursion_are_truthful() -> None:
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

    wire = serialized_value_to_wire(serialized)
    assert len(wire["entries"]) == 100  # type: ignore[arg-type]


def test_mappingproxy_only_traverses_an_exact_dict_referent() -> None:
    exact = serialize_value(MappingProxyType({"customer_id": "maya-23"}))
    assert exact.kind == "mapping"
    assert exact.type_name == "mappingproxy"
    assert exact.entries[0].value.text == "maya-23"

    calls = {"getitem": 0, "iter": 0, "len": 0, "repr": 0}

    class CustomMapping(Mapping[str, str]):
        def __len__(self) -> int:
            calls["len"] += 1
            return 1

        def __iter__(self) -> Iterator[str]:
            calls["iter"] += 1
            yield "customer_id"

        def __getitem__(self, key: str) -> str:
            calls["getitem"] += 1
            return "maya-23"

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "CustomMapping(<redacted>)"

    hostile_proxy = serialize_value(MappingProxyType(CustomMapping()))

    assert hostile_proxy.kind == "text"
    assert hostile_proxy.type_name == "mappingproxy"
    assert hostile_proxy.text == "mappingproxy(CustomMapping(<redacted>))"
    assert calls == {"getitem": 0, "iter": 0, "len": 0, "repr": 1}


def test_exact_sequence_limit_recursion_and_depth_limit_are_truthful() -> None:
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

    exact_tuple = serialize_value(("maya-23", 9))
    assert exact_tuple.kind == "sequence"
    assert exact_tuple.type_name == "tuple"
    assert [item.text or item.value for item in exact_tuple.items] == ["maya-23", 9]


def test_dataclasses_and_pydantic_models_use_stored_fields_without_hooks() -> None:
    calls = {
        "computed": 0,
        "descriptor": 0,
        "model_dump": 0,
        "storage_descriptor": 0,
    }

    @dataclasses.dataclass
    class Customer:
        customer_id: str
        active: bool

    @dataclasses.dataclass(slots=True)
    class SlottedCustomer:
        customer_id: str
        active: bool

    @dataclasses.dataclass
    class DescriptorCustomer:
        customer_id: str

    @dataclasses.dataclass
    class StoredCustomer:
        customer_id: str

    class HostileStorageCustomer(StoredCustomer):
        @property
        def __dict__(self) -> dict[str, object]:  # type: ignore[override]
            calls["storage_descriptor"] += 1
            raise AssertionError("instance storage must use the built-in descriptor")

    descriptor_customer = object.__new__(DescriptorCustomer)
    object.__getattribute__(descriptor_customer, "__dict__")["customer_id"] = "maya-23"

    def read_customer_id(_: DescriptorCustomer) -> str:
        calls["descriptor"] += 1
        raise AssertionError("stored dataclass field must bypass its descriptor")

    DescriptorCustomer.customer_id = property(read_customer_id)  # type: ignore[assignment]

    class CustomerModel(BaseModel):
        customer_id: str
        score: float

        @computed_field
        @property
        def computed_secret(self) -> str:
            calls["computed"] += 1
            raise AssertionError("computed fields are not stored inspection facts")

        def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
            calls["model_dump"] += 1
            raise AssertionError("inspection must not call model_dump")

    customer = serialize_value(Customer(customer_id="maya-23", active=True))
    assert customer.kind == "mapping"
    assert customer.type_name == "Customer"
    assert customer.original_size == 2
    assert [entry.key.text for entry in customer.entries] == ["customer_id", "active"]
    assert customer.entries[0].value.text == "maya-23"

    slotted = serialize_value(SlottedCustomer(customer_id="maya-23", active=True))
    assert slotted.kind == "mapping"
    assert [entry.key.text for entry in slotted.entries] == ["customer_id", "active"]

    descriptor = serialize_value(descriptor_customer)
    assert descriptor.kind == "mapping"
    assert descriptor.entries[0].value.text == "maya-23"

    hostile_storage = serialize_value(HostileStorageCustomer(customer_id="maya-23"))
    assert hostile_storage.kind == "mapping"
    assert hostile_storage.entries[0].value.text == "maya-23"

    model = serialize_value(CustomerModel(customer_id="maya-23", score=0.9))
    assert model.kind == "mapping"
    assert model.type_name == "CustomerModel"
    assert model.original_size == 2
    assert model.entries[1].value.value == 0.9
    assert calls == {
        "computed": 0,
        "descriptor": 0,
        "model_dump": 0,
        "storage_descriptor": 0,
    }


def test_dataclass_and_pydantic_source_discovery_stops_at_mapping_limit() -> None:
    calls = {"field_100": 0}

    @dataclasses.dataclass
    class MixedDataclass:
        field: int
        class_value: ClassVar[int] = 42
        init_only: dataclasses.InitVar[int] = 0

    wide_dataclass = dataclasses.make_dataclass(
        "WideDataclass",
        [(f"field_{index}", int) for index in range(101)],
    )
    dataclass_value = wide_dataclass(*range(101))
    dataclass_storage = object.__getattribute__(dataclass_value, "__dict__")
    dict.__delitem__(dataclass_storage, "field_100")

    def read_field_100(_: object) -> int:
        calls["field_100"] += 1
        raise AssertionError("fields beyond the mapping limit must not be read")

    wide_dataclass.field_100 = property(read_field_100)

    class WideModel(BaseModel):
        seed: int

    model_value = WideModel(seed=0)
    model_storage = object.__getattribute__(model_value, "__dict__")
    for index in range(101):
        dict.__setitem__(model_storage, f"extra_{index}", index)

    serialized_mixed = serialize_value(MixedDataclass(field=1, init_only=2))
    serialized_dataclass = serialize_value(dataclass_value)
    serialized_model = serialize_value(model_value)

    assert serialized_mixed.kind == "mapping"
    assert serialized_mixed.original_size == 1
    assert len(serialized_mixed.entries) == 1
    assert serialized_mixed.entries[0].key.text == "field"
    assert serialized_mixed.truncated is False
    assert serialized_dataclass.kind == "mapping"
    assert serialized_dataclass.original_size == 101
    assert len(serialized_dataclass.entries) == 100
    assert serialized_dataclass.entries[-1].key.text == "field_99"
    assert calls == {"field_100": 0}
    assert serialized_model.kind == "mapping"
    assert serialized_model.original_size == 102
    assert len(serialized_model.entries) == 100
    assert serialized_model.entries[-1].key.text == "extra_98"


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


def test_custom_mapping_sequence_and_model_protocols_use_one_repr_fallback() -> None:
    calls = {
        "dataclass_repr": 0,
        "mapping_getitem": 0,
        "mapping_iter": 0,
        "mapping_len": 0,
        "mapping_repr": 0,
        "model_dump": 0,
        "model_repr": 0,
        "sequence_getitem": 0,
        "sequence_iter": 0,
        "sequence_len": 0,
        "sequence_repr": 0,
    }

    class CustomMapping(Mapping[str, int]):
        def __len__(self) -> int:
            calls["mapping_len"] += 1
            return 1

        def __iter__(self) -> Iterator[str]:
            calls["mapping_iter"] += 1
            yield "answer"

        def __getitem__(self, key: str) -> int:
            calls["mapping_getitem"] += 1
            return 42

        def __repr__(self) -> str:
            calls["mapping_repr"] += 1
            return "CustomMapping(<redacted>)"

    class CustomSequence(Sequence[int]):
        def __len__(self) -> int:
            calls["sequence_len"] += 1
            return 1

        def __iter__(self) -> Iterator[int]:
            calls["sequence_iter"] += 1
            yield 42

        def __getitem__(self, index: int) -> int:
            calls["sequence_getitem"] += 1
            return 42

        def __repr__(self) -> str:
            calls["sequence_repr"] += 1
            return "CustomSequence(<redacted>)"

    class CustomModel:
        def model_dump(self) -> dict[str, object]:
            calls["model_dump"] += 1
            return {"secret": "must not be traversed"}

        def __repr__(self) -> str:
            calls["model_repr"] += 1
            return "CustomModel(<redacted>)"

    class CustomDataclassProtocol:
        __dataclass_fields__ = {"secret": object()}
        __dataclass_params__ = object()

        def __repr__(self) -> str:
            calls["dataclass_repr"] += 1
            return "CustomDataclassProtocol(<redacted>)"

    mapping = serialize_value(CustomMapping())
    sequence = serialize_value(CustomSequence())
    model = serialize_value(CustomModel())
    dataclass_protocol = serialize_value(CustomDataclassProtocol())

    assert (mapping.kind, mapping.text) == ("text", "CustomMapping(<redacted>)")
    assert (sequence.kind, sequence.text) == ("text", "CustomSequence(<redacted>)")
    assert (model.kind, model.text) == ("text", "CustomModel(<redacted>)")
    assert (dataclass_protocol.kind, dataclass_protocol.text) == (
        "text",
        "CustomDataclassProtocol(<redacted>)",
    )
    assert calls == {
        "dataclass_repr": 1,
        "mapping_getitem": 0,
        "mapping_iter": 0,
        "mapping_len": 0,
        "mapping_repr": 1,
        "model_dump": 0,
        "model_repr": 1,
        "sequence_getitem": 0,
        "sequence_iter": 0,
        "sequence_len": 0,
        "sequence_repr": 1,
    }


def test_adapter_discovery_does_not_delegate_to_a_replaced_sys_modules() -> None:
    calls = {"get": 0, "repr": 0}

    class HostileModules(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            calls["get"] += 1
            raise AssertionError("adapter discovery must require the exact module registry")

    class CustomValue:
        def __repr__(self) -> str:
            calls["repr"] += 1
            return "CustomValue(<redacted>)"

    original_modules = sys.modules
    sys.modules = HostileModules()
    try:
        serialized = serialize_value(CustomValue())
    finally:
        sys.modules = original_modules

    assert serialized.kind == "text"
    assert serialized.text == "CustomValue(<redacted>)"
    assert calls == {"get": 0, "repr": 1}


def test_mutable_public_library_aliases_never_define_trusted_adapters() -> None:
    calls = {
        "getitem": 0,
        "model_dump": 0,
        "repr": 0,
        "shape": 0,
        "tolist": 0,
    }

    class Redirected:
        @property
        def shape(self) -> tuple[int, int]:
            calls["shape"] += 1
            raise AssertionError("a mutable public alias is not canonical provenance")

        def __getitem__(self, key: object) -> object:
            calls["getitem"] += 1
            raise AssertionError("a mutable public alias must not enter an array adapter")

        def tolist(self) -> list[object]:
            calls["tolist"] += 1
            raise AssertionError("a mutable public alias must not enter an array adapter")

        def model_dump(self) -> dict[str, object]:
            calls["model_dump"] += 1
            raise AssertionError("a mutable public alias must not define a model adapter")

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "Redirected(<redacted>)"

    serialized: list[SerializedValue] = []
    pandas_frame_module = dict.__getitem__(sys.modules, "pandas.core.frame")
    for module, alias in (
        (pd, "DataFrame"),
        (np, "ndarray"),
        (pydantic, "BaseModel"),
        (pandas_frame_module, "DataFrame"),
    ):
        namespace = object.__getattribute__(module, "__dict__")
        original = dict.__getitem__(namespace, alias)
        setattr(module, alias, Redirected)
        try:
            serialized.append(serialize_value(Redirected()))
        finally:
            setattr(module, alias, original)

    assert [(value.kind, value.text) for value in serialized] == [
        ("text", "Redirected(<redacted>)"),
        ("text", "Redirected(<redacted>)"),
        ("text", "Redirected(<redacted>)"),
        ("text", "Redirected(<redacted>)"),
    ]
    assert calls == {
        "getitem": 0,
        "model_dump": 0,
        "repr": 4,
        "shape": 0,
        "tolist": 0,
    }


def test_forged_defining_module_aliases_never_define_trusted_adapters() -> None:
    calls = {
        "columns": 0,
        "getitem": 0,
        "iloc": 0,
        "model_dump": 0,
        "repr": 0,
        "shape": 0,
        "tolist": 0,
    }

    class ForgedArray:
        __module__ = "numpy"

        @property
        def shape(self) -> tuple[int, ...]:
            calls["shape"] += 1
            return (1,)

        def __getitem__(self, key: object) -> object:
            calls["getitem"] += 1
            return 7

        def tolist(self) -> list[int]:
            calls["tolist"] += 1
            return [7]

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "ForgedArray(<redacted>)"

    class ForgedFrame:
        __module__ = "pandas.core.frame"

        @property
        def shape(self) -> tuple[int, int]:
            calls["shape"] += 1
            raise AssertionError("forged DataFrame must not enter the adapter")

        @property
        def columns(self) -> tuple[str, ...]:
            calls["columns"] += 1
            raise AssertionError("forged DataFrame must not enter the adapter")

        @property
        def iloc(self) -> object:
            calls["iloc"] += 1
            raise AssertionError("forged DataFrame must not enter the adapter")

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "ForgedFrame(<redacted>)"

    class ForgedModel:
        __module__ = "pydantic.main"

        def model_dump(self) -> dict[str, object]:
            calls["model_dump"] += 1
            raise AssertionError("forged BaseModel must not enter the adapter")

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "ForgedModel(<redacted>)"

    ForgedArray.__name__ = "ndarray"
    ForgedArray.__qualname__ = "ndarray"
    ForgedFrame.__name__ = "DataFrame"
    ForgedFrame.__qualname__ = "DataFrame"
    ForgedModel.__name__ = "BaseModel"
    ForgedModel.__qualname__ = "BaseModel"

    serialized: list[SerializedValue] = []
    for module_name, alias, forged_type in (
        ("numpy._core._multiarray_umath", "ndarray", ForgedArray),
        ("pandas.core.frame", "DataFrame", ForgedFrame),
        ("pydantic.main", "BaseModel", ForgedModel),
    ):
        module = dict.__getitem__(sys.modules, module_name)
        namespace = object.__getattribute__(module, "__dict__")
        original = dict.__getitem__(namespace, alias)
        setattr(module, alias, forged_type)
        try:
            serialized.append(serialize_value(forged_type()))
        finally:
            setattr(module, alias, original)

    assert [(value.kind, value.text) for value in serialized] == [
        ("text", "ForgedArray(<redacted>)"),
        ("text", "ForgedFrame(<redacted>)"),
        ("text", "ForgedModel(<redacted>)"),
    ]
    assert calls == {
        "columns": 0,
        "getitem": 0,
        "iloc": 0,
        "model_dump": 0,
        "repr": 3,
        "shape": 0,
        "tolist": 0,
    }


def test_forged_dataframe_with_real_bases_never_defines_trust() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import pandas as pd
from pandas.core.arraylike import OpsMixin
from pandas.core.generic import NDFrame

calls = {"repr": 0, "shape": 0}

def forged_repr(_value):
    calls["repr"] += 1
    return "ForgedDataFrame(<redacted>)"

def forged_shape(_value):
    calls["shape"] += 1
    raise AssertionError("forged public protocol must not run")

forged_type = type(
    "DataFrame",
    (NDFrame, OpsMixin),
    {
        "__module__": "pandas.core.frame",
        "__repr__": forged_repr,
        "shape": property(forged_shape),
    },
)
forged = object.__new__(forged_type)
forged.__dict__["_mgr"] = vars(
    pd.DataFrame({"secret": ["maya-secret"]})
)["_mgr"]

frame_module = __import__("pandas.core.frame", fromlist=["DataFrame"])
real_public = pd.DataFrame
real_internal = frame_module.DataFrame
pd.DataFrame = forged_type
frame_module.DataFrame = forged_type
try:
    from hypergraph.runners._shared._inspect_serialization import serialize_value
    serialized = serialize_value(forged)
finally:
    pd.DataFrame = real_public
    frame_module.DataFrame = real_internal

assert serialized.kind == "text", serialized
assert serialized.text == "ForgedDataFrame(<redacted>)"
assert calls == {"repr": 1, "shape": 0}
""",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_coordinated_forged_pandas_classes_never_define_trust() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import numpy as np
import pandas as pd
import pandas.core.arraylike as arraylike
import pandas.core.frame as frame_module
import pandas.core.generic as generic
import pandas.core.indexes.base as index_module
import pandas.core.indexes.range as range_module

calls = {"frame_repr": 0, "index_repr": 0, "range_repr": 0}

def redacted(name):
    def represent(_value):
        calls[f"{name}_repr"] += 1
        return f"{name.title()}(<redacted>)"
    return represent

real_frame = pd.DataFrame
source = real_frame({"secret": ["maya-secret"]})
forged_frame = type(
    "DataFrame",
    (generic.NDFrame, arraylike.OpsMixin),
    {"__module__": "pandas.core.frame", "__repr__": redacted("frame")},
)
forged_index = type(
    "Index",
    (object,),
    {"__module__": "pandas.core.indexes.base", "__repr__": redacted("index")},
)
forged_range = type(
    "RangeIndex",
    (forged_index,),
    {"__module__": "pandas.core.indexes.range", "__repr__": redacted("range")},
)

column_axis = object.__new__(forged_index)
column_axis.__dict__["_data"] = np.asarray(["secret"])
row_axis = object.__new__(forged_range)
row_axis.__dict__["_range"] = range(1)
source._mgr.axes[0] = column_axis
source._mgr.axes[1] = row_axis
forged = object.__new__(forged_frame)
forged.__dict__["_mgr"] = source.__dict__["_mgr"]

frame_module.DataFrame = forged_frame
pd.DataFrame = forged_frame
index_module.Index = forged_index
pd.Index = forged_index
range_module.RangeIndex = forged_range
pd.RangeIndex = forged_range

from hypergraph.runners._shared._inspect_serialization import serialize_value

serialized = serialize_value(forged)
assert serialized.kind == "text", serialized
assert serialized.text == "Frame(<redacted>)"
assert "maya-secret" not in serialized.text
assert calls == {"frame_repr": 1, "index_repr": 0, "range_repr": 0}
""",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_forged_pydantic_model_with_real_metaclass_never_defines_trust() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import pydantic
import pydantic.main
from pydantic._internal._model_construction import ModelMetaclass

calls = {"repr": 0}

def forged_repr(_value):
    calls["repr"] += 1
    return "ForgedBaseModel(<redacted>)"

forged_type = type.__new__(
    ModelMetaclass,
    "BaseModel",
    (object,),
    {"__module__": "pydantic.main", "__repr__": forged_repr},
)
forged = object.__new__(forged_type)
forged.__dict__["secret"] = "stored"

real_public = pydantic.BaseModel
real_internal = pydantic.main.BaseModel
pydantic.BaseModel = forged_type
pydantic.main.BaseModel = forged_type
try:
    from hypergraph.runners._shared._inspect_serialization import serialize_value
    serialized = serialize_value(forged)
finally:
    pydantic.BaseModel = real_public
    pydantic.main.BaseModel = real_internal

assert serialized.kind == "text", serialized
assert serialized.text == "ForgedBaseModel(<redacted>)"
assert calls == {"repr": 1}
""",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_coordinated_forged_pydantic_aliases_never_define_trust() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import pydantic
import pydantic.main
import pydantic._internal._model_construction as model_construction

calls = {"model_dump": 0, "repr": 0}

def forged_repr(_value):
    calls["repr"] += 1
    return "CoordinatedBaseModel(<redacted>)"

def model_dump(_value):
    calls["model_dump"] += 1
    raise AssertionError("forged model_dump must not run")

forged_metaclass = type(
    "ModelMetaclass",
    (type,),
    {"__module__": "pydantic._internal._model_construction"},
)
forged_type = forged_metaclass(
    "BaseModel",
    (object,),
    {
        "__module__": "pydantic.main",
        "__repr__": forged_repr,
        "model_dump": model_dump,
    },
)
forged = forged_type()
forged.__dict__["secret"] = "must remain behind repr"

real_metaclass = model_construction.ModelMetaclass
real_public = pydantic.BaseModel
real_internal = pydantic.main.BaseModel
model_construction.ModelMetaclass = forged_metaclass
pydantic.BaseModel = forged_type
pydantic.main.BaseModel = forged_type
try:
    from hypergraph.runners._shared._inspect_serialization import serialize_value
    serialized = serialize_value(forged)
finally:
    model_construction.ModelMetaclass = real_metaclass
    pydantic.BaseModel = real_public
    pydantic.main.BaseModel = real_internal

assert serialized.kind == "text", serialized
assert serialized.text == "CoordinatedBaseModel(<redacted>)"
assert calls == {"model_dump": 0, "repr": 1}
""",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_dataframe_serialization_uses_only_proven_stored_values() -> None:
    calls = {"columns": 0, "iloc": 0, "itertuples": 0, "shape": 0}
    frame = pd.DataFrame(
        {
            "customer_id": ["maya-23", "ari-12"],
            "score": [0.9, 0.3],
            "active": [True, False],
        }
    )

    def bomb_property(name: str) -> property:
        def get(_self: object) -> object:
            calls[name] += 1
            raise AssertionError(f"DataFrame.{name} must not be invoked")

        return property(get)

    def bomb_itertuples(_self: object, *args: object, **kwargs: object) -> object:
        calls["itertuples"] += 1
        raise AssertionError("DataFrame.itertuples must not be invoked")

    originals = {
        "columns": pd.DataFrame.columns,
        "iloc": pd.DataFrame.iloc,
        "itertuples": pd.DataFrame.itertuples,
        "shape": pd.DataFrame.shape,
    }
    pd.DataFrame.columns = bomb_property("columns")
    pd.DataFrame.iloc = bomb_property("iloc")
    pd.DataFrame.itertuples = bomb_itertuples
    pd.DataFrame.shape = bomb_property("shape")
    try:
        serialized = serialize_value(frame)
    finally:
        pd.DataFrame.columns = originals["columns"]
        pd.DataFrame.iloc = originals["iloc"]
        pd.DataFrame.itertuples = originals["itertuples"]
        pd.DataFrame.shape = originals["shape"]

    assert serialized.kind == "table"
    assert serialized.table is not None
    assert [column.text for column in serialized.table.columns] == [
        "customer_id",
        "score",
        "active",
    ]
    assert [[cell.text if cell.kind == "text" else cell.value for cell in row.cells] for row in serialized.table.rows] == [
        ["maya-23", 0.9, True],
        ["ari-12", 0.3, False],
    ]
    assert calls == {"columns": 0, "iloc": 0, "itertuples": 0, "shape": 0}


def test_fragmented_numpy_dataframe_remains_a_bounded_table() -> None:
    frame = pd.DataFrame(index=[0])
    for column_index in range(21):
        frame[f"metric_{column_index}"] = column_index

    serialized = serialize_value(frame)

    assert serialized.kind == "table"
    assert serialized.table is not None
    assert [column.text for column in serialized.table.columns] == [f"metric_{column_index}" for column_index in range(20)]
    assert [[cell.value for cell in row.cells] for row in serialized.table.rows] == [list(range(20))]
    assert serialized.table.original_column_count == 21
    assert serialized.table.columns_truncated is True


def test_dataframe_placement_scan_limit_is_cumulative() -> None:
    def value_for(index: int) -> int | float | str:
        residue = index % 7
        if residue in {0, 1}:
            return index
        if residue in {2, 4}:
            return float(index)
        return str(index)

    frame = pd.DataFrame({f"column_{index}": [value_for(index)] for index in range(12_000)})

    serialized = serialize_value(frame)

    assert serialized.kind == "placeholder"
    assert serialized.reason == "DataFrame storage exceeds 10000-placement inspection limit"


def test_stored_dataframe_layout_preserves_empty_rows_duplicate_columns_and_placements() -> None:
    empty_columns = serialize_value(pd.DataFrame(index=[0, 1]))

    assert empty_columns.kind == "table"
    assert empty_columns.table is not None
    assert empty_columns.table.original_row_count == 2
    assert empty_columns.table.original_column_count == 0
    assert empty_columns.table.columns == ()
    assert [row.cells for row in empty_columns.table.rows] == [(), ()]

    frame = pd.DataFrame(
        {
            "integer_a": [1, 2],
            "customer": ["maya", "ari"],
            "integer_b": [3, 4],
            "score": [0.9, 0.3],
        }
    )
    frame.columns = ["metric", "customer", "metric", "score"]

    serialized = serialize_value(frame)

    assert serialized.kind == "table"
    assert serialized.table is not None
    assert [column.text for column in serialized.table.columns] == [
        "metric",
        "customer",
        "metric",
        "score",
    ]
    assert [[cell.text if cell.kind == "text" else cell.value for cell in row.cells] for row in serialized.table.rows] == [
        [1, "maya", 3, 0.9],
        [2, "ari", 4, 0.3],
    ]


def test_supported_type_subclasses_never_enter_trusted_adapters() -> None:
    calls: dict[str, int] = {}

    def called(name: str) -> None:
        calls[name] = calls.get(name, 0) + 1

    class CustomDict(dict[str, int]):
        def __len__(self) -> int:
            called("dict_len")
            return super().__len__()

        def __iter__(self) -> Iterator[str]:
            called("dict_iter")
            return super().__iter__()

        def __getitem__(self, key: str) -> int:
            called("dict_getitem")
            return super().__getitem__(key)

        def __repr__(self) -> str:
            called("dict_repr")
            return "CustomDict(<redacted>)"

    class CustomList(list[int]):
        def __len__(self) -> int:
            called("list_len")
            return super().__len__()

        def __iter__(self) -> Iterator[int]:
            called("list_iter")
            return super().__iter__()

        def __getitem__(self, key: int | slice):  # type: ignore[no-untyped-def]
            called("list_getitem")
            return super().__getitem__(key)

        def __repr__(self) -> str:
            called("list_repr")
            return "CustomList(<redacted>)"

    class CustomTuple(tuple[int, ...]):
        def __len__(self) -> int:
            called("tuple_len")
            return super().__len__()

        def __iter__(self) -> Iterator[int]:
            called("tuple_iter")
            return super().__iter__()

        def __getitem__(self, key: int | slice):  # type: ignore[no-untyped-def]
            called("tuple_getitem")
            return super().__getitem__(key)

        def __repr__(self) -> str:
            called("tuple_repr")
            return "CustomTuple(<redacted>)"

    class CustomInt(int):
        def __repr__(self) -> str:
            called("int_repr")
            return "CustomInt(<redacted>)"

    class CustomFloat(float):
        def __repr__(self) -> str:
            called("float_repr")
            return "CustomFloat(<redacted>)"

    class CustomStr(str):
        def __len__(self) -> int:
            called("str_len")
            raise AssertionError("str subclass length must not be observed")

        def __getitem__(self, key: int | slice) -> str:
            called("str_getitem")
            raise AssertionError("str subclass items must not be observed")

        def encode(self, *args: object, **kwargs: object) -> bytes:
            called("str_encode")
            raise AssertionError("str subclass encoding must not be observed")

        def __repr__(self) -> str:
            called("str_repr")
            return "CustomStr(<redacted>)"

    class CustomBytes(bytes):
        def __repr__(self) -> str:
            called("bytes_repr")
            return "CustomBytes(<redacted>)"

    class CustomDate(date):
        def isoformat(self) -> str:
            called("date_isoformat")
            raise AssertionError("date subclass isoformat must not run")

        def __repr__(self) -> str:
            called("date_repr")
            return "CustomDate(<redacted>)"

    class CustomTime(time):
        def isoformat(self, *args: object, **kwargs: object) -> str:
            called("time_isoformat")
            raise AssertionError("time subclass isoformat must not run")

        def __repr__(self) -> str:
            called("time_repr")
            return "CustomTime(<redacted>)"

    class CustomDatetime(datetime):
        def isoformat(self, *args: object, **kwargs: object) -> str:
            called("datetime_isoformat")
            raise AssertionError("datetime subclass isoformat must not run")

        def __repr__(self) -> str:
            called("datetime_repr")
            return "CustomDatetime(<redacted>)"

    values = [
        CustomDict(answer=42),
        CustomList([42]),
        CustomTuple((42,)),
        CustomInt(42),
        CustomFloat(4.2),
        CustomStr("secret"),
        CustomBytes(b"secret"),
        CustomDate(2026, 7, 14),
        CustomTime(12, 30),
        CustomDatetime(2026, 7, 14, 12, 30),
    ]

    serialized = [serialize_value(value) for value in values]

    assert [value.kind for value in serialized] == ["text"] * len(values)
    assert [value.text for value in serialized] == [
        "CustomDict(<redacted>)",
        "CustomList(<redacted>)",
        "CustomTuple(<redacted>)",
        "CustomInt(<redacted>)",
        "CustomFloat(<redacted>)",
        "CustomStr(<redacted>)",
        "CustomBytes(<redacted>)",
        "CustomDate(<redacted>)",
        "CustomTime(<redacted>)",
        "CustomDatetime(<redacted>)",
    ]
    assert calls == {
        "bytes_repr": 1,
        "date_repr": 1,
        "datetime_repr": 1,
        "dict_repr": 1,
        "float_repr": 1,
        "int_repr": 1,
        "list_repr": 1,
        "str_repr": 1,
        "time_repr": 1,
        "tuple_repr": 1,
    }


def test_type_names_and_repr_text_bypass_user_hooks() -> None:
    calls = {
        "metaclass_dataclass": 0,
        "metaclass_eq": 0,
        "metaclass_hash": 0,
        "metaclass_name": 0,
        "repr": 0,
        "text": 0,
    }

    class HostileMeta(type):
        def __getattribute__(cls, name: str) -> object:
            if name == "__dataclass_fields__":
                calls["metaclass_dataclass"] += 1
                raise AssertionError("dataclass detection must bypass custom metaclasses")
            if name == "__name__":
                calls["metaclass_name"] += 1
                raise AssertionError("type-name capture must bypass custom metaclasses")
            return super().__getattribute__(name)

        def __hash__(cls) -> int:
            calls["metaclass_hash"] += 1
            raise AssertionError("type routing must not hash custom metaclasses")

        def __eq__(cls, other: object) -> bool:
            calls["metaclass_eq"] += 1
            raise AssertionError("type routing must not compare custom metaclasses")

    class HostileText(str):
        def __len__(self) -> int:
            calls["text"] += 1
            raise AssertionError("repr text length must use the base str implementation")

        def __getitem__(self, key: int | slice) -> str:
            calls["text"] += 1
            raise AssertionError("repr text slicing must use the base str implementation")

        def encode(self, *args: object, **kwargs: object) -> bytes:
            calls["text"] += 1
            raise AssertionError("repr text encoding must use the base str implementation")

    class HostileValue(metaclass=HostileMeta):
        def __repr__(self) -> str:
            calls["repr"] += 1
            return HostileText("HostileValue(<redacted>)")

    calls = {
        "metaclass_dataclass": 0,
        "metaclass_eq": 0,
        "metaclass_hash": 0,
        "metaclass_name": 0,
        "repr": 0,
        "text": 0,
    }
    serialized = serialize_value(HostileValue())

    assert serialized.kind == "text"
    assert serialized.type_name == "HostileValue"
    assert serialized.text == "HostileValue(<redacted>)"
    assert calls == {
        "metaclass_dataclass": 0,
        "metaclass_eq": 0,
        "metaclass_hash": 0,
        "metaclass_name": 0,
        "repr": 1,
        "text": 0,
    }


def test_exact_dict_items_do_not_rehash_hostile_keys() -> None:
    calls = {"eq": 0, "hash": 0, "repr": 0}

    class HostileKey:
        def __hash__(self) -> int:
            calls["hash"] += 1
            return 42

        def __eq__(self, other: object) -> bool:
            calls["eq"] += 1
            raise AssertionError("stored dict items must not compare keys")

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "HostileKey(<redacted>)"

    key = HostileKey()
    source = {key: "maya-23"}
    calls = {"eq": 0, "hash": 0, "repr": 0}

    serialized = serialize_value(source)

    assert serialized.kind == "mapping"
    assert serialized.entries[0].key.text == "HostileKey(<redacted>)"
    assert serialized.entries[0].value.text == "maya-23"
    assert calls == {"eq": 0, "hash": 0, "repr": 1}


def test_row_table_schema_never_hashes_or_compares_hostile_hashable_keys() -> None:
    hash_calls = 0
    equality_calls = 0

    class CollisionKey:
        def __init__(self, label: str) -> None:
            self.label = label

        def __hash__(self) -> int:
            nonlocal hash_calls
            hash_calls += 1
            return 0

        def __eq__(self, other: object) -> bool:
            nonlocal equality_calls
            equality_calls += 1
            return isinstance(other, CollisionKey) and self.label == other.label

        def __repr__(self) -> str:
            return self.label

    rows = [{CollisionKey(f"row-{row}-column-{column}"): row * 100 + column for column in range(20)} for row in range(200)]
    hash_calls = 0
    equality_calls = 0

    serialized = serialize_value(rows)

    assert serialized.kind == "table"
    assert serialized.table is not None
    assert len(serialized.table.columns) == 20
    assert serialized.table.original_column_count == 20
    assert serialized.table.original_column_count_exact is False
    assert serialized.table.columns_truncated is True
    assert hash_calls == 0
    assert equality_calls == 0


def test_semantically_equal_unsafe_keys_do_not_overstate_column_lower_bound() -> None:
    hash_calls = 0
    equality_calls = 0

    class EqualKey:
        def __init__(self, label: str) -> None:
            self.label = label

        def __hash__(self) -> int:
            nonlocal hash_calls
            hash_calls += 1
            return 0

        def __eq__(self, other: object) -> bool:
            nonlocal equality_calls
            equality_calls += 1
            return isinstance(other, EqualKey) and self.label == other.label

        def __repr__(self) -> str:
            return self.label

    rows = [{EqualKey("customer_id"): "maya-23"}, {EqualKey("customer_id"): "ari-12"}]
    hash_calls = 0
    equality_calls = 0

    serialized = serialize_value(rows)

    assert serialized.kind == "table"
    assert serialized.table is not None
    assert len(serialized.table.columns) == 2
    assert serialized.table.original_column_count == 1
    assert serialized.table.original_column_count_exact is False
    assert serialized.table.columns_truncated is True
    assert hash_calls == 0
    assert equality_calls == 0


def test_exact_numpy_arrays_and_pandas_dataframes_stay_bounded_and_structured() -> None:
    vector = serialize_value(np.arange(201))
    assert vector.kind == "sequence"
    assert vector.type_name == "ndarray"
    assert vector.original_size == 201
    assert vector.truncated is True
    assert len(vector.items) == 200
    assert vector.items[-1].value == 199

    matrix = serialize_value(np.arange(201 * 21).reshape(201, 21))
    assert matrix.kind == "table"
    assert matrix.table is not None
    assert matrix.table.original_row_count == 201
    assert matrix.table.original_column_count == 21
    assert len(matrix.table.rows) == 200
    assert len(matrix.table.columns) == 20

    frame = serialize_value(
        pd.DataFrame(
            np.arange(201 * 21).reshape(201, 21),
            columns=[f"field-{column}" for column in range(21)],
        )
    )
    assert frame.kind == "table"
    assert frame.type_name == "DataFrame"
    assert frame.table is not None
    assert frame.table.original_row_count == 201
    assert frame.table.original_column_count == 21
    assert len(frame.table.rows) == 200
    assert len(frame.table.columns) == 20

    object_frame = serialize_value(
        pd.DataFrame(
            {
                "customer_id": ["maya-23", "ari-12"],
                "score": [0.9, 0.3],
            }
        )
    )
    assert object_frame.kind == "table"
    assert object_frame.table is not None
    assert [column.text for column in object_frame.table.columns] == [
        "customer_id",
        "score",
    ]
    assert object_frame.table.rows[0].cells[0].text == "maya-23"
    assert object_frame.table.rows[0].cells[1].value == 0.9


def test_numpy_legacy_internal_module_path_stays_structured() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from types import ModuleType

import numpy as np

sys.modules.pop("numpy._core._multiarray_umath", None)
legacy = ModuleType("numpy.core._multiarray_umath")
legacy.ndarray = np.ndarray
sys.modules["numpy.core._multiarray_umath"] = legacy

from hypergraph.runners._shared._inspect_serialization import serialize_value

serialized = serialize_value(np.asarray([1, 2, 3]))
assert serialized.kind == "sequence", serialized
assert [item.value for item in serialized.items] == [1, 2, 3]
""",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_extension_backed_dataframe_is_rejected_without_extension_hooks() -> None:
    calls = {
        "array": 0,
        "dtype": 0,
        "extension_repr": 0,
        "frame_repr": 0,
        "getitem": 0,
        "iter": 0,
        "len": 0,
        "take": 0,
        "tolist": 0,
    }

    class HostileDtype(ExtensionDtype):
        name = "hypergraph-hostile"
        type = object
        kind = "O"
        na_value = None

        @classmethod
        def construct_array_type(cls) -> type[HostileArray]:
            return HostileArray

    class HostileArray(ExtensionArray):
        def __init__(self, values: Sequence[object]) -> None:
            self._values = list(values)

        @classmethod
        def _from_sequence(
            cls,
            scalars: Sequence[object],
            *,
            dtype: ExtensionDtype | None = None,
            copy: bool = False,
        ) -> HostileArray:
            return cls(list(scalars) if copy else scalars)

        @property
        def dtype(self) -> ExtensionDtype:
            calls["dtype"] += 1
            return HostileDtype()

        @property
        def nbytes(self) -> int:
            return len(self._values) * 8

        def __len__(self) -> int:
            calls["len"] += 1
            return len(self._values)

        def __getitem__(self, item: int | slice) -> object:
            calls["getitem"] += 1
            if isinstance(item, slice):
                return type(self)(self._values[item])
            return self._values[item]

        def __iter__(self) -> Iterator[object]:
            calls["iter"] += 1
            return iter(self._values)

        def __array__(
            self,
            dtype: object = None,
            copy: bool | None = None,
        ) -> np.ndarray:
            calls["array"] += 1
            return np.asarray(self._values, dtype=dtype)

        def __repr__(self) -> str:
            calls["extension_repr"] += 1
            raise AssertionError("DataFrame repr must not delegate to the extension")

        def tolist(self) -> list[object]:
            calls["tolist"] += 1
            return list(self._values)

        def isna(self) -> np.ndarray:
            return np.asarray([value is None for value in self._values])

        def take(
            self,
            indices: Sequence[int],
            *,
            allow_fill: bool = False,
            fill_value: object = None,
        ) -> HostileArray:
            calls["take"] += 1
            values = take(
                np.asarray(self._values, dtype=object),
                indices,
                allow_fill=allow_fill,
                fill_value=fill_value,
            )
            return type(self)(values.tolist())

        def copy(self) -> HostileArray:
            return type(self)(self._values.copy())

    frames = (
        pd.DataFrame({"customer": HostileArray(["maya", "ari"])}),
        pd.DataFrame(
            [[1, 2]],
            columns=pd.Index(HostileArray(["customer", "score"])),
        ),
        pd.DataFrame(
            [[1], [2]],
            index=pd.Index(HostileArray(["maya", "ari"])),
        ),
    )
    original_repr = pd.DataFrame.__repr__

    def forbidden_frame_repr(_: pd.DataFrame) -> str:
        calls["frame_repr"] += 1
        raise AssertionError("extension-backed DataFrame must not use whole-value repr")

    pd.DataFrame.__repr__ = forbidden_frame_repr
    serialized_frames: list[SerializedValue] = []
    observed_calls: list[dict[str, int]] = []
    try:
        for frame in frames:
            calls = dict.fromkeys(calls, 0)
            serialized_frames.append(serialize_value(frame))
            observed_calls.append(calls.copy())
    finally:
        pd.DataFrame.__repr__ = original_repr

    assert [serialized.kind for serialized in serialized_frames] == [
        "placeholder",
        "placeholder",
        "placeholder",
    ]
    assert {serialized.type_name for serialized in serialized_frames} == {"DataFrame"}
    assert {serialized.reason for serialized in serialized_frames} == {"unsupported extension-backed DataFrame"}
    assert all(serialized.truncated for serialized in serialized_frames)
    assert observed_calls == [dict.fromkeys(calls, 0)] * 3


def test_array_and_dataframe_protocols_and_subclasses_use_repr_only() -> None:
    calls: dict[str, int] = {}

    def called(name: str) -> None:
        calls[name] = calls.get(name, 0) + 1

    class ArrayProtocol:
        @property
        def shape(self) -> tuple[int]:
            called("protocol_shape")
            return (1,)

        def __getitem__(self, key: object) -> object:
            called("protocol_getitem")
            return self

        def tolist(self) -> list[int]:
            called("protocol_tolist")
            return [42]

        def __repr__(self) -> str:
            called("protocol_repr")
            return "ArrayProtocol(<redacted>)"

    class ArraySubclass(np.ndarray):
        def __new__(cls) -> ArraySubclass:
            return np.asarray([42]).view(cls)

        @property
        def shape(self) -> tuple[int, ...]:  # type: ignore[override]
            called("array_shape")
            raise AssertionError("ndarray subclass shape must not be read")

        def __getitem__(self, key: object) -> object:
            called("array_getitem")
            raise AssertionError("ndarray subclass items must not be read")

        def tolist(self) -> list[object]:
            called("array_tolist")
            raise AssertionError("ndarray subclass tolist must not run")

        def __repr__(self) -> str:
            called("array_repr")
            return "ArraySubclass(<redacted>)"

    class FrameSubclass(pd.DataFrame):
        @property
        def shape(self) -> tuple[int, int]:  # type: ignore[override]
            called("frame_shape")
            raise AssertionError("DataFrame subclass shape must not be read")

        @property
        def columns(self) -> pd.Index:  # type: ignore[override]
            called("frame_columns")
            raise AssertionError("DataFrame subclass columns must not be read")

        @property
        def iloc(self) -> object:  # type: ignore[override]
            called("frame_iloc")
            raise AssertionError("DataFrame subclass iloc must not be read")

        def __repr__(self) -> str:
            called("frame_repr")
            return "FrameSubclass(<redacted>)"

    array_subclass = ArraySubclass()
    frame_subclass = FrameSubclass({"answer": [42]})
    calls = {}

    protocol = serialize_value(ArrayProtocol())
    array = serialize_value(array_subclass)
    frame = serialize_value(frame_subclass)

    assert (protocol.kind, protocol.text) == ("text", "ArrayProtocol(<redacted>)")
    assert (array.kind, array.text) == ("text", "ArraySubclass(<redacted>)")
    assert (frame.kind, frame.text) == ("text", "FrameSubclass(<redacted>)")
    assert calls == {"array_repr": 1, "frame_repr": 1, "protocol_repr": 1}


def test_exact_container_observation_never_mutates_inputs() -> None:
    captured = {"customer": {"id": "maya-23"}}
    snapshot = serialize_value(captured)
    assert captured == {"customer": {"id": "maya-23"}}
    assert snapshot.entries[0].value.entries[0].value.text == "maya-23"


def test_depth_placeholder_does_not_call_arbitrary_len_or_repr() -> None:
    calls = {"len": 0, "repr": 0}

    class HostileLength:
        def __len__(self) -> int:
            calls["len"] += 1
            raise AssertionError("early placeholders must not observe custom length")

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "HostileLength(<redacted>)"

    nested: object = HostileLength()
    for _ in range(7):
        nested = [nested]
    depth_limited = serialize_value(nested)
    for _ in range(7):
        depth_limited = depth_limited.items[0]
    assert depth_limited.kind == "placeholder"
    assert depth_limited.reason == "depth limit 6 exceeded"
    assert depth_limited.original_size is None
    assert calls == {"len": 0, "repr": 0}


def test_budget_placeholder_does_not_call_arbitrary_len_or_repr() -> None:
    calls = {"len": 0, "repr": 0}

    class HostileLength:
        def __len__(self) -> int:
            calls["len"] += 1
            raise AssertionError("early placeholders must not observe custom length")

        def __repr__(self) -> str:
            calls["repr"] += 1
            return "HostileLength(<redacted>)"

    rows = [{f"column-{column}": row * 100 + column for column in range(20)} for row in range(200)]
    rows[-1]["column-19"] = [*range(79), HostileLength()]

    budget_limited = serialize_value(rows)

    assert budget_limited.kind == "table"
    assert budget_limited.truncated is True
    assert calls == {"len": 0, "repr": 0}


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
    huge_name_type = type(
        "X" * 4_096,
        (),
        {"__repr__": lambda self: "HugeName(<redacted>)"},
    )

    serialized = serialize_value(huge_name_type())
    encoded = dump_serialized_value(serialized)

    assert serialized.kind == "text"
    assert serialized.text == "HugeName(<redacted>)"
    assert len(serialized.type_name) <= 48
    assert serialized.type_name.endswith("... (truncated)")
    assert len(encoded.encode("utf-8")) < 1_000


def test_datetime_subclass_type_name_uses_the_same_hard_bound() -> None:
    huge_name_datetime = type("D" * 4_096, (datetime,), {})

    serialized = serialize_value(huge_name_datetime(2026, 7, 14, 12, 30))

    assert serialized.kind == "text"
    assert len(serialized.type_name) <= 48
    assert serialized.type_name.endswith("... (truncated)")
    assert len(dump_serialized_value(serialized).encode("utf-8")) < 20_500


def test_js_unsafe_size_metadata_crosses_the_wire_as_exact_decimal_strings() -> None:
    unsafe_count = 2**53 + 1
    safe_boundary = 2**53 - 1

    matrix_wire = serialized_value_to_wire(
        SerializedValue(
            kind="table",
            type_name="DataFrame",
            table=SerializedTable(
                columns=(),
                rows=(),
                original_row_count=unsafe_count,
                original_column_count=unsafe_count,
                original_column_count_exact=True,
                rows_truncated=True,
                columns_truncated=True,
            ),
            original_size=unsafe_count,
            truncated=True,
        )
    )

    assert matrix_wire["original_size"] == str(unsafe_count)
    table = matrix_wire["table"]
    assert isinstance(table, dict)
    assert table["original_row_count"] == str(unsafe_count)
    assert table["original_column_count"] == str(unsafe_count)
    assert serialized_value_to_wire(SerializedValue(kind="sequence", type_name="list", original_size=safe_boundary))["original_size"] == safe_boundary
    assert serialized_value_to_wire(SerializedValue(kind="sequence", type_name="list", original_size=safe_boundary + 1))["original_size"] == str(
        safe_boundary + 1
    )
    assert serialized_value_to_wire(serialize_value(list(range(201))))["original_size"] == 201


def test_serializer_module_imports_without_optional_data_packages() -> None:
    module_path = Path(__file__).parents[2] / "src/hypergraph/runners/_shared/_inspect_serialization.py"
    completed = subprocess.run(
        [sys.executable, "-S", "-c", "import runpy, sys; runpy.run_path(sys.argv[1])", str(module_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
