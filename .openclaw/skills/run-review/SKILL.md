---
name: run-review
description: |
  Triggers a deep Gemini-Reviewer code review on a pull request.
  Can be called standalone or as part of the /orchestrate-feature pipeline.
  Posts the review as a PR comment and returns APPROVED or ISSUES FOUND.
user_invocable: true
model: opus
---

# Run Review — Deep Code Review via Gemini

Spawn the `reviewer` agent to perform a thorough code review on a PR.

## Usage

```
/run-review              # reviews the current branch's open PR
/run-review 42           # reviews PR #42
/run-review feature/...  # reviews the PR for the given branch
```

---

## Workflow

### 1. Identify the PR

```bash
# If no PR number given, detect from current branch:
gh pr view --json number -q .number

# Get PR metadata:
gh pr view {pr_number} --json title,headRefName,baseRefName,url
```

### 2. Spawn Gemini-Reviewer

```
Task tool:
  name: "reviewer"
  subagent_type: "reviewer"
  prompt: |
    You are the Gemini-Reviewer for the hypergraph project.

    Perform a deep code review of PR #{pr_number}.

    Steps:
    1. Read the PR description: `gh pr view {pr_number} --json body -q .body`
    2. Get the full diff: `gh pr diff {pr_number}`
    3. Read the quality criteria: `.openclaw/skills/quality-criteria/references/quality-criteria.md`
    4. Read relevant source files for context (use `read` tool as needed).
    5. Review for:
       - **Correctness**: logic errors, off-by-ones, missing edge cases
       - **Security**: unsafe exec, injection risks, data exposure
       - **Performance**: O(n²) loops on large graphs, unnecessary re-computation
       - **Test coverage**: are all new code paths covered by tests?
       - **API consistency**: does the public API match existing patterns in `src/hypergraph/`?
       - **Documentation**: are new public functions/classes documented?
    6. Post your review as a PR comment:
       ```bash
       gh pr review {pr_number} --comment --body "## Gemini Code Review\n\n{review_body}"
       ```
    7. Output exactly one of:
       - "APPROVED" — no blocking issues
       - "ISSUES FOUND:\n{severity}: {description}\n..." — list issues by severity

    Severity levels: CRITICAL > HIGH > MEDIUM > LOW
    Only CRITICAL and HIGH are blocking. Report MEDIUM/LOW as suggestions.
```

### 3. Parse the Result

- If **APPROVED**: report to user and return.
- If **ISSUES FOUND**: surface the issues to the user (or pass back to the
  coder if called from `/orchestrate-feature`).

### 4. Re-review After Fixes

If fixes were applied, re-run this skill to confirm the issues are resolved.
The reviewer will re-read the updated diff and post a follow-up comment.
