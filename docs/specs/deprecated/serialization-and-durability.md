# Serialization and Durability

**How hypergraph persists outputs, handles large values, and ensures resume correctness.**

Note: This content is consolidated in `specs/reviewed/durability.md`. This file remains as a deep-dive focused on serialization/artifacts.

This document consolidates the design for:
- Output persistence (what gets stored, where, how)
- Serialization (formats, security, extensibility)
- Resume semantics (when to skip vs rerun nodes)
- Large value handling (inline vs blob references)

Related specs:
- [State Model](../reviewed/state-model.md) - "Outputs ARE state" philosophy
- [Checkpointer](../reviewed/checkpointer.md) - Persistence interface and StepRecord
- [Durable Execution](../reviewed/durable-execution.md) - DBOS integration

---

## The Core Invariant

> **If a step can be skipped on resume, then the step must have durable outputs (inline or referenced).**

Equivalently:
- **No persisted outputs => the step must rerun** to recreate missing values

This invariant ensures:
- Resume correctness (downstream nodes have their inputs)
- Fork correctness (forked state matches what happened)
- Time-travel correctness (historical state is reconstructible)

---

## Why "Persist Everything" Is Both Right and Problematic

### Why It's Right

In hypergraph, "outputs ARE state". When a step completes:
1. The runner marks it done
2. On resume, the runner skips it
3. Downstream nodes need its outputs

If we don't persist outputs, resume breaks silently.

### Why It's Problematic

Persisting everything naively causes:

| Problem | Example |
|---------|---------|
| **Storage explosion** | 10GB embedding vectors in every step |
| **Serialization failures** | Arbitrary class instances, file handles, generators |
| **Security risks** | Pickle enables RCE if storage is compromised |
| **Version brittleness** | Class renames break deserialization |

The solution is not "stop persisting" — it's **persist everything, but not all outputs are stored the same way**.

---

## The Durability Matrix

Three dimensions determine how to handle each output:

### Dimension 1: Can We Skip on Resume?

| Value | Meaning | Examples |
|-------|---------|----------|
| **Yes (exactly-once)** | Step has side effects or non-deterministic output; cannot safely rerun | Send email, charge card, call external API, generate UUID |
| **No (rerunnable)** | Step is idempotent and deterministic; safe to rerun | Parse JSON, compute hash, transform data |

### Dimension 2: Is Output Deterministic?

| Value | Meaning | Examples |
|-------|---------|----------|
| **Deterministic** | Same inputs => same outputs | Pure functions, data transforms |
| **Non-deterministic** | Outputs vary on rerun | LLM calls, random, timestamps, external reads |

### Dimension 3: Output Size

| Value | Meaning | Examples |
|-------|---------|----------|
| **Light** | Small enough for inline DB storage (< 1MB) | Strings, dicts, small lists |
| **Heavy** | Too large for inline storage | Embeddings, DataFrames, images |

### The 8-Cell Matrix

| Skip on Resume? | Deterministic? | Size | Persistence Strategy | Why |
|---|---|---|---|---|
| Yes | Yes | Light | **Persist inline** | Cheap; enables exact resume/fork/time-travel |
| Yes | Yes | Heavy | **Persist ref** | Needs durable output; store bytes externally |
| Yes | No | Light | **Persist inline** | Rerun would diverge; must snapshot |
| Yes | No | Heavy | **Persist ref** | Snapshot semantics + size constraints |
| No | Yes | Light | **Optional** | Can recompute cheaply if needed |
| No | Yes | Heavy | **Persist ref or recompute** | Trade storage vs compute cost |
| No | No | Light | **Choose explicitly** | Persist for stability, or accept fresh-on-resume |
| No | No | Heavy | **Persist ref** | Recompute is expensive and may diverge |

### Key Insight

**Only rerunnable + deterministic steps can skip output persistence.** Everything else must persist because:
- Exactly-once => can't re-execute the side effect
- Non-deterministic => rerun gives different result, breaks downstream

---

## How Other Frameworks Handle This

### Temporal (Strictest)

**Approach:** Workflows must be 100% deterministic. All non-deterministic operations go in Activities.

