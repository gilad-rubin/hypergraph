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

## Retry execution

**Node attempt**:
One actual invocation of a FunctionNode callable within one logical node execution. A cache hit is not a Node attempt.
_Avoid_: Retry step, graph step

**Attempt series**:
The Node attempts that belong to one logical node execution and share one Retry budget.
_Avoid_: Retry run, workflow retry

**Retry budget**:
The total number of Node attempts permitted for one Attempt series, including the initial invocation.
_Avoid_: Retry count, retries

**Retry window**:
An optional elapsed-time boundary for one Attempt series. It includes Node attempts, backoff, cancellation settlement, and process downtime. It stops new attempts but is not proof that active work stopped or a hard cap on return time.
_Avoid_: Total timeout, hard timeout

**Attempt timeout**:
An optional elapsed-time boundary for one Node attempt executing cooperative asynchronous work. When it elapses, cancellation is requested and Hypergraph waits for settlement; it is not proof that the work stopped at the deadline.
_Avoid_: Hard timeout, sync timeout

**Unknown attempt outcome**:
The durable state of a committed Node attempt that started but whose settlement was not witnessed before process loss. Its external side effects may have completed.
_Avoid_: Failed attempt, crashed attempt

## Relationships

- A **Node attempt** belongs to an **Attempt series**; another retry creates another Node attempt, not another graph step.
- A **Retry budget** limits the total Node attempts in one Attempt series and counts the initial invocation.
- A **Retry window** and Retry budget independently limit one Attempt series; whichever prevents the next attempt first ends retry scheduling.
- An **Attempt timeout** applies separately to each Node attempt; a Retry window applies to the whole Attempt series.
- An **Unknown attempt outcome** consumes Retry budget because the committed invocation may have run, but it does not claim success or failure.
- A cache hit creates no **Node attempt**, consumes none of the Retry budget, and opens no Retry window.
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

## Background execution

Vocabulary for background execution. Collection and retrieval are recorded in [ADR 0003](docs/adr/0003-background-maps-collect-before-result-retrieval.md); handle ownership and stopped-batch truth are recorded in [ADR 0004](docs/adr/0004-background-handles-control-live-work.md).

**Execution handle**:
A process-local reference to one live graph execution and its cooperative control channel, through which its Execution result can be retrieved.
_Avoid_: Durable handle, job handle, workflow reference

**Execution result**:
The settled record of what happened during one run or batch, including its terminal outcome and failure data.
_Avoid_: Handle status, handle failure

**Checkpointed execution**:
A graph execution whose completed boundaries are persisted so they can seed a later execution after process loss. Its persisted state does not preserve the liveness or control channel of the original execution.
_Avoid_: Durable job, reconnected handle

**Retrieval policy**:
The caller's choice when retrieving a stored execution result: return the result or raise a captured failure. A retrieval policy does not control which work is scheduled.
_Avoid_: Presentation policy, execution policy

**Settled background batch**:
A background batch that is no longer executing. Settled does not mean successful; the batch may contain completed, failed, paused, or stopped item outcomes.
_Avoid_: Complete batch, successful batch

**Unstarted map item**:
A requested map input that the scheduler never claimed because the batch settled after cooperative stop. It was never attempted, so its original input index remains attributable but it has no RunResult.
_Avoid_: Stopped result, skipped run

### Relationships

- An **Execution handle** controls live work; an **Execution result** describes the settled outcome.
- An **Execution handle** ceases to exist with its owning process, whether or not the execution is checkpointed.
- Resuming a **Checkpointed execution** creates a new live execution from persisted state; it does not reconnect to the previous **Execution handle**.
- A persisted active status describes recorded lifecycle state. It is not proof that a worker is alive and does not grant ownership of that worker.

## Durable host

Vocabulary for the Durable Host V1 program ([PRD 0017](docs/prd/0017-durable-host-v1-program.md); decisions in ADRs 0005–0008 and the [2026-07-23 amendment package](docs/research/2026-07-23-durable-host-amendments.md)). Intent, not shipped behavior, until the ticket tree lands.

**Definition**:
A root graph bound to its runner and deployment identity, accepted by `serve(...)`. New submission always names a loaded Definition.
_Avoid_: Registered workflow, catalog entry, app

