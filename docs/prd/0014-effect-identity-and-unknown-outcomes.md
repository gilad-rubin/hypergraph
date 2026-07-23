# 0014 — Effect identity reservation and unknown outcomes

status: accepted-intent (folded from amendment A10 on 2026-07-24, Durable Host V1 ticket 01; implementation is ticket 09)

## Why this exists

A plain node that calls a provider — charges a card, sends an email, files a
record — is silently unsafe under Host recovery today: if the process dies
after dispatch but before settlement is witnessed, recovery cannot tell
"never ran" from "ran and committed externally." Re-dispatching spends the
effect twice; suppressing it loses committed work. Recovery must never
guess. Amendment A10 requires the declaration and reservation contract to
be defined before any production adoption claim.

## Fixed acceptance contract

Before (today — ambiguity is invisible):

```python
@node(output_name="receipt")
def charge_card(claim: Claim) -> Receipt:
    return stripe.charge(claim.amount)   # kill after dispatch, before
                                         # settlement → recovery cannot tell
                                         # "never ran" from "charged"
```

After:

```python
@node(output_name="receipt", effects=EXTERNAL)   # declares the boundary
def charge_card(claim: Claim) -> Receipt:
    return stripe.charge(claim.amount)

# Before the provider call, the Host commits a stable effect identity:
#   effect_id = (run identity, node address, attempt identity)
# Kill after dispatch but before settlement → the effect surfaces as
# OUTCOME_UNKNOWN. Recovery never dispatches it again automatically;
# an operator reviews and decides explicitly.
```

Requirements:

- **Declaration.** Every node that may cause an external effect declares
  that fact on the node itself. Undeclared nodes are treated as
  repeat-safe; the declaration is part of graph structure, so changing it
  changes the structural hash.
- **Reservation before dispatch.** For every declared effect, a stable,
  durable effect identity is committed before any provider call — derived
  from run identity, node address, and attempt identity so a retry or
  recovery can name exactly one effect occurrence.
- **Three-way truth.** Kill tests distinguish: pre-dispatch loss (never
  reserved or reserved-but-never-dispatched → safe to dispatch), witnessed
  settlement (committed StepRecord → complete), and ambiguous post-dispatch
  loss (dispatched, settlement unwitnessed → `OUTCOME_UNKNOWN`).
- **Unknown is terminal for automation.** An unknown outcome is never
  re-dispatched automatically and never claimed as success or failure. It
  surfaces for explicit operator review; resolving it is a deliberate act,
  consistent with the existing `Unknown attempt outcome` vocabulary and
  `AttemptOutcomeUnknownError`.
- **Node-owned retry untouched.** Host recovery and effect reservation do
  not reset Retry budgets, Retry windows, or backoff; reserving an effect
  identity is not a retry.
- A controlled repeat-safe Host proof (ticket 07) may precede this PRD, but
  production adoption, guard deletion, and any side-effect safety claim may
  not (A10).

## Test plan (red first)

- Kill before dispatch → effect never reserved as dispatched → recovery
  dispatches once.
- Kill after witnessed settlement → no re-dispatch; completed boundary
  stands.
- Kill after dispatch before settlement → `OUTCOME_UNKNOWN`; no automatic
  re-dispatch across repeated recovery cycles; explicit review path
  resolves it.
- Declared vs undeclared nodes: only declared nodes receive reservation
  treatment; undeclared nodes follow ordinary repeat-safe recovery.
- Retry parity: node RetryPolicy budgets and windows unchanged by Host
  recovery; sync + async parity; CI-equivalent run green.

## Out of scope

Automatic effect reconciliation with providers (operator judgment is the
contract), exactly-once side effects (impossible; the contract is
honest ambiguity), Postgres-tier fencing (lease epochs, PRD 0015/0016).
