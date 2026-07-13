# Background handles control live work; results own settled truth

**Status:** Accepted on 2026-07-13; implementation is tracked by [issue #155](https://github.com/gilad-rubin/hypergraph/issues/155). `start_run()` and `start_map()` are not shipped APIs until that implementation merges. This ADR supersedes ADR 0003 only where ADR 0003 proposed `wait()`; its background-map collection and retrieval policy remains accepted.

A background handle exists to let one Python process control a live Hypergraph execution without turning the handle into a second result model or a durable job reference. We keep the control surface minimal and preserve execution truth on `RunResult` and `MapResult`.

## Decision

- Export two generic public types, `SyncHandle[T]` and `AsyncHandle[T]`. `start_run()` and `start_map()` return the appropriate handle parameterized by `RunResult` or `MapResult`; blocking `run()` and `map()` keep their existing return types.
- A handle exposes only `done`, `stop(info=...)`, and `result(raise_on_failure=True)`. It does not expose status, wait, failure evidence, failed indexes, presentation, retry, or persistence methods.
- `RunResult` and `MapResult` are the sole settled-outcome surfaces. A handle controls live work and retrieves a result; it does not duplicate result data.
- `start_run()` and `start_map()` accept no `error_handling` option. Runtime failures are captured so `result()` can raise by default or return the failed result when `raise_on_failure=False`. A failure that prevents construction of a result still propagates.
- Background `start_run()` retains ordinary execution and checkpoint inputs but omits the lineage-changing `override_workflow`, `fork_from`, and `retry_from` conveniences. A caller performs the lineage operation first, then starts the resulting workflow ID; this keeps non-awaited async submission compatible with synchronous identity reservation.
- `stop()` is a cooperative, idempotent, `None`-returning request. The first accepted request owns its `info`; later requests do not rewrite it. Stop uses Hypergraph's existing signal across nested graphs and map children; it does not cancel a thread or call `Task.cancel()`.
- Cancelling one coroutine awaiting `AsyncHandle.result()` cancels only that waiter. Framework-owned execution continues until it settles or the caller explicitly invokes `handle.stop()`.
- Handles are process-local. A checkpointer may preserve completed boundaries and workflow lineage for a later execution, but it cannot reconnect to, discover, or stop the previous process's handle. A persisted active status is not worker liveness or ownership.
- A stopped `MapResult` contains only real claimed outcomes, ordered by original input index. `requested_count` records requested scope and `unstarted_item_indexes` identifies inputs never claimed because cooperative stop curtailed the batch. Hypergraph does not fabricate `RunResult`, event, log, run ID, or checkpoint history for unstarted items.
- Parent map event counts continue to describe real settled child outcomes only. Requested and unstarted scope lives on `MapResult`; the parent `RunEndEvent.status`, `batch_outcome`, and OTel outcome still report `STOPPED` when stop curtailed the batch.
- If cooperative stop curtails requested scope, the batch status is `STOPPED` even when a real attempted item failed. `failed` and `failures` still expose those failures. A stop request that arrives after every requested item settles does not rewrite the ordinary aggregate status.
- Retrieval remains independent of terminal status: `result()` raises the first real failed item in input order after settlement, while `result(raise_on_failure=False)` returns the stopped batch. Result status, the parent end event, and checkpointed parent workflow status must agree.
- Duplicate active `workflow_id` reservation is rejected synchronously before a second handle is returned. The first execution remains live and authoritative.

## Considered options

- **A richer handle mirroring result status and evidence** — rejected because it creates two authorities and name/type collisions with `RunResult` and `MapResult`.
- **Four run/map-specific handle classes** — rejected because the result type already carries the run-versus-map distinction; only sync versus async changes the control protocol.
- **A durable or reconnectable handle** — rejected because persisted state cannot preserve a live thread/task or cooperative stop channel without introducing worker leases and cross-process ownership.
- **One synthetic stopped result per requested input** — rejected because unstarted inputs have no execution history to report truthfully.
- **Propagating waiter cancellation into execution** — rejected because it bypasses cooperative stop boundaries and can leave events and checkpoints inconsistent.

## Consequences

- A stopped batch may be both `stopped` and `failed`, and `len(batch)` may be smaller than `batch.requested_count`.
- Applications that need durable recovery keep a workflow ID and checkpointer state, then create a new execution after process loss; they never store a handle as durable identity.
- Async implementations must retain and shield the framework-owned task. Sync implementations must own thread cleanup without exposing executor machinery.
- Separate handles with distinct workflow IDs are independently accepted and controllable, but the API does not promise parallel sync execution or submission order.
- API reference, how-to guidance, runnable sync/async examples, and documentation contract tests ship with the implementation so the minimal surface and stopped-batch truth cannot drift.
