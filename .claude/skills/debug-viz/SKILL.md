---
name: debug-viz
description: Debug hypergraph visualization issues: missing edges, wrong routing when expanded/collapsed, inputs outside containers, or mismatched layout bounds.
---

# Debug Viz

Use this skill for visualization bugs in `renderer.py`, `assets/layout.js`, or `assets/state_utils.js`.

## Workflow

1. Generate a debug HTML file and a compact JSON summary.
2. Inspect `edgesByState` and the expansion key used at runtime.
3. Validate input/output scoping (ownerContainer/internalOnly).
4. Confirm layout/routing phases with debug overlays.
5. Apply fix + rerun targeted viz tests.

## Quick Start

Generate debug HTML and summary JSON:

```bash
uv run python .claude/skills/debug-viz/scripts/debug_viz.py \
  myapp.graphs my_graph_config --depth 1 --open
```

Use separate outputs mode:

```bash
uv run python .claude/skills/debug-viz/scripts/debug_viz.py \
  myapp.graphs my_graph_config --depth 1 --separate-outputs --open
```

Inspect precomputed edges for a specific state key:

```bash
uv run python .claude/skills/debug-viz/scripts/inspect_edges_by_state.py \
  myapp.graphs my_graph_config --expanded
```

List all `edgesByState` keys:

```bash
uv run python .claude/skills/debug-viz/scripts/inspect_edges_by_state.py \
  myapp.graphs my_graph_config --list-keys
```

## What To Check

- **edgesByState**: The runtime uses `expandableNodes` + `separateOutputs` to pick a key. If edges are missing after expand/collapse, confirm the key and compare the edge list for the expected state.
- **Input ownership**: `ownerContainer` should be set when all consumers live inside the same expanded container; `deepestOwnerContainer` helps detect missing scoping.
- **Container outputs**: Internal-only outputs should not be shown when collapsed. In separate outputs mode, container DATA nodes are hidden when the container is expanded.
- **Reroute phase**: In `layout.js`, Step 5 re-routing should skip DATA node sources (`data_` prefix) to avoid clobbering separate-output edges.
- **Cross-boundary edges**: Deeply nested sources/targets are lifted to their direct child ancestor during child layout. If edge order looks wrong, inspect `deepToChild` lifting.

## Debug Overlays

Enable in the browser console before rendering:

```js
window.__hypergraph_debug_viz = true
```

This annotates bounds, margins, and edge validation errors (Step 6) to catch misaligned stems or missing nodes.

## Key Files

- `src/hypergraph/viz/renderer.py`: precomputes edges for all expansion states and applies scoping rules.
- `src/hypergraph/viz/assets/layout.js`: layout phases, edge routing, and reroute logic.
- `src/hypergraph/viz/assets/state_utils.js`: applyState filtering for container outputs/visibility.
- `src/hypergraph/viz/assets/constants.js`: shared layout constants.
- `src/hypergraph/viz/html_generator.py`: viewport centering and debug overlay plumbing.

## Common Symptoms → Likely Causes

| Symptom | Check | Likely Cause |
| --- | --- | --- |
| Edge points to container when expanded | `edgesByState` key + `param_to_consumer` | wrong mapping or state key mismatch |
| Input appears outside expanded container | `ownerContainer` vs `deepestOwnerContainer` | `_compute_input_scope()` returning None |
| Missing edges in separate outputs | reroute Step 5 | data edges overwritten by function reroute |
| Container outputs show when collapsed | container visibility | `_is_output_externally_consumed()` false positives |

## Tests To Run

- `uv run pytest tests/viz/test_scope_aware_visibility.py`
- `uv run pytest tests/viz/test_edges_by_state_contract.py`
- `uv run pytest tests/viz/test_edge_connections.py`
