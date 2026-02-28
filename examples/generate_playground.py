"""Generate the tracing playground HTML from real code execution.

Defines use cases, runs real graphs, captures real output, and renders
a single-file HTML playground. Nothing is hardcoded — every code block
and output string comes from actual API calls.

Run:  uv run python examples/generate_playground.py
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from hypergraph import END, AsyncRunner, Graph, SyncRunner, ifelse, node
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_PATH = Path(__file__).parent / "tracing-playground.html"


# ─── Graph definitions ───────────────────────────────────────────────


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


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


# ─── Data model ──────────────────────────────────────────────────────


@dataclass
class UseCase:
    id: str
    label: str
    title: str
    note: str
    category: str  # "tracing" | "persistence" | "map"
    status: str  # "ok" | "gap" | "defer"
    impl_note: str

    single_python: str  # Python code + output for single-item run
    single_cli: str  # CLI output for single-item (empty string if N/A)
    mapped_python: str  # runner.map() variation
    mapped_cli: str
    nested_python: str  # map_over variation
    nested_cli: str


# ─── Helpers ─────────────────────────────────────────────────────────


def run_cli(args: list[str]) -> str:
    """Run hypergraph CLI and return output."""
    result = subprocess.run(
        ["uv", "run", "hypergraph", *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    return strip_ansi(result.stdout + result.stderr).rstrip()


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def fmt_dict(d: dict, indent: int = 0) -> str:
    """Format a dict for display, one key per line if many."""
    prefix = " " * indent
    if len(d) <= 3:
        return repr(d)
    lines = ["{"]
    for k, v in d.items():
        lines.append(f"{prefix}  {k!r}: {v!r},")
    lines.append(f"{prefix}}}")
    return "\n".join(lines)


def fmt_node_stats(stats: dict) -> str:
    lines = []
    for name, s in stats.items():
        lines.append(f"  {name}: count={s.count}, avg={s.avg_ms:.1f}ms, errors={s.errors}")
    return "\n".join(lines)


def fmt_steps(steps) -> str:
    lines = []
    for s in steps:
        decision = ""
        if hasattr(s, "decision") and s.decision is not None:
            decision = f", decision={s.decision}"
        error = ""
        if hasattr(s, "error") and s.error:
            error = f", error={s.error}"
        status = s.status if isinstance(s.status, str) else s.status.value
        vals = ""
        if hasattr(s, "values") and s.values is not None:
            vals = f" → {s.values}"
        lines.append(f"  [{s.index}] {s.node_name}: {status} ({s.duration_ms:.1f}ms){vals}{decision}{error}")
    return "\n".join(lines)


def fmt_inner_logs(map_log, indent: int = 2) -> str:
    """Format inner RunLog summaries for display."""
    prefix = " " * indent
    lines = []
    for i, log in enumerate(map_log):
        lines.append(f"{prefix}[{i}] {log.graph_name}: {log.summary()}")
    return "\n".join(lines)


def fmt_step_records(steps) -> str:
    """Format StepRecord objects from checkpointer."""
    lines = []
    for s in steps:
        decision = f", decision={s.decision}" if s.decision else ""
        error = f" — {s.error}" if s.error else ""
        vals = f" → {s.values}" if s.values else ""
        lines.append(f"  [{s.index}] {s.node_name}: {s.status.value} ({s.duration_ms:.1f}ms){vals}{decision}{error}")
    return "\n".join(lines)


# ─── Scenario runners ────────────────────────────────────────────────


def run_uc1() -> UseCase:
    """UC1: Why was my run slow? — RunLog timing."""
    runner = SyncRunner()
    graph = Graph([double, triple])

    # Single
    result = runner.run(graph, {"x": 5})
    single = f"""\
runner = SyncRunner()
graph = Graph([double, triple])
result = runner.run(graph, {{"x": 5}})

>>> result.log.summary()
'{result.log.summary()}'

>>> print(result.log)
{result.log}

>>> result.log.timing
{result.log.timing}

>>> result.log.node_stats
{fmt_node_stats(result.log.node_stats)}"""

    # Mapped — runner.map()
    results = runner.map(graph, {"x": [1, 2, 3, 4, 5]}, map_over="x")
    r0 = results[0]
    mapped = f"""\
results = runner.map(graph, {{"x": [1, 2, 3, 4, 5]}}, map_over="x")

>>> results.summary()
'{results.summary()}'

# Drill into one item — same RunLog API as Single
>>> print(results[0].log)
{r0.log}

>>> results[0].log.timing
{r0.log.timing}

>>> results[0].log.node_stats
{fmt_node_stats(r0.log.node_stats)}"""

    # Nested — map_over
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node().map_over("x")])
    result_n = runner.run(outer, {"x": [1, 2, 3, 4, 5]})
    step_n = result_n.log.steps[0]
    log0 = step_n.log[0]
    nested = f"""\
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
result = runner.run(outer, {{"x": [1, 2, 3, 4, 5]}})

>>> result.log.summary()
'{result_n.log.summary()}'

# print() reveals "(5 inner)" — there's more inside this step
>>> print(result.log)
{result_n.log}

# Drill into one inner run — same RunLog API as Single
>>> step = result.log.steps[0]
>>> print(step.log[0])
{log0}

>>> step.log[0].timing
{log0.timing}"""

    return UseCase(
        id="uc1",
        label="UC1",
        title='"Why was my run slow?"',
        note="RunLog timing, node_stats, summary",
        category="tracing",
        status="ok",
        impl_note="Fully implemented. RunLog is always-on, zero config.",
        single_python=single,
        single_cli="",
        mapped_python=mapped,
        mapped_cli="",
        nested_python=nested,
        nested_cli="",
    )


def run_uc2() -> UseCase:
    """UC2: What failed and why? — Error tracking."""
    runner = SyncRunner()
    graph = Graph([succeed_a, fail_b])

    # Single
    result = runner.run(graph, {"x": 1}, error_handling="continue")
    errors = "\n".join(f"  {e.node_name}: {e.error}" for e in result.log.errors)
    single = f"""\
