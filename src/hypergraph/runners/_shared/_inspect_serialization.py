"""Bounded, typed serialization for captured inspect values.

The runtime artifact keeps the original Python objects.  This module is the
single conversion seam from those objects to inert JSON-compatible data for
inspection renderers.
"""

from __future__ import annotations

import builtins
import dataclasses
import gc
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from itertools import islice
from types import (
    GetSetDescriptorType,
    MappingProxyType,
    MemberDescriptorType,
    MethodDescriptorType,
    ModuleType,
    WrapperDescriptorType,
)
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
_MAX_TYPE_NAME_CHARACTERS = 48
_MAX_CONTAINER_SIZE = sys.maxsize
_MAX_JS_SAFE_INTEGER = 2**53 - 1
_MAX_SERIALIZED_NODES = _MAX_TABLE_ROWS * _MAX_TABLE_COLUMNS + _MAX_MAPPING_ITEMS
_MAX_SERIALIZED_TEXT_CHARACTERS = _MAX_TEXT_CHARACTERS
_MAX_EXCEPTION_FORMAT_OVERHEAD = 256
_MAX_PANDAS_BLOCKS = 1_000
_MAX_PANDAS_PLACEMENTS_TO_SCAN = 10_000
_SERIALIZATION_BUDGET_EXHAUSTED = "serialization budget exhausted"
_TYPE_NAME_TRUNCATION_MARKER = "... (truncated)"
_PY_TPFLAGS_HEAPTYPE = 1 << 9
_SAFE_ROW_KEY_TYPES = (str, bytes, int, float, bool, type(None))
_MISSING = object()
_CANONICAL_TYPE_CACHE: dict[str, type] = {}
_DATACLASS_FIELD_MARKER = vars(dataclasses).get("_FIELD")
_DATACLASS_PARAMS_TYPE = vars(dataclasses).get("_DataclassParams")
_SAFE_EXCEPTION_TYPES = tuple(
    candidate
    for candidate in vars(builtins).values()
    if type(candidate) is type and any(base is BaseException for base in type.__getattribute__(candidate, "__mro__"))
)


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
    original_column_count_exact: bool
    rows_truncated: bool
    columns_truncated: bool


@dataclass(slots=True)
class _SerializationBudget:
    """Private per-value ceiling for emitted work and captured text."""

    nodes_remaining: int = _MAX_SERIALIZED_NODES
    text_characters_remaining: int = _MAX_SERIALIZED_TEXT_CHARACTERS
    work_exhausted: bool = False
    limit_hits: int = 0

    def claim_node(self) -> bool:
        if self.nodes_remaining <= 0:
            self.work_exhausted = True
            self.limit_hits += 1
            return False
        self.nodes_remaining -= 1
        return True

    def claim_text(self, characters: int) -> bool:
        if characters > self.text_characters_remaining:
            self.limit_hits += 1
            return False
        self.text_characters_remaining -= characters
        return True


@dataclass(frozen=True, slots=True)
class _RowTableItem:
    """One captured row item or its localized access failure."""

    key: object
    value: object = None
    failure: SerializedValue | None = None
    missing: bool = False


@dataclass(frozen=True, slots=True)
class _RowTableSchema:
    """Displayed row keys plus exact-or-lower-bound source width truth."""

    displayed_keys: tuple[object, ...]
    row_items: tuple[tuple[_RowTableItem, ...], ...]
    original_column_count: int
    original_column_count_exact: bool


@dataclass(frozen=True, slots=True)
class _PandasTableStorage:
    """Bounded inert values read directly from a trusted DataFrame manager."""

    columns: tuple[object, ...]
    rows: tuple[tuple[object, ...], ...]
    original_row_count: int
    original_column_count: int


def _safe_type_name(value: object) -> str:
    try:
        name = type.__getattribute__(type(value), "__name__")
        name = str.__str__(name)
        original_size = str.__len__(name)
        bounded_name = str.__getitem__(name, slice(None, _MAX_TYPE_NAME_CHARACTERS))
        str.encode(bounded_name, "utf-8", errors="strict")
    except BaseException:
        return "unknown"
    if original_size <= _MAX_TYPE_NAME_CHARACTERS:
        return bounded_name
    prefix_size = _MAX_TYPE_NAME_CHARACTERS - len(_TYPE_NAME_TRUNCATION_MARKER)
    return f"{bounded_name[:prefix_size]}{_TYPE_NAME_TRUNCATION_MARKER}"


def _loaded_module_attribute(module_name: str, attribute_name: str) -> object:
    sys_namespace = object.__getattribute__(sys, "__dict__")
    modules = dict.get(sys_namespace, "modules")
    if type(modules) is not dict:
        return _MISSING
    module = dict.get(modules, module_name)
    if type(module) is not ModuleType:
        return _MISSING
    namespace = object.__getattribute__(module, "__dict__")
    return dict.get(namespace, attribute_name, _MISSING)


def _declares_type(candidate: object, *, module_name: str, class_name: str) -> bool:
    try:
        mro = type.__getattribute__(candidate, "__mro__")
        declared_name = type.__getattribute__(candidate, "__name__")
        declared_module = type.__getattribute__(candidate, "__module__")
    except (AttributeError, TypeError):
        return False
    return (
        type(mro) is tuple
        and type(declared_name) is str
        and declared_name == class_name
        and type(declared_module) is str
        and declared_module == module_name
    )


def _descriptor_is_owned_by(
    candidate: type,
    name: str,
    descriptor_types: tuple[type, ...],
) -> bool:
    namespace = _class_namespace(candidate)
    if namespace is None:
        return False
    descriptor = namespace.get(name, _MISSING)
    if not any(type(descriptor) is descriptor_type for descriptor_type in descriptor_types):
        return False
    try:
        return object.__getattribute__(descriptor, "__objclass__") is candidate
    except (AttributeError, TypeError):
        return False


def _is_static_extension_type(
    candidate: type,
    *,
    module_name: str,
    class_name: str,
    owned_descriptors: tuple[tuple[str, tuple[type, ...]], ...],
) -> bool:
    if not _declares_type(candidate, module_name=module_name, class_name=class_name):
        return False
    try:
        flags = type.__getattribute__(candidate, "__flags__")
        mro = type.__getattribute__(candidate, "__mro__")
    except (AttributeError, TypeError):
        return False
    return (
        type(flags) is int
        and flags & _PY_TPFLAGS_HEAPTYPE == 0
        and mro[0] is candidate
        and mro[-1] is object
        and all(_descriptor_is_owned_by(candidate, name, descriptor_types) for name, descriptor_types in owned_descriptors)
    )


