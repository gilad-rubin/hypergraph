# Framework Ports

This page turns adjacent-framework examples into a practical Hypergraph evaluation loop:

1. Pull real upstream examples into a local corpus
2. Prioritize the ones that best match Hypergraph's strengths
3. Rebuild them in a clean, Hypergraph-native style
4. Use the resulting ports to spot real documentation gaps and framework friction

The raw corpus lives under `tmp/framework_corpus/`. Rebuild it with:

```bash
uv run python scripts/build_framework_corpus.py
```

## Why This Matters

This project is less about "winning a comparison" and more about pressure-testing Hypergraph against the workflows people already reach for:

- **LangGraph / Mastra / Inngest** for agentic loops, chats, and human review
- **Hamilton / pipefunc / scikit-learn** for explicit, data-heavy DAGs
- **DBOS / Restate** for durable orchestration and long-running workflows

Looking at those examples side-by-side clarifies where Hypergraph is already elegant and where the user experience still needs work.

## Top Ports

These are the highest-priority examples I ported because they map directly onto current Hypergraph capabilities and reveal useful trade-offs.

| Port | Upstream inspiration | Why it was prioritized | Hypergraph file |
|---|---|---|---|
| Agentic RAG | LangGraph adaptive RAG | Shows nested graph composition plus route-driven source selection without a state schema | `examples/framework_ports/agentic_rag.py` |
| Support Inbox | Mastra suspend/resume, Inngest support HITL, DBOS agent inbox, Restate chat durability | Exercises nested interrupts, pause propagation, and resume semantics in a realistic support workflow | `examples/framework_ports/support_inbox.py` |
| ML Model Selection | Hamilton modular ML example, scikit-learn preprocessing/training pipelines | Shows that Hypergraph can express modular ML workflows without turning the whole graph into framework-specific plumbing | `examples/framework_ports/ml_model_selection.py` |
| Document Batch Pipeline | DBOS document ingestion, Restate RAG ingestion, pipefunc mapped examples | Demonstrates the "write one document graph, then fan it out" pattern with mapped graph nodes | `examples/framework_ports/document_batch_pipeline.py` |

## What The Ports Suggest

### Where Hypergraph feels strong

- **Nested graphs are natural.** The LangGraph-style and support-style ports become smaller once the repeated subflows are first-class graphs.
- **Automatic wiring helps.** The Hamilton- and scikit-learn-inspired pipelines stay readable because values move by name instead of through explicit edge boilerplate.
- **Interrupt propagation is a good primitive.** The support example shows that nested human-review steps can surface cleanly to the outer run.
- **Mapped graph nodes are a real differentiator.** The document and ML ports both benefit from the "think singular, scale later" model.

### Where Hypergraph still feels thin

- **Durability is more explicit than in DBOS, Restate, or Inngest.** Hypergraph has checkpointing, but it is not yet as turnkey for event waits, inbox queries, or long-running service-style orchestration.
- **The best `map_over()` patterns are under-documented.** The capability is strong, but people coming from pipefunc or batch-oriented systems need more first-class examples.
- **HITL examples should talk about persistence earlier.** Interrupts are well-covered, but the durable "pause today, resume tomorrow" story should be easier to discover.
- **Framework-comparison examples deserve a home in the docs.** These ports make the trade-offs concrete in a way abstract comparison tables do not.

## Suggested Next Ports

These upstream examples are promising next steps after the current set:

- DBOS `reliable-refunds-langchain` for a more explicit durable approval flow
- Restate `chat-bot` for longer-lived chat/session patterns
- Inngest `code-assistant-rag` for a tool-using research assistant
- Hamilton `reverse_etl` for a business-data pipeline with real IO boundaries
- pipefunc `sensor-data-processing` for a non-LLM batch-processing example

## Verification

The committed ports are covered by focused tests in `tests/test_framework_ports.py`.
