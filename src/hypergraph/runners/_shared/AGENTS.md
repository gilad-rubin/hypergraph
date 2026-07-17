# Runners Shared — Agent Guide

The scheduling and state engine. Read this before modifying the focused
`scheduling.py`, `readiness.py`, `value_resolution.py`, `state_restore.py`,
`outputs.py`, `map_inputs.py`, `results.py`, `state.py`, or `template_*.py`
modules. Treat `types.py` as a compatibility re-export surface only.

## Node Readiness (`get_ready_nodes`)

A node is ready when ALL of these pass (checked in order):

1. **Gate activation** — `_get_activated_nodes`: node has no controlling gate, OR a gate has routed to it
2. **Startup predecessors** — `_startup_predecessors_satisfied`: all DATA+ORDERING predecessors have executed (CONTROL edges excluded — gate activation is handled separately)
3. **Inputs available** — `has_all_inputs`: all input params exist in state, bound values, or defaults
4. **Wait-for satisfied** — `_wait_for_satisfied`: ordering-only deps exist and are fresh
5. **Needs execution** — `_needs_execution`: never executed, OR inputs changed (stale), OR a routing decision targets this node

After collecting ready nodes, two filters apply:
- **Gate blocking**: if a gate is ready, its targets are blocked this superstep (gate fires first)
- **Wait-for deferral**: on first execution, consumers are deferred if their wait-for producers are also ready

## Gate Activation Rules

`_get_activated_nodes` decides which gated nodes can be scheduled:

| Gate state | Node state | `default_open` | Entrypoints set | Result |
|-----------|-----------|----------------|-----------------|--------|
| Never executed, gate itself activated | Never executed | True | No | **Activated** (first-pass startup) |
| Never executed, gate itself blocked | Never executed | True | No | **Blocked** (transitive chain termination) |
| Never executed | Never executed | True | Yes, node IS entrypoint | **Activated** |
| Never executed | Never executed | True | Yes, node NOT entrypoint | **Blocked** (wait for gate) |
| Never executed | Never executed | False | Any | **Blocked** |
| Executed, live decision=this node | Any | Any | Any | **Activated** |
| Executed, orphaned decision (gate cut off upstream) | Any | Any | Any | **Blocked** (non-END decision dropped) |
| Executed, pending decision but a controller upstream is re-firing | Any | Any | Any | **Blocked** this superstep (suspended, decision kept) |
| Executed, decision=other | Any | Any | Any | **Blocked** |
| Executed, decision cleared (stale) | Any | Any | Any | **Blocked** |

**Key insight**: the entrypoint restriction prevents inputless gate targets (like interrupt nodes) from firing before the gate on first pass.

