# 0017 — Durable Host V1 decision prototype (Panda-shaped)

status: pending explicit maintainer approval (Durable Host V1 ticket 01;
ticket 02 stays blocked until approval lands in this session)

## How to read this prototype

This is a **decision artifact, not implementation**. Every code block is the
public interface a caller would write; every state table is the persisted Run
Home facts and views that calls produce. Nothing here executes. Walk the
scenarios in order; each one names the spec clause it demonstrates. If any
state shown here is not the intended end state, say so before ticket 02
opens — after approval, these before/after states become the examples the
acceptance tests encode.

Authority: [PRD 0017](0017-durable-host-v1-program.md) (delivery contract),
[amendments A1–A16](../research/2026-07-23-durable-host-amendments.md)
(provenance), ADRs 0005–0008 (decisions), PRDs
[0010](0010-durable-pause-slots-and-atomic-pause-settlement.md),
[0011](0011-local-durable-host-and-sqlite-run-home.md),
[0013](0013-pending-node-boundaries-persist-before-sibling-effects.md),
[0014](0014-effect-identity-and-unknown-outcomes.md),
[0018](0018-runhomeclient-inert-refs-and-durable-watch.md),
[0019](0019-durable-batch-immutable-manifest.md) (per-area contracts).

## The cast — one Panda deployment

Panda ingests vendor document drops. One product-owned worker process; one
SQLite Run Home file; operators inspect from a laptop process with no graph
code.

```python
from hypergraph import AsyncRunner, serve, RunHome, RunHomeClient
from hypergraph import node, EXTERNAL, BatchTolerance

@node(output_name="text")
def extract_text(doc: bytes) -> str: ...          # repeat-safe

@node(output_name="record_id", effects=EXTERNAL)  # declares: may cause an
def file_record(text: str, doc_meta: dict) -> str: ...  # external effect

ingest = ingest_graph.with_runner(AsyncRunner())  # extract → file_record
review = review_graph.with_runner(AsyncRunner())

host = serve(ingest, review,
             home=RunHome.open("file:./panda-runs.db"),
             deployment_version="2026.07.3")
client = host.client                # the one RunHomeClient
```

Each serve binding pins its complete **Definition identity** (ADR 0007):

| Definition | DefinitionId pinned at submit |
|---|---|
| `ingest` | `("ingest", "2026.07.3", structural_hash=9f3a…)` |
| `review` | `("review", "2026.07.3", structural_hash=71bc…)` |

## The ownership seam (A13) and the no-app-less-submit rule

| Verb | Lives on | Needs Definition code? |
|---|---|---|
| `submit`, `submit_batch` | **Host** | yes — new work is Definition-bound |
| `fork` | **Host** | yes — migration targets a loaded Definition |
| `work_forever` (start, bounded drain, lock release) | **Host** | yes — the worker executes graphs |
| `get`, `list`, `watch`, `stop`, `rerun`, `answer`* | **RunHomeClient** | no — inspect and control existing work only |

\* `answer` stays unavailable until durable pause slots land (A1, PRD 0010).

The Host exposes `host.client` and adds **no** pass-through copies of client
verbs. A laptop process runs `RunHomeClient(RunHome.open("file:./panda-runs.db"))`
with zero graph imports and can do everything in the second row — but there
is no submit path without loaded Definition code, and no Definition catalog
in the Run Home (owner decision 5).

`RunRef` and `BatchRef` are frozen, JSON-serializable value objects. They
carry identity only — no `.status`, `.result()`, `.stop()`, nothing live.
They are never durable handles (ADR 0004 holds).

## Scenario 1 — Batch submission is atomic and pinned (A2)

Schneider's drop arrives: 8 protocols, submitted as one durable Batch.

```python
receipt = await host.submit_batch(
    "ingest",
    items={f"protocol-{n}": {"doc": ..., "doc_meta": {...}} for n in range(17, 25)},
    workflow_id="schneider-drop-42",
    tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),
    start_at=None,
)
receipt.batch_ref          # {"home": "file:./panda-runs.db", "batch_id": "b-841"}
```

**Before:** the Run Home has no row mentioning `schneider-drop-42`.

**After — one transaction committed all of this or none of it:**

| fact | value |
|---|---|
| batch manifest | id `b-841`; definition `("ingest","2026.07.3",9f3a…)`; workflow_id `schneider-drop-42`; 8 unique item keys; `max_failed=2`; `max_failed_percent=25` |
| child identities | 8 child Run rows, one per item key, each with pinned inputs and its own `RunRef` (`schneider-drop-42:protocol-17` … `:protocol-24`) |
| start intent | accepted start command at per-Batch durable sequence `bseq=1` |
| start fingerprint | complete Definition identity + normalized inputs + effective Batch config + `start_at` |

