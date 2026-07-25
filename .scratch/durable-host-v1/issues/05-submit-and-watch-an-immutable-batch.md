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
