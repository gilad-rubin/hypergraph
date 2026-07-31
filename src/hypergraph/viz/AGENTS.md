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
   configured_entrypoints, graph_output_visibility, container_entrypoints)
3. `widget.py:render_flat_graph` embeds the IR + initial expansion/options in
   HTML with empty top-level `nodes`/`edges`; the browser derives the scene.
4. `scene_builder.py:build_initial_scene(ir, expansion_state, ...)` is the
   Python test oracle and powers `renderer/__init__.py:render_graph`; that
   compatibility helper is not the widget payload path.

**JavaScript pipeline** (split `assets/*.js` modules — see "JS Asset Modules")
1. `scene_builder.js:buildInitialScene` mirrors the Python twin — same IR,
   same expansion state, semantically equivalent output.
2. The App (`viz.js`) calls `buildInitialScene` on every
   expansion / separateOutputs / showInputs change.
3. `layoutGraph()` (`viz_layout.js`) runs dagre for node positioning + native edge routing.
4. `performCompoundLayout()` (`viz_layout.js`) handles expanded containers with a compound dagre pass.
5. `CustomEdge` (`viz_edges.js`) renders B-spline curves through dagre-provided points via `curveBasis()`.

**Mermaid**: `mermaid.py` still consumes `renderer/nodes.py` +
`renderer/scope.py` helpers rather than the compact IR path. Keep Mermaid and
interactive viz aligned on resolved port addresses even though the rendering
pipelines differ.

## Cross-Language Invariants

- Scene derivation changes usually have Python and JavaScript twins. Update
  `scene_builder.py`, `assets/scene_builder.js`, and derivation helpers
  together unless the difference is intentionally documented in a test.
