# 08 — Persist pending-node boundaries before sibling execution

**What to build:** Persist enough node-boundary intent that a process death
between sibling executions cannot forget unfinished siblings or make recovery
infer work from an incomplete superstep record.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** done — landed in commit `9e7546e2`

- [x] Every runnable sibling remains durably attributable before any sibling can cause external work
- [x] A real kill between sibling boundaries preserves completed facts and leaves unfinished siblings recoverable
- [x] Nested graph and loop behavior retain the same parent-facing execution identity
- [x] Sync and async runners expose the same recovery result
- [x] Existing checkpoint resume behavior remains compatible