def _resolved_alias_type(
    cache_key: str,
    aliases: tuple[tuple[str, str], ...],
    *,
    validator: Callable[[type], bool],
) -> type | None:
    cached = _CANONICAL_TYPE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    candidate = _loaded_module_attribute(*aliases[0])
    try:
        type.__getattribute__(candidate, "__mro__")
    except (AttributeError, TypeError):
        return None
    if any(_loaded_module_attribute(module_name, class_name) is not candidate for module_name, class_name in aliases[1:]):
        return None
    if not validator(candidate):
        return None
    _CANONICAL_TYPE_CACHE[cache_key] = candidate
    return candidate  # type: ignore[return-value]


def _canonical_ndarray_type() -> type | None:
    def validates(candidate: type) -> bool:
        return _is_static_extension_type(
            candidate,
            module_name="numpy",
            class_name="ndarray",
            owned_descriptors=(
                ("shape", (GetSetDescriptorType,)),
                ("__getitem__", (WrapperDescriptorType,)),
                ("item", (MethodDescriptorType,)),
                ("tolist", (MethodDescriptorType,)),
            ),
        )

    for internal_module in (
        "numpy._core._multiarray_umath",
        "numpy.core._multiarray_umath",
    ):
        resolved = _resolved_alias_type(
            "numpy.ndarray",
            ((internal_module, "ndarray"), ("numpy", "ndarray")),
            validator=validates,
        )
        if resolved is not None:
            return resolved
    return None


def _canonical_pandas_dataframe_type() -> type | None:
    def validates(candidate: type) -> bool:
        ndframe = _loaded_module_attribute("pandas.core.generic", "NDFrame")
        ops_mixin = _loaded_module_attribute("pandas.core.arraylike", "OpsMixin")
        if not _declares_type(candidate, module_name="pandas.core.frame", class_name="DataFrame"):
            return False
        if not _declares_type(ndframe, module_name="pandas.core.generic", class_name="NDFrame"):
            return False
        if not _declares_type(ops_mixin, module_name="pandas.core.arraylike", class_name="OpsMixin"):
            return False
        try:
            return type(candidate) is type and type.__getattribute__(candidate, "__bases__") == (ndframe, ops_mixin)
        except (AttributeError, TypeError):
            return False

    return _resolved_alias_type(
        "pandas.DataFrame",
        (("pandas.core.frame", "DataFrame"), ("pandas", "DataFrame")),
        validator=validates,
    )


def _canonical_pandas_index_type() -> type | None:
    def validates(candidate: type) -> bool:
        if not _declares_type(candidate, module_name="pandas.core.indexes.base", class_name="Index"):
            return False
        try:
            flags = type.__getattribute__(candidate, "__flags__")
            mro = type.__getattribute__(candidate, "__mro__")
        except (AttributeError, TypeError):
            return False
        return type(flags) is int and flags & _PY_TPFLAGS_HEAPTYPE != 0 and mro[0] is candidate and mro[-1] is object

    return _resolved_alias_type(
        "pandas.Index",
        (("pandas.core.indexes.base", "Index"), ("pandas", "Index")),
        validator=validates,
    )


def _canonical_pandas_range_index_type() -> type | None:
    index_type = _canonical_pandas_index_type()

    def validates(candidate: type) -> bool:
        if index_type is None or not _declares_type(
            candidate,
            module_name="pandas.core.indexes.range",
            class_name="RangeIndex",
        ):
            return False
        try:
            mro = type.__getattribute__(candidate, "__mro__")
        except (AttributeError, TypeError):
            return False
        return type(candidate) is type and mro[0] is candidate and index_type in mro

    return _resolved_alias_type(
        "pandas.RangeIndex",
        (("pandas.core.indexes.range", "RangeIndex"), ("pandas", "RangeIndex")),
        validator=validates,
    )


def _canonical_pydantic_base_model_type() -> type | None:
    def validates(candidate: type) -> bool:
        metaclass = _loaded_module_attribute(
            "pydantic._internal._model_construction",
            "ModelMetaclass",
        )
        if not _declares_type(candidate, module_name="pydantic.main", class_name="BaseModel"):
            return False
        if not _declares_type(
            metaclass,
            module_name="pydantic._internal._model_construction",
            class_name="ModelMetaclass",
        ):
            return False
        try:
            return type(candidate) is metaclass and type.__getattribute__(candidate, "__bases__") == (object,)
        except (AttributeError, TypeError):
            return False

    return _resolved_alias_type(
        "pydantic.BaseModel",
        (("pydantic.main", "BaseModel"), ("pydantic", "BaseModel")),
        validator=validates,
    )


def _is_loaded_pydantic_model(value: object) -> bool:
    base_model = _canonical_pydantic_base_model_type()
    if base_model is None:
        return False
    try:
        value_mro = type.__getattribute__(type(value), "__mro__")
    except (AttributeError, TypeError):
        return False
    return any(base is base_model for base in value_mro)


def _safe_original_size(value: object) -> int | None:
    value_type = type(value)
    if value_type is str:
        return str.__len__(value)
    if value_type is bytes:
        return bytes.__len__(value)
    if value_type is bytearray:
        return bytearray.__len__(value)
    if value_type is dict:
        return dict.__len__(value)
    if value_type is MappingProxyType:
        backing_dict = _mappingproxy_dict(value)
        return None if backing_dict is None else dict.__len__(backing_dict)
    if value_type is list:
        return list.__len__(value)
    if value_type is tuple:
        return tuple.__len__(value)
    return None


def _mappingproxy_dict(value: object) -> dict[object, object] | None:
    if type(value) is not MappingProxyType:
        return None
    referents = gc.get_referents(value)
    if len(referents) != 1 or type(referents[0]) is not dict:
        return None
    return referents[0]


def _class_namespace(value_type: type) -> dict[str, object] | None:
    try:
        namespace = type.__getattribute__(value_type, "__dict__")
    except (AttributeError, TypeError):
        return None
    return namespace  # type: ignore[return-value]


def _safe_mro(value_type: type) -> tuple[type, ...]:
    try:
        return type.__getattribute__(value_type, "__mro__")
    except (AttributeError, TypeError):
        return ()


def _bounded_dataclass_field_names(
    value: object,
) -> tuple[tuple[str, ...], int] | None:
    for owner in _safe_mro(type(value)):
        namespace = _class_namespace(owner)
        if namespace is None:
            continue
        dataclass_fields = namespace.get("__dataclass_fields__", _MISSING)
        if dataclass_fields is _MISSING:
            continue
        if type(dataclass_fields) is not dict:
            return None
        params = namespace.get("__dataclass_params__", _MISSING)
        if _DATACLASS_PARAMS_TYPE is None or type(params) is not _DATACLASS_PARAMS_TYPE:
            return None
        original_size = 0
        names: list[str] = []
        for name, field in dict.items(dataclass_fields):
            if type(name) is not str or type(field) is not dataclasses.Field:
                return None
            field_type = object.__getattribute__(field, "_field_type")
            if field_type is _DATACLASS_FIELD_MARKER:
                original_size += 1
                if len(names) < _MAX_MAPPING_ITEMS:
                    names.append(name)
        return tuple(names), original_size
    return None


