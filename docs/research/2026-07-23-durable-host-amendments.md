# Durable-host amendment package: four review rounds, sixteen amendments, five owner decisions

- Date: 2026-07-23
- Issue: none yet; design-refinement capture feeding the ADR-acceptance sitting
- Implementation: none; the ADR/PRD set (0005–0008, 0010–0011) stays as written until the owner accepts
- Status: proposed — everything below is input to one acceptance sitting; nothing is canon yet

> **Intent, not canon.** This note consolidates the 2026-07-23 refinement of the
> durable-host design so the acceptance sitting reads one document instead of
> nine. Raw research: the sibling `2026-07-23-api-atlas/` folder, the 07-21 design
> docs, and superposition's `2026-07-16-ingestion-lifecycle-primitives/`.

## Provenance — four adversarial rounds

1. **Clean-rooms (07-21).** Three isolated models converged on the Run Home design; ADRs 0005–0008, PRDs 0010–0011.
2. **Event-substrate steelman (07-23, Codex gpt-5.6-sol xhigh, same thread as the canon grill).** Built the strongest "hypergraph on an event/actor/stream substrate (S2, Rivet)" design, then rejected it: a stream store is append/read/follow, not a work runtime; two stores split truth for command races; events-as-foundation is the replay family, which must own control flow. Produced A1–A4.
3. **Engine-catalog audit (07-23).** Cross-read the 07-16 ingestion-lifecycle catalog (which the 07-21 design never cited) against the ADR/PRD set. Produced A5–A8; round-2 Codex review refined three of them and added A9–A11.
4. **API atlas (07-23).** Nine fixed-template reports covering twelve frameworks (pipefunc, Hamilton, LangGraph, Prefect, DBOS, Restate, Inngest, Hatchet, Temporal, Trigger.dev, pgflow, Resonate), synthesized and then audited in Codex round 3. Produced A12–A16 plus the Tier-0 track.

## The sixteen amendments (normative one-liners; targets in parentheses)

