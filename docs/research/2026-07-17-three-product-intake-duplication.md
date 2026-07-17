# Three-product intake duplication audit

**Date:** 2026-07-17  
**Question:** What should be extracted as shared intake/control-plane machinery from Panda, Subtext v2, and Literature Agent—and what should remain product-local?

## Verdict

Do **not** extract one shared “ingestion lifecycle” now. The apparent overlap splits into:

1. **A small, genuinely repeated kernel:** a durable item identity, source identity/revision facts, per-item isolation, a derived current status, and operation/run grouping. This is a vocabulary and projection pattern, not yet a common implementation. Bounded retry appears in Subtext and Literature Agent, but not Panda; Literature alone has the full attempt/lease/ambiguous-outcome machinery.
2. **Two materially different domains:** Panda is an operator-gated, versioned KB publish workflow; Subtext is a scheduled source-observation/replacement worker. Their `needs_review` states do not mean the same thing.
3. **One product-specific control plane:** Literature Agent’s per-file attempt history, dispatch ambiguity, leases/fencing, explicit retry acknowledgement, and reviewer receipt exist there because paid, non-idempotent provider dispatch makes them necessary. Neither other product implements the prerequisite semantics.

The first safe shared boundary is therefore a **typed observation/projection contract** (`IntakeItem`, `IntakeOutcome`, `AttemptSummary`, `ReviewRequired`) with each product retaining its own durable store and transition rules. Do not share a queue, retry worker, or HITL state machine until a second consumer has the same side-effect and recovery invariants as Literature.

## Evidence matrix