def _stored_instance_dict(value: object) -> dict[object, object] | None:
    for owner in _safe_mro(type(value)):
        namespace = _class_namespace(owner)
        if namespace is None:
            continue
        descriptor = namespace.get("__dict__", _MISSING)
        if type(descriptor) is not GetSetDescriptorType:
            continue
        try:
            storage = GetSetDescriptorType.__get__(descriptor, value, type(value))
        except (AttributeError, TypeError):
            return None
        if type(storage) is dict:
            return storage
        return None
    return None


def _read_stored_field(value: object, name: str) -> tuple[bool, object]:
    storage = _stored_instance_dict(value)
    if storage is not None and dict.__contains__(storage, name):
        return True, dict.__getitem__(storage, name)

    for owner in _safe_mro(type(value)):
        namespace = _class_namespace(owner)
        if namespace is None:
            continue
        descriptor = namespace.get(name, _MISSING)
        if type(descriptor) is MemberDescriptorType:
            try:
                return True, MemberDescriptorType.__get__(descriptor, value, type(value))
            except (AttributeError, TypeError):
                return False, None
    return False, None


def _stored_dataclass_items(
    value: object,
) -> tuple[tuple[tuple[str, object], ...], int] | None:
    field_source = _bounded_dataclass_field_names(value)
    if field_source is None:
        return None
    field_names, original_size = field_source
    stored_items: list[tuple[str, object]] = []
    for field_name in field_names:
        found, field_value = _read_stored_field(value, field_name)
        if not found:
            return None
        stored_items.append((field_name, field_value))
    return tuple(stored_items), original_size


def _stored_pydantic_items(
    value: object,
) -> tuple[tuple[tuple[object, object], ...], int] | None:
    if not _is_loaded_pydantic_model(value):
        return None
    storage = _stored_instance_dict(value)
    if storage is None:
        return None
    original_size = dict.__len__(storage)
    source_items = tuple(
        islice(
            dict.items(storage),
            _MAX_MAPPING_ITEMS,
        )
    )
    return source_items, original_size


def _canonical_static_type(
    cache_key: str,
    module_name: str,
    class_name: str,
    *,
    owned_descriptors: tuple[tuple[str, tuple[type, ...]], ...],
) -> type | None:
    return _resolved_alias_type(
        cache_key,
        ((module_name, class_name),),
        validator=lambda candidate: _is_static_extension_type(
            candidate,
            module_name=module_name,
            class_name=class_name,
            owned_descriptors=owned_descriptors,
        ),
    )


def _trusted_getset(owner_type: type, value: object, name: str) -> tuple[bool, object]:
    namespace = _class_namespace(owner_type)
    if namespace is None:
        return False, None
    descriptor = namespace.get(name, _MISSING)
    if type(descriptor) is not GetSetDescriptorType:
        return False, None
    try:
        if object.__getattribute__(descriptor, "__objclass__") is not owner_type:
            return False, None
        return True, GetSetDescriptorType.__get__(descriptor, value, type(value))
    except BaseException:
        return False, None


def _trusted_ndarray_shape(value: object, ndarray_type: type) -> tuple[int, ...] | None:
    found, shape = _trusted_getset(ndarray_type, value, "shape")
    if not found or type(shape) is not tuple:
        return None
    dimensions = tuple(tuple.__iter__(shape))
    if any(type(dimension) is not int or dimension < 0 or dimension > _MAX_CONTAINER_SIZE for dimension in dimensions):
        return None
    return dimensions


def _trusted_ndarray_item(
    value: object,
    ndarray_type: type,
    *indexes: int,
) -> tuple[bool, object]:
    namespace = _class_namespace(ndarray_type)
    if namespace is None:
        return False, None
    descriptor = namespace.get("item", _MISSING)
    if type(descriptor) is not MethodDescriptorType:
        return False, None
    try:
        if object.__getattribute__(descriptor, "__objclass__") is not ndarray_type:
            return False, None
        method = MethodDescriptorType.__get__(descriptor, value, ndarray_type)
        return True, method(*indexes)
    except BaseException:
        return False, None


def _trusted_python_pandas_type(
    candidate: type,
    *,
    module_name: str,
    class_name: str,
    c_base: type,
) -> bool:
    if not _declares_type(candidate, module_name=module_name, class_name=class_name):
        return False
    if _loaded_module_attribute(module_name, class_name) is not candidate:
        return False
    try:
        mro = type.__getattribute__(candidate, "__mro__")
    except (AttributeError, TypeError):
        return False
    return type(candidate) is type and mro[0] is candidate and c_base in mro


def _trusted_pandas_block_type(candidate: type, c_bases: tuple[type, ...]) -> bool:
    try:
        module_name = type.__getattribute__(candidate, "__module__")
        class_name = type.__getattribute__(candidate, "__name__")
        mro = type.__getattribute__(candidate, "__mro__")
    except (AttributeError, TypeError):
        return False
    return (
        type(candidate) is type
        and type(module_name) is str
        and module_name == "pandas.core.internals.blocks"
        and type(class_name) is str
        and _loaded_module_attribute(module_name, class_name) is candidate
        and type(mro) is tuple
        and mro[0] is candidate
        and any(c_base in mro for c_base in c_bases)
    )


def _pandas_block_descriptor_owner(
    block_type: type,
    owners: tuple[type, ...],
) -> type | None:
    try:
        mro = type.__getattribute__(block_type, "__mro__")
    except (AttributeError, TypeError):
        return None
    return next((owner for owner in owners if owner in mro), None)


def _trusted_pandas_axis(
    axis: object,
    *,
    capture_values: bool,
    ndarray_type: type,
    index_type: type,
    range_index_type: type,
) -> tuple[int, tuple[object, ...]] | str:
    storage = _stored_instance_dict(axis)
    if storage is None:
        return "unsupported DataFrame storage"
    if type(axis) is range_index_type:
        axis_range = dict.get(storage, "_range", _MISSING)
        if type(axis_range) is not range:
            return "unsupported DataFrame storage"
        size = range.__len__(axis_range)
        values = tuple(islice(range.__iter__(axis_range), _MAX_TABLE_COLUMNS)) if capture_values else ()
        return size, values
    if type(axis) is not index_type:
        return "unsupported DataFrame storage"
    axis_values = dict.get(storage, "_data", _MISSING)
    if type(axis_values) is not ndarray_type:
        return "unsupported extension-backed DataFrame"
    shape = _trusted_ndarray_shape(axis_values, ndarray_type)
    if shape is None or len(shape) != 1:
        return "unsupported DataFrame storage"
    if not capture_values:
        return shape[0], ()
    values: list[object] = []
    for index in range(min(shape[0], _MAX_TABLE_COLUMNS)):
        found, item = _trusted_ndarray_item(axis_values, ndarray_type, index)
        if not found:
            return "unsupported DataFrame storage"
        values.append(item)
    return shape[0], tuple(values)


