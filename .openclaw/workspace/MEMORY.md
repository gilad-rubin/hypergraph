# Zoe — Long-Term Memory

This file stores durable facts about the hypergraph project that should persist
across sessions. Update this file whenever you learn something important about
the project, its users, or its direction.

---

## Project Identity

**hypergraph** is a hierarchical and modular graph workflow framework for AI & ML,
written in Python. It is maintained as a solo open-source project.

- **Repository:** https://github.com/gilad-rubin/hypergraph
- **Language:** Python (3.10–3.13)
- **Package manager:** `uv` (use `uv run` for all commands)
- **Test runner:** `pytest` via `uv run pytest`
- **Linter/formatter:** `ruff` (auto-runs on every file edit via pre-commit hook)
- **CI:** GitHub Actions (lint + test matrix on push/PR to master)
- **Branch protection:** `master` requires PR review + all CI checks

---

## Architecture Overview

The framework has three main layers:

1. **Graph construction** (`src/hypergraph/graph/`) — `Graph()`, `GraphNode`, node types
2. **Execution** (`src/hypergraph/runners/`) — `SyncRunner`, `AsyncRunner`, shared utilities
3. **Visualization** (`src/hypergraph/viz/`) — HTML + Mermaid rendering via React/ReactFlow

Key design principles (from `dev/CORE-BELIEFS.md`):
- Build-time validation: `Graph()` catches structural errors at construction, not runtime
- Zero-code-change sync/async parity
- Flat-code style: prefer composition over deep inheritance

---

## Agent Team

| Agent | Model | Role |
|---|---|---|
| Zoe (me) | claude-opus-4-6 | Orchestrator — business context, delegation, retries |
| Claude-Planner | claude-sonnet-4-5 | Creates detailed TDD-first implementation plans |
| Codex-Agent | gpt-5.3-codex-high | Implements code — fewest bugs, best correctness |
| Gemini-Reviewer | gemini-2.5-pro | Deep code review — security, scalability, quality |

---

## Active Skills

| Skill | Trigger | Purpose |
|---|---|---|
| `/orchestrate-feature` | New feature request | Full plan → implement → review → PR pipeline |
| `/ship-feature` | After plan is ready | Delegates implementation + review to agents |
| `/run-review` | After implementation | Triggers Gemini deep review on a PR diff |
| `/address-bugs` | After CI/review failures | Analyzes failures and delegates fixes to Codex |
| `/morning-standup` | Daily / on demand | Summarizes open PRs, CI status, Sentry issues |

---

## Workflow Notes

- Plans are stored in `plans/` at the repo root (numbered: `00001-feature-name.md`)
- Each coding task gets its own git worktree + branch: `feature/00001-feature-name`
- PRs are not considered "done" until: CI passes + Gemini review approved + screenshots for UI changes
- Retry logic: on failure, read CI logs + review comments, then rewrite the prompt with failure context
- Notification: send Telegram message when a PR is ready for human review

---

## Things to Remember

<!-- Add project-specific notes here as you learn them. Examples:
- "The viz tests are slow — use `uv run pytest tests/viz/ -x` for quick iteration"
- "Customer @X requested feature Y on YYYY-MM-DD"
- "Avoid changing the public API of Graph() without a deprecation plan"
-->
