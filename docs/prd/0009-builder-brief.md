# Builder brief — stable partial-callable identity

Implement `0009-stable-partial-callable-identity.md`. Read the root `AGENTS.md`
before editing.

Work red first. The decisive falsifier launches at least two fresh Python
subprocesses and constructs a `FunctionNode` and `Graph` from the same
`functools.partial(file_defined_function, ...)`. It must expose different hashes
before repair and one identical 64-character digest after. Add focused tests that
changing one argument/keyword changes identity and opaque state refuses. The red
commit must also expose a source-defined callable instance whose `FunctionNode`
hash changes across processes; after repair, its `__call__` definition plus
deterministic instance state is stable, changed state differs, and opaque state
refuses.

Expected touch surface is `src/hypergraph/_utils.py`, `tests/test_utils.py`, and
the PRD status. Reuse `hash_definition()` recursively for `partial.func`, the
existing `_canonicalize()` for arguments, and the existing instance-fingerprint
seam for callable-object state. Exact-type check partials; do not add a second
normalizer or change Graph/node/runner APIs.

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