def _trusted_block_placement(
    placement: object,
    *,
    placement_type: type,
    ndarray_type: type,
    column_count: int,
    remaining_scan: int,
) -> tuple[int, tuple[tuple[int, int], ...], int] | str:
    found, indexer = _trusted_getset(placement_type, placement, "indexer")
    if not found:
        return "unsupported DataFrame storage"
    displayed: list[tuple[int, int]] = []
    if type(indexer) is slice:
        try:
            positions = range(*slice.indices(indexer, column_count))
        except (OverflowError, ValueError):
            return "unsupported DataFrame storage"
        if positions.step <= 0:
            return "unsupported DataFrame storage"
        placement_count = range.__len__(positions)
        for block_offset, column_index in enumerate(positions):
            if column_index >= _MAX_TABLE_COLUMNS:
                break
            displayed.append((block_offset, column_index))
        return placement_count, tuple(displayed), 0
    if type(indexer) is not ndarray_type:
        return "unsupported DataFrame storage"
    shape = _trusted_ndarray_shape(indexer, ndarray_type)
    if shape is None or len(shape) != 1:
        return "unsupported DataFrame storage"
    placement_count = shape[0]
    if placement_count > remaining_scan:
        return f"DataFrame storage exceeds {_MAX_PANDAS_PLACEMENTS_TO_SCAN}-placement inspection limit"
    for block_offset in range(placement_count):
        found, column_index = _trusted_ndarray_item(indexer, ndarray_type, block_offset)
        if not found or type(column_index) is not int or not 0 <= column_index < column_count:
            return "unsupported DataFrame storage"
        if column_index < _MAX_TABLE_COLUMNS:
            displayed.append((block_offset, column_index))
    return placement_count, tuple(displayed), placement_count


def _pandas_table_storage(value: object) -> _PandasTableStorage | str:
    ndarray_type = _canonical_ndarray_type()
    index_type = _canonical_pandas_index_type()
    range_index_type = _canonical_pandas_range_index_type()
    manager_base = _canonical_static_type(
        "pandas._libs.internals.BlockManager",
        "pandas._libs.internals",
        "BlockManager",
        owned_descriptors=(
            ("blocks", (GetSetDescriptorType,)),
            ("axes", (GetSetDescriptorType,)),
        ),
    )
    block_values_owner = _canonical_static_type(
        "pandas._libs.internals.Block.values",
        "pandas._libs.internals",
        "Block",
        owned_descriptors=(("values", (GetSetDescriptorType,)),),
    )
    legacy_numpy_values_owner = _canonical_static_type(
        "pandas._libs.internals.NumpyBlock.values",
        "pandas._libs.internals",
        "NumpyBlock",
        owned_descriptors=(("values", (GetSetDescriptorType,)),),
    )
    block_placement_owner = _canonical_static_type(
        "pandas._libs.internals.Block._mgr_locs",
        "pandas._libs.internals",
        "Block",
        owned_descriptors=(("_mgr_locs", (GetSetDescriptorType,)),),
    )
    legacy_placement_owner = _canonical_static_type(
        "pandas._libs.internals.SharedBlock._mgr_locs",
        "pandas._libs.internals",
        "SharedBlock",
        owned_descriptors=(("_mgr_locs", (GetSetDescriptorType,)),),
    )
    placement_type = _canonical_static_type(
        "pandas._libs.internals.BlockPlacement",
        "pandas._libs.internals",
        "BlockPlacement",
        owned_descriptors=(
            ("indexer", (GetSetDescriptorType,)),
            ("as_array", (GetSetDescriptorType,)),
        ),
    )
    if any(
        candidate is None
        for candidate in (
            ndarray_type,
            index_type,
            range_index_type,
            manager_base,
            placement_type,
        )
    ):
        return "unsupported DataFrame storage"
    values_owners = tuple(owner for owner in (legacy_numpy_values_owner, block_values_owner) if owner is not None)
    placement_owners = tuple(owner for owner in (legacy_placement_owner, block_placement_owner) if owner is not None)
    if not values_owners or not placement_owners:
        return "unsupported DataFrame storage"
    c_bases = values_owners + tuple(owner for owner in placement_owners if all(owner is not existing for existing in values_owners))
    assert ndarray_type is not None
    assert index_type is not None
    assert range_index_type is not None
    assert manager_base is not None
    assert placement_type is not None

    storage = _stored_instance_dict(value)
    if storage is None:
        return "unsupported DataFrame storage"
    manager = dict.get(storage, "_mgr", _MISSING)
    manager_type = type(manager)
    if not _trusted_python_pandas_type(
        manager_type,
        module_name="pandas.core.internals.managers",
        class_name="BlockManager",
        c_base=manager_base,
    ):
        return "unsupported DataFrame storage"

    found_blocks, blocks = _trusted_getset(manager_base, manager, "blocks")
    found_axes, axes = _trusted_getset(manager_base, manager, "axes")
    if not found_blocks or type(blocks) is not tuple or not found_axes or type(axes) is not list or list.__len__(axes) != 2:
        return "unsupported DataFrame storage"
    block_count = tuple.__len__(blocks)
    if block_count > _MAX_PANDAS_BLOCKS:
        return f"DataFrame storage exceeds {_MAX_PANDAS_BLOCKS}-block inspection limit"

    columns_result = _trusted_pandas_axis(
        list.__getitem__(axes, 0),
        capture_values=True,
        ndarray_type=ndarray_type,
        index_type=index_type,
        range_index_type=range_index_type,
    )
    if type(columns_result) is str:
        return columns_result
    column_count, columns = columns_result
    rows_result = _trusted_pandas_axis(
        list.__getitem__(axes, 1),
        capture_values=False,
        ndarray_type=ndarray_type,
        index_type=index_type,
        range_index_type=range_index_type,
    )
    if type(rows_result) is str:
        return rows_result
    row_count, _ = rows_result

    displayed_column_count = min(column_count, _MAX_TABLE_COLUMNS)
    displayed_row_count = min(row_count, _MAX_TABLE_ROWS)
    matrix: list[list[object]] = [[_MISSING for _ in range(displayed_column_count)] for _ in range(displayed_row_count)]
    placement_total = 0
    placements_scanned = 0
    for block in tuple.__iter__(blocks):
        block_type = type(block)
        if not _trusted_pandas_block_type(block_type, c_bases):
            return "unsupported DataFrame storage"
        values_owner = _pandas_block_descriptor_owner(block_type, values_owners)
        placement_owner = _pandas_block_descriptor_owner(block_type, placement_owners)
        if values_owner is None or placement_owner is None:
            return "unsupported DataFrame storage"
        found_values, block_values = _trusted_getset(values_owner, block, "values")
        if not found_values:
            return "unsupported DataFrame storage"
        if type(block_values) is not ndarray_type:
            return "unsupported extension-backed DataFrame"
        block_shape = _trusted_ndarray_shape(block_values, ndarray_type)
        if block_shape is None or len(block_shape) != 2 or block_shape[1] != row_count:
            return "unsupported DataFrame storage"
        found_placement, placement = _trusted_getset(
            placement_owner,
            block,
            "_mgr_locs",
        )
        if not found_placement or type(placement) is not placement_type:
            return "unsupported DataFrame storage"
        placement_result = _trusted_block_placement(
            placement,
            placement_type=placement_type,
            ndarray_type=ndarray_type,
            column_count=column_count,
            remaining_scan=_MAX_PANDAS_PLACEMENTS_TO_SCAN - placements_scanned,
        )
        if type(placement_result) is str:
            return placement_result
        placement_count, displayed_positions, scanned_count = placement_result
        if placement_count != block_shape[0]:
            return "unsupported DataFrame storage"
        placement_total += placement_count
        placements_scanned += scanned_count
        for block_offset, column_index in displayed_positions:
            for row_index in range(displayed_row_count):
                if matrix[row_index][column_index] is not _MISSING:
                    return "unsupported DataFrame storage"
                found_item, item = _trusted_ndarray_item(
                    block_values,
                    ndarray_type,
                    block_offset,
                    row_index,
                )
                if not found_item:
                    return "unsupported DataFrame storage"
                matrix[row_index][column_index] = item
    if placement_total != column_count:
        return "unsupported DataFrame storage"
    if any(item is _MISSING for row in matrix for item in row):
        return "unsupported DataFrame storage"
    return _PandasTableStorage(
        columns=columns,
        rows=tuple(tuple(row) for row in matrix),
        original_row_count=row_count,
        original_column_count=column_count,
    )


