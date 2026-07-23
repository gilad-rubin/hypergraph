# Canon grill of the durable-host design: contradictions found before spec-writing

- Date: 2026-07-21
- Issue: none yet; adversarial review capture (Codex gpt-5.6-sol, xhigh, repo read access)
- Implementation: none; research capture only
- Measured revision: `86618f7d80db841874239ac12872bc3098e79f5b`
- Status: complete — findings folded into ADRs 0005–0008 and PRDs 0010–0011; remaining PRDs tracked on the wayfinder map

> **Intent, not canon.** This is the defect list the design carried before the
> specs were written, kept so future work knows why the specs say what they say.

## Blocking findings (all corrected in the ADR/PRD set)

1. **`host.run() -> RunResult` crosses a local-only evidence boundary.**
   `RunResult` carries exact local exception objects, `RunLog`, inspection
   and checkpoint-write evidence (`results.py:115-149`); the durable `Run`
   row does not (`checkpointers/types.py:231-297`). A detached worker cannot
   reconstruct it truthfully. v1 drops `host.run()`; durable serving uses
   `submit()` + `watch()`. A future durable projection needs its own ADR
   extending ADR 0004.
2. **The pause slot is not durable.** `PauseInfo.response_key` exists only in
   memory (`results.py:666-678`); the persisted paused StepRecord carries
   neither question projection nor answer port (`checkpoint_helpers.py:73-86`).
   Durable `answer()` is impossible until pause settlement is persisted —
   this gates the entire Tier 1. → PRD 0010, first on the critical path.
3. **A separate "run head" risks a second execution journal.** Steps are the
   source of truth (`base.py:233-240`). Coordination facts (claim, lease,
   command sequence, required version) must be derived-or-adjacent, never an
   independently written terminal status. Host phases must NOT enter
   `RunStatus`/`WorkflowStatus`.
4. **Answer dedup needs `pause_id`, not just workflow_id.** Repeated pauses in
   a loop make a late answer capable of answering the wrong occurrence. The
   answer CAS requires the caller's observed pause identity.
5. **`wake_at` as a bare column is wrong.** It supplies neither the answer
   port nor values; stale timers race later pauses; and it must not bypass
   node-owned `RetryPolicy` (locked: `2026-07-14-retry-timeout-contract.md`
   Q4/Q6). The durable unit is a **scheduled answer**: `(workflow_id,
   pause_id, due_at, values)` with apply-only-if-pause-current semantics.
6. **SQLite Tier 1 must not advertise leases.** Half-enforced fencing is
   unsafe; Tier 1 uses one OS-level exclusive worker lock per Run Home.
   Epoch enforcement inside every mutation is Tier 2 (Postgres).
7. **Fleet-wide `blocked_version` is unknowable without worker
   advertisements.** Store `required_version`; expose aged-unclaimed queries;
   never claim global version blocking.
8. **Version identity is undefined.** Runner stores structural + code hashes
   but resume checks structural only (`lineage.py:69-80`). ADR 0007 must pick
   what a durable run pins.
9. **`watch()` cannot promise full event replay.** Event processors are
   best-effort (`dev/CORE-BELIEFS.md:71-75`); chunks drop under backpressure.
   Define watch as durable StepRecord/command replay + optional live preview.
10. **`Graph.with_runner()` does not exist at root** (only `GraphNode`); the
    runner binds its checkpointer at construction. `serve()` needs an
    immutable cloning contract (`runner.with_checkpointer(...)`), never
    mutation of the supplied runner.
11. **Default checkpoint durability is too weak for a Home.** Background
    `"async"` writes can lose recent nodes; a durable Home requires
    synchronous authoritative writes and rejects `"exit"`.
12. **Daft cannot be hosted yet** (no checkpointing/events, rejects delegated
    runners — `daft/runner.py:48-93`): `serve()` fails at construction for it.

## Retry-contract verdict

Lease + `OUTCOME_UNKNOWN` survives all nine locked answers, provided: host
re-dispatch is recovery (not node retry); expiry revokes commit authority
without proving work stopped; recovered `STARTED` attempts become
`OUTCOME_UNKNOWN` and raise `AttemptOutcomeUnknownError` (current behavior,
`attempts.py:501-515`); timed continuation never resets retry windows. The one
supersession needed: the `resolve_stranded_attempts` precondition "caller
knows no invocation still runs" becomes "no prior lease may still commit."

## Vocabulary rulings (adopted)

**Run Home** (the one store owning runs, steps, commands, claims) ·
**Durable host** · **Host command** · **CommandReceipt** (RowReceipt/
TableReceipt already exist) · **Lease with an epoch** (expiry = authority
loss) · **scheduled answer** (not bare wake_at) · **version-incompatible /
requires version** (NOT "stranded" — that word is taken by crash-stranded
attempts) · **paused at rest** (prose only) · **pause_id** (one durable
interrupt occurrence). Handles stay process-local per ADR 0004.

## Critical build order

```text
pause truth (PRD 0010)
  -> local host (PRD 0011)
      -> timed continuation (PRD 0012)

node-boundary writes (PRD 0013) + effect identity (PRD 0014)
  -> epoch-fenced Postgres Home (PRD 0015)
      -> shared host kill matrix (PRD 0016)
```
