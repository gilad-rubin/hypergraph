# Build plan — Step 1: `map_iter` streaming on the runner

Implements ADR 0002 **L1**: the reserved `RunnerCapabilities.supports_streaming` / `.iter()`
streaming, currently `False` ("Phase 2"). This is the foundation DerivedTable's sink consumes;
everything else in the materialization streaming work sits on top.

## Interface

A new runner method, mirroring `map()` but yielding incrementally instead of buffering a `MapResult`:

```python
# SyncRunner (template_sync)
def map_iter(
    self, graph, values=None, *, map_over, map_mode="zip",
    error_handling="raise", **input_values,
) -> Iterator[tuple[int, RunResult]]: ...

# AsyncRunner (template_async)
async def map_iter(
    self, graph, values=None, *, map_over, map_mode="zip",
    max_concurrency=None, error_handling="raise", **input_values,
) -> AsyncIterator[tuple[int, RunResult]]: ...
```

- **Yields `(index, RunResult)`** — `index` is the input item's position, so a consumer can
  correlate a result with its source item regardless of arrival order.
- **Order = completion order.** Sync is sequential, so completion order == input order there.
  Async yields each item the moment it finishes (no head-of-line blocking); `index` carries the
  correlation. *(Decision: completion-order + index, over input-order bare RunResult — chosen so a
  slow item doesn't stall finished ones, and the consumer can still write-by-identity.)*
- **Backpressure.** The producer does not run arbitrarily ahead of the consumer: sync is a lazy
  generator (compute one, yield, repeat); async bounds in-flight work by `max_concurrency` and does
  not schedule the whole input up front.
- **`error_handling`** matches `map()`: `"continue"` yields `RunResult(status=FAILED)` for a failed
  item and keeps going; `"raise"` re-raises when a failed item is reached. Default `"raise"` for
  parity with `map()`; DerivedTable will pass `"continue"`.
- **Capability.** `supports_streaming` becomes `True` on both runners.

`map()` can later be expressed as `dict(map_iter(...))`-style collection, but that refactor is out of
scope for Step 1 — `map_iter` is added alongside `map()`, not replacing it yet.

## Behaviors to test (vertical slices, one RED→GREEN at a time)

1. **(tracer)** sync `map_iter` over a 3-node graph yields one `(index, RunResult)` per input item.
2. each yielded `RunResult` carries that item's correct output values, and `index` matches the input.
3. `error_handling="continue"` → a failing item yields a FAILED result; iteration continues; siblings succeed.
4. `error_handling="raise"` → iteration raises when it reaches the failing item.
5. **laziness** → the producer does not compute every item before the first yield (assert via a
   side-effecting derive + early `break`).
6. async `map_iter` → `async for` yields incrementally; `max_concurrency` caps concurrent in-flight derives.
7. async completion-order → with staggered delays, faster items yield before slower earlier ones (index preserved).
8. `runner.capabilities.supports_streaming is True` (both runners).
9. empty `map_over` input → empty iteration (no error).

## Out of scope for Step 1

- Sinks (Step 2), DerivedTable wiring (Step 3), Daft `map_iter` (Daft deferred).
- Rewriting `map()` in terms of `map_iter`.
- Event processors / progress integration (deferred): `map_iter` v1 takes no
  `event_processors`. Doing it correctly needs batch-level dispatcher ownership
  (a parent span passed to each item run) so per-item `run()` calls don't shut
  down shared processors — a follow-up for when progress/observability is wired.

## Files

- `src/hypergraph/runners/_shared/template_sync.py`, `template_async.py` — the `map_iter` methods.
- `src/hypergraph/runners/_shared/superstep.py` (async) — incremental yield (replace the
  `asyncio.gather` collect-all with as-completed / a bounded queue) — only as needed for the async path.
- `src/hypergraph/runners/sync/runner.py`, `async_/runner.py` — flip `supports_streaming`.
- `tests/test_map_iter.py` — new test module.
