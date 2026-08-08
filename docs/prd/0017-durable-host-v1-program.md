# 0017 — Durable Host V1: local survival, Batch, and safe recovery

status: ready-for-agent

## Problem Statement

A Hypergraph caller can checkpoint execution, but it cannot durably ask for
work and then leave. Submission, live control, duplicate prevention, and
restart ownership still belong to one process. When that process dies,
products such as Panda must invent their own queue rows, restart sweeps,
double-submit guards, and operator views around the runner.

Those product-owned systems cannot give one truthful answer to basic
questions: Was the work accepted? Is a worker entitled to continue it? Which
children completed before a crash? May an external effect run again? Is this
deployment compatible with the persisted Run? Operators also cannot resume a
detached watch without gaps or distinguish waiting, paused, incompatible,
recovery-exhausted, and unknown-effect conditions without reading product
tables.

Hypergraph needs a small durable control layer around its existing runners
and checkpointer. That layer must preserve the current local runner API,
reuse the existing execution journal, and avoid becoming a second workflow
engine, broker, or product server.

## Solution

Add a Definition-bound Host for new Run and Batch submission, backed by a Run
Home that combines the existing checkpointer with adjacent coordination
facts. Add one backend-neutral RunHomeClient for inspecting and controlling
existing work through inert RunRef and BatchRef addresses. Keep runners in
charge of graph execution and StepRecords in charge of execution truth.

The first delivery tier uses SQLite and one exclusive product-owned worker.
It supports durable submit, stop, watch, recovery, rerun, immutable Batches,
failure tolerance, delayed eligibility, and runtime-tunable admission.
Submission pins exact Definition identity and inputs. Recovery has a finite
brake. Nodes that may cause external effects reserve stable effect identity
before calling a provider; ambiguous settlement becomes an unknown outcome
that requires review.

Panda supplies the first real proof. A controlled repeat-safe integration may
precede effect reservation, but Panda must not delete its temporary guards or
claim production-safe restart recovery until pending-node persistence and
effect safety pass real kill tests.

## User Stories