graph = Graph([succeed_a, fail_b])
result = runner.run(graph, {{"x": 1}}, error_handling="continue")

>>> result.status
{result.status}

>>> result.log.summary()
'{result.log.summary()}'

>>> result.log.errors
{errors}

>>> print(result.log)
{result.log}"""

    # Mapped — runner.map() with mixed success/failure
    @node(output_name="value")
    def maybe_fail(x: int) -> int:
        if x == 3:
            raise ValueError(f"x={x} is not allowed")
        return x * 10

    fail_graph = Graph([maybe_fail])
    results = runner.map(
        fail_graph,
        {"x": [1, 2, 3, 4, 5]},
        map_over="x",
        error_handling="continue",
    )
    r_failed = results.failures[0]
    mapped = f"""\
results = runner.map(
    fail_graph, {{"x": [1, 2, 3, 4, 5]}},
    map_over="x", error_handling="continue",
)

>>> results.status
{results.status}

>>> results.summary()
'{results.summary()}'

# Drill into the failed item — same RunLog API as Single
>>> results.failures[0].log.errors
  {r_failed.log.errors[0].node_name}: {r_failed.log.errors[0].error}

>>> print(results.failures[0].log)
{r_failed.log}"""

    # Nested — map_over with error_handling="continue"
    inner_fail = Graph([maybe_fail], name="checker")
    outer_fail = Graph([inner_fail.as_node().map_over("x", error_handling="continue")])
    result_n = runner.run(outer_fail, {"x": [1, 2, 3, 4, 5]}, error_handling="continue")
    step_n = result_n.log.steps[0]
    err_logs = [log for log in step_n.log if log.errors]
    nested = f"""\
inner = Graph([maybe_fail], name="checker")
outer = Graph([inner.as_node().map_over("x", error_handling="continue")])
result = runner.run(outer, {{"x": [1, 2, 3, 4, 5]}}, error_handling="continue")

>>> result.status
{result_n.status}

>>> result.log.summary()
'{result_n.log.summary()}'

# print() reveals "(5 inner)" — there's more inside this step
>>> print(result.log)
{result_n.log}

# Drill into inner logs — same RunLog API as Single
>>> failed = [log for log in result.log.steps[0].log if log.errors]
>>> failed[0].errors[0].error
'{err_logs[0].errors[0].error}'

>>> print(failed[0])
{err_logs[0]}"""

    return UseCase(
        id="uc2",
        label="UC2",
        title='"What failed and why?"',
        note="Error tracking, partial failures",
        category="tracing",
        status="ok",
        impl_note="Fully implemented. error_handling='continue' works in both run() and map().",
        single_python=single,
        single_cli="",
        mapped_python=mapped,
        mapped_cli="",
        nested_python=nested,
        nested_cli="",
    )


def run_uc3() -> UseCase:
    """UC3: What path did execution take? — Routing and cycles."""
    runner = SyncRunner()
    graph = Graph([increment, check_done])

    # Single
    result = runner.run(graph, {"count": 0})
    single = f"""\
# Cyclic graph: increment → check_done → (loop or END)
graph = Graph([increment, check_done])
result = runner.run(graph, {{"count": 0}})

>>> result["count"]
{result["count"]}

>>> result.log.summary()
'{result.log.summary()}'

# RunLog shows every re-execution with routing decisions
>>> print(result.log)
{result.log}

>>> result.log.node_stats
{fmt_node_stats(result.log.node_stats)}"""

    # Mapped — runner.map() on the cycle graph (each item gets its own RunLog)
    results_m = runner.map(graph, {"count": [0, 1, 2]}, map_over="count")
    r0_m = results_m[0]
    mapped = f"""\
# runner.map() on a cyclic graph — each item gets its own RunLog with routing
results = runner.map(graph, {{"count": [0, 1, 2]}}, map_over="count")

>>> results.summary()
'{results_m.summary()}'

>>> results["count"]
{results_m["count"]}

# Drill into one item — same RunLog API as Single
>>> print(results[0].log)
{r0_m.log}

>>> results[0].log.node_stats
{fmt_node_stats(r0_m.log.node_stats)}"""

    # Nested — map_over with .log revealing per-item routing
    inner = Graph([increment, check_done], name="counter")
    outer = Graph([inner.as_node().map_over("count")])
    result_n = runner.run(outer, {"count": [0, 1, 2]})
    step_n = result_n.log.steps[0]
    log0_n = step_n.log[0]
    nested = f"""\
# map_over — drill into per-item routing via .log
inner = Graph([increment, check_done], name="counter")
outer = Graph([inner.as_node().map_over("count")])
result = runner.run(outer, {{"count": [0, 1, 2]}})

>>> result["count"]
{result_n["count"]}

>>> result.log.summary()
'{result_n.log.summary()}'

# print() reveals "(3 inner)" — there's more inside this step
>>> print(result.log)
{result_n.log}

# Drill into one inner run — same RunLog API as Single
>>> step = result.log.steps[0]
>>> print(step.log[0])
{log0_n}

>>> step.log[0].node_stats
{fmt_node_stats(log0_n.node_stats)}"""

    return UseCase(
        id="uc3",
        label="UC3",
        title='"What path did execution take?"',
        note="Gate routing, cyclic re-execution",
        category="tracing",
        status="ok",
        impl_note="Fully implemented. RunLog tracks routing decisions and re-executions.",
        single_python=single,
        single_cli="",
        mapped_python=mapped,
        mapped_cli="",
        nested_python=nested,
        nested_cli="",
    )


async def run_uc4(db_path: str) -> UseCase:
    """UC4: Intermediate values — Checkpointer queries."""
    cp = SqliteCheckpointer(db_path, durability="sync")
    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([double, triple])

    # Single
    await runner.run(graph, {"x": 5}, workflow_id="uc4-single")
    state = cp.state("uc4-single")
    steps = cp.steps("uc4-single")
    single = f"""\
