## Verdict

The synthesis finds several useful holes, but its package needs correction before it enters the specs:

- Keep G2, G3, G4, and the narrow part of G5.
- Recast G1 as an inert `RunRef`, not a durable handle.
- Split G6 into durable output versus mutable progress; keep both deferred.
- Close G7: ADR 0007 already assigns new-code continuation to an explicit fork.
- Add two missed requirements now: resumable `watch()` cursors and clear admission-slot accounting.
- Reject two Tier 0 backlog items as written: persisted exact-argument failure snapshots violate the privacy contract, and graph-wide retry defaults contradict the locked node-only retry decision.

## 1. Gap audit

| Gap | Verdict | Correct weight |
|---|---|---|
| **G1 — universal re-attach handle** | The ergonomic need is real; the proposed object is wrong. Temporal has a true full-control handle, but Prefect does not provide universal re-attachment, Resonate’s handle is mainly result-shaped, and DBOS splits handle methods from client/operator verbs. The synthesis overstates convergence. | **Low/medium:** add a typed address, not a second client surface. |
| **G2 — query-then-act** | Real but partly covered. Hypergraph already has a backend-neutral `RunInspector` with run, step, state, checkpoint, and lineage queries. The missing part is Host coordination facts, durable Batch queries, recovery conditions, and a stable detached `RunView`. [Existing inspection seam](../../../src/hypergraph/checkpointers/inspection.py) | **High:** needed for the Panda kill proof and operator truth. |
| **G3 — app-less operator client** | Real, and accidentally omitted from section V. DBOS, Restate, and pgflow all show the value of operating without graph imports. [DBOS client finding](dbos.md#9-the-3-steals-1) | **High for read/control; deferred choice for submit.** |
| **G4 — delayed start** | Real. Multiple reports converge on one-shot delayed submission. But it does not justify a generic “schedule any command” framework. | **Medium:** useful Tier 1 feature, not a Panda wedge gate. |
| **G5 — admission** | Runtime-tunable capacity is real. Keyed fairness is a valid later need. The strategy matrix is premature and repeats Inngest/Prefect’s control sprawl. Prefect documents four overlapping mechanisms; Inngest has a compatibility matrix and misleading drop-vs-delay names. [Prefect warning](prefect.md#10-the-warnings) [Inngest warning](inngest.md#10-the-warnings) | **Medium now:** tunable cap and honest slot rules. Keying later. |
| **G6 — progress/output channel** | Misclassified. Trigger.dev metadata is mutable run-scoped state; DBOS streams are append-only ordered output. They are not one primitive. [Trigger metadata](rest-group.md) [DBOS streams](dbos.md#9-the-3-steals-1) | **Deferred:** only after a concrete resumable-output requirement. |
| **G7 — redrive onto new code** | Not a gap. ADR 0007 already says migration uses an explicit fork into a new workflow. Adding `target_version` to `redrive()` would collapse two distinct lineage claims. [ADR 0007](../../adr/0007-durable-runs-pin-compatibility-and-migrate-by-explicit-fork.md) | **Close; clarify the verbs.** |

### Warnings the synthesis was about to repeat

- “Receipt is a handle” directly contradicts both accepted ADR 0004 and proposed ADR 0005. ADR 0004 limits handles to live `done/stop/result` control and rejects reconnectable handles; ADR 0005 says a receipt records command acceptance, not execution truth. [ADR 0004](../../adr/0004-background-handles-control-live-work.md) [ADR 0005](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md)
- “Exact identical signatures” would pull local-only inspection/event knobs into `host.submit()` or durable-only options into `runner.run()`. Preserve matching names and meaning for shared fields, not byte-for-byte signatures.
- Persisting exact failure arguments conflicts with the existing privacy rule excluding exception args, stacks, and arbitrary representations from durable records. [Attempt privacy contract](../../../src/hypergraph/checkpointers/types.py)
- Graph-wide retry defaults directly contradict the approved node-only policy: only the node that owns the callable may authorize repetition. [Locked retry ownership](../2026-07-14-retry-timeout-contract.md)

## 2. Section V dispositions

### A12 — refine: `RunRef`, not a handle

Yes, receipt-as-handle collides with ADR 0004.

A command receipt and a Run have different identities:

- One Run may accumulate submit, answer, stop, and redrive-related receipts.
- A receipt says whether one command was accepted or applied.
- A Run reference only addresses persisted Run facts.
- A live handle owns a process-local control channel.

Before:

```python
receipt = await host.submit(...)
await receipt.stop()
handle = host.handle(workflow_id)
```

After:

```python
receipt = await host.submit(...)
ref = receipt.run_ref                 # RunRef("refund-c-42")

view = await client.get(ref)
await client.stop(ref)

async for update in client.watch(ref, after=cursor):
    ...
```

`RunRef` should be an immutable, serializable value with no `done`, `result`, `status`, `stop`, `answer`, or `redrive` methods. Host/client verbs accept it. This avoids a shallow object that merely mirrors every Host method.

The glossary must define:

> **Run reference:** An immutable address for one persisted Run. It carries no worker liveness, ownership, control channel, or settled result.
>
> _Avoid_: Execution handle, durable handle.

That resolves the current glossary’s deliberate “handle = process-local” rule. [Current vocabulary](../../../CONTEXT.md)

### A13 — refine and merge G3

Accept a typed read contract and defer bulk mutation. Reject raw SQL as the stable interface.

Normative shape:

```python
client = RunHome.open("file:./runs.db").client()

view = await client.get(ref)
page = await client.list(RunQuery(
    definition="refund",
    batch_id=batch_ref,
    recovery_condition=RecoveryCondition.EXHAUSTED,
))
async for update in client.watch(ref, after=cursor):
    ...
```

Requirements:

- `RunView` is a durable projection, never a reconstructed `RunResult`.
- `RunQuery` covers definition, execution status, age, Batch, recovery condition, lineage, and pagination.
- Views expose why work is waiting: scheduled, admission-limited, paused, version-incompatible, or recovery-exhausted. These remain coordination facts, not `WorkflowStatus`.
- SQLite and Postgres implement one backend-neutral interface.
- Raw SQL may remain an unsupported escape hatch; schema compatibility is not public.
- Future bulk commands must reuse `RunQuery`, but wait for a real operator need beyond one Batch or an explicit ID set.

The app-less client can safely support `get/list/watch/answer/stop/redrive`, because those derive from persisted facts. App-less `submit` is a separate question; see owner decision five below.

### A14 — refine: delayed start, not generalized commands

Accept `start_at`, reject generic scheduled commands.

Normative semantics:

> `submit(start_at=...)` shall reserve the Run, persist its start command, Batch manifest, inputs, and pinned version immediately, include `start_at` in the submit fingerprint, and exclude it from claims until Run Home time reaches that instant. A past time is immediately eligible; stop-before-due prevents execution. Recurrence remains outside the Host.

Scheduled answers remain pause-scoped commands with `pause_id` CAS. Both features may share a due-row scanner, but not one public generic command abstraction. ADR 0008 is deliberately narrower. [ADR 0008](../../adr/0008-timed-continuation-is-a-scheduled-pause-slot-command.md)

### A15 — refine: tunable capacity, one v1 behavior

Accept runtime tuning. Reject the strategy matrix in v1.

Normative semantics:

> The Run Home shall persist an operator-updatable `max_active_runs`. Accepted work above the cap waits in claim order. Paused, future-scheduled, version-incompatible, and recovery-exhausted Runs consume no active slot; a claimed Run blocked inside a provider limiter still consumes one until it reaches a durable boundary.

Also:

- Provider-resource admission remains A3’s separate injected limiter.
- Keyed admission stays deferred.
- When keying lands, the Definition derives and persists a concrete `admission_key`; do not introduce CEL or another expression language.
- `reject`, `cancel-newest`, and `cancel-in-progress` remain out. The last two generate destructive workflow commands and require their own semantics.

### A16 — reject as written; replace with a relation clarification

Before:

```python
await host.redrive(source, target_version="refund-v2")
```

After:

```python
# Retry the same work under the same pinned Definition version.
await host.redrive(source, workflow_id="refund-redrive-1")

# Move recorded state to a different Definition version.
await host.fork(
    source,
    workflow_id="refund-migration-1",
    target=DefinitionRef("refund", version="v2"),
    reason="version_migration",
)
```

State-based recovery avoids replay determinism errors; it does not prove that old checkpoint values are meaningful to new code. A migration still needs compatibility validation and explicit `fork_from` lineage.

### OutputLog sharpening — refine only

Keep it behind owner decision four. If built:

> `OutputLog` shall be an append-only set of named channels with opaque cursors, stable append identities, close and retention semantics, and explicit crash behavior. It shall never own commands, workflow status, recovery, or mutable progress counters.

Do not claim exactly-once node writes until A10 defines how chunk identity survives “append succeeded, StepRecord did not.”

### Tier 0 backlog — accept as a separate track, but prune it

Keep:

- target-output/run-plan ergonomics;
- typed projection streams;
- liveness timeouts as a separate ADR;
- MCP/CLI only after A13 gives them a stable interface.

Reject or reframe:

- Persisted exact-argument `ErrorSnapshot`: local opt-in only; never a durable default.
- Graph-wide retry defaults: contradict the locked node-owned safety decision.
- “MCP nearly free”: exposing a tool safely requires auth, input projection, receipt semantics, and the app-less-submit decision.

### Morphology rules — refine before canon

Adopt:

- Durable verbs require caller-chosen `workflow_id`.
- Shared fields keep the same names and meaning across Tier 0 and Host.
- Sync and async runner types use the same verb names.
- No version parameter on event/watch formats.
- No overloaded “input means start/resume/control” channel.
- Destructive operations require explicit verbs and targets.

Reject “identical signatures.” Put these in an API style guide or the Agent Guide’s design section, not `CORE-BELIEFS.md`; they are interface rules, not timeless graph laws.

## 3. Coherence and owner decisions

### Contradictions resolved

- A12 no longer contradicts ADR 0004/0005.
- A13 extends the existing inspection seam instead of creating a second query module.
- A14 and A8 share implementation plumbing but retain distinct semantics.
- A15 keeps work admission separate from Hyperlimit/provider admission.
- A16 preserves `retry_of` versus `forked_from`.
- OutputLog no longer competes with A9’s derived progress.
- A9 should gain resumable cursors: durable updates have a cursor; best-effort previews do not.

### The four prior owner decisions

The atlas does not change them:

1. Panda’s Batch/restart wedge should still precede durable pause work.
2. Batch should still be an immutable manifest of independent child Runs.
3. OutputLog remains a product-requirement decision, not Host infrastructure by default.
4. Tier 2 keyed/fleet admission remains deferred until a real multi-worker deployment.

It does expose a fifth decision that the synthesis currently hides:

> **Must a process with no graph code be allowed to submit a new Run?**

- **No:** app-less clients inspect and control existing Runs; new submission stays on the Definition-bound Host.
- **Yes:** Run Home needs a durable Definition catalog containing at least name, exact version identity, input contract, and fingerprint rules. Having `serve()` write that catalog automatically is still registration behavior, even if users never call `register()`.

My lean: **no for Tier 1**. Panda can submit through its Host-bearing application process. Reopen when a separate scheduler/UI must create Runs without importing the application.

### Correct build order

1. Resolve the existing Definition version identity and accept the core ADRs.
2. Specify `RunRef`, `RunView`, `RunQuery`, the app-less read/control client, and resumable watch cursors.
3. Build the PRD 0011 wedge: submit, stop, watch, restart, Batch, recovery brake, and admission cap.
4. Run Panda’s crash/restart proof.
5. Widen effect reservation per A10 before production side-effect claims.
6. Add runtime admission tuning and `start_at`.
7. Add pause settlement, validated answers, and scheduled answers.
8. Build Postgres leases/shared claims.
9. Build OutputLog only after an actual durable-output use case.

## 4. Final A1–A16 amendment table

| # | Normative text | Target |
|---|---|---|
| **A1** | Durable Host delivery shall start with submit, stop, watch, and restart recovery; pause slots gate answer and scheduled-answer support only. | ADR 0005; PRDs 0010–0011 |
| **A2** | A durable Batch shall be an immutable manifest of stable logical item keys and independent child Runs, with keyed outcomes and no durable parent `MapResult` array. | ADR 0005; Batch PRD |
| **A3** | Host work admission and provider-resource admission shall remain separate: the Host controls claims while injected provider limiters control call permits. | ADR 0005; local/shared Host PRDs |
| **A4** | No broker, stream, or OutputLog may own workflow truth or recovery; notification may reduce wake latency, and replayable output requires a separate explicit contract. | ADR 0005 |
| **A5** | Reusing a workflow ID shall use-existing only for fingerprint-identical nonterminal work, reject terminal work as `already_terminal`, reject mismatches as `workflow_id_conflict`, and require `redrive()` to create a new `retry_of` Run. | ADRs 0005/0007; PRD 0011 |
| **A6** | A Run shall pin a finite recovery-attempt cap; repeated adoption without committed graph progress shall set the coordination condition `recovery_exhausted`, remove it from reconciliation, and require redrive into a new Run. | ADRs 0005/0006; local/shared Host PRDs |
| **A7** | A Batch may pin count and percentage failure tolerances; exceeding either closes new-child admission while claimed children settle and the Batch exposes truthful partial facts. | Batch PRD |
| **A8** | A PauseSlot shall persist its graph-derived answer contract; every immediate or scheduled answer shall name the observed `pause_id`, validate one typed value before settlement, and leave the slot open after rejection. | PRD 0010; ADR 0008 |
| **A9** | Detached progress and inspection shall derive only from Run Home facts; `watch(after=cursor)` shall resume durable updates without gaps, preview updates shall remain non-resumable, and no `RunResult` shall be reconstructed. | ADR 0005; PRD 0011; Batch PRD |
| **A10** | Every explicitly effectful node shall reserve a durable attempt/effect identity before its provider call; unwitnessed settlement shall surface `OUTCOME_UNKNOWN`, never automatic respend. | ADR 0006; PRD 0014 |
| **A11** | The Host shall own workflow-continuation clocks and optional command source references; human-task assignment, claims, reminders, authentication, and consensus remain in sp or the product. | ADRs 0005/0008; sp relationship note |
| **A12 — refined** | Every Run-targeting receipt shall carry an inert `RunRef`; a `RunRef` is a serializable address with no liveness, result, status, or control methods, and Host/client verbs accept it. | ADRs 0004/0005; `CONTEXT.md` |
| **A13 — refined** | Every Run Home shall expose a backend-neutral app-less read/control client with typed `RunView`/`RunQuery`, resumable watch, and run-targeted commands; raw SQL is not public, and bulk-by-query mutation remains deferred. | ADR 0005; PRD 0011 |
| **A14 — refined** | `submit(start_at=...)` shall persist and deduplicate the Run immediately but defer claim eligibility until store-authoritative time reaches `start_at`; recurrence and generic scheduled commands remain out. | ADR 0005; PRD 0011 |
| **A15 — refined** | Run Home shall persist a runtime-tunable active-Run cap; over-limit work waits, parked/nonclaimable Runs consume no slot, provider waits remain separate, and keyed/destructive strategies remain deferred. | ADR 0005; local/shared Host PRDs |
| **A16 — replaces proposal** | `redrive()` shall retain the source Definition version and record `retry_of`; any target-version change shall use an explicit compatibility-checked fork recording `forked_from` and migration reason. | ADR 0007; PRD 0011; Batch PRD |

Explicit disagreements with the synthesis: receipt is not a handle; SQL is not the public read interface; scheduled starts do not generalize all commands; v1 admission has no strategy matrix; redrive cannot change versions; OutputLog does not absorb mutable progress; raw persisted failure arguments and graph-wide retry defaults do not enter the Tier 0 backlog.