def _bounded_string_repr_size(value: str, characters_remaining: int) -> int | None:
    value_size = str.__len__(value)
    if value_size + 2 > characters_remaining:
        return None
    repr_size = 2
    for index in range(value_size):
        character = str.__getitem__(value, index)
        if character == "\\" or character == "'" or character == '"':
            repr_size += 2
        elif str.isprintable(character):
            repr_size += 1
        else:
            repr_size += 10
        if repr_size > characters_remaining:
            return None
    return repr_size


def _bounded_exception_repr_size(
    value: object,
    *,
    depth: int,
    items_remaining: int,
    characters_remaining: int,
) -> tuple[int, int] | None:
    if items_remaining <= 0 or characters_remaining <= 0:
        return None
    value_type = type(value)
    if value_type is str:
        repr_size = _bounded_string_repr_size(value, characters_remaining)
        if repr_size is None:
            return None
    elif value_type is bytes:
        repr_size = bytes.__len__(value) * 4 + 3
    elif value_type is int:
        bit_count = int.bit_length(value)
        repr_size = 1 if bit_count == 0 else bit_count * 30_103 // 100_000 + 2
        if value < 0:
            repr_size += 1
    elif value_type is float:
        repr_size = 32
    elif value_type is bool:
        repr_size = 5
    elif value is None:
        repr_size = 4
    elif value_type is tuple:
        if depth >= _MAX_DEPTH:
            return None
        item_count = tuple.__len__(value)
        if item_count > min(_MAX_SEQUENCE_ITEMS, items_remaining - 1):
            return None
        separator_size = 2 * max(item_count - 1, 0)
        trailing_comma_size = 1 if item_count == 1 else 0
        repr_size = 2 + separator_size + trailing_comma_size
        if repr_size > characters_remaining:
            return None
        consumed_items = 1
        for item in tuple.__iter__(value):
            item_result = _bounded_exception_repr_size(
                item,
                depth=depth + 1,
                items_remaining=items_remaining - consumed_items,
                characters_remaining=characters_remaining - repr_size,
            )
            if item_result is None:
                return None
            item_size, item_count = item_result
            repr_size += item_size
            consumed_items += item_count
        return repr_size, consumed_items
    else:
        return None
    if repr_size > characters_remaining:
        return None
    return repr_size, 1


def _exception_state_is_bounded(
    value: object,
    arguments: tuple[object, ...],
    *,
    preflight_arguments: bool = True,
) -> bool:
    characters_remaining = _MAX_TEXT_CHARACTERS - _MAX_EXCEPTION_FORMAT_OVERHEAD
    items_remaining = _MAX_SEQUENCE_ITEMS + 1
    if preflight_arguments:
        arguments_result = _bounded_exception_repr_size(
            arguments,
            depth=0,
            items_remaining=items_remaining,
            characters_remaining=characters_remaining,
        )
        if arguments_result is None:
            return False
        argument_size, argument_items = arguments_result
        characters_remaining -= argument_size
        items_remaining -= argument_items

    try:
        cause = BaseException.__cause__.__get__(value, type(value))
        context = BaseException.__context__.__get__(value, type(value))
        traceback = BaseException.__traceback__.__get__(value, type(value))
    except (AttributeError, TypeError):
        return False
    remaining_control_referents = [referent for referent in (arguments, cause, context, traceback) if referent is not None]

    referents = gc.get_referents(value)
    if type(referents) is not list or list.__len__(referents) > _MAX_SEQUENCE_ITEMS:
        return False
    for referent in list.__iter__(referents):
        control_index = next(
            (index for index, control_referent in enumerate(remaining_control_referents) if referent is control_referent),
            None,
        )
        if control_index is not None:
            del remaining_control_referents[control_index]
            continue
        referent_type = type(referent)
        if referent_type is dict:
            continue
        referent_result = _bounded_exception_repr_size(
            referent,
            depth=0,
            items_remaining=items_remaining,
            characters_remaining=characters_remaining,
        )
        if referent_result is None:
            return False
        referent_size, referent_items = referent_result
        characters_remaining -= referent_size
        items_remaining -= referent_items
    return True


def _bounded_exception_text(
    value: object,
    arguments: tuple[object, ...],
) -> str | None:
    value_formatter = type.__getattribute__(type(value), "__str__")
    if value_formatter is BaseException.__str__ and tuple.__len__(arguments) == 1 and type(tuple.__getitem__(arguments, 0)) is str:
        if not _exception_state_is_bounded(
            value,
            arguments,
            preflight_arguments=False,
        ):
            return None
        return str.__str__(tuple.__getitem__(arguments, 0))
    if not _exception_state_is_bounded(value, arguments):
        return None
    try:
        return str(value)
    except BaseException:
        return None


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


