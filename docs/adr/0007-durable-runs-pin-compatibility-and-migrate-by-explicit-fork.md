# Durable runs pin compatibility and migrate by explicit fork

**Status:** Accepted on 2026-07-23 at the durable-host amendment sitting,
with the version-identity open question resolved as recorded below
(decisions and provenance: `docs/research/2026-07-23-durable-host-amendments.md`;
delivery contract: `docs/prd/0017-durable-host-v1-program.md`). Amendment
A16 folded on 2026-07-24 (Durable Host V1 ticket 01). Grounded in the
clean-room convergence and canon grill (`docs/research/2026-07-21-*.md`).

## Context

Code deploys while runs are parked for days. New, incompatible code must not
silently pick up and corrupt old runs. The runner already stores structural
and code hashes but resume validates structural compatibility only
(`lineage.py`). Meanwhile, no worker can know what *other* workers serve, so
"no compatible worker exists anywhere" is unknowable without worker
advertisements.

## Decision

- **A durable run pins its complete Definition identity at first accepted
  submit.** The pinned identity is the typed tuple
  `DefinitionId(name, deployment_version, structural_hash)`. The
  human-set `deployment_version` (matching numbered-drop practice) is part
  of claim identity; `structural_hash` anchors compatibility checks; the
  code hash is recorded for diagnosis only and never decides claim
  eligibility.
- **Workers claim only runs whose pinned identity they can serve.** A
  worker serves exact Definition identities. Compatibility declarations
  (an `accepts=(...)` tuple on the serve binding) name complete prior
  identities and still require structural compatibility, so compatible old
  Runs can drain after a deployment.
- **Truthful vocabulary:** a run a given worker cannot serve is
  **version-incompatible** for that worker; the Home stores the pinned
  identity and exposes aged-unclaimed queries. Incompatible work remains
  visible and unclaimed — a worker never guesses it can resume old state.
  The design never claims fleet-wide blockedness, and never uses "stranded"
  (reserved for crash-stranded attempts).
- **Repetition is `rerun()`, never migration (A16).** `rerun()` retains the
  source's full pinned Definition identity and source inputs, records
  `retry_of` lineage, and accepts no input override; a Batch rerun may
  narrow only to named item keys from the source manifest.
- **Migration is an explicit fork.** Moving a parked run to new code uses
  an explicit, compatibility-checked `fork` seeded from recorded history,
  recording `forked_from` lineage and a migration reason, authorized by a
  human or explicit policy. Changed inputs use a normal new submit. Rerun
  and fork lineage never merge. No silent in-place upgrade, no history
  patching, ever.

## Consequences

- Deploys become boring: old runs wait for compatible workers or an explicit
  fork; the failure mode is a visible queue, not corruption.
- The full Definition identity anchors the A5 submission fingerprint:
  identical resubmission dedupes only when name, deployment version,
  structural hash, normalized inputs, effective Batch configuration, and
  `start_at` all match.
- The PRD for the shared host (0016) carries the claim-predicate SQL and the
  operator playbook for draining version-incompatible runs.
