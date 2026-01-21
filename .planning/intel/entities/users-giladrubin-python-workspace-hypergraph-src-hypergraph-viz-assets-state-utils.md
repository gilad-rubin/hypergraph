---
path: /Users/giladrubin/python_workspace/hypergraph/src/hypergraph/viz/assets/state_utils.js
type: util
updated: 2026-01-21
status: active
---

# state_utils.js

## Purpose

State management utilities for Hypergraph visualization. Handles node state transformations and visibility based on expansion/toggle states, controlling how pipeline nodes, data nodes, and input groups are displayed.

## Exports

- `HypergraphVizState` (global) - API object exposed via UMD pattern containing:
  - `applyState(baseNodes, baseEdges, options)` - Transforms nodes/edges based on expansion state, separateOutputs toggle, showTypes toggle, and theme

## Dependencies

None

## Used By

TBD

## Notes

- Uses UMD pattern for browser compatibility (attaches to `window.HypergraphVizState`)
- Handles two display modes: embedded outputs (outputs shown within function nodes) vs separate outputs (DATA nodes shown separately)
- Manages pipeline node expansion states via a Map or plain object
- Filters edges based on visibility of connected nodes when outputs are embedded