def _budget_placeholder(
    value: object,
    *,
    original_size: int | None = None,
) -> SerializedValue:
    return _placeholder(
        value,
        reason=_SERIALIZATION_BUDGET_EXHAUSTED,
        original_size=original_size,
    )


def _serialize_text(
    text: str,
    *,
    type_name: str,
    budget: _SerializationBudget,
) -> SerializedValue:
    try:
        text = str.__str__(text)
    except BaseException as error:
        return _failed_value(text, operation="text normalization", error=error)
    original_size = str.__len__(text)
    captured_size = min(original_size, _MAX_TEXT_CHARACTERS)
    captured_text = str.__getitem__(text, slice(None, captured_size))
    try:
        str.encode(captured_text, "utf-8", errors="strict")
    except UnicodeEncodeError:
        return SerializedValue(
            kind="placeholder",
            type_name=type_name,
            original_size=original_size,
            truncated=True,
            reason="invalid Unicode",
        )
    if not budget.claim_text(captured_size):
        return SerializedValue(
            kind="placeholder",
            type_name=type_name,
            original_size=original_size,
            truncated=True,
            reason=_SERIALIZATION_BUDGET_EXHAUSTED,
        )
    return SerializedValue(
        kind="text",
        type_name=type_name,
        text=captured_text,
        original_size=original_size,
        truncated=original_size > _MAX_TEXT_CHARACTERS,
    )


def _serialize_number(
    value: int | float,
    *,
    type_name: Literal["int", "float"],
    budget: _SerializationBudget,
) -> SerializedValue:
    try:
        encoded = json.dumps(value, allow_nan=False)
    except BaseException as error:
        return _failed_value(value, operation="number encoding", error=error)
    if not budget.claim_text(len(encoded)):
        return _budget_placeholder(value)
    return SerializedValue(kind="number", type_name=type_name, value=value)


def _serialize_mapping_items(
    value: object,
    *,
    source_items: tuple[tuple[object, object], ...],
    original_size: int,
    depth: int,
    active_ids: set[int],
    budget: _SerializationBudget,
) -> SerializedValue:
    limit_hits_before = budget.limit_hits
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
        for key, item in source_items:
            serialized_key = _serialize_value(
                key,
                depth=depth + 1,
                active_ids=active_ids,
                budget=budget,
            )
            if budget.work_exhausted:
                entries.append(
                    SerializedEntry(
                        key=serialized_key,
                        value=_budget_placeholder(item),
                    )
                )
                break
            serialized_item = _serialize_value(
                item,
                depth=depth + 1,
                active_ids=active_ids,
                budget=budget,
            )
            entries.append(
                SerializedEntry(
                    key=serialized_key,
                    value=serialized_item,
                )
            )
            if budget.work_exhausted:
                break
        return SerializedValue(
            kind="mapping",
            type_name=_safe_type_name(value),
            entries=tuple(entries),
            original_size=original_size,
            truncated=(original_size > len(entries) or budget.limit_hits > limit_hits_before),
        )
    finally:
        active_ids.discard(object_id)


def _is_safe_row_key(key: object) -> bool:
    key_type = type(key)
    return any(key_type is safe_type for safe_type in _SAFE_ROW_KEY_TYPES)


def _row_keys_match(left: object, right: object) -> bool:
    if left is right:
        return True
    if _is_safe_row_key(left) and _is_safe_row_key(right):
        return left == right
    return False


def _row_table_schema(
    value: object,
    *,
    source_rows: tuple[object, ...],
    original_row_count: int,
) -> tuple[_RowTableSchema | None, SerializedValue | None]:
    ordered_keys: list[object] = []
    safe_keys: set[object] = set()
    identity_keys: set[int] = set()
    captured_rows: list[tuple[_RowTableItem, ...]] = []
    largest_row_count = 0
    exact = original_row_count == len(source_rows)

    for source_row in source_rows:
        assert type(source_row) is dict
        row_count = dict.__len__(source_row)
        largest_row_count = max(largest_row_count, row_count)
        if row_count > _MAX_TABLE_COLUMNS:
            exact = False
        captured_count = min(row_count, _MAX_TABLE_COLUMNS)
        row_items = tuple(_RowTableItem(key=key, value=item) for key, item in islice(dict.items(source_row), captured_count))
        captured_rows.append(row_items)
        if len(row_items) != captured_count:
            exact = False
        for row_item in row_items:
            key = row_item.key
            if _is_safe_row_key(key):
                if key in safe_keys:
                    continue
                safe_keys.add(key)
            else:
                exact = False
                key_id = id(key)
                if key_id in identity_keys:
                    continue
                identity_keys.add(key_id)
            ordered_keys.append(key)

    original_column_count = len(safe_keys) if exact else max(len(safe_keys), largest_row_count)
    return (
        _RowTableSchema(
            displayed_keys=tuple(ordered_keys[:_MAX_TABLE_COLUMNS]),
            row_items=tuple(captured_rows),
            original_column_count=original_column_count,
            original_column_count_exact=exact,
        ),
        None,
    )


