# Implementing a HyperTable store

`TableStore` is the synchronous persistence boundary beneath HyperTable and
plain `Table`. The graph runner may be synchronous or asynchronous; the store
contract remains synchronous.

Use the shipped implementation unless you are adapting another database:

```python
from hypergraph.materialization import LanceDBStore

store = LanceDBStore("./data")
```

## Required methods

A store subclasses `TableStore` and implements:

```python
class TableStore(ABC):
    def open(self, spec, children) -> dict[str, list[str]]: ...
    def count(self, table_name: str) -> int: ...
    def read_rows(
        self,
        table_name: str,
        where=None,
        *,
        limit: int | None = None,
        columns: list[str] | None = None,
    ) -> list[dict]: ...
    def read_one(
        self,
        table_name: str,
        identity_column: str,
        identity_value,
        *,
        columns: list[str] | None = None,
    ) -> dict | None: ...
    def write_rows(self, table_name: str, rows: list[dict]) -> None: ...
    def delete_rows(self, table_name: str, where) -> int: ...
    def max_write_gen(self, table_name: str) -> int: ...
    def evolve_schema(self, table_name: str, new_columns: dict) -> list[str]: ...
```

`open()` receives analyzed root and child table specifications. It ensures the
physical tables exist and returns their column names.

## Predicates

`where` is a sequence of `(column, operator, value)` tuples. Implement `eq`,
`ne`, `lt`, `lte`, `gt`, `gte`, and `in`. Multiple predicates are combined
with AND.

The physical child relationship uses `_parent_id`; HyperTable translates the
public parent identity and performs parent-column joins above the store.

## Generations and truthful reads

Every physical row carries `_write_gen`. A write may append a newer generation
before deleting the older one. Therefore:

- `read_one()` returns the highest generation for the requested identity;
- logical root reads deduplicate by identity;
- logical child reads deduplicate by parent identity plus child identity;
- `max_write_gen()` is durable across process restarts.

The conformance harness exercises overlapping generations and deletion.

## Schema evolution

`evolve_schema()` receives PyArrow data types. Adding an existing column must
be a no-op, including when a table currently contains zero rows. Return the
complete current column-name list.

Override `column_names()` when the backend can inspect its schema. Returning
`[]` is the base fallback.

Projection is optional to push down. A store that accepts `columns=` on both
read methods advertises projection automatically. Otherwise HyperTable reads
full rows and uses `TableStore._project_rows()`.

## Reserved storage

The store treats HyperTable's internal fields as ordinary physical values:

- `_row_fingerprint` and `_write_gen`;
- `_provenance_*` and `_recipe_fingerprint`;
- `_status` and `_error`;
- `_question`, the serialized waiting-question envelope;
- `_parent_id` on child grains.

These fields never cross the public row-read boundary. The table reconstructs
typed receipts, waiting rows, errors, and `PauseInfo` objects above the store.

## Optional capabilities

Implement `search()` for vector retrieval. Implement both `save_manifest()`
and `load_manifest()` to support persistent named indexes. If either manifest
method is missing, index creation fails loudly at use time.

## Validate an implementation

```python
from hypergraph.materialization import check_store_conformance, validate_store

validate_store(MyStore(...))
check_store_conformance(lambda path: MyStore(path))
```

The same conformance suite is run against `LanceDBStore` and an in-memory dict
store. It covers schema opening and evolution, predicates, projections,
generations, manifests, identity, and child-table behavior.
