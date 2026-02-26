---
name: ship-feature
description: |
  Given an existing plan file, delegates implementation to Codex-Agent and
  review to Gemini-Reviewer, then notifies when the PR is ready. Use this
  when a plan already exists (e.g., you wrote it manually or `/orchestrate-feature`
  already ran Phase 1).
user_invocable: true
model: opus
---

# Ship Feature — Implement + Review + PR

Use this skill when a plan already exists in `plans/` and you want to drive
the implementation through to a ready-to-merge PR.

## Usage

```
/ship-feature plans/00001-add-async-caching.md
```

Or just: `/ship-feature 00001` (Zoe will find the matching plan file).

---

## Workflow

### 1. Load the Plan

Read the specified plan file. Extract:
- `plan_number` and `feature_name` from the filename
- The branch name: `feature/{plan_number}-{feature_name}`
- The list of implementation stages

If the branch doesn't exist yet, create it:
```bash
git checkout -b feature/{plan_number}-{feature_name} origin/master
```

### 2. Delegate to Codex-Agent

Spawn the `coder` agent:

```
Task tool:
  name: "coder"
  subagent_type: "coder"
  prompt: |
    You are the Codex-Agent for the hypergraph project.

    Implement the feature described in this plan: `{plan_path}`

    Follow TDD strictly:
    - Write failing tests FIRST for each stage
    - Confirm they fail, then implement the fix
    - Confirm they pass, then commit and move to the next stage

    Commands:
    - Run tests: `uv run pytest -x -q`
    - Commit: `git add -A && git commit -m "feat({scope}): {description}"`
    - Push: `git push -u origin {branch_name}`
    - Create PR: `gh pr create --title "{feature_name}" --fill`

    When done, output: "IMPLEMENTATION DONE: PR #{pr_number}"
```

### 3. Monitor & Retry

Check on the coder every few minutes. If it fails:

1. Read CI logs: `gh run view --log-failed`
2. Read review comments: `gh api repos/gilad-rubin/hypergraph/pulls/{pr}/comments`
3. Rewrite the prompt with the failure context and respawn.
4. Maximum 3 retries before escalating to the user.

### 4. Delegate to Gemini-Reviewer

Once CI is green, spawn the `reviewer` agent:

```
Task tool:
  name: "reviewer"
  subagent_type: "reviewer"
  prompt: |
    Review PR #{pr_number} for the hypergraph project.

    Plan: {plan_path}
    Diff: run `gh pr diff {pr_number}`

    Check against `.openclaw/skills/quality-criteria/references/quality-criteria.md`.
    Post your review as a PR comment.
    Output: "APPROVED" or "ISSUES: {list}"
```

If issues found, pass them back to the coder and repeat (max 3 cycles).

### 5. Notify

When approved:
```bash
openclaw message send --channel telegram \
  --message "✅ PR #{pr_number} ready: $(gh pr view {pr_number} --json url -q .url)"
```

Report to user: "PR ready for your review."
