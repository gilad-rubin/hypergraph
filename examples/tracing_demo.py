"""End-to-end demo of Hypergraph's execution tracing stack.

Exercises every layer from the v5 plan:
  1. RunLog (always-on, in-memory trace)
  2. Checkpointer (durable persistence to SQLite)
  3. Post-hoc inspection (checkpointer sync reads)
  4. Error tracing (failed nodes, partial failures)
  5. Cyclic workflows (re-execution tracking)
  6. Gate routing decisions

Run:  uv run python examples/tracing_demo.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from hypergraph import END, AsyncRunner, Graph, ifelse, node
from hypergraph.checkpointers import SqliteCheckpointer

# ─── Graph definitions ───────────────────────────────────────────────


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


@node(output_name="count")
def increment(count: int) -> int:
    return count + 1


@ifelse(when_true=END, when_false="increment")
def check_done(count: int) -> bool:
    return count >= 3


@node(output_name="a_out")
def succeed_a(x: int) -> int:
    return x + 1


@node(output_name="b_out")
def fail_b(x: int) -> int:
    raise RuntimeError("b failed")


# ─── Helpers ─────────────────────────────────────────────────────────


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


# ─── Demo scenarios ──────────────────────────────────────────────────


async def demo_1_runlog(db_path: str) -> None:
    """Use case 1: Always-on RunLog — zero config, instant trace."""
    section("1. RunLog — Always-On Execution Trace (zero config)")

    runner = AsyncRunner()
    graph = Graph([double, triple])
    result = await runner.run(graph, {"x": 5})

    print(">>> result.log.summary()")
    print(result.log.summary())

    print("\n>>> print(result.log)")
    print(result.log)

    print("\n>>> result.log.timing")
    print(result.log.timing)

    print("\n>>> result.log.node_stats")
    for name, stats in result.log.node_stats.items():
        print(f"  {name}: count={stats.count}, avg={stats.avg_ms:.1f}ms, errors={stats.errors}")


async def demo_2_checkpointer(db_path: str) -> None:
    """Use case 2: Persistent workflow history with SqliteCheckpointer."""
    section("2. Checkpointer — Durable Workflow Persistence")

    cp = SqliteCheckpointer(db_path, durability="sync")

    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([double, triple])
    result = await runner.run(graph, {"x": 5}, workflow_id="demo-pipeline")

    print(f">>> result.status = {result.status.value}")
    print(f">>> result['tripled'] = {result['tripled']}")
    print(f">>> result.log.summary() → {result.log.summary()}")

    # Query the checkpointer directly — sync reads, no await needed
    print("\n--- Querying checkpointer (sync reads) ---")

    state = cp.values("demo-pipeline")
    print("\n>>> cp.values('demo-pipeline')")
    print(f"  {state}")

    steps = cp.steps("demo-pipeline")
    print("\n>>> cp.steps('demo-pipeline')")
    for s in steps:
        print(f"  [{s.index}] {s.node_name}: {s.status.value} ({s.duration_ms:.1f}ms) → {s.values}")

    checkpoint = cp.checkpoint("demo-pipeline")
    print("\n>>> cp.checkpoint('demo-pipeline')")
    print(f"  values: {checkpoint.values}")
    print(f"  steps: {len(checkpoint.steps)}")

    await cp.close()


async def demo_3_error_tracing(db_path: str) -> None:
    """Use case 3: Error tracing — failed nodes get tracked."""
    section("3. Error Tracing — Failed Nodes + Partial Failures")

    cp = SqliteCheckpointer(db_path, durability="sync")

    runner = AsyncRunner(checkpointer=cp)

    # Parallel nodes: one succeeds, one fails
    graph = Graph([succeed_a, fail_b])
    result = await runner.run(graph, {"x": 1}, workflow_id="demo-partial-failure", error_handling="continue")

    print(f">>> result.status = {result.status.value}")
    print(f">>> result.log.summary() → {result.log.summary()}")

    print("\n>>> result.log.errors")
    for err in result.log.errors:
        print(f"  {err.node_name}: {err.error}")

    print("\n>>> print(result.log)")
    print(result.log)

    # Verify in checkpointer — sync reads
    steps = cp.steps("demo-partial-failure")
    print("\n--- Checkpointer step records (sync) ---")
    for s in steps:
        status_line = f"  [{s.index}] {s.node_name}: {s.status.value}"
        if s.error:
            status_line += f" — {s.error}"
        else:
            status_line += f" → {s.values}"
        print(status_line)

    await cp.close()


async def demo_4_cyclic_workflow(db_path: str) -> None:
    """Use case 4: Cyclic workflow — re-executions tracked per superstep."""
    section("4. Cyclic Workflow — Loop Re-Execution Tracking")

    cp = SqliteCheckpointer(db_path, durability="sync")

    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([increment, check_done], entrypoint="increment")
    result = await runner.run(graph, {"count": 0}, workflow_id="demo-cycle")

    print(f">>> result['count'] = {result['count']}")
    print(f">>> result.log.summary() → {result.log.summary()}")

    print("\n>>> print(result.log)  — shows routing decisions + re-executions")
    print(result.log)

    print("\n>>> result.log.node_stats")
    for name, stats in result.log.node_stats.items():
        print(f"  {name}: executed {stats.count}x, total={stats.total_ms:.1f}ms")

    # Checkpointer shows each re-execution as a separate step — sync reads
    steps = cp.steps("demo-cycle")
    print("\n--- Checkpointer: each re-execution is a separate step record (sync) ---")
    for s in steps:
        decision = f", decision={s.decision}" if s.decision else ""
        print(f"  [superstep {s.superstep}] {s.node_name}: {s.values}{decision}")

    await cp.close()


async def demo_5_posthoc_inspection(db_path: str) -> None:
    """Use case 5: post-hoc inspection — checkpointer sync reads from a fresh handle."""
    section("5. Post-Hoc Workflow Inspection (checkpointer sync reads)")

    # A fresh checkpointer handle — as if inspecting from a new process
    cp = SqliteCheckpointer(db_path)

    print(">>> cp.runs()")
    for run in cp.runs():
        print(f"  {run.id}: {run.status.value}")

    print("\n>>> cp.get_run('demo-pipeline')")
    print(f"  {cp.get_run('demo-pipeline')!r}")

    print("\n>>> cp.values('demo-pipeline')")
    print(f"  {cp.values('demo-pipeline')}")

    print("\n>>> cp.steps('demo-partial-failure')")
    for s in cp.steps("demo-partial-failure"):
        error = f" — {s.error}" if s.error else ""
        print(f"  [{s.index}] {s.node_name}: {s.status.value}{error}")

    print("\n>>> cp.steps('demo-cycle')")
    for s in cp.steps("demo-cycle"):
        decision = f", decision={s.decision}" if s.decision else ""
        print(f"  [superstep {s.superstep}] {s.node_name}: {s.values}{decision}")

    print("\n>>> cp.stats('demo-cycle')")
    for name, node_stats in cp.stats("demo-cycle").items():
        print(f"  {name}: steps={node_stats['steps']}, total={node_stats['total_ms']:.1f}ms, errors={node_stats['errors']}")

    await cp.close()


async def demo_6_json_export(db_path: str) -> None:
    """Use case 6: JSON export — machine-readable traces."""
    section("6. JSON Export — Machine-Readable Traces")

    runner = AsyncRunner()
    graph = Graph([double, triple])
    result = await runner.run(graph, {"x": 7})

    print(">>> json.dumps(result.log.to_dict(), indent=2)")
    print(json.dumps(result.log.to_dict(), indent=2))


# ─── Main ────────────────────────────────────────────────────────────


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "demo.db")

        await demo_1_runlog(db_path)
        await demo_2_checkpointer(db_path)
        await demo_3_error_tracing(db_path)
        await demo_4_cyclic_workflow(db_path)
        await demo_5_posthoc_inspection(db_path)
        await demo_6_json_export(db_path)

        section("Done!")
        print("All use cases from the v5 execution tracing plan demonstrated.")
        print(f"Temporary database was at: {db_path}")


if __name__ == "__main__":
    asyncio.run(main())
