# Restate — user-facing API atlas

Researched 2026-07-23 against docs.restate.dev (live docs; Restate Server v1.7.2, released
2026-07-06 per [changelog/server](https://docs.restate.dev/changelog/server); Python SDK
v0.18.1 per [changelog/python-sdk](https://docs.restate.dev/changelog/python-sdk)).
Store/license limits (BUSL-1.1, embedded RocksDB, no our-Postgres option) and the awakeable
HITL story are already covered in `engines.md` (Restate section) and `hitl.md` §2b — referenced,
not repeated. Python SDK examples used wherever the docs provide them; TS only where Python lacks
a surface (typed ingress client nuances).

## 1. Identity

Durable-execution engine: single Rust server binary that proxies every request and journals every
SDK action; recovery = **replay** the journal, skipping completed steps
([foundations/key-concepts](https://docs.restate.dev/foundations/key-concepts)). Services are your
own processes embedding an SDK (TS/Java/Kotlin/Python/Go/Rust); the server owns state, timers,
retries, and routing. Mature core (server 1.7.x), but Python SDK (0.18.x) and the newest features
(flow control, signals) are preview-grade.

## 2. Authoring

Three service types, one decorator idiom
([foundations/services](https://docs.restate.dev/foundations/services)):

| Type | State | Concurrency contract |
|---|---|---|
| `restate.Service` | none | unlimited parallel |
| `restate.VirtualObject` | K/V per key | **at most one exclusive handler per key**; `kind="shared"` handlers run concurrently, read-only |
| `restate.Workflow` | K/V per ID | `run` handler executes **exactly once per workflow ID**; other handlers are shared, callable during retention |

```python
cart_object = restate.VirtualObject("ShoppingCart")

@cart_object.handler()
async def add_item(ctx: ObjectContext, item: Item) -> Cart:
    cart = await ctx.get("cart", type_hint=Cart) or Cart()
    cart.items.append(item)
    ctx.set("cart", cart)
    return cart

@cart_object.handler(kind="shared")
async def get_total(ctx: ObjectSharedContext) -> float: ...
```

Workflow authoring (`@signup_workflow.main()` for `run`, `@signup_workflow.handler()` for shared)
plus `app = restate.app([review_workflow])` served by any ASGI server (hypercorn in the docs'
saga example — [guides/sagas](https://docs.restate.dev/guides/sagas)). Deployments are then
*registered* with the server (`restate deployments register`), which discovers handlers and pins
in-flight invocations to immutable deployment versions
([services/versioning](https://docs.restate.dev/services/versioning)).

**No graph.** Control flow is ordinary code in the handler; durability granularity is the
journaled action (`ctx.run_typed`, calls, sleeps), not a node.

## 3. Run verbs

The complete verb surface, three layers: external client (HTTP or SDK client), in-handler (`ctx`),
operator (CLI/admin API). Sources:
[services/invocation/http](https://docs.restate.dev/services/invocation/http),
[clients/python-sdk](https://docs.restate.dev/services/invocation/clients/python-sdk),
[clients/typescript-sdk](https://docs.restate.dev/services/invocation/clients/typescript-sdk),
[develop/python/service-communication](https://docs.restate.dev/develop/python/service-communication),
[managing-invocations](https://docs.restate.dev/services/invocation/managing-invocations).

### External client verbs

| Restate verb | What it does | hypergraph equivalent |
|---|---|---|
| `POST /restate/call/{Service}/{handler}` | sync request-response over plain HTTP, JSON body | `runner.run` (in-process only) — **MISSING as HTTP convention**; designed host explicitly has *no* `host.run()` in v1 |
| `POST /restate/call/{Obj\|Wf}/{key}/{handler}` | keyed call; VO serializes per key; workflow `run` = submit-and-wait, resubmit → "Previously accepted" | `workflow_id` on `runner.run`/`host.submit` (dedup by fingerprint, richer conflict answers) |
| `POST /restate/send/{...}` → `{"invocationId": "inv_...", "status": "Accepted"}` | fire-and-forget; returns invocation ID receipt | `host.submit` → receipt (equivalent shape) |
| `POST /restate/send/{...}?delay=10s` (humantime or ISO8601) | delayed invocation, server-held timer; *not supported for workflows* | **MISSING** — no delayed submit in Tier 0 or host design (only scheduled *answers*) |
| `idempotency-key: <k>` header on call/send | dedup within retention (24h default, configurable); retry returns stored result or attaches to the running invocation | `host.submit` start-fingerprint dedup — ours is id+content-based, theirs is caller-supplied-key; both converge on "use existing" |
| `GET /restate/attach/{invocationId}` | block until that invocation completes, get result | `host.watch(wf)` (ours streams; theirs blocks for the final value) |
| `GET /restate/output/{invocationId}` | **peek**: `not ready` / result / `not found`, non-blocking | **MISSING** as a one-shot verb (ours: read RunResult from store) |
| `POST /restate/lookup --json '{"target": "workflow", ...}'` | map workflow-key / idempotency-target → invocation ID | workflow_id *is* the handle in hypergraph — not needed |
| `POST /restate/attach` + `/restate/output` by target | attach/peek by workflow key or (service, handler, idempotency key) — only workflows and idempotent invocations are attachable by target | `host.watch(workflow_id)` |
| Python: `restate.create_client("http://localhost:8080")` → `client.service_call / object_call / workflow_call / *_send(..., send_delay=, idempotency_key=)` | typed ingress client mirroring handler defs | `serve()`/host client — **hypergraph has no remote client at all yet** |
| TS: `workflowClient(...).workflowSubmit(req)` → `restateClient.result(handle)`; `.workflowAttach()`; `.workflowOutput()` → `{ready, result}` | submit / attach / peek triplet for workflows | `submit`/`watch` cover 2 of 3; peek is the read-store path |
| TS client `connect({retry: true})` | client-side auto-retry (network/429/5xx), **only when an idempotency key is present** — retry attaches instead of double-starting | **MISSING** (nice honesty detail: retries gated on dedup being possible) |
| Kafka subscription → handler | invoke handlers from events ([services/invocation/kafka](https://docs.restate.dev/services/invocation/kafka)) | MISSING (explicitly out of hypergraph scope) |

### In-handler (`ctx`) verbs

| Restate verb | What it does | hypergraph equivalent |
|---|---|---|
| `await ctx.run_typed(name, fn, *args, RunOptions(...))` | journal one side effect; per-block retry policy | node execution + checkpointer (per-node, not per-action granularity) |
| `ctx.service_call / object_call / workflow_call(handler, key=, arg=, idempotency_key=)` | **durable RPC** to another service, parent-linked in the journal | **MISSING** — graphs compose in-process; no durable cross-graph call |
| `ctx.*_send(..., send_delay=timedelta(hours=5))` | durable one-way / delayed message; detached from call tree | **MISSING** |
| `handle = ctx.service_send(...)` → `await handle.invocation_id()` → `ctx.attach_invocation(id, type_hint=str)` / `ctx.cancel_invocation(id)` | detach, re-attach, cancel children by ID | `start_run` handle (Tier 0, in-process only) |
| `await ctx.sleep(delta=timedelta(...), name="wait for payment")` | durable timer, months+; named entry shows in UI | **MISSING** (no durable sleep primitive) |
| `ctx.get/set/clear/clear_all` | per-key K/V state (VO/Workflow only) | **MISSING** (run-scoped state only; no entity store) |
| `ctx.awakeable / ctx.promise / ctx.signal` | pause primitives — see §5 | interrupt gates / `host.answer` |
| `restate.gather(f1, f2)`, `restate.select(name=fut, ...)` (pattern-match winner), `restate.as_completed(...)`, `restate.wait_completed(...)` → `(done, pending)` | durable future combinators; completion **order is journaled** for deterministic replay ([develop/python/concurrent-tasks](https://docs.restate.dev/develop/python/concurrent-tasks)) | `runner.map/map_iter` cover fan-out-over-items; `select`-style racing is **MISSING** |
| `ctx.uuid()`, `ctx.random()`, `await ctx.time()` | determinism-safe randomness/clock (seeded by invocation ID) | not needed (no determinism contract) |
| `ctx.request().id / .scope / .limit_key` | introspect own invocation identity | `workflow_id` in context |

**No batch verb anywhere.** Restate's answer to `runner.map` / `host.submit(map_over=...)` is
"send one invocation per item and hand-roll gather/tolerance yourself" (their AI docs' fan-out
patterns do exactly this). No manifest, no item keys, no per-item outcome reporting, no
tolerance policy. This is the single biggest surface hypergraph has that Restate lacks.

### Operator verbs (CLI `restate ...` / admin `:9070`)

| Restate verb | What it does | hypergraph equivalent |
|---|---|---|
| `restate invocations list` (`--status backing-off`, `--oldest-first`, `--virtual-objects-only`, `--zombie`, `--all`) | filterable listing; statuses: `pending / ready / running / backing-off / suspended / completed` | host status projections (designed) — theirs richer today |
| `restate invocations describe <id>` | status + journal + invocation *tree* (`[Ingress] └──(this)─> Greeter/greet`) | `RunResult` + checkpoint inspection |
| `restate invocations cancel <id \| Service \| Service/handler \| Obj/key/handler>` | graceful, bulk-capable; TerminalError raised at next ctx await point; compensation = your catch block (§6) | `host.stop` (cooperative stop) — same philosophy, ours has no bulk-by-target |
| `restate invocations kill <id>` | immediate halt, **no compensation**, "may leave your service in an inconsistent state. Only use as a last resort"; one-way/delayed children survive | **MISSING** — no force-kill for wedged runs |
| `restate invocations pause <id \| Service>` | operator-imposed pause (e.g. to move deployments) | **MISSING** (our pauses are only graph-declared gates) |
| `restate invocations resume <id> [--deployment latest\|<dp_id>]` | resume paused/backing-off invocation, optionally **on different (fixed) code**; docs warn: divergent logic → "non-determinism errors!" | recovery is implicit restart-scan; explicit resume verb + code-repoint **MISSING** (and safer for us — state-based recovery has no determinism to violate) |
| `restate invocations purge <id \| Service>` | delete completed invocation state/journal (retention reclaim) | **MISSING** (no retention/GC verbs designed) |
| `restate invocations restart-as-new <id \| Service>` | completed invocation → brand-new invocation ID, same input, "no partial progress will be kept"; workflows: UI-only | `host.redrive(..., retry_of=...)` — equivalent |
| Restart **from prefix** (UI only) | new invocation replaying journal prefix, continuing from there | `retry_from` / `fork_from` — **hypergraph has this as a first-class API verb; Restate ships it UI-only** |
| `restate sql "..."` / `curl :9070/query` | DataFusion SQL over `sys_invocation`, `sys_journal`, `sys_inbox`, `sys_promise`, `state`, ... | Run Home is user-owned SQLite → plain SQL for free (stronger: no bespoke query API) |
| `restate kv get/edit <Service> <key>` (`--plain \| --binary`, `--force` on write conflict) | read **and edit** live VO state from CLI | N/A (no entity state) |
| `restate rules set/list/enable/disable/delete` | dynamic concurrency rule book (§7) | **MISSING** |
| `restate deployments register [--force]` | version registration; `--force` for dev loops | N/A (no server) |

## 4. Observation

Pull-based, operator-grade; **no push channel to clients**. Sources:
[services/introspection](https://docs.restate.dev/services/introspection),
[references/sql-introspection](https://docs.restate.dev/references/sql-introspection).

- **SQL is the observation API** (details in `engines.md`). Notable schema: `sys_invocation`
  carries `retry_count`, `modified_at`, `invoked_by`/`invoked_by_id` (call tree), `trace_id`
  (Jaeger handoff), `pinned_deployment_id`; `sys_journal` v2 rows carry `entry_json` — the whole
  journal is queryable as JSON; `sys_journal_events` totally orders events against entries.
- CLI recipes as accordions: stuck invocations (`modified_at <= now() - interval '1' hour`),
  which invocation blocks a Virtual Object (`sys_keyed_service_status`), zombies pinned to
  force-deleted deployments.
- UI: journal timeline, invocation tree, state tab, cancel/kill/restart buttons.
- **Watching a run** = `GET /restate/attach/{id}` (blocks for final value) or poll
  `/restate/output`. No `runner.iter`-style event stream, no progress events, no per-step
  push. **Watching a batch** = doesn't exist; you watch N invocations by SQL query.
- Streaming to end users is a documented *pattern*, not a primitive: put updates in VO state or
  push SSE from your own handler ([ai/patterns/streaming-responses](https://docs.restate.dev/ai/patterns/streaming-responses)).

hypergraph Tier 0 (`iter`, `map_iter`, `show_progress`) and `host.watch` (durable replay + live
tail, batch child outcomes) are strictly ahead here.

## 5. HITL / pause

Awakeable mechanics + HTTP resolve/reject and durable-promise retention are in `hitl.md` §2b.
What's new since that research, per
[develop/python/external-events](https://docs.restate.dev/develop/python/external-events) — the
docs now present a deliberate **three-primitive taxonomy**:

| Primitive | Address | Resolution |
|---|---|---|
| **Signal** | invocation ID + name | **multiple times; each await gets the next resolution** |
| **Awakeable** | generated unique ID | once (resolve/reject) |
| **Workflow promise** | workflow key + name | once; readable by all handlers during retention |

```python
approved = await ctx.signal("approval", type_hint=bool)      # wait
ctx.resolve_signal(req.invocation_id, "steer", req.text)     # send (or ctx.reject_signal)
```

Signals queue durably even if sent *before* the wait; the docs' example is an agent-steering
loop (`while True: text = await ctx.signal("steer", ...)`). Typing is `type_hint=` +
optional Pydantic serializer — **no validation gate**: a bad answer is a deserialization error
inside the handler, unlike hypergraph's designed validate-before-settle
(`host.answer` rejects against the graph-declared schema and the pause survives). Rejection of
an awakeable/signal throws TerminalError in the waiting handler. Timeouts are composed, not
built-in: `restate.select(confirmation=promise, timeout=ctx.sleep(timedelta(days=1)))`.

Also note the *operator* pause layer (§3): `on-max-attempts: pause` plus manual
`invocations pause` gives Restate an operator-imposed pause lane fully separate from HITL —
hypergraph currently conflates "paused" with "waiting for an answer."

## 6. Failure & retry surface

Source: [guides/error-handling](https://docs.restate.dev/guides/error-handling) (retry-knob
matrix also in `engines.md` row 2).

**User-configurable, three scopes:** global server default → per service/handler (UI or service
config) → per `ctx.run` block:

```python
await ctx.run_typed("write", write_to_other_system,
    restate.RunOptions(
        initial_retry_interval=timedelta(milliseconds=100),
        retry_interval_factor=2.0,
        max_retry_interval=timedelta(seconds=10),
        max_duration=timedelta(minutes=5),
        max_attempts=10,
    ))
```

Defaults: 70 attempts, 50ms initial, factor 2.0, 60s cap, then **`on-max-attempts = "pause"`**
(default; or `"kill"`; or `max-attempts = "unlimited"`). Pause-not-kill as the exhaustion default
is the standout decision: a failing invocation parks, shows up in UI/SQL as paused, and waits for
`resume` — optionally `--deployment latest` after a fix. This is hypergraph's designed
`recovery_exhausted` brake, already shipped, with the resume verb attached.

- `TerminalError(msg, status_code=402, metadata={...})` — non-retryable, propagates across RPC
  hops; `RetryableError(msg, retry_after=timedelta(...))` — honor `Retry-After` from rate-limited
  APIs.
- **Cancellation** ([managing-invocations](https://docs.restate.dev/services/invocation/managing-invocations)):
  cancel ≠ kill. Cancel raises TerminalError *at the handler's next ctx await point*, propagates
  to call-graph leaves first, then back up, letting every frame run its catch-block
  compensations; the docs' saga idiom is push-compensation-then-act, unwind reversed on
  TerminalError ([guides/sagas](https://docs.restate.dev/guides/sagas)). Kill skips compensation
  entirely. Both leave one-way/delayed children running (detached).
- **Operator retry verbs:** resume (also usable to force an immediate retry during backoff),
  restart-as-new (completed → fresh, no progress kept), restart-from-prefix (UI-only surgical
  replay), all bulk-capable by service/handler target.
- **Timeout contract with your process:** `inactivity-timeout` (1m default — server asks handler
  to suspend) then `abort-timeout` (10m — kills the attempt); long `ctx.run` blocks require
  raising both in server config.
- **No tolerance concept** — nothing like `BatchTolerance`; there is no batch.

## 7. Concurrency & flow control

Source: [services/flow-control](https://docs.restate.dev/services/flow-control) (server 1.7,
opt-in, experimental).

Scopes at each level:

- **Per Virtual Object key** (always on): single exclusive writer, structural — the object *is*
  the mutex, unbounded concurrency across keys.
- **Per scope** (opt-in "vqueues"): a caller-attached namespace (`/restate/scope/{key}/call/...`)
  with a cluster-wide **rule book**: `restate rules set "checkout/*" --concurrency 5`. Patterns
  with wildcards and most-specific-wins; a `*` rule is per-scope, **not** a global pool (docs
  warn explicitly).
- **Per limit key within a scope** (two nested levels): invocation draws from scope + L1 + L2
  budgets simultaneously; admitted only when all have slots. Docs' worked example: org scope
  10000 / team 1000 / user 10.
- Scope is also **identity**: idempotency keys and VO/workflow keys are namespaced per scope —
  same key under two scopes = two objects.
- Observability tables: `sys_rules`, `sys_user_limits` (usage vs limit per level),
  `sys_vqueues`, `sys_scheduler` (per-queue head entry: what it's blocked on, how long it waited
  on each budget class).
- Rate limits, priorities, backlog caps: *planned*, same scope model. No debounce/throttle today.
- Caveats: fresh-cluster-only enablement; scope ⇒ sharding key (low-cardinality scopes hot-spot).

hypergraph comparison: Tier 0 `max_concurrency` is per-map-call; designed host has
`max_active_runs` per host. Restate's runtime-editable, hierarchical, pattern-matched budgets —
plus SQL that shows *which* budget blocks an invocation — is a full tier beyond both, and beyond
panda's hyperlimit AIMD gate (which adapts but is code-local).

## 8. Scheduling & timers

Source: [develop/python/durable-timers](https://docs.restate.dev/develop/python/durable-timers).

- `await ctx.sleep(delta=timedelta(seconds=10), name="wait for payment")` — durable, "months or
  even years"; server re-arms across crashes (12h sleep failing at 8h sleeps 4 more).
- Delayed messages (`send_delay=` / `?delay=`) are the recommended future-scheduling verb; docs
  explicitly prefer them over sleep+send (caller completes immediately, no VO blocking, no
  deployment pinning).
- Timeouts: race any DurableFuture against `ctx.sleep` via `restate.select`.
- **No native cron** — docs admit it and point to a build-it-yourself guide (VO + sleep loop /
  delayed self-send) ([guides/cron](https://docs.restate.dev/guides/cron)).
- Clock caveat: SDK computes wake-time from *its* clock; server fires on *its* clock —
  unsynchronized clocks skew timers (documented accordion).

## 9. The 3 steals

1. **Every handler is a URL — the ingress grammar.**
   `/restate/{call|send}/{Service}/{key}/{handler}?delay=10s` + `idempotency-key` header +
   `/restate/attach|output/{id}` makes every unit of work invokable, deduplicated, schedulable,
   and pollable with `curl` — no client SDK, no host API to learn. The durable host should ship
   this exact convention as its HTTP surface (`/hg/{submit|call}/{graph}/{workflow_id}`,
   `?delay=`, attach/output verbs), with the typed Python client as sugar over it.
   ```shell
   curl "localhost:8080/restate/send/MyService/myHandler?delay=10s" --json '{"name": "Mary"}'
   # → {"invocationId":"inv_1aiqX0...","status":"Accepted"}
   curl localhost:8080/restate/output/inv_1aiqX0...   # not ready | result | not found
   ```

2. **Pause-on-exhaustion + `resume --deployment latest` — the operator brake with a release
   lever.** Retries exhaust → invocation *parks* (visible in SQL/UI as paused) instead of dying;
   the fix ships; one verb repoints it at the new code. hypergraph's `recovery_exhausted` brake
   designs the parking but not the release-onto-fixed-code verb — and state-based recovery makes
   it *safer* for us than for them (no non-determinism warning needed).
   ```shell
   restate invocations resume <invocation_id> --deployment latest
   ```

3. **The rules CLI — runtime-editable hierarchical concurrency budgets.** Limits live in a
   cluster rule book, not in code; wildcard patterns, most-specific-wins, two nested limit-key
   levels, soft disable/enable, and `sys_scheduler`/`sys_user_limits` SQL showing exactly which
   budget an invocation is queued on. A host-side `rules set "reingest/*" --concurrency 4` beats
   redeploying to change `max_active_runs`.
   ```bash
   restate rules set "*"        --concurrency 10000  # each organization
   restate rules set "*/*"      --concurrency 1000   # each team in any org
   restate rules set "*/*/*"    --concurrency 10     # each individual user
   ```

4. **(minor) Multi-shot signals.** `await ctx.signal("steer", type_hint=str)` resolvable
   repeatedly, each await consuming the next value, queued durably even if sent early — a durable
   steering *channel* where awakeables/our answers are one-shot. Maps cleanly onto a future
   "steerable run" story (mid-run operator nudges without a declared gate per message).

## 10. The warnings

- **The determinism contract taxes every other feature — their docs admit it repeatedly.**
  In-place code changes: "In-flight invocations might keep failing with non-determinism errors"
  ([versioning](https://docs.restate.dev/services/versioning), which also enumerates the unsafe
  edits: reorder/add/remove SDK ops, change inputs, change branching). `resume --deployment`:
  "the invocation will start fail with **non-determinism errors**!". The immutable-deployment
  ceremony, pinned/zombie invocations (`--zombie` is a CLI filter because this happens enough to
  need one), and FaaS-versioned-URL guidance all exist to manage replay fragility. Validates
  hypergraph's no-determinism-contract, state-based-recovery bet.
- **Exclusive-handler blocking is a documented footgun in three places.** Awaiting an awakeable
  or sleeping in an exclusive VO handler queues *every* call to that key; long sleeps also pin
  old deployment versions — the docs' own fix is "break this up into multiple handlers that call
  each other with delayed messages," i.e., restructure your code around the engine's actor lock.
- **Kill is admitted-unsafe and cancel is partial.** Kill: "may leave your service in an
  inconsistent state. Only use as a last resort." Both kill *and* cancel leave one-way/delayed
  children running (detached from the call tree) — a fire-and-forget fan-out survives its
  parent's cancellation, which is exactly the batch-cancel case hypergraph must get right.
- **Flow control is fresh-clusters-only and experimental** (three feature flags; "configuration
  and APIs may change"); delayed invocations are unsupported for workflows; `restart-as-new` for
  workflows and restart-from-prefix are **UI-only**, so scripted ops hit gaps.
- **Default timeouts bite long steps.** `inactivity-timeout` 1m / `abort-timeout` 10m are server
  config; a 5-minute `ctx.run` LLM call gets suspended/aborted until you tune the server — the
  error-handling guide has a dedicated section because users hit it.
- (From `engines.md`, not repeated: BUSL-1.1, RocksDB-only store, Python SDK trailing TS.)

## 11. Verdict vs hypergraph

At the user-facing API level Restate has five things our design lacks: a plain-HTTP ingress
grammar that makes every handler callable/schedulable/pollable without a client library; durable
cross-service RPC and delayed self-scheduling (`ctx.service_call`, `send_delay=`) as in-handler
verbs; durable entity state (Virtual Object K/V with per-key serialization — our runs have no
"entity" to hang state on between runs); a complete operator verb set (pause, resume-onto-new-code,
kill, purge, bulk-by-target) where we have only stop/redrive; and runtime-editable hierarchical
concurrency budgets. We are ahead where it matters most for panda-shaped work: batch is a
first-class object (map/map_iter/manifest/item keys/tolerance — Restate literally has no batch
verb, only hand-rolled fan-out), observation is push (iter/watch/progress vs their block-or-poll
SQL), lineage (fork_from/retry_from) is API where their restart-from-prefix is UI-only, answers
are schema-validated before settling (their signals/awakeables deserialize-or-explode), and our
no-determinism-contract recovery erases the entire class of non-determinism failures their docs
spend pages warning about. Net: do not adopt (BUSL + RocksDB, per engines.md) — but copy three
API shapes into the durable host: the ingress URL grammar with `?delay=` and output-peek, the
pause-on-exhaustion → `resume --deployment` operator loop, and the rules CLI for concurrency;
and keep `sys_scheduler`-style "what is this run blocked on" as the bar for host introspection.
