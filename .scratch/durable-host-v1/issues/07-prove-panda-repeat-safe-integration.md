# 07 — Prove Panda integration on repeat-safe work

**What to build:** Route one real Panda ingestion or review path through the
local Host and prove submission, observation, stop, and kill-and-restart on
work that is known safe to repeat. Keep every existing product guard and make
no production or release claim.

**Blocked by:** 04 — Stop, restart, and exhaust recovery safely; 06 — Enforce Batch tolerance and item-scoped rerun.

**Status:** ready-for-agent

- [ ] A real Panda path submits and watches work through the Hypergraph Host
- [ ] A real process kill and restart preserves completed work and finishes repeat-safe work
- [ ] Operator-visible Run and Batch states match the approved prototype
- [ ] Panda's temporary restart, duplicate, and BackgroundTasks protections remain in place
- [ ] The proof is inspectable and explicitly labeled controlled, repeat-safe, and not production completion