cp = SqliteCheckpointer("./workflows.db", durability="sync")
runner = AsyncRunner(checkpointer=cp)
result = await runner.run(graph, {{"x": 5}}, workflow_id="uc4-single")

# Sync reads — no await needed
>>> cp.state("uc4-single")
{state}

>>> cp.steps("uc4-single")
{fmt_step_records(steps)}

>>> cp.checkpoint("uc4-single")
  values: {cp.checkpoint("uc4-single").values}
  steps: {len(cp.checkpoint("uc4-single").steps)}"""

    single_cli = run_cli(["workflows", "state", "uc4-single", "--values", "--db", db_path])

    # Mapped — runner.map() is ephemeral (no checkpoints)
    runner_sync = SyncRunner()
    results_map = runner_sync.map(graph, {"x": [5, 10, 15]}, map_over="x")
    mapped = f"""\
# runner.map() is ephemeral — per-item RunLogs but no persistence
runner_sync = SyncRunner()
results = runner_sync.map(graph, {{"x": [5, 10, 15]}}, map_over="x")

>>> results.summary()
'{results_map.summary()}'

>>> results["tripled"]
{results_map["tripled"]}

# Not checkpointed — cp.workflows() won't show these runs
# Use map_over with a checkpointer for persistence"""

    # Nested — map_over in a checkpointed run
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node().map_over("x")])
    result_m = await runner.run(outer, {"x": [5, 10, 15]}, workflow_id="uc4-multi")
    state_m = cp.state("uc4-multi")
    steps_m = cp.steps("uc4-multi")
    nested = f"""\
# map_over in a checkpointed run — the outer graph persists as one workflow
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
result = await runner.run(outer, {{"x": [5, 10, 15]}}, workflow_id="uc4-multi")

>>> result["tripled"]
{result_m["tripled"]}

# The workflow contains one step (the mapped GraphNode)
>>> cp.state("uc4-multi")
{state_m}

>>> cp.steps("uc4-multi")
{fmt_step_records(steps_m)}"""

    nested_cli = run_cli(["workflows", "state", "uc4-multi", "--values", "--db", db_path])

    await cp.close()
    return UseCase(
        id="uc4",
        label="UC4",
        title='"What values flowed through?"',
        note="Intermediate values via checkpointer",
        category="persistence",
        status="ok",
        impl_note="Fully implemented. Sync reads: cp.state(), cp.steps(), cp.checkpoint().",
        single_python=single,
        single_cli=f"$ hypergraph workflows state uc4-single --values --db <db>\n\n{single_cli}",
        mapped_python=mapped,
        mapped_cli="# runner.map() runs are ephemeral — no CLI queries available",
        nested_python=nested,
        nested_cli=f"$ hypergraph workflows state uc4-multi --values --db <db>\n\n{nested_cli}",
    )


async def run_uc5(db_path: str) -> UseCase:
    """UC5: Cross-process persistence — query after exit."""
    # Data already written by UC4. Simulate querying from a "new process".
    cp = SqliteCheckpointer(db_path)

    wfs = cp.workflows()
    state = cp.state("uc4-single")
    steps = cp.steps("uc4-single")
    single = f"""\
# In a NEW process — query old workflows (sync, no await)
cp = SqliteCheckpointer("./workflows.db")

>>> cp.workflows()
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in wfs)}

>>> cp.state("uc4-single")
{state}

>>> cp.steps("uc4-single")
{fmt_step_records(steps)}"""

    single_cli = run_cli(["workflows", "ls", "--db", db_path])

    # Mapped — runner.map() results aren't persisted
    runner_sync = SyncRunner()
    graph = Graph([double, triple])
    results_map = runner_sync.map(graph, {"x": [5, 10, 15]}, map_over="x")
    mapped = f"""\
# runner.map() is ephemeral — results exist only in the current process
results = runner.map(graph, {{"x": [5, 10, 15]}}, map_over="x")

>>> results.summary()
'{results_map.summary()}'

# After the process exits, these results are gone
# cp.workflows() won't show runner.map() runs
>>> cp.workflows()
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in wfs)}

# Use map_over with a checkpointer for cross-process persistence"""

    # Nested — query the mapped workflow
    state_m = cp.state("uc4-multi")
    nested = f"""\
# The mapped workflow is also queryable from a new process
>>> cp.state("uc4-multi")
{state_m}

>>> cp.workflows()
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in wfs)}"""

    nested_cli = run_cli(["workflows", "show", "uc4-multi", "--db", db_path])

    return UseCase(
        id="uc5",
        label="UC5",
        title='"What happened in yesterday\'s run?"',
        note="Cross-process persistence",
        category="persistence",
        status="ok",
        impl_note="Fully implemented. Sync reads work from any process without async.",
        single_python=single,
        single_cli=f"$ hypergraph workflows ls --db <db>\n\n{single_cli}",
        mapped_python=mapped,
        mapped_cli="# runner.map() runs are ephemeral — nothing to query from CLI",
        nested_python=nested,
        nested_cli=f"$ hypergraph workflows show uc4-multi --db <db>\n\n{nested_cli}",
    )