1. As a product developer, I want to submit a Run and receive a durable address, so that my process does not need to stay alive.
2. As a notebook user, I want direct runner execution to remain unchanged, so that durable serving does not become a required layer.
3. As a product developer, I want to bind a Definition to its runner once, so that submission does not repeat execution configuration.
4. As a product developer, I want new submission to require loaded Definition code, so that Hypergraph does not need a remote Definition catalog.
5. As an operator, I want to inspect existing work without importing graph code, so that diagnosis and control can run in a small process.
6. As an operator, I want RunRef and BatchRef values to be serializable and inert, so that an address never pretends to be a live handle.
7. As an operator, I want one client surface for existing Run and Batch work, so that control methods do not appear on several shallow wrappers.
8. As a product developer, I want an accepted submission to persist before execution starts, so that process loss cannot erase durable intent.
9. As a product developer, I want a repeated workflow id with identical meaning to return the existing nonterminal Run, so that webhook retries are safe.
10. As a product developer, I want a repeated workflow id with different meaning to fail loudly, so that accidental id reuse cannot corrupt work.
11. As an operator, I want terminal workflow-id reuse rejected, so that completed history never changes identity.
12. As an operator, I want a failed, stopped, or recovery-exhausted Run to rerun under a new workflow id, so that its prior history remains truthful.
13. As an operator, I want rerun to keep the original Definition identity and inputs, so that it means repetition rather than migration.
14. As an operator, I want version or state migration to use an explicit fork, so that migration and repetition never share lineage.
15. As a release owner, I want every Run to pin a Definition name, deployment version, and structural hash, so that workers claim only compatible work.
16. As a release owner, I want workers to declare exact prior Definition identities they can serve, so that compatible old Runs can drain after deployment.
17. As an operator, I want incompatible work to remain visible and unclaimed, so that a worker never guesses it can resume old state.
18. As an operator, I want a detached watch to replay durable facts and then follow new ones, so that I can reconnect after process or network loss.
19. As an operator, I want a durable cursor to resume without gaps, so that reconnecting does not lose committed changes.
20. As an operator, I want live previews marked as non-durable, so that a preview never advances the durable cursor.
21. As an operator, I want Run views to explain why work waits, so that queued, scheduled, incompatible, paused, and recovery-exhausted work do not look alike.
22. As an operator, I want to query Runs by Definition, status, age, Batch, recovery condition, and lineage, so that ordinary diagnosis does not require SQL.
23. As a product developer, I want a durable stop command to work from another process, so that control does not depend on a live local handle.
24. As a product developer, I want stop and completion races to resolve by committed order, so that the loser receives a truthful result.
25. As a product developer, I want one product-owned worker to start, drain, and release its lock explicitly, so that application shutdown is bounded and restart is predictable.
26. As an operator, I want a second SQLite worker to fail loudly, so that two local workers cannot both own execution.
27. As an operator, I want unfinished repeat-safe work to continue after worker restart without resubmission, so that process death does not orphan it.
28. As an operator, I want repeated recovery without graph progress to stop at a pinned cap, so that a poison Run cannot restart forever.
29. As an operator, I want recovery exhaustion to remain a coordination condition rather than execution status, so that status keeps one meaning.
30. As a batch caller, I want an immutable manifest of stable logical item keys, so that every requested item remains attributable after restart.
31. As a batch caller, I want each Batch item to run as an independent child Run, so that completed children stay complete and failed children can be isolated.
32. As a batch caller, I want manifest acceptance, child identities, pinned inputs, and start intent committed together, so that a partial Batch cannot appear accepted.
33. As a batch caller, I want child outcomes keyed by logical item key, so that completion order never changes result identity.
34. As a batch caller, I want unstarted items represented explicitly, so that a stopped or tolerance-tripped Batch never invents failed results.
35. As a batch caller, I want optional count and percentage failure tolerances pinned in the manifest, so that partial-success policy survives restart.
36. As a batch caller, I want either tolerance to trip only when failures exceed its threshold, so that boundary behavior is predictable.
37. As a batch caller, I want percentage tolerance calculated over all logical manifest items, so that the denominator cannot change during execution.
38. As a batch caller, I want a tripped Batch to stop new child admission while claimed children settle, so that Batch state remains honest.
39. As a batch caller, I want to rerun named source item keys only, so that I can repeat failed items without changing their inputs.
40. As an operator, I want Batch watching to have its own durable sequence, so that child changes can be followed with one gap-free cursor.
41. As an operator, I want to tune the active-Run cap at runtime, so that I can reduce load without restarting workers.
42. As an operator, I want over-limit work to wait in claim order, so that overload delays work instead of rejecting or canceling it.
43. As a product developer, I want provider-resource limits injected separately from Host admission, so that external rate limits and worker capacity keep distinct meanings.
44. As a product developer, I want waiting for a provider permit not to count as failure, so that ordinary throttling does not consume retry policy.
45. As a product developer, I want to submit with a future start time, so that one-shot delayed work needs no external timer service.
46. As an operator, I want future work persisted immediately but ineligible until store time reaches its start time, so that restart cannot lose the schedule.
47. As an operator, I want stopping future work to prevent execution, so that delayed intent remains controllable.
48. As a workflow author, I want pending node boundaries persisted before sibling effects run, so that a crash cannot silently forget unfinished siblings.
49. As a workflow author, I want to declare nodes that may cause external effects, so that Hypergraph can reserve their effect identity before dispatch.
50. As an operator, I want an effect whose settlement was not witnessed to surface as an unknown outcome, so that recovery never spends twice automatically.
51. As an operator, I want unknown effects to require an explicit decision, so that the framework does not call ambiguity failure or success.
52. As a workflow author, I want RetryPolicy to remain node-owned, so that Host recovery cannot become graph-wide retry policy.
53. As a workflow author, I want durable pause slots to store the graph-derived answer contract, so that questions survive process loss.
54. As a product developer, I want every answer to name the observed pause occurrence, so that stale human or timer answers cannot settle a later pause.
55. As a product developer, I want rejected answers to leave the pause open, so that validation failure does not consume the decision.
56. As a product developer, I want scheduled answers to use the same occurrence check as human answers, so that answer-versus-timer races resolve safely.
57. As a product developer, I want non-interrupting reminders to remain product-owned, so that a reminder cannot consume a workflow pause.
58. As an auditor, I want commands to carry an optional source reference, so that a product can join its authenticated action to Host history.
59. As a security owner, I want source references treated only as audit data, so that Hypergraph never mistakes a caller label for authentication.
60. As a Panda operator, I want ingestion and review work to survive a real process kill, so that the product can delete its local queue organs.
61. As a Panda maintainer, I want temporary restart and duplicate guards retained until effect safety passes, so that migration does not remove the only protection too early.
62. As a maintainer, I want an inspectable end-state prototype before implementation, so that public API and operator states can change cheaply.

## Implementation Decisions

- Tier 0 remains direct runner execution. Durable Host use is additive and
  never required for notebooks or in-process background handles.
- A Definition-bound Host owns new Run submission, Batch submission, and
  worker lifecycle. It exposes, but does not duplicate, one RunHomeClient.