| Concern | Panda KB | Subtext v2 source media | Literature Agent | Classification |
| --- | --- | --- | --- | --- |
| Durable item | `WorkItem` persists candidate/version, status, clashes, gates, and a string `batch_id`; KB catalog reloads it and reconciles projection ([knowledge_base.py:86-108](../../../panda/src/panda/kb/knowledge_base.py#L86-L108), [187-214](../../../panda/src/panda/kb/knowledge_base.py#L187-L214)). | One BigQuery status row per `raw_uri`, holding observed source identity, broadcast mapping, attempt/run/error fields ([bigquery_resources.py:350-383](../../../subtext_v2/src/subtext/ingest/source_media/bigquery_resources.py#L350-L383)). | `UploadItem` is a per-physical-file durable record with staged object, expected/observed digest, stage, execution state, diagnostic and linked job ([resilient_ingestion.sql:75-125](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L75-L125)). | **Genuinely analogous.** Extract only the conceptual minimum; schemas and identities differ. |
| Batch / run / window | `batch_id` is only a field on a work item; no durable batch authority or batch completion state ([knowledge_base.py:86-100](../../../panda/src/panda/kb/knowledge_base.py#L86-L100)). | A durable worker-run row records the observed date window, counts, manifest, worker version and error ([bigquery_resources.py:386-412](../../../subtext_v2/src/subtext/ingest/source_media/bigquery_resources.py#L386-L412)). | `UploadBatch` groups a user action only; it is explicitly not a shared scheduling or failure boundary, while each item/job continues independently ([CONTEXT.md:51-56](../../../literature_agent/dev/CONTEXT.md#L51-L56)). | **Analogous name, different boundary.** Do not turn `batch` into a shared job abstraction. |
| Partial metadata / issues | Missing station is a publish blocker and pauses for metadata confirmation; it does not create an independent issue record ([ingestion_lifecycle.py:179-219](../../../panda/src/panda/kb/ingestion_lifecycle.py#L179-L219)). | Bad/multiple source-to-broadcast mappings become `needs_review`; the reason is a source-catalog conflict, not editable document metadata ([status.py:154-171](../../../subtext_v2/src/subtext/ingest/source_media/status.py#L154-L171)). | Valid files can progress independently of DOI; `ImportIssue` is a durable post-registration operational/identity issue, while validation/skip outcomes stay on the batch ([CONTEXT.md:67-80](../../../literature_agent/dev/CONTEXT.md#L67-L80)). | **Semantically different.** A common `issue` type would erase whether the thing is a publish gate, source ambiguity, or accepted-record defect. |
| Duplicate and HITL | Duplicate clashes interrupt before derivation, with structured choices to keep, replace, or archive ([ingestion_lifecycle.py:89-153](../../../panda/src/panda/kb/ingestion_lifecycle.py#L89-L153)); the selected decision is persisted only as the current decision dict ([knowledge_base.py:903-933](../../../panda/src/panda/kb/knowledge_base.py#L903-L933)). | Two raw paths mapping to one broadcast are held for review, but there is no persisted human decision or choice protocol ([status.py:154-171](../../../subtext_v2/src/subtext/ingest/source_media/status.py#L154-L171)). | Byte-identical existing project files are a successful deterministic skip—no parse/extract starts; different-byte DOI conflict is a different identity problem ([CONTEXT.md:59-64](../../../literature_agent/dev/CONTEXT.md#L59-L64)). | **Panda-only HITL workflow.** Subtext has a review flag; Literature has deterministic dedupe plus identity issues. |
| Retry classification, backoff, caps | A fresh graph run has a 900-second cap, but any exception projects to `failed`; there is no retry class, schedule, or attempt budget ([knowledge_base.py:111-150](../../../panda/src/panda/kb/knowledge_base.py#L111-L150), [736-758](../../../panda/src/panda/kb/knowledge_base.py#L736-L758)). | Generic helper retries *all* exceptions three times with exponential delay; item processing then increments toward `needs_review` at its cap ([retries.py:12-37](../../../subtext_v2/src/subtext/ingest/source_media/retries.py#L12-L37), [worker.py:403-456](../../../subtext_v2/src/subtext/ingest/source_media/worker.py#L403-L456)). | Job claim checks `max_attempts`; settlement has bounded exponential backoff and distinguishes retryable from terminal results ([resilient_ingestion.sql:814-873](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L814-L873), [964-1057](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L964-L1057)). | **Shared shape, not shared policy.** Literature is the only candidate for reusable policy code, but only for equivalent remote side effects. |
| Provider idempotency / unknown outcome | Fresh rerun safety comes from KB idempotence and page memoization, not request/dispatch identity ([0041-ingestion-lifecycle-is-a-facade-driven-graph-without-a-checkpointer.md:13-38](../../../panda/docs/adr/0041-ingestion-lifecycle-is-a-facade-driven-graph-without-a-checkpointer.md#L13-L38)). | The worker retries generic exceptions; no dispatch state, request key, or unknown-provider-outcome state is present ([retries.py:12-37](../../../subtext_v2/src/subtext/ingest/source_media/retries.py#L12-L37)). | Jobs have idempotency keys, request fingerprints and dispatch state ([resilient_ingestion.sql:172-205](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L172-L205)); only confirmed transient OpenAI HTTP statuses replay, while a post-dispatch disconnect is deliberately ambiguous ([supabase-ingestion.ts:1697-1752](../../../literature_agent/lib/supabase-ingestion.ts#L1697-L1752)). | **Literature-only.** This is a response to billable/non-idempotent dispatch, not generic ingestion. |
| Liveness, leases, reaper | Timeout only; no lease, heartbeat, or reaper is evidenced in the lifecycle implementation ([knowledge_base.py:127-150](../../../panda/src/panda/kb/knowledge_base.py#L127-L150)). | Run row and current status exist, but no claim lease/heartbeat/reaper; concurrency is process-local semaphore ([worker.py:126-151](../../../subtext_v2/src/subtext/ingest/source_media/worker.py#L126-L151)). | Claim creates a fenced lease and attempt; leases renew through heartbeat ([resilient_ingestion.sql:814-897](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L814-L897)). The reaper safely requeues pre-dispatch expiry but marks post-dispatch expiry `provider_outcome_unknown` ([resilient_ingestion.sql:1156-1213](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L1156-L1213)). | **Literature-only.** Do not extract until another product needs multi-worker recovery. |
| Status projection / reconciliation | Explicitly a projection: graph pause/result is projected onto `WorkItem`; load heals stale `indexed` status from manifest truth ([knowledge_base.py:762-820](../../../panda/src/panda/kb/knowledge_base.py#L762-L820)). | Status merge projects repeated observations into the current row and preserves completed unless source identity changes ([status.py:214-262](../../../subtext_v2/src/subtext/ingest/source_media/status.py#L214-L262)). | `OperationReceipt` is explicitly a versioned read model over batch/item/job/attempt authorities, not a second source of truth ([CONTEXT.md:63-64](../../../literature_agent/dev/CONTEXT.md#L63-L64)). | **Genuine shared architectural pattern.** Share terminology/tests later, not the persistence layer. |
| Source revision detection | Versions and SHA are created on explicit `queue`; lifecycle does not poll or detect an external revision ([knowledge_base.py:609-702](../../../panda/src/panda/kb/knowledge_base.py#L609-L702)). | Re-observation compares size/CRC/MD5 for a completed URI and requires review before reprocessing on change ([status.py:214-260](../../../subtext_v2/src/subtext/ingest/source_media/status.py#L214-L260)). | Upload integrity binds expected/stored SHA for the selected immutable object; it is not an external source watcher ([resilient_ingestion.sql:75-96](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L75-L96)). | **Subtext-only.** Its source is an evolving catalog, unlike selected uploads. |
| Audit / attribution | Persisted current decision and catalog timestamps, but no separate decision/attempt history or actor attribution in this lifecycle record ([knowledge_base.py:86-103](../../../panda/src/panda/kb/knowledge_base.py#L86-L103), [903-933](../../../panda/src/panda/kb/knowledge_base.py#L903-L933)). | Operational trace is run id, attempts, error, and source fields—not an actor decision ledger ([bigquery_resources.py:350-412](../../../subtext_v2/src/subtext/ingest/source_media/bigquery_resources.py#L350-L412)). | Upload and processing attempt history is durable, and paper operations explicitly record human/agent/system actor type ([attempt_history_receipts.sql:6-29](../../../literature_agent/supabase/migrations/20260715212000_attempt_history_receipts.sql#L6-L29), [resilient_ingestion.sql:251-279](../../../literature_agent/supabase/migrations/20260715190000_resilient_ingestion.sql#L251-L279)). | **Literature-only full audit.** Panda may eventually need a narrower operator-decision audit, but that is not the same feature. |

## Extraction boundary

**Keep local**

- Panda’s `DuplicateDecision` / metadata interrupt topology and version/publish semantics.
- Subtext’s source-slot discovery, canonical-broadcast replacement, and source-metadata revision detector.
- Literature’s lease-fenced worker, provider dispatch ledger, ambiguous-outcome acknowledgement, and per-attempt audit.

**Standardize only when useful across products**

```python
@dataclass(frozen=True)
class IntakeObservation:
    item_id: str
    current_state: str
    diagnostic_code: str | None
    next_action: str | None
    observed_at: datetime

@dataclass(frozen=True)
class AttemptSummary:
    item_id: str
    number: int
    outcome: str
    retryable: bool
    next_attempt_at: datetime | None
```

This is deliberately read-only: it makes status/review surfaces comparable without pretending that a Panda duplicate gate, a Subtext source collision, and a Literature provider ambiguity have interchangeable transition rules.

## Decision

Treat “intake” as a **product-owned lifecycle with a common observation vocabulary**, not as a shared runtime. Revisit a reusable worker control plane only if Panda or Subtext independently acquire (a) paid/non-idempotent provider dispatch, (b) concurrent workers, and (c) a requirement to recover safely after a worker dies mid-call.
