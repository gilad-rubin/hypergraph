# 03 — Make Run identity, deduplication, rerun, and fork truthful

**What to build:** Give each accepted Run a complete pinned Definition
identity and a stable start fingerprint. Make webhook-style repeated
submission use the existing Run only when its meaning is identical, add
explicit rerun for repetition, and keep version migration on explicit fork.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** done — landed in commit `0676849f`; two-axis review findings repaired in `a8836312`.

- [x] Definition name, deployment version, and structural hash are pinned and visible
- [x] Identical nonterminal submission returns the existing Run; terminal reuse and fingerprint mismatch return distinct typed conflicts
- [x] A worker refuses incompatible Definition identity while an explicitly accepted prior identity can drain
- [x] Rerun creates a new workflow id with source inputs and retry lineage and accepts no input override
- [x] Fork changes compatible Definition identity with separate fork lineage and a recorded migration reason

## Note on box 2 — the workflow_id namespace was missing its third owner

Box 2 claims reuse of a `workflow_id` yields the existing Run or a distinct
typed conflict. External peer review (Codex gpt-5.6) found, and this repo
reproduced, that acceptance only consulted `host_submissions` and (from
ticket 05) `host_batches`. A Run Home is also an ordinary checkpointer, so a
`runs` row can exist with no host row behind it — Tier-0 work executed
straight against the store. A bare run completed under `workflow_id="same"`,
and `host.submit("w", {...}, workflow_id="same")` was then **accepted** with
`duplicate=False`: the host and the execution journal disagreed about what
`same` was, silently.

Repaired in this commit: `_raise_on_tier0_reuse` runs last in all four
acceptance paths (`_submit{,_sync}`, `_submit_batch{,_sync}`) and on every
generated Batch child id, after the host rows have had their say. The host
holds no pinned identity or fingerprint for a Tier-0 run, so it can never
adopt or dedupe into one: terminal Tier-0 history raises
`AlreadyTerminalError` (US11 — completed history never changes identity), a
still-running one raises `WorkflowIdConflictError`. `rerun()` and `fork()`
derive ids and route through `_submit`, so they inherit it.
Proof: `TestTier0RunIdNamespace` (real bare runs, not fabricated INSERTs).

## Note on box 4 — "a new workflow id" was only true after the previous rerun ran

Box 4 claims rerun creates a NEW workflow id. The retry ordinal was counted
from materialized `runs.retry_of` rows and the id derived **outside** the
acceptance transaction, so two reruns of a failed run requested before the
first executed both chose `<source>-retry-1`; the second returned
`duplicate=True` and silently deduped into the first. Ticket 03's own test
only passed because it executed the first rerun before requesting the
second. Batch rerun counted `host_batches` (correct rows) but still
allocated outside the transaction — the same collision under concurrent
callers.

Repaired in this commit: the ordinal is allocated inside the transaction
that inserts the submission or Batch, over rows that exist at ACCEPTANCE
time, and persisted on the row (`host_submissions.retry_index`). `RunHome`
overrides `retry_workflow{,_async}` to answer with that accepted ordinal, so
`runs.retry_index` matches the id whatever order the reruns execute in.
Proof: `TestRerunIdAllocation` (back-to-back, concurrent `asyncio.gather`,
reverse execution order) and `TestSubsetRerun`'s concurrent Batch case.
