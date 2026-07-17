# Changelog

## Unreleased

### Added

- **Beta distribution and human-gated release path** — the distribution is now
  `hypergraph-ai` at `0.2.0b1` with a Beta classifier while the import remains
  `hypergraph`. A GitHub Release is the only production trigger; separate
  Trusted Publishing jobs upload prebuilt artifacts to PyPI or, after an
  explicit manual dry-run input, TestPyPI. A distribution verifier checks the
  wheel and source distribution package trees, viz/inspect assets, changelog,
  and forbidden development directories before publication.

- **Native execution inspect mode** — before, users correlated result status,
  logs, map indexes, values, and failures by hand. Now `SyncRunner` and
  `AsyncRunner` accept `inspect=True` on `run()`, `map()`, `start_run()`, and
  `start_map()`; settled `RunResult.inspect()` / `MapResult.inspect()` return
  one explicit locally interactive display. Current inspection needs no
  checkpointer, handles remain control-only, degraded results disclose values
  that were not captured, and trusted saved notebook output remains interactive
  without a kernel while carrying the documented bounded sensitive values.
  Untrusted saved output retains native expandable terminal evidence instead
  of claiming that active HTML can run through host security policy.

- **Background run and map handles** — `SyncRunner.start_run()` /
  `start_map()` and `AsyncRunner.start_run()` / `start_map()` return
  process-local `SyncHandle` / `AsyncHandle` controls with only `done`,
  cooperative `stop(info=...)`, and `result(raise_on_failure=...)`. Async start
  calls return a handle without `await`, and cancelling one result waiter does
  not cancel framework-owned execution. Blocking runner behavior is unchanged.

- **Truthful stopped-map scope** — curtailed background maps keep only real
  claimed `RunResult` children and expose the original scope through
  `MapResult.requested_count` and `unstarted_item_indexes`. Parent event,
  checkpoint, and OTel outcomes align on `STOPPED`; existing batch counts
  continue to count real outcomes only.

- **Background-control guides and examples** — added the task-based control
  guide plus runnable synchronous and asynchronous examples covering immediate
  return, retrieval policy, cooperative stop, and waiter cancellation.

- **Truthful restored-map provenance** — checkpoint-skipped map children now return `RunResult(restored=True)` with a visible non-error `NodeRecord(status="restored")`. `MapResult.restored_count`, `MapLog.restored_count`, `RunEndEvent.batch_restored_items`, and `hypergraph.batch.restored_items` expose the restored subset while completed counts stay inclusive. Duration averages include only fresh completed items with real logs.

- **Internal-step inspection escape hatch** — `get_steps(..., show_internal=True)`, SQLite `steps(...)`, and `RunInspector.steps(...)` can expose retention carrier rows for debugging; public views hide them by default while state reconstruction still folds them.

- **`WorkflowStoppedError`** — a bare rerun of a persisted stopped workflow now fails before events or persistence writes. Pass a non-empty runtime mapping to resume the same lineage, or `override_workflow=True` to fork.

- **HyperTable child fingerprints** — child rows now have real fingerprints computed from the child's source inputs, child graph node hashes, and component config hashes (scoped to the child graph, not the parent). Re-inserting a parent skips children whose inputs and graph definition haven't changed. Makes insert naturally resumable after crashes.

- **HyperTable `on_error` policy** — `HyperTable(..., on_error="store")` writes error rows instead of raising on derivation failure. Error rows preserve source columns and identity with `_status="error"` and `_error="{ExceptionType}: {message}"`. Successful siblings are unaffected. Error rows are retried (not skipped) on the next insert/sync. Default `on_error="raise"` preserves backward compatibility. Works for both parent and child rows, and with both `SyncRunner` and `AsyncRunner`.

- **`include_status` on read methods** — `get()`, `children()`, `filter()`, and `filter_children()` accept `include_status=True` to expose `_status` and `_error` fields. Without it, these internal fields are stripped.

- **`SyncResult.errors`** — `sync()` with `on_error="store"` populates `SyncResult.errors: tuple[ErrorRow, ...]` for programmatic inspection of which items failed, alongside the existing `errored` count.

