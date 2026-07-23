# 0019 — Durable Batch: immutable manifest, keyed children, tolerance

status: accepted-intent (folded from amendments A2 and A7 on 2026-07-24, Durable Host V1 ticket 01; implementation is tickets 05–06)

## Why this shape

A persisted `MapResult` array would make completion order part of identity,
hide items that never started, and couple Batch truth to one parent run's
in-memory evidence. The twelve-framework survey found no framework with the
full shape Hypergraph needs (manifest + stable item keys + tolerance +
item-granular rerun), and Hypergraph's own pinned map-interrupt test already
states it: a batch is N independent admissions.

## Fixed acceptance contract

Before (today — a durable batch is a product invention):

```python
result = await runner.map(ingest_graph, {"doc": docs}, map_over="doc")
# One parent run owns the whole batch. A crash leaves product-owned queue
# rows to reconstruct which items ran; a stopped batch has no durable
# notion of "requested but never started" beyond this process.
```

After:

```python
receipt = await host.submit_batch(
    "ingest",
    items={"protocol-17": {"doc": ...}, "protocol-18": {"doc": ...}},
    workflow_id="schneider-drop-42",
    tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),  # optional, pinned
)
receipt.batch_ref               # inert, serializable BatchRef

view = await client.get(receipt.batch_ref)      # BatchView
view.counts          # {"completed": 1, "failed": 0, "unstarted": 1, ...}
view.outcomes        # keyed by logical item key, not completion order
view.unstarted_items # explicit item keys never admitted — never invented results

async for update in client.watch(receipt.batch_ref, after=cursor):
    ...   # one gap-free per-Batch durable sequence
```

Requirements:

- **Immutable manifest (A2).** A Batch is a manifest of unique stable
  logical item keys, each mapped to one independent child Run with its
  pinned inputs — never a durable parent `MapResult`. Duplicate item keys
  are rejected at submission.
- **Atomic acceptance.** One transaction persists the manifest, child Run
  identities, pinned inputs, and the accepted start command. A partial
  Batch can never appear accepted.
- **Keyed identity.** Child outcomes are keyed by logical item key;
  completion order never changes result identity. Each child is an ordinary
  Run (RunRef, watch, stop, recovery) whose Batch membership is recorded.
- **Explicit unstarted items.** Items never admitted — because of
  cooperative stop, tolerance trip, or worker death — are listed by key.
  Hypergraph never fabricates failed results for them, matching ADR 0004's
  unstarted-item truth at durable scale.
- **Pinned tolerances (A7).** `BatchTolerance(max_failed=…,
  max_failed_percent=…)` values are optional manifest fields, pinned at
  acceptance — never `serve()` configuration. Either tolerance trips when
  failure-equivalent children **strictly exceed** its threshold. The
  percentage denominator is always the total logical manifest item count;
  it cannot change during execution.
- **Failure equivalence.** Failed and recovery-exhausted children count
  toward tolerance. Paused, queued, delayed, admission-limited, and
  unstarted items never count.
- **Trip behavior.** A tripped Batch closes new child admission, lets
  already-claimed children settle, marks every remaining item explicitly
  unstarted, and stays truthfully PARTIAL — not failed, not stopped.
- **One durable sequence (A9).** Every manifest or child-outcome change
  receives a monotonic per-Batch sequence; when a child commit changes
  Batch truth, both records land in the same transaction. `watch(batch_ref,
  after=cursor)` follows the whole Batch gap-free and never backpressures
  execution.
- **Item-scoped rerun (A16).** `client.rerun(batch_ref,
  item_keys=[...])` accepts only keys present in the source manifest,
  creates new child Runs under new workflow ids with the source's pinned
  Definition identity and inputs, and records `retry_of` lineage. No input
  overrides; Definition-identity changes go through `fork()`.

## Test plan (red first)

- Atomic acceptance: kill between manifest write and child start — the
  Batch is either fully visible or absent.
- Stable keys with non-completion-order execution: outcomes stay keyed;
  restart preserves completed children and continues only unfinished
  repeat-safe children.
- Tolerance: exact threshold (no trip), threshold plus one (trip), count
  and percentage pinned together, fixed percentage denominator, paused
  children never counting, claimed children settling after trip, remaining
  items explicitly unstarted, Batch PARTIAL.
- Subset rerun: only source item keys accepted; new workflow ids; `retry_of`
  lineage recorded; no input override accepted.
- Watch: reconnect from a stored per-Batch cursor without gaps or repeats.
- Sync and async parity; CI-equivalent run green.

## Out of scope

Cross-Batch admission fairness or keyed admission (deferred), bulk
mutation by query, any durable parent `MapResult` projection, streaming
`.iter()`-style item delivery (that is Tier-0 `Streaming map` vocabulary,
not the durable Batch).
