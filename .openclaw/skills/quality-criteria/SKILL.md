---
name: quality-criteria
description: |
  Code quality checklist for the hypergraph project. Not directly invocable —
  loaded by agents via `skills: [quality-criteria]`.
user_invocable: false
---

# Quality Criteria

This skill provides the unified code quality checklist used by Codex-Agent
(builder) and Gemini-Reviewer (reviewer).

The full checklist is in [references/quality-criteria.md](references/quality-criteria.md).

## Usage

Agents that need quality criteria reference this skill. The checklist is then
available as preloaded context.

- **Builders** (Codex-Agent): consult the checklist while writing code.
- **Reviewers** (Gemini-Reviewer): check every item and report violations.
