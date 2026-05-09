# Evolution Across Sessions

Only the shifts that matter. The four sessions chained on top of each other on 2026-03-22.

## Session 1 (14:28) — initial plan, then a hard pivot

Started as a 4-phase implementation plan: `FailureCase` -> `InspectCollector` -> `RunView` + `result.inspect()` -> "Basic Inspect Widget". Single knob: `inspect: bool`. Blocking-only API.

The user pushed back twice:

1. **Rejected `runner.last_result`** as a side channel for partial state on raise mode. The agent had reached for "stash the failed `RunResult` on the runner so the user can inspect it after the exception." The user's response: "it's like magic… what is last_result?" Replaced with: always-on `FailureCase` on `ExecutionError.failure_case` and `RunResult.failure`.

2. **Asked for background execution.** The user pivoted from "blocking inspect" to: "we should have `start_run` and `start_map`, no? when we do that and `inspect=True` — can we show a live updating widget in the cell?"

The agent surveyed alternatives (`asyncio.create_task`, `Executor.submit`, Prefect `.submit()`, Temporal `start_workflow`, Celery `apply_async`) and recommended `start_run` / `start_map` because they mirror existing `run()` / `map()`. Rejected: `run_headless`, `dispatch_*`, `submit_*`. The user accepted `start_run` / `start_map`.

Live widget viability: confirmed — `events/rich_progress.py` already keeps a notebook display updated via `display_id`. The inspect widget reuses the same mechanism.

The session ended with `start_api_design.md` taking shape as the consolidated spec.

## Sessions 2 & 3 (18:23, parallel) — scenario-driven rewrite

The user requested: "take every use case … sync, async, fail, success, live run, checkpoint with/without, inspect=True/False … and for each combination show user-facing code, what they see visually, in code, the outputs … at least 10 use cases."

Subagents expanded the spec into 14 scenarios, then 15. Renames during the consistency sweep:

- `node_path` -> `node_name` on `FailureCase` (user request, kept the meaning that nested names are still fully qualified)
- "same failure-focused view" wording -> the explicit reuse contract: one data model, one renderer, `inspect=True` auto-renders during execution and `result.inspect()` re-renders the same inspector later
- Stale `last_result` and `return_handle` references swept out of the addendum

Open dilemma left in the doc: should `start_map()` accept `error_handling`, or should aggregate failure policy stay only on blocking `map()`?

## Session 4 (18:53) — last design pass + implementation start

Three concrete tightenings the user requested:

1. **`wait()` demoted.** "Keep `wait()` as a low-level convenience, but not emphasize it in the main examples." Main examples now use `result(raise_on_failure=False)` instead.

2. **`workflow_id` requires a checkpointer.** "Whenever you use workflow_id — make sure to specify a checkpointer. Otherwise — I think hypergraph should error." The doc had been treating `workflow_id` as a soft control label that worked in-process; the user made it an error condition. Per-run stop moves to `run.stop(...)` on the handle instead of `runner.stop(workflow_id, ...)`.

3. **Always show runner setup explicitly.** Examples must show `SyncRunner()` or `AsyncRunner(...)` instead of a floating `runner`, so sync/async is unambiguous.

Then a separate request: "add a subtask to improve error messages so the agent understands which graph/subgraph/node/item errored." Spec gained a `node_name` / `run_id` / `workflow_id` / "next-step hint" enrichment story for the plain raised exception text, with a before/after target and acceptance bar — this lives as a follow-on, not blocking the first cut.

After the design was locked, the user said "go ahead and implement the plan. use TDD, use subagents, launch a reviewer subagent throughout that checks implementation vs design." Implementation began:

- `FailureCase`, `NodeSnapshot`, `InspectCollector`, `NodeView`, `RunView`, `build_run_view` landed in `runners/_shared/inspect.py`
- `RunResult.failure`, `RunResult._inspect_data`, `RunResult.inspect()` wired through
- `ExecutionError` gained `failure_case` and `inspect_data`
- `ExecutionContext.on_node_snapshot` callback added
- Sync/async supersteps and templates updated
- `inspect` reserved as a runner option name
- Tests pass for blocking `run(..., error_handling='continue')` failure capture and `run(..., inspect=True)` output capture

The reviewer subagent's verdict at session-4 end: `start_run()` / `start_map()` handles were the highest-severity gap remaining. The agent recommended four explicit handle classes (`SyncRunHandle`, `AsyncRunHandle`, `SyncMapHandle`, `AsyncMapHandle`) — exactly the shape that landed in `handles.py` afterward.

## What did not survive

- `runner.last_result` (s1, rejected as magic)
- `capture_values=True` as a second knob (s1->s2, folded into `inspect=True`)
- `run_headless`, `dispatch_run`, `submit_run` naming (s1, lost to `start_run`)
- `wait()` as a primary-path API (s4, demoted to low-level)
- `workflow_id` as a non-checkpointed control label (s4, now errors)
- `node_path` as a field name (s4, renamed to `node_name`)

## Viewer direction (decided in s4)

The user pointed to `worktrees/9444/hypergraph/tmp/run-viewer` and `tmp/waterfall-demo.html` as references. Direction confirmed:
- HTML/JS-based, bundled like the existing `viz/html/generator.py`
- `anywidget` (not currently a dep) for the live notebook transport — same goal as `viz/js`: avoid dumping a huge HTML payload every tick
- Reuse the timeline/Gantt and intermediate-output ideas from the references; reuse Hypergraph's existing graph topology renderer; do not port the React/Vite app shell
