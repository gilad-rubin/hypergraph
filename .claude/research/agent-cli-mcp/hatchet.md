# Hatchet (hatchet-dev/hatchet) — Workflow Orchestrator CLI + MCP

> Researched: 2026-02-28
> Repo: https://github.com/hatchet-dev/hatchet
> Language: Go (CLI), Python/TypeScript/Go (SDKs)

## What It Is

A workflow orchestration platform with a Go CLI that explicitly targets AI agents as users. The CLI has a dual-mode design (interactive TUI vs machine-readable JSON) and a first-class agent skills system.

## Core Design Philosophy

**Problem**: Workflow platforms have rich web UIs but poor programmatic interfaces. Agents need to trigger, monitor, debug, and replay workflows without a browser.

**Thesis**: Every CLI command should work in two modes:
1. **Human mode** (default) — full-screen Bubbletea TUI with interactive selection
2. **Agent mode** (`-o json`) — raw JSON to stdout, no interactivity

**Implementation**: Single `isJSONOutput(cmd)` gate in every command handler.

## Architecture

### CLI Framework: Cobra + Bubbletea + Huh

| Layer | Library | Role |
|-------|---------|------|
| Command tree | Cobra | Standard Go CLI routing |
| Interactive TUI | Bubbletea | Full-screen terminal UI (alt screen) |
| Forms/prompts | Huh | Profile creation, confirmation dialogs |
| Config | Viper | Profile storage, hatchet.yaml parsing |

### Command Tree

| Command | Subcommands | Notes |
|---------|-------------|-------|
| `runs` | `list`, `get`, `cancel`, `replay`, `logs`, `events`, `list-children` | Core run management |
| `workflows` | `list`, `get` | Workflow definitions |
| `worker` | `dev`, `list`, `get` | Worker process management |
| `trigger` | `[name]`, `manual` | Fire workflows from CLI or hatchet.yaml |
| `cron` | `list`, `get`, `create`, `enable`, `disable`, `delete` | Cron management |
| `scheduled` | `list`, `get`, `create`, `delete` | Scheduled runs |
| `rate-limits` | `list` | Rate limit inspection |
| `webhooks` | `list`, `get` | Webhook management |
| `profile` | `add`, `remove`, `list`, `show`, `update`, `set-default` | Auth profiles |
| `server` | `start`, `stop` | Local Docker dev server |
| `skills` | `install` | Agent skill installer |
| `docs` | `install cursor`, `install claude-code` | MCP docs server setup |
| `quickstart` | — | Project scaffolding (py/ts/go) |

### Global Flags

- `-p / --profile`: named connection profile (prompts interactively if omitted)
- `-o / --output json`: machine mode — skips TUI, prints raw JSON

## Key Innovations

### 1. Dual-Mode Output (Most Practical Pattern)

Every resource command has an identical gate:

```go
func isJSONOutput(cmd *cobra.Command) bool {
    output, _ := cmd.Flags().GetString("output")
    return strings.ToLower(output) == "json"
}
```

Human path: `hatchet runs list` → Bubbletea alt-screen TUI
Agent path: `hatchet runs list -o json` → `json.MarshalIndent` to stdout

This is not an afterthought — the agent skill docs explicitly teach the `-o json | jq` pattern.

### 2. Agent Skills System

First-class product feature for teaching AI agents to use the CLI:

```bash
hatchet skills install [--dir ./my-project] [--force]
```

Installs structured Markdown docs into `{dir}/skills/hatchet-cli/`:

```
skills/hatchet-cli/
  AGENTS.md              # Main skill index (Codex, auto-discovered)
  CLAUDE.md              # Symlink → AGENTS.md (Claude Code)
  SKILL.md               # Metadata + when-to-use index
  references/
    setup-cli.md         # Install, profile creation, verify connectivity
    start-worker.md      # hatchet.yaml structure, `hatchet worker dev`
    trigger-and-watch.md # Fire workflow, poll for completion
    debug-run.md         # runs get → events → logs → diagnostic cheat sheet
    replay-run.md        # Replay failed runs
```

Also appends a `<!-- hatchet-skills:start -->` section to the project's `AGENTS.md`.

After install, output prompts:
```
Run `hatchet docs install` to add the Hatchet MCP server to your AI editor
```

