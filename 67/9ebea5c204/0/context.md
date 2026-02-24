# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Plan: Add `clone` parameter to `map_over()`

## Context

When a GraphNode uses `map_over`, non-mapped ("broadcast") inputs are shared by reference across all iterations. This is the correct default (matches every peer library: LangGraph, Prefect, Hamilton, Dask, Ray), but there's no escape hatch when users need iteration-independent copies — e.g., a mutable config dict or state object that nodes modify.

**Goal**: Add a `clone` parameter to `map_over()` that le...

### Prompt 2

can you merge master into this brancha dn prepare a PR for review? then /review-pr

### Prompt 3

Base directory for this skill: /Users/giladrubin/.claude/skills/review-pr

# PR Review Summary

Fetch comments for PR number (argument) or current branch's PR if none provided.

## Fetch Commands

**IMPORTANT**: You MUST fetch from **all three** GitHub comment locations. Different bots post in different places — missing one location means missing entire reviewers (e.g., Qodo only posts to issue comments).

### Step 1: Discover which bots commented (run all three in parallel)

```bash
# List un...

