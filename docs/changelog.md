# Changelog

## Unreleased

### Changed

- **BREAKING (visualization): inputs are hidden by default and revealed on
  demand; bound inputs are shown, faded and dashed, by default (#595).**
  `visualize()` now defaults to `show_inputs=False` everywhere
  (`Graph.visualize`, `hypergraph.viz.visualize`, `HyperTable.visualize`,
  `extract_debug_data`), so the diagram shows steps only. Hovering a step, or
  tapping it on a touch screen, draws its inputs as ghost pills beside it:
  graph inputs as `name : Type`, bound tools faded and dashed, and a value
  whose arrow `simplify` hides as `raw ← fetch` (tapping a collapsed container
  opens it, as before). The step's upstream and
  downstream path stays lit while the rest dims, gate labels included. A click
  or tap pins the ghosts and pans them on screen; an empty-canvas click,
  `Escape` or the Show/Hide Inputs toggle clears them. `show_bounded_inputs`
  now defaults to True, for input boxes and ghosts alike; pass False to leave
  bound inputs out. Pass `show_inputs=True, show_bounded_inputs=False` for the
  previous picture. The
  standalone HTML also lays out at device width on phones, and a tapped
  toolbar button no longer leaves its tooltip up.

- **BREAKING (OpenTelemetry): natural names, collapsed nested runs, migrated
  identity, and a >=1.24 floor.** Span names are now the user-authored graph or
  node name (unnamed graphs use `graph`); mapped items use `<graph>.item`.
  Structure moved to `hypergraph.span.role`. `role=node` means ordinary nodes
  only, `role=graph|map` includes collapsed GraphNodes, and presence of
  `hypergraph.node_name` selects all node executions. A GraphNode and its first
  child run now share one physical span at every nesting depth; outer identity
  remains at the existing keys and nested run identity/duration/outcome moves
  to `hypergraph.nested.*`. Eventless delegation and checkpoint-restored
  children have no run to collapse and remain `role=node`. Collapsed lineage
  links are added post-creation, and the final collapsed role and nested/map/
  lineage attributes are likewise known only after creation; none can affect
  head sampling. This requires OTel API/SDK/HTTP exporter >=1.24. Although the
  upstream changelog attributes `Span.add_link` to 1.23.0, the published 1.23.0
  API and SDK artifacts do not contain it; 1.24.0 is the first released version
  that implements it. Success remains `UNSET` by default;
  `set_success_status=True` opts into `OK` only for genuine completion.
  `enrich_openinference=True` opts into `CHAIN` plus Hypergraph's documented
  logical-name containment interpretation of `graph.node.*`; it is not an
  OpenInference portability guarantee. Both flags default to `False`, all
  spans stay `INTERNAL`, and Hypergraph attributes override extra attributes.

- **BREAKING (Durable Host API): new work is graph-first, and Batch
  submission speaks runner-map vocabulary.** `host.submit()`,
  `host.submit_batch()`, and `host.fork(into=...)` now take the served
  `Graph` object instead of a Definition-name string; the Host resolves it by
  the graph's own pinned identity (name **and** `structural_hash`), so the
  code you hold a reference to is the code that runs. An unserved or
  structurally drifted graph raises the new `UnservedGraphError` at the call
  site instead of being accepted and parked; a bare string raises
  `TypeError`. `submit_batch()` replaces its mapping-of-item-key-to-inputs
  `items=` parameter with `values` plus `map_over` / `map_mode` / `identity` —
  the same input-expansion vocabulary as `runner.map()`, frozen into the same
  immutable manifest. `identity` names one expanded input whose JSON-safe
  scalar value is the logical item key; missing, empty, non-scalar, or
  duplicate keys raise the new `ItemKeyError` before anything is written.
  Runner map's `max_concurrency` and `error_handling` are deliberately not
  carried over: durable concurrency is Host admission and durable failure
  policy is `BatchTolerance`. There is no `host.map()` — it would promise an
  immediate `MapResult` where a durable Batch returns a receipt.

- **BREAKING (checkpointers): a checkpointed run now stores its graph inputs,
  so those inputs must be storable.** Step records fold node *outputs*, which
  left the values a run started from unrecorded — a node placed after an
  interrupt that consumed a raw graph input could never be satisfied on
  resume. `create_run()` / `create_run_sync()` therefore take a new
  `inputs=` keyword and `SqliteCheckpointer` persists it to `runs.inputs_data`
  first-write-wins (a resume carries only the answer port and must not clobber
  the originals). Three consequences:

  - **A graph input the serializer cannot encode now fails the run at start.**
    Previously only node outputs went through the serializer, so a live client
    or connection handle passed as a graph input ran fine; it now raises
    `TypeError` naming the run and the offending input. Pass a storable
    stand-in and build the object inside a node, or configure a serializer
    that accepts it (`SqliteCheckpointer(..., serializer=JsonSerializer(lossy=True))`).
  - **`Checkpoint.values` gained the run's own inputs**, layered *underneath*
    the folded step values (a node output shadowing an input name still wins).
    Code that treated `Checkpoint.values` as "node outputs only" now sees more
    keys.
  - **`create_run` / `create_run_sync` gained a parameter.** Third-party
    checkpointers that do not accept `inputs=` must add it; ignoring the
    argument and keeping the inherited `get_run_inputs()` (which returns `{}`)
    preserves the previous behavior exactly. There is no capability probe —
    the ABC and the protocol both declare the parameter, and
    `docs/06-api-reference/checkpointers.md` documents it.

  This also **inverts two resume refusals**, deliberately. Resuming a
  checkpoint whose source run consumed a graph input no later node reproduces
  used to raise `MissingInputError`, because the value genuinely was not
  available; it now restores the input and does real work
  (`test_checkpoint_resume_restores_a_source_run_input`, async and sync). The
  guard itself is unchanged and still fires for a genuinely absent seed.

- **BREAKING (durable record content): raw exception text no longer enters
  durable or telemetry surfaces.** Previously `str(exception)` flowed
  verbatim through `NodeErrorEvent.error` → `RunLog` / checkpoint
  `StepRecord.error` → `RunResult.to_dict()` → OpenTelemetry
  `exception.message`, and (capped) into attempt-ledger rows — so a secret
  embedded in an exception message was persisted and exported. These surfaces
  now store a privacy-safe projection: the exception type name, a stable
  diagnostic code such as `[HG_NODE_FAILED]`, and static wording (for
  example `"ValueError [HG_NODE_FAILED]: Node 'charge' raised ValueError."`).
  Attempt-ledger rows keep the type name with an empty message. **Local
  object surfaces are unchanged**: the raised exception, `RunResult.error`,
  and `FailureEvidence.error` still carry the exact exception object with its
  full message. Code that parsed raw messages out of durable records must
  switch to the exact local objects or the new typed
  `FailureEvidence.diagnostic`.

- **BREAKING (HyperTable): a colliding child identity is refused, not silently
  merged.** Two mapped items under one parent that produced the same child
  identity used to share one child row: both child graphs ran and one item's
  derived values were lost without a word. That write now raises
  `DuplicateChildIdentityError` (a `ValueError` exported from `hypergraph`,
  carrying `table`, `identity`, `value` and `parent`) before any child graph
  runs and before any child row of that parent is written; every child table
  of the row is checked together. Under `on_error="store"` the row is stored as
  an `ERROR` row naming the collision and the other rows proceed. An item
  without the identity field counts as the empty identity, so two such items
  collide too. A row stored earlier over colliding items becomes an `ERROR`
  row on the next write that re-runs its fan-out boundary, never `SKIPPED` or
  `HEALED`. Two fan-outs whose child tables resolve to one name (the name comes
  from the child identity) are now refused at table analysis with
  `GraphConfigError` naming both, instead of writing into one table; so is a
  fan-out whose child table resolves to the root table's own name, which used
  to write its child rows among the root rows (a phantom row in `rows()`, a
  `KeyError` from `child()`), and a root or child table that resolves to the
  recipe journal's reserved name `recipe_journal`, which used to share the
  journal's table. Derive a child identity from something unique per item, and
  give each fan-out its own. (#499, #519)

- **`gen_sync` refuses a rename-table name used as a keyword argument** — the sync-template
  generator rewrites names, not the signatures they bind to, so `f(checkpointer=x)` now
  exits with a `GenerationError` naming the line instead of silently generating
  `f(sync_cp=x)` and a confusing `TypeError` at run time. Dev tooling only; no shipped
  file changed. (#461)

- **`gen_sync` compiles what it writes** — both generator gates now `compile()` the
  generated template instead of only parsing it, so an `await` left in a plain `def` is
  caught before the file is written, and the generator refuses f-string replacement fields
  it cannot read the same way on every supported interpreter. Literal parts of f-strings
  and the documented `# sync:skip` escape hatch are untouched. Dev tooling only. (#491)

- **The `create_v5_schema` … `create_v9_schema` aliases are gone** — all five were aliases
  of `create_v10_schema` in the private `hypergraph.checkpointers._migrate`, each naming a
  schema it did not create. They had no callers; use `create_v10_schema`. (#486)

- **The routed reconcile is a first-class step kind** — HyperTable's write planner drives a
  gate-routed slice as explicit state instead of three mutable sets and a forged provenance
  stamp. Nothing user-visible changed. (#489)

- **A SQLite write path opens a transaction instead of writing one** — the checkpointer's
  write methods share one transaction context manager per family (sync and async) rather
  than hand-writing the begin/commit/rollback scaffold fifteen times. Nothing user-visible
  changed. (#483)

- **`submit()` and `submit_sync()` refuse unknown input fields** — a submission naming a
  graph input the served graph does not have now raises `ValueError` at accept time, naming
  the unknown fields and the expected ones, instead of being stored and refused later by
  the worker. `submit_batch()` items are checked the same way. (#496)

### Added

- **`FailureLogProcessor` writes a failed node's real message and traceback to a
  standard logger.** `AsyncRunner(event_processors=[FailureLogProcessor()])` (or
  `SyncRunner`, `HostRuntime`, `serve`, a per-call `event_processors=`) logs every
  `NodeErrorEvent`'s in-memory `error_detail` to the `"hypergraph.failures"` logger at
  ERROR, one record per event: `node '<name>' in graph '<graph>' failed (run_id=...,
  workflow_id=..., item_index=...): <Type>: <message>` followed by the traceback. It is
  opt-in and persists nothing; every durable record keeps the privacy-safe projection,
  but your log handlers may persist what they receive, so treat the logger's destination
  like any place sensitive text can land. A nested failure logs once per level, innermost
  first; run-level failures that raise no `NodeErrorEvent` are not logged. Exported from
  `hypergraph` and `hypergraph.events`. (#530, closes #500)

- **`SqliteTableStore`: `Table` and `HyperTable` without lancedb.** A `TableStore` on
  stdlib `sqlite3`, exported from `hypergraph.materialization`:
  `Table(identity="k", store=SqliteTableStore("state.db"))` (or `":memory:"`, one database
  per instance). It passes `check_store_conformance`; `compare_and_set` is one transaction
  with exactly one winner across threads and processes; columns whose Arrow type has no
  SQLite type (lists, structs) are stored as JSON. No search and no manifests (so no
  `create_index` and no materialization branches on it). It needs pyarrow, which `Table`
  already needs, and never imports lancedb. (#535, closes #501)

- **Three read verbs a product had been writing for itself (issue #392):
  `RunHomeReadModel.retry_census()`, `node_timings(descend=...)`, and an
  honest inner-cache outcome.** `retry_census(run_ids=None, definition=None)`
  folds the durable attempt ledger into `{run_id: highest attempt reached}` —
  the evidence a node's retries leave *below* the graph, where a Run's status
  never shows them — at one statement per id window rather than the
  per-series question `get_attempt_records` could answer. A Run appears only
  if a node of it was attempt-managed, keyed by the run that executed that
  node, so a nested graph's fourth try stays the nested run's.
  `node_timings(..., descend=False)` reports only the steps each named Run
  committed itself, the counterpart to the existing `runs.parent_run_id`
  fold; the default is unchanged and the selection is identical either way.
  `InnerCacheEvent` gains `shared` and a derived
  `outcome` of `"hit" | "joined" | "computed"`: a call that joined another
  caller's in-flight single-flight did no work of its own, so counting
  computed work as "calls minus hits" was reporting a ceiling. `hit` keeps
  its exact meaning, and a Hypercache that does not report sharing leaves
  `shared` False, which reads as `computed`.

- **BREAKING (Durable Host): several workers may share one Run Home, and the
  exclusive worker lock is gone.** `work_forever()` took an OS-level `flock`
  on the database file, so a second worker failed immediately with
  `WorkerLockError`. That lock never answered the question it existed for —
  "is this half-finished Run dead, or is another worker holding it?" — it made
  the question unaskable, and charged a notebook or a maintenance script the
  right to execute durable work at all, including work only it could
  configure.

  A **lease** answers it, in the shape SQS, Kafka and Oban all ship. The claim
  compare-and-set now also writes `claimed_by` and `lease_until` (schema v7's
  columns, previously inert), a live worker renews every claim it holds in one
  statement at a third of `lease_ttl` (new `work_forever(..., lease_ttl=90.0)`
  parameter), and every poll pass adopts claims whose lease has run out. The
  startup restart scan generalized into that same per-pass scan; at startup a
  worker additionally adopts its own `worker_id`'s outstanding claims
  immediately — this process *is* that worker and is executing nothing — so a
  supervised restart resumes as promptly as it did under the lock.

  **Expiry proves nothing about the old worker, and this does not pretend
  otherwise.** The safety property is `claim_seq`, which already existed: an
  adopted submission carries a NEW claim, and every transition that speaks for
  one execution (`_release_submission`, the worker's dead-letter)
  compare-and-sets on the claim it was handed, so a presumed-dead worker that
  wakes up and finishes commits nothing. That is exactly the guarantee Oban's
  Lifeline documents itself as lacking.

  Four consequences worth checking against your deployment:

  - **`WorkerLockError` is retired.** It is still exported for one release so
    an existing `except WorkerLockError` keeps importing, but nothing raises
    it and the handler is now dead code. `hypergraph.host.worker._WorkerLock`
    and `lock_path_for` are deleted.
  - **A crashed worker's work waits out its lease** (≤90 s by default) unless
    the replacement runs under the same `worker_id`, or the previous worker
    exited cleanly — a clean `shutdown()` surrenders its leases and withdraws
    its registration, so work moves at once. Give each *live* worker its own
    name; reuse the name across a restart of the same deployment.
  - **`compat_state='incompatible'` is no longer parked until a restart.** It
    hid a row from every worker's scan, which was correct when there was one
    worker and wrong the moment a second could serve it. The reset moved from
    worker startup to worker *arrival*: a worker publishing coverage this Home
    has not seen from it before reopens the parked rows, in the same
    transaction as its registration. A repeated pulse of unchanged coverage
    does not, so a row waiting for its deployment still costs nothing.
  - **`PRAGMA busy_timeout` is stated explicitly** (30 s, on the async, sync
    and migration connections) rather than inherited from the driver's 5 s
    default. WAL removes reader/writer blocking, not writer/writer contention,
    and a Run Home now legally carries several writing processes.

  No schema migration: v7 already carried both columns. `RunHomeClient`,
  `watch`, `BatchView`, admission, tolerance, the pause lifecycle and the
  recovery brake are untouched.

- **Durable Host: work can travel as data, and work nobody can do is refused
  or dead-lettered.** Two failures motivated this, both of them "accepted work
  nobody alive can do". A process configured a graph, submitted a 424-item
  batch, and exited; the only process allowed to execute could not *build* that
  Definition — a submission pins its identity, never a way to reconstruct it —
  so every row was marked version-incompatible and sat queued forever with no
  executor and no error. And nothing said so: the park was silent, terminal in
  practice, and invisible to every read model.

  The answer is the pattern Celery and Temporal have shipped for years — a
  submission is data, and each worker holds a registry resolving a name to a
  constructor.

  - **`serve_builder(key, builder)`** registers a CONSTRUCTOR beside the
    instances `serve()` registers; `serve(..., builders={...})` and
    `HostRuntime.serving_builder(...)` are the same registration in bulk and on
    a runtime. `serve()` now accepts a builder-only deployment (no graphs).
  - **`submit(..., builder=(key, args))`** and `submit_batch(..., builder=…)`
    record that address on every row, so any process registering the key can
    rebuild the Definition and execute it. Construction is memoized per
    `(key, arguments)`, so a 500-item Batch builds once.
  - **The pinned identity still decides what runs.** A built Definition is
    verified against it — `BuilderIdentityError` at submit, a
    `builder_identity_mismatch` dead letter at claim. A builder never
    substitutes code for a submission that pinned something else.
  - **`NoServingWorkerError`** refuses a `builder=` address neither this
    process nor any live worker registers — at the call site, before the rows
    exist. Liveness comes from the new `host_workers` registry, which each
    `work_forever()` writes at startup, pulses while it runs, and withdraws on
    a clean exit.
  - **`state='dead_letter'`** replaces the silent park for work nothing alive
    can execute. It is settled (so `watch()` ends and a Batch reaches
    `settled`), reports `WaitingCondition.DEAD_LETTER`, carries a durable
    `dead_lettered` update and a `RunReadModel.dead_letter_reason`
    (`unserved_identity`, `builder_missing`, `builder_identity_mismatch`,
    `builder_failed`), settles its Batch item into a new `dead_letter` count
    bucket, and is revived by `client.rerun()`.

  A rolling deployment still PARKS: when anything alive serves the pinned
  Definition *name*, the row stays `compat_state='incompatible'` and drains
  through `accepts=` exactly as before. Only an address nothing answers to
  dies. Two closed vocabularies each grew by one member — `WaitingCondition`
  and `BATCH_COUNT_KEYS` — and `RunReadModel` gained one optional field.

  Schema **v6 → v7**, additive and inert on open: `host_submissions` gains
  nullable `builder_key`, `builder_args_json`, `claimed_by`, and `lease_until`
  (the last two are the claim lease, described above; they landed in this
  migration so the multi-worker behavior needed no second migration over Run
  Homes carrying live claims), and the new `host_workers` table records
  each worker's served identities, builder keys, and pulse. A v6 database with
  in-flight claimed and parked rows migrates in place with every row untouched.

- **`HostRuntime.serving_builder()` is ready on return.** The method now
  waits for its worker's constructor key to be durably visible in live
  coverage before returning. A client can submit that builder address
  immediately instead of racing the worker's first registry pulse.

- **A deployment can observe durable execution: `event_processors=` on
  `HostRuntime` and `serve()`.** In process an application passes
  `event_processors=` to `runner.run()`; a durable Run had no equivalent,
  because the runner that executes it is built by the library — `HostRuntime`
  constructs an `AsyncRunner` for an unbound graph — so the application could
  reach no runner at all. Its nodes therefore produced no events, and an
  OTel-instrumented client call inside a durable Run exported a **parentless**
  span while the same graph run in process nested under its node. Processors
  are added by the Host at execution rather than baked into one runner, so
  they cover every served Definition **and** a graph that carries its own
  runner (binding a runner to set `max_concurrency` no longer silently turns
  observability off). They arrive ahead of the worker's per-Run preview
  processor, matching the carried-before-call-site order used everywhere else.
  One instance is shared across concurrent Runs, so a processor must be safe
  to use that way; dispatch stays best-effort, so one that raises is logged
  and cannot break the Run, the preview bus, or `watch()`. Passing nothing is
  the default and is byte-identical to before; a bare processor where a
  sequence is expected raises `TypeError` at construction rather than deep
  inside the worker.

- **Visualizations hide edges a longer path already implies (`simplify`,
  default on).** Given `A → B → C`, a direct `A → C` data edge is a shortcut
  past a route the diagram already draws, so it is dropped: the same
  reachability, one less crossing line. Accepted by `Graph.visualize()`,
  `Graph.to_mermaid()`, `HyperTable.visualize()`, `render_graph()` and
  `extract_debug_data()`, and toggleable live from the widget toolbar. Pass
  `simplify=False` (or click the toolbar button) to audit which values each node
  actually consumes — the default tells you `C` runs after `A`, not that `C`
  reads `A`'s output directly; pair it with `separate_outputs=True` for full
  value provenance. Only plain data edges are ever dropped, and only a
  **data-flow** path can justify dropping one: a gate's `gate ⇢ target` means
  "may run", not "received this value", so control and ordering edges never
  stand in for a data edge. Cycle/feedback edges and mutually exclusive branch
  arms are likewise never dropped, reachability is preserved so no node is left
  disconnected, and the reduction re-runs per expansion state because an edge
  that is a shortcut while a container is collapsed can be the only path once it
  expands. A **collapsed nested graph is never assumed to pass values through**:
  a container that consumes one value at one child and emits another from an
  unrelated child carries nothing across, so hiding an edge on that assumption
  would replace the only true route with a false one. Each collapsed container
  is crossed only where `GraphIR.container_transits` says it really carries the
  value — by an unconditional internal route, since a mutex arm inside the box
  is no more of a guarantee than a gate outside it — and an unverifiable
  boundary port is treated as a dead end. Hence IR schema v5: a v4 payload has
  no transit data, so both scene builders reject it outright rather than falling
  back to the old assumption and mis-simplifying. One authority (`viz/_simplify.py`) serves the Python scene
  builder, its JS twin and the Mermaid exporter, so the widget and the text
  export cannot disagree about which edges exist. Note the vocabulary: these are
  **shortcut** edges, never "redundant" — `C` genuinely reads `A`'s output, and
  only the ordering is already implied.

- **Independently paused Batch children now continue when answered** —
  answering a durable pause no longer just stores resume input; settlement,
  the persisted answer, the Run `answer` fact, the new Batch
  `child_runnable` fact, and the submission's `paused → pending` transition
  all commit in **one** transaction, so an accepted answer and a claimable
  run are never separable by process death. A worker then resumes the *same*
  checkpointed workflow id with only `{response_key: answer}` — never the
  pinned start inputs, which strict checkpoint resume refuses as an input
  override — and the answer routes the rest of the graph normally, including
  looping to a second interrupt (a new `pause_id`, parked again).
  `BatchView` gains a distinct **`paused`** count (`active` now means
  claimed and executing) and a `paused` child holds **no** active-Run
  admission slot, so siblings keep running while items wait on people.
  `BatchView.items` is new: one `BatchItemView` per manifest key carrying the
  child's inert `RunRef`, current status and waiting condition, terminal
  outcome, and whether it ever started — the address you answer or stop an
  individual item through. Batch watch gains the `child_paused` and
  `child_runnable` lifecycle facts (repeatable; only `child_settled`,
  `child_unstarted`, `child_abandoned`, and a trip's `unstarted_items` /
  `abandoned_items` account an item for good). Stopping a paused child is a stop, not a duplicate-resolution
  decision: the run settles `STOPPED` with the domain question unanswered.
  Answer-vs-stop and answer-vs-answer races resolve only by commit order, so
  a doubly-answered item can never continue twice.

- **Persisted HyperTable Materialization Branches** —
  `HyperTable.attach(name, graph=..., outputs=...)` records a complete alternate
  recipe over one root collection. `branch.sync()` reuses matching upstream
  lineage, derives only missing or stale columns, allocates a stable physical
  namespace at the first changed grain, and delegates its terminal text/vector
  columns to the existing named query-index policy. Root deletion now discovers
  all registered branch child tables from the store, so document lifecycle has
  one owner even when no branch handle is open.

- **Attempt events, typed diagnostics, and the durable privacy boundary** —
  attempt-managed nodes (declared `retry=` and/or `timeout=`) now emit
  `NodeAttemptStartEvent` / `NodeAttemptEndEvent` per callable invocation
  between one logical `NodeStartEvent` and one terminal
  `NodeEndEvent`/`NodeErrorEvent`; cache hits emit zero attempt events, and
  intermediate failures never bump logical error counts, finish progress
  rows, or close the node span. OpenTelemetry projects attempts as
  `hypergraph.attempt.start`/`.end` span events on the single logical node
  span. Terminal failures carry a frozen, privacy-safe `Diagnostic`
  (`failure.diagnostic`, wire schema `hypergraph.diagnostic/v1`) with ten
  stable `HG_*` codes documented in the errors reference registry. Resuming a
  workflow whose last consumed durable attempt is `OUTCOME_UNKNOWN` now
  raises `AttemptOutcomeUnknownError` with reconcile-then-retry/fork guidance
  instead of silently re-running a node whose external side effects may have
  completed.

- **Beta distribution and human-gated release path** — the distribution is now
  `hypergraph-ai` at `0.2.0b1` with a Beta classifier while the import remains
  `hypergraph`. A GitHub Release is the only production trigger; separate
  Trusted Publishing jobs upload prebuilt artifacts to PyPI or, after an
  explicit manual dry-run input, TestPyPI. A distribution verifier checks the
  wheel and source distribution package trees, viz/inspect assets, changelog,
  and forbidden development directories before publication.

- **Native execution inspect mode** — before, users correlated result status,
  logs, map indexes, values, and failures by hand. Now `SyncRunner` and
  `AsyncRunner` accept `inspect=True` on `run()`, `map()`, `start_run()`, and
  `start_map()`; settled `RunResult.inspect()` / `MapResult.inspect()` return
  one explicit locally interactive display. Current inspection needs no
  checkpointer, handles remain control-only, degraded results disclose values
  that were not captured, and trusted saved notebook output remains interactive
  without a kernel while carrying the documented bounded sensitive values.
  Untrusted saved output retains native expandable terminal evidence instead
  of claiming that active HTML can run through host security policy.

- **Background run and map handles** — `SyncRunner.start_run()` /
  `start_map()` and `AsyncRunner.start_run()` / `start_map()` return
  process-local `SyncHandle` / `AsyncHandle` controls with only `done`,
  cooperative `stop(info=...)`, and `result(raise_on_failure=...)`. Async start
  calls return a handle without `await`, and cancelling one result waiter does
  not cancel framework-owned execution. Blocking runner behavior is unchanged.

- **Truthful stopped-map scope** — curtailed background maps keep only real
  claimed `RunResult` children and expose the original scope through
  `MapResult.requested_count` and `unstarted_item_indexes`. Parent event,
  checkpoint, and OTel outcomes align on `STOPPED`; existing batch counts
  continue to count real outcomes only.

- **Background-control guides and examples** — added the task-based control
  guide plus runnable synchronous and asynchronous examples covering immediate
  return, retrieval policy, cooperative stop, and waiter cancellation.

- **Truthful restored-map provenance** — checkpoint-skipped map children now return `RunResult(restored=True)` with a visible non-error `NodeRecord(status="restored")`. `MapResult.restored_count`, `MapLog.restored_count`, `RunEndEvent.batch_restored_items`, and `hypergraph.batch.restored_items` expose the restored subset while completed counts stay inclusive. Duration averages include only fresh completed items with real logs.

- **Internal-step inspection escape hatch** — `get_steps(..., show_internal=True)`, SQLite `steps(...)`, and `RunInspector.steps(...)` can expose retention carrier rows for debugging; public views hide them by default while state reconstruction still folds them.

- **`WorkflowStoppedError`** — a bare rerun of a persisted stopped workflow now fails before events or persistence writes. Pass a non-empty runtime mapping to resume the same lineage, or `override_workflow=True` to fork.

- **HyperTable child fingerprints** — child rows now have real fingerprints computed from the child's source inputs, child graph node hashes, and component config hashes (scoped to the child graph, not the parent). Re-inserting a parent skips children whose inputs and graph definition haven't changed. Makes insert naturally resumable after crashes.

- **HyperTable `on_error` policy** — `HyperTable(..., on_error="store")` writes error rows instead of raising on derivation failure. Error rows preserve source columns and identity with `_status="error"` and `_error="{ExceptionType}: {message}"`. Successful siblings are unaffected. Error rows are retried (not skipped) on the next insert/sync. Default `on_error="raise"` preserves backward compatibility. Works for both parent and child rows, and with both `SyncRunner` and `AsyncRunner`.

- **`include_status` on read methods** — `get()`, `children()`, `filter()`, and `filter_children()` accept `include_status=True` to expose `_status` and `_error` fields. Without it, these internal fields are stripped.

- **`SyncResult.errors`** — `sync()` with `on_error="store"` populates `SyncResult.errors: tuple[ErrorRow, ...]` for programmatic inspection of which items failed, alongside the existing `errored` count.

- **Reserved column name validation** — identity and source columns named `_status`, `_error`, `_row_fingerprint`, `_write_gen`, `_parent_id`, or `_provenance_*` are rejected at graph analysis time with a clear error message.

- **`SyncRunner` takes runner-level `event_processors`** — `SyncRunner(event_processors=[...])`
  now mirrors `AsyncRunner`: processors given to the constructor are dispatched on every
  `run()` and `map()` call, ahead of any passed at the call site. `max_concurrency` was
  deliberately not added — a sync engine has no concurrency budget, so there is no
  runner-level default to apply. (#476)

- **`map_iter(workflow_id=...)`** — `map_iter()` accepts `workflow_id=`, and item *i* runs
  as `<workflow_id>/<i>`, so streamed chunks from nested and mapped graphs route by the
  documented `(workflow_id, node_name)` key instead of collapsing onto a single `None`.
  It is identity only; passing it while a checkpointer is attached raises `ValueError`
  pointing at `runner.map(..., workflow_id=...)`. (#484)

### Fixed

- **A failed pending-boundary batch rolls back instead of riding the next commit.**
  `SqliteCheckpointer.record_pending_nodes{,_sync}` opened no transaction of their own, so
  a batch whose later row failed left its earlier rows in the connection's open
  transaction and the next unrelated write committed them — a superstep's siblings could
  become half-attributable, the state the per-node boundaries exist to prevent. Both now
  use the store's write-transaction context managers: the batch commits whole or not at
  all, and the error re-raises unchanged. (#527)

- **Gates and interrupt handlers receive the `NodeContext` they declare.** A `@route` /
  `@ifelse` gate function or an `@interrupt` handler with a `ctx: NodeContext` parameter
  failed at run time with a missing-argument `TypeError`; under `SyncRunner` and
  `AsyncRunner` it now gets the same context a `@node` gets, carrying the gate's own ids, so
  a routing decision can read `ctx.stop_requested` and a gate's `ctx.stream()` chunk names
  the gate. An interrupt handler's `ctx` exists only on the question path; a
  `ctx.record()` fact the store refuses there fails the run instead of pausing it (R22).
  `DaftRunner` still injects no `NodeContext`. (#528)

- **A failing fan-out boundary keeps the columns that succeeded.** Under
  `on_error="store"`, a failure in a `map_over` boundary nulled every derived column of the
  row (a total-loss ERROR row). It now stores a PARTIAL row with one `ColumnChange` naming
  the `map_over` input (reason `NODE_ERROR`), and the next `sync()` re-runs the boundary
  and rebuilds the child rows. A partial row now names every node that failed: a failed
  top-level node that owns no stored column (a side-effect node) makes the row a
  total-loss ERROR row with a full retry — before, when another column was also nulled, it
  was stored PARTIAL and the failed node was never run again. (#529)

- **`sync()` heals a child error under a fan-out boundary that also produces a parent
  column.** For that shape `sync()` reported SKIPPED forever while `insert()` of the same
  row repaired it; both now give the same answer. The repair runs the parent's nodes once
  (the child graph stays scoped to the damaged child) and rewrites the parent row only
  when a recorded stamp moved or a stored value changed, and the receipt says so (HEALED,
  or UPDATED while a child is still in error). A stale boundary count, or a boundary whose
  item list changed between runs, no longer re-runs the whole graph on every write; that
  loop existed for `insert()` on master too. (#532)

- **A colliding child identity no longer reports HEALED forever.** A fan-out boundary's
  provenance stamp now records the number of distinct child identities (what the child
  table keeps), not the raw item count, so a parent whose mapped items share an identity
  settles to SKIPPED with zero executions. Child identities must be unique per parent; two
  items with one identity occupy one child row. Migration: a table whose stored rows
  recorded a colliding identity re-runs that fan-out boundary once on its next write, then
  settles. (#534)

- **A nested run a dead worker left `active` settles with its submission.** A worker killed
  mid-run left the inner page-recipe run it had started `active` forever under a completed
  parent, and the Home's listings kept returning it. In the transaction that settles the
  submission (finished, or parked recovery-exhausted), every nested run still `active`
  beneath it is now settled `STOPPED` with `reason: "abandoned_incarnation"` on its
  `status` run update, and its never-started node boundaries are dropped. The Run's own
  row and `PAUSED` nested runs are never touched; a crash-resumed nested graph still
  resumes under its own id. (#533)

- **A raising log handler can no longer replace a node's error.** When an event processor
  failed, the dispatcher's own warning went through the same logging handlers; a handler
  that raised again escaped processor isolation and replaced the node's exception in the
  raised error, the failure evidence, the step record and the Host outcome. The warning is
  now guarded in `emit` and `emit_async`. (#530)

- **Docs: `Graph.select` prunes execution, and `ProcessLocalLimiter` is not a
  provider-quota owner.** `select()`'s docstring said every reachable node still runs; the
  runner has pruned non-contributing nodes since the scheduler adopted the canonical scope,
  and a test now pins it for both runner families, flat and nested. The limiter docs no
  longer recommend `ProcessLocalLimiter` / `provider_limit` for an HTTP provider's API
  quota: a node or graph permit is held for the whole node execution — retries, backoff and
  in-body cache hits included — so it budgets a scarce process-local resource (a GPU, a
  local model, a subprocess pool, database connections); a provider quota belongs at the
  client's transport, which admits each attempt (for example hyperlimit's
  `LimitedTransport`). Two `How to fix` hints were reworded to match. (#531, closes
  #502, #503)

- **Two child tables over one `map_over` input open on both shipped stores.** The table
  analysis emitted the fan-out's `_provenance_<map_input>` column once per child spec, so a
  graph with two child tables over the same mapped input could not open on
  `LanceDBStore` ("Duplicate field name") or `SqliteTableStore`; it is now emitted once per
  input. Also from the wave's integration review: `normalize_value` no longer retries a
  failed `import numpy` on every call when numpy is absent. (#536)

- **`to_mermaid()` output with an END node renders in Mermaid 11.** The END node's style
  class was emitted as `classDef end` / `class __end__ end`, and `end` is a reserved
  Mermaid word (it closes a `subgraph`), so any graph with a gate that can end the run
  failed to parse. Emitted class names now go through the same reserved-word guard as node
  ids: the END class is emitted as `endNode`, and every other class name is unchanged.
  `to_mermaid(colors={"end": {...}})` still restyles the END node. (#585)

- **`visualize()` arrowheads point along their edge in Safari and other WebKit
  browsers.** Every edge path ended with a curve segment whose control points sat on
  its end point, so the path gave no end direction of its own. Chromium borrowed the
  direction from earlier in the path; WebKit drew every arrowhead pointing right, beside
  the line and half under the target node. Since the dagre-routing rewrite, this has
  affected every edge kind. The path now ends with a straight segment, as D3's
  `curveBasis` does, and the drawn line is unchanged. (#587)

- **`visualize()` edges leaving one node no longer cross just under it.** Dagre orders
  each edge's first bend by the node above it. Edges leaving the same node therefore
  tie, and the tie-break could put them in the opposite order to where they go next.
  A gate's True edge headed left and then curved right across its False edge, and a
  function node's consumers tangled the same way. The layout now hands those first
  bends back out in the order of each edge's next point, so edges leave a node in the
  left-to-right order of their targets. The fix covers flat graphs and expanded
  containers. Dagre's positions are kept, and a gate still exits from its bottom
  vertex. (#590)

- **`visualize()` arrowheads point into their node, and heads on one node no longer
  overlap.** Each edge ended where the line from its last bend met the node's box, and
  that point was then clamped into the border. A bend far to one side therefore landed
  almost flat, its head lying along the border. Two edges could also be clamped onto
  one point, so one head hid the other: a gate's True edge and a side input landed
  together this way. Edges into one node now land at least a head's width apart, in
  the order they arrive. A diamond's entries spread along its two upper edges. Any
  approach steeper than 30° from straight down gets a short vertical landing, so every
  head points into its node. The fix covers flat graphs and expanded containers. Node
  positions and dagre's bends are unchanged. (#592)

- **A durable Run could fail with "database is locked" without executing a
  single node.** The SQLite store shared ONE synchronous connection across
  threads, which closed a three-party cycle: the event loop held a
  `BEGIN IMMEDIATE` open across its `await`s, the Run executing in a
  `to_thread` worker held the sync lock and waited for that write lock, and
  the loop then made one of the documented synchronous reads (`get_run`,
  `state`, `values`, the `*_sync` client mirrors) and waited for the sync
  lock. Nothing could commit until `busy_timeout` expired thirty seconds
  later — and it was the RUN that failed, not the reader. Each thread now
  gets its own connection, so a reader never queues behind another thread's
  writer, which is what WAL promised all along; cross-thread arbitration is
  SQLite's write lock alone, the same one two worker processes on one Run
  Home already rely on.

- **A second worker on a busy Run Home could die at startup with "database
  is locked".** Every new connection re-declared `PRAGMA journal_mode=WAL`,
  which takes a database-wide lock and is the one statement that does NOT
  consult the busy handler — so a connection opened while another was
  mid-write was refused outright, however long it had just said it was
  willing to wait. The journal mode belongs to the FILE and persists, so it
  is now settled once per store (waiting out a genuine conversion) instead
  of re-declared by every connection.

- **A Batch item key containing `/` was accepted and then never claimed —
  the item read `queued` forever and the Batch never rested.** The child
  workflow id is composed as `<batch workflow_id>:<item key>`, and `/` is
  reserved in a run id for hierarchy (`<parent>/<node>`), so
  `validate_workflow_id` raised — but inside the worker THREAD, long after
  acceptance. The submission stayed `claimed` with no error, no dead letter,
  and no diagnostic: indistinguishable from a slow worker. The key is now
  refused at `submit_batch()` with `ItemKeyError` naming the offending key
  and the reason, rather than escaped — an escaped key would read back
  differently in every keyed outcome, view, and rerun selector. The same
  refusal now guards the caller-chosen `workflow_id` on `submit()`,
  `submit_sync()`, and `submit_batch()` (a `ValueError` at the door), where
  it had the identical silent-stall symptom; a Batch id carrying `/`
  poisoned every child of the manifest. Only `/` is refused — markup,
  colons, spaces, and non-ASCII still key items verbatim.

- **`watch(run_ref)` could never end for a Run that settled without ever
  executing.** The stream's end condition asked only whether the submission was
  `finished`, so a run parked by the recovery brake — or now dead-lettered —
  with no runs row left the caller waiting forever on work that could not move
  again. It now uses the same `is_child_settled` rule `BatchView` uses.

- **Unchanged-parent `sync()` heals physically missing child rows** — before,
  the unchanged-parent fast path never inspected child tables, so a deleted
  or lost child row stayed missing forever behind a `skipped` receipt. Now
  `sync()` compares each child table's recorded fan-out count against the
  physically present deduplicated child rows (one read-only child-table probe
  per unchanged parent) and rebuilds only the missing children: the fan-out
  boundary re-runs once to regenerate the item list, present children and
  parent derived columns are not re-derived, and healed child writes allocate
  generations strictly above every physical child row. The repair is reported
  by the new `WriteOutcome.HEALED` / `TableReceipt.healed` — a receipt is
  never `skipped` on a path that wrote rows. All children present remains a
  zero-execution, zero-write skip.

- **HyperTable definition fingerprints harden to construction-time
  `hash_definition`** — before, a derive node that was a bound method of a
  configured object (`summarizer.summarize` with `model="gpt-4"` vs
  `model="o3"`) produced the SAME recipe fingerprint, so changing the
  configuration silently skipped the re-derive; and a dynamically-created
  function (exec/eval, no retrievable source) hashed its `repr` — a
  per-process memory address — so its rows re-derived on every run. Now a
  producing node's recipe identity is its `definition_hash`, captured ONCE at
  node construction via the repo's single definition-identity function
  `hash_definition`: bound methods mix in the instance's configuration,
  dynamic functions hash their bytecode, and builtins hash their qualified
  name. Because identity is frozen at construction, instance state that
  mutates during execution (a call counter, a cache, a client) does NOT drift
  the recipe — three inserts through the same node stamp one fingerprint.
  One-time migration consequence: rows whose recipes involve bound methods,
  dynamically-created functions, builtins, partials, or callable objects
  carry a changed fingerprint and re-derive once on the next
  `insert`/`update`/`sync`; component-config and bound-value journal entries
  re-journal under content hashes, and a functionless subgraph producer's
  journal entry re-keys from a meaningless shared hash-of-`None` to the
  node's own definition hash. Rows derived from ordinary module-level
  functions keep their exact previous fingerprint (both schemes hash the
  function's source text) and are not re-derived. Callable objects whose
  state cannot be fingerprinted now fail loudly with guidance to define
  `cache_key()` instead of drifting forever, and a non-callable that carries
  no `definition_hash` is rejected instead of silently hashing its repr.
  Routed union columns (several producers writing one column): a named
  index's recipe fingerprint is now the order-free combination of EVERY
  producer's recipe — previously it hashed the producer tuple's repr, which
  differed in every process, so such indexes always read as stale; existing
  union-column indexes flag stale once more and then stay current. And
  `explain()` no longer attributes a union column to whichever producer was
  listed first in the graph: it returns
  `{"producers": {<node name>: {"provenance", "source"}}}` with every
  producer labeled (single-producer columns keep the flat
  `{"provenance", "source"}` shape); row-actual attribution would require a
  durable producer identifier on the row and is not recorded.

- **Truthful notebook scheduler availability** — before, an
  `add_callback`-only kernel could look cross-thread capable while lacking the
  delayed owner-thread call needed for the 250 ms live-inspection update. Now
  delayed calls and cross-thread marshalling are checked independently. When a
  nonterminal view lacks a required capability, Hypergraph creates a closed
  `Live inspection unavailable` initial snapshot and does not subscribe to the
  inspection session. That initial notebook record is not settled execution
  truth; settled truth remains available through `result.inspect()` or
  `batch.inspect()` after the run or batch returns. An already-terminal initial
  artifact remains a closed `Saved snapshot`. A scheduler can also reject
  `call_later()` only after a worker's callback reaches the owner thread. That
  late owner-thread delayed-arm rejection now closes and detaches the live
  observer. If its display channel still works, Hypergraph writes one
  best-effort stale `Live inspection unavailable` settlement from the latest
  bounded artifact; the rejected payload is never shown as live. Failure of
  that final display update is observational, and the observer remains closed.
  This observer settlement does not change settled execution truth; collected
  `result.inspect()` or `batch.inspect()` remains authoritative.

- **Executable inspect recovery and nested failure attribution** — before, a
  copied full-renderer snippet dereferenced `None` or raised `StopIteration`
  when a transient failure disappeared on recovery, and full-renderer
  run-boundary, batch-boundary, and start-failure views omitted the recovery
  policy already shown by the native summary. Now both surfaces use captured
  sync/async provenance. Each retry assignment is inside `try`/`except` and
  uses `error_handling="continue"`: a persistent infrastructure exception
  prints its real type and message without reading an unbound result, while a
  transient boundary prints the settled successful result or batch. A returned
  failed result prints its real run/item error; map evidence uses
  `batch.failures` or the original item position and never a nonexistent
  `MapResult.error`. For sparse run-boundary results, the snippet translates the
  original item index around `unstarted_item_indexes` before indexing
  `batch.results`; it fails closed when that item never started or is outside
  the requested scope. Unknown provenance still emits no runner call.
  Before, primary **Show failure** selection on a nested mapped graph could stop
  at the aggregate container (`review_group`) and show its list input. Now the
  full inspector and native summary correlate the containing outer item to the
  explicit slash-qualified failing leaf, such as
  `review_group/review_customer`, with its scalar failing input while retaining
  distinct peer failures. Correlation is established from raw Python evidence
  before error/input presentation serialization and carried by an opaque
  internal occurrence identity: changing `repr()` output is never identity,
  no object address, input value, or secret enters the key, and correlation
  never invokes caller-defined equality or hashing for workflow IDs or captured
  values; captured values are correlated only by object identity. Distinct
  executions retain separate identities even when they reuse the same scalar
  and exception objects and record equal durations. A missing exact leaf fails
  closed at the run boundary instead of borrowing the container.

- **Trust-safe saved inspect evidence** — before, a notebook that treated new
  output as untrusted could strip scripts, styles, iframes, and identifiers,
  leaving a blank terminal record even though Python had settled at
  `partial / 2 completed / 1 failed`. Now terminal and stale channels include
  one small native `<details>` summary derived from the existing bounded
  payload. It exposes `First failure of N`, original item, qualified node,
  bounded inputs, exception evidence, a result-evidence snippet, and
  `docs/05-how-to/debug-workflows.md`. A complete safe exception keeps its
  exact node/run/batch label. Repr-backed evidence uses **Exception preview
  (bounded repr)**, truncated previews retain the original character count,
  and a placeholder uses **Exception details unavailable** with its reason. An
  opaque repr keeps its exception type once, while a repr already beginning
  with the type is not duplicated. Copy-faithful whitespace, valid
  `<pre><code>` nesting, and copy-inert wrap opportunities preserve inputs,
  exceptions, and recovery snippets without overflowing a 360px page or
  altering copied text. Status-only failures contribute once to
  `First failure of N` without duplicating stronger evidence. **Exact run
  exception** and **Exact batch exception** remain
  separate from attributable node evidence. Generated recovery snippets use
  only public runner/result APIs. Sync snippets call `runner.run(...)` or
  `runner.map(...)` directly; async snippets use `await runner.run(...)` or
  `await runner.map(...)`. If the runner kind was not captured, recovery code
  is unavailable instead of silently choosing sync. Returned-value snippets
  rerun with `error_handling="continue"` before reading a result. Before, an
  async or repr-backed failure could show sync-only code or an exact heading;
  after, the call syntax and label match the captured evidence. Trusted output
  still gets the full sandboxed portable inspector and hides the compact
  summary only when active HTML runs and the frame retains a non-empty local
  `srcdoc`.
  Explicit failures without a stable node match retain their own facts instead
  of borrowing a same-name node. Hypergraph never auto-trusts or signs a
  notebook, calls a server trust endpoint, weakens the iframe sandbox, changes
  ACK authentication, or adds a public transport setting.

- **Portable saved inspect delivery** — before, notebook hosts that isolate
  each saved output could show the first `pending / 0` shell because later
  payload scripts could not reach sibling output documents. Now the terminal
  channel is a self-contained portable inspector. The normal capable path
  still has exactly two physical outputs because `DisplayHandle.update()`
  replaces that channel in place. When the kernel environment reports exact
  `jupyter-server-nbmodel==0.1.1a4`, Hypergraph uses a best-effort append path
  because that executor drops `update_display_data`: ordinary updates retain
  hidden coalesced payload-only history, then settlement adds one terminal
  physical record. Shared Jupyter hides the portable fallback only after the
  original iframe accepts and applies the authenticated update; an
  isolated-output host can open the terminal record alone. Missing or
  unrecognized versions keep the normal update path. A
  separate server environment cannot be inferred from kernel package metadata.

- **Observational inspect serialization** — captured node inputs, outputs, and
  requested map inputs now use a private `CapturedMapping` snapshot adapter.
  The shallow snapshot supports `copy.copy`, `copy.deepcopy`,
  `dataclasses.asdict`, and pickle round trips; captured mappings are not stored
  as `MappingProxyType`. Separately, structured source-value rendering accepts
  only exact built-in containers, ordinary dataclasses, recognized Pydantic
  models, exact NumPy/pandas adapters, and a user-supplied `MappingProxyType`
  backed by an exact `dict`. The NumPy and pandas adapters dispatch on exact
  class identity, resolved from the module that really defines the class and
  cross-checked against the public alias, not on mutable public aliases; a
  hijacked alias withdraws the adapter instead of redirecting it. A Pydantic
  model is only ever a subclass of `BaseModel`, so that adapter accepts any
  subclass and never calls the model: fields are read from the instance
  `__dict__`.
  Within documented rank and size limits, exact NumPy arrays stay structured.
  An exact pandas DataFrame is read through pandas' public API (`shape`,
  `columns`, `iloc`), and only for the displayed corner, so storage layout
  never decides whether a frame renders: NumPy-backed, nullable
  extension-backed and Arrow-backed DataFrames all render as bounded tables, as
  do datetime columns. If that bounded read raises, the value becomes a bounded
  `unsupported DataFrame storage` placeholder without calling DataFrame `repr`,
  so an unreadable frame degrades instead of failing the run.
  Unsupported subclasses and
  custom protocols use a bounded whole-value `repr` fallback. A proxy backed by
  a custom mapping uses the same fallback. Custom `repr` remains ordinary
  Python user code, so Hypergraph cannot prevent or undo its side effects;
  raised errors become placeholders without replacing the run status.

- **Background inspect workflow identity** — checkpointer-backed sync and async
  `start_run(..., inspect=True)` calls that omit `workflow_id` now bind their
  generated ID before restored or node evidence is published. The settled
  result and inspection view agree while handles remain control-only.

- **Checkpointer run-filter parity** — async Memory/SQLite and SQLite sync reads now compose `graph_name`, inclusive UTC-normalized `since`, status, and parent filters before limit. Omitting `parent_run_id` returns all runs; explicit `None` returns top-level runs only. `count_runs()` uses the same three-state parent contract.

- **Source-derived fork IDs** — before, `runner.run(..., fork_from="job-1")` generated an unrelated `run-...` ID; now it yields `job-1-fork-<hex>`. Explicit targets remain exact, retries keep generic runner IDs, and missing/nested implicit sources fail without creating a run.

- **`set_children` parent scoping** — the cleanup predicate in `set_children` now includes `_parent_id`, preventing accidental deletion of another parent's children when child identity values overlap.

- **A gate target naming a missing node raises `GraphConfigError` for every gate shape** —
  a typo in `@route(targets=[...])` or `@ifelse(when_true=..., when_false=...)` escaped as
  a raw `networkx.exception.NetworkXError` whenever the gate declared two or more non-`END`
  targets, because the graph was built before gate targets were validated. Gate targets are
  now validated first, so every shape gets the named error with the available nodes and a
  "did you mean" suggestion. (#477)

- **A rerun carries the source's builder address** — `client.rerun()` rebuilt a submission
  field by field and copied everything except `builder_key` / `builder_args_json`, so the
  repeat landed with a NULL address and parked as `builder_missing` on a builder-only
  fleet. Both columns are now carried, for single runs and for Batch children. (#478)

- **Mermaid draws a renamed boundary output from its inner producer** — with the container
  expanded, an edge out of a renamed boundary output pointed at an id that was never
  declared, so Mermaid rendered a phantom unstyled box while the real port pill had no
  consumer. The edge now starts at the deepest visible producer, through any number of
  expanded levels. Two frozen baselines changed by one line each. (#479)

- **A name bound inside a child graph is recipe, not a child column** — a value the child
  graph `bind()`s got a content-key source column on the child table that nothing could
  ever write, so it stayed NULL forever. No column is built for such a name now, a NULL
  column an older store already holds is ignored on read and on rebuild, and
  `ChildTable.set()` refuses a child-bound name. Dropping that dead value from rebuilt
  items moves the child fingerprint, so an existing store re-derives the child rows it
  flips once, then settles. (#487)

- **`ctx.record()` chooses its write path by loop identity** — a plain `def` node body that
  drove its own asyncio loop (wrapping an async client, say) failed the whole run with
  `WrongEventLoopError` under `AsyncRunner`, because the path was chosen by "is any loop
  running" rather than by which loop the executor owns; the identical body under
  `SyncRunner` completed. A lost fact now fails the node on every runner and body pairing,
  and a `try`/`except` around `ctx.record()` still changes the body's control flow but no
  longer turns a dropped fact into a completed run. The `NodeContext` constructor keyword
  `records_on_loop` is now `record_loop` and takes that loop instead of a boolean. (#493)

- **A compacted resume hands a loop's restored value to its consumer** — under
  `retention="latest"` or `retention="windowed"`, a resumed run whose looping node had its
  earlier step row compacted away passed the consumer its *default* instead of the restored
  value, with no error and the right value sitting in `result.values` of the same result
  object. Compacted resumes now match `retention="full"` on both runner families and every
  checkpointer backend. (#494)

- **A submission its Definition refuses to start settles as a dead letter** — a claimed
  submission whose execution raised before any run row existed sat `claimed` past its
  lease forever, renewing the lease and holding an admission slot while nothing ran. It is
  now settled as a dead letter with reason `start_refused`, the worker logs the traceback,
  and `client.rerun()` accepts it. (#496)

### Changed

- **`MapResult.failed` now mirrors the aggregate status (alpha breaking
  change)** — `batch.failed` is True only when `status == RunStatus.FAILED`
  (at least one item failed and none completed), matching `RunResult.failed`.
  Use the new `batch.any_failed` (`bool(batch.failures)`) for "did anything
  fail" regardless of status. Consequences: a 99/100-success batch is
  `partial`, not `failed`, and `batch.stopped` and `batch.failed` can no
  longer both be True — a curtailed stopped batch with real attempted-item
  failures reports `stopped=True, failed=False, any_failed=True`.
  ([#296](https://github.com/gilad-rubin/hypergraph/issues/296), decision
  [#251](https://github.com/gilad-rubin/hypergraph/issues/251))

- **Active workflow identity covers every execution shape** — a runner permits
  only one active execution per `workflow_id`; duplicate blocking/background
  starts fail before a second handle is returned. Different IDs are
  independently controllable without a parallelism or start-order guarantee.

- **GraphNode boundary projection (breaking refinement)** — `Graph.as_node()` is flat by default again: a wrapped graph's inputs and outputs appear in the parent graph under their local names. Use `Graph.as_node(namespaced=True)` to project a boundary under the resolved GraphNode name, e.g. `retrieval.query` and `retrieval.docs`. ([#97](https://github.com/gilad-rubin/hypergraph/issues/97))

  **Why:** common parameters such as `query`, `messages`, and `config` often intentionally belong to the parent flow. Namespacing is still available when sibling subgraphs need independent parameters with the same local name.

  **Migration.** Code that was updated for the earlier namespaced-by-default behavior can usually remove the prefix:

  ```python
  # Before
  runner.run(outer, {"inner.x": 5})

  # After, for the default flat boundary
  runner.run(outer, {"x": 5})
  ```

  Keep the prefix by opting into a namespaced boundary:

  ```python
  outer = Graph([inner.as_node(namespaced=True)])
  runner.run(outer, {"inner.x": 5})
  ```

  Namespaced inputs also accept nested-dict sugar:

  ```python
  runner.run(outer, {"inner": {"x": 5}})
  ```

  Use `.expose(...)` to bring selected namespaced ports back into the parent flat flow:

  ```python
  retrieval = retrieval_graph.as_node(namespaced=True).expose("query")
  generation = generation_graph.as_node(namespaced=True).expose("query")
  outer = Graph([retrieval, generation])
  runner.run(outer, {"query": "what is hypergraph?"})
  ```

- **Resolved GraphNode port addresses everywhere** — `GraphNode.inputs`, `GraphNode.outputs`, `GraphNode.data_outputs`, `graph.inputs`, runtime values, bind keys, checkpoint step values, `wait_for`, visualization, and Mermaid all use the same parent-facing port addresses. A cyclic value such as `messages` may appear in both a GraphNode's inputs and outputs.

- **Expose replaces, not aliases** — On a namespaced GraphNode, `.expose("query", answer="final_answer")` replaces `retrieval.query` / `retrieval.answer` at that boundary with `query` / `final_answer`. Expose targets current local port names, not already-projected addresses. Multiple GraphNodes may expose inputs to the same parent input; duplicate aliases inside one GraphNode are a build-time error. A single GraphNode may use the same address as both input and output only when it is the same local cyclic seed/update port.

- **Run-time override of a bound value emits a `UserWarning`** — Passing a value at `runner.run(...)` for a key already present in `inputs.bound` is allowed but warned. The warning shows the old and new value for primitive types and a generic message for opaque types, so accidental overrides surface in test logs.

- **Bind precedence is parent-first at a projected address** — If an inner graph bind and an outer graph bind project to the same parent-facing address, the outer bind wins and emits a warning when the effective bound inputs are computed. If sibling inner graph binds project different values to the same flat address, graph construction errors instead of choosing one silently. Runtime values can still override the effective bind and emit the warning above.

- **`rename_inputs(...)` and `rename_outputs(...)` target local names** — GraphNode renames operate on the current local port names before boundary projection. `map_over(...)` and `clone` accept either current local names or projected parent-facing input addresses, then normalize to local names internally. Changing the GraphNode name recomputes namespaced addresses; exposed aliases stay flat.

- **GraphNode boundary hashes include the projected surface** — `definition_hash` / `structural_hash` now include boundary namespacing, exposed-port mappings, projected inputs/outputs, and local renames. Existing checkpoint compatibility and cache keys may change after upgrading graphs that use nested composition.

- **Nested dict input sugar is only for namespaced addresses** — Flat GraphNodes no longer accept `{"inner": {"x": ...}}` as a way to address child inputs. Pass the flat key directly (`{"x": ...}`), or opt into `as_node(namespaced=True)`.

## March 2026

### Added

- **Checkpoint lineage exceptions** — `WorkflowAlreadyCompletedError`, `GraphChangedError`, `WorkflowForkError`, and `InputOverrideRequiresForkError` for explicit resume/fork guidance.
- **`checkpoint=` on `runner.run()`** — explicit fork entrypoint from a saved checkpoint snapshot (`values + steps`).
- **First-class fork/retry helpers** — low-level `SqliteCheckpointer.fork_workflow()` / `retry_workflow()` (sync) and `fork_workflow_async()` / `retry_workflow_async()` (async) prepare lineage-aware checkpoints when you need manual control.
- **Graph `structural_hash`** — structure-level compatibility hash used to guard same-`workflow_id` resumes.
- **`graph.describe()`** — compact human-readable graph summary covering scoped inputs, bound values, outputs, and active nodes. Type hints are shown by default, with `show_types=False` for name-only output.
- **Run lineage metadata** — persisted `forked_from`, `fork_superstep`, `retry_of`, `retry_index` fields on runs.
- **Gate decision value persistence** — gates now emit internal `_gate_name` values so routing intent is checkpoint-visible and reconstructible.
- **Auto-generated workflow IDs for `run()`** — when a checkpointer is configured and `workflow_id` is omitted.
- **`override_workflow` on `run()`** — convenience auto-fork mode: when a `workflow_id` already exists, pass `override_workflow=True` to branch to a fresh lineage instead of raising strict resume errors.
- **Workflow-id based forking/retrying on `run()`** — `fork_from=` and `retry_from=` remove manual checkpoint plumbing for common branch/retry flows.
- **Checkpointer naming convention updated** — sync helpers are now `fork_workflow()` / `retry_workflow()`, async helpers are `fork_workflow_async()` / `retry_workflow_async()`.
- **Persisted paused workflows** — checkpointers now store interrupt-paused runs as `PAUSED` instead of overloading `ACTIVE`, and CLI/dashboard views expose paused runs distinctly.

### Changed

- **Resume contract is strict** — same `workflow_id` now means same lineage only:
  - completed workflows are terminal
  - runtime input overrides require explicit fork
  - structural graph changes require fork
- **Checkpoint state reconstruction** — restore now replays version counters from step history (instead of remap-style flattening), improving cycle correctness and stale-node detection.
- **Unified startup scheduling** — first-run readiness is predecessor-driven for both implicit and explicit edge graphs (no split behavior by edge mode).
- **Canonical graph scope** — execution scope is graph-configured (`with_entrypoint`, `select`, `bind`) and shared by scheduler, validation, and visualization.
- **Runtime scope overrides removed** — passing runtime `select=` or `entrypoint=` to runners now raises `ValueError`. Configure scope on the graph instance instead.
- **Cycles require constructor entrypoint** — constructing a cyclic graph without `Graph(..., entrypoint=...)` now raises `GraphConfigError`.
- **Internal output injection tightened** — user-provided values for edge-produced internal parameters are rejected deterministically.
- **Visualization defaults simplified** — `.visualize()` now shows unbound external inputs by default via `show_inputs=True`, keeps bound inputs hidden unless `show_bounded_inputs=True`, and never renders shared params as external inputs.
- **Visualization edge contract tightened** — rendered views now follow the Python-precomputed edge set (no JS-only transitive pruning), so what you see matches the canonical NetworkX topology.
- **Implicit producer shadow-elimination** — for contested input `p`, edge `u -> v (p)` is removed iff every valid path from `u` to `v` for `p` crosses another producer of `p` first; unresolved cases raise `GraphConfigError` at build time.

## Recent Merged PRs

### [PR #61](https://github.com/gilad-rubin/hypergraph/pull/61) (Merged February 28, 2026) - DiskCache HMAC integrity hardening

- Added HMAC-SHA256 signing for `DiskCache` payloads using a per-cache-directory secret key.
- `DiskCache.get()` now verifies HMAC integrity **before** deserialization (`pickle.loads`), preventing untrusted/tampered payload deserialization.
- Added atomic HMAC key initialization (`O_CREAT | O_EXCL`) to avoid race conditions when multiple processes initialize the same cache directory.
- Added broader recovery behavior for corrupted or legacy cache entries:
  - non-bytes payloads are evicted and treated as cache misses
  - missing/invalid HMAC metadata is evicted and treated as cache misses
  - deserialization failures evict the bad entry and return miss
- Expanded disk cache integrity tests to cover tampering, key initialization races, and bad metadata handling.

### [PR #59](https://github.com/gilad-rubin/hypergraph/pull/59) (Merged February 26, 2026) - Non-TTY progress fallback

- Added non-TTY mode to `RichProgressProcessor` for CI/piped environments where live Rich bars are not appropriate.
- Added milestone-based map progress logging at 10%, 25%, 50%, 75%, and 100%.
- Added explicit mode control via `RichProgressProcessor(force_mode="auto" | "tty" | "non-tty")`.
- Added non-TTY node start/end/failure plain-text logging for non-map runs.
- Cleaned up non-TTY tracking internals after review (removed unused fields and simplified state).
- Added dedicated tests for non-TTY behavior.

### [PR #58](https://github.com/gilad-rubin/hypergraph/pull/58) (Merged February 25, 2026) - PR workflow and contributor docs updates

- Added `.github/PULL_REQUEST_TEMPLATE.md` with a problem + before/after structure.
- Updated internal skills and contributor instructions to reuse the PR template and reduce duplicated guidance.
- Removed legacy "entire session" tracking guidance from project docs/instructions.

## February 2026

### Added

- **`with_entrypoint(*node_names)`** — Graph method that narrows execution to start at specific nodes, skipping upstream. Works for DAGs and cycles. Returns a new Graph (immutable, chainable). Upstream nodes are excluded from the active set and never execute at runtime.
- **Select-aware InputSpec** — `graph.select("a").inputs.required` now shows only what's needed to produce output "a", not the full graph. Previously `select()` only filtered returned outputs.
- **Runtime select overrides removed** — `runner.run(..., select=...)` is no longer supported. Configure output scope on the graph with `graph.select(...)` instead.
- **`entrypoints_config` property** — `graph.entrypoints_config` returns the configured entry point node names, or `None` if all nodes are active.

### Changed

- **InputSpec is now scope-aware** — `graph.inputs` considers both `with_entrypoint()` (forward-reachable) and `select()` (backward-reachable) when determining required or optional parameters, including cycle bootstrap inputs. Parameters from excluded nodes no longer appear in InputSpec; there is no separate `entrypoints` field.

## January 2026

### Added

- **Event system** — `RunStartEvent`, `NodeStartEvent`, `NodeEndEvent`, and other event types emitted during execution. Pass `event_processors=[...]` to `runner.run()` or `runner.map()` to observe execution
- **RichProgressProcessor** — hierarchical Rich progress bars with failed item tracking for `map()` operations
- **InterruptNode** — human-in-the-loop pause/resume support for async workflows
- **RouteNode & @route decorator** — conditional control flow gates with build-time target validation
- **IfElseNode & @ifelse decorator** — binary boolean routing for simple branching
- **Error handling in map()** — `error_handling` parameter for `runner.map()` and `GraphNode.map_over()` with partial result support
- **SyncRunner & AsyncRunner** — full execution runtime with superstep-based scheduling, concurrency support, and global `max_concurrency`
- **GraphNode.map_over()** — run a nested graph over a collection of inputs
- **Type validation** — `strict_types` parameter on `Graph` with a full type compatibility engine supporting generics, `Annotated`, and forward refs
- **select()** method — default output selection for graphs
- **Mutex branch support** — allow same output names in mutually exclusive branches
- **Sole Producer Rule** — prevents self-retriggering in cyclic graphs
- **Capability test matrix** — pairwise combination testing with renaming and binding dimensions
- **Comprehensive documentation** — getting started guide, routing patterns, API reference, philosophy, and how-to guides

### Changed

- **Refactored event context** — pass event context as params instead of mutable executor state
- **Refactored runners** — separated structural and execution layers
- **Refactored routing** — extracted shared validation to common module
- **Refactored graph** — extracted validation and `input_spec` into `graph/` package
- **Renamed `with_select()` to `select()`** for clearer API semantics
- **Renamed `inputs=` to `values=`** parameter in runner API

### Fixed

- **Bound values no longer deep-copied** — nested graphs with bound non-copyable objects (e.g., embedders with thread locks) now work correctly. Bound values are intentionally shared (not copied), matching dependency injection patterns. Non-copyable signature defaults now raise `GraphConfigError` with helpful guidance to use `.bind()` instead
- Preserve partial state from same-superstep nodes on failure
- Support multiple values per edge in graph data model
- Deduplicate `Graph.outputs` for mutex branches
- Reject string `'END'` as target name to avoid confusion with `END` sentinel
- Python keyword validation in node names
- `Literal` type forward ref resolution
- Generic type arity check enforcement
- Renamed input/output translation in `map_over` execution
