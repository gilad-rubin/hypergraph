# Review Checklist

Two checklists: one for coding agents (pre-submit), one for review agents.

## Pre-Submit Checklist (Coding Agents)

Before pushing or creating a PR:

- [ ] **Tests pass**: `uv run pytest`
- [ ] **Lint passes**: `uv run ruff check src/ tests/`
- [ ] **Format correct**: `uv run ruff format --check src/ tests/`
- [ ] **New public API** added to `__init__.py` `__all__`?
- [ ] **Error messages** include "How to fix:" guidance?
- [ ] **Immutability preserved** — `with_*` returns new instance, no in-place mutation?
- [ ] **Internal modules** prefixed with `_`?
- [ ] **Capability matrix updated** if adding new node type or runner feature?
- [ ] **Conventional commit** message with scope? (`feat(graph): add X`)
- [ ] **Type hints** on public API methods?
- [ ] **Sync/async parity** — if you changed a sync runner feature, did you update async too?

## Review Checklist (Review Agents)

When reviewing code changes:

### Architecture
- [ ] Module boundaries respected? (Nodes don't import from graph, graph doesn't import from runners)
- [ ] Internal modules use `_` prefix?
- [ ] Public API changes reflected in `__init__.py` `__all__`?

### Core Beliefs
- [ ] Build-time validation preferred over runtime checks?
- [ ] Immutability preserved? (No in-place mutation of nodes or graphs)
- [ ] Names follow automatic edge inference patterns?
- [ ] Functions remain testable as plain Python (`node.func(x)` works)?
- [ ] No unnecessary state mutation?
- [ ] Composition used instead of configuration flags?
- [ ] Active-set enforcement: does `with_entrypoint` / `select` properly scope both validation AND execution? (Not just one or the other)

### Code Quality
- [ ] Error messages follow three-part structure? (Problem → Context → How to fix)
- [ ] Exception types from the domain hierarchy? (`GraphConfigError`, not bare `ValueError`)
- [ ] Type hints on new public API?
- [ ] Docstrings follow Args → Returns → Raises → Note order?

### Testing
- [ ] Tests cover new behavior (not just happy path)?
- [ ] Validation tests assert specific error messages?
- [ ] Async tests if async code was changed?
- [ ] No new dependencies unless justified?

### Sync/Async Parity
- [ ] Changes to `sync/` mirrored in `async_/`?
- [ ] Changes to `_shared/template_sync.py` mirrored in `_shared/template_async.py`?
- [ ] New executor in `sync/executors/` has counterpart in `async_/executors/`?
