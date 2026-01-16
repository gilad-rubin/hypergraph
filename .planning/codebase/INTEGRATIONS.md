# External Integrations

**Analysis Date:** 2026-01-16

## APIs & External Services

**Current Status:** No external service integrations in production code.

The codebase is a pure workflow framework with no external API dependencies. All external integrations are:
1. Optional dependencies for specific use cases
2. Planned/designed but not yet implemented

## Planned Integrations (from specs)

**Observability:**
- OpenTelemetry - Designed for trace export (see `specs/reviewed/observability.md`)
- Logfire - Optional telemetry dependency (`logfire >= 2.0.0`)
- Event stream architecture ready for OTel span export

**Durable Execution:**
- DBOS - Planned `DBOSAsyncRunner` for automatic crash recovery (see `specs/reviewed/durable-execution.md`)
- SQLite - Planned `SqliteCheckpointer` for local persistence (see `specs/reviewed/checkpointer.md`)

**Distributed Execution:**
- Daft - Optional distributed data processing (`daft >= 0.6.11`)
- Modal - Optional serverless execution (`modal >= 0.64.0`)

## Data Storage

**Databases:**
- None currently implemented
- Planned: SQLite via `SqliteCheckpointer` (designed, not implemented)
- Planned: Generic `Checkpointer` interface for pluggable backends

**File Storage:**
- Local filesystem only
- Planned: Artifact storage tier for large values (blobs, embeddings)

**Caching:**
- Node-level `cache` flag exists on `FunctionNode`
- No caching implementation yet

## Authentication & Identity

**Auth Provider:**
- None - Library has no authentication requirements
- User applications handle their own auth

## Monitoring & Observability

**Error Tracking:**
- None built-in
- Exception propagation through standard Python mechanisms

**Logs:**
- No logging framework
- `warnings.warn()` for user-facing warnings (e.g., missing output_name)

**Designed (not implemented):**
- Event stream with `NodeStartEvent`, `NodeCompleteEvent`, `StreamingChunkEvent`
- `EventProcessor` interface for push-based consumption
- `.iter()` method for pull-based event access

## CI/CD & Deployment

**Hosting:**
- Not deployed - design phase library

**CI Pipeline:**
- No `.github/` workflows configured
- Tests run locally via `uv run pytest`

**Documentation:**
- GitBook integration configured (`.gitbook.yaml`)
- Docs in `docs/` directory

## Environment Configuration

**Required env vars:**
- None for core functionality

**Optional env vars:**
- Would be needed by user applications (e.g., `OPENAI_API_KEY` for LLM nodes)
- Framework itself has no env var requirements

**Secrets location:**
- Not applicable - no secrets management

## Webhooks & Callbacks

**Incoming:**
- None

**Outgoing:**
- None

**Designed:**
- `InterruptNode` for human-in-the-loop patterns (pauses execution, not webhooks)

## External Libraries (Optional Dependencies)

**ML/AI (examples group):**
- `colbert-ai >= 0.2.22` - Neural retrieval
- `model2vec >= 0.7.0` - Embedding models
- `pylate >= 1.0.0` - Late interaction models
- `rank-bm25 >= 0.2.2` - BM25 ranking

**Data Processing:**
- `pandas >= 1.3.0` - DataFrame operations
- `numpy >= 1.21.0` - Numerical computing
- `pyarrow >= 14.0.0` - Columnar data format
- `daft >= 0.6.11` - Distributed DataFrame

**Validation:**
- `pydantic >= 2.12.3` - Data validation (examples only)

## Integration Architecture

The framework is designed for integration via:

1. **Pure Node Functions** - User wraps any API call in a `@node` decorated function
2. **Runner Selection** - Choose execution backend (sync, async, Daft, DBOS)
3. **Checkpointer Plugin** - Implement `Checkpointer` interface for custom storage
4. **EventProcessor Plugin** - Implement `EventProcessor` for custom observability

**Example Integration Pattern (designed):**
```python
from hypergraph import node, Graph

@node(output_name="response")
async def call_llm(prompt: str) -> str:
    # User code handles API integration
    return await openai_client.chat(prompt)

@node(output_name="embedding")
async def embed_text(text: str) -> list[float]:
    # User code handles API integration
    return await embedding_service.embed(text)

graph = Graph([call_llm, embed_text])
```

---

*Integration audit: 2026-01-16*
