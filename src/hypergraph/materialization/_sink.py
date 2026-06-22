"""Sinks — consumers that persist a derive's streamed outputs (ADR 0002, L2).

A sink is a plain consumer object with a ``start`` / ``write`` / ``finalize``
lifecycle that declares which output port of the derive it persists. It is *not*
a graph node — the runner streams results and the sink writes them, keeping the
engine pure. ``LanceSink`` is the materialization sink: it owns row assembly and
write-new-then-delete-old persistence into a DerivedTable's LanceDB table.
"""

from __future__ import annotations

import contextlib
import dataclasses
import typing
from typing import Any, Protocol, runtime_checkable

import pyarrow as pa


def _escape(val: Any) -> str:
    return str(val).replace("'", "''")


def _get_field_type_raw(cls: type, field_name: str) -> type:
    hints = typing.get_type_hints(cls)
    return hints.get(field_name, str)


def _default_for_type(tp: type) -> Any:
    if tp is str:
        return ""
    if tp is int:
        return 0
    if tp is float:
        return 0.0
    if tp is bool:
        return False
    if hasattr(tp, "__origin__") and tp.__origin__ is list:
        return []
    return ""


@runtime_checkable
class Sink(Protocol):
    """Persists selected outputs from a derive's streamed run results.

    ``writes`` names the output port this sink persists; the runner feeds it that
    port from each result and everything else stays observable (e.g. in events).
    """

    writes: str

    def start(self) -> None: ...

    def write(self, result: Any, *, source_item: Any, content_key: str) -> list[Any]:
        """Persist the rows for one source item; return the persisted outputs."""
        ...

    def write_error(self, *, source_item: Any, content_key: str, error: BaseException) -> None:
        """Persist an error row for a failed source item."""
        ...

    def delete_superseded(self, source_id: str, content_key: str) -> None:
        """Delete prior rows for a source item whose content key changed."""
        ...

    def finalize(self) -> None: ...


class LanceSink:
    """Persists a derive's declared output into a DerivedTable's LanceDB table.

    Re-homes the row assembly and write-new-then-delete-old persistence that the
    sequential loop used to do inline, so the streamed and the legacy paths write
    identical rows.
    """

    def __init__(
        self,
        store: Any,
        output_cls: type,
        source_cls: type,
        markers: Any,
        is_root: bool,
        writes: str,
    ):
        self._store = store
        self._output_cls = output_cls
        self._source_cls = source_cls
        self._markers = markers
        self._is_root = is_root
        self.writes = writes

    def validate_against(self, output_names: typing.Iterable[str]) -> None:
        """Fail at construction if the declared port isn't produced by the derive."""
        names = set(output_names)
        if self.writes not in names:
            raise ValueError(
                f"Sink writes output '{self.writes}', but the derive produces {sorted(names)}. Name an output the derive actually produces."
            )

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    # -- persistence --------------------------------------------------------

    def _table(self):
        return self._store.get_table(self._output_cls)

    def _source_id(self, source_item: Any) -> str:
        vals = {f: getattr(source_item, f) for f in self._markers.identity_fields}
        return ":".join(str(vals[k]) for k in sorted(vals))

    def _output_to_row(
        self,
        result: Any,
        source_item: Any,
        content_key: str,
        is_error: bool = False,
        error_type: str = "",
        error_msg: str = "",
    ) -> dict:
        row = {}
        identity_fields = set(self._markers.identity_fields)
        for f in dataclasses.fields(self._output_cls):
            if is_error:
                if f.name in identity_fields and hasattr(source_item, f.name):
                    row[f.name] = getattr(source_item, f.name)
                else:
                    tp = _get_field_type_raw(self._output_cls, f.name)
                    row[f.name] = _default_for_type(tp)
            else:
                row[f.name] = getattr(result, f.name)

        row["_source_id"] = self._source_id(source_item)
        row["_content_key"] = content_key
        row["_error"] = is_error
        row["_error_type"] = error_type or ""
        row["_error_msg"] = error_msg or ""
        row["_version"] = 0

        if self._is_root:
            for f in dataclasses.fields(self._source_cls):
                row[f"_src_{f.name}"] = getattr(source_item, f.name)

        return row

    def _write_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        tbl = self._table()
        if tbl is None:
            return
        tbl.add(pa.Table.from_pylist(rows, schema=tbl.schema))

    def write(self, result: Any, *, source_item: Any, content_key: str) -> list[Any]:
        # ``result`` is a RunResult; persist the value(s) of our declared port.
        value = result[self.writes]
        outputs = value if isinstance(value, list) else [value]
        rows = [self._output_to_row(o, source_item, content_key) for o in outputs]
        self._write_rows(rows)
        return outputs

    def write_error(self, *, source_item: Any, content_key: str, error: BaseException) -> None:
        row = self._output_to_row(
            None,
            source_item,
            content_key,
            is_error=True,
            error_type=type(error).__name__,
            error_msg=str(error),
        )
        self._write_rows([row])

    def delete_superseded(self, source_id: str, content_key: str) -> None:
        tbl = self._table()
        if tbl is None:
            return
        with contextlib.suppress(Exception):
            tbl.delete(f"_source_id = '{_escape(source_id)}' AND _content_key != '{_escape(content_key)}'")
