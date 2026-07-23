# The durable host and superposition (sp): sibling spines, joined by reference

- Date: 2026-07-21
- Issue: none yet; research capture from the durable-serving design conversation
- Implementation: none; research capture only
- Measured revision: `86618f7d80db841874239ac12872bc3098e79f5b` (hypergraph); sp canon read at `../superposition/` same day (CONTEXT.md + ADR 0009)
- Status: relationship settled at design level; the joining-file example is future work

> **Intent, not canon.** Records a cross-repo design position. sp's own ADRs
> are the authority for sp-side claims; re-read them before building.

## Question

Should the durable host integrate with superposition — as an extension of sp,
or as sp's core?

## Verdict: neither — sp's own canon already names the relationship

- sp ADR 0009: "sp is the authority plane and is **execution-agnostic —
  everywhere, with no exceptions**." sp CONTEXT.md: a Run "lives as a
  telemetry trace carrying pins; **not a table of its own**." The durable
  host is precisely a table of runs plus call-path lease/heartbeat traffic —
  the one thing sp is never allowed to be (sp law: consequences, never
  attempts; touched zero-or-once at the moment of consequence).
- ADR 0009 also names the host's role exactly: "Execution happens wherever
  the caller is — a notebook with the engine, **the domain's own serving host
  (an sp client)**, a daft worker." The durable host is that serving host.
- sp reserves `Operation`: "the durable coordinator row for the minority of
  work that must survive crashes, resume, or wait on a decision. Ordinary
  runs never get one." That is the **authority-side shadow** of the host's
  run record — joined by reference, never shared storage.

## The joining pattern (a product's joining file, ~a page)

| Host (execution spine) | sp (authority spine) | Join |
|---|---|---|
| Run record (lease, epoch, phase) | `Operation` row (named/gated work only) | host record carries the Operation ref |
| Graph name + pinned version | Manifest/pin | version string becomes an sp pin on adoption |
| Pause on a human question | Pulled-question Decision (lifecycle 2) | provenance = workflow_id + pause id |
| `answer` command | Decision application receipt | decision id doubles as the command's dedup identity |
| Consequential batch start | One work-order Decision per batch | grain of consent = the batch, never the item |
| Notebook / nightly runs | nothing — the sp-free path is law | — |

Vocabulary guard adopted from this analysis: the host's fencing atom is a
**Lease with an epoch** — never "grant", which is sp's authority atom
`(actor, act, scope, effect)`.

The host must remain fully functional with zero sp ceremony below sp's
adoption thresholds; sp enters through a product's joining file, never
through hypergraph itself.