def _serialize_row_table(
    value: object,
    *,
    source_rows: tuple[object, ...],
    original_row_count: int,
    depth: int,
    active_ids: set[int],
    budget: _SerializationBudget,
) -> SerializedValue:
    limit_hits_before = budget.limit_hits
    schema, schema_failure = _row_table_schema(
        value,
        source_rows=source_rows,
        original_row_count=original_row_count,
    )
    if schema_failure is not None:
        return schema_failure
    assert schema is not None
    column_keys = schema.displayed_keys
    columns: list[SerializedValue] = []
    for key in column_keys:
        columns.append(
            _serialize_value(
                key,
                depth=depth + 1,
                active_ids=active_ids,
                budget=budget,
            )
        )
        if budget.work_exhausted:
            break
    rows: list[SerializedTableRow] = []
    for source_row, row_items in zip(source_rows, schema.row_items, strict=True):
        cells: list[SerializedValue] = []
        for key in column_keys[: len(columns)]:
            matched_item = next(
                (item for item in row_items if _row_keys_match(item.key, key)),
                None,
            )
            if matched_item is None:
                if budget.claim_node():
                    cells.append(
                        SerializedValue(
                            kind="placeholder",
                            type_name="missing",
                            truncated=True,
                            reason="missing table cell",
                        )
                    )
                else:
                    cells.append(_budget_placeholder(source_row))
                    break
                continue
            if matched_item.failure is not None:
                cells.append(matched_item.failure)
                continue
            if matched_item.missing:
                if budget.claim_node():
                    cells.append(
                        SerializedValue(
                            kind="placeholder",
                            type_name="missing",
                            truncated=True,
                            reason="missing table cell",
                        )
                    )
                else:
                    cells.append(_budget_placeholder(source_row))
                    break
                continue
            cell = matched_item.value
            cells.append(
                _serialize_value(
                    cell,
                    depth=depth + 1,
                    active_ids=active_ids,
                    budget=budget,
                )
            )
            if budget.work_exhausted:
                break
        rows.append(SerializedTableRow(cells=tuple(cells)))
        if budget.work_exhausted:
            break

    rows_truncated = original_row_count > len(rows)
    columns_truncated = not schema.original_column_count_exact or schema.original_column_count > _MAX_TABLE_COLUMNS or len(columns) < len(column_keys)
    return SerializedValue(
        kind="table",
        type_name=_safe_type_name(value),
        table=SerializedTable(
            columns=tuple(columns),
            rows=tuple(rows),
            original_row_count=original_row_count,
            original_column_count=schema.original_column_count,
            original_column_count_exact=schema.original_column_count_exact,
            rows_truncated=rows_truncated,
            columns_truncated=columns_truncated,
        ),
        original_size=original_row_count,
        truncated=rows_truncated or columns_truncated or budget.limit_hits > limit_hits_before,
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
    budget: _SerializationBudget,
) -> SerializedValue:
    limit_hits_before = budget.limit_hits
    columns: list[SerializedValue] = []
    for column in source_columns:
        columns.append(
            _serialize_value(
                column,
                depth=depth + 1,
                active_ids=active_ids,
                budget=budget,
            )
        )
        if budget.work_exhausted:
            break
    rows: list[SerializedTableRow] = []
    for source_row in source_rows:
        if type(source_row) is list:
            cells = tuple(islice(list.__iter__(source_row), len(columns)))
        elif type(source_row) is tuple:
            cells = tuple(islice(tuple.__iter__(source_row), len(columns)))
        else:
            return _placeholder(
                value,
                reason="trusted table adapter returned an unsupported row",
                original_size=original_row_count,
            )
        serialized_cells: list[SerializedValue] = []
        for cell in cells:
            serialized_cells.append(
                _serialize_value(
                    cell,
                    depth=depth + 1,
                    active_ids=active_ids,
                    budget=budget,
                )
            )
            if budget.work_exhausted:
                break
        while len(serialized_cells) < len(columns):
            if budget.claim_node():
                serialized_cells.append(
                    SerializedValue(
                        kind="placeholder",
                        type_name="missing",
                        truncated=True,
                        reason="missing table cell",
                    )
                )
            else:
                serialized_cells.append(_budget_placeholder(source_row))
                break
        rows.append(SerializedTableRow(cells=tuple(serialized_cells)))
        if budget.work_exhausted:
            break

    rows_truncated = original_row_count > len(rows)
    columns_truncated = original_column_count > _MAX_TABLE_COLUMNS or len(columns) < len(source_columns)
    return SerializedValue(
        kind="table",
        type_name=_safe_type_name(value),
        table=SerializedTable(
            columns=tuple(columns),
            rows=tuple(rows),
            original_row_count=original_row_count,
            original_column_count=original_column_count,
            original_column_count_exact=True,
            rows_truncated=rows_truncated,
            columns_truncated=columns_truncated,
        ),
        original_size=original_row_count,
        truncated=rows_truncated or columns_truncated or budget.limit_hits > limit_hits_before,
    )


def _validated_shape(
    value: object,
    shape: object,
) -> tuple[tuple[int, ...] | None, SerializedValue | None]:
    if type(shape) is not tuple:
        return None, _placeholder(value, reason="trusted adapter returned an invalid shape")
    rank = tuple.__len__(shape)
    dimensions = tuple(islice(tuple.__iter__(shape), 3))
    if rank not in {1, 2} or len(dimensions) != rank:
        return None, _placeholder(value, reason=f"array rank {rank} exceeds supported rank 2")
    if any(type(dimension) is not int or dimension < 0 for dimension in dimensions):
        return None, _placeholder(value, reason="invalid array shape")
    if any(dimension > _MAX_CONTAINER_SIZE for dimension in dimensions):
        return None, _placeholder(
            value,
            reason="array dimension exceeds platform container size",
        )
    return dimensions, None


def _serialize_pandas_dataframe(
    value: object,
    *,
    storage: _PandasTableStorage,
    depth: int,
    active_ids: set[int],
    budget: _SerializationBudget,
) -> SerializedValue:
    return _serialize_matrix_table(
        value,
        source_columns=storage.columns,
        source_rows=storage.rows,
        original_row_count=storage.original_row_count,
        original_column_count=storage.original_column_count,
        depth=depth,
        active_ids=active_ids,
        budget=budget,
    )


def _serialize_numpy_array(
    value: object,
    *,
    depth: int,
    active_ids: set[int],
    budget: _SerializationBudget,
) -> SerializedValue:
    ndarray_type = _canonical_ndarray_type()
    if ndarray_type is None or type(value) is not ndarray_type:
        return _placeholder(value, reason="unsupported NumPy storage")
    trusted_shape = _trusted_ndarray_shape(value, ndarray_type)
    shape, shape_failure = _validated_shape(value, trusted_shape)
    if shape_failure is not None:
        return shape_failure
    assert shape is not None
    if len(shape) == 1:
        source_items: list[object] = []
        for index in range(min(shape[0], _MAX_SEQUENCE_ITEMS)):
            found, item = _trusted_ndarray_item(value, ndarray_type, index)
            if not found:
                return _placeholder(value, reason="trusted NumPy storage could not be read")
            source_items.append(item)
        limit_hits_before = budget.limit_hits
        items: list[SerializedValue] = []
        for item in source_items:
            items.append(
                _serialize_value(
                    item,
                    depth=depth + 1,
                    active_ids=active_ids,
                    budget=budget,
                )
            )
            if budget.work_exhausted:
                break
        return SerializedValue(
            kind="sequence",
            type_name=_safe_type_name(value),
            items=tuple(items),
            original_size=shape[0],
            truncated=shape[0] > len(items) or budget.limit_hits > limit_hits_before,
        )
    source_rows: list[tuple[object, ...]] = []
    for row_index in range(min(shape[0], _MAX_TABLE_ROWS)):
        row: list[object] = []
        for column_index in range(min(shape[1], _MAX_TABLE_COLUMNS)):
            found, item = _trusted_ndarray_item(
                value,
                ndarray_type,
                row_index,
                column_index,
            )
            if not found:
                return _placeholder(value, reason="trusted NumPy storage could not be read")
            row.append(item)
        source_rows.append(tuple(row))
    return _serialize_matrix_table(
        value,
        source_columns=tuple(range(min(shape[1], _MAX_TABLE_COLUMNS))),
        source_rows=tuple(source_rows),
        original_row_count=shape[0],
        original_column_count=shape[1],
        depth=depth,
        active_ids=active_ids,
        budget=budget,
    )


