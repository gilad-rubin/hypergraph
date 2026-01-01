# LangGraph Durable Execution & Checkpointing Reference

> LangGraph v1.0 (October 2025) - Reference for HyperNodes design

## Overview

Durable execution is one of the four core runtime features in LangGraph 1.0. It ensures that if an agent is mid-workflow when the server restarts, it picks up exactly where it left off. No lost work. No starting over.

---

## Core Concepts

### What Durable Execution Provides

1. **Fault tolerance:** Resume from last successful step on failure
2. **Human-in-the-loop:** Pause indefinitely for human input
3. **Memory:** Maintain conversation state across interactions
4. **Time travel:** Navigate and debug execution history
5. **Long-running workflows:** Agents that run for hours or days

### How It Works

```
Graph Execution → Checkpointer → Storage Backend
                      ↓
              Checkpoint (StateSnapshot)
                      ↓
        Thread (sequence of checkpoints)
```

Each "super-step" (complete node execution) creates a checkpoint.

---

## Checkpointer Interface

All checkpointers implement `BaseCheckpointSaver`:

```python
class BaseCheckpointSaver(ABC):
    @abstractmethod
    def get_tuple(self, config: dict) -> CheckpointTuple | None:
        """Retrieve checkpoint by config."""

    @abstractmethod
    def put(
        self,
        config: dict,
        checkpoint: dict,
        metadata: dict,
        new_versions: dict
    ) -> dict:
        """Persist checkpoint, return new config."""

    @abstractmethod
    def put_writes(
        self,
        config: dict,
        writes: list[tuple[str, Any]],
        task_id: str,
        task_path: tuple[str, ...]
    ) -> None:
        """Store intermediate writes (pending outputs)."""

    @abstractmethod
    def list(
        self,
        config: dict,
        *,
        filter: dict | None = None,
        before: dict | None = None,
        limit: int | None = None
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints with filtering."""
```

---

## Checkpoint Structure

### CheckpointTuple

```python
@dataclass
class CheckpointTuple:
    config: dict                    # Thread + checkpoint IDs
    checkpoint: dict                # Actual state data
    metadata: CheckpointMetadata    # Execution metadata
    parent_config: dict | None      # Link to parent (for history)
    pending_writes: list | None     # Incomplete writes
```

### Checkpoint Data

```python
checkpoint = {
    "v": 1,                          # Schema version
    "id": "checkpoint-uuid",         # Unique ID (UUID v6, time-ordered)
    "ts": "2025-01-15T10:30:00Z",    # Timestamp
    "channel_values": {              # State values
        "messages": [...],
        "user_input": "...",
    },
    "channel_versions": {            # Version tracking
        "messages": "00000003.abc123",
        "user_input": "00000001.def456",
    },
    "versions_seen": {               # What each node has seen
        "generate": {"messages": "00000002.xyz789"},
    },
    "pending_sends": [],             # Queued messages
}
```

### CheckpointMetadata

```python
@dataclass
class CheckpointMetadata:
    source: Literal["input", "loop", "update", "fork"]
    step: int                        # -1 for initial, 0+ for execution
    writes: dict[str, Any]           # What this step wrote
    parents: dict[str, str]          # Parent checkpoints by namespace
    # Plus custom user fields
```

---

## Checkpointer Implementations

### 1. InMemorySaver (Development)

```python
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()
graph = workflow.compile(checkpointer=checkpointer)
```

**Characteristics:**
- Fast, no external dependencies
- Non-persistent (lost on restart)
- Good for testing and development

**Warning:** Never use in production.

### 2. SqliteSaver (Local Persistence)

```python
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3

conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
checkpointer = SqliteSaver(conn)
graph = workflow.compile(checkpointer=checkpointer)
```

**Async version:**

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import aiosqlite

async with aiosqlite.connect("checkpoints.db") as conn:
    checkpointer = AsyncSqliteSaver(conn)
    graph = workflow.compile(checkpointer=checkpointer)
```

**Install:** `pip install langgraph-checkpoint-sqlite`

### 3. PostgresSaver (Production)

```python
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row

conn = Connection.connect(
    "postgresql://user:pass@localhost/db",
    autocommit=True,      # Required!
    row_factory=dict_row  # Required!
)
checkpointer = PostgresSaver(conn)
graph = workflow.compile(checkpointer=checkpointer)
```

**Async version:**

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection

async with await AsyncConnection.connect(
    "postgresql://user:pass@localhost/db",
    autocommit=True,
    row_factory=dict_row
) as conn:
    checkpointer = AsyncPostgresSaver(conn)
```

**Install:** `pip install langgraph-checkpoint-postgres`

**Database schema (3 tables):**
- `checkpoints`: Metadata + inline JSONB values
- `checkpoint_blobs`: Large serialized values
- `checkpoint_writes`: Pending writes

---

## Using Checkpointers

### Basic Usage

```python
from langgraph.checkpoint.memory import InMemorySaver

# Compile with checkpointer
checkpointer = InMemorySaver()
graph = workflow.compile(checkpointer=checkpointer)

# Execute with thread_id
config = {"configurable": {"thread_id": "conversation-1"}}
result = graph.invoke({"messages": [user_message]}, config)

# Continue same conversation
result = graph.invoke({"messages": [followup_message]}, config)
# ^ Automatically loads previous state
```

### Resume After Failure

