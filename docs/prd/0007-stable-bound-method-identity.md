# 0007 — Stable bound-method identity

status: in-progress

## Fixed acceptance contract

`hash_definition()` currently describes its bound-instance fingerprint as
deterministic but serializes `cache_key()` and `vars(instance)` with `repr()`.
Container repr order changes with `PYTHONHASHSEED`, and opaque nested objects can
leak memory addresses into `FunctionNode.definition_hash` and `Graph.code_hash`.

Before:

```text
same frozen component + same frozenset state
PYTHONHASHSEED=1 -> graph code hash A
PYTHONHASHSEED=2 -> graph code hash B
```

After:

```text
same exact typed state -> one hash across processes
different state         -> different hash
unsupported opaque state -> clear TypeError requesting cache_key()
```

Acceptance criteria:

- Bound-method instance fingerprints use one canonical, type-preserving JSON
  payload; no arbitrary `repr()` participates in identity.
- Supported values are exact JSON primitives, bytes, Enum values, mappings with
  deterministic keys, lists, tuples, sets/frozensets, dataclasses, Pydantic-like
  `model_dump()` values, and explicit `cache_key()` results.
- Exact built-in containers/scalars are distinct from subclasses. A subclass
  must expose deterministic typed state or refuse.
- Nested values follow the same rule recursively. Cycles and opaque values fail
  clearly rather than losing state or using an address.
- Two subprocesses with different `PYTHONHASHSEED` values hash the same frozen
  dataclass/frozenset component identically. Changing one state leaf changes the
  hash.
- Existing same-state/different-state/cache-key bound-method behavior remains.
- Plain functions and slotted instances with no accessible state retain their
  existing code-only behavior.
- Hypergraph's warning-as-error suite and pre-commit checks remain green.

Out of scope:

- no Superposition import, persistence, Git/version lookup, or Graph API change;
- no change to runner/cache policy or the meaning of `Graph.code_hash`;
- no permissive module/qualname-only or `default=str` fallback.

