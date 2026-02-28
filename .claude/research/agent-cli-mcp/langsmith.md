# LangSmith — Trace Querying & MCP

> Researched: 2026-02-28
> Repo: https://github.com/langchain-ai/langsmith-sdk
> MCP: https://github.com/langchain-ai/langsmith-mcp-server

## What It Is

LangSmith is LangChain's hosted observability platform. Its trace querying system is the most mature agent-facing trace interface in the ecosystem.

## Data Model

Single entity: **Run**. A "trace" is just a tree of Runs sharing a `trace_id`, with one root run (`is_root=True`). No separate trace/span tables.

### Run Fields (key ones)

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | Unique run ID |
| `name` | str | Human-readable name |
| `run_type` | str | `"llm"`, `"chain"`, `"tool"`, `"retriever"`, etc. |
| `inputs` | dict | Input key-value pairs |
| `outputs` | dict | Output key-value pairs |
| `start_time` / `end_time` | datetime | Timestamps |
| `error` | str | Error message or null |
| `status` | str | `"success"`, `"error"` |
| `tags` | list[str] | User-defined labels |
| `metadata` | dict | Arbitrary key-value pairs |
| `trace_id` | UUID | Root of this trace tree |
| `parent_run_id` | UUID | Immediate parent |
| `dotted_order` | str | `{time}{uuid}.*` — sortable execution order |
| `total_tokens` | int | Token count |
| `total_cost` | Decimal | Cost estimate |
| `first_token_time` | datetime | TTFT |

## Query Language: FQL (Functional Query Language)

Custom, not SQL. Functional expressions passed as strings to `filter=`:

```python
# Operators
eq(run_type, "llm")
neq(error, null)
gt(total_tokens, 5000)
search(name, "extractor")
contains(inputs.question, "langsmith")
has(tags, "experimental")

# Composable
and(eq(run_type, "chain"), gt(latency, 10), gt(total_tokens, 5000))
or(has(tags, "beta"), gt(latency, 5))
```

### Dot notation for nested fields

`inputs.question`, `outputs.answer`, `metadata.user_id` — navigates into JSON dicts.

### Three filter scopes

- **`filter`** — applies to individual runs
- **`trace_filter`** — applies to root run of each trace
- **`tree_filter`** — applies to any run in the trace tree

These combine: "find all `extractor` runs from traces that received positive feedback."

### `select` projection

Limits which fields are returned — critical for performance:
```python
runs = client.list_runs(
    project_name="my-project",
    select=["id", "name", "inputs", "outputs", "error"]
)
```

## MCP Server

Publicly hosted at `https://langsmith-mcp-server.onrender.com/mcp`.

### Trace-related tools

| Tool | What it does |
|------|-------------|
| `fetch_runs` | Full-featured run query with FQL, paginated by character budget |
| `list_projects` | List projects/sessions |
| `get_thread_history` | Paginated message history for a conversation |

### Character-budget pagination

Pages capped at 25K-30K chars (not item count). Long strings truncated with `… (+N chars)`. Purpose-built for LLM context windows — you never blow up the agent's context.

## SDK Interface (Python)

```python
from langsmith import Client
client = Client()

# All runs in a project
runs = client.list_runs(project_name="my-project")

# Root traces only
traces = client.list_runs(project_name="my-project", is_root=True)

# By trace ID
spans = client.list_runs(trace_id="<uuid>")

# Aggregate stats
stats = client.get_run_stats(project_names=["my-project"], filter='eq(run_type, "llm")')
```

## No CLI for querying

The `langsmith` CLI handles server management only (start/stop, API keys). No query/filter CLI — all programmatic access through SDK or REST.

## Relevance to Hypergraph

### Patterns Worth Adopting

| Pattern | Application |
|---------|-------------|
| Single entity model (Run = everything) | `runs` table covers all levels |
| `parent_run_id` for nesting | Already in our proposed schema |
| Character-budget output | CLI `--max-chars` for agent-friendly pagination |
| `select` projection | CLI `--fields id,name,error` to limit output |
| Tags / metadata | `config` JSON on runs table |

### Not Applicable

- FQL is a custom query language — we use CLI commands instead of exposing raw queries
- Hosted SaaS model — hypergraph is local-first
- `dotted_order` — our `step_index` serves the same purpose more simply
- REST API — we expose CLI, not HTTP endpoints

### Key Insight

LangSmith requires agents to learn FQL syntax. Our approach (CLI as the query interface) is simpler: agents already know how to run commands and read `--help`. The DB is an implementation detail the agent never sees.
