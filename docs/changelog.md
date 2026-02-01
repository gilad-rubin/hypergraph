# Changelog

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

- Preserve partial state from same-superstep nodes on failure
- Support multiple values per edge in graph data model
- Deduplicate `Graph.outputs` for mutex branches
- Reject string `'END'` as target name to avoid confusion with `END` sentinel
- Python keyword validation in node names
- `Literal` type forward ref resolution
- Generic type arity check enforcement
- Renamed input/output translation in `map_over` execution
