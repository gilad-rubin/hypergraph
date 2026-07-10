# 0006 — Public GraphNode execution identity seam

status: done

## Fixed acceptance contract

Superposition PRD 0008 must identify configured Hypergraph execution without
reading Hypergraph private attributes. `GraphNode.map_config` currently exposes
only `(params, mode, error_handling)`, while `clone`, HyperTable child
`identity`/`schema`, and `complete_on_stop` change execution but are private.

Before:

```python
mapped = graph.as_node(name="pages").map_over(
    "page", clone=True, identity="document_id", schema=Page
)

assert mapped.map_config == (["page"], "zip", "raise")
# clone, identity, and schema cannot be read through a public API.
```

After:

```python
assert mapped.map_execution_config == GraphNodeMapExecutionConfig(
    params=("page",),
    mode="zip",
    error_handling="raise",
    clone=True,
    identity="document_id",
    schema=Page,
)

finishing = graph.as_node(name="pages", complete_on_stop=True)
assert finishing.complete_on_stop is True
```

Acceptance criteria:

- `GraphNodeMapExecutionConfig` is a frozen typed public value. List-shaped
  inputs are exposed as tuples so callers cannot mutate node configuration.
- `GraphNode.map_execution_config` returns `None` when the node is not mapped,
  otherwise it returns the exact effective `params`, `mode`, `error_handling`,
  `clone`, optional `identity`, and optional `schema` used by runners.
- `GraphNode.complete_on_stop` exposes the exact boolean used by nested-graph
  execution.
- Renames/copies preserve the same effective values the runner consumes.
- Existing `GraphNode.map_config` remains backward compatible.
- The types are importable from the public `hypergraph` package.
- Focused tests fail before implementation and pass after it; the repository's
  warning-as-error suite remains green.

Out of scope:

- no hashing, serialization, content store, Superposition import, or Git
  integration;
- no change to runner behavior, graph construction, or the existing
  `definition_hash` contract;
- no exposure of a general private-state dictionary.
