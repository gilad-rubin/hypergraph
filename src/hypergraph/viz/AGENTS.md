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
pipelines differ. Boundary-OUTPUT resolution is the exception that already
went to `ir_builder`: `_resolve_data_source_and_name` asks
`ir_builder.deepest_internal_producers` for both the inner producer and the
name it emits, so the rename translation has ONE authority. Never add a second
name matcher here.

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
  text or an explicit aria label, expose tooltips to keyboard focus
  (`:focus-visible`) as well as mouse hover, never to a touch (a tap focuses the
  button and a phone has no hover to take the tooltip away), and let Escape
  hide transient tooltip UI.
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
9. `viz_ghosts.js` — ghost inputs + focus (see "Input Visibility and Ghost
   Inputs"): ghost sets, focus sets, pill placement, the overlay layer
10. `viz.js` — App bootstrap: state management, scene refresh, theme wiring,
   `hypergraph-set-options` message listener, hover/tap/pin state

## Edge Routing

- Edge endpoints use dagre's native x-positions (spread across node width)
- Endpoints clamped within padded region: `EDGE_ENDPOINT_PADDING` (default: 0.25, fraction of node width)
- Do not add Hypergraph-side merge stems or synthetic routed paths; dagre owns edge routing. The two sanctioned post-layout adjustments re-assign dagre's own points (`untangleSiblingExits`) and add one vertical landing point before a steep entry (`landSiblingEntries`); neither moves a node or a dagre bend.
- Every edge path must end with a segment that has its own end direction (`curveBasis` ends with `L`, as D3's does). WebKit orients the arrowhead marker from the final segment alone, so a degenerate final cubic draws every head at 0°, beside its line; `tests/viz/test_edge_arrowheads.py` pins it. Browser tests that use the `_browser`/`page` fixtures render once per engine in `HYPERGRAPH_VIZ_ENGINES` (default `chromium`), and CI's `viz-webkit` job renders them in WebKit. Check both engines locally with `uv run playwright install webkit` (once), then `HYPERGRAPH_VIZ_ENGINES=chromium,webkit uv run pytest tests/viz -m viz_browser`.

- Sibling exits: dagre ties the first bends of edges leaving one node and can swap them, so edges cross just under their source. `untangleSiblingExits` (`viz_layout.js`, both the flat and the compound layout) re-deals those bends and exits in the order of each edge's next point; dagre's reserved positions do not move. `tests/viz/test_edge_exit_order.py` pins it.
- Sibling entries (ruling D53): every edge lands within 30° of straight down, so its head points into the node, and tips on one node sit at least `ENTRY_GAP` apart, in the order of each edge's previous point. `landSiblingEntries` (`viz_layout.js`, both layouts) spreads the entries and gives a steeper approach a vertical landing point; dagre's bends do not move. `tests/viz/test_edge_entry_points.py` pins it.

**BRANCH/START/END exception**: Exits always use center-x (a diamond has a single exit point, its bottom vertex). Entries use center-x for a single edge; several edges into one node spread symmetrically about its center, and on a diamond they spread along its two upper edges.

## Input Visibility and Ghost Inputs

- `widget.py:_resolve_input_visibility` is the ONE place the input defaults
  live (ruling D56): `show_inputs=False`, `show_bounded_inputs=True`, and the
  deprecated `show_external_inputs` alias. Every public entry point
  (`Graph.visualize`, `viz.visualize`, `HyperTable.visualize`,
  `extract_debug_data`) and `render_flat_graph` pass their raw `None`-default
  arguments through it, and never carry an alias block of their own. Internal
  helpers take the resolved booleans and carry no default (ruling D59):
  `renderer.render_graph`, `scene_builder.build_initial_scene` and
  `html.estimate_layout` / `LayoutEstimator` take them keyword-only, and
  `scene_builder.js` throws on a missing flag. Never add a default anywhere
  else; the one JS mirror of the resolver is `viz.js`'s fallback for a
  payload missing the flags.
- With inputs hidden, `viz_ghosts.js` shows a hovered or tapped step's inputs
  as ghost pills (tapping a collapsed container opens it instead, as before): external inputs, bound tools (faded, dashed) and values whose
  data edge `simplify` dropped (`raw ← fetch`). The dropped edges are the diff
  between the drawn scene and the same scene built with `simplify: false`;
  scene edges carry `data.valueNames` (both scene-builder twins) so the ghost
  can name the value.
- Ghosts are an overlay: no layout pass, no node moves. Placement is measured
  from the rendered node and edge-label boxes, once per focused step; the
  rule is documented at the top of `viz_ghosts.js`. Any re-layout clears the
  ghost state.
- Focus dimming goes through each component's own state: nodes get the
  `hg-dim` / `hg-focus` class, and `CustomEdge` dims its path AND its label
  from `data.dimmed`. Never find an edge label by its text.
- Touch never goes through hover: the App tracks the last `pointerType`, a tap
  pins directly, and emulated mouse events after a tap are ignored.
- A pin's pan glides, so its end is only observable on screen: once the
  rendered view reaches the target, `window.__hypergraphVizPinFramed` counts
  up. A browser test that reads positions after a pin waits on that count
  (`_pin` in `test_ghost_inputs.py`), never on a fixed delay: an engine may
  deliver a tap's click after `tap()` returns, and the view is then read mid-pan.
- `tests/viz/test_ghost_inputs.py` pins all of it (ghost sets, no overlaps,
  focus, pin/clear/toggle, touch) in both engines.

## Node Types and Mapping

- Flat graph containers: `node_type == "GRAPH"`
- React Flow mapping: `GRAPH` → `PIPELINE`
- Synthetic nodes: `INPUT`, `INPUT_GROUP`, `DATA`, `OUTPUT` (boundary-output
  anchor for a container output no descendant produces)

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
  edges to the deepest internal producer / EVERY internal consumer when a
  container is expanded — one value can enter a container at several nodes, so
  ``target_when_expanded`` is a tuple whenever more than one consumer reads it.
  A rewritten endpoint that sits inside a still-collapsed inner container is
  resolved up to its deepest visible ancestor in the scene builders (edges
  aggregate to the boundary rather than vanishing), and endpoints sharing an
  ancestor dedup to one edge.
  An identity-mode fan-out edge (``HyperTable.visualize``) re-routes instead to
  the mapped item's field INPUT pill(s) — ``segment_pages ──pages──▶ [page_text]
  ──▶ embed_page`` — so ``target_when_expanded`` may also be a tuple of pill ids,
  and each such pill is flagged ``IRExternalInput.map_fed`` (styled distinctly,
  not as a free-floating external input). Falls back to the container entrypoint
  when the mapped item has no matching field (e.g. ``list[str]``).
- ``IREdge.value_names_when_expanded`` names those same values the way the
  INNER producer emits them, index-aligned with ``value_names``. Once the
  source rewrite fires, the DATA pill in ``separate_outputs`` mode belongs to
  the node the rewrite landed on and is keyed by THAT node's output name — a
  container renaming an output at its boundary
  (``with_outputs(item_out="generated")``) would otherwise compose
  ``data_<inner producer>_generated``, an id nothing emits, and the edge ships
  hidden with a dangling source while the consumer loses its only incoming
  edge. Both twins consume the field ONLY when its length matches
  ``value_names``, so a malformed payload degrades to ``value_names``
  identically in Python and JS instead of diverging. Optional, so no schema
  bump: an old scene builder that ignores it draws the pre-fix picture.
- A container output NO descendant produces (a mounted HyperTable's receipt:
  the ``MaterializationNode``'s own output is the whole table's completion)
  gets a synthesized ``node_type == "OUTPUT"`` anchor pill INSIDE the
  container (id ``<container>/__output__<name>``), and the boundary edge's
  ``source_when_expanded`` points at it — an edge's source must be a node;
  only a COLLAPSED container may stand in as one. The pill follows normal
  child visibility (hidden while its container chain is collapsed), and
  ``performCompoundLayout`` ranks it below the container's visible sinks via
  rank-only, undrawn dagre edges so the boundary edge flows downhill.
  ``mermaid.py`` synthesizes the same anchors via
  ``ir_builder.container_output_anchors`` (schema v6).
- INPUT pills hoist, never hide: an external input owned by a collapsed
  container surfaces at its deepest VISIBLE ancestor with its edge aggregated
  to the collapsed hull — a graph's contract inputs must survive any collapse.
  Only ``map_fed`` pills disappear with their container (the container-level
  fan-out edge represents the same value while collapsed).
- ``IREdge.is_back_edge`` marks DFS back-edges so feedback routing survives
  arbitrary expansion changes. An *ordering* back edge whose source outputs ∩
  target inputs ∩ shared-state is non-empty (a routed interrupt's answer
  returning to its gate) carries that value name as ``IREdge.label`` —
  ``ir_builder.shared_answer_label``, shared with Mermaid. Feedback edges
  render their orthogonal channel faithfully (``roundedChannelPath`` in
  ``viz_edges.js``) instead of ``curveBasis``, which smeared the corners into
  a wide orbit.
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
     - Only **plain data** and **INPUT pill** edges are removal candidates.
       Control, ordering, output, start/end and mutex (`exclusive`) edges never
       get dropped. An input feeding a chain keeps only its EARLIEST
       consumer(s); an input edge into a *collapsed* container is judged
       box-level — it drops when any other visible route delivers into that
       box (phantom in-port→box path links, added in both scene-builder twins
       and Mermaid) — while data edges into the same box stay port-strict.
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
| Consumer loses its incoming edge when a container expands in separate-outputs mode | renamed boundary OUTPUT: the DATA id was composed from the container-level name instead of `IREdge.value_names_when_expanded` | `renderer/ir_builder.py` + both `scene_builder` twins |
| Mermaid draws an undeclared `data_<container>_<outer name>` box, or an arrow out of an expanded `subgraph` hull | boundary output rename not translated; `mermaid.py` resolved the source without asking `ir_builder.deepest_internal_producers` | `mermaid.py:_resolve_data_source_and_name` |
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
- `src/hypergraph/viz/widget.py` — compact IR payload used by the HTML widget path; `_resolve_input_visibility`, the one home of the input defaults
- `src/hypergraph/viz/assets/viz_ghosts.js` — ghost inputs + focus overlay
- `src/hypergraph/viz/renderer/nodes.py` + `scope.py` — shared helpers used by `mermaid.py` and `ir_builder.py`
- `src/hypergraph/viz/assets/*.js` — split JS app modules (see "JS Asset Modules"); load order in `assets/__init__.py:FIRST_PARTY_ASSET_NAMES`
- `src/hypergraph/viz/html/generator.py` — HTML assembly with embedded assets
- `src/hypergraph/viz/html/estimator.py` — iframe dimension estimation
- `scripts/render_notebook_viz.py` — gallery generator with DialKit controls
