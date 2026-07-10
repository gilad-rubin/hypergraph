# Builder brief — GraphNode execution identity seam

Implement the fixed contract in
`0006-graph-node-execution-identity-seam.md`. Read the root and nodes
`AGENTS.md` files before editing.

Work red first. Add focused public-API tests proving that `clone=True` versus
`False`, child `identity`/`schema`, and `complete_on_stop=True` versus `False`
are observable without private attributes. Preserve the old three-item
`map_config` tuple exactly.

Expected touch surface is the GraphNode module, the package export surface,
focused GraphNode tests, and the contract status. Use a frozen dataclass (or an
equally typed immutable value), not an arbitrary dictionary. Do not change a
runner, hash implementation, graph behavior, or any Superposition file.

Run:

```text
UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -q <focused tests>
UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'
UV_CACHE_DIR=/private/tmp/uv-cache uv run pre-commit run --all-files
git diff --check
```

Commit red and green separately. Every commit must include:

```text
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

Do not push, merge to master, or touch unrelated worktrees.
