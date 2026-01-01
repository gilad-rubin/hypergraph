# Persistence API Reference

Complete protocol and type definitions for durable execution.

> **Looking for concepts and examples?** See [Persistence & Durable Execution](../architecture/persistence.md) first.

---

## Quick Navigation

| Type | Purpose |
|------|---------|
| [StepIdentity](#stepidentity) | Hierarchical step addressing |
| [StepSnapshot](#stepsnapshot) | Step execution record |
| [StepAttempt](#stepattempt) | Single retry attempt |
| [PendingWrite](#pendingwrite) | Partial batch completion |
| [StreamingState](#streamingstate) | Incremental stream progress |
| [StreamPersistenceMode](#streampersistencemode) | When to persist streaming chunks |
| [Checkpoint](#checkpoint) | Full workflow state snapshot |
| [Checkpointer](#checkpointer-protocol) | Persistence interface |
| [RetryPolicy](#retrypolicy) | Retry configuration |
| [WorkflowReusePolicy](#workflowreusepolicy) | Workflow ID reuse behavior |
| [Serializer](#serializer-protocol) | Custom serialization |
| [PayloadLimits](#payloadlimits) | Size constraints |
| [Lease](#lease) | Distributed step locking |

---

## Identity Types

### StepIdentity

Hierarchical step identifier supporting nested graphs.

```python
@dataclass(frozen=True)
class StepIdentity:
    """Hierarchical step identifier.

    Supports nested graphs with path-based addressing:
    - Root step: ("generate:0",)
    - Nested step: ("outer:0", "rag:1", "retrieve:0")

    Attributes:
        path: Tuple of step segments from root to this step.
    """

    path: tuple[str, ...]

    @property
    def step_id(self) -> str:
        """Full path as string.

        Example: "outer:0/rag:1/retrieve:0"
        """
        return "/".join(self.path)

    @property
    def parent_step_id(self) -> str | None:
        """Parent step ID, or None if root.

        Example: "outer:0/rag:1" for step "outer:0/rag:1/retrieve:0"
        """
        if len(self.path) <= 1:
            return None
        return "/".join(self.path[:-1])

    @property
    def local_step_id(self) -> str:
        """Just this step's portion.

        Example: "retrieve:0" for step "outer:0/rag:1/retrieve:0"
        """
        return self.path[-1]

    @property
    def depth(self) -> int:
        """Nesting depth (1 = root level)."""
        return len(self.path)

    @property
    def node_name(self) -> str:
        """Node name extracted from local step ID.

        Example: "retrieve" for step "retrieve:0"
        """
        return self.local_step_id.rsplit(":", 1)[0]

    @property
    def batch_index(self) -> int:
        """Batch index extracted from local step ID.

        Example: 0 for step "retrieve:0"
        """
        return int(self.local_step_id.rsplit(":", 1)[1])

    def child(self, local_id: str) -> "StepIdentity":
        """Create a child step identity.

        Example:
            parent = StepIdentity(("outer:0", "rag:1"))
            child = parent.child("retrieve:0")
            # child.step_id == "outer:0/rag:1/retrieve:0"
        """
        return StepIdentity(path=self.path + (local_id,))

    @classmethod
    def root(cls, node_name: str, batch_index: int) -> "StepIdentity":
        """Create a root-level step identity.

        Example:
            identity = StepIdentity.root("generate", 0)
            # identity.step_id == "generate:0"
        """
        return cls(path=(f"{node_name}:{batch_index}",))

    @classmethod
    def from_string(cls, step_id: str) -> "StepIdentity":
        """Parse a step ID string.

        Example:
            identity = StepIdentity.from_string("outer:0/rag:1/retrieve:0")
            # identity.path == ("outer:0", "rag:1", "retrieve:0")
        """
        return cls(path=tuple(step_id.split("/")))
```

---

## Step Types

### StepStatus

```python
StepStatus = Literal[
    "created",      # Scheduled, not yet claimed
    "pending",      # Claimed by worker, not started
    "running",      # Currently executing
    "completed",    # Finished successfully
    "failed",       # Failed after all retry attempts
    "interrupted",  # Paused for human input
]
```

**Status transitions:**

```
created → pending → running → completed
                  ↘         ↘ failed
                   → interrupted
```

### StepAttempt

```python
@dataclass
class StepAttempt:
    """Record of a single execution attempt.

    Attributes:
        attempt_number: Zero-indexed attempt number.
        started_at: When this attempt started.
        completed_at: When this attempt finished (success or failure).
        status: Outcome of this attempt.
        error: Error message if failed.
        outputs: Result if successful.
    """

    attempt_number: int
    started_at: datetime
    completed_at: datetime | None = None
    status: Literal["running", "success", "failed", "timeout"] = "running"
    error: str | None = None
    outputs: dict[str, Any] | None = None

    @property
    def duration_ms(self) -> float | None:
        """Duration in milliseconds, or None if not completed."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None
```

### StepSnapshot

```python
@dataclass
class StepSnapshot:
    """Complete record of a step execution.

    Combines execution state, outputs, retry history, and metadata.

    Attributes:
        identity: Hierarchical step identifier.
        run_id: Parent run identifier.
        node_name: Name of the executed node.
        status: Current execution status.
        inputs_hash: Hash of inputs (for cache matching).
        outputs: Step outputs (if completed).
        error: Error message (if failed).
        decision: Routing decision (for gates).
        attempts: List of execution attempts.
        streaming_state: Partial streaming progress (if streaming).
        created_at: When this step was created.
    """

    # Identity
    identity: StepIdentity
    run_id: str

    # Node info
    node_name: str

    # Execution state
    status: StepStatus = "created"

    # Inputs (for replay verification)
    inputs_hash: str | None = None

    # Results (populated after execution)
    outputs: dict[str, Any] | None = None
    error: str | None = None

    # For gates/routing
    decision: str | None = None

    # Retry tracking
    attempts: list[StepAttempt] = field(default_factory=list)

    # Streaming progress
    streaming_state: "StreamingState | None" = None

    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)

    # ---- Computed properties ----

    @property
    def step_id(self) -> str:
        """Full step ID string."""
        return self.identity.step_id

    @property
    def batch_index(self) -> int:
        """Batch index from identity."""
        return self.identity.batch_index

    @property
    def is_nested(self) -> bool:
        """True if this step is inside a nested graph."""
        return self.identity.depth > 1

    @property
    def attempt_count(self) -> int:
        """Number of execution attempts."""
        return len(self.attempts)

    @property
    def last_error(self) -> str | None:
        """Error from most recent failed attempt."""
        if self.attempts and self.attempts[-1].status == "failed":
            return self.attempts[-1].error
        return None

    @property
    def total_duration_ms(self) -> float:
        """Total time across all attempts."""
        return sum(
            a.duration_ms or 0
            for a in self.attempts
        )

    @property
    def started_at(self) -> datetime | None:
        """When first attempt started."""
        if self.attempts:
            return self.attempts[0].started_at
        return None

    @property
    def completed_at(self) -> datetime | None:
        """When final attempt completed."""
        if self.attempts and self.attempts[-1].completed_at:
            return self.attempts[-1].completed_at
        return None
```

---

## Parallel Execution Types

### PendingWrite

```python
@dataclass
class PendingWrite:
    """A completed step within an incomplete batch.

    When parallel steps run, completed ones are saved as pending writes
    before the entire batch finishes. This enables partial recovery
    if a crash occurs mid-batch.

    Attributes:
        step_id: Identifier of the completed step.
        node_name: Name of the executed node.
        outputs: Step outputs.
        completed_at: When the step completed.
    """

    step_id: str
    node_name: str
    outputs: dict[str, Any]
    completed_at: datetime = field(default_factory=datetime.utcnow)
```

**Usage pattern:**

```python
# During parallel execution
async def execute_parallel_batch(nodes: list[HyperNode], batch_index: int):
    # Check for pending writes from previous (crashed) attempt
    pending = await checkpointer.get_pending_writes(run_id, batch_index)
    completed_ids = {w.step_id for w in pending}

    # Only execute nodes that haven't completed
    remaining = [n for n in nodes if f"{n.name}:{batch_index}" not in completed_ids]

    async def run_one(node):
        result = await execute_step(node)

        # Save immediately as pending write
        await checkpointer.put_pending_write(
            run_id,
            batch_index,
            PendingWrite(
                step_id=f"{node.name}:{batch_index}",
                node_name=node.name,
                outputs=result,
            ),
        )
        return result

    # Execute remaining in parallel
    results = await asyncio.gather(*[run_one(n) for n in remaining])

    # Promote to committed when batch completes
    await checkpointer.promote_pending_writes(run_id, batch_index)
```

---

## Streaming Types

### StreamingState

```python
@dataclass
class StreamingState:
    """Tracks partial streaming output for recovery.

    For long-running streaming outputs (e.g., LLM generation),
    this tracks incremental progress so recovery can continue
    from the last persisted chunk instead of starting over.

    Attributes:
        step_id: Identifier of the streaming step.
        output_name: Name of the streaming output.
        chunks: Accumulated chunks so far.
        chunk_count: Total chunks received.
        last_persisted_index: Index of last persisted chunk.
        is_complete: Whether streaming finished.
    """

    step_id: str
    output_name: str
    chunks: list[str] = field(default_factory=list)
    chunk_count: int = 0
    last_persisted_index: int = 0
    is_complete: bool = False

    @property
    def accumulated_value(self) -> str:
        """Join all chunks into final value."""
        return "".join(self.chunks)

    @property
    def unpersisted_chunks(self) -> list[str]:
        """Chunks not yet persisted."""
        return self.chunks[self.last_persisted_index:]


class StreamPersistenceMode(Enum):
    """Controls when streaming chunks are persisted.

    Principle: Resume from what the user saw.
    If the user watched streaming output before a crash, they should
    continue from exactly where they left off.

    Modes:
        IMMEDIATE: Persist each chunk as sent to user (default).
                   Highest durability, more I/O.
        BATCHED: Persist every N chunks or N seconds.
                 Balanced, may replay a few chunks on resume.
        ON_COMPLETE: Only persist when stream finishes.
                     Fastest, but lose all progress on crash.
    """

    IMMEDIATE = "immediate"
    BATCHED = "batched"
    ON_COMPLETE = "on_complete"


@dataclass
class BatchedStreamConfig:
    """Configuration for BATCHED streaming mode.

    Only used when StreamPersistenceMode.BATCHED is selected.
    Persistence triggers when ANY threshold is reached.
    """

    chunk_interval: int = 50
    time_interval_seconds: float = 2.0
    byte_threshold: int = 5_000
```

**Usage pattern (immediate mode - default):**

```python
async def execute_streaming_node(node, step_id, mode: StreamPersistenceMode):
    # Check for partial stream to resume
    partial = await checkpointer.load_partial_stream(run_id, step_id, node.output_name)

    if partial and not partial.is_complete:
        start_index = partial.chunk_count
        accumulated = partial.chunks.copy()
    else:
        start_index = 0
        accumulated = []

    async for chunk in node.stream(inputs, resume_from=start_index):
        accumulated.append(chunk)

        # Send to user
        emit(StreamingChunkEvent(chunk=chunk, index=len(accumulated) - 1))

        # Persist immediately (default) - what user saw is saved
        if mode == StreamPersistenceMode.IMMEDIATE:
            await checkpointer.persist_chunk(
                run_id, step_id, node.output_name, chunk, len(accumulated)
            )

    # Mark stream complete
    await checkpointer.complete_stream(
        run_id, step_id, node.output_name,
        "".join(accumulated),
    )
```

---

## Interrupt Types

### SuspendPayload

Inspired by [Mastra's suspendPayload](https://mastra.ai/docs/v1/workflows/human-in-the-loop), structured context for why a workflow paused.

```python
@dataclass
class SuspendPayload:
    """Structured context explaining why workflow is suspended.

    Provides clear information to users/systems about what's needed.

    Attributes:
        reason: Human-readable explanation of why we paused.
        required_action: What the user needs to do.
        deadline: Optional deadline for response.
        metadata: Additional context-specific data.
    """

    reason: str
    required_action: str | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

### InterruptState

```python
@dataclass
class InterruptState:
    """Complete state of an interrupted workflow.

    Provides both the context (why paused) and schema (what's needed).

    Attributes:
        interrupt_name: Name of the interrupt point.
        suspend_payload: Structured reason for suspension.
        resume_schema: Type/schema of expected resume data.
        value: The value to show the user (from input_param).
    """

    interrupt_name: str
    suspend_payload: SuspendPayload | None
    resume_schema: type | None  # Pydantic model or dataclass
    value: Any

    def validate_resume_data(self, data: Any) -> bool:
        """Validate resume data against schema."""
        if self.resume_schema is None:
            return True
        # Use Pydantic or dataclass validation
        ...
```

### BailResult

Inspired by [Mastra's bail()](https://mastra.ai/docs/v1/workflows/human-in-the-loop), graceful exit without error.

```python
@dataclass
class BailResult:
    """Result of a bailed workflow.

    Workflows can bail (exit gracefully) when users reject actions
    or conditions make continuation pointless.

    Attributes:
        reason: Why the workflow bailed.
        outputs: Any partial outputs to return.
        step_id: Where the bail occurred.
    """

    reason: str
    outputs: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None


def bail(reason: str, outputs: dict[str, Any] | None = None) -> NoReturn:
    """Exit workflow gracefully without error.

    Use when user rejects an action or continuation is pointless.
    Workflow completes with status="bailed", not "error".

    Args:
        reason: Why bailing.
        outputs: Partial outputs to include in result.

    Example:
        if not user_decision.approved:
            bail("User rejected the proposal", outputs={"status": "rejected"})
    """
    raise BailException(reason=reason, outputs=outputs or {})
```

---

## Checkpoint Types

### Checkpoint

```python
@dataclass
class Checkpoint:
    """Complete snapshot of workflow execution state.

    Checkpoints are created at key boundaries:
    - After each batch completes
    - At interrupts
    - On workflow completion

    Attributes:
        checkpoint_id: Unique checkpoint identifier.
        workflow_id: Parent workflow identifier.
        run_id: Current run identifier.
        parent_run_id: Parent run ID (for nested graphs).
        batch_index: Current execution position.
        values: All accumulated output values.
        versions: Version numbers for staleness detection.
        status: Current workflow status.
        pending_interrupt: Interrupt name if paused.
        interrupt_value: Value to show user if interrupted.
        pending_writes: Partial batch completions.
        partial_streams: Incomplete streaming outputs.
        nested_runs: Map of nested graph paths to run IDs.
        created_at: When checkpoint was created.
        graph_hash: Hash of graph definition (for version checking).
        step_count: Total steps executed.
    """

    # ---- Identity ----
    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str = ""
    run_id: str = ""
    parent_run_id: str | None = None

    # ---- Position ----
    batch_index: int = 0

    # ---- State ----
    values: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)

    # ---- Execution Status ----
    status: Literal["running", "interrupted", "completed", "bailed", "error"] = "running"

    # Interrupt state (if status == "interrupted")
    pending_interrupt: str | None = None
    interrupt_value: Any = None
    suspend_payload: SuspendPayload | None = None  # Structured suspend context
    resume_schema: type | None = None  # Expected resume data type

    # Bail state (if status == "bailed")
    bail_reason: str | None = None

    # ---- Pending Work ----
    pending_writes: list[PendingWrite] = field(default_factory=list)
    partial_streams: dict[str, StreamingState] = field(default_factory=dict)

    # ---- Nested Graphs ----
    nested_runs: dict[str, str] = field(default_factory=dict)

    # ---- Metadata ----
    created_at: datetime = field(default_factory=datetime.utcnow)
    graph_hash: str | None = None
    step_count: int = 0

    # ---- Computed Properties ----

    @property
    def is_interrupted(self) -> bool:
        """True if paused at an interrupt."""
        return self.status == "interrupted"

    @property
    def is_complete(self) -> bool:
        """True if workflow finished."""
        return self.status in ("completed", "error")

    def get_value(self, name: str, default: Any = None) -> Any:
        """Get a value from checkpoint state."""
        return self.values.get(name, default)

    def get_version(self, name: str) -> int:
        """Get version number for a value."""
        return self.versions.get(name, 0)


@dataclass
class CheckpointMetadata:
    """Lightweight checkpoint info for listing.

    Used by list operations to avoid loading full state.
    """

    checkpoint_id: str
    workflow_id: str
    run_id: str
    batch_index: int
    status: Literal["running", "interrupted", "completed", "error"]
    created_at: datetime
    interrupt_name: str | None = None
    step_count: int = 0
```

---

## Retry Policy

### RetryPolicy

```python
@dataclass
class RetryPolicy:
    """Configures retry behavior for failed steps.

    Attributes:
        max_attempts: Maximum total attempts (including first).
        initial_delay: Seconds to wait before first retry.
        max_delay: Maximum delay between retries.
        backoff_multiplier: Multiply delay by this after each retry.
        retryable_exceptions: Exception types that trigger retry.
        non_retryable_exceptions: Exception types that never retry.
    """

    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    backoff_multiplier: float = 2.0
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,)
    non_retryable_exceptions: tuple[type[Exception], ...] = ()

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Determine if step should retry after failure.

        Args:
            attempt: Current attempt number (1-indexed).
            error: The exception that was raised.

        Returns:
            True if should retry, False if should fail.
        """
        if attempt >= self.max_attempts:
            return False
        if isinstance(error, self.non_retryable_exceptions):
            return False
        return isinstance(error, self.retryable_exceptions)

    def delay_for_attempt(self, attempt: int) -> float:
        """Calculate delay before next retry.

        Uses exponential backoff capped at max_delay.

        Args:
            attempt: Current attempt number (1-indexed).

        Returns:
            Seconds to wait before next attempt.
        """
        delay = self.initial_delay * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.max_delay)
```

**Usage:**

```python
# Node with retry policy
@node(
    outputs="result",
    retry=RetryPolicy(
        max_attempts=5,
        initial_delay=2.0,
        backoff_multiplier=3.0,
        retryable_exceptions=(TimeoutError, ConnectionError),
    )
)
async def call_external_api(query: str) -> dict:
    return await api.call(query)

# Delays: 2s, 6s, 18s, 54s (capped at 60s)
```

---

## Durability Mode

```python
DurabilityMode = Literal["sync", "async", "exit"]
```

| Mode | Behavior | Use Case |
|------|----------|----------|
| `"sync"` | Block until persisted | Critical workflows |
| `"async"` | Persist asynchronously | General workloads |
| `"exit"` | Only on completion/interrupt | High throughput |

---

## WorkflowReusePolicy

Controls behavior when starting a workflow with an existing `workflow_id`.

```python
class WorkflowReusePolicy(Enum):
    """Policy for handling duplicate workflow IDs.

    The workflow_id serves as an idempotency key. This policy controls
    what happens when a workflow is started with an ID that already exists.

    Matches Temporal's Workflow ID Reuse Policy:
    https://docs.temporal.io/workflow-execution/workflowid-runid
    """

    REJECT_DUPLICATE = "reject_duplicate"
    """Error if workflow exists (any status). Use for strictly unique operations."""

    ALLOW_IF_FAILED = "allow_if_failed"
    """Allow only if previous workflow failed/cancelled/timed out.
    Use when retrying failed workflows."""

    TERMINATE_RUNNING = "terminate_running"
    """Terminate existing running workflow, start new one.
    Use when replacing a running workflow is acceptable."""

    RETURN_EXISTING = "return_existing"
    """If workflow completed, return existing result. If running, wait and return.
    Default behavior — provides idempotent workflow execution."""
```

**Usage:**

```python
runner = AsyncRunner(
    checkpointer=SQLiteCheckpointer("./db.sqlite"),
    workflow_reuse_policy=WorkflowReusePolicy.RETURN_EXISTING,  # Default
)

# First call executes the workflow
result1 = await runner.run(graph, inputs={...}, workflow_id="order-123")

# Second call with same ID returns cached result (no re-execution)
result2 = await runner.run(graph, inputs={...}, workflow_id="order-123")
assert result1.outputs == result2.outputs
```

---

## Serializer Protocol

Custom serialization for step outputs and checkpoint state.

```python
from typing import Protocol, TypeVar, Callable

T = TypeVar("T")


class Serializer(Protocol):
    """Protocol for serializing/deserializing checkpoint payloads.

    Default implementation uses msgpack with JSON fallback,
    supporting common types (primitives, dataclasses, Pydantic, datetime, etc.).

    Inspired by LangGraph's JsonPlusSerializer.
    """

    def serialize(self, value: Any) -> bytes:
        """Serialize a value to bytes.

        Raises:
            SerializationError: If value cannot be serialized.
            PayloadTooLargeError: If serialized size exceeds limits.
        """
        ...

    def deserialize(self, data: bytes, expected_type: type[T] | None = None) -> T:
        """Deserialize bytes to a value.

        Args:
            data: Serialized bytes.
            expected_type: Optional type hint for deserialization.

        Raises:
            DeserializationError: If data cannot be deserialized.
        """
        ...

    def register(self, type_: type[T]) -> Callable[[Callable[[T], bytes]], Callable[[T], bytes]]:
        """Decorator to register a custom encoder for a type."""
        ...

    def decoder(self, type_: type[T]) -> Callable[[Callable[[bytes], T]], Callable[[bytes], T]]:
        """Decorator to register a custom decoder for a type."""
        ...


# Built-in serializer with common type support
class DefaultSerializer:
    """Default serializer using msgpack with JSON fallback.

    Supports:
        - Primitives (str, int, float, bool, None)
        - Collections (list, dict, set, tuple)
        - Dataclasses and Pydantic models
        - datetime, date, time, timedelta
        - UUID, Enum
        - bytes (base64 encoded)
    """

    def __init__(
        self,
        *,
        pickle_fallback: bool = False,
        max_payload_size: int = 2 * 1024 * 1024,  # 2 MB
        warning_size: int = 256 * 1024,           # 256 KB
    ):
        """
        Args:
            pickle_fallback: If True, fall back to pickle for unsupported types.
                             Security warning: only use with trusted data.
            max_payload_size: Maximum serialized size in bytes.
            warning_size: Log warning when payload exceeds this size.
        """
        ...
```

**Custom type registration:**

```python
from hypergraph.persistence import DefaultSerializer
import pandas as pd

serializer = DefaultSerializer()

@serializer.register(pd.DataFrame)
def serialize_dataframe(df: pd.DataFrame) -> bytes:
    return df.to_parquet()

@serializer.decoder(pd.DataFrame)
def deserialize_dataframe(data: bytes) -> pd.DataFrame:
    import io
    return pd.read_parquet(io.BytesIO(data))
```

---

## PayloadLimits

Configuration for payload size constraints.

```python
@dataclass
class PayloadLimits:
    """Payload size limits for step outputs and checkpoints.

    Following Temporal's blob size limits:
    https://docs.temporal.io/cloud/limits

    Attributes:
        max_payload_size: Maximum size for a single payload (step output).
        warning_size: Log warning when payload exceeds this size.
        max_history_size: Maximum total size for workflow history.
    """

    max_payload_size: int = 2 * 1024 * 1024      # 2 MB
    warning_size: int = 256 * 1024               # 256 KB
    max_history_size: int = 50 * 1024 * 1024     # 50 MB

    def validate(self, payload: bytes, context: str = "") -> None:
        """Validate payload size, raising if too large.

        Args:
            payload: Serialized payload bytes.
            context: Description for error messages (e.g., "step output").

        Raises:
            PayloadTooLargeError: If payload exceeds max_payload_size.

        Logs:
            Warning if payload exceeds warning_size but under max.
        """
        size = len(payload)
        if size > self.max_payload_size:
            raise PayloadTooLargeError(
                f"{context} payload size ({size:,} bytes) exceeds limit "
                f"({self.max_payload_size:,} bytes). "
                f"Consider storing large data externally and passing a reference."
            )
        if size > self.warning_size:
            logger.warning(
                f"{context} payload size ({size:,} bytes) exceeds warning threshold "
                f"({self.warning_size:,} bytes). Consider reducing payload size."
            )
```

---

## Lease

Distributed locking for step execution in multi-worker deployments.

```python
@dataclass
class Lease:
    """A time-bound lock on a step for distributed execution.

    Leases prevent multiple workers from executing the same step.
    They auto-expire after TTL if not renewed (heartbeat).

    Inspired by distributed lease patterns:
    https://www.linkedin.com/pulse/leases-fences-distributed-designpatterns-pratik-pandey
    """

    step_id: str
    worker_id: str
    acquired_at: datetime
    expires_at: datetime
    lease_token: str  # Unique token to verify ownership

    @property
    def ttl_remaining(self) -> float:
        """Seconds until lease expires."""
        return max(0, (self.expires_at - datetime.utcnow()).total_seconds())

    @property
    def is_expired(self) -> bool:
        """True if lease has expired."""
        return datetime.utcnow() >= self.expires_at


class LeaseManager(Protocol):
    """Protocol for distributed lease management.

    Used by multi-worker deployments to coordinate step execution.
    Single-worker deployments can skip lease management.
    """

    async def acquire_step_lease(
        self,
        run_id: str,
        step_id: str,
        worker_id: str,
        ttl: float = 30.0,
    ) -> Lease | None:
        """Attempt to acquire a lease on a step.

        Args:
            run_id: Run identifier.
            step_id: Step to lock.
            worker_id: Identifier of this worker.
            ttl: Lease duration in seconds.

        Returns:
            Lease if acquired, None if step is already leased.
        """
        ...

    async def renew_lease(self, lease: Lease, ttl: float = 30.0) -> Lease:
        """Renew an existing lease (heartbeat).

        Args:
            lease: Current lease to renew.
            ttl: New TTL in seconds.

        Returns:
            Updated lease with new expiration.

        Raises:
            LeaseExpiredError: If lease already expired.
            LeaseNotOwnedError: If lease was taken by another worker.
        """
        ...

    async def release_lease(self, lease: Lease) -> None:
        """Release a lease early (step completed or failed)."""
        ...

    async def acquire_workflow_lock(
        self,
        workflow_id: str,
        worker_id: str,
        ttl: float = 60.0,
    ) -> Lease | None:
        """Acquire exclusive lock on a workflow for resumption.

        Only one worker can resume a workflow at a time.
        """
        ...


@dataclass
class LeaseConfig:
    """Configuration for lease behavior."""

    step_lease_ttl: float = 30.0
    """Default TTL for step leases in seconds."""

    heartbeat_interval: float = 10.0
    """How often to renew leases (should be < ttl/2)."""

    workflow_lock_ttl: float = 60.0
    """TTL for workflow-level locks."""

    retry_delay: float = 1.0
    """Delay before retrying failed lease acquisition."""
```

---

## Versioning API

Types for workflow versioning and code change compatibility.

```python
def patched(patch_id: str) -> bool:
    """Check if current execution should use patched code path.

    Use for backward-compatible code changes while workflows are in-flight.
    Inserts a marker in workflow history so replay uses consistent path.

    Inspired by Temporal's patching:
    https://docs.temporal.io/develop/python/versioning

    Args:
        patch_id: Unique identifier for this patch (e.g., "v2-new-algorithm").

    Returns:
        True for new workflows (use new code).
        False for replaying old workflows (use old code).

    Example:
        @node(outputs="result")
        async def process(data: dict) -> dict:
            if patched("v2-improved-algorithm"):
                return new_algorithm(data)  # New workflows
            else:
                return old_algorithm(data)  # In-flight workflows
    """
    ...


def deprecate_patch(patch_id: str) -> None:
    """Mark a patch as deprecated (all old workflows completed).

    Call this after all workflows using the old code path have completed.
    Can be removed entirely after the retention period.

    Args:
        patch_id: The patch ID to deprecate.
    """
    ...


@dataclass
class PatchInfo:
    """Information about a patch marker in workflow history."""

    patch_id: str
    created_at: datetime
    deprecated_at: datetime | None = None
```

---

## Checkpointer Protocol

### Core Interface

```python
from typing import Protocol, Iterator
from contextlib import AbstractAsyncContextManager


class Checkpointer(Protocol):
    """Persistence interface for durable execution.

    Implementations must provide all methods for full durability.
    For testing/development, use MemoryCheckpointer.
    """

    # ---- Configuration ----

    @property
    def durability_mode(self) -> DurabilityMode:
        """Current durability mode."""
        ...

    # ===============================================
    # Step Lifecycle
    # ===============================================

    async def snapshot_step(
        self,
        run_id: str,
        step: StepSnapshot,
    ) -> None:
        """Create a step snapshot (status='created').

        Called before step execution begins.

        Args:
            run_id: Run identifier.
            step: Step snapshot to save.
        """
        ...

    async def snapshot_step_if_new(
        self,
        run_id: str,
        step: StepSnapshot,
    ) -> bool:
        """Atomically create snapshot if step_id doesn't exist.

        Used for idempotent step creation in distributed scenarios.

        Args:
            run_id: Run identifier.
            step: Step snapshot to save.

        Returns:
            True if created, False if already exists.
        """
        ...

    def atomic_step_completion(
        self,
        run_id: str,
        step_id: str,
    ) -> AbstractAsyncContextManager["AtomicStepContext"]:
        """Context manager for atomic step execution with exactly-once semantics.

        This is the core mechanism for exactly-once step completion. The context
        manager ensures that step outputs and completion status are saved atomically
        in a single database transaction.

        **Exactly-once guarantee:**
        - If step already completed, returns cached outputs (no re-execution)
        - If step executes, outputs + status=completed saved atomically
        - Crash between execution and commit = step will retry on recovery

        On enter:
            - Checks if step already completed (returns cached if so)
            - Sets status to 'running'
            - Records started_at

        On exit (success):
            - Commits outputs + status=completed atomically
            - Records completed_at and duration

        On exit (exception):
            - Records attempt as failed
            - May retry based on RetryPolicy

        Args:
            run_id: Run identifier.
            step_id: Step identifier.

        Returns:
            Async context manager yielding AtomicStepContext.

        Raises:
            LookupError: If step_id not found.
            StepAlreadyRunningError: If step claimed by another worker.

        Example:
            async with checkpointer.atomic_step_completion(run_id, step_id) as step:
                if step.cached:
                    return step.outputs  # Already completed, skip execution

                result = await node.execute(inputs)
                step.set_outputs(result)
                # On exit: outputs + status saved atomically
        """
        ...


@dataclass
class AtomicStepContext:
    """Context for atomic step completion.

    Provides access to cached outputs (if step already completed)
    and methods to set outputs for atomic commit.
    """

    step_id: str
    cached: bool
    """True if step already completed (outputs available, no execution needed)."""

    outputs: dict[str, Any] | None
    """Cached outputs if step already completed, None otherwise."""

    def set_outputs(self, outputs: dict[str, Any], decision: str | None = None) -> None:
        """Set outputs to be committed atomically on context exit.

        Args:
            outputs: Step output values.
            decision: Routing decision (for gates).

        Must be called before context exit if cached=False.
        """
        ...

    async def record_attempt(
        self,
        run_id: str,
        step_id: str,
        attempt: StepAttempt,
    ) -> None:
        """Record a retry attempt.

        Called after each attempt (success or failure).

        Args:
            run_id: Run identifier.
            step_id: Step identifier.
            attempt: Attempt record to append.
        """
        ...

    async def record_outputs(
        self,
        run_id: str,
        step_id: str,
        outputs: dict[str, Any],
        decision: str | None = None,
    ) -> None:
        """Record step outputs after execution.

        Called within record_execution context before exiting.

        Args:
            run_id: Run identifier.
            step_id: Step identifier.
            outputs: Step output values.
            decision: Routing decision (for gates).
        """
        ...

    # ===============================================
    # Parallel/Batch Handling
    # ===============================================

    async def put_pending_write(
        self,
        run_id: str,
        batch_index: int,
        write: PendingWrite,
    ) -> None:
        """Save completed step within in-progress batch.

        Called immediately when each parallel step completes,
        before the entire batch finishes.

        Args:
            run_id: Run identifier.
            batch_index: Current batch index.
            write: Completed step record.
        """
        ...

    async def get_pending_writes(
        self,
        run_id: str,
        batch_index: int,
    ) -> list[PendingWrite]:
        """Get pending writes for batch resumption.

        Used when resuming to find which parallel steps completed.

        Args:
            run_id: Run identifier.
            batch_index: Batch index to query.

        Returns:
            List of pending writes for this batch.
        """
        ...

    async def promote_pending_writes(
        self,
        run_id: str,
        batch_index: int,
    ) -> None:
        """Promote pending writes to committed.

        Called when entire batch completes successfully.
        Converts pending writes into regular step snapshots.

        Args:
            run_id: Run identifier.
            batch_index: Batch index to promote.
        """
        ...

    # ===============================================
    # Streaming Persistence
    # ===============================================

    async def append_stream_chunks(
        self,
        run_id: str,
        step_id: str,
        output_name: str,
        chunks: list[str],
        total_count: int,
    ) -> None:
        """Append streaming chunks (incremental persistence).

        Called periodically during streaming based on policy.

        Args:
            run_id: Run identifier.
            step_id: Step identifier.
            output_name: Name of the streaming output.
            chunks: New chunks to append.
            total_count: Total chunk count after appending.
        """
        ...

    async def complete_stream(
        self,
        run_id: str,
        step_id: str,
        output_name: str,
        final_value: str,
    ) -> None:
        """Mark stream as complete.

        Called when streaming finishes successfully.

        Args:
            run_id: Run identifier.
            step_id: Step identifier.
            output_name: Name of the streaming output.
            final_value: Complete accumulated value.
        """
        ...

    async def load_partial_stream(
        self,
        run_id: str,
        step_id: str,
        output_name: str,
    ) -> StreamingState | None:
        """Load partial stream for resumption.

        Args:
            run_id: Run identifier.
            step_id: Step identifier.
            output_name: Name of the streaming output.

        Returns:
            Streaming state if exists, None otherwise.
        """
        ...

    # ===============================================
    # Checkpoint Lifecycle
    # ===============================================

    async def save_checkpoint(
        self,
        checkpoint: Checkpoint,
    ) -> str:
        """Save a full checkpoint.

        Called at batch boundaries, interrupts, and completion.

        Args:
            checkpoint: Checkpoint to save.

        Returns:
            Checkpoint ID.
        """
        ...

    async def snapshot_end(
        self,
        run_id: str,
        status: Literal["completed", "error"],
        outputs: dict[str, Any],
        error: str | None = None,
    ) -> None:
        """Record run completion.

        Called when workflow finishes (success or failure).

        Args:
            run_id: Run identifier.
            status: Final status.
            outputs: Final output values.
            error: Error message if failed.
        """
        ...

    # ===============================================
    # Query Operations
    # ===============================================

    async def load_step(
        self,
        run_id: str,
        step_id: str,
    ) -> StepSnapshot | None:
        """Load a specific step snapshot.

        Args:
            run_id: Run identifier.
            step_id: Step identifier.

        Returns:
            Step snapshot if exists, None otherwise.
        """
        ...

    async def load_all_steps(
        self,
        run_id: str,
    ) -> list[StepSnapshot]:
        """Load all steps for a run.

        Steps are returned in execution order.

        Args:
            run_id: Run identifier.

        Returns:
            List of step snapshots.
        """
        ...

    async def load_checkpoint(
        self,
        workflow_id: str,
        checkpoint_id: str | None = None,
    ) -> Checkpoint | None:
        """Load a checkpoint.

        If checkpoint_id is None, returns most recent for workflow.

        Args:
            workflow_id: Workflow identifier.
            checkpoint_id: Specific checkpoint ID, or None for latest.

        Returns:
            Checkpoint if found, None otherwise.
        """
        ...

    async def list_checkpoints(
        self,
        workflow_id: str,
        limit: int | None = None,
    ) -> list[CheckpointMetadata]:
        """List checkpoints for a workflow.

        Returns checkpoints in reverse chronological order (newest first).

        Args:
            workflow_id: Workflow identifier.
            limit: Maximum number to return.

        Returns:
            List of checkpoint metadata.
        """
        ...

    # ===============================================
    # Distributed Coordination
    # ===============================================

    async def claim_next(
        self,
        run_id: str,
    ) -> StepSnapshot | None:
        """Atomically claim next 'created' step.

        Used for distributed/multi-worker execution.
        Sets status to 'pending' atomically.

        Args:
            run_id: Run identifier.

        Returns:
            Claimed step snapshot, or None if none available.
        """
        ...

    # ===============================================
    # Time Travel
    # ===============================================

    async def get_history(
        self,
        workflow_id: str,
        limit: int | None = None,
    ) -> list[Checkpoint]:
        """Get checkpoint history for workflow.

        Returns full checkpoints (not just metadata) for time travel.

        Args:
            workflow_id: Workflow identifier.
            limit: Maximum checkpoints to return.

        Returns:
            List of checkpoints (newest first).
        """
        ...

    async def fork_from(
        self,
        checkpoint_id: str,
        new_workflow_id: str | None = None,
    ) -> Checkpoint:
        """Fork from an existing checkpoint.

        Creates a new checkpoint with copied state, enabling
        exploration of alternative execution paths.

        Args:
            checkpoint_id: Source checkpoint to fork from.
            new_workflow_id: New workflow ID, or auto-generated.

        Returns:
            New checkpoint ready for execution.
        """
        ...

    async def update_state(
        self,
        checkpoint_id: str,
        updates: dict[str, Any],
    ) -> Checkpoint:
        """Update state in a checkpoint.

        Creates a new checkpoint with modified values.
        Original checkpoint is not modified.

        Args:
            checkpoint_id: Source checkpoint.
            updates: Values to update/add.

        Returns:
            New checkpoint with updated state.
        """
        ...

    # ===============================================
    # Nested Graph Support
    # ===============================================

    async def register_nested_run(
        self,
        parent_run_id: str,
        nested_path: str,
        nested_run_id: str,
    ) -> None:
        """Register a nested graph execution.

        Called when a GraphNode begins execution.

        Args:
            parent_run_id: Parent run identifier.
            nested_path: Path to nested graph (e.g., "rag:1").
            nested_run_id: Run ID for nested execution.
        """
        ...

    async def load_nested_run(
        self,
        parent_run_id: str,
        nested_path: str,
    ) -> str | None:
        """Get run ID for a nested graph.

        Args:
            parent_run_id: Parent run identifier.
            nested_path: Path to nested graph.

        Returns:
            Nested run ID if exists, None otherwise.
        """
        ...

    async def list_nested_runs(
        self,
        parent_run_id: str,
    ) -> list[tuple[str, str]]:
        """List all nested runs under a parent.

        Args:
            parent_run_id: Parent run identifier.

        Returns:
            List of (nested_path, nested_run_id) tuples.
        """
        ...
```

---

## Built-in Implementations

### MemoryCheckpointer

```python
@dataclass
class MemoryCheckpointer:
    """In-memory checkpointer for testing/development.

    State is lost on restart. Use for unit tests
    or development only.

    Attributes:
        durability_mode: Always "sync" for memory.
        deep_copy: If True, snapshots are deep copied.
    """

    durability_mode: DurabilityMode = "sync"
    deep_copy: bool = True

    # Internal storage
    _steps: dict[str, dict[str, StepSnapshot]] = field(default_factory=dict)
    _checkpoints: dict[str, list[Checkpoint]] = field(default_factory=dict)
    _pending_writes: dict[str, dict[int, list[PendingWrite]]] = field(default_factory=dict)
    _streams: dict[str, dict[str, StreamingState]] = field(default_factory=dict)
```

### FileCheckpointer

```python
@dataclass
class FileCheckpointer:
    """JSON file-based checkpointer.

    Each workflow gets its own JSON file.
    Simple and portable, suitable for prototypes.

    Attributes:
        directory: Directory for checkpoint files.
        durability_mode: Persistence timing.
    """

    directory: Path
    durability_mode: DurabilityMode = "sync"

    def _file_for_workflow(self, workflow_id: str) -> Path:
        """Get file path for a workflow."""
        return self.directory / f"{workflow_id}.json"
```

### SQLiteCheckpointer

```python
class SQLiteCheckpointer:
    """SQLite-based checkpointer.

    Production-ready for single-server deployments.
    All data in one file, no external dependencies.

    Tables:
        - workflows(workflow_id, created_at, metadata)
        - runs(run_id, workflow_id, parent_run_id, status)
        - steps(step_id, run_id, batch_index, status, outputs, ...)
        - checkpoints(checkpoint_id, workflow_id, run_id, state, ...)
        - pending_writes(run_id, batch_index, step_id, outputs)
        - streams(run_id, step_id, output_name, chunks, ...)
    """

    def __init__(
        self,
        path: str | Path,
        durability_mode: DurabilityMode = "sync",
    ):
        """
        Args:
            path: Path to SQLite database file.
            durability_mode: Persistence timing.
        """
        ...
```

### PostgresCheckpointer

```python
class PostgresCheckpointer:
    """PostgreSQL-based checkpointer.

    Production-ready for multi-server deployments.
    Supports concurrent access from multiple workers.

    Same schema as SQLiteCheckpointer.
    """

    def __init__(
        self,
        connection_string: str,
        durability_mode: DurabilityMode = "sync",
        pool_size: int = 5,
    ):
        """
        Args:
            connection_string: PostgreSQL connection URL.
            durability_mode: Persistence timing.
            pool_size: Connection pool size.
        """
        ...
```

---

## Runner Integration

### AsyncRunner with Checkpointer

```python
class AsyncRunner:
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        checkpointer: Checkpointer | None = None,
        durability_mode: DurabilityMode = "async",
        stream_persistence: StreamPersistenceMode = StreamPersistenceMode.IMMEDIATE,
        workflow_reuse_policy: WorkflowReusePolicy = WorkflowReusePolicy.RETURN_EXISTING,
        serializer: Serializer | None = None,
        payload_limits: PayloadLimits | None = None,
        lease_config: LeaseConfig | None = None,
        event_processors: list[EventProcessor] | None = None,
    ):
        """
        Args:
            cache: Cache backend for node outputs.
            checkpointer: Persistence backend for durable execution.
            durability_mode: When to persist (overrides checkpointer default).
            stream_persistence: When to persist streaming chunks (default: IMMEDIATE).
            workflow_reuse_policy: How to handle duplicate workflow IDs (default: RETURN_EXISTING).
            serializer: Custom serializer for payloads (default: DefaultSerializer).
            payload_limits: Size constraints for payloads (default: 2MB per payload).
            lease_config: Configuration for distributed step locking (multi-worker only).
            event_processors: Observability processors.
        """
        ...

    async def run(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        *,
        workflow_id: str | None = None,
        run_id: str | None = None,
        checkpoint: Checkpoint | bytes | str | None = None,
        resume: bool = False,
        # ... other params
    ) -> RunResult:
        """
        Execute graph with optional checkpointing.

        Resume options (in priority order):
            1. checkpoint=<Checkpoint>: Resume from specific object
            2. checkpoint=<bytes>: Resume from serialized checkpoint
            3. checkpoint=<str>: Load checkpoint by ID
            4. resume=True + workflow_id: Load latest checkpoint

        Args:
            graph: Graph to execute.
            inputs: Input values (merged with checkpoint state on resume).
            workflow_id: Durable workflow identifier.
            run_id: Specific run ID (auto-generated if not provided).
            checkpoint: Checkpoint to resume from.
            resume: If True, load latest checkpoint for workflow_id.

        Returns:
            RunResult with outputs and checkpoint info.
        """
        ...

    async def time_travel(
        self,
        graph: Graph,
        *,
        workflow_id: str,
        target_step: str,
        inputs: dict[str, Any] | None = None,
        new_workflow_id: str | None = None,
    ) -> RunResult:
        """
        Re-execute workflow from a specific step.

        Inspired by Mastra's time-travel feature. Loads snapshot data,
        reconstructs state up to target_step, then executes from there.

        Args:
            graph: Graph to execute.
            workflow_id: Source workflow to time-travel from.
            target_step: Step ID to start from (e.g., "process:2" or "rag:1/generate:0").
            inputs: Override inputs for the target step.
            new_workflow_id: Create as new workflow (default: fork from original).

        Returns:
            RunResult from continued execution.

        Use cases:
            - Failed step recovery: Re-run with corrected inputs
            - Transient failure retry: Retry after network/rate-limit issues
            - Testing: Execute specific step with custom data

        Example:
            # Retry failed step with different parameters
            result = await runner.time_travel(
                graph,
                workflow_id="order-12345",
                target_step="call_api:3",
                inputs={"timeout": 60},  # Longer timeout
            )

            # Time travel into nested graph
            result = await runner.time_travel(
                graph,
                workflow_id="order-12345",
                target_step="rag_pipeline:1/generate:0",
                inputs={"temperature": 0.9},
            )
        """
        ...
```

---

## Exceptions

```python
class PersistenceError(Exception):
    """Base exception for persistence errors."""
    pass


class CheckpointNotFoundError(PersistenceError):
    """Checkpoint doesn't exist."""

    def __init__(self, checkpoint_id: str):
        self.checkpoint_id = checkpoint_id
        super().__init__(f"Checkpoint not found: {checkpoint_id}")


class WorkflowNotFoundError(PersistenceError):
    """Workflow doesn't exist."""

    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id
        super().__init__(f"Workflow not found: {workflow_id}")


class StepAlreadyRunningError(PersistenceError):
    """Step is already being executed."""

    def __init__(self, step_id: str, current_status: str):
        self.step_id = step_id
        self.current_status = current_status
        super().__init__(
            f"Step '{step_id}' has status '{current_status}', "
            f"expected 'created' or 'pending'"
        )


class GraphVersionMismatchError(PersistenceError):
    """Graph definition changed since checkpoint."""

    def __init__(self, checkpoint_hash: str, current_hash: str):
        self.checkpoint_hash = checkpoint_hash
        self.current_hash = current_hash
        super().__init__(
            f"Graph definition changed since checkpoint.\n"
            f"Checkpoint hash: {checkpoint_hash}\n"
            f"Current hash: {current_hash}\n"
            f"Use fork_from() to create a new workflow from this checkpoint."
        )


class MaxRetriesExceededError(PersistenceError):
    """Step exceeded maximum retry attempts."""

    def __init__(self, step_id: str, max_attempts: int, last_error: str):
        self.step_id = step_id
        self.max_attempts = max_attempts
        self.last_error = last_error
        super().__init__(
            f"Step '{step_id}' failed after {max_attempts} attempts.\n"
            f"Last error: {last_error}"
        )


class WorkflowAlreadyExistsError(PersistenceError):
    """Workflow with this ID already exists (reuse policy violation)."""

    def __init__(self, workflow_id: str, existing_status: str, policy: str):
        self.workflow_id = workflow_id
        self.existing_status = existing_status
        self.policy = policy
        super().__init__(
            f"Workflow '{workflow_id}' already exists with status '{existing_status}'.\n"
            f"Current reuse policy: {policy}"
        )


class PayloadTooLargeError(PersistenceError):
    """Payload exceeds size limit."""

    def __init__(self, size: int, limit: int, context: str = ""):
        self.size = size
        self.limit = limit
        self.context = context
        super().__init__(
            f"{context + ' ' if context else ''}Payload size ({size:,} bytes) "
            f"exceeds limit ({limit:,} bytes).\n"
            f"Consider storing large data externally and passing a reference."
        )


class SerializationError(PersistenceError):
    """Value cannot be serialized."""

    def __init__(self, value_type: str, reason: str):
        self.value_type = value_type
        self.reason = reason
        super().__init__(
            f"Cannot serialize value of type '{value_type}': {reason}\n"
            f"Register a custom codec with Serializer.register()."
        )


class DeserializationError(PersistenceError):
    """Data cannot be deserialized."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Cannot deserialize data: {reason}")


class LeaseExpiredError(PersistenceError):
    """Lease expired before renewal."""

    def __init__(self, step_id: str, expired_at: str):
        self.step_id = step_id
        self.expired_at = expired_at
        super().__init__(
            f"Lease on step '{step_id}' expired at {expired_at}.\n"
            f"Step may have been claimed by another worker."
        )


class LeaseNotOwnedError(PersistenceError):
    """Attempted to renew/release a lease owned by another worker."""

    def __init__(self, step_id: str, owner_worker_id: str, requesting_worker_id: str):
        self.step_id = step_id
        self.owner_worker_id = owner_worker_id
        self.requesting_worker_id = requesting_worker_id
        super().__init__(
            f"Step '{step_id}' is leased by worker '{owner_worker_id}', "
            f"not '{requesting_worker_id}'."
        )


class NonDeterministicReplayError(PersistenceError):
    """Workflow replay produced different execution path than history."""

    def __init__(self, expected_step: str, actual_step: str, history_index: int):
        self.expected_step = expected_step
        self.actual_step = actual_step
        self.history_index = history_index
        super().__init__(
            f"Non-deterministic replay at history index {history_index}.\n"
            f"Expected step: {expected_step}\n"
            f"Actual step: {actual_step}\n"
            f"Workflow code may have changed. Use patched() for compatible changes."
        )
```

---

## Complete Example

```python
from hypergraph import Graph, AsyncRunner, node, route, InterruptNode
from hypergraph.persistence import SQLiteCheckpointer, RetryPolicy

# Define nodes
@node(
    outputs="data",
    retry=RetryPolicy(max_attempts=3, retryable_exceptions=(ConnectionError,))
)
async def fetch_data(url: str) -> dict:
    return await httpx.get(url).json()

@node(outputs="processed")
async def process(data: dict) -> dict:
    return transform(data)

@node(outputs="approval_prompt")
def create_prompt(processed: dict) -> str:
    return f"Approve this: {processed}"

approval = InterruptNode(
    name="approval",
    input_param="approval_prompt",
    response_param="user_decision",
)

@route(targets=["finalize", "revise"])
def check_approval(user_decision: str) -> str:
    return "finalize" if user_decision == "approve" else "revise"

@node(outputs="result")
def finalize(processed: dict) -> dict:
    return {"status": "approved", "data": processed}

@node(outputs="processed")
def revise(processed: dict, user_decision: str) -> dict:
    return apply_feedback(processed, user_decision)

# Build graph
graph = Graph(nodes=[
    fetch_data, process, create_prompt, approval,
    check_approval, finalize, revise,
])

# Create runner with persistence
checkpointer = SQLiteCheckpointer("./workflows.db")
runner = AsyncRunner(checkpointer=checkpointer)

# First execution
result = await runner.run(
    graph,
    inputs={"url": "https://api.example.com/data"},
    workflow_id="order-12345",
)

if result.interrupted:
    print(f"Waiting for approval: {result.interrupt_value}")
    # App closes, user comes back later...

# Resume with user's decision
result = await runner.run(
    graph,
    inputs={"user_decision": "approve"},
    workflow_id="order-12345",
    resume=True,
)

print(result.outputs["result"])
# {"status": "approved", "data": {...}}

# Time travel: inspect history
history = await checkpointer.get_history("order-12345")
for cp in history:
    print(f"{cp.checkpoint_id}: batch {cp.batch_index}, status {cp.status}")

# Fork and try different path
forked = await checkpointer.fork_from(
    history[1].checkpoint_id,  # Go back to before approval
    new_workflow_id="order-12345-alternate",
)

alt_result = await runner.run(
    graph,
    inputs={"user_decision": "reject"},
    checkpoint=forked,
)
```

---

**See also:**
- [Persistence & Durable Execution](../architecture/persistence.md) — Conceptual guide
- [Execution Types](./execution-types.md) — Runtime events and state
- [Runners](./runners.md) — Runner API and configuration