```java
// Activity (persisted, not replayed)
@ActivityMethod
String callExternalApi(String input);

// Workflow (deterministic, replayed on resume)
int random = Workflow.sideEffect(Integer.class, () -> random.nextInt(100));
String uuid = Workflow.randomUUID().toString();
```

**What gets persisted:**
- Complete event history (every command, signal, timer)
- Activity results
- SideEffect results

**Key feature:** TypeScript SDK actually *replaces* `Math.random()`, `Date`, and `setTimeout()` with deterministic versions in the V8 sandbox.

### DBOS (Step-Boundary)

**Approach:** Workflows must be deterministic. Non-deterministic code must be in `@step`.

```python
# WRONG - non-deterministic in workflow
@DBOS.workflow()
def bad():
    if random.random() > 0.5:  # Breaks on replay!
        do_thing()

# RIGHT - non-deterministic in step
@DBOS.step()
def flip_coin():
    return random.random() > 0.5

@DBOS.workflow()
def good():
    if flip_coin():  # Step result is cached
        do_thing()
```

**What gets persisted:**
- Workflow inputs (at start)
- Each step's output (one DB write per step)
- Workflow outcome (at completion)

**Key difference from Temporal:** Replay is at step boundaries, not full event history.

### LangGraph (Pragmatic)

**Approach:** No strict determinism requirement. `@task` decorator marks functions whose results should be cached.

```python
@task
def call_external_api():
    return requests.get("https://api.example.com").json()
```

**What gets persisted:**
- Checkpoints at node boundaries
- Task results (cached and retrieved on replay)

**Key feature:** Three durability modes: "exit", "sync", "full".

### Mastra (Minimal)

**Approach:** No explicit determinism requirements. Recommends idempotency as best practice.

**What gets persisted:**
- Workflow snapshots (run ID, input data, step results)

---

## hypergraph's Approach: Node = Step

hypergraph follows the DBOS model but with implicit step boundaries:

> **Every node is implicitly a step.** Node outputs are persisted. On resume, completed nodes are skipped and their cached outputs are used.

This means:
- Users don't need to declare `@step` — nodes *are* steps
- Non-deterministic operations should be in their own nodes
- The framework persists all node outputs by default

### Example

```python
@node(output_name="choice")
def flip_coin() -> bool:
    return random.random() > 0.5  # Non-deterministic

@node(output_name="result")
def process(choice: bool) -> str:
    if choice:
        return do_thing_a()
    return do_thing_b()
```

On first run: `flip_coin` executes, returns True, persisted.
On resume: `flip_coin` is skipped, cached True is used, same path taken.

---

## Serialization Architecture

### Two-Stage Pipeline (Following Temporal)

```
Node Output → Serializer → bytes → Codec → bytes → Storage
                (format)            (transform)
```

| Stage | Responsibility | Examples |
|-------|----------------|----------|
| **Serializer** | Convert Python values to bytes | JSON, msgpack, pickle |
| **Codec** | Transform bytes (optional) | Encryption, compression |
| **Storage** | Persist bytes | SQLite, Postgres, S3 |

### Serializer Interface

```python
class Serializer(Protocol):
    """Convert values to/from bytes."""

    def serialize(self, value: Any) -> bytes:
        """Convert value to bytes for storage."""
        ...

    def deserialize(self, data: bytes) -> Any:
        """Convert bytes back to value."""
        ...
```

### Codec Interface

```python
class Codec(Protocol):
    """Transform serialized bytes (encryption, compression)."""

    def encode(self, data: bytes) -> bytes:
        """Transform bytes before storage (e.g., encrypt)."""
        ...

    def decode(self, data: bytes) -> bytes:
        """Transform bytes after retrieval (e.g., decrypt)."""
        ...
```

### Built-in Serializers

| Serializer | Default? | Supports | Security | When to Use |
|------------|:--------:|----------|----------|-------------|
| `JsonSerializer` | Yes | JSON primitives, datetime, bytes (base64) | Safe | Most cases |
| `MsgPackSerializer` | No | Same as JSON but binary | Safe | Performance-sensitive |
| `PickleSerializer` | No | Arbitrary Python objects | **Dangerous** | Dev only, trusted storage |

### Security Posture

**Pickle is opt-in and loud:**

