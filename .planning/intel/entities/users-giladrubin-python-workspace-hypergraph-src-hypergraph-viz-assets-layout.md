---
path: /Users/giladrubin/python_workspace/hypergraph/src/hypergraph/viz/assets/layout.js
type: hook
updated: 2026-01-21
status: active
---

# layout.js

## Purpose

Provides a React hook (`useHypergraphLayout`) for constraint-based node positioning in Hypergraph visualizations. Calculates node dimensions based on type (DATA, INPUT, FUNCTION, BRANCH, etc.) and recursively lays out nested graphs.

## Exports

- `useHypergraphLayout` - React hook that computes positioned nodes and edges using ConstraintLayout algorithm
- `calculateDimensions` - Helper function to compute width/height for a node based on its type and content

## Dependencies

- React (global)
- ConstraintLayout (global)

## Used By

TBD

## Notes

- Uses UMD pattern, exposing `HypergraphVizLayout` on the window object
- Layout constants define sizing: `MAX_NODE_WIDTH` (280px), `GRAPH_PADDING` (40px), `HEADER_HEIGHT` (32px)
- Supports recursive layout for nested GraphNodes via `layoutNestedGraph` function
- Character width estimation uses fixed 7px per character for label sizing
- Handles multiple node types: DATA, INPUT, INPUT_GROUP, BRANCH, FUNCTION, PIPELINE