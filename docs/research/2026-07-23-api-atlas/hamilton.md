# API Atlas — Apache Hamilton (incubating)

Sources: official docs at https://hamilton.apache.org (Apache Hamilton incubating; package
`apache-hamilton`, formerly `sf-hamilton`; latest release 1.90.0, 2026-04-25 per PyPI).
Fetched 2026-07-23. None of the four existing workspace research docs (engines.md, hitl.md,
patterns.md, 2026-07-21-durable-execution-landscape.md) mention Hamilton — this report is
the sole coverage.

## 1. Identity

Python dataflow micro-framework: the DAG is **derived from function signatures** (parameter
name = upstream node name), one module = one dataflow. Execution philosophy: in-process,
pull-based calculation ("ask for outputs, engine computes the minimal subgraph") — no queue,
no replay, no durability; recovery = re-run with fingerprint-based caching. Born at Stitch
Fix (2021 OSS), via DAGWorks, now Apache incubating; mature core (1.x for years), but the
dynamic-parallelism tier is still flagged experimental.

## 2. Authoring

Functions are nodes; dependencies come from matching parameter names to other function
names; type annotations are mandatory; docstring becomes the node description; `_helper`
functions are excluded (https://hamilton.apache.org/concepts/node/):

```python
def A() -> int:
    """Constant value 35"""
    return 35

def B(A: int) -> float:
    """Divide A by 3"""
    return A / 3
```

You hand the module(s) to a Builder (https://hamilton.apache.org/concepts/builder/):

```python
from hamilton import driver
import my_dataflow
dr = driver.Builder().with_modules(my_dataflow, my_other_dataflow).build()
```

Builder surface (https://hamilton.apache.org/reference/drivers/Driver/): `with_modules`,
`with_config(dict)` (feeds `@config.when` branch selection — Hamilton's poor-man's hypster),
`with_adapters(*hooks)`, `with_cache(...)`, `with_materializers(*from_/to)`,
`enable_dynamic_execution(allow_experimental_mode=True)`, `with_local_executor` /
`with_remote_executor` / `with_execution_manager` / `with_grouping_strategy`,
`allow_module_overrides()`, `copy()`.

Decorator layer (https://hamilton.apache.org/concepts/function-modifiers/):
- `@config.when(task="binary_classification")` — conditional node inclusion by config.
- `@parameterize(revenue_by_age=dict(df=source("df"), groupby_col=value("age")), ...)` —
  one function stamps out N nodes; `source()` = wire to a node, `value()` = literal.
- `@extract_columns("user_id", "weekday")` / `@extract_fields(dict(X_train=np.ndarray, ...))`
  — explode a DataFrame/dict into per-column/per-key nodes (column-level lineage).
- `@subdag(feature_modules, inputs={"path": source("source_path")}, config={})` — embed a
  reusable module-DAG under a namespace; `@parameterized_subdag` stamps out many
  (https://hamilton.apache.org/reference/decorators/subdag/).
- `@resolve(when=ResolveAt.CONFIG_AVAILABLE, decorate_with=lambda cfg...: parameterize(...))`
  — decorators computed from driver config at build time; gated behind
  `hamilton.enable_power_user_mode=True` (https://hamilton.apache.org/reference/decorators/resolve/).
- `@tag(pii='true')`, `@check_output(range=(0,100), importance="warn")`, `@schema.output(...)`,
  `@cache(behavior="recompute", format="parquet")`, `@dataloader()/@datasaver()`,
  `@load_from.parquet(path=source("data_path"))` / `@save_to.json(...)`.

Notebook authoring (https://hamilton.apache.org/how-tos/use-in-jupyter-notebook/):

```python
%%cell_to_module -m MODULE_NAME --display --rebuild-drivers
def hello() -> str: return "hello"
def world(hello: str) -> str: return f"{hello} world"
```

## 3. Run verbs

The entire run surface hangs off one object (`Driver`); everything is blocking and
in-process. Full signatures at https://hamilton.apache.org/reference/drivers/Driver/.

| Hamilton verb | What it does | hypergraph equivalent |
|---|---|---|
| `dr.execute(final_vars, inputs=, overrides=)` | Sync run; computes the **minimal subgraph** for the requested outputs; `final_vars` accepts `str \| Callable \| HamiltonNode`; result assembled by a ResultBuilder (dict by default) | `runner.run(graph, inputs)` — but hypergraph has **no pull-based output selection** and **no overrides** in the brief'd surface |
| `overrides={"node": val}` (param on every verb) | Pin a node's value; its computation and upstream-only deps are skipped | MISSING in hypergraph |
| `dr.raw_execute(final_vars, ...)` | Same, but raw `{node: value}` dict, no result-builder | closest to hypergraph's `RunResult` values |
| `dr.materialize(*materializers, additional_vars=)` | Run + execute `to.*` savers / `from_.*` loaders; returns `(metadata, results)` (https://hamilton.apache.org/concepts/materialization/) | MISSING in hypergraph (IO-edges-as-API) |
| `await async_driver.Builder()...build()`; `await dr.execute(...)` | Async run for FastAPI-style serving; `build()` itself is awaited (`ainit`) (https://hamilton.apache.org/reference/drivers/AsyncDriver/) | `await runner.run(...)` — hypergraph async-native, no split driver |
| `Parallelizable[T]` yield / `Collect[T]` param + `enable_dynamic_execution(allow_experimental_mode=True)` + `with_remote_executor(executors.MultiProcessingExecutor(max_tasks=5))` | Dynamic fan-out/fan-in **inside the graph**; block between them runs once per item (https://hamilton.apache.org/concepts/parallel-task/) | `runner.map(graph, ..., map_over=...)` — hypergraph maps at the runner, Hamilton maps in the graph. Hamilton has **no runner-level batch verb** (users loop `dr.execute` themselves) |
| `dr.validate_execution(final_vars, inputs=, overrides=)` / `validate_materialization(...)` | Pre-flight: validate the exact run without executing | MISSING in hypergraph as a user verb |
| `dr.visualize_execution(final_vars, "path.png", inputs=, overrides=)` | Render the exact execution path pre-run | MISSING (hypergraph has graph viz, not per-run-plan viz) |
| `dr.list_available_variables(tag_filter=...)`, `what_is_upstream_of/downstream_of/the_path_between` | Catalog + lineage queries on the built driver | MISSING as user verbs |
| `Builder.copy()` | Fork a half-configured builder as a template | n/a (config templating lives in hypster) |
| `hamilton ui` (CLI) | Launch the tracking UI at localhost:8241 | MISSING (hypergraph: `show_progress` bars only) |
| — | Streaming results / event iteration | Hamilton MISSING (`runner.iter`, `map_iter` have no analog; hooks are the only tap) |
| — | Background/detached run with a handle | Hamilton MISSING (no `start_run`/`done`/`stop()`) |
| — | Durable submit / worker loop / watch | Hamilton MISSING (no host tier at all; docs punt orchestration to Airflow et al.) |
| — | Interrupt/pause/resume | Hamilton MISSING |

## 4. Observation

Push-based via lifecycle adapters, not pull-based events
(https://hamilton.apache.org/reference/lifecycle-hooks/): `ProgressBar` (tqdm) and
`RichProgressBar` (task-level aware), `PrintLn`, `PDBDebugger(node_filter="B", during=True)`
(drops into pdb inside a node), `DDOGTracer`, `MLFlowTracker`, `OpenLineageAdapter`,
`SlackNotifier`. Custom hooks subclass `NodeExecutionHook`
(`pre/post_node_execute`), `GraphExecutionHook`, or the `Task*` hooks
(TaskSubmissionHook/TaskReturnHook/TaskExecutionHook/TaskGroupingHook) for
Parallelizable batches — but issue #1196 admits adapter coverage of dynamic DAGs is
incomplete. There is **no `async for event in ...` surface**; watching a run means
installing an adapter before build.

Dashboards: Hamilton UI (https://hamilton.apache.org/concepts/ui/) — execution telemetry,
artifact catalog across runs, DAG/lineage explorer, dataflow versioning — wired by adding
one adapter:

```python
tracker = adapters.HamiltonTracker(project_id=PROJECT_ID, username="you@x.com",
                                   dag_name="my_dag_version", tags={"environment": "DEV"})
dr = driver.Builder().with_modules(*mods).with_adapters(tracker).build()
```

Cache observation is its own first-class surface (https://hamilton.apache.org/concepts/caching/):
`dr.cache.logs(level="debug", run_id=dr.cache.last_run_id)`, `dr.cache.view_run()` (renders
which nodes hit/missed/executed), `dr.cache.run_ids`, `dr.cache.data_versions[...]`.

## 5. HITL / pause

None. No pause, signal, approval, or resume of any kind — the run is a synchronous function
call. The only human-in-the-loop affordance is developer-time: `PDBDebugger` pauses
execution in a debugger. Compare hypergraph's `result.paused` gates and the host
`answer(..., pause_id=...)` design; Hamilton simply does not play in this dimension (the
workspace hitl.md research axis is inapplicable).

## 6. Failure & retry surface

No per-node retry policies, no timeouts, no redrive verbs in core. What the user gets:
- `@check_output(data_type=np.int32, range=(0,100), importance="warn")` — data-quality
  gates with warn-vs-fail routing (https://hamilton.apache.org/concepts/function-modifiers/).
- `GracefulErrorAdapter(error_to_catch=MyError, sentinel_value=None, try_all_parallel=True)`
  — continue past failures: failed node's output becomes the sentinel, dependents become
  no-ops, unaffected branches complete
  (https://hamilton.apache.org/reference/lifecycle-hooks/GracefulErrorAdapter/). For
  Parallelizable items, `try_all_parallel=True` yields `[0, 1, 2, None]` when item 3 fails —
  their closest analog to hypergraph's `BatchTolerance`, but untyped and adapter-scoped.
- `@accept_error_sentinels` — a downstream node opts in to **receive** the sentinels instead
  of being skipped, enabling partial-result aggregation and error reporting in-graph.
- Operator recovery = re-run the same `dr.execute` with `.with_cache()` on: unchanged nodes
  hit cache, failed subtree recomputes. Effective, but implicit — nothing like `redrive`,
  `retry_from`, or item-key selection exists.

## 7. Concurrency & flow control

All scope is per-driver, per-executor. `with_remote_executor(MultiProcessingExecutor(max_tasks=5))`
caps parallel tasks for Parallelizable blocks; `with_local_executor` runs everything else;
`with_execution_manager` routes specific nodes to specific executors;
`with_grouping_strategy` controls how nodes group into tasks
(https://hamilton.apache.org/concepts/builder/, /reference/drivers/Driver/). Ray/Dask
executors exist as plugins. No rate limits, no keys/fairness, no debounce/throttle, no
priorities anywhere — flow control beyond a worker-count cap is out of scope by design.

## 8. Scheduling & timers

None, explicitly: no cron, sleep, wake-at, or reminders. Docs position Hamilton as the thing
that runs *inside* your Airflow/Prefect/Metaflow task. This is the same "host is someone
else's job" stance hypergraph Tier 0 takes — except hypergraph's durable tier plans to
absorb part of it (scheduled answers) while Hamilton never will.

## 9. The 3 steals (plus one)

1. **Pull-based `final_vars` + `overrides` — the graph as a calculator.** Ask for any node
   by name *or by function reference*; the engine computes only the minimal subgraph; pin
   any node's value to skip its whole upstream. This is the single best notebook ergonomic
   in the field: recompute one output, stub one expensive dependency, no graph edits.
   ```python
   results = dr.execute(final_vars=["model"], overrides={"raw_df": cached_df},
                        inputs={"data_path": "..."})
   ```
   (https://hamilton.apache.org/reference/drivers/Driver/)

2. **Pre-flight as first-class verbs: `validate_execution` + `visualize_execution`.**
   Validate the *exact* run (outputs+inputs+overrides) without executing, and render the
   exact path it would take. Cheap insurance before an expensive run; hypergraph has neither
   as a user verb.
   ```python
   dr.validate_execution(["C"], inputs={"x": 1})
   dr.visualize_execution(["C"], "execute_c.png")
   ```

3. **`%%cell_to_module` Jupyter magic.** A notebook cell *is* the module *is* the DAG, with
   `--display` rendering it and `--rebuild-drivers` refreshing stale drivers on re-run.
   Direct blueprint for keeping hypergraph's Tier 0 zero-ceremony in notebooks.
   ```python
   %%cell_to_module -m features --display --rebuild-drivers
   def world(hello: str) -> str: return f"{hello} world"
   ```
   (https://hamilton.apache.org/how-tos/use-in-jupyter-notebook/)

4. *(bonus)* **`@accept_error_sentinels`.** Failure routing as a graph-level opt-in: a
   designated node receives failed-branch sentinels instead of being skipped, so partial
   batch results and error reports are just another node. Elegant complement to
   `BatchTolerance`.

## 10. The warnings

1. **Dynamic fan-out grafted onto a static-DAG core never stopped leaking.**
   `enable_dynamic_execution(allow_experimental_mode=True)` is still required years in; no
   nested Parallelizable/Collect; **one Collect per block** (issues #742, #1029/#1030 —
   https://github.com/apache/hamilton/issues/742); pickle serialization "breaks with certain
   cases" under multiprocessing; `dr.cache.view_run()` doesn't support task-based execution;
   lifecycle adapters don't fully cover dynamic DAGs (#1196). Lesson for hypergraph: when
   map/fan-out semantics ship, every surface (caching, observability, viz) must speak them
   from day one, or the seams show for years.

2. **Two-generation API strata everywhere.** Legacy `driver.Driver({}, module)` vs
   `Builder`; legacy `GraphAdapter`/`ResultBuilder` vs lifecycle adapters
   (`LegacyResultMixin` survives); async `ainit()` kept "for backwards compatibility;
   planned replacement in Hamilton 2.0"; sync hooks "may behave unexpectedly" on the
   AsyncDriver (https://hamilton.apache.org/reference/drivers/AsyncDriver/); and **four**
   materialization approaches whose docs table weighs their trade-offs
   (https://hamilton.apache.org/concepts/materialization/). Each was additive-polite; the
   sum is a matrix users must learn.

3. **Honesty gaps in fingerprinting.** Cache `code_version` hashing misses changes in
   nested helper functions ("manually force recomputation or clear cache"), and cache keys
   "may become unstable across Python/Hamilton version upgrades"
   (https://hamilton.apache.org/concepts/caching/) — silent staleness in the exact place
   users trust the tool most.

4. **`@resolve` — the docs indict their own feature**: it "goes against one of Hamilton's
   primary tenets (that all code is highly readable)" and hides behind
   `hamilton.enable_power_user_mode=True` (https://hamilton.apache.org/reference/decorators/resolve/).
   When config-driven graph shaping outgrows `@config.when`, the decorator layer buckles —
   hypergraph rightly outsources this whole axis to hypster.

## 11. Verdict vs hypergraph

Hamilton is a dependency-injection calculator, not a run host, so the comparison is
asymmetric: it has nothing on hypergraph's entire runtime axis — no async-event streaming,
no background handles, no map verb at the runner, no pause/HITL, no durable tier, no
stop/redrive/watch — and its own docs punt orchestration to external schedulers. But at the
in-process API level it beats hypergraph's Tier 0 on *interrogation ergonomics*: pull-based
`final_vars` (compute exactly what I ask, minimal subgraph), `overrides` (pin any node,
skip its upstream), `validate_execution`/`visualize_execution` as pre-flight verbs,
`what_is_upstream_of`-style lineage queries, cache introspection (`dr.cache.view_run()`),
and the `%%cell_to_module` notebook loop — all of which serve the same notebook-first user
hypergraph targets. Its cache-keyed re-execution is a derived cousin of hypergraph's
explicit checkpoint+resume, and its fingerprint honesty gaps argue *for* hypergraph's
recorded-state approach. Net recommendation: steal the calculator surface (output selection,
overrides, run-plan preview) into Tier 0; keep map/batch at the runner rather than in-graph
(Hamilton's Parallelizable purgatory is the cautionary tale); do not import its adapter
taxonomy or materializer matrix; and treat its two-generation API strata as the cost of
"additive" evolution without deprecation discipline — the exact trap the durable tier's
"host is additive" philosophy must dodge.
