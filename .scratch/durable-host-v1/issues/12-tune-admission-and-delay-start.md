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
