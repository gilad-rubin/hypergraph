# Durable hosts accept commands; runners retain execution semantics

**Status:** Accepted on 2026-07-23 at the durable-host amendment sitting
(decisions and provenance: `docs/research/2026-07-23-durable-host-amendments.md`;
delivery contract: `docs/prd/0017-durable-host-v1-program.md`). Amendments
A1–A16 folded on 2026-07-24 (Durable Host V1 ticket 01). Grounded in the
three-model clean-room convergence (`docs/research/2026-07-21-cleanroom-durable-host-experiment.md`)
and the canon grill (`docs/research/2026-07-21-durable-host-canon-grill.md`).
Extends — never supersedes — ADR 0004: handles stay process-local live
control.

## Context

Everything that makes `runner.run()` safe lives in one process's memory: the
active-run registry, the stop signal, event delivery, duplicate-run
prevention. Webhooks retry, processes crash, humans park runs for a week, and
deploys happen with runs in flight. The missing layer is a durable home for
*intent and authority* — not a second execution engine.

## Decision

- **One new composition root, additive.** A `serve(...)` call takes
  Definitions — each a root graph carrying its own runner via a declarative
  binding plus its deployment identity — plus one **Run Home** (the single
  store owning runs, steps, host commands, and claims). Direct
  `runner.run()` / `start_run()` remain first-class and unchanged (Tier 0);
  the host is never a toll booth.
- **Public ownership is split once (A13).** The Definition-bound **Host**
  owns new Run submission, Batch submission, and worker lifecycle. One
  backend-neutral **`RunHomeClient`** owns all existing-work reads and
  controls — `get`, `list`, `watch`, `answer`, `stop`, `rerun` — for `RunRef`
  and the applicable `BatchRef` operations. The Host exposes that client but
  does not copy its methods as pass-through conveniences. `RunView` names why
  a Run waits; `BatchView` names counts and unstarted items; `RunQuery`
  covers definition/status/age/batch/recovery/lineage/pagination. (The
  Tier-1 local host ships `RunQuery` with definition/status/waiting/age/
  limit; the lineage, batch, and pagination query fields land with the
  Batch tickets 05/06.) Raw SQL is
  an unsupported escape hatch; bulk-by-query mutation stays deferred. The
  client contract, inert refs, and watch sequences are specified in PRD 0018.
- **Receipts carry inert addresses (A12).** Every Run-targeting receipt
  carries an immutable, serializable `RunRef`; every Batch receipt carries an
  inert `BatchRef`. Neither exposes liveness, result, status, or control
  methods. "Durable handle" stays a banned term (ADR 0004 holds: handles are
  process-local live control).
- **New submission stays Definition-bound (owner decision 5).** A process
  without loaded Definition code cannot submit new work; the Run Home grows
  no Definition catalog. An app-less `RunHomeClient` inspects and controls
  existing work only.
- **The Run Home is the existing checkpointer plus coordination facts —
  never a second execution journal.** Steps remain the source of truth.
  Coordination facts (command sequence, claim, pinned Definition identity,
  scheduled answers) live in adjacent structures; no independently written
  terminal status. Host coordination states never enter `RunStatus` or
  `WorkflowStatus`.
- **Durable command intake with name-based dedup.** `submit()` dedupes on
  `workflow_id`; use-existing applies only to fingerprint-identical
  nonterminal work, where the fingerprint covers the complete Definition
  identity, normalized inputs, effective Batch configuration, and requested
  `start_at` (A5). Terminal reuse returns a typed `already_terminal`
  conflict; fingerprint mismatch returns a typed `workflow_id_conflict`.
  `answer()` dedupes on the durable `pause_id` (one answer per pause
  occurrence, atomically checked); `stop()` is idempotent. Callers never
  manage idempotency keys; a `CommandReceipt` records
  acceptance/application only — never terminal execution truth. Commands may
  carry an opaque `source_ref` for audit; it is never authentication and
  never affects deduplication (A11).
- **Repetition is `rerun()`; migration is `fork()` (A5, A16).** Failed,
  stopped, or recovery-exhausted work starts again only through an explicit
  `rerun()` into a new Run with a new workflow id and `retry_of` lineage;
  rerunning completed work is an ordinary new submit. A Batch rerun mints a
  **new immutable Batch manifest** with a new `BatchRef` and explicit Batch
  lineage, containing new child Runs only for the selected source item keys;
  it never mutates the source Batch. Any Definition-identity
  change is an explicit compatibility-checked `fork()` recording
  `forked_from` and a migration reason. The lineage relations never merge.
  `redrive` is rejected vocabulary.
