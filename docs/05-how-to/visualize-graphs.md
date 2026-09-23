# Visualize Graphs

Hypergraph includes built-in interactive visualization. Call `.visualize()` on any graph to see its structure in a Jupyter or VSCode notebook — or save it as a standalone HTML file.

## Basic Usage

```python
from hypergraph import Graph, node

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add_one(doubled: int) -> int:
    return doubled + 1

graph = Graph([double, add_one])
graph.visualize()
```

This renders an interactive graph diagram inline. Nodes are connected automatically based on their input/output names.

The graph shows steps only: the values a step takes from outside (graph inputs, bound tools) are hidden until you ask for them. Hover a step, or tap it on a touch screen, to see its inputs as ghost pills beside it — see [Inputs on demand](#inputs-on-demand).

## Parameters

```python
graph.visualize(
    depth=0,                   # How many nested graph levels to expand
    theme="auto",              # "dark", "light", or "auto"
    show_types=True,           # Show type annotations on nodes
    separate_outputs=False,    # Render outputs as separate DATA nodes
    show_inputs=False,         # Draw input boxes; hidden inputs appear on hover/tap
    show_bounded_inputs=True,  # Include bound inputs (faded, dashed)
    simplify=True,             # Hide data edges a longer path already implies
    filepath=None,             # Save to HTML file instead of displaying
)
```

### `depth` — Expand nested graphs

When your graph contains nested graphs (via `.as_node()`), `depth` controls how many levels are expanded on load.

```python
inner = Graph([double, add_one], name="pipeline")
outer = Graph([inner.as_node(), final_step])

outer.visualize(depth=0)  # Inner graph shown as a single collapsed box
outer.visualize(depth=1)  # Inner graph expanded, showing double → add_one
outer.visualize(depth=2)  # Two levels deep (if further nesting exists)
```

You can also expand/collapse nested graphs interactively by clicking the toggle button on container nodes.

### `theme` — Color scheme

- `"auto"` (default) — Detects your notebook environment (Jupyter dark theme → dark mode)
- `"dark"` — Dark background with light nodes
- `"light"` — Light background with dark nodes

### `show_types` — Type annotations

```python
graph.visualize(show_types=True)
```

Displays parameter types and return types on each node. Long type names are shortened (e.g., `my_module.MyClass` → `MyClass`, truncated at 25 characters).

### `separate_outputs` — Output visibility

```python
graph.visualize(separate_outputs=True)
```

By default, edges connect functions directly. With `separate_outputs=True`, each output becomes a visible DATA node, making the data flow explicit.

### `show_inputs` — Input boxes

```python
graph.visualize(show_inputs=True)
```

Inputs are hidden by default, so the diagram shows the steps and how they connect. Hidden inputs are still one hover or tap away: see [Inputs on demand](#inputs-on-demand). Pass `show_inputs=True`, or press **Show Inputs** in the widget toolbar, to draw every input as a box above the steps that take it.

### `show_bounded_inputs` — Include bound inputs

```python
graph.bind(model="gpt-4o").visualize(show_bounded_inputs=False)
```

Bound inputs (tools and settings given with `Graph.bind`) are included by default, drawn faded and with a dashed border so they read as set up once rather than passed in on each run. That holds for input boxes (`show_inputs=True`) and for the ghost pills. Pass `show_bounded_inputs=False` to leave them out of both, so the diagram shows only the values a caller still provides.

### Inputs on demand

With inputs hidden, hovering a step (tapping it on a phone or tablet) draws what that step takes from outside the diagram as **ghost pills** beside it, each with a short dashed arrow into the step:

| Ghost | Pill | When |
| --- | --- | --- |
| Graph input | `url : str` | a value no step produces; the caller passes it in |
| Bound tool | `parser : Parser`, faded and dashed | a value bound with `Graph.bind` (left out when `show_bounded_inputs=False`) |
| Hidden edge | `raw ← fetch` | a value another step produces whose arrow `simplify` hides |

So nothing a step consumes is invisible. At the same time the step's upstream and downstream path stays lit and everything else dims, a gate's True/False labels included.

- **Pin**: click (or tap) the step. The ghosts stay after the pointer leaves, and the view pans (zooming out if it must) so they are on screen.
- **Clear**: click (or tap) empty canvas, or press `Escape`.
- **Toggle**: **Show Inputs** draws the input boxes and turns the ghosts off; **Hide Inputs** brings them back. Either clears any pinned ghosts.

Nothing is re-laid out: the ghosts float over the diagram. They are placed beside the step on the side with more room, never over another node, an edge label or each other. When neither side has room they sit in a row above or below the step, and types are dropped (names only) only when no spot fits them.

### `simplify` — Hide shortcut edges

```python
graph.visualize(simplify=False)   # show every data edge
```

Consider three nodes where `render` reads both `parse`'s output and, directly, `fetch`'s:

```python
@node(output_name="raw")
def fetch(url: str) -> str: ...

@node(output_name="parsed")
def parse(raw: str) -> str: ...

@node(output_name="page")
def render(parsed: str, raw: str) -> str: ...
```

There are three data edges, but `fetch → render` is a shortcut past a path that
already exists:

| `simplify=True` (default) | `simplify=False` |
| --- | --- |
| `fetch → parse → render` | `fetch → parse → render` plus `fetch → render` |

The shortcut adds no reachability information, so hiding it removes a crossing
line without changing what the diagram says about ordering. Toggle it live from
the widget toolbar (the three-dots-with-a-bypass button); `to_mermaid()` takes
the same flag, so both exporters agree.

Turn it **off** when you are auditing which values each node actually consumes —
`simplify=True` tells you `render` runs after `fetch`, but not that it reads
`fetch`'s output directly. Pair `simplify=False` with `separate_outputs=True`
for a full value-provenance view.

Never dropped, so a diagram is always safe to read for ordering and control:

- **Control and ordering edges** — a gate's dotted `gate ⇢ archive` means
  "archive may run", which no data path implies.
- **Cycle (feedback) edges** — in a loop every edge is reachable the long way
  round, so simplification never eats a cycle.
- **Mutually exclusive branch arms** — two arms of an `@ifelse`/`@route` feeding
  one consumer are alternatives, not a chain plus a shortcut.

### `filepath` — Save to HTML

```python
graph.visualize(filepath="my_graph.html")
```

Saves a standalone HTML file with all assets bundled (React, React Flow, Tailwind CSS). Opens in any browser, no server needed. Useful for sharing or embedding in documentation.

## Node Types in the Visualization

| Node type | Visual style | Description |
|-----------|-------------|-------------|
| **Function** | Indigo border | Regular `@node` functions |
| **Pipeline** | Amber border | Nested graphs (containers) |
| **Route** | Purple border | `@route` and `@ifelse` gate nodes |
| **Data** | Green border | Output data nodes (in `separate_outputs` mode) |
| **Input** | Gray (bound inputs faded, dashed) | Graph input parameters, drawn with `show_inputs=True` |

## START and Entrypoints

If a graph has configured entrypoints (`Graph(..., entrypoint=...)` or `with_entrypoint(...)`), visualization renders a synthetic **START** marker connected to those entrypoint nodes.

- START only appears when entrypoints are configured.
- START is rendered above the graph and routes into the configured entry scope.
- For expanded nested containers, START edges route to the visible internal entry node (not the container shell).

## Expanded Container Edge Routing

When nested graphs are expanded, cross-boundary edges remap to the visible internal producer/consumer nodes.

- Containers are visual groups, not executable endpoints.
- Dashed/control edges should never originate from a container START marker.
- Shared values (for example `messages`) anchor to the correct internal endpoint and do not create phantom external links or separate INPUT nodes.
- A renamed boundary output (`with_outputs(item_out="generated")`, `rename_outputs`) keeps its link to the consumer. With `separate_outputs=True` the expanded view routes through the inner producer's own DATA node, labelled with the inner name (`item_out`); collapsing the container returns the link to the container's DATA node under the outer name (`generated`). One caveat, independent of renaming: when a single boundary edge carries values from two different inner producers, the expanded view reconnects through the first producer only.
- `to_mermaid()` obeys the same rule: expanded, the arrow leaves the inner producer (merged mode) or that producer's own DATA pill under the inner name (`separate_outputs=True`), and it keeps resolving through each expanded level when a rename is chained across several boundaries. When the value has no single visible producer inside — two mutex arms produce it, or the real producer sits in a still-collapsed inner container — the edge stays on the container rather than assert a route that may not run (in `separate_outputs=True` mode that fallback still keys the DATA pill by the container and the outer name, which Mermaid renders as an undeclared box — a pre-existing limitation, tracked as a follow-up).

## Works Offline

All JavaScript dependencies (React, React Flow, Dagre layout) are bundled with hypergraph. No CDN calls, no internet required.

## Mermaid Text Diagrams

`graph.to_mermaid()` returns a `MermaidDiagram` — a text-based Mermaid flowchart instead of the interactive widget. Useful anywhere Markdown renders Mermaid (GitHub, GitBook) or in plain terminals with no notebook:

```python
diagram = graph.to_mermaid()   # accepts the same depth/show_types/separate_outputs/simplify as .visualize()
diagram                        # renders inline in Jupyter/VS Code via a text/vnd.mermaid MIME type
print(diagram)                 # raw Mermaid source, works anywhere
diagram.source                 # the raw string directly
```

## Debugging Graph Structure

`hypergraph.viz` also has non-interactive tools for spotting structural issues before you render anything — useful in scripts, tests, or headless environments.

```python
from hypergraph.viz import validate_graph, find_issues

result = validate_graph(graph)
if not result.valid:
    print(result.errors)

issues = find_issues(graph)
# IssueReport(validation_errors=[], orphan_edges=[], disconnected_nodes=[],
#             missing_parents=[], self_loops=[])
```

`VizDebugger` (also reachable as `graph.debug_viz()`) adds interactive tracing for a specific node or edge:

```python
debugger = graph.debug_viz()  # same as VizDebugger(graph)

trace = debugger.trace_node("double")
print(trace.status)          # "FOUND" or "NOT_FOUND"
print(trace.outgoing_edges)  # [{'to': 'add_one', 'value': 'doubled', 'type': 'data'}]

edge_trace = debugger.trace_edge("double", "add_one")
```

## Agent-Friendly Display Mode

Every hypergraph object shown at the end of a notebook cell — a `Graph`, a `RunResult`, a checkpointer — stores its rich HTML widget in the `.ipynb` file. A single displayed `Graph` adds ~800 KB. When an agent runs the notebook and reads it back, that HTML is wasted context.

Plain display mode makes implicit display fall back to the compact text reprs instead:

```python
import hypergraph

hypergraph.set_display_mode("plain")

graph    # Graph: pipeline | 2 nodes | 1 edge | no cycles   (~100 bytes, not ~800 KB)
result   # RunResult(status=completed, values={'doubled': 4}, ...)
```

Or set it from the environment — it is read at display time, so setting it after import works too:

```bash
export HYPERGRAPH_DISPLAY=plain
```

A `set_display_mode()` call overrides the environment variable. Explicit visualization stays rich in plain mode: `graph.visualize()` and `graph.to_mermaid()` still render the full output — an agent that asks for the widget gets the widget. Switch back anytime with `hypergraph.set_display_mode("rich")`.
