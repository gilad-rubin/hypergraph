---
name: morning-standup
description: |
  Daily standup report: summarizes open PRs, CI status, recent commits,
  and any issues that need human attention. Run every morning or on demand.
  Also scans for proactive work opportunities (failing tests, stale PRs, etc.).
user_invocable: true
model: sonnet
---

# Morning Standup — Daily Dev Status Report

Compile a concise status report and identify what needs attention today.

## Usage

```
/morning-standup
```

Or schedule it via cron (see `.openclaw/scripts/cron-monitor.sh`).

---

## Workflow

Run all data-gathering steps in parallel, then synthesize.

### 1. Gather Data

```bash
# Open PRs and their CI status
gh pr list --repo gilad-rubin/hypergraph --json number,title,headRefName,statusCheckRollup,reviewDecision,url \
  --jq '.[] | {number, title, branch: .headRefName, ci: .statusCheckRollup[0].state, review: .reviewDecision, url}'

# Recent commits on master (last 24h)
git log origin/master --since="24 hours ago" --oneline

# Failed CI runs (last 24h)
gh run list --repo gilad-rubin/hypergraph --status failure --limit 5 \
  --json databaseId,name,headBranch,createdAt,url

# Open issues labeled 'bug' or 'autobuild'
gh issue list --repo gilad-rubin/hypergraph --label "bug,autobuild" \
  --json number,title,labels,url

# Stale PRs (open > 3 days with no activity)
gh pr list --repo gilad-rubin/hypergraph --json number,title,updatedAt,url \
  --jq '[.[] | select((.updatedAt | fromdateiso8601) < (now - 259200))]'
```

### 2. Synthesize the Report

Write a concise Markdown report:

```markdown
# 🦞 Hypergraph Standup — {date}

## Open PRs
| PR | Status | CI | Review |
|---|---|---|---|
| #{number} {title} | {status} | {ci_status} | {review_decision} |

## Needs Attention
- [ ] {item requiring human action}

## Proactive Opportunities
- {stale PR, failing test, open bug that could be auto-fixed}

## Recent Activity (last 24h)
- {commit summary}
```

### 3. Identify Proactive Work

For each item that can be handled autonomously:

- **Open bug issue with `autobuild` label** → suggest running `/orchestrate-feature`
- **PR with CI failure** → suggest running `/address-bugs {pr_number}`
- **Stale PR (no activity > 3 days)** → suggest rebasing and re-running review
- **PR approved but not merged** → flag for human merge

### 4. Deliver the Report

Send the report via Telegram (if configured):
```bash
openclaw message send --channel telegram --message "{report}"
```

Also write it to `MEMORY.md` daily log:
```
memory/YYYY-MM-DD.md: ## Standup\n{summary}
```

---

## Scheduling

To run this automatically every morning at 9am, add to crontab:
```bash
# See .openclaw/scripts/cron-monitor.sh for the full monitoring script
0 9 * * 1-5 /path/to/.openclaw/scripts/cron-monitor.sh
```
