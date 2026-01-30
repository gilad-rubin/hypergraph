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

### [ ] Step: error_handling in as_node().map_over()

Propagate error_handling through `GraphNode.map_over()` to executors.

- Add `error_handling` parameter to `GraphNode.map_over()`
- Store as `_error_handling` on GraphNode, expose via `map_config`
- Update `SyncGraphNodeExecutor` and `AsyncGraphNodeExecutor` to pass `error_handling` to `runner.map()`
- Update `_collect_as_lists` to use `None` placeholders for failed items (preserving list length)
- Add tests: `as_node().map_over("x", error_handling="continue")` with partial failures

---

### [ ] Step: Final verification and cleanup

- Run full test suite: `uv run pytest tests/ -x`
- Verify no regressions in existing tests
- Write report to `{@artifacts_path}/report.md`

### [ ] Step: Code Quality

review code quality based on CLAUDE.md and other rule files and cleanup the code

### [ ] Step: documentation

update documentation, API reference, README
