# Review PR — Autonomous PR Pipeline

Fetch PR review comments from ALL sources, triage by severity, fix issues, and iterate until approved.

## Triggers
- `/review-pr` or `/review-pr <number>`
- "review this PR", "fix PR comments", "address PR feedback"

## Workflow

### 1. SETUP
- Run `git branch --show-current` to confirm the current branch. **Never commit to main/master directly.**
- If a PR number is provided, use it. Otherwise, detect via `gh pr view --json number -q .number`.
- If no PR exists yet: run the conflict check (below), then `gh pr create` and post the URL.
- Determine repo owner/name: `gh repo view --json nameWithOwner -q .nameWithOwner`

#### Conflict Check (run before creating OR pushing to a PR)
```bash
git fetch origin master
git merge origin/master --no-commit --no-ff
```
- If **conflicts** occur: resolve them, run tests, then commit the merge.
- If **clean**: `git merge --abort` and continue.
- **Never** create or update a PR with unresolved merge conflicts.

### 2. POLL — Collect ALL Reviewer Feedback
Fetch from **every comment source** — don't assume only one reviewer exists.

```bash
# PR metadata and check status
gh pr view {pr} --json reviews,comments,reviewRequests,statusCheckRollup

# Inline review comments (CodeRabbit, Greptile, humans)
gh api repos/{owner}/{repo}/pulls/{pr}/comments

# Issue-level / top-level comments (Qodo, bots often post here)
gh api repos/{owner}/{repo}/issues/{pr}/comments

# Review-level summaries
gh api repos/{owner}/{repo}/pulls/{pr}/reviews
```

Also check for Greptile review comments via MCP if available:
- `list_merge_request_comments` with `greptileGenerated: true` to get AI review feedback

Parse each comment for: reviewer name, file, line, body, severity hint.
**Skip resolved/outdated comments** — only process unresolved threads.

### 3. TRIAGE — Categorize Findings
Classify every finding:

| Severity | Criteria |
|----------|----------|
| **Critical** | Security, data loss, crashes, broken logic |
| **High** | Bugs, incorrect behavior, missing validation |
| **Medium** | Code quality, naming, missing tests, style |
| **Low** | Nits, formatting, minor suggestions |

Create a task checklist ordered by severity (critical first). Group related findings that touch the same file/area.

### 4. FIX — Address Each Finding
For each finding (or group of related findings), spawn a general-purpose Task sub-agent with:
- The specific file(s) and line(s)
- The reviewer's comment(s)
- The fix approach
- Instruction to run relevant tests before reporting back

Work through findings from critical to low priority. Spawn agents in parallel for independent findings.

### 5. VERIFY — Full Test Suite
After all fixes:
```bash
uv run pytest tests/ -q
```
If failures occur, fix them directly or spawn debugging sub-agents.

### 6. COMMIT & PUSH
```bash
git branch --show-current  # Safety check — never push from main/master

# Merge latest master and resolve any conflicts before pushing
git fetch origin master
git merge origin/master  # Resolve conflicts if any, run tests, then continue

git add <changed files>
git commit -m "fix: address PR review feedback"
git push
```
Return to step 2 for the next review cycle.

### 7. STOP CONDITION
Stop when:
- All reviewers approve, OR
- 3 review cycles complete (report remaining items to user)

Report final status: what was fixed, what remains, PR URL.

## Safety Rules
- **Always** verify current branch before any commit or push
- **Never** commit to main/master directly
- **Never** force-push unless explicitly asked
- **Always** merge latest master and resolve conflicts before pushing
- Parse **all** comment sources — inline, issue-level, review summaries, and Greptile
- Skip resolved/outdated comment threads
- Run tests before every push
