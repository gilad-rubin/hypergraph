# Visualization System Reference

This document captures the current visualization architecture, key invariants, and debugging workflow for the hypergraph viz stack.

Cross-widget UX defaults live in: `dev/WIDGET-PREFERENCES.md`.

## System Overview (PR #88, Stage 1)

The viz stack is built around a **compact IR + twin scene_builders**: a single
``GraphIR`` describes pure-graph facts; one Python and one JS scene_builder
turn that IR + an expansion state into a React Flow scene. The legacy 2^N
``edgesByState``/``nodesByState`` precompute is gone — clicks re-derive the
scene client-side without a kernel round-trip.

**Python pipeline**
1. `Graph` → `to_flat_graph()`
2. `renderer/ir_builder.py:build_graph_ir(flat_graph)` → `GraphIR`
   (pure facts: nodes, edges, expandable_nodes, external_inputs,
   configured_entrypoints, graph_output_visibility)
3. `scene_builder.py:build_initial_scene(ir, expansion_state, ...)` → React
   Flow `{nodes, edges}` for the initial render. Same function powers
   the test oracle.
4. `renderer/__init__.py:render_graph` is now a thin wrapper that ships
   the IR + initial scene to `html/generator.py`.

**JavaScript pipeline** (`assets/viz.js` + `assets/scene_builder.js`)
1. `scene_builder.js:buildInitialScene` mirrors the Python twin — same IR,
   same expansion state, semantically equivalent output.
2. Section 7 (App in `viz.js`) calls `buildInitialScene` on every
   expansion / separateOutputs / showInputs change.
3. `layoutGraph()` runs dagre for node positioning + native edge routing.
4. `performCompoundLayout()` handles expanded containers with a compound dagre pass.
5. `CustomEdge` renders B-spline curves through dagre-provided points via `curveBasis()`.

**Mermaid**: `mermaid.py` still consumes `renderer/nodes.py` +
`renderer/scope.py` helpers rather than the compact IR path. Keep Mermaid and
interactive viz aligned on resolved port addresses even though the rendering
pipelines differ.

## Viz.js Architecture

`assets/viz.js` is organized in 7 sections:
1. **Constants + Helpers** — layout constants, node-type offsets
2. **Theme** — host theme detection, light/dark switching
3. **Layout** — `layoutGraph()`, `performCompoundLayout()`, feedback edge routing
4. **Edge Component** — `curveBasis()`, `CustomEdge`, label placement
5. **Node Components** — `CustomNode` for all node types
6. **Controls** — zoom/fit/toggle buttons, `DevLayoutControls` (DialKit)
7. **App + Init** — state management, `useLayout` hook, rendering

## Edge Routing

- Edge endpoints use dagre's native x-positions (spread across node width)
- Endpoints clamped within padded region: `EDGE_ENDPOINT_PADDING` (default: 0.25, fraction of node width)
- Do not add Hypergraph-side merge stems or synthetic routed paths; dagre owns edge routing.

**BRANCH/END exception**: Always use center-x regardless of mode (diamond has single exit point at bottom vertex).

## Node Types and Mapping

- Flat graph containers: `node_type == "GRAPH"`
- React Flow mapping: `GRAPH` → `PIPELINE`
- Synthetic nodes: `INPUT`, `INPUT_GROUP`, `DATA`

**Invariant**: Container detection must use `node_type == "GRAPH"` in Python and `nodeType == "PIPELINE"` in JS.

## Expansion State (IR-driven)

There is no precomputed state table. The scene for a given
``(expansion_state, separate_outputs, show_inputs, show_bounded_inputs)``
tuple is derived on demand by ``scene_builder`` from the IR.

In tests, use the ``scene_for_state(graph, expansion_state=..., ...)``
helper from ``tests/viz/conftest.py``. In the browser, App calls
``HypergraphSceneBuilder.buildInitialScene`` from ``assets/scene_builder.js``.

The IR carries all expansion-rewriting information eagerly:
- ``IREdge.source_when_expanded`` / ``target_when_expanded`` re-route
  edges to the deepest internal producer/consumer when a container is expanded.
- ``IREdge.is_back_edge`` marks DFS back-edges so feedback routing survives
  arbitrary expansion changes.
- ``IRNode.outputs[i].internal_only`` flags outputs whose consumers all
  live in the same container — used to filter ``data.outputs`` on collapsed
  GRAPH containers and to drive ``data.internalOnly`` styling on DATA nodes.

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

4. **Exclusive (mutex) data edges** (both modes)
   - When two producers in different branches of an exclusive gate
     (`@ifelse`, or `@route` with `multi_target=False`) feed the same input,
     each contributing edge is tagged with `data.exclusive=True`.
   - Detection lives in `viz/_common.py::compute_exclusive_data_edges` and
     reads gate `branch_data` off the flat graph.
   - React Flow renders them with `strokeDasharray='4 4'`; Mermaid uses the
     dotted arrow (`-.->`).

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
- Slider: "Endpoint padding" (0–0.45 as a fraction of node width; overrides `EDGE_ENDPOINT_PADDING` default of 0.25)
- Slider: "Vertical gap" (dagre rank separation)

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
| Edge points to container when expanded | `target_when_expanded` not populated in IR | `renderer/ir_builder.py` |
| Input appears outside expanded container | `ownerContainer` not derived from `deepest_owner` | `scene_builder.py` (Python + JS) |
| Edge starts/ends with visible gap | wrong node-type offset | viz.js Section 1 |
| Incoming edges overlap unexpectedly | dagre route or endpoint padding needs inspection | `assets/viz.js` |
| Branch labels at wrong position | `outgoingMidpointDistance` heuristic | viz.js Section 4 |
| Python and JS scene differ | scene_builder.py / scene_builder.js out of sync | tests/viz/test_scene_builder.py + Stage 3 parity |

## Test Coverage Pointers

- `tests/viz/test_scope_aware_visibility.py`
- `tests/viz/test_edges_by_state_contract.py`
- `tests/viz/test_edge_connections.py`
- `tests/viz/test_visual_layout_issues.py`

## File Map

- `src/hypergraph/viz/ir_schema.py` — `GraphIR` / `IRNode` / `IREdge` / `IRExternalInput` dataclasses
- `src/hypergraph/viz/renderer/ir_builder.py` — `build_graph_ir(flat_graph)`
- `src/hypergraph/viz/scene_builder.py` — Python scene builder (test oracle + initial-render path)
- `src/hypergraph/viz/assets/scene_builder.js` — JS twin
- `src/hypergraph/viz/renderer/__init__.py` — `render_graph()` thin wrapper (IR + initial scene)
- `src/hypergraph/viz/renderer/nodes.py` + `edges.py` + `scope.py` — legacy helpers retained for `mermaid.py`
- `src/hypergraph/viz/assets/viz.js` — single-file JS app (layout, rendering, controls)
- `src/hypergraph/viz/html/generator.py` — HTML assembly with embedded assets
- `src/hypergraph/viz/html/estimator.py` — iframe dimension estimation
- `scripts/render_notebook_viz.py` — gallery generator with DialKit controls