| # | Amendment |
|---|---|
| A1 | Host delivery starts with submit/stop/watch/restart-recovery; pause slots (PRD 0010) gate only `answer()` and scheduled answers. (ADR 0005; PRDs 0010–0011) |
| A2 | A durable Batch is an immutable manifest of unique stable logical item keys and independent child Runs, with keyed outcomes and explicit unstarted items — never a durable parent `MapResult` array. One transaction persists the manifest, child Run identities, pinned inputs, and accepted start command. (ADR 0005; Batch PRD) |
| A3 | Work admission (host claims) and provider-resource admission (injected limiter, e.g. hyperlimit) stay separate; waiting on a permit is never a failure; scope names must be honest. (ADR 0005; PRD 0011) |
| A4 | No broker/stream/OutputLog may own workflow truth or recovery; notification may cut wake latency; replayable output needs its own explicit contract. (ADR 0005) |
| A5 | Workflow-id reuse: use-existing only for fingerprint-identical nonterminal work (fingerprint = full Definition identity + normalized inputs + effective Batch config + `start_at`); terminal → `already_terminal`; mismatch → `workflow_id_conflict`. Failed, stopped, or recovery-exhausted work starts again only through explicit `rerun()` into a new Run with a new workflow id and `retry_of`; repeating completed work is an ordinary new submit with a new workflow id. (ADRs 0005/0007; PRD 0011) |
| A6 | Each Run pins a finite recovery-attempt cap at first submit; re-adoption without committed graph progress sets coordination condition `recovery_exhausted` (never a WorkflowStatus), skipped by reconciliation, revived only by `rerun()` into a new Run. Counter resets only on committed StepRecord, durable pause, or terminal transition. (ADRs 0005/0006; PRD 0011) |
| A7 | Batch may pin count and percentage failure tolerances (both optional, either trips, strictly `>`), in the immutable manifest, not `serve()`; percentage denominator = total logical items in that manifest. Failure-equivalent = FAILED + recovery-exhausted children; paused/queued/admission-limited children never count. Tripping closes new-child admission, claimed children settle, remaining items become explicitly unstarted, and the Batch stays truthfully PARTIAL. (Batch PRD) |
| A8 | PauseSlot persists the graph-derived answer contract (`answer_type` → JSON `answer_schema` + occurrence options); every answer — human or scheduled — names the observed `pause_id`, validates one typed `value` before settlement, and leaves the slot open on rejection; no `serve()` validator callables. Fixes PRD 0011's example omitting `pause_id`; narrows ADR 0008's payload to one declared value. (PRD 0010; ADR 0008) |
| A9 | Detached progress/inspection/watch derive solely from Run Home facts. Every Run fact receives one monotonic per-Run update sequence; every Batch manifest/child-outcome fact receives one monotonic per-Batch sequence. When a child commit changes Batch truth, both records land in the same transaction. `watch(after=cursor)` resumes the addressed Run or Batch gap-free. Live previews are marked non-durable and never advance the cursor. No `RunResult` reconstruction; a Batch watcher never backpressures execution. (ADR 0005; PRD 0011; Batch PRD) |
| A10 | The effect PRD must define how a node declares that it may cause an external effect and how its stable effect identity is derived. Every such node reserves a durable attempt/effect identity before its provider call; unwitnessed settlement surfaces `OUTCOME_UNKNOWN`, never automatic respend. A controlled repeat-safe host proof may precede this, but production adoption and side-effect safety claims may not. (ADR 0006; PRD 0014) |
| A11 | Host owns workflow-continuation clocks and optional opaque command `source_ref` (audit, never authentication); non-interrupting reminders are NOT scheduled answers (must not consume the pause) and live in sp/product with human-task claim/availability clocks, assignment, consensus. Corrects ADR 0008's reminder claim. (ADRs 0005/0008; sp relationship note) |
| A12 | Every Run-targeting receipt carries an inert `RunRef`; every Batch receipt carries an inert `BatchRef`. Both are immutable, serializable addresses with no liveness/result/status/control methods; client verbs accept them. "Durable handle" stays a banned term (ADR 0004 holds: handles are process-local live control). (ADRs 0004/0005; CONTEXT.md) |
| A13 | Public ownership is split once: the Definition-bound Host owns new submission, Batch submission, and worker lifecycle; one backend-neutral `RunHomeClient` owns all existing-work reads and controls (`get`, `list`, `watch`, `answer`, `stop`, `rerun`) for `RunRef` and the applicable `BatchRef` operations. The Host exposes that client but does not copy its methods as pass-through conveniences. `RunView` names why a Run waits; `BatchView` names counts and unstarted items; `RunQuery` covers definition/status/age/batch/recovery/lineage/pagination. Raw SQL is an unsupported escape hatch; bulk-by-query mutation stays deferred. This deepens the existing RunInspector seam instead of creating a second operator surface. (ADR 0005; PRD 0011; Batch PRD) |
| A14 | `submit(start_at=…)` persists and fingerprints the Run immediately, defers claim eligibility until store-authoritative time passes; past time = immediately eligible; stop-before-due prevents execution. Cron recurrence and generic scheduled commands stay out; scheduled answers remain pause-scoped (shared due-row scanner, separate semantics). (ADR 0005; PRD 0011) |
| A15 | The active-Run cap is a runtime-tunable Run Home value; over-limit work waits in claim order; parked/future-scheduled/version-incompatible/recovery-exhausted Runs consume no slot; a claimed Run blocked in a provider limiter still does. Overflow strategies (reject/cancel-*) and expression-language keys rejected for v1; keyed admission deferred — when it lands, the Definition derives a concrete `admission_key`, no CEL. (ADR 0005; PRD 0011) |
| A16 | `rerun()` retains the source's full pinned Definition identity and source inputs, records `retry_of`, and accepts no input override; a Batch rerun may narrow only to named item keys from the source manifest. Any target-version change is an explicit compatibility-checked `fork` recording `forked_from` + migration reason; changed inputs use a normal new submit. The lineage relations never merge. (ADR 0007; PRD 0011; Batch PRD) |