**Definition identity**:
The typed tuple `DefinitionId(name, deployment_version, structural_hash)` pinned at first accepted submit. The code hash is recorded for diagnosis only and never decides claim eligibility.
_Avoid_: Version string (that is only one part), code hash, graph hash

**Host**:
The Definition-bound object that owns new Run submission, Batch submission, explicit fork, and worker lifecycle. It exposes one RunHomeClient and never copies its verbs.
_Avoid_: Server, control plane, orchestrator service

**Run Home**:
The existing checkpointer plus coordination facts in the same transactional store. StepRecords remain the sole execution journal.
_Avoid_: Second journal, event store, broker state

**RunHomeClient**:
The single backend-neutral surface for existing work — get, list, watch, stop, rerun, and answer — accepting RunRef or the applicable BatchRef. Constructing one requires no Definition code; it cannot submit new work.
_Avoid_: Operator wrapper, admin API, app client

**RunRef / BatchRef**:
Immutable, serializable addresses for one Run or Batch. They expose no liveness, status, result, or control methods, and are never called durable handles.
_Avoid_: Durable handle (banned per ADR 0004), job token, reconnectable handle

**Start fingerprint**:
The dedup identity of a submission: complete Definition identity, normalized inputs, effective Batch configuration, and requested start time. Worker identity and submission time are excluded.
_Avoid_: Idempotency key (callers never manage one), request hash

**Durable Batch**:
An immutable manifest of unique stable logical item keys, each mapped to one independent child Run, with keyed outcomes and explicit unstarted items. Never a durable parent MapResult array.
_Avoid_: Persisted MapResult, batch job array

**Logical item key**:
The caller-chosen stable key naming one requested Batch item; child outcomes are keyed by it regardless of completion order.
_Avoid_: Item index, result position

**Batch tolerance**:
Optional manifest-pinned count and percentage failure thresholds. Either trips only when failure-equivalent children strictly exceed it; the percentage denominator is always the total manifest item count.
_Avoid_: Error rate, failure policy

**Rerun**:
Creating a new Run under a new workflow id from a source Run or Batch with its pinned Definition identity and inputs intact, recording `retry_of` lineage. A Batch rerun may narrow to named source item keys. Never accepts input overrides.
_Avoid_: Redrive (an SQS dead-letter term), retry (node-owned), resume (continues the same Run)

**Fork**:
Explicit, compatibility-checked migration of parked work to a different Definition identity, recording `forked_from` lineage and a migration reason. Never shares the rerun operation.
_Avoid_: Upgrade, in-place migration, redrive

**Recovery-exhausted condition**:
The coordination condition set when repeated recovery without committed graph progress reaches a Run's pinned recovery cap. It is never a WorkflowStatus, and only rerun revives the work.
_Avoid_: Failed run, poison status, dead-letter state

**Unknown effect outcome**:
The surfaced state of a declared external effect that was dispatched but whose settlement was not witnessed. It is never re-dispatched automatically and requires an explicit operator decision.
_Avoid_: Failed effect, lost effect, retryable error

**Durable update sequence**:
The monotonic per-Run (or per-Batch) sequence assigned to every committed fact in its own transaction. `watch(after=cursor)` resumes from it without gaps; live previews are non-durable and never advance it.
_Avoid_: Event offset, log position (no OutputLog exists), progress counter

### Relationships

- The **Host** owns new work; the **RunHomeClient** owns existing work. The Host exposes the client but never duplicates its verbs.
- A process without loaded **Definition** code may use a **RunHomeClient** but can never submit; there is no app-less submit catalog.
- A **RunRef** or **BatchRef** is always safe to store and pass between processes; an Execution handle never is.
- Use-existing dedup applies only to **Start fingerprint**-identical nonterminal work; terminal reuse and fingerprint mismatch are distinct typed conflicts.
- **Rerun** repeats; **fork** migrates. Their lineage relations never merge.
- A **recovery-exhausted condition** parks a Run without touching Retry budgets, Retry windows, or WorkflowStatus.
- An **Unknown effect outcome** extends the Unknown attempt outcome rule to declared external effects: ambiguity is surfaced, never resolved by guessing.

## Materialization

