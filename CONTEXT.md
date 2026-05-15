# Hypergraph

Hypergraph is a workflow orchestration framework where users compose nodes and nested graphs through named values.

## Language

**Port**:
A named input or output on a node or graph boundary.
_Avoid_: Argument, field

**Local port name**:
The local port name before any graph-node namespace prefix is applied.
_Avoid_: Suffix, internal name

**Port address**:
A parent-facing port name, optionally qualified by graph-node path segments such as `retrieval.query`.
_Avoid_: Port name

**Flat port**:
A port that participates directly in its parent graph's shared name flow.
_Avoid_: Public port, global port

**Namespaced port**:
A port addressed through its child node name, such as `retrieval.query`.
_Avoid_: Private port, hidden port

**Exposed port**:
A child port whose parent-facing address is intentionally renamed into the parent graph's flat name flow.
_Avoid_: Unscoped port, leaked port

**Selected output**:
An output included in a graph's configured output surface.
_Avoid_: Exposed output

**Graph-node surface**:
The ports a graph presents after graph-level scoping such as selected outputs and entrypoints are applied.
_Avoid_: Graph internals, raw graph ports

**Shared parameter**:
A value intentionally held in one graph's state across cyclic execution rather than carried by ordinary data edges.
_Avoid_: Shared input, shared output

**Entrypoint**:
A node where execution is configured to start, excluding upstream nodes from the active graph.
_Avoid_: Start node, root node

## Relationships

- A **Port** is either a **Flat port** or a **Namespaced port** from the parent graph's point of view.
- Renaming changes a **Local port name** and is agnostic to boundary addressing.
- A **Port address** may contain `.` only as a namespace separator inserted by Hypergraph, not inside a user-authored port name.
- Boundary addressing is resolved before graph semantics: flat mode, namespaced mode, and expose only define parent-facing **Port addresses**.
- `GraphNode.inputs` and `GraphNode.outputs` are resolved parent-facing **Port addresses** after boundary projection, not raw child **Local port names**.
- The same **Port address** may appear in both `GraphNode.inputs` and `GraphNode.outputs`; cyclic values such as `messages` need both an input seed and a produced output.
- A **Selected output** controls which outputs a graph makes available to callers or parent graphs before graph-node boundary transforms are applied.
- `rename_inputs(...)` and `rename_outputs(...)` rename **Local port names** only; they do not connect values and do not rename exposed parent-facing addresses.
- An **Exposed port** replaces the child port's namespaced parent-facing address for both inputs and outputs; callers use the exposed flat name, not both names.
- An `expose(...)` alias is the final flat parent-facing **Port address** at that graph-node boundary, not a renamed **Local port name** that is later namespaced.
- An **Exposed port** defines the final parent-facing flat address for that port; rename the **Local port name** before exposing it, or expose directly with an alias.
- An **Exposed port** is local to the graph-node boundary where it is declared; ancestors may namespace that flat surface again.
- Exposing a name applies to matching input and output ports on the current **Graph-node surface**; graph validation decides whether the resulting flat graph semantics are valid.
- `expose(...)` targets **Local port names** on the current **Graph-node surface**, not already-projected **Port addresses**.
- Direction-specific expose operations are out of scope for the MVP; when a **Local port name** exists as both input and output, exposing it exposes both directions.
- Exposing a name is valid when at least one matching input or output port exists on the current **Graph-node surface**.
- Multiple GraphNodes may expose input ports to the same flat address and share one parent input; duplicate aliases inside one GraphNode are rejected. Multiple exposed outputs with the same flat address follow the ordinary duplicate-output conflict rules.
- An **Exposed port** may only target ports that exist on the current **Graph-node surface** after graph-level scoping such as selected outputs and entrypoints.
- Only **Namespaced ports** can become **Exposed ports**; exposing an already **Flat port** is an error.
- After boundary addressing is resolved, an **Exposed port** is an ordinary **Flat port** for graph semantics such as auto-wiring, default consistency, type validation, and duplicate-output conflict checks.
- Visualization must render the same **Port addresses** that graph construction and execution use, including renames, selected outputs, exposed ports, and whether a shared input belongs at the parent or child boundary.
- For the MVP, expose is a graph-node boundary operation, not a graph-level operation.
- For the MVP, namespacing is a graph-node boundary operation declared when a graph is used as a node, not an intrinsic graph property.
- A namespaced graph-node namespaces both inputs and outputs; expose is the only MVP mechanism for returning selected names to the parent flat flow.
- A graph-node always has a resolved name from either the wrapped graph or the `as_node(name=...)` call; namespacing is orthogonal and uses that resolved graph-node name as the namespace prefix.
- A **Shared parameter** is scoped to the graph that declares it; it does not automatically become shared in parent or child graphs.
- An **Entrypoint** can turn an upstream output into a required input at the graph boundary.

## Example Dialogue

> **Dev:** "If `chat.messages` is both an input and output, does exposing `messages` only expose the input?"
> **Domain expert:** "No. An **Exposed port** opens the matching child port into the parent flat flow; if the child has both input and output ports named `messages`, both are exposed."
>
> **Dev:** "Can I expose `response` as `research_answer` and then use `rename_outputs(research_answer='answer')`?"
> **Domain expert:** "No. `research_answer` is the parent-facing **Port address**, not a **Local port name**. Rename the local name first, or choose the final exposed name in `expose(...)`."
>
> **Dev:** "After `namespaced=True`, does `GraphNode.outputs` contain `response` or `researcher.response`?"
> **Domain expert:** "`GraphNode.outputs` contains the resolved parent-facing **Port address**, so the value is `researcher.response`."

## Flagged Ambiguities

- "private" was used for namespaced ports, but **Namespaced port** is the canonical term because the port remains addressable.
- "shared input" and "shared output" were used for cyclic state, but **Shared parameter** is the canonical term because the value is stateful rather than an ordinary one-way port.
- "expose" was considered as adding an extra alias, but **Exposed port** is now resolved as a parent-facing rename: the exposed flat name replaces the namespaced address at that boundary.
- Existing docs sometimes say `select()` controls which outputs are "exposed" when nested; use **Selected output** for that concept to avoid confusing it with `.expose(...)`.
- `rename_inputs(...)` and `rename_outputs(...)` were sometimes used as wiring language, but they are canonical **Local port name** renames only.
- `link_inputs` was considered for shared input fan-out, but the MVP keeps **Exposed port** as the single boundary-flattening concept; `link_inputs` may be added later as direct-child input-only sugar.
- Direction-specific expose was considered, but the MVP keeps **Exposed port** name-based across matching input and output directions.
