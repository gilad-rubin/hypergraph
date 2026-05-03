# Review Checklist

Three sections: structural sweeps (data-driven), pre-submit checklist, and review checklist.

## Structural Sweeps

Coordination checks derived from analysis of 82 PRs and 493 review bot comments.
Three meta-patterns explained 81.5% of all findings: mirror drift, parity gaps, and
consumer cascades. Run the applicable sweeps before sending code for review.

### Mirror Sweep

**When:** You changed a public signature, default value, enum value, parameter name, or behavior.

**What:** Search for the old value across all mirrors — it's likely still referenced somewhere.

```bash
# Search all code, docs, and examples for the old value
rg -F -- '<old_value>' docs/ README.md examples/ notebooks/ src/hypergraph/
```

**Scope:** docstrings, doc pages, README, examples, notebooks, `__init__.py` exports,
viz/CLI output strings, error messages.

**Note:** files under `examples/` are exercised by `tests/test_examples_*.py` via
`runpy` — they're tests, not just docs. A behavior migration that updates tests
must also update example scripts that hit the same code path.

For public event changes, mirrors specifically include:
- `src/hypergraph/events/__init__.py`
- `src/hypergraph/__init__.py`
- `docs/06-api-reference/events.md`
- examples/notebooks that show `TypedEventProcessor` callbacks

### Parity Check

**When:** You changed a file that has a parallel counterpart.

**What:** Diff or review the counterpart to verify both sides stay in sync.

```bash
# Sync/async runner templates
diff src/hypergraph/runners/_shared/template_sync.py src/hypergraph/runners/_shared/template_async.py

# Check executor counterparts exist
ls src/hypergraph/runners/sync/executors/
ls src/hypergraph/runners/async_/executors/
```

**Parallel surfaces:**
- `template_sync.py` ↔ `template_async.py`
- `sync/executors/` ↔ `async_/executors/`
- Flat graph logic ↔ nested graph logic (test with nested graphs)
- Core runner ↔ DaftRunner (test parity for renames, select, multi-output)

### Consumer Grep

**When:** You added or modified a shared contract: enum variant, Literal member,
dataclass field on a public type, event name, config key, `__all__` export,
CLI subcommand, or viz state key.

**What:** Find all sites that consume existing values, then verify the new value
is handled at each site.

```bash
# Find all consumers of an existing variant (e.g., before adding STOPPED, grep for COMPLETED)
rg -F -- 'COMPLETED' src/ tests/
# Then verify each site also handles the new STOPPED variant

# For new TypedEventProcessor callbacks, grep for an existing handler and mirror the new one
rg -F -- 'on_cache_hit' src/ tests/ docs/
```

### Validation-Runtime Alignment

**When:** You changed validation logic (build-time checks, input validation, type checks).

**What:** Verify that runtime behavior matches the validation contract.

- Add at least one test that exercises the validated path at runtime
- The test should fail if the validation is removed (not vacuously true)
- Check: `rg -n 'def test_.*<validation_name>' tests/` to verify coverage exists

### Frontend Render-Loop Sweep

**When:** You touched `src/hypergraph/viz/assets/viz.js` — especially the App component body, hook dep arrays, or any code path where a meta-derived value can fall back to an empty literal.

**What:** React's `useMemo` / `useEffect` / `useCallback` compare deps with `===` (referential identity). A fresh `{}` / `[]` literal at component-body scope re-allocates every render and, if it ends up in a dep array, triggers an unbounded render loop. See `src/hypergraph/viz/DEBUGGING.md` § Performance & Layout Regressions for a shipped regression (9,833 layouts in 6 seconds before iframe locked up).

```bash
# Top-level App-body fallbacks (the historic offenders)
rg -nE '^\s+var \w+ = .*\|\| (\{\}|\[\])' src/hypergraph/viz/assets/viz.js

# All ref-creating expressions in App body — review whether each one is later captured into a hook dep
rg -nE '^\s+var \w+ = (\w+\.(map|filter|concat)\(|\{[a-zA-Z_]|\[\w)' src/hypergraph/viz/assets/viz.js
```

**Replace** `|| {}` / `|| []` with `|| EMPTY_OBJ` / `|| EMPTY_ARR` (frozen singletons; see `viz.js:52`). **Wrap** `.map()` / `.filter()` / object literals in `useMemo(...)` if their result is captured into another hook's deps. **Hoist** stable JSX-prop objects (like `nodeTypes={...}`) to module scope.

A single unstable dep can produce an exponentially-runaway state machine where every individual operation is fast (~0.5ms) but the loop never terminates — wallclock per-call timing won't catch it. Use `window.__hgAllMarks` instrumentation + Playwright drive-through if you suspect one (recipe in viz/DEBUGGING.md).

---

## Pre-Submit Checklist (Coding Agents)

Before pushing or creating a PR:

- [ ] **All extras installed** so CI-skipped tests run locally: `uv sync --group dev --extra daft && uv run playwright install chromium`. CI fails on `skipped > 0` — see [CONTRIBUTING.md § CI parity](CONTRIBUTING.md#ci-parity--install-all-extras-before-the-gate).
- [ ] **Tests pass (CI-equivalent)**: `uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'` reports `0 skipped` and `0 failed`.
- [ ] **Lint passes**: `uv run ruff check src/ tests/`
- [ ] **Format correct**: `uv run ruff format --check src/ tests/`
- [ ] **New public API** added to `__init__.py` `__all__`?
- [ ] **New public event/callback** mirrored in package-root exports, API docs, and example code?
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
- [ ] Nested graph boundaries preserve the configured public interface? (`with_inputs`/`with_outputs`, scoped outputs, and bound values don't leak across sibling GraphNodes)
- [ ] Checkpointed nested execution can re-run safely? (Repeated GraphNode execution, especially inside cycles, must not collide with child workflow IDs)

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
- [ ] Event contract changes covered in `tests/events/test_types.py` and in any processor-specific consumers (progress/OTel/docs examples)?
- [ ] Nested regressions covered when touching GraphNode/interrupt/checkpointing code? Include renamed outputs, pause/resume, and checkpointed re-execution where relevant.

### Sync/Async Parity
- [ ] Changes to `sync/` mirrored in `async_/`?
- [ ] Changes to `_shared/template_sync.py` mirrored in `_shared/template_async.py`?
- [ ] New executor in `sync/executors/` has counterpart in `async_/executors/`?

### API Design
- [ ] Top-level result properties simple enough for `if result.x:`? (Booleans/enums for control flow, not dataclasses)
- [ ] Detailed metadata pushed to events or step records, not surfaced on the primary result?
- [ ] Framework manages its own internal state? (No dicts the app must maintain/clean up)
- [ ] New naming doesn't shadow existing concepts? (e.g., "interrupt" already means InterruptNode)
- [ ] Invariants enforced with errors, not just documented? (e.g., one active run per workflow)
- [ ] Builds on existing patterns (checkpointer, events, contextvars) rather than inventing parallel systems?
