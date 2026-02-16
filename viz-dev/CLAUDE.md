# Viz Dev Environment

Interactive development environment for tuning the hypergraph visualization system's edge routing constants without touching production code.

## Quick Start

```bash
cd viz-dev
npm install
npm run generate    # Creates example graph JSON in public/data/
npm run dev         # Starts Vite at http://localhost:3000
```

For Agentation (visual annotation):
```bash
npx agentation-mcp server   # Starts MCP server on :4747
```

## Architecture

```
viz-dev/
  package.json              # Vite + React + Agentation
  vite.config.js            # React plugin, fs.allow for symlink
  index.html                # Production-matching CSS/styles, #root + #dev-root
  public/
    assets/ → symlink       # -> ../../src/hypergraph/viz/assets/ (production JS/CSS)
    data/                   # Generated graph JSON (gitignored)
  src/
    main.jsx                # Bootstrap: set window.React, load IIFEs, mount DevApp
    DevApp.jsx              # Graph selector, re-layout, copy constants, Agentation
    ConstantsPanel.jsx      # ~30 grouped sliders for all numeric constants
    graph-loader.js         # Fetch graph JSON + manifest from /data/
  generate_data.py          # Python script to create example graphs
```

**Key invariant**: Zero changes to `src/hypergraph/viz/`. Everything wraps around existing IIFE code.

## How Constants Become Reactive

The IIFE modules capture `window.HypergraphVizConstants` into local variables at load time. Changing the global after load has no effect.

**Solution** (in `main.jsx`):
1. Mutate `window.HypergraphVizConstants` with slider values
2. Remove the 4 dependent `<script>` tags: `constraint-layout.js`, `layout.js`, `components.js`, `app.js`
3. Re-add them with `?t=Date.now()` cache-bust (forces browser re-evaluation)
4. Call `window.HypergraphVizApp.init()` to re-mount the viz

A 150ms debounce prevents rapid slider dragging from queuing re-inits.

## How React Coexistence Works

The production viz uses UMD bundles (`react.production.min.js`). The dev environment imports React from npm and sets `window.React = React` before any IIFE loads. All modules share one React instance.

The `#root` element is replaced (not reused) on re-init to avoid React's "container already has a root" warning.

## Example Graphs

`generate_data.py` creates 13 example graphs:

| Graph | Nodes | Features |
|-------|-------|----------|
| Simple Pipeline | 7 | Linear 3-node chain |
| Fan-in / Fan-out (RAG) | 10 | Multiple inputs converging |
| Binary Branching | 8 | `@route` with 2 targets |
| Agent Loop (Cycle) | 8 | `emit`/`wait_for` ordering edges |
| Nested (Collapsed) | 9 | Subgraph as collapsed box |
| Nested (Expanded) | 9 | Subgraph expanded (depth=1) |
| With Type Annotations | 9 | `show_types=True` |
| Complex Nested (3 layers) | 25 | 3 levels: root → process_item → build_chunks, with `@ifelse` |
| Complex Nested (Expanded) | 28 | Same, depth=8 |
| Full Pipeline | 31 | validate → ifelse → retrieve → generate, 3 subgraphs |
| Full Pipeline (Expanded) | 33 | Same, all expanded |
| + 2 Separate Outputs variants | | `separate_outputs=True` for simple + fan-in |

## Agentation Integration

[Agentation](https://agentation.dev) provides a visual annotation overlay. Click elements on the canvas to annotate issues. The annotations flow to Claude via the MCP server.

- Component: `<Agentation endpoint="http://localhost:4747" />` in `DevApp.jsx`
- MCP server: `npx agentation-mcp server` (port 4747)
- Claude reads annotations via `agentation_watch_annotations` tool
- Claude resolves annotations via `agentation_resolve` tool

## Workflow: Tuning Constants

1. Start dev server + pick a complex graph
2. Adjust sliders (e.g., `EDGE_CURVE_STYLE` 0→1 for smooth curves)
3. Observe edge routing changes live
4. When happy, click "Copy Constants" → paste JSON into `constants.js`
5. (Optional) Annotate visual issues via Agentation for Claude to see

## Files That Read Constants

These IIFEs are reloaded when constants change:
- `constraint-layout.js` — constraint solver, corridor routing
- `layout.js` — layout phases, stem placement
- `components.js` — edge rendering, curve drawing
- `app.js` — debug overlay, node offsets

These are loaded once and NOT reloaded (no constant dependency):
- `htm.min.js`, `kiwi.bundled.js` — libraries
- `reactflow.umd.js` — React Flow (uses window.React)
- `theme_utils.js` — theme detection only
