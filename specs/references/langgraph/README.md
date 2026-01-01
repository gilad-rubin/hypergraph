# LangGraph Reference Documentation

> Reference materials for LangGraph v1.0 (October 2025) to inform HyperNodes design decisions.

## Purpose

These documents capture LangGraph's approach to key features that HyperNodes needs to mature. Each document includes:
- How LangGraph implements the feature
- API surface and code examples
- Design decisions and trade-offs
- **Implications for HyperNodes** - what we should adopt or do differently

## Documents

| Document | Topic | HyperNodes Status |
|----------|-------|-------------------|
| [streaming.md](streaming.md) | 5 streaming modes, events, token streaming | Has `AsyncRunner.iter()`, missing modes |
| [retry-policy.md](retry-policy.md) | Exponential backoff, per-node policies | Mentioned but not specified |
| [time-travel.md](time-travel.md) | Replay, fork, state history navigation | Has checkpoint, missing navigation |
| [durable-execution.md](durable-execution.md) | Checkpointing, persistence backends, fault tolerance | Has interrupt checkpoint, missing backends |

## Key LangGraph v1.0 Features

LangGraph 1.0 stabilized four core runtime features:

1. **Durable Execution** - Checkpoint at every step, resume on failure
2. **Human-in-the-Loop** - Interrupt and resume with user input
3. **Streaming** - Multiple modes for different use cases
4. **Memory** - Short-term (checkpoints) and long-term (stores)

## HyperNodes Gaps to Address

Based on these references, priority gaps:

### High Priority
- [ ] Checkpointer interface with pluggable backends
- [ ] Thread concept for grouping related executions
- [ ] RetryPolicy per-node configuration
- [ ] State snapshot retrieval (time travel)

### Medium Priority
- [ ] Streaming mode selection (values, updates, messages)
- [ ] Custom stream writer for tool progress
- [ ] Pending writes preservation on partial failure
- [ ] Fork from checkpoint

### Lower Priority
- [ ] Memory store for cross-thread data
- [ ] Encrypted serialization
- [ ] Debug stream mode

## Design Philosophy Differences

| Aspect | LangGraph | HyperNodes |
|--------|-----------|------------|
| State | Central TypedDict | Function signatures |
| Streaming | 5 modes to choose from | Unified event stream |
| Checkpointing | Built-in backends | User provides bytes |
| Retry | Per-node RetryPolicy | Runner-level (TBD) |

## Sources

All documentation based on:
- [LangGraph Official Docs](https://docs.langchain.com/oss/python/langgraph/)
- [LangGraph GitHub](https://github.com/langchain-ai/langgraph)
- [DeepWiki LangGraph Reference](https://deepwiki.com/langchain-ai/langgraph/)
