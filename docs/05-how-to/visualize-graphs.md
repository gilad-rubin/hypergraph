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

## Parameters

```python
graph.visualize(
    depth=0,                # How many nested graph levels to expand
    theme="auto",           # "dark", "light", or "auto"
    show_types=False,       # Show type annotations on nodes
    separate_outputs=False, # Render outputs as separate DATA nodes
    show_external_inputs=False, # Show root-level INPUT/INPUT_GROUP nodes
    filepath=None,          # Save to HTML file instead of displaying
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

### `show_external_inputs` — Root input visibility

```python
graph.visualize(show_external_inputs=True)
```

Root-level external INPUT/INPUT_GROUP nodes are hidden by default to reduce clutter in cyclic graphs.  
Use this option (or the side-panel toggle in the widget) to show or hide them interactively.

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
| **Input** | Gray | Graph input parameters |

## START and Entrypoints

If a graph has configured entrypoints (`Graph(..., entrypoint=...)` or `with_entrypoint(...)`), visualization renders a synthetic **START** marker connected to those entrypoint nodes.

- START only appears when entrypoints are configured.
- START is rendered above the graph and routes into the configured entry scope.
- For expanded nested containers, START edges route to the visible internal entry node (not the container shell).

## Expanded Container Edge Routing

When nested graphs are expanded, cross-boundary edges remap to the visible internal producer/consumer nodes.

- Containers are visual groups, not executable endpoints.
- Dashed/control edges should never originate from a container START marker.
- Shared values (for example `messages`) anchor to the correct internal endpoint and do not create phantom external links.

## Works Offline

All JavaScript dependencies (React, React Flow, Kiwi constraint solver) are bundled with hypergraph. No CDN calls, no internet required.
