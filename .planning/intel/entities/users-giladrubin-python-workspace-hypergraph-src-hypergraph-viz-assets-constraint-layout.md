---
path: /Users/giladrubin/python_workspace/hypergraph/src/hypergraph/viz/assets/constraint-layout.js
type: util
updated: 2026-01-21
status: active
---

# constraint-layout.js

## Purpose

A two-phase constraint-based layout engine for hypergraph visualization that positions nodes via constraint relaxation (using kiwi.js/Cassowary solver) and routes edges with collision avoidance. Produces clean, readable DAG layouts with proper edge routing for graph visualizations.

## Exports

None (IIFE pattern - registers layout algorithm globally or with visualization framework)

## Dependencies

- kiwi.js (external, loaded via `window.kiwi` - Cassowary constraint solver)

## Used By

TBD

## Notes

- Uses IIFE pattern (`(function() { 'use strict'; ... })()`) for encapsulation
- Relies on kiwi.js being loaded before this script (accesses `window.kiwi`)
- Supports both vertical and horizontal orientations for layout
- Contains utility functions for geometry calculations: `clamp`, `snap`, `distance1d`, `angle`, `nearestOnLine`
- Node positioning helpers: `nodeLeft`, `nodeRight`, `nodeTop`, `nodeBottom`
- `groupByRow` organizes nodes into rows based on orientation for layered layout
- `offsetNode` and `offsetEdge` handle coordinate transformations