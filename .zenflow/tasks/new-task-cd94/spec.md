# Technical Specification: Failure Handling in runner.map() and run()

## Difficulty: Medium

Multiple files affected, async/sync parity required, edge cases around `as_node().map_over()`, but the core logic changes are straightforward.

## Technical Context

- **Language**: Python 3.10+
- **Key files**: `src/hypergraph/runners/sync/runner.py`, `src/hypergraph/runners/async_/runner.py`, `src/hypergraph/runners/_shared/types.py`
- **Executors**: `src/hypergraph/runners/sync/executors/graph_node.py`, `src/hypergraph/runners/async_/executors/graph_node.py`
- **Tests**: `tests/test_runners/test_sync_runner.py`, `tests/test_runners/test_async_runner.py`, `tests/test_exception_propagation.py`

## Requirements

### 1. `runner.run()` — Return partial values on failure

Currently returns `values={}` on failure. Change to capture state accumulated before the error.

**Approach**: In `run()`, catch the exception but also capture the `GraphState` built so far. Use `filter_outputs()` on the partial state to populate `values`.

**Implementation**: Refactor `_execute_graph` to raise an exception that carries the partial state, or restructure `run()` to always have access to state. The cleanest approach: make `_execute_graph` return state even on failure by wrapping the execution differently.

```python
# Option: structured result from _execute_graph
try:
    state = self._execute_graph(graph, values, max_iter)
    output_values = filter_outputs(state, graph, select)
    return RunResult(values=output_values, status=RunStatus.COMPLETED)
except Exception as e:
    # State is lost — need to capture it
```

Best approach: use a custom exception that carries state, or restructure to capture state in a local variable before the error propagates. Since `_execute_graph` calls `run_superstep_sync` which raises on node failure, we can wrap the superstep loop to capture partial state:

```python
def _execute_graph(self, graph, values, max_iterations):
    state = initialize_state(graph, values)
    for iteration in range(max_iterations):
        ready_nodes = get_ready_nodes(graph, state)
        if not ready_nodes:
            break
        state = run_superstep_sync(graph, state, ready_nodes, values, self._execute_node)
    else:
        if get_ready_nodes(graph, state):
            raise InfiniteLoopError(max_iterations)
    return state
```

The problem: when `run_superstep_sync` raises, the state from *before* that superstep is the last good state. We can wrap this:

```python
def run(self, graph, values, *, select=None, max_iterations=None):
    # ... validation ...
    state = initialize_state(graph, values)
    try:
        state = self._execute_graph_from_state(graph, state, values, max_iter)
        output_values = filter_outputs(state, graph, select)
        return RunResult(values=output_values, status=RunStatus.COMPLETED)
    except Exception as e:
        partial_values = filter_outputs(state, graph, select)
        return RunResult(values=partial_values, status=RunStatus.FAILED, error=e)
```

This requires splitting state initialization out of `_execute_graph`. Both sync and async runners need the same change.

### 2. `runner.map()` — Add `error_handling` parameter

**API**:
```python
def map(self, graph, values, *, map_over, map_mode="zip", select=None,
        error_handling: Literal["raise", "continue"] = "raise") -> list[RunResult]:
```

**Behavior**:
- `"raise"` (default): On first `RunResult` with `status=FAILED`, raise `result.error`. For SyncRunner, break the loop. For AsyncRunner with worker pool, signal workers to stop. For AsyncRunner unlimited concurrency, all tasks are already started so check results after gather.
- `"continue"`: Return all `RunResult` objects, some may have `status=FAILED`.

**SyncRunner**:
```python
for variation_inputs in input_variations:
    result = self.run(graph, variation_inputs, select=select)
    results.append(result)
    if error_handling == "raise" and result.status == RunStatus.FAILED:
        raise result.error
return results
```

