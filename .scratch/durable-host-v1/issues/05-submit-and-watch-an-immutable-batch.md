# 05 — Submit and watch an immutable durable Batch

**What to build:** Let a caller submit stable logical item keys as one durable
Batch whose children execute as independent Runs. A detached watcher must see
keyed outcomes, completed children after restart, and explicit items that
never started.

**Blocked by:** 03 — Make Run identity, deduplication, rerun, and fork truthful; 04 — Stop, restart, and exhaust recovery safely.

**Status:** done — landed in commit `8091a1d5` (includes the nested-GraphNode child-persistence blocker fix).

- [x] Batch acceptance atomically persists the immutable manifest, pinned inputs, child identities, and start intent
- [x] Each unique logical item key maps to one independent child Run and one keyed outcome
- [x] BatchRef and BatchView expose child counts, outcomes, and explicit unstarted items
- [x] Batch updates have one gap-free durable cursor and never backpressure execution
- [x] A real restart preserves completed child Runs and continues only unfinished repeat-safe children

## Note on box 4 — the cursor was gap-free; the stream was not complete

Box 4 claims Batch updates have one gap-free durable cursor. The `bseq`
numbering was never the problem: `RunHomeClient._watch_batch` computed
`terminal = stopped or settled` and returned on it. `_write_batch_stop`
appends the `stopped` fact FIRST and writes child stop commands the pre-run
gate applies later, each committing its own `child_unstarted` fact — so a
live watcher could receive `stopped`, see no newer rows at that instant, and
return permanently, missing every fact that committed afterwards. That
contradicts PRD 0019 A9 ("`watch(batch_ref, after=cursor)` follows the whole
Batch gap-free"), the invariant this branch has now repaired four times.
`test_watch_terminates_on_durable_stop` codified the premature EOF by
expecting exactly `manifest, stopped`.

Repaired in this commit: `stopped` is a durable control fact, not EOF. The
stream ends only when every manifest child is accounted — settled,
unstarted, or recovery-exhausted (`RunHome._all_children_settled`, which
replaces `_batch_settlement`). Every path that settles a child commits the
child's Batch fact in the SAME transaction as the state flip, so settled
implies delivered. The old test is corrected to
`test_watch_does_not_end_at_the_stop_fact`; a live-watcher regression test
(`test_a_live_watcher_follows_a_stopped_batch_past_the_stop_fact`, ticket 06)
stops a Batch mid-flight and proves all four `child_unstarted` facts arrive
after the `stopped` fact, with the `_accounted_from_stream` oracle showing
6 of 6 items accounted from the stream alone.

Related, same commit: a child parked on a durable pause used to be counted
`active` by `BatchView` and settled by `is_child_settled` at the same time,
so a Batch reported `settled=True` while a human decision was outstanding.
See ticket 13's note.
