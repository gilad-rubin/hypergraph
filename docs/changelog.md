# Changelog

## March 2026

### Added

- **Checkpoint lineage exceptions** — `WorkflowAlreadyCompletedError`, `GraphChangedError`, `WorkflowForkError`, and `InputOverrideRequiresForkError` for explicit resume/fork guidance.
- **`checkpoint=` on `runner.run()`** — explicit fork entrypoint from a saved checkpoint snapshot (`values + steps`).
- **First-class fork/retry helpers** — low-level `SqliteCheckpointer.fork_workflow()` / `retry_workflow()` (sync) and `fork_workflow_async()` / `retry_workflow_async()` (async) prepare lineage-aware checkpoints when you need manual control.
- **Graph `structural_hash`** — structure-level compatibility hash used to guard same-`workflow_id` resumes.
- **Run lineage metadata** — persisted `forked_from`, `fork_superstep`, `retry_of`, `retry_index` fields on runs.
- **Gate decision value persistence** — gates now emit internal `_gate_name` values so routing intent is checkpoint-visible and reconstructible.
- **Auto-generated workflow IDs for `run()`** — when a checkpointer is configured and `workflow_id` is omitted.
- **`override_workflow` on `run()`** — convenience auto-fork mode: when a `workflow_id` already exists, pass `override_workflow=True` to branch to a fresh lineage instead of raising strict resume errors.
- **Workflow-id based forking/retrying on `run()`** — `fork_from=` and `retry_from=` remove manual checkpoint plumbing for common branch/retry flows.
- **Checkpointer naming convention updated** — sync helpers are now `fork_workflow()` / `retry_workflow()`, async helpers are `fork_workflow_async()` / `retry_workflow_async()`.

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
- **Visualization defaults simplified** — external inputs are hidden by default in `.visualize()` and can be toggled with `show_external_inputs` (API) or the side-panel control.
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
- **Runtime select narrowing** — `runner.run(graph, values, select="a")` validates only the inputs needed for output "a". Passing `select` at runtime recomputes InputSpec scoped to the selected outputs.
- **`entrypoints_config` property** — `graph.entrypoints_config` returns the configured entry point node names, or `None` if all nodes are active.
- **`on_internal_override` parameter** — `run()` and `map()` accept `on_internal_override="ignore"|"warn"|"error"` (default: `"warn"`) to control how non-conflicting internal parameter overrides are handled. Contradictory compute+inject inputs always raise `ValueError` regardless of policy.

### Changed

- **InputSpec is now scope-aware** — `graph.inputs` considers both `with_entrypoint()` (forward-reachable) and `select()` (backward-reachable) when determining required, optional, and entrypoint parameters. Parameters from excluded nodes no longer appear in InputSpec.

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