### 3. MCP Integration (Docs-Only)

Official HTTP MCP server for documentation queries:

```
https://docs.hatchet.run/api/mcp
```

Hardcoded as `const defaultMCPURL = "https://docs.hatchet.run/api/mcp"` in `docs.go`.

**Tools exposed:**
- `search_docs` — find documentation pages by query
- `get_full_docs` — retrieve comprehensive context

**Installation via CLI:**
```bash
# Cursor: writes .cursor/rules/hatchet-docs.mdc + prints deeplink
hatchet docs install cursor

# Claude Code: shells out to `claude mcp add --transport http hatchet-docs <url>`
hatchet docs install claude-code
```

Custom URL via `--url` flag for self-hosted docs.

### 4. Community MCP Server (GJakobi/hatchet-mcp)

A **runtime monitoring** MCP server (read-only), filling the gap the official docs server doesn't cover. Python, using `FastMCP`.

| Tool | Description |
|------|-------------|
| `list_workflows` | All registered workflows |
| `list_runs` | Runs with filters (workflow, status, since, limit) |
| `get_run_status` | Status of a specific run |
| `get_run_result` | Output of a completed run |
| `get_queue_metrics` | Job counts by status over 24h |
| `search_runs` | Search by metadata key-value |

Tiny repo (4 files), useful as a pattern rather than reference implementation.

### 5. Profile System

Profiles decouple auth from commands:

- Created from Hatchet API tokens (JWT containing server URL)
- Stored locally via Viper
- Contains: Token, ApiServerURL, GrpcHostPort, TenantId, TLSStrategy
- TLS auto-detected by probing the gRPC endpoint
- Every command takes `-p profile` instead of inline tokens

### 6. Worker Config (`hatchet.yaml`)

```yaml
triggers:
  - command: "python scripts/trigger_bulk.py"
    name: "bulk"
    description: "Bulk process jobs"

dev:
  preCmds:
    - "poetry install"
  runCmd: "poetry run python src/worker.py"
  files:
    - "**/*.py"
  reload: true
```

`triggers` are named shell commands — used by `hatchet trigger [name]`. The `dev` section is a file-watcher wrapper around worker startup.

### 7. Run Management (Full Detail)

**List** with rich filtering:
```bash
hatchet runs list -o json --since 24h --status FAILED,CANCELLED --workflow my-wf --limit 100
```

**Get** returns full run detail including tasks, events, outputs, errors.

**Logs** with follow mode:
```bash
hatchet runs logs <run-id> --tail 50 --since 5m -f  # polls every 2s
```
Auto-detects DAG vs single task — DAG fetches all task logs, merges + sorts by timestamp with `[task-name]` prefix.

**Events**: Lifecycle events — `eventType`, `message`, `taskDisplayName`, `timestamp`.

**Cancel/Replay**: Single (by UUID) or bulk (by filters). Interactive mode shows count + confirmation; `-y` or `-o json` skips confirmation.

## What It Doesn't Have

- No token-optimized output format (raw JSON only)
- No progressive disclosure (full schema always available)
- No CTAs / next-step suggestions in output
- Official MCP is docs-only — no runtime monitoring from first party
- No graph visualization from CLI

## Relevance to Hypergraph

### Patterns Worth Adopting

| Pattern | Application |
|---------|-------------|
| Dual-mode output (`-o json`) | `hypergraph run -o json` for agents, rich tables for humans |
| Agent skills with structured references | `hypergraph skills install` → AGENTS.md + reference docs |
| AGENTS.md + CLAUDE.md symlink convention | Already doing this — validate alignment |
| MCP docs server | Serve hypergraph docs via MCP for IDE integration |
| Profile system for auth | Not needed now (local-only), but pattern is clean |
| `hatchet.yaml` triggers | `pyproject.toml [tool.hypergraph]` registry serves same role |
| Bulk operations with filters | `hypergraph runs list --status failed --since 24h` |
| Follow-mode logs | `hypergraph runs logs <id> -f` for long-running workflows |

### Not Applicable

- Hatchet is a hosted platform with REST APIs — hypergraph is a local library
- Bubbletea TUI is Go-specific — Python equivalents (Rich, Textual) fill this role
- gRPC transport — hypergraph has no server component
- Profile/auth system — no multi-tenant concern