Vocabulary for `hypergraph.materialization` — incremental, declarative tables derived from a source. See [ADR 0001](docs/adr/0001-sync-and-async-derived-table-classes.md) and [ADR 0002](docs/adr/0002-stream-materialization-through-runner-with-sinks.md).

**DerivedTable**:
A table whose rows are derived from a source and kept in sync with it incrementally, persisted in a store. Sync and async are separate classes (`DerivedTable`, `AsyncDerivedTable`).
_Avoid_: Materialized view, computed table, cache

**Derive**:
The function or Graph that turns one source item into one or more output rows.
_Avoid_: Transform, compute, UDF, mapper

**Source**:
What a DerivedTable derives from — either an entity type (root) or another DerivedTable (chained).
_Avoid_: Parent, upstream, input

**Root table**:
A DerivedTable whose source is a type; the only kind that accepts external mutations, and the table that holds the runner for its whole chain.
_Avoid_: Base table, source table

**Chained table**:
A DerivedTable whose source is another DerivedTable; populated only via cascade, never mutated externally.
_Avoid_: Dependent table, child table

**Cascade**:
The always-on propagation of a source change to every dependent DerivedTable, one level at a time.
_Avoid_: Trigger, refresh

**Identity**:
The marker on the source field(s) that stably identify an item and its derived rows.
_Avoid_: Primary key, id field

**Content key**:
The hash of all invalidation-relevant state — ContentKey-marked fields, component configs, derive definition, and output schema — used to skip rows whose inputs are unchanged.
_Avoid_: Cache key, fingerprint, etag

**Explosion**:
A one-to-many derive — one source item yields a list of output rows.
_Avoid_: Fan-out, expansion, flatmap

**Sink**:
A consumer that persists selected run outputs to a store as results stream from the runner, declaring which output ports it writes.
_Avoid_: Writer, output node, exporter

**Streaming map**:
Runner execution (the `.iter()` capability) that yields each item's result as it completes, with backpressure, instead of buffering all results into one `MapResult`.
_Avoid_: Batch map (that is the buffered `MapResult` form)

**Sync (the reconcile operation)**:
The root-table operation that makes the table exactly match a given set of source items — inserting new, re-deriving changed, deleting missing. Named identically on both the sync and async classes; do not read it as "synchronous."
_Avoid_: Refresh, merge

**HyperTable**:
A persistent, incremental table built on a Hypergraph graph. Wraps a graph + a store + an identity declaration. Each node's output is a stored column, and a content-key check decides whether to re-run. Supports child tables via `map_over` grain boundaries. Evolution of `DerivedTable` with graph-native composition.
_Avoid_: Derived table (that's the earlier abstraction)

**Grain**:
One row-cardinality level in a HyperTable. The root has one row per root identity; each `map_over` boundary creates a child Grain with one row per mapped identity.
_Avoid_: Stage, pipeline table

**Lineage**:
The full upstream derivation identity of a stored value: source Grain plus every producing recipe and upstream Lineage. Matching recipe fingerprints without matching upstream Lineage does not make two values the same.
_Avoid_: Recipe fingerprint (that is only one part), history

**Materialization Branch**:
A persisted Lineage plan attached to one HyperTable root. It reuses every matching Materialized Artifact, allocates a stable physical namespace at the first changed Grain or column, and derives missing or stale work from current root rows.
_Avoid_: Superset graph, index table, pipeline branch

**Materialized Artifact**:
One stored derived column or child table at one Grain, identified by its root collection, upstream Lineage, producing recipe, and output. It is shared when more than one registered plan can reach it; it never carries an owner stamp.
_Avoid_: Owned table, index-owned data

**Error policy (`on_error`)**:
A HyperTable constructor parameter: `"raise"` (default, propagate exceptions) or `"store"` (write error rows, continue processing siblings). Applies to `insert()` and `sync()`, not `update()`.
_Avoid_: Error mode, failure strategy

**Error row**:
A row written under `on_error="store"` when derivation fails. Source columns preserved, derived columns `None`, `_status="error"`, `_error` contains the exception string. Retried (not skipped) on the next insert/sync when the fingerprint matches.
_Avoid_: Failed row, bad row

**Row status (`_status`)**:
Internal column: `"complete"` or `"error"`. `None` treated as `"complete"` for migration safety.
_Avoid_: State, health
