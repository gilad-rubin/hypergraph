"""A node downstream of a reused structured column stays fresh.

A node's fresh output is a model or a dataclass. A store that keeps structured
cells as their plain data (any store that serializes them, as a production
store over JSON cells does) hands the same value back as a dict. A column
provenance must read both alike, or a node downstream of a reused structured
column is never fresh and re-runs on every reconcile.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from pydantic import BaseModel

from hypergraph import Graph, node
from hypergraph.materialization import SqliteTableStore
from hypergraph.materialization._fingerprint import compute_column_provenance
from hypergraph.materialization._lancedb_store import LanceDBStore

CALLS = {"use": 0}


class Meta(BaseModel):
    title: str
    tags: list[str]


@dataclasses.dataclass
class MetaData:
    title: str
    tags: list[str]


_JSON = "json:"


def _encode(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _JSON + json.dumps(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _JSON + json.dumps(dataclasses.asdict(value))
    if isinstance(value, dict):
        return _JSON + json.dumps(value)
    return value


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    return {key: json.loads(value[len(_JSON) :]) if isinstance(value, str) and value.startswith(_JSON) else value for key, value in row.items()}


class _JsonCells:
    """Keeps a structured cell as JSON text and reads it back as plain data."""

    def write_rows(self, table_name, rows):
        return super().write_rows(table_name, [{key: _encode(value) for key, value in row.items()} for row in rows])

    def read_rows(self, *args, **kwargs):
        return [_decode(row) for row in super().read_rows(*args, **kwargs)]

    def read_one(self, *args, **kwargs):
        row = super().read_one(*args, **kwargs)
        return None if row is None else _decode(row)


class JsonSqliteStore(_JsonCells, SqliteTableStore):
    pass


class JsonLanceStore(_JsonCells, LanceDBStore):
    pass


@pytest.fixture(params=["lance", "sqlite"])
def store(request, tmp_path):
    if request.param == "sqlite":
        s = JsonSqliteStore()
        yield s
        s.close()
    else:
        yield JsonLanceStore(str(tmp_path / "store"))


@pytest.fixture(autouse=True)
def _reset():
    CALLS["use"] = 0


@node(output_name="meta")
def model_meta(text: str) -> Meta:
    return Meta(title=text.upper(), tags=["b", "a"])


@node(output_name="meta")
def dataclass_meta(text: str) -> MetaData:
    return MetaData(title=text.upper(), tags=["b", "a"])


@node(output_name="headline")
def use_meta(meta: Any) -> str:
    CALLS["use"] += 1
    title = meta["title"] if isinstance(meta, dict) else meta.title
    return f"# {title}"


@node(output_name="label")
def label(tag: str) -> str:
    return tag.upper()


@pytest.mark.parametrize("producer", [model_meta, dataclass_meta], ids=["model", "dataclass"])
def test_a_node_downstream_of_a_reused_structured_column_is_not_rerun(store, producer):
    table = Graph([producer, use_meta, label]).as_table(identity="doc_id", store=store)
    table.insert(doc_id="d1", text="intro", tag="a")
    assert CALLS["use"] == 1
    CALLS["use"] = 0

    # `tag` feeds only `label`: `meta` is reused from its stored column (a dict
    # now), and `use_meta`'s inputs are unchanged.
    table.update("d1", tag="b")

    assert CALLS["use"] == 0
    assert table.get("d1")["headline"] == "# INTRO"
    assert table.get("d1")["label"] == "B"


def test_a_structured_input_reads_as_its_plain_data():
    as_model = compute_column_provenance(use_meta, {"meta": Meta(title="T", tags=["b", "a"])}, {})
    as_dataclass = compute_column_provenance(use_meta, {"meta": MetaData(title="T", tags=["b", "a"])}, {})
    as_dict = compute_column_provenance(use_meta, {"meta": {"tags": ["b", "a"], "title": "T"}}, {})
    as_list = compute_column_provenance(use_meta, {"meta": [Meta(title="T", tags=[])]}, {})
    as_list_of_dicts = compute_column_provenance(use_meta, {"meta": [{"title": "T", "tags": []}]}, {})

    assert as_model == as_dataclass == as_dict
    assert as_list == as_list_of_dicts
    assert compute_column_provenance(use_meta, {"meta": {"title": "U", "tags": ["b", "a"]}}, {}) != as_dict


def test_a_scalar_input_reads_as_it_always_did():
    # A scalar keeps its type in the provenance: 1 and "1" are different inputs.
    assert compute_column_provenance(use_meta, {"meta": 1}, {}) != compute_column_provenance(use_meta, {"meta": "1"}, {})