```python
from hypergraph.checkpointers import PickleSerializer

# Explicit opt-in with warning
checkpointer = SqliteCheckpointer(
    path="./workflows.db",
    serializer=PickleSerializer(),  # ⚠️ Only for dev/trusted storage
)
```

LangGraph had a critical RCE vulnerability (CVE-2025-64439) in their serializer. We learn from this by:
1. Defaulting to JSON (safe)
2. Making pickle explicit and documented
3. Recommending production serializers without pickle

---

## Large Value Handling: BlobRef

### The Problem

Embeddings, DataFrames, and images don't fit in DB rows efficiently:
- Temporal: 2MB hard limit per payload
- DBOS: ~2MB recommended
- Postgres: 1GB technical limit but performance degrades

### The Solution: Artifact References

```python
@dataclass(frozen=True)
class ArtifactRef:
    """Reference to externally-stored large value."""
    storage: str       # "s3", "gcs", "file", etc.
    key: str           # Unique identifier in storage
    size: int          # Bytes
    content_type: str  # MIME type or "application/octet-stream"
    checksum: str      # For integrity verification
```

### How It Works

```python
# User code (unchanged)
@node(output_name="embeddings")
def compute_embeddings(text: str) -> np.ndarray:
    return model.encode(text)  # Returns 10MB array

# What gets persisted in StepRecord
step.values = {
    "embeddings": ArtifactRef(
        storage="s3",
        key="workflows/123/embeddings/abc123",
        size=10_000_000,
        content_type="application/x-numpy",
        checksum="sha256:...",
    )
}

# On resume, framework resolves ref back to value
embeddings = await artifact_store.get(step.values["embeddings"])
```

### Configuration

```python
runner = AsyncRunner(
    checkpointer=SqliteCheckpointer("./workflows.db"),
    artifact_store=S3ArtifactStore(bucket="my-bucket"),
    blob_threshold=1_000_000,  # 1MB - values larger than this become refs
)
```

### Artifact Store Interface

```python
class ArtifactStore(Protocol):
    """Store and retrieve large values externally."""

    async def put(
        self,
        value: bytes,
        content_type: str,
        workflow_id: str,
    ) -> ArtifactRef:
        """Store bytes and return reference."""
        ...

    async def get(self, ref: ArtifactRef) -> bytes:
        """Retrieve bytes from reference."""
        ...

    async def delete(self, ref: ArtifactRef) -> None:
        """Delete stored artifact."""
        ...
```

---

## Output Storage Tiers

### Tier 1: State (Inline)

Small JSON-ish values persisted directly in StepRecord.

```python
step.values = {"answer": "The capital is Paris", "confidence": 0.95}
```

### Tier 2: Artifact (Ref)

Large or non-JSON values stored externally; StepRecord contains reference.

```python
step.values = {"embeddings": ArtifactRef(...)}
```

### Tier 3: Event (Observability Only)

Streaming/UI/debug data emitted for observability but not part of state.

```python
# Streaming chunks go to EventProcessor, not state
async for chunk in generate_streaming(...):
    yield chunk  # Events, not persisted state
```

### Tier 4: Transient (Advanced)

In-process only; never persisted. Only valid for rerunnable nodes.

```python
@node(output_name="connection", transient=True)
def get_db_connection() -> Connection:
    return db.connect()  # Cannot persist; will reconnect on resume
```

**Warning:** Transient outputs break time-travel and fork semantics. Use only when:
- The node is deterministic (can safely rerun)
- Rerunning is acceptable (fast, no side effects)

---

## Resume Semantics

### Default: Snapshot (Exactly-Once)

```python
@node(output_name="confirmation")
def send_email(to: str, body: str) -> str:
    email_service.send(to, body)  # Side effect!
    return f"Sent to {to}"
```

- Step is marked complete after execution
- On resume, step is skipped
- Cached output is used

### Opt-in: Recompute

```python
@node(output_name="timestamp", recompute=True)
def get_current_time() -> str:
    return datetime.now().isoformat()  # Fresh on every run
```

- Step reruns on resume
- No output persistence needed
- Breaks time-travel/fork (accepted trade-off)

---

## Non-Persistable Outputs

Some values cannot be serialized meaningfully:
- Live resources (DB connections, file handles)
- Generators, coroutines
- Closures with captured state

### Pattern A: Return Persistable Handle (Recommended)

