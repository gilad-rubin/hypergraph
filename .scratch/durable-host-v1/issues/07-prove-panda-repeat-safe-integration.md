# 07 — Prove Panda integration on repeat-safe work

**What to build:** Route one real Panda ingestion or review path through the
local Host and prove submission, observation, stop, and kill-and-restart on
work that is known safe to repeat. Keep every existing product guard and make
no production or release claim.

**Blocked by:** 04 — Stop, restart, and exhaust recovery safely; 06 — Enforce Batch tolerance and item-scoped rerun. ALSO BLOCKED on an explicit maintainer message confirming Panda issue 0029 has landed on Panda main — do not open the Panda repo before that confirmation (0029 touches the same files this ticket would).

**Status:** blocked-external (Panda 0029 not yet confirmed landed)

**Proof target (maintainer-specified 2026-07-24):** the Panda INGESTION
WORK-ITEM path, against the defect documented in Panda issue 0029
(`docs/issues/0029-durable-cancel-and-single-drive.md` on Panda main): a
drive's completion rewrites the entire catalog blob from its own in-memory
snapshot, so (symptom 1) a mid-drive cancel is silently undone and reverts
unrelated work-item edits, and (symptom 2) two Retry clicks 200ms apart run
the lifecycle graph twice on one document. The proof must show both races
becoming impossible by construction — symptom 1 via ticket-04 durable stop
with committed-order race resolution, symptom 2 via ticket-03
start-fingerprint dedup — alongside the kill-and-restart this ticket already
requires. Do NOT fix 0029 in this ticket; Panda fixes it separately with
domain-level guards. Read 0029 for the mechanism, not for work to do.

- [ ] A real Panda path submits and watches work through the Hypergraph Host
- [ ] A real process kill and restart preserves completed work and finishes repeat-safe work
- [ ] The cancel-during-drive and double-retry races from Panda issue 0029 are shown impossible by construction through the Host
- [ ] Operator-visible Run and Batch states match the approved prototype
- [ ] Panda's temporary restart, duplicate, and BackgroundTasks protections remain in place
- [ ] The proof is inspectable and explicitly labeled controlled, repeat-safe, and not production completion