async def run_uc6(db_path: str) -> UseCase:
    """UC6: Show me all failed workflows — Dashboard."""
    # Create a failed workflow
    cp = SqliteCheckpointer(db_path, durability="sync")
    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([succeed_a, fail_b])
    await runner.run(graph, {"x": 1}, workflow_id="uc6-failed", error_handling="continue")
    await cp.close()

    cp2 = SqliteCheckpointer(db_path)
    all_wfs = cp2.workflows()
    failed = cp2.workflows(status=WorkflowStatus.FAILED)

    single = f"""\
>>> cp.workflows()
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in all_wfs)}

>>> cp.workflows(status=WorkflowStatus.FAILED)
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in failed)}

# Drill into the failed workflow
>>> cp.steps("{failed[0].id}" if failed else "???")
{fmt_step_records(cp2.steps(failed[0].id)) if failed else "  (no failed workflows)"}"""

    single_cli = run_cli(["workflows", "--db", db_path])

    # Mapped — runner.map() with MapResult.failures (in-process filtering)
    @node(output_name="checked")
    def flaky(x: int) -> int:
        if x % 3 == 0:
            raise ValueError(f"x={x} is banned")
        return x * 10

    runner_sync = SyncRunner()
    flaky_graph = Graph([flaky])
    results_map = runner_sync.map(
        flaky_graph,
        {"x": [1, 2, 3, 4, 5, 6]},
        map_over="x",
        error_handling="continue",
    )
    failure_errors = [f.error for f in results_map.failures]
    mapped = f"""\
# runner.map() — in-process filtering via MapResult (ephemeral)
results = runner.map(
    flaky_graph, {{"x": [1, 2, 3, 4, 5, 6]}},
    map_over="x", error_handling="continue",
)

>>> results.summary()
'{results_map.summary()}'

>>> results.failed
{results_map.failed}

>>> [f.error for f in results.failures]
{failure_errors}

# In-process only — not persisted to checkpointer
# For persistent failure tracking, use map_over with a checkpointer"""

    # Nested — dashboard shows both single and mapped workflows
    nested = f"""\
# The dashboard shows all workflows — single runs and mapped runs
>>> cp.workflows()
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in all_wfs)}

>>> cp.workflows(status=WorkflowStatus.COMPLETED)
  {chr(10).join(f"  {w.id}: {w.status.value}" for w in cp2.workflows(status=WorkflowStatus.COMPLETED))}"""

    nested_cli = run_cli(["workflows", "ls", "--status", "completed", "--db", db_path])

    return UseCase(
        id="uc6",
        label="UC6",
        title='"Show me all failed workflows"',
        note="Dashboard, status filtering",
        category="persistence",
        status="ok",
        impl_note="Fully implemented. Filter by status via Python API or CLI.",
        single_python=single,
        single_cli=f"$ hypergraph workflows --db <db>\n\n{single_cli}",
        mapped_python=mapped,
        mapped_cli="# runner.map() failures are ephemeral — use MapResult.failures in-process",
        nested_python=nested,
        nested_cli=f"$ hypergraph workflows ls --status completed --db <db>\n\n{nested_cli}",
    )


def run_uc7() -> UseCase:
    """UC7: AI Agent Debugging — JSON export."""
    runner = SyncRunner()
    graph = Graph([double, triple])

    # Single
    result = runner.run(graph, {"x": 5})
    log_dict = result.log.to_dict()
    json_pretty = json.dumps(log_dict, indent=2, default=str)
    # Truncate for display
    if len(json_pretty) > 600:
        json_pretty = json_pretty[:600] + "\n  ... (truncated)"
    single = f"""\
result = runner.run(graph, {{"x": 5}})

# Machine-readable trace for agents
>>> json.dumps(result.log.to_dict(), indent=2)
{json_pretty}"""

    single_cli = run_cli(["workflows", "--help"])

    # Mapped — runner.map() JSON export
    results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")
    batch_json = json.dumps(results.to_dict(), indent=2, default=str)
    if len(batch_json) > 600:
        batch_json = batch_json[:600] + "\n  ... (truncated)"
    mapped = f"""\
results = runner.map(graph, {{"x": [1, 2, 3]}}, map_over="x")

# Batch-level JSON export (includes per-item metadata)
>>> json.dumps(results.to_dict(), indent=2)
{batch_json}

# Per-item summaries via RunResult.summary()
>>> [r.summary() for r in results]
{[r.summary() for r in results]}"""

    # Nested — map_over JSON with recursive .log
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node().map_over("x")])
    result_n = runner.run(outer, {"x": [1, 2, 3]})
    nested_json = json.dumps(result_n.log.to_dict(), indent=2, default=str)
    if len(nested_json) > 1400:
        nested_json = nested_json[:1400] + "\n  ... (truncated)"
    nested = f"""\
# map_over — to_dict() includes recursive inner logs
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
result = runner.run(outer, {{"x": [1, 2, 3]}})

# inner logs appear in each step's JSON — full recursive trace
>>> json.dumps(result.log.to_dict(), indent=2)
{nested_json}

# Agents can parse inner logs to inspect per-item execution
>>> result.summary()
'{result_n.summary()}'"""

    return UseCase(
        id="uc7",
        label="UC7",
        title="AI Agent Debugging",
        note="Structured JSON for agents",
        category="tracing",
        status="ok",
        impl_note="Fully implemented. .to_dict() for Python, --json for CLI.",
        single_python=single,
        single_cli=f"$ hypergraph workflows --help\n\n{single_cli}",
        mapped_python=mapped,
        mapped_cli="# runner.map() results are ephemeral — use .to_dict() per item\n# For persistent JSON, use map_over with a checkpointer + CLI --json",
        nested_python=nested,
        nested_cli="",
    )


