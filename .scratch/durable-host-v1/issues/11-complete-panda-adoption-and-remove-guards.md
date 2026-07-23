# 11 — Complete Panda durable-host adoption and remove product guards

**What to build:** Move Panda ingestion and review background work fully onto
the durable Host, prove restart safety for repeat-safe and effectful paths,
and then remove the queue organs, temporary restart and duplicate guards,
BackgroundTasks, and reflective resource closer that Hypergraph now replaces.

**Blocked by:** 07 — Prove Panda integration on repeat-safe work; 08 — Persist pending-node boundaries before sibling execution; 09 — Reserve effect identity and surface unknown outcomes; 10 — Land stateful resource lifecycle for consumer cleanup.

**Status:** ready-for-agent

- [ ] Ingestion and review work use the Host worker lifecycle rather than product BackgroundTasks
- [ ] Real kill tests preserve completed work and surface unsafe ambiguous effects for review
- [ ] Temporary restart, duplicate, and queue bookkeeping are removed only after equivalent Host proof passes
- [ ] Reflective graph cleanup is removed in favor of scoped resource lifecycle
- [ ] The operator walkthrough shows what became possible and the production test suite passes