- RunHomeClient owns reads and controls for existing work: get, list, watch,
  answer, stop, and rerun. Applicable operations accept RunRef or BatchRef.
- RunRef and BatchRef are immutable serializable addresses. They expose no
  liveness, status, result, or control methods and are never called durable
  handles.
- RunView reports persisted Run facts and named waiting conditions. BatchView
  reports manifest counts, child outcomes, and explicit unstarted items.
- The Run Home is the existing checkpointer plus coordination facts in the
  same transactional store. StepRecords remain the execution journal.
- SQLite permits one exclusive worker owner. Other processes may submit,
  inspect, watch, and append control commands. **Superseded 2026-08-05:**
  SQLite permits SEVERAL workers, arbitrated per submission by a renewed
  lease and fenced by `claim_seq` (ADR 0006 tier-boundary note).
- Worker lifecycle has explicit start, bounded drain, and lease surrender.
  Product process supervision restarts the worker; Hypergraph adds no
  control-plane server.
- Definition identity is the typed tuple of name, deployment version, and
  structural hash. Code hash is recorded for diagnosis but does not decide
  claim eligibility.
- A worker serves exact Definition identities. Compatibility declarations
  name complete prior identities and still require structural compatibility.
- The submission fingerprint covers complete Definition identity, normalized
  inputs, effective Batch configuration, and requested start time. Worker
  identity and submission time are excluded.
- Workflow-id use-existing applies only to fingerprint-identical nonterminal
  work. Terminal reuse and fingerprint mismatch return distinct typed
  conflicts.
- Rerun creates a new Run with a new workflow id, retains source Definition
  identity and inputs, records retry lineage, and accepts no input override.
  A Batch rerun mints a new immutable Batch manifest with a new BatchRef and
  explicit Batch lineage, containing new child Runs only for the selected
  source item keys; it never mutates the source Batch.
- Fork performs explicit compatibility-checked migration, records fork
  lineage and a reason, and never shares the rerun operation.
- Host recovery does not reset node Retry budgets or Retry windows. Repeated
  recovery without committed progress reaches a pinned recovery-exhausted
  condition and stops automatic adoption.
- Committed StepRecord, durable pause, or terminal transition counts as
  recovery progress. Recovery exhaustion is not a WorkflowStatus.
- Every Run mutation receives a monotonic per-Run durable sequence in its
  transaction. Every Batch manifest or child-outcome change receives a
  monotonic per-Batch sequence; child and Batch facts commit together.
- Watch resumes from the addressed Run or Batch cursor without gaps. Live
  preview may accompany durable facts but is marked non-durable and never
  advances the cursor.
- A durable Batch is an immutable manifest of unique stable logical item keys
  and independent child Runs, not a persisted MapResult.
- Batch acceptance atomically persists its manifest, child identities,
  pinned inputs, and accepted start command.
- Count and percentage failure tolerances are optional manifest values.
  Either trips when failure-equivalent children strictly exceed it.
  Percentage uses total manifest item count.
- Failed and recovery-exhausted children are failure-equivalent. Paused,
  queued, delayed, admission-limited, and unstarted items are not.
- A tolerance trip closes new child admission, lets claimed children settle,
  marks all remaining items unstarted, and leaves the Batch partial.
- Host admission and provider-resource admission are separate. The Run Home
  owns a runtime-tunable active-Run cap; over-limit work waits in claim
  order, and a claimed Run waiting on a provider permit still consumes its
  Host slot. Provider-resource limits are injected at graph, node, or
  component scope with honest scope names (process-local versus
  distributed); for an underlying provider quota the shared component is
  often the preferred owner — several graphs and nodes reuse it, the
  component owns admission, and it acquires at the exact scarce call, while
  graph- and node-level limits compose as narrower work budgets. A
  provider-permit wait is neither a failure nor a retry attempt.
- Admission v1 waits in claim order. Reject, cancel-oldest, cancel-newest,
  expression-language keys, and keyed fairness are excluded.
- Future start time is persisted and fingerprinted at acceptance. Store time
  controls claim eligibility; a past time is immediately eligible.
- Every node that may cause an external effect declares that fact. Stable
  effect identity is reserved durably before provider dispatch.
- A started effect whose settlement is not witnessed becomes unknown outcome.
  Hypergraph never spends it again automatically.
- Durable pause slots persist the graph-derived answer schema and occurrence
  options. Human and scheduled answers name the current pause id, validate
  one typed value, and settle atomically.
- Scheduled answers share a due-row scanner with delayed starts but remain
  pause-scoped commands. Recurring schedules and general scheduled commands
  are excluded.
