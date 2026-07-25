# 12 — Tune active-Run admission and delay start

**What to build:** Let operators tune local active-Run capacity while the
worker is live and let callers persist one-shot future work that becomes
claimable only when store-authoritative time reaches its requested start.

**Blocked by:** 04 — Stop, restart, and exhaust recovery safely; 05 — Submit and watch an immutable durable Batch.

**Status:** done — landed in commit `edde1fe3`; box 1 was ticked early there
(see the note) and two-axis review findings were repaired in the follow-up
commit.

- [x] The active-Run cap changes at runtime and over-limit work waits in claim order
- [x] Delayed, paused, incompatible, and recovery-exhausted Runs consume no active slot
- [x] A claimed Run waiting on an injected provider limiter still consumes its Host slot without becoming failed or spending a retry attempt; provider limiters live at graph, node, or component scope with honest scope names, and a provider-quota component acquires at the exact scarce call
- [x] Future start time persists and fingerprints at submission and past time becomes immediately eligible
- [x] Stop-before-due prevents execution while reject, cancel, keyed, and expression strategies remain absent

## Note on box 1 — ticked early in `edde1fe3`, proven in the follow-up

At `edde1fe3` the cap was a process-local Python attribute
(`RunHome._max_active_runs`), read back by the same object that set it. That
proved "the cap changes at runtime" only for code holding the *worker's own*
`RunHome`, which is not the ticket's operator ("tune local active-Run
capacity while the worker is live") and not US41. It also made US21 wrong by
construction: a `RunHomeClient` opened in a separate inspection process
computed `admission_full` from its own uncapped object and reported `QUEUED`
for exactly the work the worker was holding back as `ADMISSION_LIMITED`.

The follow-up commit moves the cap into the store (`host_settings`, schema
v6, guarded by `_ensure_v6_objects`). The setter writes through, the getter
and both admission checks read the store inside their transactions, and two
new tests hold two `RunHome` objects on one store to prove a cap set through
one is honored and reported by the other. Passing `max_active_runs` to
`open()` writes through; omitting it adopts the stored value.

## Note on box 3 — "compose" was not true as written

Box 3 claims provider limiters "live at graph, node, or component scope" and
compose. External peer review (Codex gpt-5.6) found, and this repo
reproduced, that they did not compose: `provider_permits` fixed the order of
the scopes *within* one execution path (graph budgets, then the node budget)
and imposed no order across limiter instances. Two individually legal graphs

    Graph([node(provider_limit=beta)]).with_provider_limit(alpha)
    Graph([node(provider_limit=alpha)]).with_provider_limit(beta)

therefore acquired `(alpha, beta)` and `(beta, alpha)`. Run concurrently,
each held the permit the other waited for. The wait is deliberately not an
attempt, so nothing timed out: both permits were held forever. The same
cycle could be spelled across the nested-graph boundary, where
`compose_graph_limits` merges an enclosing budget with an inner one.

Repaired by `fix(runners): order provider-limit acquisition globally to
prevent deadlock`: every `ProcessLocalLimiter` is stamped with a
never-reused construction rank, and `provider_permits` deduplicates by
identity and then sorts by that rank, so every execution path in the process
takes shared limiters in one order. Proof lives in
`tests/test_runners/test_provider_limit_ordering.py` (two-graph cycle, a
three-limiter rotation, nested composition, dedup, and a sync mirror of
each — all bounded so a regression fails instead of hanging).

The same change closes a second hole the box did not anticipate: a delegated
**synchronous** runner (`as_node(runner=SyncRunner())`) under `AsyncRunner`
runs inline on the event-loop thread, where the ticket-12 loop-thread guard
makes a contended acquire *raise*. That failed only under contention — it
passed uncontended — so it now raises `IncompatibleRunnerError` at the
GraphNode boundary before the nested run starts.
