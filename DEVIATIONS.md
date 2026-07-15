# T28 contract reading: run-resume versus row-converge

1. A checkpointer owns an execution; HyperTable owns a domain row that can be reconciled repeatedly.
2. The two systems share outcome vocabulary (`paused`, `pause`, `response_key`), not execution mechanics.
3. Checkpointer resume re-enters a held run; a table update starts a fresh derivation against stored row facts.
4. A completed workflow is closed, while a complete row remains eligible for future re-derivation.
5. Checkpointer state preserves the old run; HyperTable converges under the current graph, sources, and configuration.
6. Therefore changed upstream input may invalidate an answer and legitimately re-ask with new provenance.
7. Provenance-clean columns are cache hits; only stale or downstream columns should run during convergence.
8. Cycles and shared-state accumulation remain checkpointer concerns and are not table features.
9. An interrupt is a human-executed node whose answer is a derived column supplied from outside the engine.
10. A paused derivation is a `waiting` row carrying one typed question envelope until an answer update drives convergence.

# Deviations and operational facts

- Setup command-location correction: `git clone . .worktrees/t28-hypertable` succeeded, but the chained `git checkout` remained in the held parent checkout and was denied at `.git/index.lock`; the checkout was rerun inside the independent clone. No main-tree file changed.
- Environment accommodation explicitly allowed by the brief: the global uv cache failed with `Operation not permitted`, so all uv commands use `UV_CACHE_DIR="$PWD/.uv-cache"`.
- Baseline limitation: `uv run pytest` reached 3252 passing tests but the managed sandbox denied Chromium Mach-port registration (`bootstrap_check_in ... Permission denied (1100)`), cascading to 35 failures, 195 errors, and 15 skips. Chromium was already installed; `playwright install` was not run.
- Granted untouched-list deviation (foreman, 2026-07-15): make the smallest `_provenance.py` change needed to distinguish a newly supplied answer from a stale stored answer. Without that distinction, an upstream change would incorrectly reuse the old answer instead of re-asking.
- Granted implementation choice (foreman, 2026-07-15): implement parent-column predicates on `child(name).rows(where=...)` as a handle-side parent/child join. The physical child link remains `_parent_id`; public child rows expose the named parent identity.
- Routed cold-boot implementation fact: a one-node reconcile graph cannot isolate a gate because its declared targets are absent. Answer-only updates therefore build the smallest valid graph slice from the answered interrupt through its descendants and feed it stored upstream values. This preserves routed execution and the no-upstream-rerun contract without touching graph or runner internals. Gate routing outputs remain physically unchanged and are filtered from public rows by their producing-node role.
- Adversarial-review repair: routed union columns also require their controlling gate when source updates or `rederive()` converge a row; an isolated gate crashes because its declared targets are absent, while isolated branch nodes ignore routing. The write planner now executes the gate's downstream slice, uses the ordinary `RunResult.log` to identify the branch nodes that actually ran, and stamps only that route while retaining provenance-clean upstream values. This stayed inside `materialization/_writes.py`; no additional `_provenance.py`, graph, runner, or on-disk change was needed.
- Foreman acceptance repair (2026-07-15): the required `Graph([...]).as_table()` migration exposed a graph-build conflict that the deleted `HyperTable([nodes])` constructor used to avoid by extracting child grains before constructing the root graph. Two `map_over(..., identity=...)` children may legally emit the same inner column because each persists to its own child table, but graph conflict validation treated those outputs as competing root values. The narrow fix touches the otherwise read-only `graph/_conflict.py`: only nodes whose public `map_execution_config.identity` is set are grain-isolated during pairwise output-conflict checks; ordinary graph producers and ordinary `map_over()` nodes retain the existing conflict rules.
- Scope-fence review finding: `notebooks/hypertable-showcase.ipynb` still records the legacy table surface. Notebooks are outside the authorized file list, while the ticket's deletion sweep explicitly scopes living docs to `docs/08-hypertable/`, the HITL page, and README. It was not modified.
