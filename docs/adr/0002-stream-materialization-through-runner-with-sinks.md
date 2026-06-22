# Bounded-memory materialization streams through the runner, with a sink protocol

**Status:** Accepted — defines a new Hypergraph runner capability (the reserved `.iter()` streaming) and how DerivedTable persists results. Pre-implementation.

To materialize a large derive without buffering a whole cascade level in memory, DerivedTable consumes results as a **stream** from the runner and persists them through a **sink**.

- **Streaming lives in the runner.** We implement the reserved `RunnerCapabilities.supports_streaming` / `.iter()` (currently `False`, marked "Phase 2") as an incremental, backpressured `map_iter` that yields each item's result as it completes, instead of collecting everything into one `MapResult`.
- **The write is a sink consumer, not a graph node.** A sink implements `start` / `write` / `finalize` and declares which output ports it persists (validated against `graph.outputs` at construction — a build-time error if it names an output the graph does not produce). The runner feeds it only the named outputs from the stream; the rest stay observable in events.

Memory is bounded to one source item's fan-out — matching the design spec's "bounded by the largest single derive call's output."

## Considered options

- **Caller-side chunk loop in DerivedTable** — rejected: a coarse hand-roll of streaming the engine should own, with no compute/write overlap.
- **Sink as a node inside the graph** (Daft's plan-operator model, e.g. `write_lance` as an operator) — rejected: a side-effecting terminal node violates Hypergraph's pure value-flow model, and it is redundant because DerivedTable's writes are always terminal.
- **Stream `.iter()` + sink consumer (chosen).**

## Why this shape

Pixeltable (pull-based exec nodes + a bounded queue) and Daft (push-based morsels + a `DataSink` with `start`/`write`/`finalize`) both put streaming **in the engine** and the write in a **sink consumer**. We adopt that split — Daft's `run_iter_tables` + `DataSink` — minus the plan-operator sink that fights a pure engine.

## Consequences

- The same `sink=LanceSink(table)` call works across `SyncRunner` / `AsyncRunner` (driven via `map_iter`) and `DaftRunner` (mapped to Daft's native streaming write).
- New plumbing in the shared `map` templates: the async path's `asyncio.gather` (superstep) becomes as-completed / a bounded queue, for incremental yield with backpressure.
- Supersedes the design spec's manual `batch_size` chunking (mechanism 3): the sink batches writes; the runner bounds produce-side memory.
