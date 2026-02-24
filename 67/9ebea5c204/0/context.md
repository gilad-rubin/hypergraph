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

