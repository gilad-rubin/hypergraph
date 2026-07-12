---
name: debug-viz
description: Debug hypergraph visualization issues: missing edges, wrong routing when expanded/collapsed, inputs outside containers, or mismatched layout bounds.
---

# Debug Viz

Use this skill for visualization bugs in the compact `GraphIR`, the Python/JS
scene builders, or the split `viz_*.js` browser modules.

## Current Architecture

1. `renderer/ir_builder.py` converts the flat graph into a compact `GraphIR`.
2. `scene_builder.py` and `assets/scene_builder.js` independently derive the
   same React Flow scene for an expansion state.
3. `renderer/__init__.py` ships the IR, initial scene, initial expansion state,
   and routing maps to `html/generator.py`.
4. `assets/viz.js` rebuilds the scene after expansion or display-option changes.
5. Split modules handle the remaining browser work: `assets/viz_layout.js`,
   `assets/viz_edges.js`, `assets/viz_nodes.js`, `assets/viz_controls.js`, and
   `assets/viz_debug.js`.

## Workflow

1. Generate a debug HTML file and compact JSON summary.
2. Inspect `meta.ir`, `meta.initial_expansion`, and the initial scene.
3. Build a collapsed or expanded scene directly with the Python scene builder.
4. Compare Python output with browser state through the debug API.
5. Apply the fix, update the Python/JS twins when derivation changes, and run
   the focused viz tests.

## Quick Start

Generate debug HTML and a summary of the IR, initial scene, and routing maps:

```bash
uv run python .claude/skills/debug-viz/scripts/debug_viz.py \
  myapp.graphs my_graph_config --depth 1 --open
```

Use separate outputs mode:

```bash
uv run python .claude/skills/debug-viz/scripts/debug_viz.py \
  myapp.graphs my_graph_config --depth 1 --separate-outputs --open
```

Inspect a scene derived for an explicit all-collapsed or all-expanded state:

```bash
uv run python .claude/skills/debug-viz/scripts/inspect_scene.py \
  myapp.graphs my_graph_config --collapsed

uv run python .claude/skills/debug-viz/scripts/inspect_scene.py \
  myapp.graphs my_graph_config --expanded --separate-outputs
```

## What To Check

- **IR facts**: verify node parents, expandable node order, external-input
  ownership, and expanded source/target rewrites in `meta.ir`.
- **Expansion state**: compare `meta.initial_expansion` with the state passed to
  `build_initial_scene()`.
- **Input ownership**: `ownerContainer` is state-dependent;
  `deepestOwnerContainer` is the deepest state-independent owner.
- **Routing maps**: `output_to_producer`, `param_to_consumer`, and
  `node_to_parent` feed compound layout and the browser debug API.
- **Twin parity**: derivation changes normally require matching updates in
  `scene_builder.py` and `assets/scene_builder.js`.
- **Layout and edges**: inspect `assets/viz_layout.js` for dagre and compound
  layout, then `assets/viz_edges.js` for final curve rendering.

## Browser Debugging

`window.__hypergraphVizDebug` is installed after layout for every visualization.
It exposes the current nodes, edges, layout measurements, routing maps, and a
summary. Wait for `window.__hypergraphVizReady === true` before reading it.

`window.__hypergraph_debug_viz = true` is a separate pre-render flag that shows
the developer layout controls. The Python `_debug_overlays` option only records
metadata today; it does not gate the browser API or render extra UI.

## Key Files

- `src/hypergraph/viz/ir_schema.py`: `GraphIR` schema.
- `src/hypergraph/viz/renderer/ir_builder.py`: flat graph to IR.
- `src/hypergraph/viz/scene_builder.py`: Python scene builder and test oracle.
- `src/hypergraph/viz/assets/scene_builder.js`: JavaScript scene-builder twin.
- `src/hypergraph/viz/renderer/__init__.py`: initial scene and metadata payload.
- `src/hypergraph/viz/assets/viz_layout.js`: layout and routing.
- `src/hypergraph/viz/assets/viz_debug.js`: `window.__hypergraphVizDebug`.
- `src/hypergraph/viz/assets/viz.js`: state management and scene refresh.
- `src/hypergraph/viz/html/generator.py`: standalone HTML assembly.

## Tests To Run

- `uv run pytest -n 0 tests/viz/test_debug_skill_scripts.py`
- `uv run pytest -n 0 tests/viz/test_scene_builder.py`
- `uv run pytest -n 0 tests/viz/test_viz_modules_js.py`
- `uv run pytest -n 0 tests/viz/test_renderer.py`
