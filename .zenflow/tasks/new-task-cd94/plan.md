# Spec and build

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Agent Instructions

Ask the user questions when anything is unclear or needs their input. This includes:
- Ambiguous or incomplete requirements
- Technical decisions that affect architecture or user experience
- Trade-offs that require business context

Do not make assumptions on important decisions — get clarification first.

---

## Workflow Steps

### [x] Step: Technical Specification
<!-- chat-id: 59974add-b1fa-449f-bbae-ef2a41b0edb1 -->

Completed. See `spec.md` for full details.

---

### [x] Step: Partial values in run() on failure
<!-- chat-id: 8d4e1597-287a-459c-ba47-d10a2424b4b6 -->

Refactor `SyncRunner.run()` and `AsyncRunner.run()` to return partial values (state accumulated before the error) instead of `values={}` when execution fails.

- Split state initialization out of `_execute_graph` so `run()` has access to state even after an exception
- Use `filter_outputs(state, graph, select)` to populate `RunResult.values` on failure
- Both sync and async runners need identical changes
- Add tests: run a graph where node 2 of 3 fails, verify `values` contains node 1's output

---

### [x] Step: error_handling parameter in runner.map()
<!-- chat-id: 79bf0846-bb13-4e69-af2e-7f6022f1eb42 -->

Add `error_handling: Literal["raise", "continue"] = "raise"` parameter to both `SyncRunner.map()` and `AsyncRunner.map()`.

- Add `ErrorHandling` type alias in `_shared/types.py`
- **SyncRunner**: Check result status after each iteration, raise on first failure if `"raise"`
- **AsyncRunner unlimited**: After `asyncio.gather`, scan results and raise first failure if `"raise"`
- **AsyncRunner worker pool**: Use `asyncio.Event` to signal workers to stop on failure if `"raise"`
- `"continue"` mode: return all results as-is (some may be FAILED)
- Add tests for both modes in both runners

---

### [x] Step: error_handling in as_node().map_over()
<!-- chat-id: f789059b-ca53-4b31-9625-fd4414e17386 -->

Propagate error_handling through `GraphNode.map_over()` to executors.

- Add `error_handling` parameter to `GraphNode.map_over()`
- Store as `_error_handling` on GraphNode, expose via `map_config`
- Update `SyncGraphNodeExecutor` and `AsyncGraphNodeExecutor` to pass `error_handling` to `runner.map()`
- Update `_collect_as_lists` to use `None` placeholders for failed items (preserving list length)
- Add tests: `as_node().map_over("x", error_handling="continue")` with partial failures

---

### [x] Step: Final verification and cleanup
<!-- chat-id: ac0a0bda-9614-430d-9545-782df8805c82 -->

- Run full test suite: `uv run pytest tests/ -x`
- Verify no regressions in existing tests
- Write report to `{@artifacts_path}/report.md`

### [x] Step: Code Quality
<!-- chat-id: 5ddf959f-97a3-4978-88be-328cb7609d5b -->

review code quality based on CLAUDE.md and other rule files and cleanup the code

### [ ] Step: documentation
<!-- chat-id: ad049605-bdb4-408a-8b38-9afa72902ec3 -->

Updated documentation for error_handling feature:
- docs/06-api-reference/runners.md: Added error_handling param to SyncRunner.map() and AsyncRunner.map(), added partial values on failure section
- docs/06-api-reference/nodes.md: Added error_handling param to GraphNode.map_over() with example
- docs/05-how-to/batch-processing.md: Rewrote Error Handling section with fail-fast, continue, and nested graph examples

### [x] Step: Re-examine edge cases, look at capabilities matrix
<!-- chat-id: 85aa9f5e-aca3-401f-b0b7-731c19540f9f -->

add tests for edge cases, "red-team" the implementation to find issues and fix them / surface for discussion

Completed: Added 22 edge case tests in `tests/test_error_handling_edge_cases.py` covering:
- All items fail (continue mode) - sync, async, async+concurrency
- Multiple failures (continue mode) - correct None positioning
- First item fails (boundary case) - raise and continue
- Single item - success and failure
- Empty input lists - raise and continue
- Product mode + error_handling - continue and raise
- Renamed outputs + continue mode
- map_over: all-None output, multiple failures, single item, empty input
- map_over raise mode: outer run() correctly reports FAILED

Findings surfaced:
- Multi-output inner graphs + map_over is untested (not error_handling-specific)
- Capabilities matrix does NOT include error_handling as a dimension; adding it would double the full matrix (~16K tests). Current targeted tests provide sufficient coverage.
- All 950 tests pass (22 new + 928 existing).