A kill anywhere inside acceptance leaves the Batch fully absent — never
half-accepted (PRD 0019, atomic acceptance).

## Scenario 2 — Webhook retry and conflicting reuse (A5)

```python
dup = await host.submit_batch("ingest", items={...same...},
                              workflow_id="schneider-drop-42",
                              tolerance=BatchTolerance(max_failed=2, max_failed_percent=25))
assert dup.duplicate and dup.batch_ref == receipt.batch_ref   # use-existing

await host.submit("ingest", {"doc": ...}, workflow_id="schneider-drop-42")
# WorkflowIdConflictError — same id, different fingerprint

# … after the Batch settles terminally:
await host.submit_batch("ingest", items={...same...}, workflow_id="schneider-drop-42", ...)
# AlreadyTerminalError — completed history never changes identity
```

**Before → after:** the nonterminal duplicate writes **no** new row; the two
conflicts raise distinct typed errors and write no execution facts. Callers
never manage idempotency keys.

## Scenario 3 — Worker kill and restart (tickets 02/04/08)

The worker is claimed by `work_forever(worker_id="panda-app-0")` behind the
OS-level exclusive lock. Mid-batch:

**Before the kill (persisted facts):**

| item key | child Run state | journal |
|---|---|---|
| protocol-17 | terminal: completed | StepRecords committed |
| protocol-18 | terminal: completed | StepRecords committed |
| protocol-19 | claimed, `extract_text` committed; `file_record` pending | pending-node boundary recorded (PRD 0013) |
| protocol-20…24 | unstarted | manifest rows only |

`kill -9 panda-app-0`. systemd restarts the process; `work_forever` takes
the lock and runs the restart scan.

**After restart — without any resubmission:**

| item key | what happens |
|---|---|
| protocol-17, 18 | untouched — completed children stay complete |
| protocol-19 | re-adopted; `extract_text` is journal-skipped; `file_record` dispatches from its pending boundary (repeat-safe, not yet declared-effectful in this run — see Scenario 7 for the effectful case) |
| protocol-20…24 | claimed in order and executed |

The Run record for protocol-19 shows `recovery_attempts=1` against its
pinned `recovery_cap=3`. Committed StepRecords, durable pauses, or terminal
transitions reset that counter; recovery without committed progress
increments it. A poison child that reaches the cap is parked:

```
RunView(protocol-23): waiting="recovery_exhausted"   # coordination condition,
                                                     # NOT a WorkflowStatus
```

Recovery-exhausted children count as failure-equivalent for tolerance;
`waiting=` keeps them operator-visible and queryable (`client.list(RunQuery(recovery="exhausted"))`).

## Scenario 4 — Cursor reconnection is gap-free (A9)

An operator laptop watches the Batch. Every manifest or child-outcome
change carries one monotonic per-Batch `bseq`; child and Batch facts commit
in the same transaction.

```python
cursor = None
async for update in client.watch(receipt.batch_ref, after=cursor):
    if update.durable:
        cursor = update.cursor      # e.g. bseq=14
    # live previews arrive with update.durable == False and never move cursor
```

The laptop dies at `bseq=14`. A new process reconnects:

```python
client2 = RunHomeClient(RunHome.open("file:./panda-runs.db"))   # no graph code
async for update in client2.watch(receipt.batch_ref, after="bseq:14"):
    ...   # replays bseq=15,16,17… in order — no gaps, no repeats —
          # then tails live previews, still marked non-durable
```

**Before → after:** reconnecting from a stored cursor loses nothing and
duplicates nothing; a preview observed before the crash is re-deliverable
as preview but can never be mistaken for a durable fact.

## Scenario 5 — Tolerance trip (A7)

Manifest: 8 items, `max_failed=2`, `max_failed_percent=25`. The percentage
denominator is fixed at acceptance: 25% of **8** = 2 failures allowed; a
trip needs failure-equivalent children to **strictly exceed** a threshold —
so 3 failed children trips both.

**Before:** completed 3, failed 2 (protocol-20, protocol-21), claimed 1
(protocol-22), unstarted 2 (protocol-23, protocol-24). Batch is not
tripped: 2 does not strictly exceed 2.

protocol-19's child fails → failure-equivalent count becomes 3 > 2.

**After — one transaction at the next `bseq`:**

| fact | value |
|---|---|
| batch status | **PARTIAL** — truthful, not failed, not stopped |
| admission | closed; no new child claims |
| protocol-22 (claimed) | left to settle normally |
| protocol-23, protocol-24 | recorded as **explicitly unstarted** — item keys listed, no invented results |
| `BatchView.counts` | `{"completed": 3, "failed": 3, "unstarted": 2, ...}` after protocol-22 settles |