**AsyncRunner** (unlimited concurrency):
All tasks start concurrently via `asyncio.gather`. Since `run()` never raises (returns RunResult), gather always completes. After gather, check results:
```python
results = await asyncio.gather(*tasks)
results = list(results)
if error_handling == "raise":
    for r in results:
        if r.status == RunStatus.FAILED:
            raise r.error
return results
```

**AsyncRunner** (worker pool):
```python
stop_event = asyncio.Event()

async def _worker():
    while not stop_event.is_set():
        try:
            idx, v = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        result = await self.run(graph, v, select=select, ...)
        results_list.append(result)
        order.append(idx)
        if error_handling == "raise" and result.status == RunStatus.FAILED:
            stop_event.set()
            return
```
After workers finish, if any failed and `error_handling == "raise"`, raise the first error.

### 3. `as_node().map_over()` — Propagate error_handling

The `GraphNodeExecutor._collect_as_lists()` currently raises on first failure. This is correct for the default `"raise"` mode. For `"continue"` mode in `map_over`, we have a problem:

**The user noted**: "we need to be careful because it should always return a list of items and not modify its length"

This is the key edge case. When `error_handling="continue"` and some items fail:
- The output lists must maintain the same length as the input lists
- Failed items need placeholder values

**Proposed approach**: Use `None` as placeholder for failed items. The output list length always matches input length.

```python
def _collect_as_lists(self, results, node):
    collected = {name: [] for name in node.outputs}
    for result in results:
        if result.status == RunStatus.FAILED:
            for name in node.outputs:
                collected[name].append(None)  # placeholder
        else:
            renamed_values = node.map_outputs_from_original(result.values)
            for name in node.outputs:
                collected[name].append(renamed_values.get(name))
    return collected
```

**How to pass `error_handling` to the executor**: The executor calls `self.runner.map()`. We need the executor to know the error_handling mode. Options:
1. Store it on the GraphNode (via `map_over(..., error_handling=...)`)
2. Pass it through the runner's execution context

Option 1 is cleaner — the user configures it at the node level:
```python
inner.as_node().map_over("x", error_handling="continue")
```

The executor reads `node._error_handling` and passes it to `runner.map()` and uses it in `_collect_as_lists()`.

## Edge Cases

1. **All items fail in continue mode**: Returns list of RunResults all with FAILED status. For `map_over`, returns lists of all `None`.
2. **Empty input**: Already handled — returns `[]`.
3. **Async unlimited concurrency + raise**: All tasks are already launched. We still check results and raise the first error found. Some tasks may have completed successfully — their results are discarded.
4. **Nested map_over with raise**: Current behavior preserved — executor raises on first failure, which propagates up.
5. **run() partial values**: The `values` dict contains outputs computed before the failure. Outputs from the failed node and downstream nodes are absent.

## Files to Modify

| File | Change |
|------|--------|
| `src/hypergraph/runners/_shared/types.py` | Add `ErrorHandling` literal type |
| `src/hypergraph/runners/sync/runner.py` | `run()`: partial values on failure. `map()`: add `error_handling` param |
| `src/hypergraph/runners/async_/runner.py` | Same changes for async |
| `src/hypergraph/runners/sync/executors/graph_node.py` | Pass `error_handling` to `runner.map()`, handle `None` placeholders in `_collect_as_lists` |
| `src/hypergraph/runners/async_/executors/graph_node.py` | Same changes for async executor |
| `src/hypergraph/nodes/graph_node.py` | Add `error_handling` param to `map_over()` |
| `tests/test_runners/test_sync_runner.py` | Tests for both modes |
| `tests/test_runners/test_async_runner.py` | Tests for both modes |
| `tests/test_exception_propagation.py` | Update existing tests, add new ones |

## Verification

1. `uv run pytest tests/test_runners/test_sync_runner.py -x -v`
2. `uv run pytest tests/test_runners/test_async_runner.py -x -v`
3. `uv run pytest tests/test_exception_propagation.py -x -v`
4. `uv run pytest tests/test_runners/test_graphnode_map_over.py -x -v`
5. `uv run pytest tests/ -x` (full suite)
