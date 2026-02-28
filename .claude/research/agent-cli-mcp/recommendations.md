# Recommendations: Making Hypergraph Agent-Native

> Based on research into [incur](incur.md) (CLI framework), [Hatchet](hatchet.md) (workflow platform), and [LangSmith](langsmith.md) (trace querying)
> Date: 2026-02-28

## Goal

Enable agents to autonomously build, run, debug, fix, and profile hypergraph graphs and workflows.

## Current State

Hypergraph already has:
- **CLI** (`hypergraph run`, `hypergraph map`, `hypergraph graph ls/inspect`) via Click + Rich
- **pyproject.toml registry** (`[tool.hypergraph]`) for graph discovery
- **AGENTS.md / CLAUDE.md** convention throughout the repo
- **RunLog / MapLog** with progressive disclosure (`.log` → `.steps[i].log`)
- **JSON-serializable traces** (`to_dict()` on all log types)
- **Event system** (processors for OTel, Rich progress, custom)

## Recommendations

### Tier 1: High Impact, Low Effort

#### 1.1 Dual-Mode CLI Output

**From**: Hatchet's `-o json` pattern
**Effort**: Small — add `--output` flag to existing Click commands

Every CLI command should support:
- Default: Rich tables/panels for humans (already done)
- `--output json`: Raw JSON envelope for agents
- `--output agent`: Token-optimized summary (name + status + next steps)

The `agent` format is where incur's token-cost thinking meets Hatchet's dual-mode:
```
pipeline | completed | 3 nodes | 12ms
→ hypergraph run pipeline --input '{"x": 2}' to re-run
→ hypergraph runs logs <id> for trace
```

**Priority**: High — this is the single change that makes the CLI agent-usable.

#### 1.2 CTAs (Call-to-Actions) in CLI Output

**From**: incur's typed next-step suggestions
**Effort**: Small — append guidance lines to command output

After every command, suggest the logical next step:
```
# After `hypergraph run pipeline`
→ hypergraph runs logs <run-id> for execution trace
→ hypergraph run pipeline --input '{"x": 3}' to re-run with different input

# After `hypergraph graph ls`
→ hypergraph graph inspect <name> for input spec and node details

# After a failed run
→ hypergraph runs logs <run-id> --failed for error details
→ hypergraph run pipeline --input '{"x": 1}' to retry
```

In `--output agent` mode, CTAs become the primary output — agents get exactly the commands they need.

#### 1.3 Agent Skills Package

**From**: Both incur and Hatchet have this
**Effort**: Medium — create Markdown reference docs + installer command

```bash
hypergraph skills install [--dir .] [--agents claude-code,cursor]
```

Generates:
```
skills/hypergraph/
  AGENTS.md          # Skill index
  CLAUDE.md          # Symlink → AGENTS.md
  SKILL.md           # Frontmatter metadata
  references/
    build-graph.md   # How to construct graphs (decorators, wiring, validation)
    run-graph.md     # Running + interpreting results
    debug-graph.md   # .log drill-down, error diagnosis, common fixes
    map-graph.md     # Batch execution, MapLog, cross-item analysis
    profile-graph.md # Finding bottlenecks via node_stats
    inspect-graph.md # CLI inspection, input spec, visualization
```

**Key insight from incur**: The skill file frontmatter (name + description + command) is what agents see at session start. Full docs are loaded on-demand via `--help`. This progressive disclosure pattern keeps session start cheap.

### Tier 2: Medium Impact, Medium Effort

#### 2.1 MCP Documentation Server

**From**: Hatchet's `https://docs.hatchet.run/api/mcp`
**Effort**: Medium — HTTP server exposing docs as MCP tools

Two tools:
- `search_docs(query)` → relevant doc sections
- `get_full_docs(topic)` → complete page content

Installation:
```bash
hypergraph docs install claude-code
# → claude mcp add --transport http hypergraph-docs http://localhost:PORT/mcp
```

**Why**: Agents in Claude Code, Cursor, etc. get hypergraph documentation without needing skill files pre-installed. The MCP server can also serve dynamic content (graph registry, input specs) that static skill files can't.

#### 2.2 Progressive Discovery in CLI

**From**: incur's frontmatter → help → full schema pattern
**Effort**: Small-Medium — restructure existing `graph ls` and `graph inspect`

Three levels:
1. `hypergraph graph ls` → names + one-line descriptions (cheap)
2. `hypergraph graph inspect <name>` → input spec, node list, edges (medium)
3. `hypergraph graph inspect <name> --full` → full schema, defaults, type info (expensive)

The agent only pays for the detail it needs. Currently `graph ls` and `graph inspect` exist but don't have this graduated detail.

#### 2.3 Run Log Streaming

**From**: Hatchet's `runs logs <id> -f` follow mode
**Effort**: Medium — requires event streaming to CLI

For long-running workflows (especially with checkpointing):
```bash
hypergraph runs logs <workflow-id> -f  # follow mode
hypergraph runs logs <workflow-id> --tail 10
```

This matters most for async workflows with the checkpointer — agents need to monitor without polling.

### Tier 3: Lower Priority / Future

#### 3.1 Token-Optimized Output Format

**From**: incur's TOON format
**Assessment**: Interesting but premature for hypergraph

TOON saves 30-60% tokens on tabular data. But hypergraph's CLI output is mostly small (graph names, run status, node stats) — the token savings are marginal. Worth revisiting when output gets larger (e.g., streaming logs, large map results).

#### 3.2 Runtime MCP Server

**From**: GJakobi/hatchet-mcp community server
**Assessment**: Only relevant with checkpointer/persistent workflows

Tools like `list_runs`, `get_run_status`, `search_runs` make sense when hypergraph has a persistent run store. The checkpointer already has this data — exposing it via MCP would be the natural extension.

#### 3.3 Token Cost Benchmarking

**From**: incur's measured 3.1x improvement
**Assessment**: Adopt as a design metric, not a feature

Measure: How many tokens does an agent spend to go from "I need to run a graph" to "I have the result and understand what happened"? Track this across CLI changes to ensure agent experience improves.

## Summary: Recommended Order

| # | What | Effort | Impact |
|---|------|--------|--------|
| 1 | Dual-mode CLI output (`--output json/agent`) | Small | High |
| 2 | CTAs in CLI output | Small | High |
| 3 | Agent skills package (`skills install`) | Medium | High |
| 4 | Progressive discovery (graduated `graph inspect`) | Small | Medium |
| 5 | MCP docs server | Medium | Medium |
| 6 | Run log streaming (`-f` follow) | Medium | Medium |
| 7 | Token cost benchmarking (metric, not feature) | Small | Low |
| 8 | Runtime MCP server (after checkpointer matures) | Large | Future |

## Pattern Comparison

| Dimension | incur | Hatchet | Hypergraph (current) | Hypergraph (proposed) |
|-----------|-------|---------|---------------------|----------------------|
| Discovery | Skill frontmatter → --help → schema | skills install + MCP docs | AGENTS.md + graph ls/inspect | Skills package + MCP docs + progressive inspect |
| Output modes | TOON / JSON / YAML / Markdown | TUI / JSON | Rich tables only | Rich / JSON / Agent |
| Next steps | Typed CTAs in response | Skill docs teach patterns | None | CTAs in every command |
| Agent auth | N/A (local) | Profile system | N/A (local) | N/A (local) |
| Token cost | 3.1x measured improvement | Not measured | Not measured | Track as metric |
| MCP | stdio (command tree → tools) | HTTP docs server | None | HTTP docs server |
| Trace access | None | REST API + CLI | .log in Python | .log in Python + CLI |
