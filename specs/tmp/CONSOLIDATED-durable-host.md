# Consolidated: hypergraph's durable serving layer

> **SUPERSEDED IN PART (2026-07-21, same day).** Sections 1–2 predate the
> round-5 API refinement and round-7 canon grill: `register()` and caller
> idempotency keys are GONE (start dedupes on workflow_id, answers on
> pause_id), `host.run()` is dropped from v1, "stranded" is renamed
> version-incompatible, and SQLite Tier 1 uses an exclusive worker lock, not
> leases. Authority now lives in docs/adr/0005–0008 and docs/prd/0010–0011;
> defect list in docs/research/2026-07-21-durable-host-canon-grill.md.

**Date:** 2026-07-21
**Method:** one clean-room brief (needs N1–N11, anonymized primitives; see `~/cleanroom-durable-serving/brief.md`) answered independently by three models — Codex `gpt-5.6-sol` xhigh, Fable 5 (fresh subagent), Kimi K3 (effort high) — compared against the six-round anchored design (Rivet/DBOS/Restate exploration + two Codex peer reviews). Source designs in this folder: `cleanroom-durable-host-design.md` (Codex), `cleanroom-fable-design.md`, `cleanroom-kimi-k3-design.md`.

---

## 1. Locked — unanimous across all four designs

These decisions had four independent derivations. They are settled; re-opening any of them requires new evidence, not new taste.

