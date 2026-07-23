<!-- wayfinder:map -->

# Wayfinder — durable host (Tier 1)

Charted 2026-07-23. The ADRs referencing a "wayfinder map ticket" resolve
here. Design authority: ADRs 0005–0008 (`docs/adr/`), PRDs 0010–0011
(`docs/prd/`), defect history in
`docs/research/2026-07-21-durable-host-canon-grill.md`. First consumer:
panda (`../panda/docs/issues/wayfinder-api-cleanup.md`, ticket 0028) —
constraints from that deployment: single process, retry only
safe-to-repeat steps, consumer keeps no job machinery of its own.

## Tickets

### HG-1 — Owner sign-off: accept ADRs 0005–0008; resolve version identity

**What to decide:** Flip ADRs 0005/0006/0007/0008 from "proposed" to
accepted, and answer ADR 0007's open question — what a durable run pins as
its version. Recorded lean (2026-07-23, from the panda sitting): an
explicit human-set version string (matching numbered-drop practice), with
the structural hash recorded for diagnostics only — hash-as-identity would
strand every in-flight run on any code edit.

**Blocked by:** None — owner action.

- [ ] Four ADR status headers flipped, acceptance dated
- [ ] ADR 0007 amended with the chosen identity
- [ ] PRD 0010's "blocked" status cleared

### HG-2 — Implement PRD 0010: durable pause slots + atomic settlement

**What to build:** PRD 0010 as written — `pause_id`, durable question
projection and answer port, `settle_pause` CAS with its typed errors, slot
written in the same transaction as the paused step, Memory + SQLite
backends.

**Blocked by:** HG-1.

- [ ] PRD 0010 acceptance list green on both backends

### HG-3 — Implement PRD 0011: local durable host + SQLite Run Home

**What to build:** PRD 0011 as written — `Graph.with_runner()`, `serve(...,
home=RunHome.open(...))`, verbs submit/answer/stop/watch,
`work_forever()` with the exclusive worker lock, restart scan re-entering
unfinished runs, version refusal per HG-1's identity, synchronous
checkpoint durability for Home-bound runners.

**Blocked by:** HG-2.

- [ ] PRD 0011 acceptance list green; restart/double-submit/double-answer
      proven by tests
- [ ] Panda's adoption ticket (`../panda/docs/issues/0028`) unblocked

### HG-4 — Land the stateful-resource lifecycle branch

**What to build:** Finish `codex/stateful-resource-lifecycle` (@stateful
handles, resource scopes) and merge. Pre-merge fixes from the 2026-07-23
panda review: recognize a coroutine `close` as async cleanup (consumers
use `async def close()`; the branch treats `close` as sync-only), raise a
loud config error for a lazy handle passed as a runtime input, and add
tests that one failing cleanup does not prevent later cleanups.

**Blocked by:** None — independent of HG-1..3.

- [ ] Branch merged with the three fixes
- [ ] Consumer can delete its reflective graph-closer (panda
      `graph_lifecycle.py`)

## Edges

```
HG-1 ─→ HG-2 ─→ HG-3 ─→ panda 0028 (adoption)
HG-4 ────────────────→ panda 0028 (resource-scope half)
```
