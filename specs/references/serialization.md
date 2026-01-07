# Serialization in workflow systems (LangGraph, Temporal, DBOS, Mastra, Inngest)

This document is a focused survey of how a few workflow frameworks handle the same production concern called out in `specs/tmp/issues_summary.md`: **durable systems need to persist intermediate values, but serialization is hard** (security, type support, size, and schema evolution).

## The underlying problem (ELI20)

If you want to *pause and resume* a workflow, you must store its “memory” somewhere durable (DB, object store, etc.). That “memory” is whatever your nodes/steps returned (hypergraph: “outputs ARE state”).

Real systems run into four predictable problems:

1. **Security:** “serialize anything” formats (notably Python `pickle`) are convenient but can enable RCE if you ever deserialize attacker-controlled bytes.
2. **Type support:** plain JSON is safe and portable, but can’t represent many types without conventions (datetime, bytes, numpy/pandas, custom classes).
3. **Large payloads:** embeddings, dataframes, images, and long transcripts can exceed DB row/event limits and/or get expensive to rehydrate.
4. **Evolution:** renames and schema drift break decoding unless you version and/or keep decoders backward compatible.

---

## Quick comparison

| System | Default persisted format | Extensible? | Large payload posture | Security posture |
|---|---|---:|---|---|
| **LangGraph** | JSON-ish “JsonPlus” with optional pickle fallback | Yes (pluggable serializer) | Separate “blobs” storage for large values | Can encrypt; pickle is opt-in fallback |
| **Temporal** | SDK “payloads” via DataConverter (JSON/protobuf/bytes) | Yes (data converter + payload codec) | Hard payload limits; recommends compression/batching/object-store references | No pickle; codec supports encryption/compression |
| **DBOS** | `pickle` → base64 string | Yes (`Serializer` interface) | No special blob system; you store what you serialize | Pickle by default (convenient, risky) |
| **Mastra** | JSON (`JSON.stringify`) in storage adapters | Yes (swap storage providers; but still JSON-y) | No explicit blob tier in core; DB row sizes apply | JSON by default; no built-in encryption in core |
| **Inngest** | JSON (step outputs + run state) | Limited (must remain JSON-compatible) | Hard limits (step output + total run state) | JSON enforced; portable across languages |

---

## LangGraph

**What they do:**
- Default is a “JSON-plus” serializer (`JsonPlusSerializer`) that handles JSON primitives plus common higher-level types (messages, Pydantic, dataclasses).
- They support **an explicit pickle fallback** for “hard” Python objects (example use case: Pandas).
- They support **encryption** via an `EncryptedSerializer`.
- Postgres persistence splits state across tables:
  - `checkpoints`: metadata + inline JSONB values
  - `checkpoint_blobs`: **large serialized values**
  - `checkpoint_writes`: pending writes (fault tolerance)

**Why it matters for the hypergraph issue:**
- LangGraph makes the “pickle is dangerous” trade-off explicit by pushing it behind `pickle_fallback=True`.
- They treat “large payloads” as a first-class storage concern (separate blob table), even if it’s still inside the same DB.

**Pointers:**
- hypergraph repo reference: `specs/references/langgraph/durable-execution.md` (see “Serialization” + Postgres schema section)
- LangGraph docs: https://docs.langchain.com/oss/python/langgraph/

---

## Temporal

**What they do:**
- Temporal persists workflow history as events; **all workflow/activity args & return values are “payloads”**.
- The Python SDK uses a `DataConverter` which explicitly composes:
  - `PayloadConverter` (Python values ⇄ payloads)
  - `PayloadCodec` (encode/decode bytes; used for compression/encryption)
- Default payload conversion supports:
  - `None`, `bytes`, all protobuf messages, and anything `json.dump` accepts.
- Temporal enforces **hard size limits**, and the official docs recommend mitigation strategies:
  - compress with a custom payload codec
  - batch into smaller chunks
  - offload large payloads to an object store and pass references

**Concrete numbers (from Temporal docs):**
- Max payload per single request: **2 MB**
- Max Event History transaction size: **4 MB**
- Workflow Event History is limited (events/total size) and needs management (e.g., continue-as-new) for long workflows

**Why it matters for the hypergraph issue:**
- Temporal’s model is “portable by default”: avoid pickle; encode types into JSON/protobuf/bytes.
- They operationalize “large outputs” with real budgets and a canonical solution: store blobs elsewhere, pass pointers.
- The “codec” layer is a clean hook for encryption-at-rest and compression without changing business logic.

