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
