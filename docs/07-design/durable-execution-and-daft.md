# Durable workflows + Daft for batch RAG evals and long-running agents

Notes from 2026-10-16 discussion on keeping one mental model when a RAG agent needs to support both high-volume batch evals and occasional long-running or HITL cases.

## The preference

One primary mental model is preferable to an arbitrary split ("this run is Daft, that run is DBOS"). Long runs (>20 min) or occasional human approval should not force a different authoring style.

## Recommended model: durable workflow outer, Daft inside a step

Author everything as a durable workflow (DBOS, or equivalent). When a step must process many independent cases efficiently (e.g. 1000 RAG evals), that step calls a high-performance engine such as Daft. From the workflow's point of view the batch call is just another durable step.

Benefits:
- Crash recovery, resume, queues, and HITL live in one place (the workflow).
- Parallel performance and columnar optimization live inside the batch step.
- You always write and reason about the same workflow shape.
- A single long sequential agent with optional approval stays a workflow; you do not force it into a DataFrame.

Sketch:

```python
@DBOS.step()
def run_rag_batch(cases: list[dict]) -> list[dict]:
    df = daft.from_pylist(cases)
    df = df.with_column("context", retrieve_udf(df["query"]))
    df = df.with_column("answer", generate_udf(df["query"], df["context"]))
    df = df.with_column("score", score_udf(df["answer"], df["expected"]))
    return df.collect().to_pylist()

@DBOS.workflow()
def eval_rag_agent(eval_set_id: str):
    cases = load_eval_cases(eval_set_id)
    results = run_rag_batch(cases)
    # optional HITL on low scores via DBOS.recv()
    save_results(eval_set_id, results)
    return results
```

## Reverse model (Daft outer)

You can drive from Daft and launch a DBOS workflow per row for cases that need durability or HITL. This works but creates two top-level execution models, which is usually more awkward for recovery and approval.

## Relation to Hypergraph

Hypergraph already has a `DaftRunner` that lowers DAG graphs into Daft UDFs (see `docs/05-how-to/daft-workflows.md`). The same "workflow outer, data engine inside" pattern applies:

- Use Hypergraph (or DBOS) for the control plane when you need cycles, gates, interrupts, checkpointing, or HITL.
- Drop into `DaftRunner` / a Daft step only for the embarrassingly parallel data-heavy portions (batch retrieval, generation, scoring).
- Keep the outer authoring model stable even when individual runs become long or require approval.

## When pure Daft is enough

If the workload is strictly a DAG of independent cases with no need for mid-run pause, resume, or human approval, `DaftRunner` (or plain Daft) can be the whole story. The moment any case can be long-running or require HITL, prefer the durable outer model.

## Sources

- Conversation on unifying mental models for RAG evals + long-running / HITL agents, 2026-10-16.
- Existing Hypergraph Daft integration: `docs/05-how-to/daft-workflows.md`.
