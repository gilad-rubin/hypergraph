# Inngest — user-facing API atlas

Researched 2026-07-23 against live docs at inngest.com/docs (TS SDK v4 era; Python SDK 0.5+;
REST API v1 stable, v2 in development). Existing workspace research referenced, not repeated:
waitForEvent semantics + race caveat in `hitl.md` §2c; fan-out/batching partial-failure caution
and self-hosting/state-store analysis in `engines.md`; dedup/registration notes in
`2026-07-21-durable-execution-landscape.md`.

## 1. Identity

Event-driven durable execution platform: an event bus + managed queue + step-checkpointing
SDKs. Execution philosophy is **replay-with-memoized-steps** — the function is re-entered from
the top on every step boundary, completed steps' results are injected by hashed step ID, so all
non-determinism must live inside `step.run`
([how-functions-are-executed](https://www.inngest.com/docs/learn/how-functions-are-executed)).
TypeScript-first (mature, 4 major breaking migrations behind it), Go second, Python third and
explicitly newer/narrower; cloud SaaS is the product, self-host is possible but state is
Inngest-owned schema (engines.md).

## 2. Authoring

One verb: `inngest.createFunction(config, handler)`. The config object is the entire operational
contract — triggers AND the full flow-control block live next to the code
([reference/functions/create](https://www.inngest.com/docs/reference/functions/create)):

```ts
export default inngest.createFunction(
  {
    id: "import-product-images",
    triggers: { event: "shop/product.imported" },
  },
  async ({ event, events, step, runId, logger, attempt }) => { /* ... */ }
);
```

Config keys: `id` (stable across deploys), `name`, `triggers` (up to 10, event and/or cron,
wildcards `app/user.*`), `concurrency`, `throttle`, `rateLimit`, `debounce`, `idempotency`
(CEL key, 24h window), `batchEvents`, `priority`, `retries` (0–20, default 4), `onFailure`,
`cancelOn`, `timeouts` (`start`/`run`), `singleton`.

Python is a decorator, same shape ([multiple-triggers](https://www.inngest.com/docs/guides/multiple-triggers)):

```py
@inngest_client.create_function(
    fn_id="resync-user-data",
    trigger=[
        inngest.TriggerEvent(event="user.created"),
        inngest.TriggerEvent(event="user.updated"),
        inngest.TriggerCron(cron="0 5 * * *")
    ],
)
def my_handler(ctx: inngest.Context) -> None: ...
```

Deployment: `serve({ client, functions })` mounts an HTTP endpoint (`/api/inngest`, PUT
registers; 24+ framework adapters)
([serving-inngest-functions](https://www.inngest.com/docs/learn/serving-inngest-functions)), or
`connect({ apps: [...] })` — an outbound-WebSocket worker that pulls executions; public beta,
long-running servers only, Python `inngest[connect]` 0.5.0+
([setup/connect](https://www.inngest.com/docs/setup/connect)).

Step surface inside the handler ([learn/inngest-steps](https://www.inngest.com/docs/learn/inngest-steps)):
`step.run`, `step.sleep`, `step.sleepUntil`, `step.waitForEvent`, `step.waitForSignal`,
`step.invoke`, `step.sendEvent` (Python: `ctx.step.*` snake_case; parallelism via
`ctx.group.parallel(...)`, TS via un-awaited `step.run` + `Promise.all`
([step-parallelism](https://www.inngest.com/docs/guides/step-parallelism), max 1,000 steps,
4MB total step data)).

## 3. Run verbs

There is **no local run verb**. Every execution goes through the event system or the platform;
you cannot call a function as plain code (the dev server exists to make this tolerable locally).
The complete user-facing verb surface:

| Inngest verb | What it does | hypergraph equivalent |
|---|---|---|
| `inngest.send(event \| events[])` → `{ids}` | Fire-and-forget publish; every function subscribed to that event starts a run. N events = N independent runs (fan-out) ([fan-out-jobs](https://www.inngest.com/docs/guides/fan-out-jobs)) | `host.submit` is the closest, but submit targets ONE graph; **MISSING in hypergraph:** one publish triggering many subscribers |
| `step.sendEvent(id, events[])` | Same as send but memoized as a step — no duplicate sends on retry ([step-send-event](https://www.inngest.com/docs/reference/typescript/functions/step-send-event)) | No equivalent (no event bus) |
| `step.run(id, fn)` | Checkpointed, individually-retried unit; result memoized | Node execution — hypergraph's checkpoint unit, but declared in graph topology, not inline |
| `step.invoke(id, {function, data, timeout})` | Call another function AS a step, await its result; callee runs under its **own** flow-control config; `referenceFunction({appId, functionId, schemas})` for typed cross-app calls ([invoking-functions-directly](https://www.inngest.com/docs/guides/invoking-functions-directly)) | Nested graphs (compile-time). **MISSING in hypergraph:** runtime invoke of an independently-configured workflow with its own limits |
| `step.sleep(id, "2d")` / `step.sleepUntil(id, date)` | Durable timer, up to **one year**, zero compute while parked ([sleeps](https://www.inngest.com/docs/features/inngest-functions/steps-workflows/sleeps)) | **MISSING in Tier 0**; durable tier has scheduled answers only |
| `step.waitForEvent(id, {event, if/match, timeout})` | Park until matching event or timeout → `null` | hitl.md §2c — reference. Interrupt-gate analog, untyped payload |
| `step.waitForSignal(id, {signal, timeout})` + `inngest.sendSignal({signal, data})` | Transactional (not eventually-consistent) resume keyed by signal string; docs: "In most cases, you should use wait for event" ([wait-for-signal](https://www.inngest.com/docs/features/inngest-functions/steps-workflows/wait-for-signal)) | `host.answer(wf, pause_id, value)` — hypergraph validates against a declared answer schema before settling; Inngest injects arbitrary JSON |
| `{ cron: "TZ=Europe/Paris 0 12 * * 5" }` trigger | Scheduled runs; mixes freely with event triggers (10 max) | **MISSING in hypergraph** (no cron anywhere) |
| `serve(...)` / `connect(...)` | App registration / persistent worker pulling executions | `host.work_forever(worker_id)` ≈ connect |
| `GET /v1/events/{eventId}/runs` | Poll run status + `output` by the event id `send` returned ([fetch-run-status-and-output](https://www.inngest.com/docs/examples/fetch-run-status-and-output)) | `handle.result()` / `host.watch` (ours streams; theirs polls — Realtime covers push, §4) |
| `POST /v1/cancellations` `{app_id, function_id, started_after/before, if}` | **Bulk cancel by predicate over a time range** ([cancel-running-functions](https://www.inngest.com/docs/guides/cancel-running-functions)) | `host.stop(wf)` is one-at-a-time. **MISSING: bulk predicate ops** |
| `cancelOn: [{event, if, timeout}]` (config) | Declarative cancellation — up to 5 events; a matching event kills the run, sleeps end immediately ([cancel-on-events](https://www.inngest.com/docs/features/inngest-functions/cancellation/cancel-on-events)) | **MISSING** — hypergraph stop is imperative only |
| Function Replay (dashboard) | Bulk re-run by time range + status filter, paced "as to not overwhelm your application" ([platform/replay](https://www.inngest.com/docs/platform/replay)); UI-only, no API | `host.redrive(source, item_keys=...)` — ours is API-first and item-granular; theirs is run-granular and UI-only |
| — | — | **MISSING in Inngest:** `runner.map` / `MapResult` — no batch aggregate at all. Fan-out produces N unrelated runs with no handle, no aggregate result, no tolerance policy (engines.md caution). `batchEvents` is the *inverse* of map: many events coalesced into ONE run |
| — | — | **MISSING in Inngest:** local in-process run (`runner.run` in a notebook); `map_iter` backpressured streaming; `fork_from`/`retry_from` lineage |

## 4. Observation

- **Dashboard** ([observability-metrics](https://www.inngest.com/docs/platform/monitor/observability-metrics)):
  function status charts, runs/steps throughput, **backlog** (pending runs), top-6 failing
  functions, per-function metrics, event logs with linked runs, Traces page with per-step
  timelines, Insights page for SQL over raw event/run data. Dev Server replicates this locally.
- **Polling**: `GET /v1/events/{id}/runs` → status (`Completed`/`Failed`/`Cancelled`) + output.
- **Realtime** (GA, v4 SDK) ([features/realtime](https://www.inngest.com/docs/features/realtime)):
  typed channels/topics (Zod schemas), published from inside functions, consumed in the browser
  via `useRealtime({channel, topics, token})` with server-minted, channel+topic-scoped tokens.
  Two publish verbs with different durability:

```ts
await publish(ch.tokens, { token: "Hello", step: "research" });          // cheap, may re-fire on retry
await step.realtime.publish("status-complete", ch.status, { message: "Done" }); // memoized, once
```

Watching a **batch** is where it collapses: fan-out children are unrelated runs; there is no
aggregate to watch — you build your own by keying a Realtime channel or counting events.

## 5. HITL / pause

Covered in hitl.md §2c (waitForEvent shape, timeout-returns-null, race discussion #986) — not
repeated. Deltas found now: `step.waitForSignal` adds a transactional resume path keyed by an
arbitrary signal string, and Realtime is the officially blessed way to deliver the "question" to
a human UI. **No settle-time validation**: whatever JSON arrives in the matching event/signal is
injected into the paused run; schema enforcement is whatever your event-schema registry does at
send time. Hypergraph's answer-validated-before-settling (pause survives rejection) has no
Inngest equivalent.

## 6. Failure & retry surface

User-configured ([inngest-errors](https://www.inngest.com/docs/features/inngest-functions/error-retries/inngest-errors)):

- `retries: 0..20` (default 4) — applies per step; a step that exhausts retries throws a
  catchable `StepError`, and the handler can run fallback steps (compensation in plain code).
- `throw new NonRetriableError("Store not found", { cause: err })` — stop retrying.
- `throw new RetryAfterError("Hit Twilio rate limit", "30m")` — schedule the retry (ms, string, Date).
- `timeouts: { start, run }` — max queue wait and max execution duration.
- `onFailure: async ({ error, event, step, runId }) => {}` — fires after retries exhaust; it is
  materialized as a **separate Inngest function** subscribed to the system event
  `inngest/function.failed`, so a single global failure handler is just
  `triggers: { event: "inngest/function.failed" }`
  ([handling-failures](https://www.inngest.com/docs/reference/functions/handling-failures)).
  There is also `inngest/function.cancelled` for post-cancel cleanup.

Operator verbs: bulk cancellation API (predicate + time range), Bulk Cancellation UI, Function
Replay UI (time range + status, paced re-runs, no API). No per-item redrive — the unit is always
the run.

## 7. Concurrency & flow control

The distinctive part of Inngest's API: **six orthogonal declarative controls, each a config key
on the function, each scoped by an optional CEL `key` expression over the trigger event** —
per-tenant fairness is one line, no queue topology to design
([flow-control](https://www.inngest.com/docs/guides/flow-control)).

| Control | Shape | Unit acted on | Scope | Over-limit behavior |
|---|---|---|---|---|
| `concurrency` ([guide](https://www.inngest.com/docs/guides/concurrency)) | `{limit, scope: fn\|env\|account, key}`, up to **2 constraints** per function | **executing steps** (sleeping/waiting runs don't count) | fn default; env/account share a named virtual queue across functions | FIFO queue per key |
| `throttle` ([guide](https://www.inngest.com/docs/guides/throttling)) | `{limit, period 1s–7d, burst, key}` | run **starts** | function (+key) | **enqueue & delay** (GCRA, smooths spikes) |
| `rateLimit` ([guide](https://www.inngest.com/docs/guides/rate-limiting)) | `{limit, period ≤24h, key}` | run starts | function (+key) | **skip entirely — lossy** ("Events that exceed the rate limit are skipped"; events still stored) |
| `debounce` ([guide](https://www.inngest.com/docs/guides/debounce)) | `{period 1s–7d, key, timeout}` | run starts | function (+key) | coalesce; **last event wins**; `timeout` forces execution |
| `batchEvents` ([guide](https://www.inngest.com/docs/guides/batching)) | `{maxSize, timeout, key, if}` | input coalescing: many events → **one run** receiving `events[]` | function (+key) | flush on size OR timeout; 10MiB cap |
| `priority` ([guide](https://www.inngest.com/docs/guides/priority)) | `{run: "event.data.account_type == 'enterprise' ? 120 : 0"}` | queue position | function | factor in seconds, **−600..+600**: "run ahead of any jobs enqueued in the last 120 seconds" |
| `singleton` ([guide](https://www.inngest.com/docs/guides/singleton)) | `{key, mode: "skip"\|"cancel"}` (TS 3.39+) | whole runs per key | function (+key) | skip the new run, or cancel the old one |
| `idempotency` | CEL key string | run creation | function, 24h window | duplicate keys don't start runs |

Per-tenant canonical example (verbatim, concurrency guide):

```ts
concurrency: {
  limit: 1,
  key: `event.data.customerId`,
},
```

Account-scoped keys let unrelated functions compete on one shared virtual queue (e.g., a global
"openai" capacity), each with its own activation threshold. Note the compatibility minefield in §10.

## 8. Scheduling & timers

Cron triggers (with `TZ=` prefix) coexist with event triggers on one function; `step.sleep` /
`step.sleepUntil` park up to a year with zero compute and no concurrency-slot consumption;
`debounce.timeout` and `cancelOn.timeout` are the other time knobs. No first-class "send this
event at time T" on the send API; the idiom is a function that sleeps then acts.

## 9. The 3 steals (+1)

1. **Flow control as a declarative, CEL-keyed config block on the workload** — concurrency,
   throttle, debounce, priority all accept `key: "event.data.customerId"`, turning multi-tenant
   fairness from queue architecture into one line. Hypergraph's durable tier has only a global
   `max_active_runs`; a `limits=` block on `serve()`/`submit` with a key function would be the
   analog.
   ```ts
   concurrency: { limit: 1, key: `event.data.customerId` }
   ```
2. **Priority as bounded queue-time credit in seconds** — not abstract priority classes:
   `run: "... ? 120 : 0"` means "may jump ahead of anything enqueued in the last 120s". Bounded
   (±600s) so starvation is structurally impossible, dynamic per run, composes with FIFO.
3. **`onFailure` is just another function on a system event** — failure routing is composition,
   not a callback registry: `triggers: { event: "inngest/function.failed" }` gives a global
   failure pipeline with steps, retries, and observability of its own. Hypergraph's
   `recovery_exhausted` brake could emit a watchable system event the same way.
4. (Bonus) **Dual-durability publish** — `publish()` (cheap, may re-fire on retry) vs
   `step.realtime.publish()` (memoized, exactly-once-ish): the API makes the at-least-once vs
   once cost trade-off explicit at the call site instead of pretending one primitive covers
   token streams and state transitions alike.

## 10. The warnings

1. **The replay/determinism contract is the tax for the inline-step ergonomics.** Every step
   boundary re-enters the function from the top over HTTP; any non-determinism outside
   `step.run` corrupts memoization ("must be placed within a step.run call" —
   [how-functions-are-executed](https://www.inngest.com/docs/learn/how-functions-are-executed));
   loops pay a replay round-trip per iteration; step IDs + order are an invisible versioning
   contract for in-flight runs. The v3 migration retro-fitted mandatory IDs onto `sleep`/
   `waitForEvent` precisely because implicit IDs made "determinism across changes to a function"
   unreasonable ([sdk/migration](https://www.inngest.com/docs/sdk/migration)) — an admitted
   design correction. Hypergraph's state-based no-replay stance dodges this entire class.
2. **The flow-control suite is pairwise incompatible and the names lie.** Batching can't combine
   with idempotency, rateLimit, cancelOn, priority, or debounce; singleton can't combine with
   batching or concurrency; and `rateLimit` silently **drops** work while `throttle` delays it —
   worse, v2 renamed the original `throttle` to `rateLimit`, then a new `throttle` with the
   opposite (lossless) semantics appeared later. Their own docs need a dedicated comparison page
   to untangle it. Six orthogonal-looking knobs, a hidden compatibility matrix.
3. (Also) `waitForEvent` race window — hitl.md §2c / discussion #986, not repeated. And four
   breaking SDK majors (v0→v4) in a few years: config-as-data next to code is lovely to write
   but every rename is a user migration.

## 11. Verdict vs hypergraph

What Inngest has that our design lacks at the API level: the entire **declarative flow-control
vocabulary** (CEL-keyed concurrency/throttle/debounce/priority/singleton — our durable tier has
one global `max_active_runs`), **cron triggers**, **declarative cancellation** (`cancelOn` event
matching next to imperative stop), **bulk predicate operations** (`POST /v1/cancellations` with
`if` over a time range), **durable sleep**, and a **client-deliverable typed streaming surface**
(Realtime channels with scoped tokens — our `watch` stops at the Python process). What we do
better: a real local tier (Inngest functions cannot run as plain code, ever — `runner.run` in a
notebook has no equivalent); first-class batch with an aggregate handle, item keys, tolerance,
and item-granular redrive (their fan-out yields N orphan runs, their Replay is UI-only and
run-granular, their `batchEvents` is input coalescing, not map); schema-validated answers that
settle-or-survive vs inject-whatever-JSON; and no replay/determinism/step-ID contract taxing
every user. Net: keep our execution model and batch story, steal their flow-control config block
(keyed limits + bounded priority credit) as a `serve()`/`submit`-level surface, add declarative
cancel-on and a bulk-ops predicate verb to the host, and treat Realtime's scoped-token streaming
as the reference for taking `watch` beyond the process boundary.