Paused, queued, delayed, and admission-limited children never count toward
tolerance; unstarted items never become fake failures.

## Scenario 6 — Version-incompatible refusal (ADR 0007)

Panda deploys `2026.07.3` while an old Run `nightly-backfill` is parked,
pinned to `("ingest", "2026.06.1", structural_hash=44de…)`.

**Before → after the deploy:**

| fact | value |
|---|---|
| `nightly-backfill` | remains persisted, **unclaimed**; the new worker refuses it loudly |
| `RunView.waiting` | `"version_incompatible"` — queryable via `RunQuery`, aged-unclaimed |
| worker log | names the pinned identity it cannot serve; no guessing, no silent resume |

Two truthful ways forward, never an automatic one:

```python
# (a) the deployment declares it can drain old work:
host = serve(ingest, home=..., deployment_version="2026.07.3",
             accepts=(("ingest", "2026.06.1", "44de…"),))   # full prior identities,
#           structural compatibility still checked → worker now claims it

# (b) an operator migrates explicitly — see Scenario 8 (fork)
```

## Scenario 7 — Unknown effect, never auto-respent (A10, PRD 0014)

`file_record` is declared `effects=EXTERNAL`. For child protocol-19 the
worker commits the reservation **before** the provider call:

```
effect_id = (run="schneider-drop-42:protocol-19", node="file_record", attempt=2)
```

Three kill points, three truthful outcomes:

| kill point | persisted facts | recovery behavior |
|---|---|---|
| before dispatch | reservation absent or undispatched | safe to dispatch once |
| after provider return, settlement witnessed | StepRecord committed | complete; never re-dispatched |
| **after dispatch, before settlement** | reservation committed, no settlement witnessed | **`OUTCOME_UNKNOWN`** |

**After the ambiguous kill:**

- The child Run surfaces `OUTCOME_UNKNOWN`; recovery **never dispatches the
  effect again automatically**, across any number of restarts.
- The unknown effect is neither success nor failure — it does not count
  toward Batch tolerance; it waits for an explicit operator decision (was
  the record filed? check the provider, then resolve).
- Node-owned `RetryPolicy` budgets and windows are untouched — Host
  recovery is not a retry.

## Scenario 8 — `rerun()` repeats; `fork()` migrates (A16)

After the Batch settles PARTIAL, Panda fixes data for two protocols and
repeats just those items:

```python
rerun_receipt = await client.rerun(receipt.batch_ref,
                                   item_keys=["protocol-20", "protocol-21"])
```

**After:** two new child Runs under **new** workflow ids
(`schneider-drop-42-r1:protocol-20`, `…:protocol-21`), same pinned
Definition identity, same source inputs, `retry_of` recorded against the
source children. Input overrides are rejected; keys outside the source
manifest are rejected. The original Batch history is untouched.

Separately, the parked `nightly-backfill` Run must move to the new code:

```python
fork_receipt = await host.fork(run_ref, into="ingest",
                               reason="2026.07.3 schema migration, approved by ops")
```

**After:** a new Run seeded from recorded history, pinned to
`("ingest","2026.07.3",9f3a…)`, with `forked_from` lineage and the reason
stored. Compatibility is checked at fork time. `retry_of` and `forked_from`
never merge: `RunQuery(lineage=...)` can always tell repetition from
migration.

## What this prototype deliberately never shows

- No broker, stream, second journal, or event-sourced execution.
- No Hypergraph server, dashboard, or worker supervisor — the worker is
  product-owned (FastAPI lifespan, systemd restart).
- No reconnectable handle and no OutputLog — previews are non-durable,
  full event replay is not promised.
- No app-less submit or Definition catalog.
- No claim that Panda is production-safe: guard deletion and production
  claims wait for pending-node and effect-safety kill tests (tickets
  08/09/11).

## Approval checklist for the maintainer

- [ ] Interface shape: `serve` / Host verbs / `host.client` / inert refs as shown
- [ ] Scenario 1–2: atomic Batch acceptance; fingerprint dedup; typed conflicts
- [ ] Scenario 3: restart truth, pending boundaries, recovery-exhausted as condition
- [ ] Scenario 4: gap-free durable cursor; previews never advance it
- [ ] Scenario 5: strictly-exceeds tolerance, fixed denominator, PARTIAL with explicit unstarted
- [ ] Scenario 6: version refusal visible and unclaimed; `accepts=` drains
- [ ] Scenario 7: reservation before dispatch; `OUTCOME_UNKNOWN`; no auto-respend
- [ ] Scenario 8: `rerun()` vs `fork()`; lineage never merges
- [ ] Ownership seam and no-app-less-submit rule are unambiguous
