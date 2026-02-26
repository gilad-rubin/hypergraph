# Hypergraph — OpenClaw Dev Team Setup

This directory contains the configuration for a **one-person AI dev team** built
on [OpenClaw](https://openclaw.ai). The setup implements the architecture described
in the article *"The One-Person Dev Team: How AI Agent Swarms Are Redefining Solo
Entrepreneurship"* and adapts it specifically for the `hypergraph` Python library.

---

## The Architecture

The setup follows a **two-tier model**: a high-level orchestrator that holds
business context and delegates to specialist agents that hold code context.

```
You (human)
    │
    ▼
Zoe (Orchestrator) ── claude-opus-4-6
    │   Holds: project memory, architectural decisions, retry logic
    │
    ├──▶ Claude-Planner ── claude-sonnet-4-5
    │       Creates TDD-first implementation plans
    │
    ├──▶ Codex-Agent ── gpt-5.3-codex-high
    │       Writes code, runs tests, commits, opens PRs
    │
    └──▶ Gemini-Reviewer ── gemini-2.5-pro
            Deep code review: security, scalability, quality
```

The orchestrator never writes code. It plans, delegates, monitors, and retries
intelligently when things go wrong.

---

## Directory Structure

```
.openclaw/
├── README.md               ← You are here
├── openclaw.json           ← Main agent + routing configuration
├── .env.example            ← Environment variable template
├── workspace/              ← Orchestrator's persistent workspace
│   ├── MEMORY.md           ← Long-term project memory
│   └── memory/             ← Daily append-only logs
├── skills/                 ← Agent skills (workflows)
│   ├── orchestrate-feature/  ← Full plan→implement→review→PR pipeline
│   ├── ship-feature/         ← Implement + review when plan already exists
│   ├── run-review/           ← Deep Gemini code review on any PR
│   ├── address-bugs/         ← Intelligent CI/review failure recovery
│   ├── morning-standup/      ← Daily status report
│   └── quality-criteria/     ← Shared quality checklist (not user-invocable)
└── scripts/
    ├── setup.sh            ← First-time setup script
    └── cron-monitor.sh     ← CI monitoring + Telegram notifications
```

---

## Quick Start

### 1. Prerequisites

- Node.js ≥22
- GitHub CLI (`gh`) authenticated
- `uv` (Python package manager)

### 2. Run Setup

```bash
.openclaw/scripts/setup.sh
```

The setup script will:
- Install OpenClaw globally
- Create `.openclaw/.env` from the template
- Set up the workspace directory
- Run `openclaw doctor`

### 3. Fill in API Keys

Edit `.openclaw/.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...   # Zoe + Claude-Planner (required)
OPENAI_API_KEY=sk-...          # Codex-Agent (required)
GEMINI_API_KEY=...             # Gemini-Reviewer (required)
TELEGRAM_BOT_TOKEN=...         # Notifications (optional)
TELEGRAM_CHAT_ID=...           # Notifications (optional)
```

### 4. Start the Gateway

```bash
openclaw gateway --port 18789 --verbose
```

### 5. Talk to Zoe

```bash
# Morning standup
openclaw agent --message "/morning-standup"

# Ship a new feature
openclaw agent --message "/orchestrate-feature: Add LRU caching to the sync runner"

# Review an open PR
openclaw agent --message "/run-review 42"

# Fix CI failures on a PR
openclaw agent --message "/address-bugs 42"
```

---

## Skills Reference

| Skill | Trigger | What It Does |
|---|---|---|
| `/orchestrate-feature` | New feature request | Full pipeline: plan → TDD implement → review → PR |
| `/ship-feature <plan>` | Plan already exists | Delegates implement + review to agents |
| `/run-review [pr]` | After implementation | Deep Gemini review, posts as PR comment |
| `/address-bugs [pr]` | CI failure or review issues | Reads failure context, delegates targeted fixes |
| `/morning-standup` | Daily / on demand | Open PRs, CI status, autobuild issues |

---

## GitHub Actions Integration

The workflow `.github/workflows/openclaw-agent.yml` triggers the orchestrator
automatically when an issue is labeled `autobuild`.

**Setup:**
1. Go to **Settings → Secrets and variables → Actions**
2. Add: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`
3. Optionally add: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**Usage:**
1. Create a GitHub issue describing the feature
2. Add the `autobuild` label
3. The agent will plan, implement, and open a PR automatically
4. Review and merge the PR

---

## Daily Cron (Optional)

To receive a daily standup report every weekday morning:

```bash
crontab -e
# Add:
0 9 * * 1-5 /path/to/repo/.openclaw/scripts/cron-monitor.sh --standup
```

---

## How It Differs from the `.claude/` Setup

The existing `.claude/skills/` directory uses a **team simulation** model where
Claude Code spawns multiple sub-agents that communicate via `SendMessage` and a
shared `TaskList`. This works well for Claude Code's native multi-agent support.

The `.openclaw/` setup uses OpenClaw's **agent binding** model instead:
- Each agent has a fixed role and model, configured in `openclaw.json`
- The orchestrator spawns agents via the `sessions_spawn` tool
- Communication is direct (orchestrator reads agent output) rather than via a
  shared message bus
- The orchestrator holds long-term memory in `MEMORY.md`, persisting across sessions

Both setups coexist in the repository. Use `.claude/` for Claude Code workflows
and `.openclaw/` for the OpenClaw gateway.

---

## Troubleshooting

**`openclaw doctor` reports errors:**
Run `openclaw doctor` and follow the remediation steps. Common issues:
- Missing API keys in `.env`
- Gateway not running (`openclaw gateway --port 18789`)
- Node.js version too old (need ≥22)

**Agent gets stuck:**
Kill the session and retry with a more focused prompt. Check `MEMORY.md` for
any notes about known issues.

**CI keeps failing:**
Run `/address-bugs <pr_number>`. If it fails 3 times, the issue likely requires
human intervention — check the CI logs directly.

**Gemini-Reviewer not available:**
Gemini 2.5 Pro has a free tier with generous limits. If you hit rate limits,
fall back to `anthropic/claude-sonnet-4-5` for the reviewer role by editing
`openclaw.json`.
