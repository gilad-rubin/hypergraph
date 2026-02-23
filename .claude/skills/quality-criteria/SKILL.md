---
name: quality-criteria
description: |
  Code quality checklist: code smells, design principles, SOLID, flat-code rules.
  Not directly invocable — loaded by agents via skills: [quality-criteria].
user_invocable: false
---

# Quality Criteria

This skill provides a unified code quality checklist used by builders and reviewers.

The full checklist is in [references/quality-criteria.md](references/quality-criteria.md).

## Usage

Agents that need quality criteria add `skills: [quality-criteria]` to their frontmatter.
The checklist is then available as preloaded context.

Builders should consult the checklist while writing code.
Reviewers should check every item and report violations as findings.