async def run_uc8(db_path: str) -> UseCase:
    """UC8: Live monitoring — durability modes."""
    cp = SqliteCheckpointer(db_path, durability="sync")
    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([double, triple])
    await runner.run(graph, {"x": 5}, workflow_id="uc8-live")

    state = cp.state("uc8-live")
    await cp.close()

    single = f"""\
# durability="sync" — data visible immediately for live queries
cp = SqliteCheckpointer("./workflows.db", durability="sync")
runner = AsyncRunner(checkpointer=cp)
await runner.run(graph, {{"x": 5}}, workflow_id="uc8-live")

# Query from another process while running (or after)
cp2 = SqliteCheckpointer("./workflows.db")
>>> cp2.state("uc8-live")
{state}

# Durability modes control when data is visible:
#   "async"  — background writes (default, good balance)
#   "sync"   — block until written (crash-safe)
#   "exit"   — only at run completion (fastest)"""

    single_cli = run_cli(["workflows", "show", "uc8-live", "--db", db_path])

    # Mapped — runner.map() is ephemeral (no live query)
    runner_sync = SyncRunner()
    results_map = runner_sync.map(graph, {"x": [5, 10]}, map_over="x")
    mapped = f"""\
# runner.map() is ephemeral — no live query from another process
results = runner.map(graph, {{"x": [5, 10]}}, map_over="x")

>>> results.summary()
'{results_map.summary()}'

>>> results["tripled"]
{results_map["tripled"]}

# Not checkpointed — can't query from CLI or another process
# Durability modes only apply to checkpointer-backed runs"""

    # Nested — map_over with durability="sync"
    cp3 = SqliteCheckpointer(db_path, durability="sync")
    runner3 = AsyncRunner(checkpointer=cp3)
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node().map_over("x")])
    await runner3.run(outer, {"x": [5, 10]}, workflow_id="uc8-multi")
    state_m = cp3.state("uc8-multi")
    await cp3.close()

    nested = f"""\
# map_over with durability="sync" — each step visible immediately
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
await runner.run(outer, {{"x": [5, 10]}}, workflow_id="uc8-multi")

>>> cp.state("uc8-multi")
{state_m}"""

    nested_cli = run_cli(["workflows", "show", "uc8-multi", "--db", db_path])

    return UseCase(
        id="uc8",
        label="UC8",
        title='"Where are we right now?"',
        note="Live monitoring, durability modes",
        category="persistence",
        status="ok",
        impl_note="Fully implemented. Durability controls when data is queryable.",
        single_python=single,
        single_cli=f"$ hypergraph workflows show uc8-live --db <db>\n\n{single_cli}",
        mapped_python=mapped,
        mapped_cli="# runner.map() is ephemeral — no CLI queries for live monitoring",
        nested_python=nested,
        nested_cli=f"$ hypergraph workflows show uc8-multi --db <db>\n\n{nested_cli}",
    )


async def run_uc9(db_path: str) -> UseCase:
    """UC9: Fork and retry — checkpoint (deferred: history=)."""
    cp = SqliteCheckpointer(db_path, durability="sync")
    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([double, triple])
    await runner.run(graph, {"x": 5}, workflow_id="uc9-fork")

    checkpoint = cp.checkpoint("uc9-fork")
    await cp.close()

    single = f"""\
# checkpoint() returns state + steps — a snapshot for forking
>>> cp.checkpoint("uc9-fork")
  values: {checkpoint.values}
  steps: {len(checkpoint.steps)}

# Use checkpoint values as inputs to a new run
result = await runner.run(
    updated_graph,
    values={{**checkpoint.values}},
    workflow_id="uc9-fork-retry",
)

# NOT YET: history= parameter for true fork-and-retry
# result = await runner.run(graph, values=..., history=checkpoint.steps)"""

    single_cli = run_cli(["workflows", "state", "uc9-fork", "--values", "--db", db_path])

    # Mapped — runner.map() is ephemeral (can't fork/retry)
    runner_sync = SyncRunner()
    results_map = runner_sync.map(graph, {"x": [5, 10]}, map_over="x")
    mapped = f"""\
# runner.map() is ephemeral — can't fork or retry from a checkpoint
results = runner.map(graph, {{"x": [5, 10]}}, map_over="x")

>>> results.summary()
'{results_map.summary()}'

# No checkpoint data — results exist only in-process
# For fork-and-retry, use map_over with a checkpointer"""

    # Nested — map_over with checkpoint
    cp2 = SqliteCheckpointer(db_path, durability="sync")
    runner2 = AsyncRunner(checkpointer=cp2)
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node().map_over("x")])
    await runner2.run(outer, {"x": [5, 10]}, workflow_id="uc9-multi")
    checkpoint_m = cp2.checkpoint("uc9-multi")
    await cp2.close()

    nested = f"""\
# checkpoint() works for mapped workflows too
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
await runner.run(outer, {{"x": [5, 10]}}, workflow_id="uc9-multi")

>>> cp.checkpoint("uc9-multi")
  values: {checkpoint_m.values}
  steps: {len(checkpoint_m.steps)}"""

    nested_cli = run_cli(["workflows", "state", "uc9-multi", "--values", "--db", db_path])

    return UseCase(
        id="uc9",
        label="UC9",
        title='"Fork and retry from here"',
        note="Checkpoint replay",
        category="persistence",
        status="defer",
        impl_note="Partially implemented. checkpoint() works; history= param deferred to v2.",
        single_python=single,
        single_cli=f"$ hypergraph workflows state uc9-fork --values --db <db>\n\n{single_cli}",
        mapped_python=mapped,
        mapped_cli="# runner.map() runs are ephemeral — no checkpoints to fork from",
        nested_python=nested,
        nested_cli=f"$ hypergraph workflows state uc9-multi --values --db <db>\n\n{nested_cli}",
    )


