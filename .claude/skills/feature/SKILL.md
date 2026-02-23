---
name: feature
description: |
  End-to-end feature implementation with quality gates at each phase.
  Triggers: /feature, implement feature, build feature, new feature
user_invocable: true
model: opus
---

# Feature Workflow

End-to-end feature implementation with a **doer+critic** pattern using Claude Code Teams. At each phase, a builder produces an artifact and a reviewer critiques it against shared quality criteria. Both see the same standards.

## The Pattern

```
Builder produces artifact (plan / code / docs)
    ↓
Reviewer critiques against shared quality criteria
    ↓
APPROVED? → next phase
    ↓ no
Builder fixes issues, sends to reviewer via SendMessage → re-review
(max 3 iterations; then escalate to user)
```

---

## Setup: Create the Team

Before starting, create a team to coordinate all agents:

```
TeamCreate:
  team_name: "feature-{short-kebab-name}"
  description: "Implementing: {feature description}"
```

Then create tasks for all phases upfront using `TaskCreate`:
- Task: "Plan the feature" (Phase 1)
- Task: "Review the plan" (Phase 1, blocked by plan task)
- Task: "Implement the feature" (Phase 2, blocked by plan review)
- Task: "Review the implementation" (Phase 2, blocked by implementation)
- Task: "Write documentation" (Phase 3, blocked by implementation review)
- Task: "Review documentation" (Phase 3, blocked by doc writing)
- Task: "Create PR" (Phase 4, blocked by doc review)

Use `TaskUpdate` with `addBlockedBy` to set up the dependency chain.

---

## Phase 1: Plan

### Builder (you — team lead)

Claim and start the plan task (`TaskUpdate` → `in_progress`).

1. **Explore** the codebase (spawn haiku subagents via Task tool for file discovery)
2. **Research** docs and patterns (spawn sonnet subagents for DeepWiki/Context7/Perplexity)
3. **Check test matrix** — read `tests/capabilities/matrix.py` if it exists
4. **Read quality criteria** — read `.claude/skills/quality-criteria/references/quality-criteria.md`
5. **Produce plan:**
   - Requirements and scope
   - Architecture design with SOLID analysis
   - File-by-file changes
   - TDD: test cases defined before implementation
   - New capability dimensions for test matrix (if applicable)
   - Step-by-step with verification at each step

Mark plan task as `completed`.

### Reviewer (code-reviewer teammate)

Spawn a `code-reviewer` teammate and assign the review task:

```
Task tool:
  name: "plan-reviewer"
  subagent_type: "code-reviewer"
  team_name: "feature-{name}"
  prompt: |
    You are a plan reviewer on team "feature-{name}".

    1. Read the team task list (TaskList) and claim the plan review task
    2. Review this feature plan against your preloaded quality criteria

    <plan>
    {plan_text}
    </plan>

    Additional instructions:
    - Research best practices for this type of feature (spawn sonnet subagent for Perplexity)
    - Red-team: what capability × facet combinations could break?
    - Challenge assumptions, propose simpler alternatives
    - Send your review to the team lead via SendMessage:
      - type: "message", recipient: "team-lead"
      - End with: APPROVED or ISSUES FOUND (list by severity)
    - Mark the review task as completed when done
```

### Review Loop

- If reviewer sends **APPROVED** → proceed to Phase 2
- If **ISSUES FOUND** → fix the plan, then use `SendMessage` to send the revised plan back to `plan-reviewer` for re-review
- After 3 iterations → present unresolved issues to user for decision

---

## Phase 2: Implement

### Builder (you — team lead)

Claim the implementation task (`TaskUpdate` → `in_progress`).

1. Write failing tests first (TDD from plan)
2. Implement code to pass tests
3. Run tests: `uv run pytest <relevant tests>`
4. Commit after each logical step (`git commit` with conventional commit format)

Mark implementation task as `completed`.

### Reviewer (code-reviewer teammate)

Send the diff to the existing `plan-reviewer` teammate (or spawn a new one if it shut down):

```
SendMessage:
  type: "message"
  recipient: "plan-reviewer"
  content: |
    Review this implementation against your preloaded quality criteria.

    <diff>
    {git_diff_output}
    </diff>

    Additional instructions:
    - Research: known issues with libraries/patterns used? (spawn sonnet subagent)
    - Verify test coverage for new code paths
    - Check that tests match the plan's TDD specification
    - Claim the implementation review task and mark completed when done
    - Send results via SendMessage to team lead: APPROVED or ISSUES FOUND
  summary: "Review implementation diff"
```

### Review Loop

- If **APPROVED** → proceed to Phase 3
- If **ISSUES FOUND** → fix the code, re-run tests, send updated diff via `SendMessage`
- After 3 iterations → present issues to user for decision

---

## Phase 3: Update Documentation

### Builder (docs-writer teammate)

Spawn a `docs-writer` teammate:

```
Task tool:
  name: "docs-writer"
  subagent_type: "docs-writer"
  team_name: "feature-{name}"
  prompt: |
    You are the docs writer on team "feature-{name}".

    1. Read the team task list (TaskList) and claim the docs writing task
    2. Update documentation for the following feature

    <feature>
    {feature_description}
    </feature>

    <changed_files>
    {list_of_changed_files}
    </changed_files>

    Instructions:
    - Follow your preloaded docs-writer conventions
    - Update affected doc pages, README, and CLAUDE.md if needed
    - Apply quality criteria to all code examples
    - When done, send a message to the team lead listing what you changed
    - Mark the docs task as completed
    - Stay alive for potential revision requests
```

### Reviewer (docs-reviewer teammate)

Spawn a `docs-reviewer` teammate:

```
Task tool:
  name: "docs-reviewer"
  subagent_type: "docs-reviewer"
  team_name: "feature-{name}"
  prompt: |
    You are the docs reviewer on team "feature-{name}".

    1. Read the team task list (TaskList) and claim the docs review task
    2. Review the documentation changes

    Instructions:
    - Review against your preloaded docs-writer conventions and quality criteria
    - Research: how do top libraries document similar features? (use Perplexity)
    - Send your review to the team lead via SendMessage: APPROVED or ISSUES FOUND
    - Mark the review task as completed when done
```

### Review Loop

- If **APPROVED** → proceed to Phase 4
- If **ISSUES FOUND** → send feedback to `docs-writer` via `SendMessage`, who fixes and notifies team lead
- After 3 iterations → present issues to user for decision

---

## Phase 4: PR

Create a pull request using project conventions:

```bash
gh pr create --title "<concise title>" --body "$(cat <<'EOF'
## Summary
<1-3 bullet points from the plan>

## Test plan
<checklist from TDD spec>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Mark the PR task as `completed`.

---

## Phase 5: Address PR Comments

Use the existing `/review-pr` skill for the feedback loop on PR review comments.

---

## Teardown

After all phases complete:

1. Send `shutdown_request` to all active teammates (`plan-reviewer`, `docs-writer`, `docs-reviewer`)
2. Wait for shutdown confirmations
3. Call `TeamDelete` to clean up team and task files

---

## Summary

| Phase | Builder | Reviewer | Communication |
|-------|---------|----------|---------------|
| Plan | Team lead | plan-reviewer (code-reviewer) | SendMessage for iterations |
| Implement | Team lead | plan-reviewer (reused) | SendMessage with diff |
| Docs | docs-writer teammate | docs-reviewer teammate | SendMessage between peers |
| PR | Team lead | — | — |
| PR Comments | Team lead | /review-pr skill | — |

All agents share quality criteria via `skills: [quality-criteria]` in their agent definitions.
All coordination happens through the shared `TaskList` + `SendMessage`.