- **Reserved column name validation** — identity and source columns named `_status`, `_error`, `_row_fingerprint`, `_write_gen`, `_parent_id`, or `_provenance_*` are rejected at graph analysis time with a clear error message.

### Fixed

- **HyperTable definition fingerprints harden to construction-time
  `hash_definition`** — before, a derive node that was a bound method of a
  configured object (`summarizer.summarize` with `model="gpt-4"` vs
  `model="o3"`) produced the SAME recipe fingerprint, so changing the
  configuration silently skipped the re-derive; and a dynamically-created
  function (exec/eval, no retrievable source) hashed its `repr` — a
  per-process memory address — so its rows re-derived on every run. Now a
  producing node's recipe identity is its `definition_hash`, captured ONCE at
  node construction via the repo's single definition-identity function
  `hash_definition`: bound methods mix in the instance's configuration,
  dynamic functions hash their bytecode, and builtins hash their qualified
  name. Because identity is frozen at construction, instance state that
  mutates during execution (a call counter, a cache, a client) does NOT drift
  the recipe — three inserts through the same node stamp one fingerprint.
  One-time migration consequence: rows whose recipes involve bound methods,
  dynamically-created functions, builtins, partials, or callable objects
  carry a changed fingerprint and re-derive once on the next
  `insert`/`update`/`sync`; component-config and bound-value journal entries
  re-journal under content hashes, and a functionless subgraph producer's
  journal entry re-keys from a meaningless shared hash-of-`None` to the
  node's own definition hash. Rows derived from ordinary module-level
  functions keep their exact previous fingerprint (both schemes hash the
  function's source text) and are not re-derived. Callable objects whose
  state cannot be fingerprinted now fail loudly with guidance to define
  `cache_key()` instead of drifting forever, and a non-callable that carries
  no `definition_hash` is rejected instead of silently hashing its repr.
  Routed union columns (several producers writing one column): a named
  index's recipe fingerprint is now the order-free combination of EVERY
  producer's recipe — previously it hashed the producer tuple's repr, which
  differed in every process, so such indexes always read as stale; existing
  union-column indexes flag stale once more and then stay current. And
  `explain()` no longer attributes a union column to whichever producer was
  listed first in the graph: it returns
  `{"producers": {<node name>: {"provenance", "source"}}}` with every
  producer labeled (single-producer columns keep the flat
  `{"provenance", "source"}` shape); row-actual attribution would require a
  durable producer identifier on the row and is not recorded.

- **Truthful notebook scheduler availability** — before, an
  `add_callback`-only kernel could look cross-thread capable while lacking the
  delayed owner-thread call needed for the 250 ms live-inspection update. Now
  delayed calls and cross-thread marshalling are checked independently. When a
  nonterminal view lacks a required capability, Hypergraph creates a closed
  `Live inspection unavailable` initial snapshot and does not subscribe to the
  inspection session. That initial notebook record is not settled execution
  truth; settled truth remains available through `result.inspect()` or
  `batch.inspect()` after the run or batch returns. An already-terminal initial
  artifact remains a closed `Saved snapshot`. A scheduler can also reject
  `call_later()` only after a worker's callback reaches the owner thread. That
  late owner-thread delayed-arm rejection now closes and detaches the live
  observer. If its display channel still works, Hypergraph writes one
  best-effort stale `Live inspection unavailable` settlement from the latest
  bounded artifact; the rejected payload is never shown as live. Failure of
  that final display update is observational, and the observer remains closed.
  This observer settlement does not change settled execution truth; collected
  `result.inspect()` or `batch.inspect()` remains authoritative.

- **Executable inspect recovery and nested failure attribution** — before, a
  copied full-renderer snippet dereferenced `None` or raised `StopIteration`
  when a transient failure disappeared on recovery, and full-renderer
  run-boundary, batch-boundary, and start-failure views omitted the recovery
  policy already shown by the native summary. Now both surfaces use captured
  sync/async provenance. Each retry assignment is inside `try`/`except` and
  uses `error_handling="continue"`: a persistent infrastructure exception
  prints its real type and message without reading an unbound result, while a
  transient boundary prints the settled successful result or batch. A returned
  failed result prints its real run/item error; map evidence uses
  `batch.failures` or the original item position and never a nonexistent
  `MapResult.error`. For sparse run-boundary results, the snippet translates the
  original item index around `unstarted_item_indexes` before indexing
  `batch.results`; it fails closed when that item never started or is outside
  the requested scope. Unknown provenance still emits no runner call.
  Before, primary **Show failure** selection on a nested mapped graph could stop
  at the aggregate container (`review_group`) and show its list input. Now the
  full inspector and native summary correlate the containing outer item to the
  explicit slash-qualified failing leaf, such as
  `review_group/review_customer`, with its scalar failing input while retaining
  distinct peer failures. Correlation is established from raw Python evidence
  before error/input presentation serialization and carried by an opaque
  internal occurrence identity: changing `repr()` output is never identity,
  no object address, input value, or secret enters the key, and correlation
  never invokes caller-defined equality or hashing for workflow IDs or captured
  values; captured values are correlated only by object identity. Distinct
  executions retain separate identities even when they reuse the same scalar
  and exception objects and record equal durations. A missing exact leaf fails
  closed at the run boundary instead of borrowing the container.

- **Trust-safe saved inspect evidence** — before, a notebook that treated new
  output as untrusted could strip scripts, styles, iframes, and identifiers,
  leaving a blank terminal record even though Python had settled at
  `partial / 2 completed / 1 failed`. Now terminal and stale channels include
  one small native `<details>` summary derived from the existing bounded
  payload. It exposes `First failure of N`, original item, qualified node,
  bounded inputs, exception evidence, a result-evidence snippet, and
  `docs/05-how-to/debug-workflows.md`. A complete safe exception keeps its
  exact node/run/batch label. Repr-backed evidence uses **Exception preview
  (bounded repr)**, truncated previews retain the original character count,
  and a placeholder uses **Exception details unavailable** with its reason. An
  opaque repr keeps its exception type once, while a repr already beginning
  with the type is not duplicated. Copy-faithful whitespace, valid
  `<pre><code>` nesting, and copy-inert wrap opportunities preserve inputs,
  exceptions, and recovery snippets without overflowing a 360px page or
  altering copied text. Status-only failures contribute once to
  `First failure of N` without duplicating stronger evidence. **Exact run
  exception** and **Exact batch exception** remain
  separate from attributable node evidence. Generated recovery snippets use
  only public runner/result APIs. Sync snippets call `runner.run(...)` or
  `runner.map(...)` directly; async snippets use `await runner.run(...)` or
  `await runner.map(...)`. If the runner kind was not captured, recovery code
  is unavailable instead of silently choosing sync. Returned-value snippets
  rerun with `error_handling="continue"` before reading a result. Before, an
  async or repr-backed failure could show sync-only code or an exact heading;
  after, the call syntax and label match the captured evidence. Trusted output
  still gets the full sandboxed portable inspector and hides the compact
  summary only when active HTML runs and the frame retains a non-empty local
  `srcdoc`.
  Explicit failures without a stable node match retain their own facts instead
  of borrowing a same-name node. Hypergraph never auto-trusts or signs a
  notebook, calls a server trust endpoint, weakens the iframe sandbox, changes
  ACK authentication, or adds a public transport setting.

- **Portable saved inspect delivery** — before, notebook hosts that isolate
  each saved output could show the first `pending / 0` shell because later
  payload scripts could not reach sibling output documents. Now the terminal
  channel is a self-contained portable inspector. The normal capable path
  still has exactly two physical outputs because `DisplayHandle.update()`
  replaces that channel in place. When the kernel environment reports exact
  `jupyter-server-nbmodel==0.1.1a4`, Hypergraph uses a best-effort append path
  because that executor drops `update_display_data`: ordinary updates retain
  hidden coalesced payload-only history, then settlement adds one terminal
  physical record. Shared Jupyter hides the portable fallback only after the
  original iframe accepts and applies the authenticated update; an
  isolated-output host can open the terminal record alone. Missing or
  unrecognized versions keep the normal update path. A
  separate server environment cannot be inferred from kernel package metadata.

- **Observational inspect serialization** — captured node inputs, outputs, and
  requested map inputs now use a private `CapturedMapping` snapshot adapter.
  The shallow snapshot supports `copy.copy`, `copy.deepcopy`,
  `dataclasses.asdict`, and pickle round trips; captured mappings are not stored
  as `MappingProxyType`. Separately, structured source-value rendering accepts
  only exact built-in containers, ordinary dataclasses, recognized Pydantic
  models, exact NumPy/pandas adapters, and a user-supplied `MappingProxyType`
  backed by an exact `dict`. Trusted NumPy, pandas, and Pydantic adapters use
  canonical class provenance, not mutable public aliases.
  Within documented rank and size limits, exact arrays with canonical NumPy
  1.x and 2.x `ndarray` provenance stay structured. Exact pandas DataFrames
  require a recognized trusted NumPy-backed internal storage layout: standard
  NumPy-backed storage Hypergraph knows how to inspect. An allowed pandas
  version with an unrecognized internal storage layout now becomes a bounded
  `unsupported DataFrame storage` placeholder without calling DataFrame `repr`.
  An ExtensionArray-backed DataFrame—one whose data blocks, row axis, or column
  axis use extension storage—gets the narrower
  `unsupported extension-backed DataFrame` result without invoking extension
  hooks. This is an implementation
  safety boundary, not an all-version guarantee. DataFrame `repr` delegates to
  extension hooks, so both placeholders bypass it. Unsupported subclasses and
  custom protocols use a bounded whole-value `repr` fallback. A proxy backed by
  a custom mapping uses the same fallback. Custom `repr` remains ordinary
  Python user code, so Hypergraph cannot prevent or undo its side effects;
  raised errors become placeholders without replacing the run status.

- **Background inspect workflow identity** — checkpointer-backed sync and async
  `start_run(..., inspect=True)` calls that omit `workflow_id` now bind their
  generated ID before restored or node evidence is published. The settled
  result and inspection view agree while handles remain control-only.

- **Checkpointer run-filter parity** — async Memory/SQLite and SQLite sync reads now compose `graph_name`, inclusive UTC-normalized `since`, status, and parent filters before limit. Omitting `parent_run_id` returns all runs; explicit `None` returns top-level runs only. `count_runs()` uses the same three-state parent contract.

- **Source-derived fork IDs** — before, `runner.run(..., fork_from="job-1")` generated an unrelated `run-...` ID; now it yields `job-1-fork-<hex>`. Explicit targets remain exact, retries keep generic runner IDs, and missing/nested implicit sources fail without creating a run.

- **`set_children` parent scoping** — the cleanup predicate in `set_children` now includes `_parent_id`, preventing accidental deletion of another parent's children when child identity values overlap.

### Changed

- **`MapResult.failed` now mirrors the aggregate status (alpha breaking
  change)** — `batch.failed` is True only when `status == RunStatus.FAILED`
  (at least one item failed and none completed), matching `RunResult.failed`.
  Use the new `batch.any_failed` (`bool(batch.failures)`) for "did anything
  fail" regardless of status. Consequences: a 99/100-success batch is
  `partial`, not `failed`, and `batch.stopped` and `batch.failed` can no
  longer both be True — a curtailed stopped batch with real attempted-item
  failures reports `stopped=True, failed=False, any_failed=True`.
  ([#296](https://github.com/gilad-rubin/hypergraph/issues/296), decision
  [#251](https://github.com/gilad-rubin/hypergraph/issues/251))

- **Active workflow identity covers every execution shape** — a runner permits
  only one active execution per `workflow_id`; duplicate blocking/background
  starts fail before a second handle is returned. Different IDs are
  independently controllable without a parallelism or start-order guarantee.

- **GraphNode boundary projection (breaking refinement)** — `Graph.as_node()` is flat by default again: a wrapped graph's inputs and outputs appear in the parent graph under their local names. Use `Graph.as_node(namespaced=True)` to project a boundary under the resolved GraphNode name, e.g. `retrieval.query` and `retrieval.docs`. ([#97](https://github.com/gilad-rubin/hypergraph/issues/97))

  **Why:** common parameters such as `query`, `messages`, and `config` often intentionally belong to the parent flow. Namespacing is still available when sibling subgraphs need independent parameters with the same local name.

  **Migration.** Code that was updated for the earlier namespaced-by-default behavior can usually remove the prefix:

  ```python
  # Before
  runner.run(outer, {"inner.x": 5})

  # After, for the default flat boundary
  runner.run(outer, {"x": 5})
  ```

  Keep the prefix by opting into a namespaced boundary:

  ```python
  outer = Graph([inner.as_node(namespaced=True)])
  runner.run(outer, {"inner.x": 5})
  ```

  Namespaced inputs also accept nested-dict sugar:

  ```python
  runner.run(outer, {"inner": {"x": 5}})
  ```

  Use `.expose(...)` to bring selected namespaced ports back into the parent flat flow:

  ```python
  retrieval = retrieval_graph.as_node(namespaced=True).expose("query")
  generation = generation_graph.as_node(namespaced=True).expose("query")
  outer = Graph([retrieval, generation])
  runner.run(outer, {"query": "what is hypergraph?"})
  ```

- **Resolved GraphNode port addresses everywhere** — `GraphNode.inputs`, `GraphNode.outputs`, `GraphNode.data_outputs`, `graph.inputs`, runtime values, bind keys, checkpoint step values, `wait_for`, visualization, and Mermaid all use the same parent-facing port addresses. A cyclic value such as `messages` may appear in both a GraphNode's inputs and outputs.

- **Expose replaces, not aliases** — On a namespaced GraphNode, `.expose("query", answer="final_answer")` replaces `retrieval.query` / `retrieval.answer` at that boundary with `query` / `final_answer`. Expose targets current local port names, not already-projected addresses. Multiple GraphNodes may expose inputs to the same parent input; duplicate aliases inside one GraphNode are a build-time error. A single GraphNode may use the same address as both input and output only when it is the same local cyclic seed/update port.

- **Run-time override of a bound value emits a `UserWarning`** — Passing a value at `runner.run(...)` for a key already present in `inputs.bound` is allowed but warned. The warning shows the old and new value for primitive types and a generic message for opaque types, so accidental overrides surface in test logs.

- **Bind precedence is parent-first at a projected address** — If an inner graph bind and an outer graph bind project to the same parent-facing address, the outer bind wins and emits a warning when the effective bound inputs are computed. If sibling inner graph binds project different values to the same flat address, graph construction errors instead of choosing one silently. Runtime values can still override the effective bind and emit the warning above.

- **`rename_inputs(...)` and `rename_outputs(...)` target local names** — GraphNode renames operate on the current local port names before boundary projection. `map_over(...)` and `clone` accept either current local names or projected parent-facing input addresses, then normalize to local names internally. Changing the GraphNode name recomputes namespaced addresses; exposed aliases stay flat.

- **GraphNode boundary hashes include the projected surface** — `definition_hash` / `structural_hash` now include boundary namespacing, exposed-port mappings, projected inputs/outputs, and local renames. Existing checkpoint compatibility and cache keys may change after upgrading graphs that use nested composition.

- **Nested dict input sugar is only for namespaced addresses** — Flat GraphNodes no longer accept `{"inner": {"x": ...}}` as a way to address child inputs. Pass the flat key directly (`{"x": ...}`), or opt into `as_node(namespaced=True)`.

## March 2026

### Added

- **Checkpoint lineage exceptions** — `WorkflowAlreadyCompletedError`, `GraphChangedError`, `WorkflowForkError`, and `InputOverrideRequiresForkError` for explicit resume/fork guidance.
- **`checkpoint=` on `runner.run()`** — explicit fork entrypoint from a saved checkpoint snapshot (`values + steps`).
- **First-class fork/retry helpers** — low-level `SqliteCheckpointer.fork_workflow()` / `retry_workflow()` (sync) and `fork_workflow_async()` / `retry_workflow_async()` (async) prepare lineage-aware checkpoints when you need manual control.
- **Graph `structural_hash`** — structure-level compatibility hash used to guard same-`workflow_id` resumes.
- **`graph.describe()`** — compact human-readable graph summary covering scoped inputs, bound values, outputs, and active nodes. Type hints are shown by default, with `show_types=False` for name-only output.
- **Run lineage metadata** — persisted `forked_from`, `fork_superstep`, `retry_of`, `retry_index` fields on runs.
- **Gate decision value persistence** — gates now emit internal `_gate_name` values so routing intent is checkpoint-visible and reconstructible.
- **Auto-generated workflow IDs for `run()`** — when a checkpointer is configured and `workflow_id` is omitted.
- **`override_workflow` on `run()`** — convenience auto-fork mode: when a `workflow_id` already exists, pass `override_workflow=True` to branch to a fresh lineage instead of raising strict resume errors.
- **Workflow-id based forking/retrying on `run()`** — `fork_from=` and `retry_from=` remove manual checkpoint plumbing for common branch/retry flows.
- **Checkpointer naming convention updated** — sync helpers are now `fork_workflow()` / `retry_workflow()`, async helpers are `fork_workflow_async()` / `retry_workflow_async()`.
- **Persisted paused workflows** — checkpointers now store interrupt-paused runs as `PAUSED` instead of overloading `ACTIVE`, and CLI/dashboard views expose paused runs distinctly.

### Changed

- **Resume contract is strict** — same `workflow_id` now means same lineage only:
  - completed workflows are terminal
  - runtime input overrides require explicit fork
  - structural graph changes require fork
- **Checkpoint state reconstruction** — restore now replays version counters from step history (instead of remap-style flattening), improving cycle correctness and stale-node detection.
- **Unified startup scheduling** — first-run readiness is predecessor-driven for both implicit and explicit edge graphs (no split behavior by edge mode).
- **Canonical graph scope** — execution scope is graph-configured (`with_entrypoint`, `select`, `bind`) and shared by scheduler, validation, and visualization.
- **Runtime scope overrides removed** — passing runtime `select=` or `entrypoint=` to runners now raises `ValueError`. Configure scope on the graph instance instead.
- **Cycles require constructor entrypoint** — constructing a cyclic graph without `Graph(..., entrypoint=...)` now raises `GraphConfigError`.
- **Internal output injection tightened** — user-provided values for edge-produced internal parameters are rejected deterministically.
- **Visualization defaults simplified** — `.visualize()` now shows unbound external inputs by default via `show_inputs=True`, keeps bound inputs hidden unless `show_bounded_inputs=True`, and never renders shared params as external inputs.
- **Visualization edge contract tightened** — rendered views now follow the Python-precomputed edge set (no JS-only transitive pruning), so what you see matches the canonical NetworkX topology.
- **Implicit producer shadow-elimination** — for contested input `p`, edge `u -> v (p)` is removed iff every valid path from `u` to `v` for `p` crosses another producer of `p` first; unresolved cases raise `GraphConfigError` at build time.

## Recent Merged PRs

### [PR #61](https://github.com/gilad-rubin/hypergraph/pull/61) (Merged February 28, 2026) - DiskCache HMAC integrity hardening

- Added HMAC-SHA256 signing for `DiskCache` payloads using a per-cache-directory secret key.
- `DiskCache.get()` now verifies HMAC integrity **before** deserialization (`pickle.loads`), preventing untrusted/tampered payload deserialization.
- Added atomic HMAC key initialization (`O_CREAT | O_EXCL`) to avoid race conditions when multiple processes initialize the same cache directory.
- Added broader recovery behavior for corrupted or legacy cache entries:
  - non-bytes payloads are evicted and treated as cache misses
  - missing/invalid HMAC metadata is evicted and treated as cache misses
  - deserialization failures evict the bad entry and return miss
- Expanded disk cache integrity tests to cover tampering, key initialization races, and bad metadata handling.

### [PR #59](https://github.com/gilad-rubin/hypergraph/pull/59) (Merged February 26, 2026) - Non-TTY progress fallback

- Added non-TTY mode to `RichProgressProcessor` for CI/piped environments where live Rich bars are not appropriate.
- Added milestone-based map progress logging at 10%, 25%, 50%, 75%, and 100%.
- Added explicit mode control via `RichProgressProcessor(force_mode="auto" | "tty" | "non-tty")`.
- Added non-TTY node start/end/failure plain-text logging for non-map runs.
- Cleaned up non-TTY tracking internals after review (removed unused fields and simplified state).
- Added dedicated tests for non-TTY behavior.

### [PR #58](https://github.com/gilad-rubin/hypergraph/pull/58) (Merged February 25, 2026) - PR workflow and contributor docs updates

- Added `.github/PULL_REQUEST_TEMPLATE.md` with a problem + before/after structure.
- Updated internal skills and contributor instructions to reuse the PR template and reduce duplicated guidance.
- Removed legacy "entire session" tracking guidance from project docs/instructions.

## February 2026

### Added

- **`with_entrypoint(*node_names)`** — Graph method that narrows execution to start at specific nodes, skipping upstream. Works for DAGs and cycles. Returns a new Graph (immutable, chainable). Upstream nodes are excluded from the active set and never execute at runtime.
- **Select-aware InputSpec** — `graph.select("a").inputs.required` now shows only what's needed to produce output "a", not the full graph. Previously `select()` only filtered returned outputs.
- **Runtime select overrides removed** — `runner.run(..., select=...)` is no longer supported. Configure output scope on the graph with `graph.select(...)` instead.
- **`entrypoints_config` property** — `graph.entrypoints_config` returns the configured entry point node names, or `None` if all nodes are active.

### Changed

- **InputSpec is now scope-aware** — `graph.inputs` considers both `with_entrypoint()` (forward-reachable) and `select()` (backward-reachable) when determining required or optional parameters, including cycle bootstrap inputs. Parameters from excluded nodes no longer appear in InputSpec; there is no separate `entrypoints` field.

## January 2026

### Added

- **Event system** — `RunStartEvent`, `NodeStartEvent`, `NodeEndEvent`, and other event types emitted during execution. Pass `event_processors=[...]` to `runner.run()` or `runner.map()` to observe execution
- **RichProgressProcessor** — hierarchical Rich progress bars with failed item tracking for `map()` operations
- **InterruptNode** — human-in-the-loop pause/resume support for async workflows
- **RouteNode & @route decorator** — conditional control flow gates with build-time target validation
- **IfElseNode & @ifelse decorator** — binary boolean routing for simple branching
- **Error handling in map()** — `error_handling` parameter for `runner.map()` and `GraphNode.map_over()` with partial result support
- **SyncRunner & AsyncRunner** — full execution runtime with superstep-based scheduling, concurrency support, and global `max_concurrency`
- **GraphNode.map_over()** — run a nested graph over a collection of inputs
- **Type validation** — `strict_types` parameter on `Graph` with a full type compatibility engine supporting generics, `Annotated`, and forward refs
- **select()** method — default output selection for graphs
- **Mutex branch support** — allow same output names in mutually exclusive branches
- **Sole Producer Rule** — prevents self-retriggering in cyclic graphs
- **Capability test matrix** — pairwise combination testing with renaming and binding dimensions
- **Comprehensive documentation** — getting started guide, routing patterns, API reference, philosophy, and how-to guides

### Changed

- **Refactored event context** — pass event context as params instead of mutable executor state
- **Refactored runners** — separated structural and execution layers
- **Refactored routing** — extracted shared validation to common module
- **Refactored graph** — extracted validation and `input_spec` into `graph/` package
- **Renamed `with_select()` to `select()`** for clearer API semantics
- **Renamed `inputs=` to `values=`** parameter in runner API

### Fixed

- **Bound values no longer deep-copied** — nested graphs with bound non-copyable objects (e.g., embedders with thread locks) now work correctly. Bound values are intentionally shared (not copied), matching dependency injection patterns. Non-copyable signature defaults now raise `GraphConfigError` with helpful guidance to use `.bind()` instead
- Preserve partial state from same-superstep nodes on failure
- Support multiple values per edge in graph data model
- Deduplicate `Graph.outputs` for mutex branches
- Reject string `'END'` as target name to avoid confusion with `END` sentinel
- Python keyword validation in node names
- `Literal` type forward ref resolution
- Generic type arity check enforcement
- Renamed input/output translation in `map_over` execution
