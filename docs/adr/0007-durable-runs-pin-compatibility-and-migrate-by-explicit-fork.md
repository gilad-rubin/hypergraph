# Durable runs pin compatibility and migrate by explicit fork

**Status:** Proposed on 2026-07-21, pending maintainer review — **with one open question the maintainer must decide: the exact version identity** (see below). Grounded in the clean-room convergence and canon grill (`docs/research/2026-07-21-*.md`).

## Context

Code deploys while runs are parked for days. New, incompatible code must not
silently pick up and corrupt old runs. The runner already stores structural
and code hashes but resume validates structural compatibility only
(`lineage.py`). Meanwhile, no worker can know what *other* workers serve, so
"no compatible worker exists anywhere" is unknowable without worker
advertisements.

## Decision

- **A durable run pins its version at first accepted submit.** Workers claim
  only runs whose pinned version they can serve; refusal is loud and
  queryable.
- **Truthful vocabulary:** a run a given worker cannot serve is
  **version-incompatible** for that worker; the Home stores
  `required_version` and exposes aged-unclaimed queries. The design never
  claims fleet-wide blockedness, and never uses "stranded" (reserved for
  crash-stranded attempts).
- **Migration is an explicit fork.** Moving a parked run to new code uses
  the existing `fork_from` primitive through durable command intake — a new
  workflow seeded from recorded history, authorized by a human or explicit
  policy. No silent in-place upgrade, no history patching, ever.
- **Open question (maintainer decision, tracked on the wayfinder map):**
  what exactly is the pinned identity —
  `graph.structural_hash` | `graph.code_hash` | an explicit deployment
  version string | a typed combination? The trade-off: structural-only
  permits silent behavior change with same shape; code-hash blocks trivial
  refactors; explicit strings put honesty in the operator's hands.
  Compatibility declarations (an `accepts=(...)` tuple on the serve binding)
  are in scope once the identity is chosen.

## Consequences

- Deploys become boring: old runs wait for compatible workers or an explicit
  fork; the failure mode is a visible queue, not corruption.
- The PRD for the shared host (0016) carries the claim-predicate SQL and the
  operator playbook for draining version-incompatible runs.
