# Implementing a TableStore

HyperTable separates **computation** (your graph) from **persistence** (a store).
The store is a pluggable backend behind the `TableStore` interface — LanceDB ships
as the reference implementation, but you can back a table with anything: SQLite,
Postgres, DuckDB, S3 + a search index, or an in-memory dict for tests.

This page is for store *authors*. If you only use HyperTable, you never see these
methods.

## The shape

Subclass `TableStore` and implement the required methods. Each operates on a
`table_name` and plain row dicts:

```python
import pyarrow as pa
from hypergraph.materialization import TableStore

class MyStore(TableStore):
    def open(self, spec, children): ...        # ensure tables exist -> {name: [columns]}
    def count(self, table_name): ...           # physical row count
    def read_rows(self, table_name, where=None, *, limit=None): ...
    def read_one(self, table_name, identity_column, identity_value): ...
    def write_rows(self, table_name, rows): ...
    def delete_rows(self, table_name, where): ...   # -> count deleted
    def max_write_gen(self, table_name): ...        # highest _write_gen, or 0
    def evolve_schema(self, table_name, new_columns): ...  # new_columns: {name: pa.DataType}
```

`search`, `save_manifest`, and `load_manifest` are optional (they default to
"not supported" / no-op) — implement them only if your backend offers them.

## The invariants that aren't obvious

Most of the interface is self-explanatory. These few are not, and a store that
gets them wrong fails *silently* — so they're worth stating outright.

### `read_one` returns the newest generation

Every row carries a `_write_gen`. HyperTable writes the new generation **before**
deleting the old one (crash-safe ordering), so after a crash a single identity can
briefly have two physical rows. `read_one` **must** return the one with the highest
`_write_gen`:

```python
def read_one(self, table_name, identity_column, identity_value):
    matches = [r for r in self._rows(table_name) if r[identity_column] == identity_value]
    if not matches:
        return None
    return max(matches, key=lambda r: r.get("_write_gen", 0))   # newest, not first
```

Returning the *first* match instead is the single most common store bug — and it's
invisible until a crash leaves a stale row ahead of the current one.

### Reserved columns pass through untouched

HyperTable manages several columns; your store must store and return them like any
other, never reject or rewrite them: `_write_gen`, `_row_fingerprint`, `_status`,
`_error`, `_parent_id`, and `_provenance_*`. Child rows are scoped by `_parent_id`
— `read_rows(child, [("_parent_id", "eq", parent)])` is how HyperTable lists a
parent's children.

### `delete_rows` returns the physical count; predicates use the operator set

`delete_rows` returns how many physical rows it removed. Predicates are
`(column, op, value)` tuples; support every operator HyperTable emits:
`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`.

### `evolve_schema` speaks Arrow, not Python

New columns arrive as `dict[str, pyarrow.DataType]` — **Arrow is the intermediate
type system**. An Arrow-native store maps the Arrow type to its column type; a
schemaless store (JSON, key-value) ignores the values and just records the names.
No store converts from Python types — HyperTable does that once, upstream.

```python
# Arrow-native (LanceDB): use the type
fields = [pa.field(name, arrow_type) for name, arrow_type in new_columns.items()]

# Schemaless (JSON store): use only the names
self._schema[table_name] += list(new_columns.keys())
```

## What is *not* your job

The store is a dumb persistence layer. HyperTable owns everything semantic:
incrementality and fingerprints, write-new-then-delete-old ordering, the
parent/child cascade, error rows, and dedup-on-read for query results. Don't
re-implement any of it in the store — just store rows faithfully and honor the
invariants above.

## Validate against the contract

`validate_store(store)` is a quick shape check (subclass + `open()`). For the real
thing, run the **conformance harness** in your test suite — it drives a fresh store
through the observable contract (newest-gen reads, every operator, delete counts,
schema evolution, parent/child filtering) and fails with a precise message naming
any invariant you missed:

```python
from hypergraph.materialization import check_store_conformance

def test_my_store_conforms(tmp_path):
    check_store_conformance(MyStore(path=str(tmp_path / "store")))
```

The reference `LanceDBStore` and both of Panda's stores (a JSON `LocalTableStore`
and an `S3 + Azure Search` store) pass this same harness — so it's calibrated to
accept both Arrow-native and schemaless designs.
```text
TableStore conformance failed:
  - read_one_returns_newest_generation: read_one must return the highest _write_gen
    when one identity has multiple generations (crash-leftover dedup). Got n=10,
    expected the n=20 / _write_gen=2 row.
```