- Non-interrupting reminders, assignment, claim clocks, escalation, and
  consensus remain product or Superposition concerns.
- Commands may store an opaque source reference for audit. Authentication and
  deduplication never depend on it.
- Panda integration has two gates. A controlled repeat-safe proof may land
  after the local Host slice. Full adoption, guard deletion, and release
  claims require pending-node and effect-safety kill tests.
- Before implementation, an inspectable prototype must show the public
  interface and the states produced by kill/restart, cursor continuation,
  completed-child preservation, tolerance trip, version refusal, unknown
  effect, rerun, and fork.

## Testing Decisions

- The main test seam is the public Definition-bound Host plus RunHomeClient
  against a real SQLite Run Home. Tests assert observable receipts, views,
  updates, commands, results, and persisted recovery behavior.
- Child-process kill tests use a real process boundary and reopen the same
  Run Home. Mocked exceptions do not count as restart proof.
- The prototype precedes implementation and provides the expected
  before/after state examples that acceptance tests later encode.
- Direct runner tests prove Tier 0 behavior and process-local Execution
  handles remain unchanged.
- Submission tests cover use-existing, terminal conflict, fingerprint
  conflict, pinned Definition identity, future start, and stop-before-due.
- Worker tests cover exclusive startup, bounded drain, lock release, restart
  scan, version refusal, and finite recovery exhaustion.
- Watch tests disconnect and reconnect from stored cursors. They assert no
  missing or repeated durable sequence and prove previews do not advance the
  cursor.
- Batch tests use stable item keys and non-completion-order execution. They
  assert atomic manifest visibility, keyed outcomes, explicit unstarted
  items, partial status, and child completion preservation after restart.
- Tolerance tests cover exact threshold, threshold plus one, count and
  percentage together, the fixed denominator, paused children, claimed
  children settling, and subset rerun lineage.
- Admission tests change the cap while work is queued and prove provider
  permit waits do not change failure state.
- Version tests serve exact and explicitly accepted prior identities, refuse
  incompatible work, and distinguish rerun from migration fork.
- Pause tests use repeated loop pauses, stale and double answers, schema
  rejection, answer-versus-timer races, and nested graph port projection.
- Pending-node tests kill between sibling boundaries and prove unfinished
  siblings remain visible after restart.
- Effect tests kill before dispatch, during provider execution, and after the
  provider returns but before settlement. Only the ambiguous case becomes
  unknown outcome, and it never dispatches again automatically.
- Backend conformance tests support the public seam for Memory, SQLite, and
  later Postgres, but do not replace end-to-end Host behavior tests.
- Sync and async runner paths must present the same durable semantics.
- Panda proof runs real ingestion and review flows, including an actual
  kill-and-restart, before temporary guards or BackgroundTasks are removed.
- Each implementation ticket runs focused tests first and the repository's
  warning-as-error CI-equivalent suite before completion.

## Out of Scope

- Reconnectable token or log output. A future OutputLog needs a separate
  product requirement and contract.
- Submission from a process without loaded Definition code or a durable
  Definition catalog.
- A Hypergraph HTTP server, dashboard, deployment service, or worker
  supervisor.
- A broker, stream, second journal, event-sourced execution, or deterministic
  replay contract.
- Full event replay or reconstruction of process-local RunResult evidence.
- Cron, recurring schedules, or a generic scheduled-command framework.
- Bulk mutation by query, expression-language admission keys, adaptive
  fleet-wide flow control, and overflow cancellation strategies.
- Shared Postgres execution until the local tier and Panda proof establish a
  real multi-worker need.
- Non-interrupting reminders and human-task assignment or consensus.
- Hosting Daft or another runner that cannot provide checkpoint and event
  capabilities. Such runners remain available through direct execution.
- Tier-0 calculator, projection-stream, liveness-timeout, cross-map-cap,
  executor-delegation, and generated CLI work; those remain a separate
  ergonomics track.

## Further Notes

- This spec incorporates amendments A1–A16 from the four-round durable-host
  review package. That research remains provenance; this document is the
  agent-ready delivery contract.
- The approved leans are: accept the durable-host ADR direction; use complete
  Definition identity; build immutable durable Batch; defer OutputLog; keep
  new submission Definition-bound; use public rerun rather than redrive.
- The local Host wedge intentionally precedes durable pause slots. Answer and
  scheduled-answer verbs remain unavailable until the pause-slot slice lands.
- The decision-grade prototype is a hard gate. Code tickets remain blocked
  until the maintainer confirms that its end state matches this spec.
- Postgres shared execution remains a later tier with transactional
  lease-epoch fencing and a real multi-worker kill matrix.