- **A durable Batch is an immutable manifest (A2).** Unique stable logical
  item keys, independent child Runs, keyed outcomes, and explicit unstarted
  items — never a durable parent `MapResult` array. One transaction persists
  the manifest, child Run identities, pinned inputs, and the accepted start
  command. The full Batch contract is specified in PRD 0019.
- **Every durable fact carries a gap-free sequence (A9).** Every Run fact
  receives one monotonic per-Run update sequence; every Batch manifest or
  child-outcome fact receives one monotonic per-Batch sequence, with child
  and Batch facts committing in the same transaction. `watch(after=cursor)`
  resumes the addressed Run or Batch without gaps. Live previews are marked
  non-durable and never advance the cursor. Detached observation derives
  solely from Run Home facts — no `RunResult` reconstruction — and a Batch
  watcher never backpressures execution.
- **Work admission and provider-resource admission stay separate (A3).** The
  Run Home owns a runtime-tunable active-Run cap (A15): over-limit work waits
  in claim order; queued, future-scheduled, version-incompatible, and
  recovery-exhausted Runs consume no slot; a claimed Run waiting on a
  provider permit still consumes its Host slot. Provider-resource limits are
  injected separately and may exist at graph, node, and component scope,
  with scope names that stay honest about being process-local or
  distributed. For an underlying provider quota the shared component is
  often the preferred owner: several graphs and nodes reuse it, the
  component owns admission, and it acquires at the exact scarce call.
  Graph- and node-level limits compose as narrower work budgets; they never
  replace the component's quota. Waiting on a provider permit is neither a
  failure nor a retry attempt — ordinary throttling never consumes retry
  policy. Overflow strategies (reject, cancel-oldest, cancel-newest),
  expression-language keys, and keyed fairness are excluded from v1.
- **One-shot delayed starts are durable (A14).** `submit(start_at=…)`
  persists and fingerprints the Run immediately; store-authoritative time
  controls claim eligibility, a past time is immediately eligible, and
  stop-before-due prevents execution. Cron recurrence and generic scheduled
  commands stay out.
- **`host.run()` is excluded from v1.** `RunResult` carries local-only
  evidence (exact exception objects, `RunLog`, checkpoint-write evidence)
  that a detached worker cannot reconstruct truthfully. Durable serving is
  `submit()` + `watch()`. A future durable result projection requires a new
  ADR that explicitly extends ADR 0004.
- **`watch()` is durable replay plus live preview.** History is replayed
  from StepRecords and host commands in durable sequence order; live events
  remain best-effort preview, per the existing event-processor contract.
  Full event replay is not promised.
- **No broker, stream, or OutputLog owns workflow truth or recovery (A4).**
  A notification may cut wake latency but never carries truth. Replayable
  output needs its own explicit contract and is deferred until a real
  product need exists (owner decision 4).
- **Host delivery starts with submit/stop/watch/restart-recovery (A1).**
  Durable pause slots (PRD 0010) gate only `answer()` and scheduled answers;
  the local Host wedge lands first.
- **Runner binding clones, never mutates.** `serve()` binds each supplied
  runner to the Home's checkpointer via an immutable cloning contract; a
  runner that cannot satisfy host requirements (today: Daft — no
  checkpointing, no events) fails at construction, loudly.

## Considered and rejected

- External engines as the durability layer (Temporal/DBOS/Restate — replay
  engines must own control flow; DBOS is a process singleton whose OSS
  recovery has no fleet failover): `docs/research/2026-07-21-durable-execution-landscape.md`.
- A durable `ExecutionHandle`: rejected by ADR 0004; receipts carrying inert
  refs plus watch are the durable surfaces.
- Engine-backed host internals (DBOS as dedicated worker) remain a possible
  Tier 3 implementation of THIS contract if a deployment ever needs
  fleet-wide flow control; no compatibility promise is made now.

## Consequences

- The notebook→production path is construction-time only: the same graphs
  and verbs run against a local (SQLite) or shared (Postgres) Run Home.
- The host is fully usable without superposition; sp joins later by
  reference (`docs/research/2026-07-21-superposition-relationship.md`).
- PRD 0010 (durable pause slots) no longer gates the local Host wedge (A1);
  it gates only the `answer()` verb and scheduled answers.
