# Visualization System Reference

This document captures the current visualization architecture, key invariants, and debugging workflow for the hypergraph viz stack.

## System Overview

**Python pipeline**
1. `Graph` → `to_flat_graph()`
2. `renderer.py` → React Flow nodes/edges + `meta` (includes `nodesByState`, `edgesByState`)
3. `html_generator.py` → HTML + JS assets + debug overlays

**JavaScript pipeline**
1. `app.js` builds expansion state + selects `nodesByState`/`edgesByState`
2. `layout.js` runs layout phases and routes edges
3. `constraint-layout.js` solves constraints and routes edge paths

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

Selection happens in `app.js` via `expansionStateToKey()` and lookups in `meta.nodesByState` / `meta.edgesByState`.

**Why**: Ensures expand/collapse produces identical edges whether the graph is initially rendered expanded or toggled interactively.

## Edge Computation Model

`renderer.py` generates edges for a given expansion state in two modes:

1. **Merged outputs** (`separate_outputs=False`)
   - Edges go function → function
   - Data nodes are hidden
   - Container edges re-route to internal consumers/producers when expanded

2. **Separate outputs** (`separate_outputs=True`)
   - Edges go function → DATA → consumer
   - Container DATA nodes are hidden when the container is expanded
   - Reroute logic must not override DATA node sources

3. **Ordering edges** (both modes)
   - Created by `emit`/`wait_for` declarations on nodes
   - `edge_type="ordering"` in NetworkX graph
   - Rendered with purple dashed style (`#8b5cf6`, `strokeDasharray: "6 3"`)
   - Suppressed when a data edge already exists between the same pair

**Rename handling**
- `with_inputs` / `with_outputs` can rename exposed parameters.
- `_find_internal_producer_for_output()` resolves container output names to internal producers when names differ.

## Input Grouping + Scope

External inputs are grouped by **consumer set** and **bound status**:
- Single param → `INPUT`
- Multiple params → `INPUT_GROUP` (stable ID: `input_group_<sorted_params>`)

**Scope rules**:
- If all consumers are inside one expanded container → set `ownerContainer`
- Otherwise keep at root
- `deepestOwnerContainer` is metadata for debugging

`layout.js` dynamically assigns `parentNode` for owned inputs so they appear inside expanded containers; Python controls visibility via precomputed `hidden` flags.

## Output Visibility

Container outputs are only shown when consumed externally:
- **Merged outputs**: filter container output list in `renderer.py`
- **Separate outputs**: suppress container DATA nodes if container is expanded

This prevents duplicate output nodes when a container is expanded.

## Layout Pipeline (layout.js)

The recursive layout is split into explicit phases:

1. **layoutChildrenPhase**: layout each expanded container’s children
2. **layoutRootPhase**: layout top-level nodes
3. **composePositionsPhase**: merge child and root layouts
4. **routeCrossBoundaryEdgesPhase**: attach edges that cross container boundaries
5. **applyEdgeReroutesPhase**: re-route cross-boundary edges using routing data
6. **validateEdgesPhase** (debug only): verify stem alignment to visible bounds

**Deep lift**: edges originating from deeply nested nodes are lifted to their direct child ancestor when laying out a container’s children (`deepToChild`). This ensures internal ordering uses the real edge structure.

## Routing Details (constraint-layout.js)

- **Stems**: 2-point vertical stems at node centers for entry/exit
- **Shoulder waypoint**: optional mid-point to create a natural fan-out curve
- **Corridor routing**: avoids obstacles with left/right corridors; guided by `naturalX`

Key config values live in `assets/constants.js` and are shared across layout and routing.

## Edge Styling + Routing Knobs (constants.js)

These are the main “shape” controls we’ve added and tuned.

- `EDGE_CURVE_STYLE`
  - `0` = straight polyline
  - `1` = smooth curve (B‑spline)
  - between `0..1` = gentler Catmull‑Rom curve
- `EDGE_ELBOW_RADIUS`
  - Only used for straight polylines (rounded elbows)
  - Example: `EDGE_CURVE_STYLE: 0`, `EDGE_ELBOW_RADIUS: 12`
- `EDGE_SHARP_TURN_ANGLE`
  - Only applies when `EDGE_CURVE_STYLE < 1`
  - Forces straight segments on sharp turns
