# 0018 — RunHomeClient, inert refs, and durable watch sequences

status: accepted-intent (folded from amendments A9, A12, A13 on 2026-07-24, Durable Host V1 ticket 01; implementation lands across tickets 02–06)

## Why this spec exists

ADR 0004 permanently answers "give me a durable handle" with: handles are
process-local live control. The durable surfaces are inert addresses plus one
backend-neutral client. This spec fixes that surface so the Host
(PRD 0011), the Batch contract (PRD 0019), and every later tier share one
ownership seam and one reconnection story.

## Fixed acceptance contract

Before (today — observation requires the process that started the work):

```python
handle = runner.start_run(refund_graph, {"claim_id": "c-42"},
                          workflow_id="refund-c-42")
# `handle` controls the live execution and dies with this process.
# Another process cannot inspect, stop, or follow the run; a webhook
# retry has no safe address to hold.
```

After:

```python
receipt = await host.submit("refund", {"claim_id": "c-42"},
                            workflow_id="refund-c-42")
ref = receipt.run_ref            # RunRef: frozen, JSON-serializable, inert
json.dumps(ref.to_dict())        # safe to store in a product table

# A small operator process — no graph code imported:
client = RunHomeClient(RunHome.open("file:./runs.db"))
view = await client.get(ref)                 # RunView: persisted facts
assert view.waiting == "admission_limited"   # named waiting condition, or None

runs = await client.list(RunQuery(definition="refund",
                                  status=WorkflowStatus.PAUSED,
                                  older_than=timedelta(hours=1)))

cursor = None
async for update in client.watch(ref, after=cursor):
    if update.durable:
        cursor = update.cursor   # only durable facts advance the cursor
```

Requirements:

- **Inert refs (A12).** `RunRef` and `BatchRef` are immutable, serializable
  value objects. They expose identity only (workflow id / batch id and Home
  coordinates needed to address the work) — no liveness, status, result, or
  control methods. They are never called durable handles.
- **One client surface (A13).** `RunHomeClient` owns `get`, `list`,
  `watch`, `stop`, `rerun`, and (once PRD 0010 lands) `answer`, accepting
  `RunRef` or the applicable `BatchRef`. The Host exposes it as
  `host.client` and never duplicates its verbs. The client is
  backend-neutral: the same surface runs against SQLite now and Postgres
  later, and constructing one requires no Definition code.
- **Truthful views.** `RunView` reports persisted Run facts plus a named
  waiting condition — one of queued, scheduled (future `start_at`), paused,
  version-incompatible, admission-limited, or recovery-exhausted — so
  waiting work never looks alike. `BatchView` (PRD 0019) reports manifest
  counts, keyed child outcomes, and explicit unstarted items.
- **Gap-free durable sequences (A9).** Every Run mutation receives one
  monotonic per-Run durable sequence inside its own transaction; every
  Batch manifest or child-outcome change receives one monotonic per-Batch
  sequence, with child and Batch facts committing together. `watch(ref,
  after=cursor)` replays every committed fact after the cursor in sequence
  order — no gaps, no repeats — then tails live events.
- **Previews never advance the cursor.** Live tail events arrive marked
  non-durable (`update.durable is False`); they are best-effort preview per
  the existing event-processor contract and never reconstruct `RunResult`.
  A Batch watcher never backpressures execution.
- **Typed conflicts, not strings.** Terminal workflow-id reuse and
  fingerprint mismatch (A5), stale or double answers (PRD 0010), and
  stop-after-terminal each raise distinct typed errors so callers can
  branch on them.

## Test plan (red first)

- Serialize a `RunRef`/`BatchRef` to JSON and back; construct a client in a
  process with no graph imports and get/list/stop by ref.
- Watch a Run, disconnect mid-stream, reconnect with the stored cursor:
  assert no missing and no repeated durable sequence values.
- Force a live preview during replay: cursor unchanged afterwards.
- `RunView.waiting` distinguishes queued, scheduled, paused,
  version-incompatible, admission-limited, and recovery-exhausted fixtures.
- Sync and async client paths present the same semantics; CI-equivalent
  run green.

## Out of scope

Batch-specific view and watch shapes (PRD 0019), the answer verb payload
contract (PRD 0010), scheduled answers (PRD 0012), any reconnectable
token/log output (deferred OutputLog), any HTTP or remote client transport.
