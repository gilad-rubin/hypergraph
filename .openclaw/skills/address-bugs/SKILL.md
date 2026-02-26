---
name: address-bugs
description: |
  Analyzes CI failures and code review comments on the current PR, then
  delegates targeted fixes to Codex-Agent. This is the "intelligent retry"
  skill — it reads failure context before rewriting the prompt, unlike a
  simple loop that repeats the same prompt on failure.
user_invocable: true
model: opus
---

# Address Bugs — Intelligent Failure Recovery

Use this skill when a PR has CI failures or unresolved review comments.
It reads all failure context before delegating fixes, producing better
prompts than a blind retry.

## Usage

```
/address-bugs              # fixes issues on the current branch's PR
/address-bugs 42           # fixes issues on PR #42
```

---

## Workflow

### 1. Collect All Failure Context

Run these in parallel to gather the full picture:

```bash
# CI status and failed checks
gh pr checks {pr_number}

# Failed CI run logs (most recent run)
FAILED_RUN=$(gh run list --branch {branch} --limit 1 --json databaseId -q '.[0].databaseId')
gh run view $FAILED_RUN --log-failed 2>&1 | head -200

# All PR review comments (inline)
gh api repos/gilad-rubin/hypergraph/pulls/{pr_number}/comments \
  --jq '[.[] | {path: .path, line: .line, body: .body, user: .user.login}]'

# Top-level PR comments (bots often post here)
gh api repos/gilad-rubin/hypergraph/issues/{pr_number}/comments \
  --jq '[.[] | {body: .body, user: .user.login}]'

# Review summaries
gh api repos/gilad-rubin/hypergraph/pulls/{pr_number}/reviews \
  --jq '[.[] | {state: .state, body: .body, user: .user.login}]'
```

### 2. Triage the Issues

Classify each issue:

| Severity | Criteria | Action |
|---|---|---|
| **Critical** | Test failure, import error, syntax error | Fix immediately |
| **High** | Logic bug, missing validation, security issue | Fix in this pass |
| **Medium** | Code quality, naming, missing test | Fix if time allows |
| **Low** | Nit, formatting, minor suggestion | Skip (ruff handles formatting) |

Skip issues that are already resolved (outdated comments) or are false positives.

### 3. Delegate Fixes to Codex-Agent

For each Critical/High issue (or group of related issues), spawn the `coder`:

```
Task tool:
  name: "coder-fix-{issue_id}"
  subagent_type: "coder"
  prompt: |
    You are the Codex-Agent for the hypergraph project.

    Fix the following issue on branch `{branch_name}`:

    <issue>
    File: {file_path} (line {line_number})
    Reviewer: {reviewer}
    Comment: {comment_body}

    CI failure context (if applicable):
    {ci_log_excerpt}
    </issue>

    Instructions:
    1. Read the file and understand the context.
    2. Write a failing test that reproduces the issue (if testable).
    3. Apply the minimal fix.
    4. Run: `uv run pytest {relevant_test_file} -x`
    5. Confirm the test passes.
    6. Run the full suite: `uv run pytest -x -q`
    7. Commit: `git add -A && git commit -m "fix: {short description}"`
    8. Output: "FIXED: {description}"
```

Spawn independent fixes in parallel. Wait for all to complete.

### 4. Push and Re-check

After all fixes are committed:

```bash
# Merge latest master to avoid conflicts
git fetch origin master
git merge origin/master

# Push
git push
```

Wait 30 seconds for CI to trigger, then check status:
```bash
gh pr checks {pr_number} --watch
```

### 5. Re-run Review

After CI passes, trigger `/run-review {pr_number}` to confirm the review
comments are resolved.

### 6. Report

- If all issues resolved: notify user — "All issues fixed. PR #{pr_number} ready."
- If issues remain after 3 passes: report remaining issues to user for manual review.

---

## Guardrails

- **Never skip the failing test step** — always confirm the bug is reproducible
  before fixing it.
- **Never commit to master** — always on the feature branch.
- **Maximum 3 fix passes** per PR — if issues persist, escalate to user.
