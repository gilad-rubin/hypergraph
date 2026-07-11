# 0009 — Stable partial-callable identity

status: in-progress

## Fixed acceptance contract

`hash_definition()` falls back to `repr()` for `functools.partial`. When the
wrapped callable is a Python function, that representation embeds its memory
address, so identical `FunctionNode` and `Graph` definitions change across fresh
processes.

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
- Two subprocesses produce the same `FunctionNode.definition_hash` and
  `Graph.code_hash` for one file-defined function wrapped in the same partial.
  Changing an argument or keyword changes both hashes.
- Existing plain functions, bound methods, dynamic functions, and Graph APIs retain
  their behavior. Hypergraph's warning-as-error suite and pre-commit checks pass.

Out of scope:

- no Superposition import, persistence, Git/version lookup, or Graph API change;
- no runner/cache policy change or new dependency;
- no permissive `repr()`/module-name fallback for partials.
