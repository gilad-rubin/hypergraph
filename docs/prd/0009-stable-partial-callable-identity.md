# 0009 — Stable partial-callable identity

status: in-progress

## Fixed acceptance contract

`hash_definition()` falls back to `repr()` for `functools.partial` and callable
instances. Those representations can embed memory addresses, so identical
`FunctionNode` and `Graph` definitions change across fresh processes.

Before:

```text
partial(source_function, mode="clinical")
process A -> graph code hash A
process B -> graph code hash B
```

After:

```text
same exact wrapped callable/args/kwargs -> one hash across processes
changed callable/arg/keyword           -> different hash
unsupported subtype/opaque state       -> clear TypeError
```

Acceptance criteria:

- Exact built-in `functools.partial` is identified structurally from the wrapped
  callable, positional arguments, and keyword arguments; no `repr()` participates.
- The wrapped callable reuses `hash_definition()` recursively. Bound-method state,
  dynamic-code canonicalization, and ordinary source identity therefore remain one
  implementation rather than being copied.
- Arguments and keywords reuse the existing typed canonicalizer. Ordering is
  deterministic; opaque values and cycles refuse loudly.
- Partial subclasses refuse unless their additional typed behavior is modeled;
  this wave may choose exact-type refusal.
- A source-defined callable instance is identified from the executable
  `type(instance).__call__` definition plus deterministic instance state through
  the existing bound-instance fingerprint seam. Opaque callable instances refuse;
  no address-bearing fallback remains.
- Two subprocesses produce the same `FunctionNode.definition_hash` and
  `Graph.code_hash` for one file-defined function wrapped in the same partial.
  The same is true for one source-defined callable instance. Changing a partial
  argument/keyword or callable-instance state changes both hashes.
- Existing plain functions, bound methods, dynamic functions, and Graph APIs retain
  their behavior. Hypergraph's warning-as-error suite and pre-commit checks pass.

Out of scope:

- no Superposition import, persistence, Git/version lookup, or Graph API change;
- no runner/cache policy change or new dependency;
- no permissive `repr()`/module-name fallback for partials.
