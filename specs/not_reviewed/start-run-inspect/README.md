# start_run / start_map + Live Inspect

Recovered design intent from four Codex sessions on 2026-03-22.

`start_run()` and `start_map()` are background-execution primitives on `SyncRunner` / `AsyncRunner`. They return a handle (`SyncRunHandle`, `AsyncRunHandle`, `SyncMapHandle`, `AsyncMapHandle`) immediately while the run executes in a thread (sync) or `asyncio.Task` (async). When called with `inspect=True`, the handle is paired with a `LiveInspectState` that backs both an HTML/anywidget live notebook view and a programmatic `RunView`. The same `RunView` artifact and the same renderer are reused for live updates and post-run `result.inspect()` calls — there is exactly one inspect data model and one renderer. Always-on `FailureCase` capture (node name, resolved inputs, error, item index for maps) lands on `RunResult.failure` and `ExecutionError.failure_case` regardless of `inspect`. Checkpointing stays a separate axis: `workflow_id` / `retry_from` / `fork_from` require a checkpointer or error.

## Files

- [design-decisions.md](./design-decisions.md) — the consolidated final design (API, architecture, widget UX)
- [evolution.md](./evolution.md) — how the design changed across the 4 sessions (only the shifts that matter)
- [open-questions.md](./open-questions.md) — items left undecided

## Source spec

The fuller scenario-driven spec the sessions produced lives at
[`specs/not_reviewed/graph_design_docs/start_api_design.md`](../graph_design_docs/start_api_design.md). The docs in this folder summarize and highlight the design decisions inside that spec, not replace it.

## Implementation status (as of session 4 end)

Implemented:
- `FailureCase`, `NodeSnapshot`, `InspectCollector`, `NodeView`, `RunView`, `LiveInspectState`, `InspectWidget`, `MapInspectWidget` in `src/hypergraph/runners/_shared/inspect.py`
- `SyncRunHandle`, `AsyncRunHandle`, `SyncMapHandle`, `AsyncMapHandle` in `src/hypergraph/runners/_shared/handles.py`
- `runner.start_run()` / `runner.start_map()` on both runners
- `RunResult.failure`, `RunResult._inspect_data`, `RunResult.inspect()`
- `ExecutionError.failure_case`, `ExecutionError.inspect_data`
- `ExecutionContext.on_node_snapshot` callback
- HTML renderer in `src/hypergraph/runners/_shared/inspect_html.py`

Pending (per session-4 review): rich anywidget transport, full waterfall/intermediate-output panel parity with the run-viewer references, nested-map item drilldown in the viewer, checkpoint-aware retry/fork wiring on `start_*`.