def _serialize_value(
    value: object,
    *,
    depth: int,
    active_ids: set[int],
    budget: _SerializationBudget,
) -> SerializedValue:
    if not budget.claim_node():
        return _budget_placeholder(
            value,
            original_size=_safe_original_size(value),
        )
    if depth > _MAX_DEPTH:
        return _placeholder(
            value,
            reason=f"depth limit {_MAX_DEPTH} exceeded",
            original_size=_safe_original_size(value),
        )

    value_type = type(value)
    if value_type is bool:
        return SerializedValue(kind="boolean", type_name="bool", value=value)
    if value_type is int:
        if int.bit_length(value) > _MAX_JSON_INTEGER_BITS:
            return _placeholder(
                value,
                reason=f"number exceeds {_MAX_TEXT_CHARACTERS}-character limit",
            )
        return _serialize_number(value, type_name="int", budget=budget)
    if value_type is float:
        if math.isfinite(value):
            return _serialize_number(value, type_name="float", budget=budget)
        text = "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
        return _serialize_text(text, type_name="float", budget=budget)
    if value is None:
        return SerializedValue(kind="null", type_name="NoneType")
    if value_type is datetime:
        return _serialize_text(
            datetime.isoformat(value),
            type_name="datetime",
            budget=budget,
        )
    if value_type is date:
        return _serialize_text(
            date.isoformat(value),
            type_name="date",
            budget=budget,
        )
    if value_type is time:
        return _serialize_text(
            time.isoformat(value),
            type_name="time",
            budget=budget,
        )
    if value_type is str:
        return _serialize_text(value, type_name="str", budget=budget)
    if any(value_type is exception_type for exception_type in _SAFE_EXCEPTION_TYPES):
        args = BaseException.args.__get__(value, value_type)
        if type(args) is not tuple:
            return _placeholder(
                value,
                reason="exception contains unsupported arguments",
            )
        text = _bounded_exception_text(value, args)
        if text is None:
            return _placeholder(
                value,
                reason="exception contains unsupported arguments",
            )
        serialized = _serialize_text(
            text,
            type_name=_safe_type_name(value),
            budget=budget,
        )
        if serialized.kind == "placeholder":
            return serialized
        return SerializedValue(
            kind="exception",
            type_name=serialized.type_name,
            text=serialized.text,
            original_size=serialized.original_size,
            truncated=serialized.truncated,
        )
    dataclass_items = _stored_dataclass_items(value)
    if dataclass_items is not None:
        source_items, original_size = dataclass_items
        return _serialize_mapping_items(
            value,
            source_items=source_items,
            original_size=original_size,
            depth=depth,
            active_ids=active_ids,
            budget=budget,
        )
    pydantic_items = _stored_pydantic_items(value)
    if pydantic_items is not None:
        source_items, original_size = pydantic_items
        return _serialize_mapping_items(
            value,
            source_items=source_items,
            original_size=original_size,
            depth=depth,
            active_ids=active_ids,
            budget=budget,
        )
    if value_type is dict:
        original_size = dict.__len__(value)
        source_items = tuple(islice(dict.items(value), _MAX_MAPPING_ITEMS))
        return _serialize_mapping_items(
            value,
            source_items=source_items,
            original_size=original_size,
            depth=depth,
            active_ids=active_ids,
            budget=budget,
        )
    if value_type is MappingProxyType:
        backing_dict = _mappingproxy_dict(value)
        if backing_dict is not None:
            original_size = dict.__len__(backing_dict)
            source_items = tuple(islice(dict.items(backing_dict), _MAX_MAPPING_ITEMS))
            return _serialize_mapping_items(
                value,
                source_items=source_items,
                original_size=original_size,
                depth=depth,
                active_ids=active_ids,
                budget=budget,
            )
    dataframe_type = _canonical_pandas_dataframe_type()
    if dataframe_type is not None and value_type is dataframe_type:
        pandas_storage = _pandas_table_storage(value)
        if type(pandas_storage) is str:
            return _placeholder(value, reason=pandas_storage)
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(value, reason="recursive reference")
        active_ids.add(object_id)
        try:
            return _serialize_pandas_dataframe(
                value,
                storage=pandas_storage,
                depth=depth,
                active_ids=active_ids,
                budget=budget,
            )
        finally:
            active_ids.discard(object_id)
    ndarray_type = _canonical_ndarray_type()
    if ndarray_type is not None and value_type is ndarray_type:
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(value, reason="recursive reference")
        active_ids.add(object_id)
        try:
            return _serialize_numpy_array(
                value,
                depth=depth,
                active_ids=active_ids,
                budget=budget,
            )
        finally:
            active_ids.discard(object_id)
    if value_type is list or value_type is tuple:
        limit_hits_before = budget.limit_hits
        if value_type is list:
            original_size = list.__len__(value)
            source_items = tuple(islice(list.__iter__(value), _MAX_SEQUENCE_ITEMS))
        else:
            original_size = tuple.__len__(value)
            source_items = tuple(islice(tuple.__iter__(value), _MAX_SEQUENCE_ITEMS))
        object_id = id(value)
        if object_id in active_ids:
            return _placeholder(
                value,
                reason="recursive reference",
                original_size=original_size,
            )
        active_ids.add(object_id)
        try:
            if source_items and all(type(item) is dict for item in source_items):
                return _serialize_row_table(
                    value,
                    source_rows=source_items,
                    original_row_count=original_size,
                    depth=depth,
                    active_ids=active_ids,
                    budget=budget,
                )
            items: list[SerializedValue] = []
            for item in source_items:
                items.append(
                    _serialize_value(
                        item,
                        depth=depth + 1,
                        active_ids=active_ids,
                        budget=budget,
                    )
                )
                if budget.work_exhausted:
                    break
            return SerializedValue(
                kind="sequence",
                type_name=_safe_type_name(value),
                items=tuple(items),
                original_size=original_size,
                truncated=(original_size > len(items) or budget.limit_hits > limit_hits_before),
            )
        finally:
            active_ids.discard(object_id)

    try:
        text = repr(value)
    except BaseException as error:
        return _failed_value(value, operation="repr", error=error)
    return _serialize_text(
        text,
        type_name=_safe_type_name(value),
        budget=budget,
    )


def serialize_value(value: object) -> SerializedValue:
    """Convert one Python value into an inert typed node."""

    try:
        return _serialize_value(
            value,
            depth=0,
            active_ids=set(),
            budget=_SerializationBudget(),
        )
    except BaseException as error:
        return _failed_value(value, operation="serialization", error=error)


def _count_to_wire(count: int) -> int | str:
    if count <= _MAX_JS_SAFE_INTEGER:
        return count
    return str(count)


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
        wire["original_size"] = _count_to_wire(value.original_size)
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
            "original_row_count": _count_to_wire(value.table.original_row_count),
            "original_column_count": _count_to_wire(value.table.original_column_count),
            "original_column_count_exact": value.table.original_column_count_exact,
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
