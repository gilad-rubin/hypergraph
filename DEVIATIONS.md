# T27 Deviations Log

## Granted guesses and environment deviations

- The sandbox permits source writes under the repository but denies writes to
  the main checkout's `.git` directory. `git worktree add` failed with
  `cannot lock ref 'refs/heads/t27-interrupt-answer-name' ... Operation not
  permitted`. To preserve an isolated checkout, branch, and local commit
  history, this ticket uses a local clone at the requested worktree path,
  pinned to `eb74e48f`, with branch `t27-interrupt-answer-name`. No source
  edits were made in the held main tree.
- The default uv cache is not sandbox-writable. The initial `uv sync --group
  dev` failed with `Failed to initialize cache at
  /Users/giladrubin/.cache/uv`; all subsequent uv commands use
  `UV_CACHE_DIR="$PWD/.uv-cache"` as authorized by the brief.
- Chromium is already installed, so `playwright install` was intentionally
  skipped. The pristine baseline browser launch failed with
  `MachPortRendezvousServer ... Permission denied (1100)` and produced `35
  failed, 3328 passed, 15 skipped, 195 errors`. This is recorded rather than
  worked around, as directed by the sandbox note. The final unfiltered
  all-extras run later launched Chromium successfully and passed all 3587
  tests.
- After installing the required `daft` extra, its stateful worker-resource test
  failed because the sandbox denies POSIX shared-memory creation:
  `PermissionError: [Errno 1] Operation not permitted: '/psm_...'` from
  `multiprocessing.shared_memory.SharedMemory`. The focused framework-port and
  ordinary Daft tests still run; the single shared-memory test is deselected
  only in the supplemental sandbox-green suite. It remained included and
  passed in the final unfiltered CI-parity run.
- The repository has no configured Python type-check command and the attempted
  focused `uv run pyright ...` could not spawn (`No such file or directory`).
  Type-facing behavior is covered by strict-types construction tests; lint,
  formatting, focused tests, and CI-parity pytest remain the available gates.

## Contract implementation choices

- Chose duck-typed structural introspection rather than a runtime Protocol.
  `InterruptNode.ask_annotation` resolves the handler return annotation and
  `InterruptNode.output_annotation` / `get_output_type()` expose its
  class-level `answer_type` object unchanged, including typing aliases.
- Introduced the private `_validate_answer_name` helper and the public read-only
  `InterruptNode.answer_name` property. `answer_name` is genuinely required and
  keyword-only in both public signatures; Python's missing-argument error names
  it for legacy `output_name`-only calls, while tuple values and unsupported
  keyword combinations get contract-teaching `TypeError`s. Unsupported keywords
  are rejected, never interpreted.
- Introduced `_validate_interrupt_return_annotations` in graph validation. It
  checks only structural presence of class-level `answer_type`; it does not
  require `answer_type` to be an instance of `type`, preserving typing aliases.
- Introduced `_validate_ask_payload` in the async interrupt executor. It checks
  the frozen runtime seam directly (`prompt: str`, `options: tuple[str, ...] |
  None`, `evidence: tuple`) before a pause can surface.
- Introduced `_validate_pause_options_have_routes` at the async runner's pause
  boundary. The executor intentionally does not carry a Graph reference; the
  pause boundary sees the correct graph-scope `response_key` and therefore also
  re-checks after each nested GraphNode projection. It is restricted to active
  gates and evaluates each consuming gate as a pure function with each runtime
  option, so answer tokens such as `replace-existing` may map to valid Python
  node names such as `replace_existing`. This evaluation is necessary because
  neither the decorator nor the return annotation duplicates runtime options.
- Kept runtime question/route contract failures as `RuntimeError`, matching the
  existing interrupt executor's handler-failure surface. The exception module
  has no interrupt-contract domain type; introducing and exporting one would
  widen the public API beyond T27.
- `PauseInfo` now has only `node_name`, `value`, and `response_key`; deleted
  `output_param`, `output_params`, `values`, and `response_keys` rather than
  retaining aliases.
- Documentation uses the ticket's companion-vocabulary choice: it states that
  concrete Ask classes ship separately and includes a minimal inline frozen
  `Confirm` dataclass so the structural seam and primary examples remain
  self-contained. No concrete question vocabulary was added under `src/`.

## Ripple files outside the primary scope list

- `src/hypergraph/runners/async_/executors/graph_node.py`: project the single
  `response_key` through a nested GraphNode boundary.
- `src/hypergraph/runners/_shared/template_async.py`: emit interrupt events
  from `PauseInfo.response_key`.
- `src/hypergraph/runners/_shared/caching.py`: bypass node-result cache lookup
  and storage for interrupts so a previously supplied answer can never
  auto-resolve a later unanswered run.
- `src/hypergraph/runners/_shared/template_sync.py`: mechanical shared-template
  field migration only; no sync interrupt support was added.
- `src/hypergraph/runners/_shared/AGENTS.md`: replaced the canonical local
  three-path auto-resolve/None-pause guidance with the v4 answer-supplied,
  question-pause, and loud-invalid-question lifecycle.
- `docs/03-patterns/08-caching.md`: removed the false claim that interrupt
  auto-responses can be replayed from cache; question pauses produce no
  cacheable dataflow output.
- `examples/framework_ports/support_inbox.py`: a collected example used the
  deleted conditional auto-resolve contract. Added the test/example-only
  `DeveloperReplyQuestion`, `developer_review_policy`, and
  `skip_developer_review`; critical tickets route to the interrupt while
  noncritical tickets route to the automatic reply producer.
- `examples/chat_app.py`: introduced test/example-only `UserMessageQuestion`
  and moved the conversation seed into question evidence.
- `examples/slack_interrupt_auto_resume_demo.py`: introduced test/example-only
  `SlackQuestion`; the auto-resume helper now carries cycle state from the
  typed question's evidence rather than treating `PauseInfo.value` as raw
  node input.
- `notebooks/04_cycles.ipynb`, `notebooks/checkpoint_decision_matrix.ipynb`,
  `notebooks/checkpointer-explorer-showcase.ipynb`,
  `notebooks/interrupt-loop-debug.ipynb`, and
  `notebooks/checkpoint_lineage_walkthrough.ipynb`: mirror-sweep migrations to
  `answer_name` with minimal local structural question classes. Notebook JSON
  was validated with `jq empty`; cells were not re-executed because this ticket
  does not change their unrelated saved outputs.

## Static options-check proposal

- A supplementary build check is possible when `annotation.answer_type` is a
  `Literal[...]`: compare its literal string values with consuming gate targets
  during graph validation. This would catch dead *possible answers*, but it
  cannot replace the runtime payload check because ordinary `Choice` declares
  `answer_type = str` and its concrete options exist only on the returned
  instance. Not implemented in T27 to avoid silently widening the frozen seam.

## Ratified options-check resolution

- The foreman ratified narrow option 3: pause-time validation still checks
  every consuming gate whose inputs can be resolved from the answer and the
  current partial state. When another gate input is not yet settled, that gate
  is skipped at the pause boundary so the question surfaces; its normal
  routing-time target validation remains loud for an unmatched answer. This
  intentionally adds no scheduler machinery and no public API.
