## Verdict

The catalog strengthens the Run Home design; it does not justify an engine or event substrate.

- A5–A8 all belong.
- A5 needs an explicit redrive operation, not a Temporal-style reuse-policy parameter.
- A6 needs a recovery brake, but the condition should be `recovery_exhausted`, not a new quarantined `WorkflowStatus`.
- A7 is sound with exact count/percentage rules.
- A8 must derive validation from the graph’s `InterruptNode` contract, require the observed `pause_id`, and accept one typed answer value.
- Add three amendments: detached observation truth, effect-call reservation, and the human-task ownership boundary.
- Keep “no broker for correctness,” “no second durable event journal,” and state-based resume.

The catalog’s earlier “DBOS wins” conclusion answered an engine-selection question. Its useful output here is the primitive catalog, not its engine choice. DBOS still conflicts with ADR 0005’s modular ownership boundary. Compare the [catalog verdict](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/engines.md#L159) with [ADR 0005’s engine rejection](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md).

## 1. Independent catalog read

My unpruned catalog-derived list was:

1. Logical items, dispatch units, and aggregate runs need distinct identities.
2. A failed item must never poison unrelated siblings.
3. Batch members need stable item keys; positional order is not enough.
4. Every bulk result needs per-item outcomes, including partial completion after stop.
5. Run-level failure tolerance is separate from item retry.
6. Node retry and process-crash recovery need separate budgets.
7. Repeated consumer crashes need an automatic poison-work brake.
8. Recovery exhaustion needs a durable, queryable holding condition and explicit redrive.
9. An external call needs a durable pre-call reservation because its outcome may become unknown.
10. Request deduplication must reject the same identity with different parameters.
11. Dependency-aware redrive should reuse successful upstream work.
12. Decision types should be Definition artifacts, enumerable before a run pauses.
13. Invalid answers must fail before durable settlement.
14. Answers must target a specific pause occurrence; early or late answers must not drift into another pause.
15. Command acceptance, application, and actor/source audit are distinct facts.
16. Workflow timeout and human-task claim/availability clocks have different owners.
17. Query and progress views must derive from durable truth, not status mirrors.
18. Microbatching, large-result export, compensation, field-level degradation, human assignment, consensus, priorities, and bulk-by-filter controls are separate optional capabilities.

The strongest evidence is the catalog’s item/batch/run split, content-versus-delivery distinction, quarantine/redrive rule, ambiguous-outcome rule, and partial-success contract. [Catalog summary](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/README.md#L38). The detailed catalog adds exact tolerance and partial-result semantics. [Step Functions tolerance](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/patterns.md#L8), [custom-ID result correlation](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/patterns.md#L254). The HITL survey adds validator-gated settlement, declared schemas, actor audit, and separate clocks. [Temporal validator](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/hitl.md#L105), [A2I clocks](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/hitl.md#L348).

After pruning:

- Put in the Host specs now: stable Batch manifests, per-item projections, recovery limits, redrive, start fingerprints, answer contracts, pause correlation, detached views, and effect reservations.
- Name but defer: dispatch chunking, object-store result export, bulk-by-filter control, priorities, and retry-exhaustion-to-pause.
- Keep outside the Host: field-level degradation, compensation, human task claiming/consensus, authentication, and provider-side deduplication.

## 2. A5–A8 reconciliation

### A5 — terminal-run submit policy: refine

I reject a public `reuse_policy=` parameter. It would make ordinary `submit()` capable of restarting terminal work depending on a subtle option.

Use this state matrix:

| Existing workflow | Same start fingerprint | Different fingerprint |
|---|---|---|
| None | Accept new Run | — |
| Active or paused | Return duplicate/use-existing receipt | Reject `workflow_id_conflict` |
| Completed, failed, stopped, partial | Reject `already_terminal` | Reject `workflow_id_conflict` |
| `recovery_exhausted` | Reject `recovery_exhausted` | Reject `workflow_id_conflict` |

The start fingerprint must cover the Definition identity and pinned version, normalized inputs, and effective Batch configuration. Worker ID, submission time, and other operational data must not enter it. This adds Stripe-style parameter-mismatch protection, which the current “use existing” wording lacks. [Stripe precedent](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/patterns.md#L190).

Before:

```python
await host.submit(
    "ingest",
    {"document": doc},
    workflow_id="doc-42",
)  # Meaning is unspecified if doc-42 is terminal.
```

After:

```python
receipt = await host.submit(
    "ingest",
    {"document": doc},
    workflow_id="doc-42",
)

redrive = await host.redrive(
    "doc-42",
    workflow_id="doc-42-redrive-1",
)
```

My API position:

- Redrive is a distinct Host command and public verb. It is not a submit reuse policy.
- Redrive creates a new Run and records `retry_of`; it never revives or mutates the source Run.
- Version migration remains a fork, using `forked_from`. It may change Definition version or migrate state.
- These belong to one lineage family internally, but they are not one relation. The checkpointer already distinguishes them in `Run.retry_of` and `Run.forked_from`. [Current lineage fields](../../../src/hypergraph/checkpointers/types.py), [separate fork/retry operations](../../../src/hypergraph/checkpointers/base.py).

Batch redrive:

```python
# One item: creates a standalone new Run linked to the old child.
await host.redrive(
    child_workflow_id,
    workflow_id="doc-42-redrive-1",
)

# Batch source: creates a new Batch containing the failed or
# recovery-exhausted logical items from the source manifest.
await host.redrive(
    batch_id,
    workflow_id="reingest-2026-07-23-redrive-1",
)

# Optional selected subset of eligible failed items.
await host.redrive(
    batch_id,
    workflow_id="reingest-selected-redrive",
    item_keys={"doc-17", "doc-81"},
)
```

The source Batch remains immutable. A Batch redrive preserves each logical `item_key` and links each new child to its source child.

### A6 — crash-loop brake: accept behavior, change the model and name

Do not add `QUARANTINED` to `WorkflowStatus`. ADR 0005 already forbids Host phases from entering execution status, and `stranded` is reserved. [ADR 0005 status boundary](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md).

Use a coordination fact:

```python
RecoveryCondition.EXHAUSTED
```

The UI may display “Needs attention,” but the canonical term should be **recovery-exhausted**.

Exact semantics:

- `max_recovery_attempts` counts post-loss re-adoptions only; the original dispatch does not count.
- The effective cap is pinned at first submit. Changing `serve()` configuration must not silently alter an existing Run’s budget.
- A recovery attempt increments atomically when the Host accepts authority to re-dispatch an unfinished Run after process or lease loss.
- After the cap is consumed without progress, the reconciler writes `recovery_exhausted` and stops claiming the Run.
- The original Run stays unfinished and queryable. Only redrive creates new executable work.

What resets the consecutive counter:

| Event | Reset? |
|---|---|
| New committed `StepRecord` | Yes |
| Durable pause or terminal transition | Yes |
| Accepted answer starting a new execution segment | Yes |
| Attempt `STARTED` reservation | No |
| Heartbeat | No |
| New lease epoch | No |
| Worker process start | No |
| Explicit redrive | New Run starts at zero; source never resets |

A “progress commit” must mean graph progress, not any database write. Otherwise a poison node could reserve an attempt, crash, and reset its recovery budget forever.

This remains separate from node retry. Recovery never resets an open Attempt series or its retry window; the current locked model says same-workflow resume continues the existing budget. [Retry contract](../2026-07-14-retry-timeout-contract.md). ADR 0006 already says Host re-dispatch is recovery, not node retry. [ADR 0006](../../adr/0006-shared-execution-authority-uses-a-lease-with-an-epoch.md).

A6 must ship with Tier 1’s automatic restart scan. Unconditional re-adoption in the current draft is unsafe. [PRD 0011 restart scan](../../prd/0011-local-durable-host-and-sqlite-run-home.md).

### A7 — batch tolerance: accept with exact rules

Support both count and percentage in one typed, Batch-scoped policy:

```python
BatchTolerance(
    tolerated_failure_count=10,
    tolerated_failure_percentage=5.0,
)
```

Rules:

- Each field is optional; neither set means admit all manifest items.
- Percentage is `100 * failure_equivalent_items / manifest_item_count`.
- If both are set, admission closes when either threshold is exceeded.
- “Exceeded” means `>`, not `>=`.
- `FAILED` and `recovery_exhausted` children count as failure-equivalent.
- Paused, queued, running, rate-limited, retrying, and never-admitted children do not.
- Once exceeded, stop claiming new children. Already claimed children continue.
- The Batch exposes `needs_attention`; unclaimed members remain explicitly `not_admitted`.
- Tolerance does not rewrite truthful outcomes. A Batch with successes and tolerated failures remains `PARTIAL`, not “completed successfully.”

The effective policy belongs in the immutable Batch manifest. Accept it on Batch submission, not as a `serve()` default. A Host-wide default sits at the wrong level; different ingests have different failure economics.

Hyperlimit interaction is deliberately narrow:

- Waiting for a permit or cooldown is not a failure.
- Retryable 429 attempts are not Batch failures.
- A child counts only when its Run settles failed after its node policy is exhausted.
- The Host must not inspect exceptions and invent a throttle exemption.
- Fleet-wide provider admission remains separate deferred work; Batch tolerance is not a rate limiter.

### A8 — answer validation before settlement: accept and strengthen

The validator comes from the graph’s `InterruptNode` contract, not a caller-supplied `serve()` callback.

Hypergraph already requires each interrupt question type to expose `answer_type`, and `InterruptNode` publishes that as the answer port’s output type. [Interrupt contract](../../../src/hypergraph/nodes/interrupt.py), [build-time enforcement](../../../src/hypergraph/graph/validation.py). But the resume executor currently consumes the supplied answer without runtime validation. [Current answer path](../../../src/hypergraph/runners/async_/executors/interrupt_node.py).

The PauseSlot should therefore persist:

```python
PauseSlot(
    pause_id=...,
    response_key=...,
    question=...,          # occurrence-specific projection
    answer_schema=...,     # JSON-safe schema derived from answer_type
    options=...,           # occurrence-specific closed options, if any
    definition_version=...,
)
```

Public API:

```python
await host.answer(
    "refund-c-42",
    pause_id=slot.pause_id,
    value=True,
    source_ref="sp:decision:decision-918",
)
```

This corrects a real draft mismatch: PRD 0010 requires `pause_id` for settlement, while PRD 0011’s Host example omits it. [PRD 0010](../../prd/0010-durable-pause-slots-and-atomic-pause-settlement.md), [PRD 0011](../../prd/0011-local-durable-host-and-sqlite-run-home.md).

Exact validation boundary:

- Pre-settlement validation checks the stored answer schema and occurrence-specific options.
- Expected bad input returns a rejected `CommandReceipt` with `reason=invalid_answer` and structured issues. It is not an exception at the Host surface.
- The pause remains current; the caller may correct the value.
- Rejected answer commands may remain in the command audit, but they never write resume input or StepRecords.
- An identical retry of an already accepted answer returns the original receipt.
- A different value for the same settled `pause_id` is rejected; first settlement wins.
- Domain validation remains graph logic. If “technically valid but unacceptable” should re-ask, the graph validates it and loops to a new pause occurrence.
- Early answers are rejected; they are never buffered for a future pause.
- No arbitrary callable is accepted on `serve()`. That would create hidden, unversioned policy outside the Definition.

Scheduled answers use the same path:

- Validate when armed.
- Revalidate against the stored slot contract when due.
- CAS the same `pause_id`.
- If the human won first, reject the scheduled answer as stale/already settled.
- Change ADR 0008’s payload from arbitrary `values` to one typed `value`. If the graph needs `{approved, timed_out}`, that should be its declared answer type rather than an undeclared extra resume port.

## 2b. Observation and control family

The four durable concepts should mean:

- **Definition:** Graph, bound runner, pinned version, and enumerable interrupt answer contracts.
- **Run:** one graph execution with StepRecords, attempts, state, pause, and lineage.
- **Batch:** an immutable manifest of logical item keys and independent child Runs.
- **Command:** submit, answer, stop, scheduled answer, or redrive intent and its receipt.

Observation has two modes:

- **Attached:** live process objects—events, handles, `RunResult`, `MapResult`.
- **Detached:** Run Home projections—watch updates, durable inspection, progress snapshots.

| Surface | Durable-tier story |
|---|---|
| `show_progress` / `RichProgressProcessor` | Tier 0 stays event-driven. Durable progress derives from the Batch manifest, child Run facts, valid claims, pauses, and recovery conditions. Reuse the renderer-neutral presentation layer, not the existing event processor unchanged. `RichProgressProcessor` currently owns an event tracker directly. [Implementation](../../../src/hypergraph/events/rich_progress.py) |
| Progress after crash | Completed children stay completed. An `ACTIVE` Run without a valid claim must not render as running. Re-adopted children show running/recovering; paused children show awaiting decision; recovery-exhausted children show needs attention. |
| `RunResult.inspect()` / logs | Remains attached settled truth. A detached `RunInspector` view exposes Run metadata, StepRecords, folded state, attempts, commands, pause, lineage, version, and coordination facts. The existing inspector already provides runs, steps, state, checkpoint, and lineage. [RunInspector](../../../src/hypergraph/checkpointers/inspection.py) |
| What detached inspection cannot claim | Exact exception objects, the exact local `RunLog`, uncommitted checkpoint-write failures, dropped streaming chunks, event-processor state, or other facts that died with the worker. `RunResult` explicitly carries these local surfaces. [RunResult fields](../../../src/hypergraph/runners/_shared/results.py) |
| Viz | Render the registered Definition and overlay durable StepRecords when the pinned Definition version is available. Without that version, show recorded node addresses and history, not a guessed current graph. |
| `start_run` / `start_map` handles | Tier-0-only. Receipt + `watch()` + `host.stop()` are the detached operational twin, but they must not share a type or pretend a receipt has `result()`. Handles contain a live task/thread and stop signal. [Handles](../../../src/hypergraph/runners/_shared/handles.py) |
| `runner.iter()` | Attached live events, including a bounded lossy chunk preview. `watch(Run)` is durable fact replay plus optional live preview; it is not replay of `iter()` events. [Iter contract](../../../src/hypergraph/runners/async_/runner.py) |
| `runner.map_iter()` | `watch(Batch)` has a similar consumption shape—child outcomes arrive as they commit—but different semantics. `map_iter()` backpressures execution when its consumer is slow; a detached watcher must never control Batch admission. [Backpressure contract](../../../src/hypergraph/runners/_shared/template_async.py) |
| Interrupts inside a Batch | Each child owns its PauseSlot. Batch summary exposes `awaiting_decision_count` and child pause references. `answer()` addresses the child workflow ID plus pause ID. Paused children never count toward A7 tolerance. |
| Current map caveat | Runner-level `map()` supports paused child results, but intentionally does not support in-place child resume. [Pinned behavior](../../../tests/test_map_interrupt_items.py). A durable Batch must therefore admit independent top-level child Runs, not wrap one resumable `runner.map()` parent. |

### One-store truth

| Fact | Authority |
|---|---|
| Execution status, outputs, state, checkpoint | Run + StepRecords |
| Retry and unknown-outcome evidence | Attempt ledger |
| Current pause and accepted answer | PauseSlot + settlement transaction |
| Submit/answer/stop/redrive audit | Command log |
| Definition version, claim, epoch, recovery condition | Coordination facts |
| Batch membership and policy | Batch manifest |
| Progress, inspection, summaries | Derived views of the rows above |

A cached summary is legal only if it is updated transactionally with its source facts and can be rebuilt. It must never become a second status authority. This keeps ADR 0005’s “existing checkpointer plus coordination facts” rule intact. [ADR 0005](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md).

## 3. Catalog findings not covered by A1–A8

| Finding | Decision | Owner / trigger |
|---|---|---|
| Effect `dispatch_state` before provider calls | **Adopt now as a build gate.** Reuse `AttemptStatus.STARTED`; do not invent another state. Widen reservation/effect identity coverage to every explicitly effectful node. | Hypergraph, future PRD 0014. Current coverage only applies to retry/timeout FunctionNodes. [Coverage check](../../../src/hypergraph/runners/_shared/attempts.py) |
| Command actor/source audit | **Adopt now.** Commands may carry an opaque `source_ref`, stored in the command record but never used as authentication or dedup identity. | Product/sp authenticates; Host records the reference. This matches the existing “joined by reference” design. |
| Non-interrupting reminders | **Adopt the ownership correction now.** They are not scheduled answers because they must not consume the PauseSlot. | sp/product notification or human-task plane. ADR 0008 currently claims scheduled answers also serve reminders and should be corrected. [Current claim](../../adr/0008-timed-continuation-is-a-scheduled-pause-slot-command.md) |
| Human task’s unclaimed and claimed clocks | **Name now, build outside Hypergraph.** | sp/product owns assignment, claim, reservation expiry, reminders, and escalation. Host owns only workflow continuation deadlines. |
| `on-max-attempts: pause` | **Defer with trigger.** | Graph feature when real users need failure-to-pause routing. It must not become a RetryPolicy flag because retry cannot catch `PauseExecution` or change graph control flow. |
| Content vs delivery failure | **Adopt vocabulary boundary.** `recovery_exhausted` is delivery/worker failure; content degradation is graph/product output. | Host handles delivery recovery only. |
| Field-level degradation and `_meta.changes` | **Explicitly out.** | Domain graph, HyperTable, contentbase, or Panda record model. The Host must not interpret or null domain fields. |
| Dispatch chunking / `ItemBatcher` | **Defer with trigger:** measured per-child scheduling/storage overhead. | Host Batch implementation. Call it a dispatch chunk, not another Batch. |
| Large-result `ResultWriter` | **Defer with trigger:** child outputs or StepRecords become too large for Run Home retention. | Artifact store adapter; Batch manifest keeps references. |
| Compensation/sagas | **Explicitly out.** | Graph/domain components declare compensation. Host never guesses how to undo side effects. |
| Human claim/assignment/consensus | **Explicitly out.** | sp/product task plane. A PauseSlot is not a work queue. |
| Priorities and bulk-by-filter operator commands | **Defer with trigger:** measured contention or operator need across many Runs. | Shared Host tier. |
| Partial-success response | **Adopt through A2.** Every Batch view returns keyed child outcomes, including paused, stopped, failed, recovery-exhausted, and not-admitted. | Batch PRD. |

The most important gate is effect reservation. Today, a plain FunctionNode without retry or timeout can execute, crash before its StepRecord, and execute again. PRD 0011 already admits this window but treats node-boundary writes and effect identity as future work. [PRD 0011](../../prd/0011-local-durable-host-and-sqlite-run-home.md). That is acceptable for a clearly labeled Tier 1 alpha, but it must gate production side-effect claims and the shared-tier kill matrix.

## 4. Final amendment package

| # | Normative text | Target |
|---|---|---|
| **A1** | Durable Host delivery shall start with submit, stop, watch, and restart recovery; PRD 0010 gates answer and scheduled-answer support only, not the whole Host. | ADR 0005 consequence; PRDs 0010–0011 |
| **A2** | A durable Batch shall be an immutable manifest of stable logical item keys and independent child Runs, with keyed per-item outcomes and no parent RunResult array as durable truth. | ADR 0005; new Batch PRD |
| **A3** | Host work admission and provider-resource admission shall remain separate: the Host controls child claims, while an injected provider limiter controls call permits and waiting never becomes a Batch failure. | ADR 0005; PRD 0011; later shared-host PRD |
| **A4** | No broker or stream may own workflow truth or recovery; notification may reduce latency, and an optional OutputLog may be added only for an explicit replayable-output requirement. | ADR 0005 `watch()` and rejected-options text |
| **A5 — changed** | Reusing a workflow ID shall return use-existing only for fingerprint-identical nonterminal work, reject terminal work as `already_terminal`, reject payload/version mismatch as `workflow_id_conflict`, and require explicit `redrive()` to create a new `retry_of` Run. | ADRs 0005 and 0007; PRD 0011 |
| **A6 — changed** | Each Run shall pin a finite recovery-attempt cap; repeated re-adoptions without committed graph progress shall produce the coordination condition `recovery_exhausted`, skipped by reconciliation and recoverable only by redrive into a new Run. | ADRs 0005–0006; PRD 0011; shared-host PRD |
| **A7** | A Batch may pin count and percentage failure tolerances; exceeding either shall atomically close new-child admission while already claimed children continue and the Batch exposes truthful partial and needs-attention facts. | New Batch PRD |
| **A8 — changed** | Every PauseSlot shall persist the graph-derived answer schema and occurrence options; `answer()` and scheduled answers shall require the observed `pause_id`, validate one typed `value` before settlement, and leave the slot open after rejection. | PRD 0010; PRD 0011; ADR 0008 |
| **A9 — new** | Detached progress, inspection, and `watch()` shall derive solely from Run Home facts; live events remain preview, no `RunResult` is reconstructed, and a Batch watcher never backpressures execution. | ADR 0005; PRD 0011; Batch PRD |
| **A10 — new** | Every explicitly effectful node shall reserve a durable attempt/effect identity before its provider call, and a crash without witnessed settlement shall surface `OUTCOME_UNKNOWN` rather than automatic respend. | ADR 0006; future PRD 0014 |
| **A11 — new** | The Host shall own workflow continuation clocks and record optional command source references, while non-interrupting reminders, human-task availability/claim clocks, assignment, authentication, and consensus remain in sp or the product. | ADRs 0005 and 0008; PRD 0010; sp relationship note |

The explicit disagreements are A5’s new `redrive()` verb, A6’s rejection of “quarantined state” and same-Run revival, and A8’s replacement of arbitrary `values`/validator hooks with mandatory `pause_id` plus one graph-typed `value`. A7 stands as proposed with the rules above.