**Transitive chain termination** (issue #220): activation is a shrinking
fixpoint (worklist — linear predicate calls regardless of declaration order).
First-pass `default_open` permission requires the controlling gate itself to
still be activated, so `gate_a -> gate_b -> target` blocks `target` when
`gate_a` decides END or routes elsewhere — data readiness cannot bypass a dead
control path. Explicit entrypoint targets are exempt from the transitive
requirement: the user asked to start there, and the controlling gate may be
outside the active scope.

**Orphaned pending decisions** (issue #220, review finding): a pending
decision is causally live only while its deciding gate is not *cut off* — a
gate is cut off when every controlling gate either currently holds an explicit
decision that excludes it (END or routed elsewhere) or is itself cut off.
Cut-off gates' pending non-END decisions are DELETED before activation
(`_drop_orphaned_decisions`), so a half-consumed multi-target selection cannot
fire targets after the chain was explicitly terminated upstream. The
distinguishing signal that keeps cycles working: a controller whose selection
was merely CONSUMED (decision None) keeps its targets' pending decisions live —
it may re-fire and route to them again. Deletion (not suppression) prevents an
orphaned decision from resurrecting after the upstream exclusion is consumed.
END decisions are never deleted: they activate nothing and stay as terminal
markers.

**Gate-first re-evaluation** (issue #220, review round 2): when a controlling
gate is scheduled to re-execute — the same `_needs_execution` signal that
clears the gate's own stale decision — gates below it in the control chain
are transiently *suspended* (`_compute_suspended_gates`): their pending
decisions do not activate targets this superstep. The re-firing gate delivers
its verdict first; consequences propagate on the next evaluation (a
re-selection re-runs the mid-gate and refreshes its decision; an exclusion
orphans it). Suspension is recomputed from state every evaluation — nothing
is persisted, decisions are kept, and it lifts the moment the controller has
re-fired. It distinguishes "controller decision None because harmlessly
consumed" (downstream decisions stay live) from "controller decision None and
controller is stale" (verdict pending). Independent nodes outside the
re-firing gate's chain — including ungated nodes co-batched in the same
superstep — are unaffected.

## Staleness (`_is_stale`)

A previously-executed node is stale if any input version changed since last execution.

Two optimization rules skip staleness for non-gated nodes:
1. **Sole Producer Rule** — skip if this node itself produces the param (prevents self-loop re-trigger)
2. **Descendant Producer Rule** (DAGs only) — skip if ALL producers are descendants (prevents downstream writes from triggering upstream re-execution)

Both rules are **disabled for gate-controlled nodes** — gates explicitly drive cycle re-execution.

**Routing as re-trigger**: `_has_pending_activation` checks if a routing decision targets this node. This is essential for inputless gate targets that would otherwise never be stale.

## Interrupt Lifecycle

InterruptNode execution has two paths and one loud error:

1. **Answer-supplied path** (`is_resuming=True` + the answer port in `provided_values`): pass the answer through as the node output and pop the consumed key
2. **Question path**: call the handler, validate its structural question payload, and raise `PauseExecution` with `PauseInfo(value=question, response_key=answer_name)`
3. **Invalid question**: a `None` return or malformed payload raises loudly; handler returns never become dataflow outputs

With a checkpointer, `is_resuming` prevents fresh-run values from masquerading
as a resume payload. Without a checkpointer it is intentionally true so an
answer supplied up front supports headless/CSV/batch execution.

## Resume Payload State

`GraphState.values` can contain restored checkpoint state and current-run
inputs. Do not infer "this is an interrupt resume" from the presence of a
GraphNode or InterruptNode output name in `state.values`; normal persisted
outputs can have the same shape. Use explicit runtime resume metadata, and
carry new `GraphState` fields through `copy()` and checkpoint initialization.

## Aggregate Status

Shared status helpers in this package define public runner semantics. When
touching `RunStatus`, `MapResult`, or batch summaries, update every consumer
that derives the same status and add tests for mixed outcomes such as
completed+failed, completed+stopped, paused+failed, and empty batches.

## GraphNode Boundary Addressing (`address_for_node_input`)

A `GraphNode`'s inputs are already projected to the parent-facing address:
flat by default, or `"<node.name>.<param>"` when the node was created with
`as_node(namespaced=True)` unless that port was exposed back to a flat name.

State, provided values, and bound values all use this canonical form, so any
code that looks up a node-input value should use that canonical address instead
of guessing a bare or prefixed name:

```python
from hypergraph.runners._shared.value_resolution import address_for_node_input

addr = address_for_node_input(node, param)
if addr in state.values:
    ...
```

The helper is intentionally thin after boundary projection: `node.inputs`
already contains the resolved parent-facing address, so the helper currently
returns `param`. It remains useful as a readable assertion at record/read sites.

**Sites that already use it (canonical examples):**
- `has_input` (readiness check, `value_resolution.py`)
- `get_value_source` (PROVIDED + BOUND + EDGE branches, `value_resolution.py`)
- `_is_stale` (read side, `readiness.py`)
- sync/async `superstep.py` (recording `input_versions` under the same key the
  staleness check reads — record-key MUST equal read-key)
- `runners/daft/operations.py` (DataFrame column naming)

**Trap:** local-name lookups (`state.values[param]`, `param in graph.inputs.bound`,
`param in provided_values`) silently miss namespaced or exposed parent-facing
addresses. Both sides of a record/read pair must agree on the address —
otherwise consumed-version defaults to 0 on read while being recorded as 0,
and "not stale" silently classifies as fresh forever. PR #95 had exactly this
bug at the staleness path (see commit `a3d3277` for the fix).

If you find yourself trying to pass a namespace dict into the helper, the
caller is probably carrying an old pre-projection assumption.

## Common Pitfalls

- **Shared params + cycles**: a node that both consumes and produces `messages` triggers the Sole Producer Rule. Split into separate consume/produce nodes if the node is NOT gate-controlled.
- **Inputless gate targets**: without `_has_pending_activation`, they never re-fire after first execution because `_is_stale` iterates over zero inputs.
- **Control edges vs ordering edges**: control edges are excluded from `startup_predecessors`. If you need ordering, use `wait_for` or data edges. Gates handle control flow separately.
- **Local-name input lookups**: see "GraphNode Boundary Addressing" above. If you're touching `value_resolution.py` / `superstep.py` / an executor and writing `state.values[param]` or `param in node_bound`, route through `address_for_node_input` instead.
