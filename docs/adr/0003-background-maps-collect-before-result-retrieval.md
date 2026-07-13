# Background maps collect item failures before result retrieval

**Status:** Partially superseded by [ADR 0004](0004-background-handles-control-live-work.md), which removes `wait()` and assigns settled status and failure data exclusively to results. The collection-versus-retrieval decision remains accepted. Implementation is tracked by [issue #155](https://github.com/gilad-rubin/hypergraph/issues/155); `start_map()` is not a shipped API until that implementation merges.

Background execution must let a caller regain control immediately and inspect the eventual batch without losing later item outcomes. Reusing blocking `map(error_handling="raise")` would not provide one scheduling contract: sync mapping stops at the first failed item, bounded async mapping stops claiming new items after a failure is observed, and unbounded async mapping may already have started every item.

We therefore separate collection from retrieval.

## Decision

- `start_map()` does not accept `error_handling`. A mapped-item failure does not stop sibling items from being scheduled.
- Background mapping captures every requested item outcome unless the caller explicitly stops the handle or a batch-level failure prevents construction of a `MapResult`.
- Sync and async handles follow the same collection contract, independent of async concurrency limits.
- `handle.result(raise_on_failure=True)` waits for the batch to settle, then raises the original error from the first failed `RunResult` in input order. `True` is the default, and raising order does not depend on whether node-level evidence is available.
- `handle.result(raise_on_failure=False)` returns the settled `MapResult`, including failed item results and their `FailureEvidence` when the failure is attributable to a node.
- `handle.wait()` waits for settlement without turning captured item failures into retrieval exceptions.
- A batch-level failure that prevents construction of a `MapResult` still propagates from `wait()` and `result()`. `raise_on_failure` governs captured mapped-item failures only.
- Blocking `map()` keeps its existing `error_handling="raise" | "continue"` contract.

The planned user-facing shape is:

```python
handle = runner.start_map(document_graph, {"document": documents}, map_over="document")

# Exception-first code: raising happens after every item has settled.
handle.result()

# Diagnostic code: inspect every outcome, including failures with no node evidence.
batch = handle.result(raise_on_failure=False)
for item_index, item in enumerate(batch.results):
    if not item.failed:
        continue
    if item.failure is None:
        print(item_index, type(item.error).__name__)
    else:
        print(item.failure.item_index, item.failure.node_name)
```

`AsyncRunner.start_map()` returns its handle immediately without `await`. The async handle's `wait()` and `result()` methods are awaitable; for example, use `batch = await handle.result(raise_on_failure=False)`.

## Considered options

- **Mirror blocking `map()`** — rejected because a non-raising retrieval cannot recover outcomes that were never scheduled, and scheduling already differs between sync, bounded async, and unbounded async runners.
- **Accept both execution modes but default background mapping to continue** — rejected because `error_handling` and `raise_on_failure` become overlapping controls, including combinations that return an already-truncated batch without raising.
- **Always collect mapped-item outcomes and raise only at retrieval (chosen)** — gives handles one stable purpose and preserves complete diagnostic evidence unless work is explicitly stopped.

## Consequences

- A failed background batch can continue consuming resources and producing item-level side effects. Callers use `handle.stop()` when they intend to curtail work.
- `handle.result()` remains exception-first by default, matching blocking runner calls and Python future/task retrieval conventions, while diagnostic callers opt into `raise_on_failure=False`.
- Implementing issue #155 must add the public API reference, practical how-to, runnable sync and async examples, and documentation contract tests in the same PR as the code.
