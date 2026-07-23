<!-- wayfinder:map -->

# Wayfinder — durable host (Tier 1)

Charted 2026-07-23; updated 2026-07-24 (Durable Host V1 ticket 01). The
ADRs referencing a "wayfinder map ticket" resolve here. Design authority:
ADRs 0004–0008 (`docs/adr/`, accepted with amendments A1–A16), PRDs
0010–0011, 0013–0014, 0018–0019 (`docs/prd/`), defect history in
`docs/research/2026-07-21-durable-host-canon-grill.md`. First consumer:
panda (`../panda/docs/issues/wayfinder-api-cleanup.md`, ticket 0028) —
constraints from that deployment: single process, retry only
safe-to-repeat steps, consumer keeps no job machinery of its own.

**Delivery plan supersession.** The owner sign-off below (HG-1) is resolved,
and the durable-host build now runs as the Durable Host V1 program:
`docs/prd/0017-durable-host-v1-program.md` with its ticket tree in
`.scratch/durable-host-v1/issues/` (tickets 01–15) and the inspectable
decision prototype at `docs/prd/0017-durable-host-v1-decision-prototype.md`.
Ticket 02 stays blocked until the maintainer explicitly approves that
prototype. HG-2/HG-3 survive as tickets 13/02–06; HG-4 survives as
ticket 10. Panda adoption is tickets 07 and 11; no Panda guard is deleted
and no production claim is made before ticket 11.

## Resolved decisions (2026-07-23 sitting, folded 2026-07-24)

- ADRs 0005–0008 accepted with amendments A1–A16
  (`docs/research/2026-07-23-durable-host-amendments.md`).
- ADR 0007's open question resolved: the pinned identity is the typed tuple
  `DefinitionId(name, deployment_version, structural_hash)`; the code hash
  is diagnostic only and never decides claim eligibility.
- Public repetition verb is `rerun()` (never `redrive`); migration is an
  explicit `fork()`. New submission stays Definition-bound — no app-less
  submit catalog. OutputLog deferred until a real product need exists.

## Historical tickets (superseded by the 0017 ticket tree)

### HG-1 — Owner sign-off: accept ADRs 0005–0008; resolve version identity

Resolved 2026-07-23; recorded above and in the ADR status headers.

- [x] Four ADR status headers flipped, acceptance dated
- [x] ADR 0007 amended with the chosen identity
- [x] PRD 0010's "blocked" status cleared

### HG-2 — Implement PRD 0010: durable pause slots + atomic settlement

Now Durable Host V1 ticket 13 (`13-persist-typed-pause-slots-and-answer`),
with A8's typed answer contract folded into PRD 0010.

### HG-3 — Implement PRD 0011: local durable host + SQLite Run Home

Now Durable Host V1 tickets 02–06, with the A13 ownership split (Host vs
RunHomeClient, PRD 0018) and the durable Batch contract (PRD 0019).

### HG-4 — Land the stateful-resource lifecycle branch

Now Durable Host V1 ticket 10 (`10-land-stateful-resource-lifecycle`).
Pre-merge fixes from the 2026-07-23 panda review: recognize a coroutine
`close` as async cleanup (consumers use `async def close()`; the branch
treats `close` as sync-only), raise a loud config error for a lazy handle
passed as a runtime input, and add tests that one failing cleanup does not
prevent later cleanups.

## Edges (0017 ticket tree)

```
01 ─→ 02 ─→ 03 ─→ 04 ─→ 05 ─→ 06 ─→ 07 ─┐
      02 ─→ 08 ─→ 09 ───────────────────┤
      10 (independent) ─────────────────┴─→ 11 ─┐
      04, 05 ─→ 12 ─→ 14 ←── 13 ←── 02          │
      09, 11, 14 ───────────────────────────────┴─→ 15
```
