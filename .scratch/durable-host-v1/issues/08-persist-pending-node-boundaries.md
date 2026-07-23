# 08 — Persist pending-node boundaries before sibling execution

**What to build:** Persist enough node-boundary intent that a process death
between sibling executions cannot forget unfinished siblings or make recovery
infer work from an incomplete superstep record.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** ready-for-agent

- [ ] Every runnable sibling remains durably attributable before any sibling can cause external work
- [ ] A real kill between sibling boundaries preserves completed facts and leaves unfinished siblings recoverable
- [ ] Nested graph and loop behavior retain the same parent-facing execution identity
- [ ] Sync and async runners expose the same recovery result
- [ ] Existing checkpoint resume behavior remains compatible
