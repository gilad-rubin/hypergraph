# Daft Runner Agent Guide

Daft execution should preserve the graph surface that users selected before the
DataFrame plan is built.

## Active Scope

- Build execution plans from the active graph scope: selected outputs,
  configured entrypoints, and graph-level input validation. Do not topologically
  sort or validate against inactive nodes.
- Add tests for `select(...)`, configured entrypoints, and mapped inputs when
  changing plan construction.

## GraphNode Outputs

- Never rely on dictionary iteration to pick GraphNode outputs. Use explicit
  output keys and the graph-node output mapping so renamed and multi-output
  GraphNodes stay deterministic.
- When changing nested Daft execution, test original output names and renamed
  parent-facing output names together.