def run_uc10() -> UseCase:
    """UC10: Process a batch — runner.map()."""
    runner = SyncRunner()
    graph = Graph([double, triple])

    # Single (baseline)
    result = runner.run(graph, {"x": 5})
    single = f"""\
runner = SyncRunner()
graph = Graph([double, triple])
result = runner.run(graph, {{"x": 5}})

>>> result["tripled"]
{result["tripled"]}

>>> result.log.summary()
'{result.log.summary()}'

>>> print(result.log)
{result.log}

>>> result.log.timing
{result.log.timing}"""

    # Mapped — runner.map()
    results = runner.map(graph, {"x": [1, 2, 3, 4, 5]}, map_over="x")
    r0 = results[0]
    mapped = f"""\
# runner.map() — run the same graph on multiple inputs
results = runner.map(graph, {{"x": [1, 2, 3, 4, 5]}}, map_over="x")

>>> results.summary()
'{results.summary()}'

>>> results["tripled"]
{results["tripled"]}

# Drill into one item — same RunLog API as Single
>>> print(results[0].log)
{r0.log}

>>> results[0].log.timing
{r0.log.timing}"""

    # Nested — map_over comparison
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node().map_over("x")])
    result_n = runner.run(outer, {"x": [1, 2, 3, 4, 5]})
    step_n = result_n.log.steps[0]
    log0_n = step_n.log[0]
    nested = f"""\
# map_over — same batch, different semantics
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node().map_over("x")])
result = runner.run(outer, {{"x": [1, 2, 3, 4, 5]}})

>>> result["tripled"]
{result_n["tripled"]}

>>> result.log.summary()
'{result_n.log.summary()}'

# print() reveals "(5 inner)" — there's more inside this step
>>> print(result.log)
{result_n.log}

# Drill into one inner run — same RunLog API as Single
>>> step = result.log.steps[0]
>>> print(step.log[0])
{log0_n}

>>> step.log[0].timing
{log0_n.timing}"""

    return UseCase(
        id="uc10",
        label="UC10",
        title='"Process a batch of items"',
        note="runner.map() → MapResult",
        category="map",
        status="ok",
        impl_note="Fully implemented. MapResult wraps per-item RunResults with batch metadata.",
        single_python=single,
        single_cli="",
        mapped_python=mapped,
        mapped_cli="# runner.map() runs are ephemeral (not checkpointed)\n# Each result has .log for per-item tracing",
        nested_python=nested,
        nested_cli="",
    )


def run_uc11() -> UseCase:
    """UC11: Nested graph over a list — map_over."""
    runner = SyncRunner()

    # Single — normal nested graph
    inner = Graph([double, triple], name="pipeline")
    outer = Graph([inner.as_node()])
    result = runner.run(outer, {"x": 5})
    step_s = result.log.steps[0]
    log0_s = step_s.log
    single = f"""\
# Nested graph: inner runs as a single node in outer
inner = Graph([double, triple], name="pipeline")
outer = Graph([inner.as_node()])
result = runner.run(outer, {{"x": 5}})

>>> result["tripled"]
{result["tripled"]}

>>> result.log.summary()
'{result.log.summary()}'

>>> print(result.log)
{result.log}

# Drill into inner execution
>>> step = result.log.steps[0]
>>> print(step.log)
{log0_s}

>>> step.log.timing
{log0_s.timing}"""

    # Mapped — runner.map() on nested graph
    outer_flat = Graph([inner.as_node()])
    results_m = runner.map(outer_flat, {"x": [1, 2, 3, 4, 5]}, map_over="x")
    r0_m = results_m[0]
    step0_m = r0_m.log.steps[0]
    mapped = f"""\
# runner.map() on a nested graph — N independent runs
outer = Graph([inner.as_node()])
results = runner.map(outer, {{"x": [1, 2, 3, 4, 5]}}, map_over="x")

>>> results.summary()
'{results_m.summary()}'

>>> results["tripled"]
{results_m["tripled"]}

# Drill into one item — same RunLog API as Single
>>> print(results[0].log)
{r0_m.log}

>>> results[0].log.steps[0].log.timing
{step0_m.log.timing}"""

    # Nested — map_over
    outer_m = Graph([inner.as_node().map_over("x")])
    result_m = runner.run(outer_m, {"x": [1, 2, 3, 4, 5]})
    step_m = result_m.log.steps[0]
    log0_m = step_m.log[0]
    nested = f"""\
# map_over — inner graph runs once per item, outputs become lists
outer = Graph([inner.as_node().map_over("x")])
result = runner.run(outer, {{"x": [1, 2, 3, 4, 5]}})

>>> result["tripled"]
{result_m["tripled"]}

>>> result.log.summary()
'{result_m.log.summary()}'

>>> print(result.log)
{result_m.log}

# Drill into one inner run — same RunLog API as Single
>>> step = result.log.steps[0]
>>> print(step.log[0])
{log0_m}

>>> step.log[0].timing
{log0_m.timing}"""

    return UseCase(
        id="uc11",
        label="UC11",
        title='"Nested graph over a list"',
        note="GraphNode.map_over() → list outputs",
        category="map",
        status="ok",
        impl_note="Fully implemented. map_over wraps outputs in list[T] automatically.",
        single_python=single,
        single_cli="",
        mapped_python=mapped,
        mapped_cli="",
        nested_python=nested,
        nested_cli="",
    )


def run_uc12() -> UseCase:
    """UC12: Cartesian product — map_over with mode='product'."""
    runner = SyncRunner()
    inner = Graph([add], name="adder")

    # Single
    outer = Graph([inner.as_node()])
    result = runner.run(outer, {"a": 10, "b": 20})
    single = f"""\
inner = Graph([add], name="adder")
outer = Graph([inner.as_node()])
result = runner.run(outer, {{"a": 10, "b": 20}})

>>> result["sum"]
{result["sum"]}

>>> result.log.summary()
'{result.log.summary()}'

>>> print(result.log)
{result.log}"""

    # Mapped — runner.map() maps one param (no product mode)
    results_m = runner.map(outer, {"a": [1, 2, 3], "b": 10}, map_over="a")
    r0_m = results_m[0]
    mapped = f"""\
# runner.map() maps one param — b is shared across all items
results = runner.map(outer, {{"a": [1, 2, 3], "b": 10}}, map_over="a")

>>> results.summary()
'{results_m.summary()}'

>>> results["sum"]
{results_m["sum"]}

# Drill into one item — same RunLog API as Single
>>> print(results[0].log)
{r0_m.log}"""

    # Nested — product mode
    outer_p = Graph([inner.as_node().map_over("a", "b", mode="product")])
    result_p = runner.run(outer_p, {"a": [1, 2, 3], "b": [10, 20]})

    outer_z = Graph([inner.as_node().map_over("a", "b")])
    result_z = runner.run(outer_z, {"a": [1, 2], "b": [10, 20]})
    nested = f"""\
# mode="product" — cartesian product of mapped params
outer = Graph([inner.as_node().map_over("a", "b", mode="product")])
result = runner.run(outer, {{"a": [1, 2, 3], "b": [10, 20]}})

# 3 × 2 = 6 combinations
>>> result["sum"]
{result_p["sum"]}

>>> result.log.summary()
'{result_p.log.summary()}'

>>> print(result.log)
{result_p.log}

# zip mode (default) requires equal-length lists
outer_zip = Graph([inner.as_node().map_over("a", "b")])
result_zip = runner.run(outer_zip, {{"a": [1, 2], "b": [10, 20]}})
>>> result_zip["sum"]
{result_z["sum"]}"""

    return UseCase(
        id="uc12",
        label="UC12",
        title='"Every combination of inputs"',
        note="map_over mode='product' vs 'zip'",
        category="map",
        status="ok",
        impl_note="Fully implemented. zip (default) for parallel, product for cartesian.",
        single_python=single,
        single_cli="",
        mapped_python=mapped,
        mapped_cli="",
        nested_python=nested,
        nested_cli="",
    )


