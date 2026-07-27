# pipefunc — user-facing API atlas

Sources: official docs at https://pipefunc.readthedocs.io/en/latest/ (version **0.93.1**, latest
release 2026-07-19; fetched 2026-07-23) and github.com/pipefunc/pipefunc (473 stars, MIT, 160
releases, active). No prior workspace research covers pipefunc; lifecycle/HITL landscape context
lives in `hypergraph/docs/research/2026-07-21-durable-execution-landscape.md` and
`superposition/docs/research/2026-07-16-ingestion-lifecycle-primitives/*` — this file covers only
the user-facing API shape.

## 1. Identity

Python DAG-of-functions library for scientific/HPC workflows: decorate functions, it wires the DAG
from parameter names, runs sweeps ("maps") over N-dimensional input grids, in-process or on SLURM.
Execution philosophy: **dataflow + state-based resume from persisted array storage** (a run folder
of per-element files) — no replay, no queue, no server, no determinism contract. Mature-ish single
-track project (0.x, 160 releases, <10 µs/function overhead claim), notebook-first.
(https://pipefunc.readthedocs.io/en/latest/, FAQ: https://pipefunc.readthedocs.io/en/latest/faq/)

## 2. Authoring

Decorator + list-of-functions constructor. Edges come from matching parameter names to
`output_name`s. Fan-out is declared **on the node** via a `mapspec` index string, not at the call
site. (Tutorial: https://pipefunc.readthedocs.io/en/latest/tutorial/)

```python
from pipefunc import pipefunc, Pipeline

@pipefunc(output_name="c")
def f_c(a, b):
    return a + b

@pipefunc(output_name="d")
def f_d(b, c):
    return b * c

pipeline = Pipeline([f_c, f_d], profile=True)
```

Same thing without the decorator: `f = PipeFunc(f, output_name="c", renames={"a": "x"})`.
Full node knobs (reference: https://pipefunc.readthedocs.io/en/latest/reference/pipefunc/):
`output_name`, `output_picker`, `renames`, `defaults`, `bound` (fixed args), `profile`, `debug`,
`cache`, `mapspec`, `internal_shape`, `post_execution_hook`, `resources`, `resources_variable`,
`resources_scope` ("map" | "element"), `scope` (namespace prefix), `variant`. **No retry or
timeout parameters exist.** Pipeline ctor: `lazy`, `cache_type`/`cache_kwargs`,
`validate_type_annotations` (type-hint mismatch across an edge fails at construction),
`default_resources`, `scope`, `name`, `description`. Composition: `pl1 | pl2`, `pipeline.add/drop/
replace`, `pipeline.nest_funcs({"c","d"}, ...)` → `NestedPipeFunc` (limitation: nested mapspecs
may not contain reductions or `internal_shape`).

The mapspec mini-language (https://pipefunc.readthedocs.io/en/latest/concepts/mapspec/):

```python
@pipefunc("y", mapspec="x[i] -> y[i]")            # element-wise
@pipefunc("z", mapspec="x[i], y[j] -> z[i, j]")   # cross product
@pipefunc("r", mapspec="x[a], y[a], z[b] -> r[a, b]")  # zip a, cross b
# "x[i, :] -> y[i]"  reduction (drop an index)
# "... -> x[i]"      dynamic axis from an implicit input (internal_shape / '?')
```

Reductions are just nodes without the index: a no-mapspec function whose parameter is a mapped
output receives the whole array (`def take_sum(y: Array[int]) -> int`). `pipeline.add_mapspec_axis
("a", axis="i")` retrofits a sweep axis onto an existing pipeline and propagates it downstream.

## 3. Run verbs

The complete verb surface. Docs: tutorial "Executing the Pipeline", Execution and Parallelism
(https://pipefunc.readthedocs.io/en/latest/concepts/execution-and-parallelism/), pipefunc.map
reference (https://pipefunc.readthedocs.io/en/latest/reference/pipefunc.map/), run-status
(https://pipefunc.readthedocs.io/en/latest/concepts/run-status/), CLI
(https://pipefunc.readthedocs.io/en/latest/concepts/cli/), MCP
(https://pipefunc.readthedocs.io/en/latest/concepts/mcp/).

| pipefunc verb | What it does | hypergraph equivalent |
|---|---|---|
| `pipeline(a=1, b=2)` / `pipeline("e", a=1, b=2)` | Sync, sequential, in-memory single run; computes only up to the chosen output (defaults to leaf) | `await runner.run(graph, inputs)` — ours is async-native; **target-output selection MISSING on ours** |
| `pipeline.run("e", kwargs={...}, full_output=True)` | Explicit form; `full_output` returns every intermediate as a dict | `RunResult` (all node outputs) |
| `pipeline.func("e")` → `f(**kwargs)` | Reusable callable handle for one output (also `f.call_with_root_args(...)`) | MISSING on ours |
| `pipeline.map(inputs, run_folder=..., parallel=True, executor=..., storage=..., chunksizes=..., resume=False, show_progress=..., error_handling="raise"\|"continue", return_results=True, output_names=..., fixed_indices=..., internal_shapes=..., auto_subpipeline=False)` | Blocking batch/sweep; mapspec fan-out; persists per-element results to storage; returns `ResultDict` | `await runner.map(graph, inputs, map_over=...)` |
| `pipeline.map_async(..., start=True, display_widgets=True)` → `AsyncMap` | Non-blocking; `await runner.task`, `.progress`, `.start()`, `.cancel()`, `.result()` (sync bridge for scripts) | `runner.start_map(...)` handle (done / stop() / result()) |
| `run_map(pipeline, ...)` / `run_map_async(pipeline, ...)` | Module-level function equivalents of the two above | — |
| `pipefunc.map.gather_maps(runners, max_concurrent=N)` / `launch_maps(...)` | Run MANY prepared `map_async(start=False)` runners with a cross-run concurrency cap; blocking-await or fire-and-forget `asyncio.Task` | MISSING in Tier 0; host `max_active_runs` (designed, unbuilt) |
| `map(..., resume=True, resume_validation="auto"\|"strict"\|"skip")` | Reload persisted elements from `run_folder`, compute only what is missing/failed | checkpointer resume; ours re-runs a workflow_id |
| `map(..., fixed_indices={"i": slice(0, 5)})` | Compute only a subset of the grid | `host.redrive(..., item_keys={...})` (designed); MISSING Tier 0 |
| `pipeline.cli()` | Auto-generated CLI from signatures+docstrings (pydantic-validated); modes `cli`/`json`/`docs`; map options as `--map-run_folder`, `--map-parallel`, `--map-resume`, ... | MISSING on ours |
| `pipefunc-cli status RUN --pretty` / `list-runs DIR` / `watch RUN --interval 1 --timeout 300` | Operator inspection of any run folder from any process: status (pending/running/incomplete/completed/failed/cancelled), progress fraction, per-output progress; watch exits 0/1/2 | `host.watch` (designed, unbuilt); no CLI anywhere in our design |
| `build_mcp_server(pipeline)` | One call → MCP server with 8 tools: `execute_pipeline_sync`, `execute_pipeline_async` (job id), `check_job_status`, `list_jobs`, `cancel_job`, `run_info`, `list_historical_runs`, `load_outputs` | MISSING on ours |
| `load_outputs("y", run_folder=...)`, `load_all_outputs`, `load_dataframe(backend="pandas"\|"polars")`, `load_xarray_dataset` | Read results (incl. partial runs, as masked arrays) from disk after the process died | MISSING on ours (RunResult is in-process only until host lands) |
| `AsyncMap.cancel()` | Stop a running map | `handle.stop()` / `host.stop(wf)` |
| — per-item streaming | MISSING on theirs (results land in storage; you poll or wait) | `runner.map_iter(...)` — ours ahead |
| — live event stream | MISSING on theirs (`post_execution_hook` per-node callback is the closest) | `runner.iter(...)` — ours ahead |
| — worker/queue entrypoint | MISSING on theirs (push model: executors incl. SLURM; no pull worker) | `host.work_forever(...)` (designed) |
| — submit dedup / workflow identity | MISSING on theirs (`run_folder` path is the only identity; no fingerprint) | host submit dedup (designed) |

Notes on the split: `run` executes any output with any args but supports **no mapspec/map-reduce**;
`map` supports mapspec but takes **root arguments only** and computes all outputs unless
`output_names` filters. `parallel=True` is ignored when `executor` is given. Executors and storage
are **dicts keyed by output name** with `""` as default: `executor={"y": ThreadPoolExecutor(...),
"": ProcessPoolExecutor(...)}`, `storage={"z": "file_array", "": "dict"}` — mixing thread pools for
IO-bound nodes with process pools/SLURM for CPU nodes in one run. Accepted executor types:
ProcessPool/ThreadPool, ipyparallel, `dask.distributed.Client().get_executor()`, mpi4py MPIPool,
loky, executorlib, `adaptive_scheduler.SlurmExecutor`. Scheduling: functions are dispatched by
generation by default; an **eager** scheduling strategy starts a function the moment its own deps
are ready. Storage backends via `storage_registry`: `file_array`, `dict`, `shared_memory_dict`,
plus zarr variants.

## 4. Observation

- `show_progress=True | "rich" | "ipywidgets" | "headless"` on `map`/`map_async`. `"headless"`
  writes a `pipefunc_status.json` heartbeat with no UI — built for scripts whose runs other
  processes watch. `AsyncMap.progress` exposes the tracker; Jupyter gets live widgets
  (`display_widgets=True`).
- **The run folder is the observation surface.** `pipefunc-cli status` reports `status`,
  `status_source: "heartbeat" | "disk_heuristic"`, `progress_fraction`, per-output progress;
  `list-runs` scans a directory of runs; `watch` polls to terminal state with exit codes. Run
  folders stay inspectable when copied elsewhere. (https://pipefunc.readthedocs.io/en/latest/concepts/run-status/)
- Partial results of a crashed/failed run load as `numpy.ma.MaskedArray` (missing elements
  masked) via `load_outputs` / `load_all_outputs`; full runs load as xarray Datasets or
  pandas/polars DataFrames — the sweep grid becomes labeled dimensions.
- `pipeline.visualize()` renders the DAG; `pipeline.info()` / `print_documentation()` document it;
  `profile=True` + `pipeline.print_profiling_stats()` gives per-node CPU/memory/time (sequential
  runs only).

## 5. HITL / pause

None. No pause, signal, approval, or interrupt concept anywhere in the docs. The nearest
primitives are `map_async(start=False)` (a run prepared but not started) and
`post_execution_hook`. Everything hypergraph has here (paused results, answer schemas,
`host.answer` validation, scheduled answers) is absent.

## 6. Failure & retry surface

- **User config:** `error_handling="raise"` (default: first failure aborts) or `"continue"`:
  failing elements become `ErrorSnapshot` values in the result arrays; downstream consumers of
  errored cells get `PropagatedErrorSnapshot` placeholders with `.get_root_causes()` (empty for
  multi-error reductions). Infrastructure errors (pickling, validation, storage IO) abort
  regardless. (https://pipefunc.readthedocs.io/en/latest/concepts/error-handling/)
- **ErrorSnapshot** is stored on the function AND the pipeline (`pipeline.error_snapshot`),
  captures function, args/kwargs, traceback, timestamp, user, machine, IP, cwd, and supports
  `.reproduce()` (re-run locally with the exact failing args), `.save_to_file()` /
  `.load_from_file()`.
- **No retry policies, no timeouts, no error routing, no backoff.** The recovery story is:
  `error_handling="continue"` → inspect snapshots → fix code → re-`map` the same `run_folder`
  with `resume=True` (validated by `resume_validation`), which recomputes missing/failed cells
  and keeps completed ones. `fixed_indices` narrows a recompute to chosen grid cells.
- Operator verbs: `resume` (per-folder), `AsyncMap.cancel()`, `pipefunc-cli watch` exit codes for
  scripting. No bulk ops across runs, no redrive lineage (a resumed run overwrites in place — no
  `retry_of` chain).

## 7. Concurrency & flow control

Scopes, from inner to outer:
- **Per element:** `chunksizes` (int, per-output dict, or callable `count -> size`) batches map
  elements into executor tasks to amortize overhead.
- **Per node (output name):** the executor dict — each output can have its own pool with its own
  `max_workers`; likewise per-node `storage`. Node `resources={"cpus": 2, "memory": "8GB",
  "gpus": ...}` (static, or a callable of the inputs: `resources=lambda kw: {"cpus": abs(kw["x"])
  % 3 + 1}`) with `resources_scope="map" | "element"` — but resources are only enforced by the
  Adaptive Scheduler/SLURM environment. (https://pipefunc.readthedocs.io/en/latest/concepts/resource-management/)
- **Per run:** `parallel=True/False`; eager vs generation scheduling.
- **Across runs:** `gather_maps` / `launch_maps(..., max_concurrent=N)` — an in-process cap on
  simultaneously executing maps.
No rate limits, no keys/fairness, no debounce/throttle, no priorities.

## 8. Scheduling & timers

None. No cron, sleep, wake-at, or reminders. Closest: SLURM submission via
`SlurmExecutor()` passed to `map_async` (`resources_scope="element"` = one SLURM job per grid
cell; `size_per_learner` chunks a big sweep into jobs), which delegates queueing to the cluster.
(https://pipefunc.readthedocs.io/en/latest/concepts/slurm/)

## 9. The 3 steals (plus one)

1. **The run folder as a first-class, portable artifact with a CLI** — any process (or the
   `pipefunc-cli`) can ask a directory "what is this run, how far along is it, what completed?"
   without the library, the original process, or a server; heartbeat when live, disk heuristic
   when dead; partial results load as masked arrays. This is exactly the evidence-honesty story
   hypergraph's Run Home wants, already shaped as operator verbs.
   ```bash
   pipefunc-cli status runs/my-run --pretty
   pipefunc-cli list-runs runs --max-runs 20
   pipefunc-cli watch runs/my-run --interval 1 --timeout 300   # exit 0/1/2
   ```
2. **`build_mcp_server(pipeline)`** — one call turns a typed graph into an agent-facing tool
   surface: sync execute, async execute returning a job id, status/list/cancel, plus historical
   run inspection — i.e. hypergraph's designed host verbs (`submit`/`watch`/`stop`) auto-derived
   from the graph's own signature and docstrings. hypergraph graphs are typed; this is nearly
   free for us and is the 2026-shaped distribution channel.
   ```python
   from pipefunc.mcp import build_mcp_server
   build_mcp_server(pipeline)   # 8 tools: execute_pipeline_async, check_job_status, ...
   ```
3. **`ErrorSnapshot.reproduce()`** — a failure captures the callable plus the exact args (and
   env context), persists, and re-runs locally in one line. Beats a stack trace in a log for
   debugging one failed doc out of 500 in a batch.
   ```python
   snap = pipeline.error_snapshot
   snap.kwargs          # the exact failing inputs
   snap.reproduce()     # re-raise locally, offline
   ```
4. *(bonus)* **Per-output executor/storage dicts + results-as-dataset** — route IO-bound nodes to
   threads and CPU nodes to processes/SLURM in one run (`executor={"y": ThreadPoolExecutor(2),
   "": ProcessPoolExecutor(2)}`), then read the whole sweep back as
   `load_xarray_dataset(run_folder=...)` with the grid as labeled dims. hypergraph's `map_over`
   returns a list; a sweep-shaped result surface is worth considering for eval grids.

## 10. The warnings

- **`cleanup` → `resume` deprecation (0.89.0, removal in 1.0)** — they shipped a destructive
  boolean (`cleanup=True` default wipes the run folder) and had to rename it to its intent-
  positive twin; the default `resume=False` STILL deletes previous results silently. Lesson:
  never make "erase prior evidence" the unmarked default of a batch verb.
  (https://pipefunc.readthedocs.io/en/latest/reference/pipefunc.map/)
- **The `run` / `map` two-worlds split** — one verb has no map-reduce, the other accepts only
  root arguments and computes everything; caching hashability rules differ between them; profiling
  works only sequentially; `parallel` is silently ignored when `executor` is given. The FAQ needs
  a dedicated "what is the difference between pipeline.run and pipeline.map?" entry and a
  comparison table — users demonstrably trip on it.
  (https://pipefunc.readthedocs.io/en/latest/concepts/execution-and-parallelism/)
- **Notebook-first async leaks into scripts** — running `map_async` in a plain `.py` needs three
  compensating flags (`start=False`, `display_widgets=False`, then `runner.result()` instead of
  `await runner.task`), documented as an FAQ workaround rather than designed away.
  (https://pipefunc.readthedocs.io/en/latest/faq/)
- **API churn on variants** — `variant_group` deprecated in 0.58.0, folded into dict-valued
  `variant={"method": "add"}`; 160 releases at 0.x means steady breakage.
- **Nesting limits** — `NestedPipeFunc` mapspecs may not contain reductions or `internal_shape`;
  composition and map-reduce don't fully commute.

## 11. Verdict vs hypergraph

pipefunc and hypergraph Tier 0 are near-twins in authoring (decorated functions, name-wired DAG,
zero-ceremony notebook use), but they diverge in where the sophistication went: pipefunc spent it
on the **data plane** — the mapspec index language for N-d sweeps/zips/reductions declared per
node, per-output executor and storage routing, chunked dispatch, partial-grid recompute
(`fixed_indices`), and results that come back as xarray/pandas datasets — and on the **artifact**:
a portable run folder any process can inspect, resume, or watch via a shipped CLI, plus free CLI
and MCP-server generation from the graph's own types. All of that is missing or thinner on our
side (we return lists, select no target output, ship no CLI/MCP, and until the host lands our
runs die with the process). hypergraph spent it on the **control plane**, where pipefunc has
essentially nothing: no events, no per-item streaming, no HITL pause/answer, no retries or
timeouts, no scheduling, no workflow identity/dedup/lineage (a folder path is the only identity,
and resume overwrites in place), and cancellation is best-effort in-process. Net: our runtime
model is strictly more general and our durable-host design has no counterpart in pipefunc — but
their edges are cheap for us and user-lovable. Steal the run-folder-as-inspectable-artifact
posture for Run Home (status/list/watch CLI, partial-result loading), auto-generate an MCP/CLI
surface from typed graphs, adopt ErrorSnapshot-grade failure capture with `.reproduce()`, and
consider target-output selection plus dataset-shaped map results; skip the mapspec string language
(powerful but a parser-shaped cliff — `map_over` + explicit nodes reads better) and do not copy
the run/map verb split or a destructive resume default.
