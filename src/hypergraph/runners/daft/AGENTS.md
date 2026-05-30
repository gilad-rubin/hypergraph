# Daft Runner Agent Guide

Daft execution should preserve the graph surface that users selected before the
DataFrame plan is built.

## Active Scope

- Build execution plans from the active graph scope: selected outputs,
  configured entrypoints, and graph-level input validation. Do not topologically
  sort or validate against inactive nodes.
- Add tests for `select(...)`, configured entrypoints, and mapped inputs when
  changing plan construction.

## Integration Boundary

- Document user-facing Daft APIs through `hypergraph.integrations.daft`, not
  `hypergraph.runners.daft`.
- Keep typed Daft metadata in focused private modules such as `_options.py` and
  `_stateful.py`; do not let `operations.py` accumulate public decorators or
  option-merging policy.
- When adding a Daft lowering option, update the typed option model, operation
  validation, docs/examples, and at least one runtime test together.
- `daft.stateful`/`daft_node` expose only `daft.cls`/`daft.func` placement
  controls as flat kwargs (no public `Options` object). Resource lifecycle
  (`resource`/`close`/`aclose`) stays on core `@stateful` + Sync/Async; Daft has
  no deterministic teardown hook, so DaftRunner rejects any `resource=True`
  stateful. Do not re-add lifecycle args to the Daft decorators.

## GraphNode Outputs

- Never rely on dictionary iteration to pick GraphNode outputs. Use explicit
  output keys and the graph-node output mapping so renamed and multi-output
  GraphNodes stay deterministic.
- When changing nested Daft execution, test original output names and renamed
  parent-facing output names together.