# ─── HTML generation ─────────────────────────────────────────────────

CSS = """\
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0d1117;--panel:#161b22;--card:#1c2128;--hover:#262c36;
  --border:#30363d;--text:#c9d1d9;--dim:#8b949e;--bright:#f0f6fc;
  --blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
  --purple:#bc8cff;--cyan:#39d2c0;
  --mono:'SF Mono','Cascadia Code','JetBrains Mono',Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
}
body{font-family:var(--sans);background:var(--bg);color:var(--text);height:100vh;overflow:hidden}
.app{display:grid;grid-template-columns:280px 1fr;grid-template-rows:auto 1fr;height:100vh}

/* Header */
.hdr{grid-column:1/-1;padding:10px 20px;border-bottom:1px solid var(--border);background:var(--panel);display:flex;align-items:center;gap:12px}
.hdr h1{font-size:15px;font-weight:600;color:var(--bright)}
.hdr .tag{font-size:10px;padding:2px 8px;border-radius:10px;background:#1f3a5f;color:var(--blue);font-weight:500}
.hdr .sub{margin-left:auto;font-size:11px;color:var(--dim)}

/* Sidebar */
.side{background:var(--panel);border-right:1px solid var(--border);overflow-y:auto;padding:12px}
.side-label{font-size:9px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--dim);margin:12px 0 6px 4px}
.side-label:first-child{margin-top:4px}

.uc{padding:8px 10px;border-radius:6px;cursor:pointer;border:1px solid transparent;margin-bottom:2px;transition:all .12s}
.uc:hover{background:var(--hover)}
.uc.active{background:var(--card);border-color:var(--blue)}
.uc .uc-id{font-size:10px;font-weight:700;color:var(--dim);margin-bottom:1px}
.uc .uc-title{font-size:12px;font-weight:600;color:var(--bright);line-height:1.3}
.uc .uc-note{font-size:10px;color:var(--dim);margin-top:2px}
.uc .pill{display:inline-block;font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600;margin-top:3px}
.pill-ok{background:#0d2818;color:var(--green)}
.pill-gap{background:#3d1f00;color:var(--amber)}
.pill-defer{background:#21162a;color:var(--purple)}

/* Main */
.main{overflow-y:auto;padding:16px 20px 60px;display:flex;flex-direction:column;gap:12px}

/* Toggle */
.toggle-bar{display:flex;gap:0;border-radius:8px;overflow:hidden;border:1px solid var(--border);align-self:flex-start}
.toggle-btn{padding:6px 18px;font-size:12px;font-family:var(--sans);background:var(--card);color:var(--dim);border:none;cursor:pointer;font-weight:600;transition:all .12s}
.toggle-btn.active{background:var(--blue);color:#0d1117}
.toggle-btn + .toggle-btn{border-left:1px solid var(--border)}

/* Section */
.section{background:var(--card);border:1px solid var(--border);border-radius:8px}
.sec-hdr{padding:8px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
.sec-hdr .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sec-hdr h3{font-size:12px;font-weight:600;color:var(--bright)}
.sec-hdr .badge{font-size:9px;padding:1px 6px;border-radius:3px;margin-left:auto;font-weight:600}

pre.code{font-family:var(--mono);font-size:11.5px;line-height:1.55;padding:12px 14px;overflow-x:auto;white-space:pre;color:var(--text);max-height:520px;overflow-y:auto}
pre.code code{font-family:inherit;font-size:inherit;background:none;padding:0}

/* Prism overrides to match our theme */
code[class*="language-"],pre[class*="language-"]{color:var(--text);background:none;font-family:var(--mono);font-size:11.5px;line-height:1.55;text-shadow:none}
.token.comment,.token.prolog,.token.doctype,.token.cdata{color:#6e7681}
.token.punctuation{color:var(--text)}
.token.property,.token.tag,.token.boolean,.token.number,.token.constant,.token.symbol{color:var(--amber)}
.token.selector,.token.attr-name,.token.string,.token.char,.token.builtin{color:var(--green)}
.token.operator,.token.entity,.token.url,.language-css .token.string,.style .token.string{color:var(--text)}
.token.atrule,.token.attr-value,.token.keyword{color:var(--purple)}
.token.function,.token.class-name{color:var(--blue)}
.token.regex,.token.important,.token.variable{color:var(--cyan)}

/* Implementation note */
.impl-note{padding:10px 14px;font-size:11px;line-height:1.5;border-top:1px solid var(--border)}
.impl-note .label{font-weight:700;margin-right:4px}
.impl-note.gap{background:#1a1400;color:var(--amber)}
.impl-note.ok{background:#0a1a0e;color:var(--green)}
.impl-note.defer{background:#150e1e;color:var(--purple)}
"""