- `EDGE_NONSTRAIGHT_WEIGHT`
  - Penalizes cumulative bend angles
  - Helps prefer “big angle then straight” over many small bends
- `EDGE_CURVE_WEIGHT`, `EDGE_TURN_WEIGHT`, `EDGE_LATERAL_WEIGHT`
  - Penalize extra X changes, left/right flips, and total sideways travel
- `EDGE_NODE_PENALTY`, `EDGE_NODE_CLEARANCE`
  - Penalize corridor segments passing over nodes
  - Expanded containers are excluded
- `EDGE_EDGE_PENALTY`, `EDGE_EDGE_CLEARANCE`
  - Penalize corridor segments crossing other edges
- `EDGE_SHARED_TARGET_SPACING_SCALE`
  - Scales separation between sources of the same target
- `EDGE_STRAIGHTEN_MAX_SHIFT`
  - Limits how far we can shift to keep a straight corridor

**Blending invariant**
- The final tail segment into a node is always straight (even for curved heads).
- This forces all incoming edges to share the same stem and visually merge.

## Edge Renderer Notes (components.js)

- Curves are drawn by default using B‑spline or Catmull‑Rom (based on `EDGE_CURVE_STYLE`).
- If `EDGE_CURVE_STYLE == 0` or sharp‑turn mode kicks in, edges are polylines.
- We detect the **final vertical tail** and draw it as straight lines to guarantee
  convergence, because B‑splines do not pass through intermediate points.

## Gallery Script (render_notebook_viz.py)

Generates a scrollable gallery of all notebook visualizations.

**Usage**
- `uv run python scripts/render_notebook_viz.py`
- Output: `outputs/viz_gallery/index.html`

**Options**
- `--no-open` disables auto‑open
- `--iframe-height 800` adjusts embedded preview height
- `--verbose` shows notebook output

Debug overlays are forced **off** in gallery renders.

## Node-Type Offsets and Visible Bounds

Different node types have different wrapper-to-visible gaps (shadows, padding). Offsets are defined in `constants.js` and applied in:
- `constraint-layout.js` (visible bottoms)
- `layout.js` (edge stem placement)
- `app.js` debug API (visible vs wrapper bounds)

**Invariant**: edge Y coordinates must target the **visible** bounds, not the React Flow wrapper.

## Viewport Centering

`html_generator.py` performs a post-layout DOM measurement to center content:
- Uses inner content bounds (not wrapper bounds)
- Centers in full viewport (not “available width”)
- Applies all corrections in a single viewport update

## Debug Surfaces

- **Debug API**: `window.__hypergraphVizDebug` and `window.__hypergraphVizReady`
- **Debug overlay**: set `window.__hypergraph_debug_viz = true` before render
- **Edge validation**: enabled when debug overlays are on (Step 6)

## Common Failure Modes

| Symptom | Likely Cause | Fix Location |
| --- | --- | --- |
| Edge points to container when expanded | edgesByState key mismatch or missing consumer mapping | `renderer.py` / `app.js` |
| Input appears outside expanded container | ownerContainer not set | `renderer.py` `_compute_input_scope()` |
| DATA node duplicated when expanded | container DATA not filtered | `renderer.py` |
| Separate outputs edge becomes function edge | Step 5 reroute clobbers DATA edge | `layout.js` `applyEdgeReroutesPhase` |
| Edge starts/ends with visible gap | wrong node-type offset | `constants.js` + layout/routing usage |
| Incoming edges don’t merge | curve doesn’t pass through convergence point | `components.js` tail‑straight logic |

## Test Coverage Pointers

- `tests/viz/test_scope_aware_visibility.py`
- `tests/viz/test_edges_by_state_contract.py`
- `tests/viz/test_edge_connections.py`
- `tests/viz/test_visual_layout_issues.py`

## File Map

- `src/hypergraph/viz/renderer.py`: edge computation, scoping, precomputed states
- `src/hypergraph/viz/assets/layout.js`: layout phases + reroute logic
- `src/hypergraph/viz/assets/constraint-layout.js`: constraint solver + routing
- `src/hypergraph/viz/assets/constants.js`: shared layout constants
- `src/hypergraph/viz/html_generator.py`: HTML + centering + debug overlays