1. **Host-as-root composition.** Graphs register into one host, each with its own execution strategy (runner). Serving is deployment-scoped; runners are graph-scoped. (`host.register/serve(graph, strategy=..., ...)`)
2. **Epoch-fenced single-writer, enforced inside the write.** One monotonic counter per workflow; every journal/state mutation carries the writer's epoch and is rejected transactionally if a newer epoch exists. A lease expiry revokes *authority*, never proves *death*. Fencing and the journal must live in the same transactional store — all four designs derived this from N4 alone.
3. **Durable, idempotent command intake.** Caller-supplied key; retry returns the original receipt; commands for one workflow are totally ordered; stop-vs-continue races resolve by commit order with defined, truthful rejections (`superseded`, `already_terminal`).
4. **State-based resume is the recovery mechanism.** Crash recovery = expired lease → claim with higher epoch → re-invoke `run(workflow_id=...)`. No replay contract, no determinism requirements, no second execution journal.
5. **Version pinning + loud refusal + fork migration (resolves the round-1 upgrade dilemma exactly as leaned: strict reject + explicit fork).** A run pins its graph version at start; incompatible workers never claim it; unclaimable runs surface as `stranded`/`blocked_version` (queryable, loud); migration is explicit via the *existing* `fork_from` primitive. Fable adds `accepts=("refund/7",)` for deliberate compatibility declarations.
6. **Parked runs are rows at rest.** No lease, no process, no memory, no timers while waiting on a human. Answer arrival flips them runnable.
7. **Effect identity for side effects, never exactly-once.** A stable per-logical-node-occurrence key (derived from the attempt reservation, deliberately EXCLUDING attempt number and worker id) is injected into node context; the external system (payment provider) owns final dedup. No API or doc may say "exactly-once."
8. **The boundary (what NOT to build).** No message broker (the claimable-rows scan IS the queue; lease = visibility timeout; LISTEN/NOTIFY only as latency sugar). No lock/consensus service (fencing rides the journal's store — a separate lock tier reintroduces the check-then-write gap N4 forbids). No process supervisor (systemd/k8s/cron). No HTTP surface (library calls: `submit`/`start`/`watch`). No dashboards.
9. **Storage-agnostic concept names; backend chosen once at construction.** `Coordination.local/.shared` (Codex), `Home.open("file:..." | db-url)` (Fable), `PgJournal` appearing exactly once at construction (Kimi). The user-facing axis is *local vs shared authority*, never a database brand.

## 2. Divergences → decisions (with recommendation)

**D1 — Atom count.** Kimi: 2 atoms (run record carries intake-key dedup column, single question/answer slot, stop flag, lease triple; journal = existing interface, network-backed). Fable/Codex: 3 (separate command log). *Recommendation:* run record with lease **fields** (not a separate lease table — Codex/Kimi agree) **plus** a command log (Fable/Codex): the log gives ordered multi-command semantics, the "run continued because of command X" audit edge, and room for multiple pending questions later. Kimi's single-slot CAS is the fallback simplification if the log feels heavy in v1.

**D2 — Durable event record: NO (2-vs-1, and hypergraph specifics agree).** Fable and Kimi both refuse a new event record: `watch()` = journal replay + live tail (relay processor / LISTEN-NOTIFY). Hypergraph's StepRecords already carry history. Adopt Codex's in-transaction cursor-event projection only if real gaps emerge (e.g., streaming chunks not in the journal).

**D3 — API dialect.** Codex: `submit(Start(...))` command objects. Kimi: four verbs — `host.start/answer/stop/watch`. Fable: hybrid (`submit(Command.start(...))`). *Recommendation:* methods (`service.start(...)`, `resume/answer`, `stop`, `watch`) returning receipts — matches hypergraph's small-API ethos; the command object stays an internal atom.

**D4 — Names.** Host (near-unanimous noun) for the loop+registry. The paired store: `Home` (Fable — warm, tech-free) vs `Coordination` (Codex — explicit) vs "it's just the shared checkpointer plus control tables" (Kimi — fewest new nouns; maps to `PostgresCheckpointer` + control schema). **sp collision rule:** the fencing atom is a **Lease with an epoch** (Fable/Kimi), NOT "ExecutionGrant" — *Grant* is superposition's authority atom. Maintainer's taste call on Home vs Coordination vs checkpointer-family.

**D5 — Notebook path.** Keep direct `runner.run()` as the zero-ceremony path (unchanged today); the host is additive (Kimi's `run_local`, Fable's "not a toll booth"). Never require the host for local work.

## 3. What only the anchored six-round process saw (clean rooms structurally couldn't)

- **Pending node writes:** a crash mid-superstep loses successful siblings' outputs (records save at superstep end). The brief abstracted superstep granularity away, so no clean room could find it. Still on the build list.
- **Attempt-ledger contract redefinition:** `resolve_stranded_attempts` currently assumes the caller *knows* no prior invocation runs; under leases this becomes "no prior lease may still commit" — stranded attempts stay OUTCOME_UNKNOWN and may physically overlap.
- **Attempt-ledger coverage:** only nodes with `retry`/`timeout` enter the ledger today; the effect-key derivation (locked item 7) requires reservation for every side-effect-capable node.
- **The sp relationship** (see memory `durability-layer-design`): the host is "the domain's own serving host (an sp client)" per sp ADR 0009; sp's `Operation` row is the authority-side shadow of the run record, joined by reference; Decisions apply as `Continue` commands with `decision_id` as idempotency key; the host stays fully sp-free below adoption thresholds.

## 3b. Round 5 — API refinement after ecosystem check (2026-07-21, maintainer objections)

Three objections checked against Prefect 3, Inngest, Restate, and Temporal. All three upheld:

**Idempotency keys mostly disappear from the API.** Temporal: "the Workflow Id acts as an idempotency key"; `WorkflowIDConflictPolicy.USE_EXISTING` returns the running workflow's handle. Restate: `Idempotency-Key` is an HTTP header — transport concern, not API parameter. Inngest: dedup by event id / per-function config. Applied to hypergraph:
- `start` dedupes on **workflow_id itself** — starting `refund-c-42` twice returns the same run (use-existing semantics). No extra key.
- `answer`/resume dedupes on the **pause slot** — a pause can be answered once, even by two different clients (CAS on the run record). No extra key.
- `stop` is naturally idempotent.
- Optional `request_id=` remains ONLY for auto-generated workflow ids (rare), and belongs at the HTTP layer (header pass-through), not in the core signature. Internally the command log still records what was applied (audit); callers never manage keys.

**No imperative `.register()` lines.** Prefect: `serve(slow_deploy, fast_deploy)` — one call, list in. Inngest: `serve(app, client, [fn1, fn2])`. Temporal: `Worker(workflows=[...])`. Applied: graphs (which already carry `name`) get a runner attached declaratively, and one `serve` call takes the list:
```python
refund = refund_graph.with_runner(AsyncRunner())     # declaration, not registration
triage = triage_graph.with_runner(SyncRunner())
host = hypergraph.serve(refund, triage, home=Home.open(env("RUN_HOME", "file:./runs.db")))
```

**One verb family, not `run` vs `start`.** Temporal's split is start (fire) vs execute (fire+await). Applied: `host.run(...)` = submit + await outcome, returns the SAME RunResult shape as `runner.run` — the served twin of the verb users already know; `host.submit(...)` = fire-and-forget receipt. "start" is dropped.
```python
result  = await host.run("refund", {"claim_id": "c-42"}, workflow_id="refund-c-42")  # notebook feel
receipt = await host.submit("refund", {...}, workflow_id="refund-c-42")              # webhook handler
await host.answer("refund-c-42", approved=True)      # pause-slot CAS
await host.stop("refund-c-42")
async for ev in host.watch("refund-c-42"): ...
await host.work_forever()                             # worker entrypoint per VM
```

## 3c. Round 6 — gap analysis vs engines; the `wake_at` amendment (2026-07-21)

Comparing the locked design against DBOS/Temporal/Restate feature sets exposed exactly one hole worth fixing NOW: **durable timers**. Parked runs today wake on ANSWER only, never on TIME — so "if Dana doesn't answer in 72h, auto-escalate" and "retry the payment in 24h" are inexpressible. Amendment: one nullable `wake_at` column on the run record; the claim predicate becomes `runnable OR (parked AND wake_at <= now()) OR lease expired`. One column, no new atom. Cron/schedules stay outside (OS/host-side, per the locked boundary).

Deferred-by-scale (documented, not built): global flow control (fleet-wide rate limits / concurrency caps — the first engine feature that will genuinely bite at scale; per-worker `max_concurrency` covers the near term), queue priorities, ops dashboard, >1-Postgres scale ceiling. Covered-differently (not gaps): replay, child workflows (nested graphs are native), signals (answer/pause + run-values), patching (version pin + fork).

Build-vs-rent position (Q2, reaffirmed with one refinement): core stays native — the load-bearing 20% (fencing inside checkpointer transactions) is unbuyable, and the rest rides Postgres primitives (transactions, unique constraints, `FOR UPDATE SKIP LOCKED`). The host protocol should leave room for an internal engine swap (a DBOS-backed host as a dedicated-worker implementation is structurally honest and would rent flow control + timers + dashboard) — but only when a real deployment demands those, never as the default path.

## 4. Build sequence (unchanged from round 2, now vocabulary-final)

1. Protocol + in-process implementation (asyncio; notebooks/tests).
2. Prove start/answer/stop/watch segments over `SqliteCheckpointer`.
3. Lease + epoch fencing bound into every checkpointer mutation; adversarial stale-writer tests (the test suite is most of the cost).
4. Shared backend (Postgres checkpointer + control schema) when production serving becomes the active goal.
5. Pending node writes + attempt-ledger coverage widening (independent of 1–4, valuable regardless).
