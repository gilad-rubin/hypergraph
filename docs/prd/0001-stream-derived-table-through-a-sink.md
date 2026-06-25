# PRD 0001 — Materialize DerivedTable by streaming the derive through a sink

status: superseded

> **Superseded.** `DerivedTable` and its sink-based persistence were removed in
> favor of `HyperTable` (graph-native, `TableStore` protocol). The `map_iter`
> runner primitive this PRD relied on still exists as a standalone capability,
> but the sink layer it describes no longer does. Kept for historical context.

## Problem Statement

`DerivedTable` materializes derived rows by running its `derive` over the source
items in a **sequential `for` loop** inside `_derive_and_store`, building rows and
writing them inline. Two problems follow from this:

- The engine is hardcoded. The runner streaming primitive (`map_iter`, ADR 0002
  L1) now exists, but `DerivedTable` doesn't use it — so materialization can't
  ride the runner's concurrency/backpressure, and the "decouple the DB from the
  actions" goal (ADR 0002) is unrealized in the table layer.
- The write is entangled with the loop. There is no first-class place that owns
  "persist these derived rows," which is the seam a streaming engine needs to
  push results into, and which a future `DaftRunner` (native sink) would replace.

## Solution

Introduce a **Sink** — a consumer with a `start` / `write` / `finalize`
lifecycle that declares which graph output ports it persists — and rewire
`DerivedTable._derive_and_store` to run the `derive` through `runner.map_iter`
and feed each streamed result to a `LanceSink`. The content-key pre-filter stays
*above* the runner (DerivedTable still decides what is pending); the runner owns
*how* the pending items are computed; the sink owns *persisting* them. Behavior
is preserved exactly — all existing `DerivedTable` tests stay green — while the
engine becomes a real, swappable runner and the write becomes a real, reusable
sink. End to end: `insert(items)` → content-key skip → `map_iter` over the
pending items → `LanceSink` writes each derived row as its item completes.

## User Stories

1. As a DerivedTable user, I want `insert()` / `update()` / `sync()` to behave
   exactly as before, so that adopting the new engine is invisible to my code.
2. As a DerivedTable user, I want a row written for each source item as its
   derive completes, so that materialization streams instead of buffering the
   whole batch in memory.