- Container entrypoints have ONE derivation authority (locked decision D14,
  #211): `renderer/scope.py:compute_container_entrypoints`, stamped on
  `GraphIR.container_entrypoints` by the IR builder. Semantics are
  self-EXCLUSIVE — a child is compared only with outputs owned by *other*
  children, so a self-loop never disqualifies it, and multiple independent
  entrypoints are preserved (cyclic containers fall back to the first child).
  Scene builders (Python and JS) and the Mermaid exporter consume it; never
  re-derive entrypoints from node inputs/outputs.
- Treat unordered semantic fields as unordered in parity tests. Sort or
  normalize fields such as target sets before comparing Python and JS output.

## Offline Assets and Controls

- Interactive visualization must remain fully offline. When adding or splitting
  first-party JavaScript assets, embed them through the asset manifest and
  update `FIRST_PARTY_ASSET_NAMES` plus module smoke tests.
- Icon-only controls need accessible names and keyboard behavior. Use tooltip
  text or an explicit aria label, expose tooltips to focus as well as hover, and
  let Escape hide transient tooltip UI.
- Mermaid id sanitization should normalize before reserved-word lookup, and
  tests should cover mixed-case reserved words when the reserved set changes.

## JS Asset Modules

The former single-file `viz.js` is split into no-build modules loaded by
side-effect in the order defined by `FIRST_PARTY_ASSET_NAMES`
(`assets/__init__.py`). Each attaches its API to a `window` global
(`HypergraphDerivation`, `HypergraphSceneBuilder`, `HypergraphViz*`):

1. `derivation.js` — pure graph-walk primitives over GraphIR + expansion
   state (visibility, expansion-aware routing, container-entrypoint lookup
   from the canonical `ir.container_entrypoints` field); no React Flow,
   layout, or styling knowledge
2. `scene_builder.js` — JS twin of `scene_builder.py`; consumes `derivation.js`
3. `viz_runtime.js` — shared constants + helpers: `NODE_TYPE_OFFSETS`,
   `NODE_TYPE_TOP_INSETS`, `EDGE_ENDPOINT_PADDING`, node-type resolution,
   theme detection
4. `viz_layout.js` — `layoutGraph()`, `performCompoundLayout()`, feedback
   edge routing, the `useLayout` hook
5. `viz_edges.js` — `curveBasis()`, `CustomEdge`, label placement
6. `viz_nodes.js` — `CustomNode` components for all node types
7. `viz_controls.js` — zoom/fit/toggle buttons, `DevLayoutControls` (DialKit)
8. `viz_debug.js` — `installDebugApi()` → `window.__hypergraphVizDebug`
9. `viz.js` — App bootstrap: state management, scene refresh, theme wiring,
   `hypergraph-set-options` message listener

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
  An identity-mode fan-out edge (``HyperTable.visualize``) re-routes instead to
  the mapped item's field INPUT pill(s) — ``segment_pages ──pages──▶ [page_text]
  ──▶ embed_page`` — so ``target_when_expanded`` may be a tuple of pill ids, and
  each such pill is flagged ``IRExternalInput.map_fed`` (styled distinctly, not
  as a free-floating external input). Falls back to the container entrypoint
  when the mapped item has no matching field (e.g. ``list[str]``).
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

4. **Transitive reduction** (`simplify`, default ON, both modes)
   - `viz/_simplify.py::shortcut_edge_keys` is the ONE authority for "is this
     edge implied by a longer path?". Three consumers share it: `scene_builder.py`,
     `assets/scene_builder.js` (twin `simplifyTransitiveEdges`), `mermaid.py`.
     Never re-derive the reachability walk in a new call site.
   - Two invariants make it safe to default on. Both are load-bearing; a change
     that widens either silently deletes real information:
     - Only **plain data** edges are removal candidates. Control, ordering,
       input, output, start/end and mutex (`exclusive`) edges never get dropped.
     - The path graph is the **unconditional data-flow spine only** (`data` +
       `output`, plus inert `input`). Only an edge that *always* carries a value
       may justify a removal, because the reader must be able to trust the
       surviving path. Two exclusions, same reasoning one level apart:
       control/ordering (`gate ⇢ target` means "may run", so standing in for
       `producer → target` leaves the consumer with no visible data source) and
       `exclusive` arms (an arm carries its value only on its branch, so it must
       not hide an unconditional edge that is the sole route on the other
       branch). Being a non-candidate is NOT enough — an edge barred from
       removal must also be barred from the path graph unless it is
       unconditional. `tests/test_frozen_baselines` (`gated.mmd`) guards the
       control case; `test_simplify_edges.py` guards the exclusive case in both
       twins.
   - Back edges are excluded from the path graph, or the reduction eats cycles.
     Mermaid has no `is_back_edge` field of its own; it calls
     `renderer/scope.py::find_back_edges` (also the source of
     `IREdge.is_back_edge`) to get the same answer.
   - Runs on the **assembled scene**, after expansion rewriting: an edge that is
     a shortcut while a container is collapsed can be the only path once it
     expands. Hidden edges are neither path segments nor candidates.
   - A **collapsed container is never assumed to pass values through.** Drawn as
     one box it invites joining every in-edge to every out-edge, which is false
     whenever the container does two unrelated jobs, and then a real edge gets
     hidden behind a route that does not exist. Each collapsed container is
     therefore split into per-port path nodes, joined only for the
     `[entry, exit]` pairs `ir_builder.py::compute_container_transits` (stamped on
     `GraphIR.container_transits`) says it really carries. Ports come from
     `IREdge.*_when_expanded` in the scene builders and
     `ir_builder.resolve_boundary_ports` in Mermaid — both normalized to the
     container's **direct child**, since transits are recorded between direct
     children and `*_when_expanded` names the deepest node. An unresolvable port
     becomes a dead end, never a pass-through: unverified means "do not hide".
     Unresolvable covers an absent `*_when_expanded`, a tuple fan-out, AND an
     exit with several deepest producers — no single child definitely emits the
     value, so asserting one would assert a route that may not run.
   - The unconditional-path rule applies **inside** a container as well: the
     transit walk uses only unconditional inner `data` edges, excluding control
     edges and mutex arms. A conditional internal route must not make the box
     look like a pass-through and license hiding an unconditional edge outside
     it. This is the same rule as for the top-level path graph, and it has now
     been missed three times (control edges, then mutex arms, then inside
     containers) — when adding any new conditional edge kind, check every place
     a path is built, not just the obvious one.
   - JS-only hazard: node ids come from user-authored Python names, and every
     `Object.prototype` member name (`__proto__`, `constructor`, `toString`, …)
     is a legal Python identifier. Every id-keyed map in `assets/*.js` must
     therefore be `Object.create(null)`, and any map arriving from `JSON.parse`
     (e.g. `ir.container_transits`) must be re-keyed into one before lookup.
     Two distinct failures, so test both: writing `map['__proto__']` on a plain
     `{}` throws and blanks the canvas, while *reading* a missing key such as
     `map['constructor']` silently returns a function that then gets used as
     data. The Python twin uses dicts and cannot fail either way, so shared
     parity fixtures will never catch it — test the JS side directly.

5. **Exclusive (mutex) data edges** (both modes)
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

Offsets defined in `assets/viz_runtime.js`:
- `NODE_TYPE_OFFSETS` — bottom gap (shadow/padding) per node type
- `NODE_TYPE_TOP_INSETS` — top gap per node type

**Invariant**: edge Y coordinates must target the **visible** bounds, not the React Flow wrapper.

## Dev Controls (DialKit)

Dev-only controls visible when `window.__hypergraph_debug_viz = true`:
- Slider: "Endpoint padding" (0–0.45 as a fraction of node width; overrides `EDGE_ENDPOINT_PADDING` default of 0.25)
- Slider: "Vertical gap" (dagre rank separation)

Gallery page (`scripts/render_notebook_viz.py`) has a DialKit bar that broadcasts settings to all iframes via `postMessage`. The App (`viz.js`) listens for `{ type: 'hypergraph-set-options', options: {...} }` messages.

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
| Dagre "setting 'rank'" crash, blank canvas | edge incident to an *expanded* container (dagre compound parent) — usually a renamed boundary param (`map_over`/`rename_inputs`/`rename_outputs`) not translated via the GRAPH node's `input_name_map`/`output_name_map` | `renderer/ir_builder.py` + `renderer/scope.py:get_deepest_consumers` |
| Input appears outside expanded container | `ownerContainer` not derived from `deepest_owner` | `scene_builder.py` (Python + JS) |
| Edge starts/ends with visible gap | wrong node-type offset | `assets/viz_runtime.js` (`NODE_TYPE_OFFSETS`) |
| Incoming edges overlap unexpectedly | dagre route or endpoint padding needs inspection | `assets/viz_layout.js` |
| Python and JS scene differ | scene_builder.py / scene_builder.js out of sync | tests/viz/test_scene_builder.py + tests/viz/test_parity.py |

## Test Coverage Pointers

- `tests/viz/test_scene_builder.py` — Python scene builder against the IR oracle
- `tests/viz/test_simplify_edges.py` — `simplify` reduction + Python/JS/Mermaid alignment
- `tests/viz/test_derivation_js.py` — drives `node` to run `derivation.js` directly
- `tests/viz/test_viz_modules_js.py` — module smoke tests for the split assets
- `tests/viz/test_scope_aware_visibility.py`
- `tests/viz/test_edge_connections.py`
- `tests/viz/test_visual_layout_issues.py`

## File Map

- `src/hypergraph/viz/ir_schema.py` — `GraphIR` / `IRNode` / `IREdge` / `IRExternalInput` dataclasses
- `src/hypergraph/viz/renderer/ir_builder.py` — `build_graph_ir(flat_graph)`
- `src/hypergraph/viz/scene_builder.py` — Python scene builder and test oracle
- `src/hypergraph/viz/_simplify.py` — `simplify` transitive-reduction authority
- `src/hypergraph/viz/assets/scene_builder.js` — JS twin
- `src/hypergraph/viz/renderer/__init__.py` — explicit Python scene + metadata compatibility helper
- `src/hypergraph/viz/widget.py` — compact IR payload used by the HTML widget path
- `src/hypergraph/viz/renderer/nodes.py` + `scope.py` — shared helpers used by `mermaid.py` and `ir_builder.py`
- `src/hypergraph/viz/assets/*.js` — split JS app modules (see "JS Asset Modules"); load order in `assets/__init__.py:FIRST_PARTY_ASSET_NAMES`
- `src/hypergraph/viz/html/generator.py` — HTML assembly with embedded assets
- `src/hypergraph/viz/html/estimator.py` — iframe dimension estimation
- `scripts/render_notebook_viz.py` — gallery generator with DialKit controls
