# Builder brief — stable dynamic-callable identity

Implement the fixed contract in `0008-stable-dynamic-callable-identity.md`. Read
the root `AGENTS.md` before editing.

Work red first. The decisive falsifier launches at least two Python subprocesses
with different `PYTHONHASHSEED` values and hashes the same dynamically compiled
function whose default contains a `frozenset`. It must fail before the repair
because the hashes differ, then pass with one identical 64-character digest. Add
focused in-process tests proving a changed supported default and closure leaf
change identity, while opaque state refuses.

Expected touch surface is `src/hypergraph/_utils.py`, focused utility tests, and
the PRD status. Reuse the existing canonicalizer; remove every arbitrary `repr()`
from the dynamic-code fallback. Do not change graph/node/runner APIs or import
Superposition.

Run:

```text
UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -q tests/test_utils.py
UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'
UV_CACHE_DIR=/private/tmp/uv-cache uv run pre-commit run --all-files
git diff --check
```

Commit red and green separately. Every commit must include:

```text
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

Do not push, merge to master, edit another worktree, or spawn subagents.
