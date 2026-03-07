# Runners Shared — Agent Guide

The scheduling and state engine. Read this before modifying `helpers.py`, `types.py`, or `template_*.py`.

## Node Readiness (`get_ready_nodes`)

A node is ready when ALL of these pass (checked in order):

1. **Gate activation** — `_get_activated_nodes`: node has no controlling gate, OR a gate has routed to it
2. **Startup predecessors** — `_startup_predecessors_satisfied`: all DATA+ORDERING predecessors have executed (CONTROL edges excluded — gate activation is handled separately)
3. **Inputs available** — `_has_all_inputs`: all input params exist in state, bound values, or defaults
4. **Wait-for satisfied** — `_wait_for_satisfied`: ordering-only deps exist and are fresh
5. **Needs execution** — `_needs_execution`: never executed, OR inputs changed (stale), OR a routing decision targets this node

After collecting ready nodes, two filters apply:
- **Gate blocking**: if a gate is ready, its targets are blocked this superstep (gate fires first)
- **Wait-for deferral**: on first execution, consumers are deferred if their wait-for producers are also ready

## Gate Activation Rules

`_get_activated_nodes` decides which gated nodes can be scheduled:

| Gate state | Node state | `default_open` | Entrypoints set | Result |
|-----------|-----------|----------------|-----------------|--------|
| Never executed | Never executed | True | No | **Activated** (first-pass startup) |
| Never executed | Never executed | True | Yes, node IS entrypoint | **Activated** |
| Never executed | Never executed | True | Yes, node NOT entrypoint | **Blocked** (wait for gate) |
| Never executed | Never executed | False | Any | **Blocked** |
| Executed, decision=this node | Any | Any | Any | **Activated** |
| Executed, decision=other | Any | Any | Any | **Blocked** |
| Executed, decision cleared (stale) | Any | Any | Any | **Blocked** |

**Key insight**: the entrypoint restriction prevents inputless gate targets (like interrupt nodes) from firing before the gate on first pass.

## Staleness (`_is_stale`)

A previously-executed node is stale if any input version changed since last execution.

Two optimization rules skip staleness for non-gated nodes:
1. **Sole Producer Rule** — skip if this node itself produces the param (prevents self-loop re-trigger)
2. **Descendant Producer Rule** (DAGs only) — skip if ALL producers are descendants (prevents downstream writes from triggering upstream re-execution)

Both rules are **disabled for gate-controlled nodes** — gates explicitly drive cycle re-execution.

**Routing as re-trigger**: `_has_pending_activation` checks if a routing decision targets this node. This is essential for inputless gate targets that would otherwise never be stale.

## Interrupt Lifecycle

InterruptNode execution has three paths:

1. **Resume path** (`is_resuming=True` + all outputs in `provided_values`): auto-resolve from provided values, pop consumed keys
2. **Handler path** (function returns non-None): normalize response to output dict
3. **Pause path** (function returns None): raise `PauseExecution` with `PauseInfo`

The `is_resuming` flag prevents false auto-resolve on fresh runs when provided_values happen to match interrupt output names.

## Common Pitfalls

- **Shared params + cycles**: a node that both consumes and produces `messages` triggers the Sole Producer Rule. Split into separate consume/produce nodes if the node is NOT gate-controlled.
- **Inputless gate targets**: without `_has_pending_activation`, they never re-fire after first execution because `_is_stale` iterates over zero inputs.
- **Control edges vs ordering edges**: control edges are excluded from `startup_predecessors`. If you need ordering, use `wait_for` or data edges. Gates handle control flow separately.