3. As a DerivedTable user, I want content-unchanged items skipped before any
   compute, so that incrementality (the table's core value) is preserved.
4. As a DerivedTable user, I want `on_error="raise"` to still commit successes,
   skip writing failures, and raise `DerivationError` after processing, so that
   error semantics are unchanged.
5. As a DerivedTable user, I want `on_error="ignore"` to still write error rows
   and not raise, so that bulk reconciliation tolerates partial failure.
6. As a DerivedTable user, I want one-to-many explosion (a `derive` returning a
   list) to still write one row per output item, so that fan-out is preserved.
7. As a DerivedTable user, I want cascade to dependents to still happen level by
   level, so that chained tables stay in sync.
8. As a DerivedTable user, I want write-new-then-delete-old crash safety
   preserved, so that a crash leaves recoverable duplicates, not data loss.
9. As a DerivedTable user, I want each mutating operation to still bump the
   table version by exactly one, regardless of how many physical writes the
   streamed run performs, so that versioning semantics are unchanged.
10. As a DerivedTable author, I want to set the `runner` once on the root table,
    so that the whole chain shares one execution engine and color (per ADR 0001).
11. As a DerivedTable author, I want chained tables to inherit the root's runner
    rather than take their own, so that a chain is uniformly configured by
    construction, not by after-the-fact validation.
12. As a sink author, I want a `Sink` protocol with `start` / `write` /
    `finalize`, so that I can implement a new persistence target (Parquet, a
    different store) without touching DerivedTable internals.
13. As a sink author, I want to declare which output ports my sink persists, so
    that only the intended outputs are written and the rest stay observable.
14. As a sink author, I want declared ports validated against the graph's
    outputs at construction, so that naming a non-existent output fails at
    build time, not mid-run.
15. As a DerivedTable user with a multi-output derive graph, I want only the
    designated row-producing output persisted, so that scaffolding outputs
    (intermediate values) don't pollute the table.
16. As a maintainer, I want `LanceSink` to reuse the existing row-building and
    write-new-then-delete-old logic, so that the rewrite re-homes behavior
    rather than reimplementing it.
17. As a maintainer, I want the runner to default to `SyncRunner`, so that
    existing synchronous usage and tests keep working with no change.
18. As a maintainer, I want a clear seam where a future `DaftRunner` (whose
    write is a native sink) can bypass `LanceSink`, so that the design stays
    Daft-ready (ADR 0002).
19. As a DerivedTable user, I want `recompute()` to use the same streamed path,
    so that re-derivation benefits from the engine identically to insert.
20. As a DerivedTable user, I want query methods (`get`/`filter`/`count`/
    `errors`) to read exactly as before, so that the read side is untouched.

## Implementation Decisions

- **New `Sink` protocol** (in the materialization package): methods
  `start()`, `write(result)`, `finalize()`, and a declaration of the output
  port name(s) it persists (`writes`). The protocol is the L2 consumer from
  ADR 0002; it is *not* a graph node.
- **Port validation at construction.** A sink's declared `writes` ports are
  checked against the derive graph's `outputs`; an unknown port raises a
  build-time error (consistent with Hypergraph's "ambiguity is a build-time
  error" principle).
- **`LanceSink`** implements `Sink` and owns persistence: it re-homes
  DerivedTable's existing row construction (`_output_to_row`) and
  write-new-then-delete-old (`_write_rows` / `_delete_old_rows`) so semantics are
  identical. It batches writes (the existing `batch_size`-style behavior).
- **`DerivedTable._derive_and_store` is rewired** to: (a) content-key pre-filter
  to compute the pending items (unchanged), (b) build a graph from `derive` — a
  plain function is wrapped as a one-node graph; a `Graph` derive is used
  directly — bound with the components, (c) run `self._runner.map_iter(graph,
  {source_param: pending}, map_over=source_param, error_handling="continue")`,
  (d) feed each streamed `(index, RunResult)` to the sink, mapping the result
  back to its source item by index, (e) apply the existing `on_error` policy
  (raise vs error-row) and cascade. The runner always sees
  `error_handling="continue"`; DerivedTable owns the raise/ignore policy.
- **Source parameter detection.** The graph input that is not a bound component
  is the source port to `map_over`. For a function derive it is the derive's
  first non-component parameter; for a `Graph` derive it is the single
  unbound input. Ambiguity is a build-time error.
- **Runner ownership.** `runner` is a constructor argument on the **root**
  DerivedTable (default `SyncRunner()`); chained tables inherit it from their
  source rather than accepting their own. This makes the uniform-engine
  invariant (ADR 0001) structural.
- **Versioning is decoupled from physical writes.** One logical operation
  (insert/sync/recompute) still bumps the table version by one, even though the
  streamed sink may perform many physical LanceDB writes. The existing version
  tracking is preserved, not driven off per-write counts.
- **No async in this PRD.** The sync path (`SyncRunner.map_iter`, sequential)
  is wired; `AsyncDerivedTable` + concurrency is a later step.

## Testing Decisions

- **A good test asserts external behavior through the public API** — the result
  of `insert`/`update`/`delete`/`sync`/`recompute` observed via
  `get`/`filter`/`count`/`errors`/`version` — never the fact that `map_iter` or
  a sink was used internally.
- **Primary seam: the DerivedTable public mutation + query API.** This seam
  already exists and is exercised by the current 62 tests
  (`tests/test_derived_table.py`, `tests/test_materialization_types.py`). They
  are the regression gate: the engine/sink rewrite must keep all of them green
  unchanged. This is the ideal single, highest seam.
- **New tests, added at the same public seam**, for behavior the current suite
  doesn't cover:
  - A sink declaring an output port the derive graph doesn't produce raises at
    construction (build-time error).
  - A multi-output derive graph persists only the declared row output; the
    scaffolding outputs are absent from the table.
  - Runner inheritance: a chained table uses the root's runner (observable via
    behavior, e.g. constructing a chain with a non-default runner on the root
    materializes the chain).
- **Prior art:** the existing `tests/test_derived_table.py` fixtures (frozen
  dataclass entities with `Identity`/`ContentKey`, a local embedder component,
  a LanceDB store path) and `tests/test_map_iter.py` for the runner seam.

## Out of Scope

- `AsyncDerivedTable` and concurrent materialization (later step).
- `DaftRunner` integration (the seam is left open; no Daft code).
- Event processors / progress integration in `map_iter` (deferred in Step 1).
- Any change to query semantics, the content-key formula, or the cascade DAG
  rules — those are preserved as-is.

## Further Notes

- Realizes ADR 0002 (L1 `map_iter` + L2 sink protocol; no L3 graph-node sink)
  and respects ADR 0001 (runner on the root, inherited).
- The end-to-end payoff a user feels: with the sync runner, rows are written
  incrementally as each derive completes (bounded memory); swapping in the async
  runner later makes those derives run concurrently — no DerivedTable change.
- `DerivationError`, `ErrorRow`, `SyncResult`, content-key skip, explosion, and
  cascade are all preserved behaviors, re-homed across the new seam, not
  redesigned.