RENDER_JS = """\
const CATEGORIES = [
  {key:'tracing', label:'Tracing'},
  {key:'persistence', label:'Persistence'},
  {key:'map', label:'Mapping'},
];

let state = { uc: UCS[0].id, view: 'single' };

function renderSidebar() {
  const sb = document.getElementById('sidebar');
  let h = '';
  for (const cat of CATEGORIES) {
    const items = UCS.filter(u => u.category === cat.key);
    if (!items.length) continue;
    h += `<div class="side-label">${cat.label}</div>`;
    for (const uc of items) {
      const cls = state.uc === uc.id ? ' active' : '';
      const pill = uc.status === 'ok' ? '<span class="pill pill-ok">implemented</span>'
                 : uc.status === 'gap' ? '<span class="pill pill-gap">has gaps</span>'
                 : '<span class="pill pill-defer">v2 deferred</span>';
      h += `<div class="uc${cls}" onclick="select('${uc.id}')">
        <div class="uc-id">${uc.label}</div>
        <div class="uc-title">${uc.title}</div>
        <div class="uc-note">${uc.note}</div>
        ${pill}
      </div>`;
    }
  }
  sb.innerHTML = h;
}

function select(id) { state.uc = id; render(); }
function setView(v) { state.view = v; render(); }

function render() {
  renderSidebar();
  const uc = UCS.find(u => u.id === state.uc);
  const m = document.getElementById('main');
  const v = state.view;

  const implCls = uc.status === 'ok' ? 'ok' : uc.status === 'gap' ? 'gap' : 'defer';
  const implLabel = uc.status === 'ok' ? 'Implemented' : uc.status === 'gap' ? 'Gap' : 'Deferred';

  let html = '';

  // Title + toggle
  html += `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <h2 style="font-size:16px;color:var(--bright)">${uc.label}: ${uc.title}</h2>
    <div class="toggle-bar">
      <button class="toggle-btn${v === 'single' ? ' active' : ''}" onclick="setView('single')">Single</button>
      <button class="toggle-btn${v === 'mapped' ? ' active' : ''}" onclick="setView('mapped')">Mapped</button>
      <button class="toggle-btn${v === 'nested' ? ' active' : ''}" onclick="setView('nested')">Nested</button>
    </div>
  </div>`;

  const pyCode = v === 'single' ? uc.single_python
               : v === 'mapped' ? uc.mapped_python
               : uc.nested_python;
  const cliCode = v === 'single' ? uc.single_cli
                : v === 'mapped' ? uc.mapped_cli
                : uc.nested_cli;

  // Python section
  if (pyCode) {
    const badge = v === 'single'
      ? '<span class="badge" style="background:#0d2818;color:var(--green)">single item</span>'
      : v === 'mapped'
      ? '<span class="badge" style="background:#21162a;color:var(--purple)">runner.map()</span>'
      : '<span class="badge" style="background:#0a2a2a;color:var(--cyan)">map_over</span>';
    html += `<div class="section">
      <div class="sec-hdr">
        <div class="dot" style="background:var(--green)"></div>
        <h3>Python API</h3>
        ${badge}
      </div>
      <pre class="code"><code class="language-python">${pyCode}</code></pre>
    </div>`;
  }

  // CLI section
  if (cliCode) {
    html += `<div class="section">
      <div class="sec-hdr">
        <div class="dot" style="background:var(--cyan)"></div>
        <h3>CLI</h3>
        <span class="badge" style="background:#0a1a2e;color:var(--cyan)">post-hoc</span>
      </div>
      <pre class="code"><code class="language-bash">${cliCode}</code></pre>
    </div>`;
  }

  // Implementation note
  html += `<div class="section">
    <div class="impl-note ${implCls}">
      <span class="label">${implLabel}:</span> ${uc.impl_note}
    </div>
  </div>`;

  m.innerHTML = html;
  if (typeof Prism !== 'undefined') Prism.highlightAllUnder(m);
}

render();
"""


def generate_html(use_cases: list[UseCase]) -> str:
    ucs_data = []
    for uc in use_cases:
        d = asdict(uc)
        # HTML-escape all code strings
        for key in ("single_python", "single_cli", "mapped_python", "mapped_cli", "nested_python", "nested_cli"):
            d[key] = html.escape(d[key])
        ucs_data.append(d)

    ucs_json = json.dumps(ucs_data)
    ucs_json = ucs_json.replace("</", "<\\/")  # XSS prevention

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hypergraph — Use Case Explorer</title>
<style>
{CSS}</style>
</head>
<body>
<div class="app">
<div class="hdr">
  <h1>Hypergraph Use Cases</h1>
  <span class="tag">generated</span>
  <span class="sub">Real output from <code>uv run python examples/generate_playground.py</code></span>
</div>
<div class="side" id="sidebar"></div>
<div class="main" id="main"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1/prism.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1/components/prism-python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1/components/prism-bash.min.js"></script>
<script>
const UCS = {ucs_json};
{RENDER_JS}</script>
</body>
</html>"""


# ─── Main ────────────────────────────────────────────────────────────


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "playground.db")

        print("Running use cases...")
        use_cases = []

        # Sync UCs (no checkpointer needed)
        for fn in [run_uc1, run_uc2, run_uc3, run_uc7, run_uc10, run_uc11, run_uc12]:
            uc = fn()
            print(f"  {uc.label}: {uc.title}")
            use_cases.append(uc)

        # Async UCs (need checkpointer + db)
        for fn in [run_uc4, run_uc5, run_uc6, run_uc8, run_uc9]:
            uc = await fn(db_path)
            print(f"  {uc.label}: {uc.title}")
            use_cases.append(uc)

        # Sort by id
        use_cases.sort(key=lambda u: u.id)

        print(f"\nGenerating HTML ({len(use_cases)} use cases)...")
        html_content = generate_html(use_cases)
        OUTPUT_PATH.write_text(html_content)
        print(f"Written to {OUTPUT_PATH} ({len(html_content):,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
