# Builder brief — stable bound-method identity

Implement the fixed contract in `0007-stable-bound-method-identity.md`. Read the
root `AGENTS.md` before editing.

Work red first. The decisive falsifier launches at least two Python subprocesses
with different `PYTHONHASHSEED` values and hashes the same file-defined frozen
dataclass component whose state contains a frozenset. It must fail before the
repair because the hashes differ, then pass with one identical 64-character
digest. Add focused in-process tests for different state and opaque refusal.

Expected touch surface is `src/hypergraph/_utils.py`, focused utility tests, and
the PRD status. Keep one small canonical normalizer in the owning module; do not
change graph/node/runner APIs or import Superposition.

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

Do not push, merge to master, or edit another worktree.
