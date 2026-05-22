# Runners Agent Guide

Execution changes must preserve sync/async behavioral parity unless a test names
the intentional difference.

## Run Status Semantics

- Top-level run and map statuses are user-facing behavior. Treat precedence
  changes among `FAILED`, `PARTIAL`, `PAUSED`, `STOPPED`, and `SUCCESS` as
  maintainer decisions unless the existing contract is clearly contradicted by
  implementation.
- Boolean helpers such as `completed`, `stopped`, and `failed` must match the
  exact aggregate semantics they advertise. If mixed terminal outcomes are
  possible, preserve a detailed path for callers to inspect per-item results
  instead of hiding the mix behind a broad status.

## Error Attribution

- Step records should describe nodes that actually started or were otherwise
  attributable to the failure. Do not turn a whole ready batch into failed
  steps just because a generic superstep exception escaped.
- If a failure cannot be attributed to a specific node, prefer a run-level
  failure with no per-node failed step over invented node failures.
- When wrapping errors, avoid self-referential exception causes. If an inbound
  exception is already the wrapper type, re-raise it directly.

## Checkpointed Resume

- Runtime resume values are not the same thing as restored `state.values`.
  Persisted state may contain ordinary outputs whose names match interrupt
  outputs. Use an explicit resume signal/field, not output-name presence alone,
  when deciding whether to relax missing-input checks.
- When adding state fields that affect scheduling or resume, update copy and
  checkpoint-restore paths together.

## Validation

- Add focused sync and async tests for runner behavior that touches scheduling,
  checkpoint records, pause/resume, stop, or error handling.
- For checkpoint-visible failures, assert through the checkpointer/public run
  result rather than only asserting an internal exception shape.
