# API Atlas brief — map one framework's USER-FACING API against hypergraph's

You are researching ONE framework's user-facing API from its official docs, to
compare against hypergraph's current + designed API (below). The goal is to learn:
what do they expose to users, what do they name it, what feels great, what's
missing from OUR design, what mistakes do they admit.

## The comparison target (hypergraph)

**Tier 0 — in-process, shipped today:**

```python
result = await runner.run(graph, {"doc": doc}, workflow_id="doc-42")
batch  = await runner.map(graph, {"doc": docs}, map_over="doc", max_concurrency=8)
async for item in runner.map_iter(graph, {...}, map_over="doc"): ...   # backpressured streaming
async for ev in runner.iter(graph, {...}): ...                          # live event stream
handle = runner.start_run(graph, {...})    # background; handle: done / stop() / result()
handle = runner.start_map(graph, {...})
# show_progress=True -> rich progress bars (event-driven)
# result.paused -> interrupt gates (HITL); resume = re-run with answers; checkpointer resume
# lineage: fork_from / retry_from create new workflow ids; typed nodes; RunResult/MapResult
```

**Durable tier — designed (ADRs 0005-0008, PRDs 0010-0011 + 11 amendments), unbuilt:**

```python
host = serve(graph.with_runner(AsyncRunner()), home=RunHome.open("file:./runs.db"),
             max_active_runs=12)
receipt = await host.submit("graph", {...}, workflow_id="reingest:2026-07-23",
                            map_over="doc", tolerance=BatchTolerance(count=10))
# submit dedup: start-fingerprint (version+inputs+config); nonterminal same-fp -> use-existing;
# different fp -> workflow_id_conflict; terminal -> already_terminal
await host.answer(wf, pause_id=slot.pause_id, value=True, source_ref="sp:decision:918")
# answer validated against graph-declared answer schema BEFORE settling; pause survives rejection
await host.stop(wf)                        # durable cooperative stop
await host.redrive(source, workflow_id="...-redrive-1", item_keys={"doc-17"})  # new Run, retry_of
async for update in host.watch(wf): ...    # durable replay + live tail; batch: child outcomes
await host.work_forever(worker_id="w1")    # worker loop; restart scan; recovery_exhausted brake
# Batch = immutable manifest of stable item keys + independent child Runs; scheduled answers;
# no host.run() in v1 (local-only evidence honesty); one store (Run Home) = checkpointer + coordination
```

Philosophy: state-based recovery (checkpoint + resume), NOT replay; no determinism
contract; notebook-first Tier 0 stays zero-ceremony; host is additive.

## What to produce

Write a markdown file with EXACTLY these sections (keep code verbatim from docs,
cite doc URLs inline):

1. **Identity** — what the framework is, its execution philosophy (replay/state/queue/
   dataflow), language, maturity. 3 lines max.
2. **Authoring** — how users declare work (decorators, classes, graph builders,
   function signatures). Minimal verbatim example.
3. **Run verbs** — the complete user-facing verb surface: sync run, async, batch/map,
   streaming, background/detached, worker entrypoints. A table: their verb -> what it
   does -> hypergraph equivalent (or MISSING on either side).
4. **Observation** — progress, events, streaming results, state queries, dashboards,
   inspection. How does a user watch a run and a batch?
5. **HITL / pause** — pause/resume/signal/approval APIs, typing, validation.
6. **Failure & retry surface** — what the USER configures (retry policies, timeouts,
   tolerance, error routing) and what operator verbs exist (resume/rerun/replay/
   redrive, bulk ops).
7. **Concurrency & flow control** — concurrency caps, rate limits, keys/fairness,
   debounce/throttle/batching, priorities — and at what SCOPE each applies.
8. **Scheduling & timers** — cron, sleep, wake-at, reminders.
9. **The 3 steals** — the 2-4 most lovable, distinctive API idioms hypergraph should
   consider (name + one-line why + verbatim snippet).
10. **The warnings** — API decisions their own docs/issues/community admit are
    painful (footguns, deprecations, confusing splits).
11. **Verdict vs hypergraph** — one paragraph: what OUR design is missing that THEY
    have at the user-facing API level; what we do better; net recommendation.

Rules: primary sources (official docs) only; fetch the actual pages; note the docs
version/date. Reuse existing workspace research where it exists (read these first):
- [engine comparison](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/engines.md)
- [HITL comparison](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/hitl.md)
- [primitive patterns](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/patterns.md)
- [durable execution landscape](../2026-07-21-durable-execution-landscape.md)
Those cover lifecycle/HITL dimensions; YOUR job is the user-facing API shape, which
they mostly do not cover. Do not repeat their content; reference it.

Focus your token budget on sections 3, 9, 10, 11 — the verb table and the steals are
the deliverable. Be exhaustive on the verb surface; you are the only agent covering
your framework.
