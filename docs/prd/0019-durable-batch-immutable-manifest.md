# 0019 — Durable Batch: immutable manifest, keyed children, tolerance

status: accepted-intent (folded from amendments A2 and A7 on 2026-07-24, Durable Host V1 ticket 01; implementation is tickets 05–06)

## Why this shape

A persisted `MapResult` array would make completion order part of identity,
hide items that never started, and couple Batch truth to one parent run's
in-memory evidence. Panda-shaped ingestion needs every requested item to
remain attributable across restart, partial success to be an explicit pinned
policy, and failed items to be repeatable without touching their inputs.
Hypergraph's own pinned map-interrupt test already states the core fact:
a batch is N independent admissions.

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
    ingest_graph,
    {"protocol_id": ["protocol-17", "protocol-18"], "doc": [doc_17, doc_18]},
    map_over=["protocol_id", "doc"],
    key_by="protocol_id",
    workflow_id="schneider-drop-42",
    tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),  # optional, pinned
)
receipt.batch_ref               # inert, serializable BatchRef

view = await client.get(receipt.batch_ref)      # BatchView
view.counts          # {"completed": 1, "failed": 0, "unstarted": 1, ...}
view.outcomes        # keyed by logical item key, not completion order
view.unstarted_items # explicit item keys never admitted — never invented results
view.abandoned_items # keys whose child HAD started when admission closed

async for update in client.watch(receipt.batch_ref, after=cursor):
    ...   # one gap-free per-Batch durable sequence
```

Requirements:

- **Immutable manifest (A2).** A Batch is a manifest of unique stable
  logical item keys, each mapped to one independent child Run with its
  pinned inputs — never a durable parent `MapResult`. The caller states
  the expansion in `runner.map`'s own vocabulary (`values` plus
  `map_over` / `map_mode`) and names the key input with `key_by`; the
  expansion is frozen into `(item_key, inputs)` pairs BEFORE the
  acceptance transaction, so mutating the caller's collection afterwards
  cannot change durable intent. Duplicate, missing, empty, or non-scalar
  item keys are rejected at submission — never replaced by a generated
  map index, which no operator could reproduce.
- **Atomic acceptance.** One transaction persists the manifest, child Run
  identities, pinned inputs, and the accepted start command. A partial
  Batch can never appear accepted.
- **Keyed identity.** Child outcomes are keyed by logical item key;
  completion order never changes result identity. Each child is an ordinary
  Run (RunRef, watch, stop, recovery) whose Batch membership is recorded.
- **Explicit unstarted items.** Items never admitted — because of
  cooperative stop, tolerance trip, or worker death — are listed by key.
  Hypergraph never fabricates failed results for them, matching ADR 0004's
  unstarted-item truth at durable scale. An item that HAD started when
  closed admission settled it is listed separately as **abandoned**: it
  committed steps and may have landed side effects, so calling it
  unstarted would tell an operator nothing happened when something did.
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
  already-claimed children settle, accounts every remaining item
  explicitly (unstarted if it never began, abandoned if it had), and
  stays truthfully PARTIAL — not failed, not stopped. Closed admission
  is closed at the CLAIM: answering a paused child of a tripped Batch
  settles its question and returns the child to claim order like any
  other answer, and the claim gate then refuses it — settling it
  `abandoned`, because it had already started, rather than running it.
- **One durable sequence (A9).** Every manifest or child-outcome change
  receives a monotonic per-Batch sequence; when a child commit changes
  Batch truth, both records land in the same transaction. `watch(batch_ref,
  after=cursor)` follows the whole Batch gap-free and never backpressures
  execution.
- **Item-scoped rerun mints a new Batch (A16).** `client.rerun(batch_ref,
  item_keys=[...])` accepts only keys present in the source manifest and
  creates a **new immutable Batch manifest** with a new `BatchRef`,
  containing new child Runs (new workflow ids, source's pinned Definition
  identity and source inputs) only for the selected keys. The new Batch
  records explicit Batch lineage (`retry_of` against the source Batch) and
  each child records `retry_of` against its source child. The source Batch
  is never mutated. No input overrides; Definition-identity changes go
  through `fork()`.

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
- Subset rerun: only source item keys accepted; a new immutable manifest
  with a new `BatchRef` is minted; the source Batch is byte-identical
  afterwards; Batch `retry_of` and per-child `retry_of` lineage recorded;
  no input override accepted.
- Watch: reconnect from a stored per-Batch cursor without gaps or repeats.
- Sync and async parity; CI-equivalent run green.

## Out of scope

Cross-Batch admission fairness or keyed admission (deferred), bulk
mutation by query, any durable parent `MapResult` projection, streaming
`.iter()`-style item delivery (that is Tier-0 `Streaming map` vocabulary,
not the durable Batch).
