# PRD 0003 — HyperTable mutations and store protocol

status: done

## Problem Statement

HyperTable (PRD 0002) can insert rows and read them back, but nothing else. You
can't update a source value and have downstream columns re-derive. You can't delete
a row and have its children cascade-delete. You can't call sync with a fresh list and
have it reconcile against what's stored. The fingerprints and provenance hashes are
written on every insert but never read back — incrementality is stored but not used.

The root cause: the `TableStore` protocol was designed for append-only insert + bulk
read. It has no single-row lookup, no delete, no predicate-filtered reads. Every
mutation feature needs operations the store can't do yet.

## Solution

Widen the `TableStore` protocol to support the full mutation vocabulary, then wire
HyperTable's public API to use it. After this PRD ships:

- `update(id, **changes)` re-derives downstream columns when a source value changes,
  skips derivation when only metadata changes, and cascades to child tables.
- `delete(id)` removes the row and cascade-deletes all children.
- `sync(current_items)` reconciles: insert new, update changed (by fingerprint),
  delete missing, skip unchanged — returns a `SyncResult`.
- `insert` skips rows whose fingerprint already matches (incrementality).
- `recompute(column)` re-derives one column for all rows (component swap).
- `backfill(column)` derives a new column for existing rows that have NULL.

## User Stories

1. As a developer, I want `table.update(id, text="new value")` to re-derive only
   the columns downstream of `text`, so that I don't pay for a full re-derivation.

2. As a developer, I want updating a metadata column (one that doesn't feed any
   node) to store the new value without running the graph, so that metadata edits
   are cheap.

3. As a developer, I want update to cascade to child tables when the parent's
   split node output changes, so that children stay consistent.

4. As a developer, I want `table.delete(id)` to remove the parent row and all its
   child rows, so that referential integrity is maintained.

5. As a developer, I want `table.sync(current_items)` to insert new items, update
   changed items (by content-key fingerprint), delete items no longer present, and
   skip unchanged items — returning a `SyncResult` with counts.

6. As a developer, I want insert to compare the new row's fingerprint against any
   existing row with the same identity, so that unchanged rows are skipped (true
   incrementality).

7. As a developer, I want `table.recompute("vector")` to re-derive the `vector`
   column for all rows using the current bound components, so that swapping an
   embedder doesn't require a full re-insert.

8. As a developer, I want `table.backfill("word_count")` to derive the column for
   all rows where it's NULL, so that adding a new node to the graph populates
   existing data.

9. As a developer, I want the store protocol to support single-row reads by identity,
   so that fingerprint checks don't load the entire table.

10. As a developer, I want the store protocol to support predicate-based deletes, so
    that cascade-delete and write-generation cleanup work correctly.

11. As a developer, I want the store protocol to support predicate-based reads with
    optional limit, so that filtered queries don't load the entire table.

12. As a developer, I want `open()` to return schema metadata, so that HyperTable
    can detect removed/renamed columns without a separate method.

13. As a developer, I want `evolve_schema` to accept typed columns (not just string),
    so that new derived columns get correct types.

14. As a developer, I want the store protocol to have no database-specific imports at
    the module level, so that importing the protocol doesn't pull in lancedb/pyarrow.

15. As a developer, I want `SyncResult` to tell me how many rows were inserted,
    updated, deleted, and skipped, so that I can verify reconciliation.

16. As a developer, I want delete to use the write-new-then-delete-old pattern
    (write with higher `_write_gen`, then delete old), so that crashes mid-operation
    are recoverable.

17. As a developer, I want child table deletes scoped by `_parent_id`, so that
    deleting one parent doesn't affect another parent's children.

18. As a developer, I want `recompute` to use `graph.with_entrypoint()` to run only
    the affected node, so that recomputation is efficient.

19. As a developer, I want update on a content-key column to recompute the row
    fingerprint and per-column provenance, so that subsequent syncs correctly detect
    the row as up-to-date.

20. As a developer, I want batch writes (`write_rows`) instead of single-row writes,
    so that bulk operations are efficient.

## Implementation Decisions

### Widened TableStore protocol

Eight methods, no database-specific imports at the protocol level:

```python
RowOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
RowPredicate = Sequence[tuple[str, RowOperator, Any]]

class TableStore(Protocol):
    def open(self, spec, children) -> dict[str, list[str]]:
        # Returns {table_name: [column_names]} for schema validation
    def count(self, table_name) -> int: ...
    def read_rows(self, table_name, where=None, *, limit=None) -> list[dict]: ...
    def read_one(self, table_name, identity_column, identity_value) -> dict | None: ...
    def write_rows(self, table_name, rows) -> None: ...
    def delete_rows(self, table_name, where) -> int:
        # Returns number of deleted rows
    def max_write_gen(self, table_name) -> int: ...
    def evolve_schema(self, table_name, new_columns) -> list[str]:
        # new_columns: dict[str, Any] mapping name to type hint
        # Returns updated column list
```

`read_all` is replaced by `read_rows(table, where=None)`. `write_row` is replaced by
`write_rows(table, [row])`. Both old methods are removed.

### No update_row — write-then-delete

Mutations use the write-new-then-delete-old pattern from PRD 0002's design:
1. Write the new row with the current `_write_gen`.
2. Delete old rows matching `(identity, _write_gen < current)`.

This is crash-safe: on recovery, duplicates (same identity, different `_write_gen`)
resolve by keeping the highest generation. It works naturally with append-only stores
like LanceDB.

### Structured predicates for delete and read

`RowPredicate` is a sequence of `(column, operator, value)` tuples. All clauses are
AND-ed. Operators: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`.

Examples:
- Cascade delete children: `[("_parent_id", "eq", "v1")]`
- Generation cleanup: `[("doc_id", "eq", "d1"), ("_write_gen", "lt", 5)]`
- Read one parent's children: `[("_parent_id", "eq", "v1")]`

### Incrementality in insert

Before running the graph, `insert` checks:
1. `read_one(table, identity_col, id)` — does a row exist?
2. Compare `_row_fingerprint` — same inputs + same node defs + same components?
3. If match → skip. If mismatch → write-then-delete (update in place).
4. If no existing row → normal insert.

### Partial graph execution for update and recompute

Hypergraph's `graph.with_entrypoint(node_name)` skips upstream nodes. The downstream
nodes that need re-running are found via `nx.successors()` on the graph's networkx
DAG. For `recompute("vector")`, find the node that produces `vector` via
`graph.sole_producers["vector"]`, then run from that entrypoint with the row's stored
source values.

### SyncResult dataclass

```python
@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
```

### Module changes

- `_table_store.py`: widened protocol, updated `LanceDBStore`, protocol-level imports
  are stdlib-only (lancedb/pyarrow move inside `LanceDBStore`).
- `_hypertable.py`: `update`, `delete`, `sync`, `recompute`, `backfill` methods.
  `insert` gains incrementality. `SyncResult` dataclass.
- Existing methods (`get`, `count`, `children`, `insert`, `visualize`) updated to
  use `read_rows`/`write_rows`/`read_one` instead of `read_all`/`write_row`.

## Testing Decisions

### What makes a good test

Tests assert external behavior through HyperTable's public API. Insert/update/delete/
sync/recompute/backfill, then verify via get/count/children. No test inspects
`TableSpec`, content-key hashes, or store internals directly. Incrementality is tested
by observing skip counts in `SyncResult`, not by inspecting fingerprint values.

### Test modules

- **`test_hypertable_mutations.py`** — update (source + metadata + cascade), delete
  (with cascade), sync (insert/update/delete/skip), recompute, backfill.
- **`test_hypertable_incrementality.py`** — insert skips unchanged, insert updates
  changed, sync skip counts, fingerprint changes when node body or component changes.

### Prior art

- `test_hypertable_construction.py` and `test_hypertable_e2e.py` — same style:
  class-based grouping, `LanceDBStore` fixture, `SyncRunner`, dict-based inserts.

## Out of Scope

- **Query namespace (`.queries`)** — attaching query graphs is a separate concern
  from mutations. Deferred to a follow-up PRD.
- **Ephemeral outputs** — `ephemeral=True` node flag. Orthogonal to mutations.
- **Schema evolution validation** — error on node removal or type change at open time.
  Uses the schema metadata from `open()` but is a separate feature.
- **AsyncRunner support** — mutations are sync-only in this PRD.
- **Per-row error handling** — error markers on failed rows. Separate concern.
- **Streaming writes via sink** — large binary column optimization. Separate concern.

## Further Notes

- The store protocol widening is backwards-incompatible for any existing `TableStore`
  implementations. Since `LanceDBStore` is the only implementation and lives in the
  same module, this is safe.
- `RowPredicate` filtering happens in Python for LanceDB (filter after `to_pandas()`).
  A future optimization can push predicates to LanceDB's native filter syntax.
- The `with_entrypoint` API already exists in Hypergraph and is tested. No changes to
  Hypergraph core are needed.