**Pointers:**
- DataConverter API docs (Python): https://python.temporal.io/temporalio.converter.DataConverter.html
- Default payload converter doc (Python): https://python.temporal.io/temporalio.converter.DefaultPayloadConverter.html
- Blob size limit guidance (official docs): https://docs.temporal.io/troubleshooting/blob-size-limit-error

---

## DBOS

**What they do:**
- DBOS persists workflow state in its system DB tables, and **serializes “program data” via a serializer interface**.
- **Default is Python `pickle` then Base64 encoding** (stored as `TEXT`), but you can supply a custom serializer.
- Introspection paths try to be resilient: if deserialization fails, DBOS logs a warning and returns the raw serialized string instead of crashing (`safe_deserialize`).

**Why it matters for the hypergraph issue:**
- DBOS is the “convenient default” end of the spectrum: easiest dev UX, but inherits pickle’s security + evolution problems.
- They mitigate *operationally* by allowing custom serializers and making introspection tolerant of decode failures.

**Pointers:**
- DBOS docs (Custom Serialization): https://docs.dbos.dev/python/reference/contexts#custom-serialization
- DBOS code (default pickle serializer): https://github.com/dbos-inc/dbos-transact-py/blob/main/dbos/_serialization.py

---

## Mastra

**What they do:**
- Mastra persists workflow execution state as **workflow snapshots** in a storage backend (default is LibSQL/SQLite; Postgres exists).
- Storage adapters typically coerce values via:
  - `Date` → ISO string
  - `object` → `JSON.stringify(object)`
- Snapshot parsing is “best effort”: when parsing fails, they keep/return the raw snapshot string.
- They optimize some queries by treating the snapshot as JSON (e.g., filtering via `json_extract(snapshot, '$.status')` in LibSQL).

**Why it matters for the hypergraph issue:**
- Mastra chooses the “portable default”: JSON everywhere, which avoids pickle, but shifts type richness onto conventions (you get strings back for dates, custom classes flatten).
- Their “best effort parsing” mirrors DBOS’ `safe_deserialize`: introspection should not be fragile.

**Pointers (Mastra repo):**
- LibSQL workflows domain (snapshot read/write + JSON parse fallback): `stores/libsql/src/storage/domains/workflows/index.ts`
- LibSQL value coercion rules (`JSON.stringify` / ISO timestamps): `stores/libsql/src/storage/db/utils.ts`
- Storage overview: `packages/core/src/storage/README.md`

---

## Inngest

**What they do:**
- Step outputs are **serialized as JSON** (“Return values and serialization” is explicit in docs).
- They enforce size limits at the platform layer:
  - Step-returned data limit: **4 MB**
  - Total function run state limit (event data + all step outputs + function output + metadata): **32 MB**
  - Event payload size depends on plan (e.g., free tier 256KB, upgradable to 3MB)

**Why it matters for the hypergraph issue:**
- Inngest forces the JSON trade-off: safety + portability, but you must design your state schema.
- Hard limits + clear budgeting make “large outputs” an explicit architectural decision (store externally, return refs).

**Pointers:**
- `step.run()` serialization statement: https://www.inngest.com/docs/reference/functions/step-run
- Usage limits (payload + run state budgets): https://www.inngest.com/docs/usage-limits/inngest

---

## Patterns to copy into hypergraph (design-level takeaways)

These show up repeatedly across systems:

1. **Safe default, explicit escape hatch**
   - Default to JSON/MsgPack/Protobuf-like safety.
   - If you support pickle, gate it behind an explicit “dev-only / trusted-store-only” toggle like LangGraph (and document the risk loudly).

2. **Two-stage pipeline: “value → payload → codec”**
   - Temporal’s split (`PayloadConverter` vs `PayloadCodec`) is a clean model:
     - conversion handles types
     - codec handles compression/encryption

3. **Hard size budgets + “blob pointer” first-class type**
   - Temporal and Inngest treat size as a platform limit, not a user surprise.
   - LangGraph adds a “blob” tier (even inside DB). For hypergraph, this maps naturally to: inline value vs external blob reference.

4. **Introspection should be resilient**
   - DBOS and Mastra both degrade gracefully when decoding fails (return raw string + warning).
   - For hypergraph, that suggests: “failing to decode should not prevent listing workflows / viewing steps”.

5. **Version metadata travels with bytes**
   - Even if you pick JSON, include (serializer name, schema version, app version) alongside payload bytes so decoders can evolve safely.