Naming decision for the sitting: use public **`rerun()`**, not `redrive()`.
`redrive` is an SQS dead-letter term; it does not help an ordinary Hypergraph
caller. `retry` stays node-owned, `resume` continues the same Run, `restart`
describes process recovery, and `rerun` creates a new Run under the same pinned
Definition identity. The stored lineage relation remains `retry_of`.

## The five owner decisions

1. **Accept ADRs 0005–0008 with A1–A16?** Everything waits on this. Lean: accept — four rounds converged; later rounds refined, not reversed.
2. **Version identity (ADR 0007's open question).** Lean: `DefinitionId(name, deployment_version, structural_hash)`. Workers serve an exact identity; `accepts=(…)` names full prior identities and still passes structural compatibility checks. Record the code hash for diagnosis, not claim eligibility. The full identity anchors A5's fingerprint.
3. **Batch shape confirm** (immutable manifest, stable item keys, independent children, tolerance in manifest, `BatchRef`, and a per-Batch durable update sequence). Lean: yes — the survey found no framework with this full shape, and hypergraph's own pinned map-interrupt test already states "a batch is N independent admissions".
4. **Is reconnectable token/log output a near-term product need?** Decides OutputLog (A4). Lean: not now. If built: append-only named channels, opaque cursors, stable append identities — never mutable progress counters, never commands.
5. **May a process without graph code submit a NEW Run?** No = app-less client inspects/controls only; submission stays Definition-bound. Yes = Run Home grows a durable Definition catalog (name, version identity, input contract, fingerprint rules) — registration by another name, which the clean-rooms removed. Lean: no for Tier 1.

## Build order (post-acceptance)

1. Decisions 1–5 + full Definition identity → 2. Fold accepted amendments into the ADRs, PRDs, and both wayfinders; write the RunRef/RunHomeClient/watch-sequence spec, Batch PRD, pending-node PRD 0013, and effect PRD 0014 → 3. Decision-grade prototype and explicit owner approval → 4. Build the local Host wedge (submit/stop/watch/Batch/reconciler + A5 fingerprint/rerun + A6 brake + A7 tolerance; SQLite, one exclusive worker with explicit start/drain/lock-release lifecycle) → 5. Controlled Panda integration proof using repeat-safe work only; no production deletion or release claim → 6. A10 effect reservation + pending-node writes and their kill tests → 7. Complete panda ticket 0028 against real ingestion/review work, then delete 0025 guards and BackgroundTasks → 8. Runtime admission tuning + `start_at` → 9. PRD 0010 pause slots with A8, then scheduled answers → 10. Postgres tier (leases/epochs/kill matrix) on real multi-worker need → 11. OutputLog on real product need.

Panda ticket 0028 must therefore split its proof from its completion gate:
the controlled repeat-safe integration may land before A10, but the ticket is
not accepted and no temporary guard is deleted until effect safety exists.

## Prototype gate before implementation

The review page and this note are decision briefs. Before the implementation
wave, one inspectable prototype must show the exact interface and state changes
for a real Panda-shaped Batch:

```python
host = serve(ingest_definition, home=RunHome.open("file:./runs.db"))
client = host.client

receipt = await host.submit_batch(
    "ingest",
    items={"protocol-17": {...}, "protocol-18": {...}},
    workflow_id="schneider-drop-42",
)

cursor = None
async for update in client.watch(receipt.batch_ref, after=cursor):
    cursor = update.cursor if update.durable else cursor
```

The prototype must make these outcomes inspectable: worker kill and restart,
gap-free continuation from `cursor`, completed children staying complete,
`OUTCOME_UNKNOWN` for an unwitnessed effect, tolerance trip with explicit
unstarted items, version-incompatible refusal, and `rerun()` versus `fork()`.
Implementation starts only after the owner explicitly agrees that this is the
intended end state.

## Layering answers (asked and settled 2026-07-23)

- **sp is decoupled both ways.** Host imports no sp; sp executes nothing (its ADR 0009). The join is a product-side page: Operation row ↔ run record by reference, Decision id = answer dedup identity, `source_ref` = provenance not authentication.
- **Daft vs host = data plane vs control plane.** Daft makes one huge computation fast; the host makes asked-for work survive. Composition: DaftRunner runs whole graphs in Tier 0 only (`serve()` rejects it — no checkpoints/events, canon grill finding 12); "Daft inside a node" keeps node-boundary durability with Daft's optimizer inside, and is the hostable shape. If Daft grows checkpoint/event capability it becomes hostable automatically. Panda's bulk work is provider-rate-bound; hyperlimit + async concurrency is the right tool there, not a columnar engine.
- **No Hypergraph control-plane server in Tier 1.** The Host is a library and a worker is any product-owned process that imports the Definitions. Panda starts with one worker in FastAPI lifespan, with explicit startup, bounded drain, OS-lock release, and systemd restart behavior; a later product may place the same worker entrypoint in a separate process without creating a second orchestration system. SQLite Home = a file; Postgres later supplies shared transactional truth, not execution by itself. Known costs: nothing executes while all product workers are down, no in-host cron, one worker owner per SQLite Home, and the initial Panda worker shares app resources. A13's client can still inspect and write control commands while no worker is live.
- **Cross-run map cap (pipefunc `launch_maps`).** Tier 0 has no cross-run cap today (the 429-storm scar); the host's shared, tunable `max_active_runs` over N submitted Batches is the answer. A notebook-tier `gather_maps`-style helper is Tier-0 backlog.
- **Executor delegation (pipefunc SLURM dicts, Daft, Ray).** The runner seam owns *how a run executes* and may delegate (whole-graph via a delegating runner, or per-node executors/resources — a Tier-0 backlog item); the host owns *who may execute at all*. An engine-backed host (DBOS as internal engine) remains a legal Tier-3 implementation of the same contract.
- **What this removes in hypergraph today: nothing — and that is the point.** Engine code stays; the deletions land in products (panda's five queue organs, ticket 0028). Inside hypergraph it closes real windows (PRD 0013 mid-superstep sibling loss; A10's silent double-execution of plain effectful nodes; the unsound `resolve_stranded_attempts` precondition), gives ADR 0004's recurring durable-handle requests a permanent answer (RunRef + client), and holds the do-not-build list — no broker, no second journal, no event record, no determinism contract — with reasons attached.

## Tier-0 ergonomics track (separate from host amendments; pruned by round 3)

Keep: Hamilton-style calculator surface (output selection, `overrides=`, pre-flight `validate_execution`/`visualize_execution`); LangGraph-v3-style typed projection stream for `runner.iter` (no `version=` params ever); liveness timeouts (`idle_timeout` + heartbeat — panda's "timed out at 240s while healthy" scar) as its own small ADR against the locked 07-14 contract; cross-run map cap helper; per-node executor/resources delegation; pipefunc-style MCP/CLI generation only after A13 and decision 5.
Pruned with reasons: durable ErrorSnapshot with exact args (violates the attempt privacy contract — type-only, no args/stack/repr; local opt-in only); graph-wide retry defaults (locked canon: only the node owning the callable authorizes repetition).

## Cross-framework validations worth keeping on the record

The twelve reports found no framework with the full Batch shape (manifest + item
keys + tolerance + item-granular rerun), no answer-validation match beyond
Temporal's update validators, and no start-fingerprint submit dedup. Treat
those as bounded survey findings, not universal claims. Four reports also
document replay costs worth avoiding here: Inngest step-ID versioning, DBOS
patch machinery, Restate zombie handling, and Hatchet's durable-decorator
contract. Other cautions remain useful: Hamilton's long-lived experimental
fan-out, LangGraph's coexisting stream formats, Prefect's return-value outcome
rules and implicit caching, pipefunc's destructive resume default, and
Inngest's flow-control compatibility matrix.
