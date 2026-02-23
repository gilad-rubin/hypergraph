# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Plan: Partial Input Semantics

## Context

`InputSpec.required` is currently a static, graph-wide property. But what users actually need to provide depends on **where they start** and **what outputs they want**. Today there's no way to express this, leading to:
- Users can't discover what inputs they need for a specific execution plan
- Cycle entry point errors aren't actionable (don't show what each option requires)
- Invalid input combinations (upstream + downs...

### Prompt 2

[Request interrupted by user]

### Prompt 3

please use the /feature skill

### Prompt 4

Base directory for this skill: /Users/giladrubin/.claude/skills/feature

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
Builder fi...

