# A sync run must never require an event loop, so the sync template is generated

**Status:** Accepted on 2026-07-17 (issue #243, option C) and confirmed on
2026-09-13 when the generator landed. The measurement that motivated it held
across ~430 commits: 89.2% of `template_sync.py` appears verbatim in
`template_async.py` after de-asyncing. Preconditioned on #242 (the exit ladder
extracted to `runners/_shared/run_teardown.py`), so the copy-pasted teardown
ladders were not frozen into the generator's input.

## Context

`runners/_shared/template_sync.py` and `template_async.py` are the same
~1,400-line run/map lifecycle written twice. Parity was enforced by a
`dev/REVIEW-CHECKLIST.md` item asking a human to read the diff between them.
`CLAUDE.md` states the invariant — "Preserve sync/async behavioral parity when
modifying runners" — but nothing machine-checked it, and matched behavioral test
pairs only cover the cases someone thought to pair.

It kept not being caught. PR #290 repaired four sync/async parity micro-bugs at
once. Ten days after this ticket was filed, commit `ddd95cae` gave `create_run`
an `inputs` parameter and updated only the async half of the Protocol —
`create_run_sync` had been accepting and persisting `inputs` for weeks against a
declaration no implementation matched.

Underneath the duplication sits a constraint that had never been written down
anywhere: **a sync run must never require an event loop.** `SyncRunner` is the
runner a user reaches for from a notebook cell, a script, a test, or a worker
thread inside a framework that owns its own loop. It has to work there with no
loop running, and with a loop already running that it does not own.

## Considered options

- **A. A blocking facade over the async engine** — `SyncRunner.run()` drives
  `AsyncRunnerTemplate` through `asyncio.run` or a portal thread, and the sync
  template disappears. **Rejected**, and it is the constraint above that kills
  it:
  - `asyncio.run` raises inside an already-running loop, so the facade breaks in
    exactly the place `SyncRunner` is most used — a Jupyter cell.
  - A portal thread avoids that but does not carry ContextVars cleanly across
    the boundary, and the runner's stop signal, inspection session, and
    OTel span context are ContextVars.
  - `map()`'s sync semantics are *sequential and fail-fast*: item N+1 never
    starts after item N fails under `error_handling="raise"`. That is
    user-visible behavior, not an implementation detail. A shared concurrent
    engine would change it, or would need a "pretend to be sequential" mode that
    is the duplication again, hidden.
- **B. A sans-io effect-generator core** — one lifecycle written as a generator
  yielding effects, with a sync and an async interpreter. **Rejected as a first
  move**: it adds an interpreter layer to a codebase whose review culture
  deletes pass-throughs, and it turns every runner stack trace into an indirect
  one. Still available later if the marker count grows.
- **C. unasync-style codegen** (the urllib3 / httpcore / psycopg pattern) —
  generate the sync template from the async one and make CI fail on drift.
  **Adopted.**

## Decision

- **`template_sync.py` is generated from `template_async.py`** by
  `scripts/gen_sync.py`. The async template is the source of truth. The
  generated file is committed, carries an `# @generated` header, and is never
  hand-edited.
- **The transform is `async def` -> `def`, `await x` -> `x`,
  `async with`/`async for` -> `with`/`for`, plus an explicit rename table** for
  the names that genuinely differ: the checkpointer's `SyncCheckpointerProtocol`
  half (`get_run_async` -> `get_run`, `create_run` -> `create_run_sync`,
  `get_checkpoint` -> `checkpoint`, …), `AsyncRunTeardown` -> `RunTeardown`, the
  `_async` / `_sync` abstract-hook pairs, and `emit_async` -> `emit`. Rewriting
  runs over `tokenize` output, so nothing inside a string or a comment is ever
  renamed.
- **Three markers carry the differences a rule cannot express**, and every one
  must state a reason — the generator refuses a marker that does not:

  | marker | effect on the generated file |
  |---|---|
  | `# sync:skip: <reason>` | drop this one line |
  | `# sync:skip-start: <reason>` … `# sync:skip-end` | drop this region |
  | `# sync:only-start: <reason>` … `# sync:only-end` | emit this commented block as live sync code |

  A marker is a *claim that the two halves genuinely differ*. Every marker that
  exists today traces back to this ADR's constraint: the concurrent `map`
  fan-out, the backpressured `map_iter` worker pool, the shared
  `asyncio.Semaphore` limiter, and the background checkpoint-error sink are
  marked rather than translated; the sequential fail-fast `map` loop and the
  `SyncCheckpointerProtocol` gate are `sync:only` blocks. Prefer deleting a
  difference over adding a marker.
- **CI's lint job runs `scripts/gen_sync.py --check`** and fails with the
  offending hunk. This replaces the manual `diff` in `dev/REVIEW-CHECKLIST.md`.
- **Scope is the template only.** `runners/sync/runner.py`,
  `sync/superstep.py`, and `sync/executors/*` stay hand-written for now and are
  candidates once the tool is proven. `checkpointers/sqlite.py` stays out
  permanently: its sync half is not a de-async — it uses a `threading.RLock`
  instead of an `asyncio.Lock`, a cached sync connection instead of `self._db`,
  and different rollback helpers. Generating it would mean `sync:skip` regions
  around most of every method body. That surface goes to #244's shared SQL
  builders instead. The two mechanisms are complementary: codegen for "same
  logic, different I/O keyword", shared builders for "same SQL, genuinely
  different transaction machinery."

## Consequences

- Editing the sync template is now a build error; the workflow is "edit
  `template_async.py`, run the generator, commit both."
- The drift class that produced `ddd95cae` and PR #290 is impossible inside the
  templates. It is *not* impossible in `checkpointers/` or `runners/sync/*`,
  which remain hand-maintained.
- `_flush_and_complete` / `_flush_and_fail` are gone from the sync template: the
  async template's inline flush generates identical calls, and generation makes
  keeping a sync-only helper cost a marker it does not earn.
- Asymmetries that used to be invisible are now spelled out with a reason next
  to them. That is the point — and it means an asymmetry nobody intended shows
  up as a marker somebody has to justify.
- The generator pipes its output through the repo's own ruff
  (`check --fix-only --select I,F401`, then `format`), so the generated file is
  byte-stable under the lint job and `--check` can never disagree with
  `ruff format --check .`.
- Adding an `await` to the async template is now a two-file commit. If that
  friction ever outweighs the guarantee, option B is the next move, not
  option A — the constraint at the top of this ADR does not expire.
