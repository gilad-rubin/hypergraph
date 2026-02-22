# Visualization System Reference

This document captures the current visualization architecture, key invariants, and debugging workflow for the hypergraph viz stack.

## System Overview

**Python pipeline**
1. `Graph` → `to_flat_graph()`
2. `renderer/` → React Flow nodes/edges + `meta` (includes `nodesByState`, `edgesByState`)
3. `html/generator.py` → HTML + JS assets

**JavaScript pipeline** (single file: `assets/viz.js`)
1. Section 7 (App) builds expansion state + selects `nodesByState`/`edgesByState`
2. `layoutGraph()` runs dagre for node positioning + native edge routing
3. `performRecursiveLayout()` handles expanded containers (recursive dagre passes)
4. `addConvergenceStems()` inserts merge/diverge points for shared endpoints
5. `CustomEdge` renders B-spline curves via `curveBasis()`

## Single-File Architecture (viz.js)

All JS is in one file (`assets/viz.js`) organized in 7 sections:
1. **Constants + Helpers** — layout constants, node-type offsets
2. **Theme** — host theme detection, light/dark switching
3. **Layout** — `layoutGraph()`, `performRecursiveLayout()`, feedback edge routing
4. **Edge Component** — `curveBasis()`, `CustomEdge`, label placement
5. **Node Components** — `CustomNode` for all node types
6. **Controls** — zoom/fit/toggle buttons, `DevEdgeControls` (DialKit)
7. **App + Init** — state management, `useLayout` hook, rendering

## Edge Routing Modes

Controlled by `EDGE_CONVERGE_TO_CENTER` flag (default: `true`):

**Center mode** (`convergeToCenter=true`):
- All edge endpoints forced to node center-x
- `addConvergenceStems()` inserts V-shape merge points for targets with 2+ incoming edges
- `EDGE_CONVERGENCE_OFFSET` controls stem height (default: 20px)

**Dagre mode** (`convergeToCenter=false`):
- Edge endpoints use dagre's native x-positions (spread across node width)
- Endpoints clamped within padded region: `EDGE_ENDPOINT_PADDING` (default: 16px)
- No convergence stems needed

**BRANCH/END exception**: Always use center-x regardless of mode (diamond has single exit point at bottom vertex).

## Node Types and Mapping

- Flat graph containers: `node_type == "GRAPH"`
- React Flow mapping: `GRAPH` → `PIPELINE`
- Synthetic nodes: `INPUT`, `INPUT_GROUP`, `DATA`

**Invariant**: Container detection must use `node_type == "GRAPH"` in Python and `nodeType == "PIPELINE"` in JS.

## Expansion State + Precomputed State

Nodes and edges are **precomputed** for all valid expansion states (and both `separate_outputs` modes).

Key format:
- `"nodeId:0|sep:0"` (collapsed, merged outputs)
- `"nodeId:1|sep:1"` (expanded, separate outputs)
- `"sep:0"` / `"sep:1"` (no expandable nodes)

Selection happens in App via `expansionStateToKey()` and lookups in `meta.nodesByState` / `meta.edgesByState`.

## Edge Computation Model

`renderer/` generates edges for a given expansion state in two modes:

1. **Merged outputs** (`separate_outputs=False`)
   - Edges go function → function
   - Data nodes are hidden

2. **Separate outputs** (`separate_outputs=True`)
   - Edges go function → DATA → consumer
   - Container DATA nodes hidden when expanded

3. **Ordering edges** (both modes)
   - Created by `emit`/`wait_for` declarations
   - `edge_type="ordering"` in NetworkX graph
   - Rendered with dashed style

## Input Grouping + Scope

External inputs are grouped by **consumer set** and **bound status**:
- Single param → `INPUT`
- Multiple params → `INPUT_GROUP` (stable ID: `input_group_<sorted_params>`)

## Node-Type Offsets and Visible Bounds

Offsets defined in viz.js Section 1:
- `NODE_TYPE_OFFSETS` — bottom gap (shadow/padding) per node type
- `NODE_TYPE_TOP_INSETS` — top gap per node type

**Invariant**: edge Y coordinates must target the **visible** bounds, not the React Flow wrapper.

## Dev Controls (DialKit)

Dev-only controls visible when `window.__hypergraph_debug_viz = true`:
- Toggle: "Converge to center" (switches edge routing mode)
- Slider: "Stem height" (convergence offset, 0-60px)
- Slider: "Endpoint padding" (dagre mode only, 0-60px)

Gallery page (`scripts/render_notebook_viz.py`) has a DialKit bar that broadcasts settings to all iframes via `postMessage`. Viz.js listens for `{ type: 'hypergraph-set-options', options: {...} }` messages.

## Gallery Script (render_notebook_viz.py)

Generates a scrollable gallery of all notebook visualizations with DialKit controls.

**Usage**
- `uv run python scripts/render_notebook_viz.py`
- Output: `outputs/viz_gallery/index.html`

**Options**
- `--no-open` disables auto-open
- `--iframe-height 800` adjusts embedded preview height
- `--verbose` shows notebook output

## Common Failure Modes

| Symptom | Likely Cause | Fix Location |
| --- | --- | --- |
| Edge points to container when expanded | edgesByState key mismatch | `renderer/` |
| Input appears outside expanded container | ownerContainer not set | `renderer/` |
| Edge starts/ends with visible gap | wrong node-type offset | viz.js Section 1 |
| Incoming edges don't merge | convergence stem not inserted | `addConvergenceStems()` |
| Branch labels at wrong position | `outgoingMidpointDistance` heuristic | viz.js Section 4 |

## Test Coverage Pointers

- `tests/viz/test_scope_aware_visibility.py`
- `tests/viz/test_edges_by_state_contract.py`
- `tests/viz/test_edge_connections.py`
- `tests/viz/test_visual_layout_issues.py`

## File Map

- `src/hypergraph/viz/renderer/` — edge computation, scoping, precomputed states
- `src/hypergraph/viz/assets/viz.js` — single-file JS app (layout, rendering, controls)
- `src/hypergraph/viz/html/generator.py` — HTML assembly with embedded assets
- `src/hypergraph/viz/html/estimator.py` — iframe dimension estimation
- `src/hypergraph/viz/renderer/instructions.py` — VizInstructions data contract
- `scripts/render_notebook_viz.py` — gallery generator with DialKit controls
