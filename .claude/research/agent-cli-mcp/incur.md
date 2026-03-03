# incur (wevm/incur) — Agent-Native CLI Framework

> Researched: 2026-02-28
> Repo: https://github.com/wevm/incur
> Language: TypeScript

## What It Is

A CLI framework that treats agents as first-class users. Not a workflow engine — it's the layer that makes CLIs cheap for LLMs to use.

## Core Design Philosophy

**Problem**: Traditional CLIs are expensive for AI agents. Every interaction burns tokens on schema discovery, verbose JSON responses, and context re-establishment. MCP servers solve discovery but front-load the cost (all tool schemas injected at session start).

**Thesis**: Make three things cheap simultaneously:
1. **Session start** — only load skill file frontmatter (names + descriptions), not full schemas
2. **Discovery** — load `--help` output for just the commands the agent needs, on demand
3. **Response** — default to TOON format (30-60% fewer tokens than JSON)

**Measured result**: 3.1x fewer tokens per session vs MCP or one-skill alternatives for a 20-command CLI.

## Architecture

### Minimal API Surface: Three Functions

| Function | Role |
|----------|------|
| `Cli.create(name, options)` | Factory — creates a CLI instance |
| `.command(name, def)` | Register — adds commands, mounts sub-CLIs as groups |
| `.serve()` | Execute — parses argv, resolves, validates, runs, formats |

### Command Tree

Commands form a tree stored in `Map<string, CommandEntry>`. Resolution walks tokens against the tree. Groups have sub-maps; leaves have handlers.

### Source Structure (~15 flat modules)

```
src/
  Cli.ts          # Core: create/command/serve
  Mcp.ts          # MCP stdio server: command tree → MCP tools
  Skill.ts        # SKILL.md generation: command tree → markdown
  SyncSkills.ts   # Skill installation + staleness detection
  SyncMcp.ts      # MCP server registration with agents
  Formatter.ts    # Output: TOON, JSON, YAML, Markdown, JSONL
  Help.ts         # Help text generation + --llms manifest
  Parser.ts       # Argv parsing against Zod schemas
  Schema.ts       # Zod → JSON Schema conversion
```

## Key Innovations

### 1. Skills System (Most Novel Part)

- `skills add` traverses command tree → generates `SKILL.md` files
- `depth` parameter controls granularity (monolithic vs per-group)
- Files deployed to canonical location (`~/.agents/skills/`) with symlinks to agent-specific dirs
- SHA256 hash for staleness detection — warns on CLI invocation if stale
- Supports 20 agents (Claude Code, Cursor, Cline, Windsurf, Codex, Gemini CLI, etc.)

**SKILL.md Format:**
```yaml
---
name: deploy
description: Deployment commands. Run `my-cli deploy --help` for usage.
command: my-cli deploy
---
```

### 2. TOON (Token Optimized Object Notation)

Default output format — 30-60% fewer tokens than JSON:
```
context:
  task: Our favorite hikes
friends[3]: ana,luis,sam
hikes[3]{id,name,distanceKm}:
  1,Blue Lake Trail,7.5
  2,Ridge Overlook,9.2
```

### 3. CTAs (Call-to-Actions)

Commands return typed "next step" suggestions:
```typescript
return ok({ items }, {
  cta: {
    commands: [
      { command: 'get 1', description: 'View item' },
      { command: 'list', args: { state: 'closed' }, description: 'View closed' },
    ],
  },
})
```

CTAs are type-inferred from the command tree — always valid.

### 4. MCP Integration

`--mcp` flag starts MCP stdio server. Command tree → MCP tools:
- Tool names: underscore-delimited paths (e.g., `deploy_start`)
- Zod schemas → JSON Schema `inputSchema`
- Standard MCP stdio transport, tools only (no resources/prompts)

### 5. Token Cost Benchmarking

| Phase | MCP | One Skill | incur Skills |
|-------|-----|-----------|--------------|
| Session start | 6,747 tok | 624 tok | 805 tok |
| Discovery | 0 tok | 11,489 tok | 387 tok |
| Response | ~2,188 tok | ~2,160 tok | ~1,158 tok |

## What It Doesn't Have

- No execution tracing/observability
- No graph/workflow concepts (flat command tree)
- No persistent state/checkpointing
- No parallel execution (no map, no fan-out)

## Relevance to Hypergraph

### Patterns Worth Adopting

| Pattern | Application |
|---------|-------------|
| Progressive disclosure (frontmatter → help → full schema) | Graph registry: list names → inspect one → full input spec |
| CTAs — typed next-step suggestions | After `run()`: suggest `.log`, `map()`. After errors: suggest fix |
| Agent-optimized output | CLI `--format agent` mode |
| Skill file generation from source of truth | Auto-generate from `pyproject.toml` registry |
| Multi-agent installation | Detect Claude Code, Cursor, etc. |
| Token cost as design metric | Benchmark agent sessions end-to-end |

### Not Applicable

- incur is a CLI framework, hypergraph is a workflow engine — different abstraction level
- incur's flat command tree vs hypergraph's DAG/cycle graphs
- incur has no execution state, traces, or observability
