# 0008 — Stable dynamic-callable identity

status: in-progress

## Fixed acceptance contract

`hash_definition()` canonicalizes bound-instance state, but its bytecode fallback
still serializes code constants, defaults, keyword defaults, and closure cells with
`repr()`. A dynamically compiled function whose default contains a `frozenset`
therefore receives different hashes under different `PYTHONHASHSEED` values.

Before:

```text
same dynamic function + same typed default
PYTHONHASHSEED=1 -> graph code hash A
PYTHONHASHSEED=2 -> graph code hash B
```

After:

```text
same exact code/defaults/closure -> one hash across processes
different supported state       -> different hash
unsupported opaque state        -> clear TypeError requesting deterministic state
```

Acceptance criteria:

- The bytecode fallback uses one canonical, type-preserving payload for constants,
  defaults, keyword defaults, and non-empty closure cells; no arbitrary `repr()`
  participates in their identity.
- Nested code constants are represented deterministically without source filenames or
  memory addresses. Checkout path and first-line location remain non-identity facts.
- The canonical value vocabulary and refusal behavior reuse the existing
  `_canonicalize()` seam; do not create a second normalizer.
- Two subprocesses with different `PYTHONHASHSEED` values hash the same dynamically
  compiled function with a `frozenset` default identically. Changing one default or
  closure state leaf changes the hash.
- Opaque defaults/closures and cycles fail clearly instead of degrading to `repr()`.
- Source-defined functions, bound-method state, plain builtins, and the public Graph
  API retain their existing behavior.
- Hypergraph's warning-as-error suite and pre-commit checks remain green.

Out of scope:

- no Superposition import, persistence, Git/version lookup, or Graph API change;
- no runner/cache policy change or new dependency;
- no module/qualname-only or `default=str` fallback.
