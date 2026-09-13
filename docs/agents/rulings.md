# Rulings ledger

Decisions taken by a foreman during implementation waves, so no question is asked twice. Append; never rewrite a ruling, withdraw it with a new line.

## Wave 2026-09-13 (33 refined backlog tickets, PRs #412-#446)

- R1 (#408, #368): graph modifiers that change what runs (selection, bindings at any depth) are identity-bearing for any surface that promises "same identity, same computation" (DefinitionId structural hash, HyperTable recipe fingerprint).
- R2 (#245): notebook HTML reprs keep master's elision policy (keys-only dicts, elided sequences, capped strings); a unified traversal is fine, a policy change to render full nested values is not, because nested values can be large and can carry data the summary deliberately hid.
- R3 (#309): a rejected resume must emit no run-start event and write nothing; gates that decide whether a run may proceed sit before `_emit_run_start`, and a test pins that placement.
- R4 (#246): cross-package consumers of graph helpers import from `hypergraph.graph.addressing`; `_helpers` stays package-private.
- R5 (process): builders never run `git stash` (the stack is shared across worktrees); `uv run --with` does not pin a package the project already has; `uv run --python 3.10 mypy` recreates a worktree's venv without extras.
- R6 (process): a contract's May-edit list must include every call site the Done-when requires (charged to the foreman when it does not: #246, #245).
- R7 (#314 → #248): a heal whose retry fails again still reports HEALED/COMPLETE; receipt truth is #248 F5 and inherits this case.
- R8 (#348): throughput/ETA never include human wait: an item parked on a gate contributes neither its parked stretch nor, after the answer, its think time to the pace window; the pace is machine pace.
- R9 (WITHDRAWN, #277 over #239): the foreman ruled to soften the compaction boundary using provenance; the builder measured that only GraphNode executors consult completion evidence, so softening re-runs folded FunctionNodes (the exact #239 bug). Ruling reversed: the boundary stays; provenance makes the refusal exact and names the folded producers; the issue's acceptance box 5 is amended to the retention='latest' witness. Lesson: a ruling that changes a safety gate needs the measured counterfactual first.
- R10 (process, #248): a clean `git rebase` can merge a caller onto a moved callee with no conflict when the file is mypy-baselined; after rebasing a decomposition leaf, run the full suite AND read every rebased hunk that touches a moved symbol.
- R11 (#407): a fresh rerun records its lineage (retry_of, retry_index) on the runs row, the run-start event and the trace exactly like a default rerun; only checkpoint seeding is skipped.
- R12 (#242): a leaf labelled behavior-preserving matches base on every observable order and value (teardown step order, published durations, release counts); a truer value is a separate leaf, and the order is pinned by a test per exit kind.
- R13 (#248): HyperTable receipts classify by derivation work: SKIPPED = no node executed and no derived row written (restamp-only bookkeeping stays SKIPPED); HEALED = child rows rebuilt and all healthy; UPDATED = rows written but not all healthy or values changed.
- R14 (#408): restore-time errors live in hypergraph/exceptions.py and hypergraph.__all__ (precedent CompactedRetentionError); an identity-bearing change to Definition hashing ships with a public way to compute the pinned hash and an upgrade paragraph for existing Homes.
- R15 (#330): the per-node settlement marker is bookkeeping; a failure writing it never fails the run or discards the completed value (log and continue); a boundary write BEFORE dispatch still fails the run as before.
- R16 (#404): nothing a node calls from an async body may block the event loop with a SQLite write; async-family writes go through the checkpointer's async path as loop tasks awaited before the step record; the sync family (thread) may write directly. Same rule as the read-side fix in 6bf2f05d.
- R17 (#405): an exclusive key is part of what a submission asserts: a rerun carries it, a differing key under an existing workflow_id is a typed conflict (aspect exclusive_key), and any claim that a read is "indexed" is proven by EXPLAIN QUERY PLAN in a test.
