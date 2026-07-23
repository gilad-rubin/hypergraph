# API Atlas synthesis — 12 frameworks vs the refined durable-host design

Inputs: nine reports in this directory (pipefunc, hamilton, langgraph, prefect, dbos,
restate, inngest, hatchet, rest-group covering temporal/trigger.dev/pgflow/resonate),
each mapping the framework's user-facing API against hypergraph Tier 0 + the
A1-A11-amended host design.

## I. Convergent gaps — multiple frameworks point at the same hole in OUR design

**G1. No universal re-attach handle.** Temporal (`get_workflow_handle(id)` -> full
verb set), Resonate (`resonate.get(id)`), DBOS (WorkflowHandle), Prefect (futures).
Our host scatters watch/stop/answer/redrive as loose module-level verbs keyed by
string ids. Proposal: **the receipt IS a handle, and `host.handle(workflow_id)`
re-mints it** — an object carrying watch()/stop()/answer()/redrive()/status
projection, cheap, no state, honest (no `result()` — ADR 0005 evidence rule stands).

**G2. No operator query-then-act surface.** Hatchet (one `RunFilter` drives
`runs.list` AND `bulk_cancel`/`bulk_replay`), DBOS (`list_workflows` with ~27
filters incl. lineage + bulk verbs), Restate (bulk-by-target), pgflow (SQL read
contract as documented API). Proposal: **adopt the query vocabulary now** — a
documented, stable read contract on the Run Home (filter by status / age /
definition / batch / recovery condition / lineage; pgflow-style "your SQL is the
API" for the local tier) — and **defer bulk-by-filter mutation** with a named
trigger (first real operator need beyond batch-scoped redrive, which A5's
`item_keys` already covers).

**G3. No app-less operator client.** DBOS `DBOSClient` (store URL only, no app
import), Restate ingress grammar (every verb is a URL: `/{call|send}/{svc}/{key}
/{handler}?delay=`, attach/output by id), pipefunc CLI. Proposal: **Run Home thin
client** — submit/watch/stop/redrive/answer/list against the store without loading
graph code (panda's UI and scripts are the first consumers); the HTTP grammar
stays product-side (panda's FastAPI) but the CLIENT contract is host-owned.

**G4. No delayed start.** Restate `?delay=10s` / `send_delay`, DBOS enqueue
`delay`, Hatchet `schedule`, Prefect/Inngest/LangGraph/DBOS all ship cron. Our
boundary keeps cron recurrence outside (OS/product) — every report validates
keeping RECURRENCE out, but one-shot **`submit(..., start_at=)` is just a
scheduled Start command** — ADR 0008 already invented scheduled commands for
answers; generalize the mechanism, keep cron out.

**G5. Admission is constructor-frozen and unkeyed.** Restate rules CLI
(`rules set "checkout/*" --concurrency 5`, live), Prefect named limits
(`with concurrency("database")` + `gcl update`), DBOS runtime queue tuning,
Inngest CEL-keyed concurrency, Hatchet ConcurrencyExpression + overflow
strategies (round-robin / cancel-in-progress / cancel-newest), DBOS
partition-key fairness. Proposal: (a) **`max_active_runs` becomes runtime-tunable
via a host command** (cheap, Tier 1); (b) **keyed work admission** (per-tenant/
per-KB fairness) is a named deferral with the key-expression shape recorded
(panda is single-tenant today); (c) A3's scope-honesty rule extends: overflow
STRATEGY must also be named (queue vs reject vs cancel-newest), default queue.

**G6. No user-authored progress/output channel.** Trigger.dev metadata rollup
(children `metadata.root.increment("done", 1)`; observers subscribe to one
object), DBOS durable named streams (`write_stream(key, v)` exactly-once in-run,
`read_stream(wf, key, offset)` from anywhere). This sharpens A4's OutputLog
door: **if/when built, the shape is named, offset-addressed durable streams,
user-writable from nodes** — not just token relay. Still gated on owner decision
4 (not now). Batch-level counters need no new machinery — the manifest + child
facts already derive them (A9); user-defined counters ride the OutputLog door.

**G7. Redrive/fork lack the "onto new code" arm.** Restate
`resume --deployment latest` (their release lever, admitted-fragile under
replay), DBOS `fork_workflow(start_step=N, application_version=...)`. Our A5
redrive + ADR 0007 fork are separate relations; **redrive should accept an
explicit target version** (default: same pinned version; explicit version =
the migration lever, recorded). State-based recovery makes this SAFER for us
than for them — no determinism errors possible.

## II. Tier 0 ergonomics backlog (NOT host amendments — separate track)

- **Calculator surface** (Hamilton): output selection (`final_vars`-style ask for
  any node), `overrides=` pinning, `validate_execution()` / `visualize_execution()`
  pre-flight verbs, upstream/downstream lineage queries.
- **Projection stream** (LangGraph v3): one stream object with independently
  consumable typed views (`.values/.messages/.interrupts/.output`, `interleave()`);
  `show_progress` becomes a built-in transformer. NEVER a `version=` param.
- **Failure capture** (pipefunc ErrorSnapshot): persist callable ref + exact args
  on node failure; `snapshot.reproduce()` offline. Fits FailureEvidence.
- **Liveness timeouts** (LangGraph `TimeoutPolicy(idle_timeout=, refresh_on=)`):
  extends the locked 07-14 retry/timeout contract — panda's "timed out after 240s
  on a healthy slow run" scar is the use case. Needs its own small ADR against
  the locked contract; graph-level, not host.
- **Graph-wide failure defaults** (LangGraph `set_node_defaults`): builder-level
  retry/timeout defaults, per-node override wins.
- **MCP/CLI generation** (pipefunc `build_mcp_server`): typed graphs -> agent-
  facing server (submit/status/cancel) nearly free; a distribution door for the
  host verb surface.

## III. Morphology rules (design principles, fold into the API style canon)

- **Escalating verbs, identical signatures** (Prefect fn/submit/delay/serve;
  Temporal execute_/start_ pairs): `host.submit` takes exactly `runner.run`'s
  shape plus host-only params. One rule teaches the surface.
- **Identity-first habit** (Resonate): workflow_id stays the first-class,
  caller-chosen identity everywhere; never optional-and-generated by default
  on durable verbs.
- **The object is the client** (Hatchet): consider `graph.with_runner(...)`
  bindings exposing submit/schedule directly later; not v1.
- **Never**: a `version=` param on event surfaces (LangGraph churn); overloaded
  input-as-control-channel (LangGraph `None`/dict/Command confusion); silently
  destructive defaults on batch verbs (pipefunc cleanup); return-value-decides
  outcomes (Prefect); doubled sync/async verb sets with different defaults (DBOS).

## IV. Validations — locked decisions every atlas report independently confirms

No-replay/state-based recovery (Inngest, DBOS, Restate, Hatchet, LangGraph all
tax users with determinism contracts, step-ID versioning, or patching machinery);
batch-as-first-class-object (NO framework has manifest+item-keys+tolerance —
we'd be first); schema-validated answers surviving rejection (only Temporal
validators compare); fingerprint submit dedup (only Stripe-style, nobody has it
on workflows); retry_of lineage redrive (Hatchet re-run forgets ancestry;
Prefect mutates run_count in place); watch-as-durable-replay (Hatchet live-only
drops early events); explicit outcome model; opt-in memoization; evidence-
preserving defaults; typed inputs and object references from day one.

## V. Proposed disposition (for Codex review)

New amendments: **A12** universal handle (receipt=handle, host.handle(id));
**A13** Run Home read contract now, bulk-by-filter deferred-with-trigger;
**A14** `start_at=` delayed submit via generalized scheduled commands, cron
stays out; **A15** runtime-tunable work admission + named overflow strategy,
keyed admission deferred-with-shape-recorded; **A16 (amends A5/ADR 0007)**
redrive accepts explicit target version. OutputLog (A4) sharpened to
named/offset/user-writable streams, still door-gated. Plus the Tier 0 backlog
(section II) as a separate named track, and section III folded into API style
canon. Open question for review: does A12's handle re-introduce ADR 0004
confusion (process-local handle vs durable handle naming), and should it be
called something else (`RunRef`?) to keep "handle = live process object" true?