```python
# Original execution fails mid-way
try:
    result = graph.invoke(inputs, config)
except Exception:
    pass  # Node 3 of 5 failed

# Resume from last successful checkpoint
result = graph.invoke(None, config)  # Pass None to use checkpoint state
# ^ Starts from node 3, doesn't re-run nodes 1-2
```

### Resume After Interrupt

```python
# Execution pauses at interrupt
result = graph.invoke(inputs, config)
if result.get("__interrupt__"):
    # Get user input
    user_response = await get_user_input()

    # Resume with response
    result = graph.invoke(
        {"user_decision": user_response},
        config
    )
```

---

## Pending Writes (Fault Tolerance)

When a node fails mid-execution:

```python
# Execution flow:
# Node A completes → writes saved
# Node B completes → writes saved
# Node C fails → execution stops

# On resume:
# - Node A writes: already saved, not re-run
# - Node B writes: already saved, not re-run
# - Node C: re-executed
```

**Key feature:** Successful nodes' outputs are preserved even when sibling nodes fail.

```python
# Inspect pending writes
state = graph.get_state(config)
for task in state.tasks:
    if task.state == "pending":
        print(f"Pending: {task.name}")
    if task.error:
        print(f"Failed: {task.name} - {task.error}")
```

---

## Serialization

### Default: JsonPlusSerializer

Handles:
- JSON primitives
- LangChain messages
- Pydantic models
- Dataclasses
- Custom types via `__reduce__`

### Pickle Fallback

For complex types (Pandas DataFrames, etc.):

```python
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

checkpointer = InMemorySaver(
    serde=JsonPlusSerializer(pickle_fallback=True)
)
```

### Encryption

For sensitive data:

```python
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

serde = EncryptedSerializer.from_pycryptodome_aes()
checkpointer = SqliteSaver(conn, serde=serde)
```

---

## Thread and Namespace Organization

### Threads

Group related checkpoints:

```python
# Same thread = same conversation
config_1 = {"configurable": {"thread_id": "user-123-conv-1"}}
config_2 = {"configurable": {"thread_id": "user-123-conv-1"}}

# Different thread = independent conversation
config_3 = {"configurable": {"thread_id": "user-123-conv-2"}}
```

### Checkpoint Namespace (Subgraphs)

Prevents naming conflicts in nested graphs:

```python
# Parent graph: checkpoint_ns = ""
# Child graph: checkpoint_ns = "rag_pipeline"
# Grandchild: checkpoint_ns = "rag_pipeline:retriever"
```

---

## Memory Store (Cross-Thread)

For data shared across conversations:

```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()

# Compile with both checkpointer and store
graph = workflow.compile(
    checkpointer=checkpointer,
    store=store
)

# In node: access store
def my_node(state, config, *, store):
    user_id = config["configurable"]["user_id"]
    namespace = (user_id, "preferences")

    # Save
    store.put(namespace, "theme", {"value": "dark"})

    # Retrieve
    prefs = store.search(namespace)
```

**Checkpointer vs Store:**
- Checkpointer: Within-thread state (short-term memory)
- Store: Cross-thread data (long-term memory)

---

## LangGraph API (Managed)

When using LangGraph Cloud/API:

```python
# No checkpointer configuration needed
# Infrastructure handles persistence automatically
from langgraph_sdk import get_client

client = get_client()
thread = await client.threads.create()
result = await client.runs.create(thread["thread_id"], assistant_id, input={...})
```

---

## Implications for HyperNodes

### Current HyperNodes approach:
- `RunResult.checkpoint` for interrupt resume
- Checkpoint is opaque `bytes`
- No built-in persistence backends

### What to adopt:

1. **Checkpointer interface:**
```python
class BaseCheckpointer(ABC):
    @abstractmethod
    def get(self, config: CheckpointConfig) -> Checkpoint | None: ...

    @abstractmethod
    def put(self, config: CheckpointConfig, checkpoint: Checkpoint) -> None: ...

    @abstractmethod
    def list(self, thread_id: str) -> Iterator[Checkpoint]: ...
```

2. **Built-in implementations:**
```python
# Development
runner = AsyncRunner(checkpointer=MemoryCheckpointer())

# Production
runner = AsyncRunner(checkpointer=PostgresCheckpointer(conn))
```

3. **Thread concept:**
```python
result = runner.run(
    graph,
    inputs={...},
    config={"thread_id": "conversation-123"}
)
```

4. **Pending writes for fault tolerance:**
```python
# On partial failure, preserve completed node outputs
# Resume skips already-completed nodes
```

5. **Serialization options:**
```python
checkpointer = PostgresCheckpointer(
    conn,
    serializer=EncryptedSerializer(key=...)
)
```

---

## Sources

- [LangGraph Persistence Documentation](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Persistence Concepts (GitHub)](https://github.com/langchain-ai/langgraph/blob/main/docs/docs/concepts/persistence.md)
- [Checkpointing Architecture (DeepWiki)](https://deepwiki.com/langchain-ai/langgraph/4.1-checkpointing)
- [PostgresSaver for LangGraph](https://medium.com/@mehta.harshita31/never-lose-ai-memory-in-production-postgressaver-for-langgraph-2f165c3688a0)
- [LangGraph 1.0 Release Notes](https://medium.com/@romerorico.hugo/langgraph-1-0-released-no-breaking-changes-all-the-hard-won-lessons-8939d500ca7c)
