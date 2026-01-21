# External Integrations

**Analysis Date:** 2026-01-21

## APIs & External Services

**Not Detected:**
The core hypergraph framework has no built-in external API integrations. Users can integrate with external services via custom node functions.

## Data Storage

**Databases:**
- Not integrated at framework level
- Users implement database access in custom nodes
- Example support: Pydantic models for validation, pandas for data operations (available as optional dependencies)

**File Storage:**
- Local filesystem only (no cloud storage SDK integrated)
- Examples show local file operations
- Users can integrate S3/cloud storage in custom nodes

**Caching:**
- Optional: `diskcache>=5.6.3` (available in examples dependencies)
- No built-in caching layer (framework is stateless between runs)
- Graph definition hashing available for cache key generation via `definition_hash` in `src/hypergraph/graph/core.py`

## Authentication & Identity

**Not Applicable:**
- Framework has no built-in authentication
- Users implement auth in custom nodes
- No account management or identity provider integration

## Monitoring & Observability

**Structured Logging:**
- Optional integration: `logfire>=2.0.0` (part of telemetry group)
- Location: Not integrated into core, available for user implementation
- Environment variable pattern: Standard (users configure via env vars when using logfire)

**Progress Tracking:**
- Optional: `tqdm>=4.67.1` (part of telemetry group)
- Optional: `rich>=13.0.0` (part of telemetry group)
- Used for: User-level progress display, not framework-integrated telemetry

**Error Tracking:**
- Not built-in; framework exposes exceptions:
  - `GraphConfigError` - Build-time validation failures
  - `MissingInputError` - Runtime input validation
  - `InfiniteLoopError` - Cycle detection limits
  - `IncompatibleRunnerError` - Runner capability mismatch
  - All in `src/hypergraph/exceptions.py`

## CI/CD & Deployment

**Hosting Targets (Optional):**
- Modal (`modal>=0.64.0` + `cloudpickle>=3.0.0`) - Serverless compute
  - For: Running graphs on Modal's serverless infrastructure
  - Requires: cloudpickle for function serialization

**Distributed Execution (Optional):**
- Daft (`daft>=0.6.11`) - Ray-based distributed computing
  - For: Batch processing across distributed workers
  - Handles: Data distribution and task scheduling

**CI Pipeline:**
- Not declared in pyproject.toml
- Test suite configured for: pytest with xdist parallelization
- Likely: GitHub Actions (based on repository at github.com/gilad-rubin/hypergraph)

## Environment Configuration

**Required env vars:**
- None at framework level
- Optional: Users configure telemetry/monitoring via env vars when using optional packages

**Secrets location:**
- Not applicable - framework has no built-in secrets management
- Users handle secrets in custom nodes (environment variables, secret managers, etc.)

## Webhooks & Callbacks

**Incoming:**
- Not applicable - hypergraph is not a server framework

**Outgoing:**
- Not built-in
- Users implement via custom nodes (requests, httpx, etc.)

## Serialization & Interop

**Function Serialization:**
- Optional: `cloudpickle>=3.0.0` - For serializing functions when using Modal
- Standard: pickle-compatible (core functions are pickleable)
- No JSON serialization of graph definitions built-in

**Data Format Support:**
- Arrow: Optional via `pyarrow>=14.0.0` (batch operations)
- DataFrames: Optional via `pandas>=1.3.0` (examples)
- Type hints: Native support via Python type annotations
- No Protocol Buffer or Avro integration

## Visualization Integration

**Jupyter Environment:**
- Framework: `ipywidgets>=8.1.7` - Interactive widget framework
- Kernel: `ipykernel>=6.31.0` - Jupyter kernel support
- Notebooks: `notebook>=7.5.0` - Jupyter server
- Format: `nbformat>=5.10.4` - Notebook I/O

**Browser Technologies:**
- React Flow (embedded in generated HTML) - Graph visualization
- Layout Engine: Constraint-based layout (custom JavaScript)
- Rendering: SVG/Canvas (browser-native)
- No external JavaScript CDN dependencies detected in HTML generation

**Display:**
- Output: HTML widget rendered in iframe (sandboxed)
- Theme support: Auto/dark/light (CSS-based)
- Save format: Standalone HTML files or Jupyter widget display

## Data Validation

**Type Checking:**
- Type hints: Native Python typing module
- Compatibility checking: Custom type system in `src/hypergraph/_typing.py`
- Validation framework: Optional `pydantic>=2.12.3` (available for user nodes)

## Special Integrations (Optional)

**Distributed Batch Processing Pipeline:**
- Framework: Daft (Ray-based)
- Use case: `runner.map()` for large-scale batch operations
- Configuration: Automatic via daft when installed

**Serverless Execution:**
- Framework: Modal
- Use case: Running graphs on Modal's serverless infrastructure
- Implementation: Requires cloudpickle for function serialization
- Location: Custom runners would integrate at `src/hypergraph/runners/` level

---

*Integration audit: 2026-01-21*