```python
# Instead of returning a live connection...
@node(output_name="connection")
def get_connection() -> Connection:  # ❌ Cannot persist
    return db.connect()

# ...return connection parameters
@node(output_name="db_config")
def get_db_config() -> dict:  # ✅ Persistable
    return {"host": "localhost", "port": 5432}

@node(output_name="result")
def query(db_config: dict) -> list:
    conn = db.connect(**db_config)  # Reconnect from config
    return conn.execute("SELECT ...")
```

### Pattern B: Mark as Transient

```python
@node(output_name="conn", transient=True)
def get_connection() -> Connection:
    return db.connect()  # Rebuilt on resume
```

### Pattern C: Serialization Error (Default)

If output is not serializable and not marked transient, the framework raises an error with guidance.

---

## Schema Evolution

### What Breaks Deserialization

| Change | JSON Impact | Pickle Impact |
|--------|-------------|---------------|
| Add field | ✅ Safe (missing = None) | ✅ Safe |
| Remove field | ✅ Safe (ignored) | ⚠️ May fail |
| Rename field | ❌ Old data missing new name | ❌ Fails |
| Change type | ⚠️ May work, may fail | ❌ Usually fails |
| Rename class | N/A | ❌ Fails |

### Mitigation: Version Metadata

Every persisted value includes version metadata:

```python
@dataclass
class SerializedValue:
    data: bytes                    # The serialized payload
    serializer: str                # "json", "msgpack", "pickle"
    version: str                   # App-defined schema version
    created_at: datetime           # When serialized
```

This enables:
- Graceful degradation (old deserializer for old data)
- Migration tooling (transform old format to new)
- Debugging (know what version created the data)

### Graceful Degradation

Following DBOS and Mastra, introspection should not crash:

```python
async def get_state(workflow_id: str) -> dict:
    steps = await get_steps(workflow_id)
    state = {}
    for step in steps:
        try:
            state.update(deserialize(step.values))
        except DeserializationError as e:
            # Return raw data instead of crashing
            state[step.node_name] = RawValue(step.values, error=str(e))
            logger.warning(f"Failed to deserialize {step.node_name}: {e}")
    return state
```

---

## Size Limits and Recommendations

| Context | Limit | Recommendation |
|---------|-------|----------------|
| Inline value | 1 MB | Use ArtifactRef above this |
| Single ArtifactRef | 100 MB | Split or compress larger values |
| Total workflow state | No hard limit | Monitor; consider pruning old steps |
| Event payload | 1 MB | Summarize large streaming data |

---

## User-Facing API Summary

### Simple Case (No Configuration Needed)

```python
graph = Graph(nodes=[embed, retrieve, generate])
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./db"))

# All outputs persisted automatically
# Resume works transparently
result = await runner.run(graph, values={...}, workflow_id="chat-123")
```

### Custom Serialization

```python
from hypergraph.serializers import MsgPackSerializer

checkpointer = SqliteCheckpointer(
    path="./workflows.db",
    serializer=MsgPackSerializer(),
)
```

### Large Value Storage

```python
from hypergraph.artifacts import S3ArtifactStore

runner = AsyncRunner(
    checkpointer=SqliteCheckpointer("./db"),
    artifact_store=S3ArtifactStore(bucket="my-workflows"),
    blob_threshold=1_000_000,  # 1MB
)
```

### Encryption

```python
from hypergraph.codecs import AESCodec

checkpointer = SqliteCheckpointer(
    path="./workflows.db",
    codec=AESCodec(key_env="HYPERGRAPH_ENCRYPTION_KEY"),
)
```

---

## Summary

| Concern | hypergraph Approach |
|---------|---------------------|
| **What's persisted** | All node outputs (like DBOS steps) |
| **Serialization** | JSON default, pluggable serializers, pickle opt-in |
| **Large values** | Auto-offload to ArtifactStore via ArtifactRef |
| **Security** | No pickle by default; codec layer for encryption |
| **Resume** | Snapshot semantics (skip completed); opt-in recompute |
| **Schema evolution** | Version metadata; graceful degradation |

**Mental model:**
1. Every node is a step
2. Every step output is persisted (inline or ref)
3. Completed steps are skipped on resume
4. Large values become references automatically
