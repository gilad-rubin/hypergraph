# External Integrations

**Analysis Date:** 2026-01-16

## APIs & External Services

**None Required**

The hypergraph core library has no required external API dependencies. It is a pure Python workflow orchestration framework that operates entirely locally.

**Optional Integrations (via extras):**

| Integration | Package | Purpose |
|-------------|---------|---------|
| Modal | `modal >= 0.64.0` | Serverless function execution |
| Logfire | `logfire >= 2.0.0` | Observability/telemetry |

## Data Storage

**Databases:**
- None required by core library
- User applications connect their own storage

**File Storage:**
- Local filesystem only for core operations
- No cloud storage dependencies

**Caching:**
- None built-in
- User applications implement their own caching

## Authentication & Identity

**Auth Provider:**
- None required
- Library does not handle authentication

**Notes:**
- Modal integration (optional) uses Modal's auth system
- Logfire integration (optional) uses Logfire's auth system

## Monitoring & Observability

**Error Tracking:**
- None built-in
- Optional: Logfire integration via `telemetry` extra

**Logs:**
- Standard Python logging
- Optional: Rich console output via `telemetry` extra
- Optional: Logfire structured logging via `telemetry` extra

**Progress Tracking:**
- Optional: tqdm via `telemetry` extra

## CI/CD & Deployment

**Hosting:**
- PyPI (target: `hypergraph-ai` package)

**CI Pipeline:**
- No `.github/` directory present
- No CI configuration detected

**Documentation:**
- GitBook hosting (`.gitbook.yaml` present)

## Environment Configuration

**Required env vars:**
- None for core functionality

**Optional env vars (for extras):**
- Modal: `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` (per Modal docs)
- Logfire: `LOGFIRE_TOKEN` (per Logfire docs)

**Secrets location:**
- Not applicable for core library
- User applications manage their own secrets

## Webhooks & Callbacks

**Incoming:**
- None

**Outgoing:**
- None

## Third-Party Library Integrations

**Graph Operations:**
- NetworkX - Used for all graph algorithms
  - Cycle detection: `nx.simple_cycles()`
  - DAG validation: `nx.is_directed_acyclic_graph()`
  - Graph traversal: `DiGraph` methods
  - Location: `src/hypergraph/graph.py`

**Data Processing (Optional):**
- PyArrow - Batch processing (`batch` extra)
- Daft - Dataframe operations (`daft` extra)
- Pandas - Data manipulation (`examples` extra)

**ML/AI (Examples Only):**
- colbert-ai - ColBERT retrieval
- model2vec - Embedding models
- pylate - Late interaction models
- pytrec-eval - IR evaluation
- rank-bm25 - BM25 ranking

These are in the `examples` extra only, not core dependencies.

## Standard Library Usage

**Core Dependencies (stdlib only):**
- `typing` - Type hints and annotations (`src/hypergraph/_typing.py`)
- `hashlib` - Definition hashing for caching
- `dataclasses` - Data structures (`InputSpec`, etc.)
- `inspect` - Function introspection
- `warnings` - Type resolution warnings
- `abc` - Abstract base classes
- `copy` - Shallow graph copying

## Integration Patterns

**User Integration Points:**

1. **Node Functions** - Users provide pure functions that hypergraph orchestrates
2. **Graph Composition** - Graphs can nest as nodes for hierarchical workflows
3. **Runners** (coming soon) - Will provide sync/async execution interfaces

**No Framework Lock-in:**
- Functions are testable without hypergraph
- No state schemas or special decorators required beyond `@node`
- Pure Python functions with type hints

---

*Integration audit: 2026-01-16*
