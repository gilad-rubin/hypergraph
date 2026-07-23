# Clean-room experiment: three models design the durable host independently

- Date: 2026-07-21
- Issue: none yet; method + synthesis capture from the durable-serving design conversation
- Implementation: none; research capture only
- Measured revision: `86618f7d80db841874239ac12872bc3098e79f5b`
- Status: complete — nine decisions locked by four-way unanimity; open taste calls recorded in `2026-07-21-consolidated-durable-host-design.md`

> **Intent, not canon.** This captures an experiment's method and outcome.
> The binding decisions graduate to ADRs; this note is the evidence trail.

## Method (clean-slate / clean-room protocol)

Design fixation is real: a designer who has seen an implementation reproduces
it. To escape six rounds of accumulated framing, the reopened question —
"what objects should a user touch to run several graphs durably?" — was
re-asked from scratch behind a wall:

- A **brief** stated eleven needs (N1–N11) as role-felt pains (retrying
  webhooks, a week-long human pause, a crash mid-payment, deploys over parked
  runs, notebook parity) and hypergraph's primitives **anonymized** to their
  categories ("an execution strategy", "a journal", "resumption is
  state-based"). Zero hypergraph nouns, zero prior design vocabulary — the
  wall keeps what *constrains* the question, hides what *answers* it.
- Three models answered independently, fresh-context, no repo access:
  Codex `gpt-5.6-sol` (xhigh), Fable 5, Kimi K3 (high). The brief lives at
  `~/cleanroom-durable-serving/brief.md` (Stage A public, Stage B sealed
  mapping back to real names).

Full answers: `2026-07-21-cleanroom-codex-durable-host.md`,
`2026-07-21-cleanroom-fable-durable-host.md`,
`2026-07-21-cleanroom-kimi-durable-host.md`.

## Outcome — unanimous (all three clean rooms + the anchored design)

1. Host-as-root: graphs registered into one host, each with its own runner.
2. Epoch-fenced single-writer enforced **inside** every journal write, same
   transactional store as the journal; lease expiry revokes authority, never
   proves death.
3. Durable idempotent command intake; per-workflow total order; stop-vs-
   continue races decided by commit order with truthful rejections.
4. State-based resume as the recovery mechanism (claim → re-invoke
   `run(workflow_id=...)`); no replay contract.
5. Version pinning at start + loud `stranded` + explicit migration via the
   existing `fork_from` primitive.
6. Parked runs are rows at rest.
7. Stable effect identity for side-effect nodes; never advertise exactly-once.
8. Do not build: broker, lock service, process supervisor, HTTP surface,
   dashboard (claimable rows ARE the queue; lease = visibility timeout).
9. Storage-agnostic concept names; the user-facing axis is local-vs-shared
   authority, chosen once at construction.

## Notable divergences and unique contributions

- **Atoms**: Kimi folded everything into 2 records (run row + journal);
  Fable/Codex kept a command log (audit, ordering, multiple pending
  questions). Codex added durable cursor-events; Fable and Kimi both said
  "the journal already is the event history" (2-v-1, adopted).
- **Codex**: pause answered exactly-once across different request ids;
  effect id excludes attempt number; `blocked_version` state.
- **Fable**: `Home.open("file:..." | url)` construction; `accepts=(...)`
  revision-compatibility tuple; stop→continue rejected as
  `superseded("stopped")`; "deploys are just crashes with better manners."
- **Kimi**: fencing gate as one `INSERT ... SELECT` statement; "the claim
  scan is the queue — lease = visibility timeout"; shared backend framed as
  a network-backed implementation of the *existing* journal interface.

## What the clean rooms structurally could not see (anchored-only findings)

The brief abstracted superstep granularity and the sp workspace away, so no
clean room could find: pending node writes (crash mid-superstep loses
successful siblings' outputs — records save at superstep end), the
attempt-ledger contract redefinition under leases (stranded attempts stay
OUTCOME_UNKNOWN and may physically overlap), attempt-ledger coverage
widening (today only retry/timeout nodes reserve attempts), and the
superposition relationship (`2026-07-21-superposition-relationship.md`).
Both processes — anchored iteration and clean-room restart — earned their
keep; neither alone was sufficient.
