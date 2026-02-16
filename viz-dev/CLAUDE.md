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

## Working With the User on Viz

**The user thinks visually, not in implementation terms.** Don't describe fixes using "bezier," "waypoints," "control points," or "dot products" — describe what they'll SEE ("bigger curves," "softer angles," "edges merge together"). Always add a slider so they can explore the effect themselves.

**Workflow:**
1. Make ONE change at a time, let the user test visually
2. Use Agentation annotations as the primary feedback loop
3. If a change has unwanted side effects, REVERT it immediately — don't pile fixes on top of a broken approach
4. When stuck, explain the visual tradeoff honestly and let the user decide

**Hard-won lessons:**
- **Edge merging is sacred.** Any change that causes edges sharing a target to visually separate is immediately rejected. Changes to curve SHAPE (bezier-level) are safe. Changes to waypoint POSITIONS break merging.
- **Uniformity matters more than perfection.** If 8 out of 10 edges look one way and 2 look different, the inconsistency is the problem — even if the 2 are "technically correct."
- **Add sliders, don't pick values.** The user wants to explore parameter space themselves. Set reasonable defaults, provide a tunable range, and let them find what feels right.
- **Revert fast.** If the user says it doesn't work, revert and rethink — don't try to patch around the issue.

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
    ConstantsPanel.jsx      # ~35 grouped sliders for all numeric constants
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

## Edge Routing Pipeline

Two separate routing pipelines produce edge waypoints — this is a known pain point:

| Pipeline | Where | Edges affected |
|----------|-------|----------------|
| **Corridor router** | `constraint-layout.js` | Internal edges within same layout scope |
| **Cross-boundary** | `layout.js` | Edges entering/exiting expanded containers |

### Rendering chain (components.js)

```
data.points → normalizePolylinePoints → buildRoundedPolyline (with turn softening)
```

1. **normalizePolylinePoints**: dedup, drop short-segment points (threshold: `EDGE_ELBOW_RADIUS`), only drop nearly-collinear points (`EDGE_MICRO_MERGE_ANGLE`)
2. **buildRoundedPolyline**: Q bezier at each corner with `EDGE_ELBOW_RADIUS`. Control point pulled toward chord by `EDGE_TURN_SOFTENING`.

### Key constants for edge appearance

| Constant | Default | What you SEE |
|----------|---------|-------------|
| `EDGE_ELBOW_RADIUS` | 28 | How big the rounded corner is (bigger = curve starts earlier) |
| `EDGE_TURN_SOFTENING` | 0.15 | How much softer than 90° the corners feel |
| `EDGE_MICRO_MERGE_ANGLE` | 60 | Removes tiny kinks — higher = more aggressive cleanup |
| `EDGE_SHOULDER_RATIO` | 0.3 | Where the horizontal run starts (only affects horizontal-layout edges) |

### Failed approaches (don't repeat these)

| Approach | Why it failed |
|----------|--------------|
| **EDGE_CORNER_RELAXATION** (moving waypoints to soften corners) | Breaks edge merging — each edge moves differently, so shared stems diverge |
| **Orthogonalize pass** (inserting waypoints to break diagonals into H/V) | Creates extra corners near targets, makes edges "wobbly" |
| Both approaches tried to change waypoint POSITIONS, which is fundamentally incompatible with edge convergence |

**What works:** Change curve SHAPE (`EDGE_TURN_SOFTENING`) + curve SIZE (`EDGE_ELBOW_RADIUS`). These modify rendering without touching waypoints, so convergence is preserved.

### Root causes of common visual issues

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Sharp corner on one edge, smooth on others | Short segments cap corner radius | `normalizePolylinePoints` drops short-segment points |
| Edges don't blend into shared target | `mergeSharedTargetEdgesPhase` skipped edges with >2 points | Removed `points.length > 2` filter in `layout.js` |
| Edges cross through nodes | Soft penalty, not hard constraint | `EDGE_NODE_PENALTY` (corridor), `rerouteAroundCrossedNodes` (cross-boundary) |
| Some edges look different from others | Two routing pipelines produce different waypoint patterns | Open issue — needs unified routing |

## Workflow: Tuning Constants

1. Start dev server + pick a complex graph (Full Pipeline Expanded is good)
2. Adjust sliders — start with `EDGE_ELBOW_RADIUS` and `EDGE_TURN_SOFTENING`
3. Observe edge routing changes live
4. When happy, click "Copy Constants" → paste JSON into `constants.js`
5. (Optional) Annotate visual issues via Agentation for Claude to see

## Agentation Integration

[Agentation](https://agentation.dev) provides a visual annotation overlay. Click elements on the canvas to annotate issues. The annotations flow to Claude via the MCP server.

- Component: `<Agentation endpoint="http://localhost:4747" />` in `DevApp.jsx`
- MCP server: `npx agentation-mcp server` (port 4747)
- Claude reads annotations via `agentation_watch_annotations` tool
- Claude resolves annotations via `agentation_resolve` tool

## Files That Read Constants

These IIFEs are reloaded when constants change:
- `constraint-layout.js` — constraint solver, corridor routing
- `layout.js` — layout phases, stem placement, convergence merging
- `components.js` — edge rendering, curve drawing, turn softening
- `app.js` — debug overlay, node offsets

These are loaded once and NOT reloaded (no constant dependency):
- `htm.min.js`, `kiwi.bundled.js` — libraries
- `reactflow.umd.js` — React Flow (uses window.React)
- `theme_utils.js` — theme detection only
