"""Run inspection CLI commands: ls, show, values, steps, search, stats."""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated

import typer

from hypergraph.cli._db import open_checkpointer
from hypergraph.cli._format import (
    DEFAULT_LIMIT,
    describe_value,
    format_datetime,
    format_duration,
    format_status,
    parse_since,
    print_ctas,
    print_json,
    print_lines,
    print_table,
    truncate_value,
)

app = typer.Typer(help="Inspect and manage graph runs.")

# Common options
DbOption = Annotated[str | None, typer.Option("--db", help="Database path")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Output as JSON")]
OutputOption = Annotated[str | None, typer.Option("--output", help="Write JSON to file")]
LimitOption = Annotated[int, typer.Option("--limit", help="Max results")]


def _sort_runs(run_list: list, sort: str) -> list:
    """Sort runs with stable, user-facing order modes."""
    mode = sort.lower()
    if mode == "newest":
        return sorted(run_list, key=lambda r: (r.created_at is not None, r.created_at), reverse=True)
    if mode == "oldest":
        return sorted(run_list, key=lambda r: (r.created_at is not None, r.created_at))
    if mode == "duration":
        return sorted(run_list, key=lambda r: (r.duration_ms or 0.0, r.created_at), reverse=True)
    if mode == "errors":
        return sorted(run_list, key=lambda r: (r.error_count, r.created_at), reverse=True)
    if mode == "id":
        return sorted(run_list, key=lambda r: r.id)
    raise ValueError(f"Unknown sort mode: {sort!r}")


def _print_run_traces(run_list: list) -> None:
    """Print grouped run traces (parent + children summary) below ls table."""

    def _group_key(run) -> str:
        if run.parent_run_id:
            return run.parent_run_id
        if "/" in run.id:
            return run.id.split("/", 1)[0]
        return run.id

    grouped = defaultdict(list)
    by_id = {run.id: run for run in run_list}
    for run in run_list:
        grouped[_group_key(run)].append(run)

    if not grouped:
        return

    print("\nRun Traces\n")
    for group_id in sorted(grouped.keys()):
        members = grouped[group_id]
        parent = by_id.get(group_id)
        children = sorted(
            [r for r in members if r.id != group_id],
            key=lambda r: (r.created_at is not None, r.created_at),
            reverse=True,
        )
        if parent is None:
            parent = max(
                members,
                key=lambda r: (
                    r.status.value == "active",
                    r.status.value == "failed",
                    r.created_at is not None,
                    r.created_at,
                ),
            )

        child_label = f" | {len(children)} child runs" if children else ""
        print(
            f"  {group_id} | {format_status(parent.status.value)} | {format_duration(parent.duration_ms)}"
            f" | {parent.node_count} steps | {parent.error_count} errors{child_label}"
        )

        if children:
            child_rows = [
                [c.id, format_status(c.status.value), format_duration(c.duration_ms), str(c.node_count), str(c.error_count)] for c in children
            ]
            print_lines(print_table(["Child", "Status", "Duration", "Steps", "Errors"], child_rows, indent=4))
        print()


@app.callback(invoke_without_command=True)
def runs_dashboard(
    ctx: typer.Context,
    db: DbOption = None,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Quick status dashboard: active + recent runs."""
    if ctx.invoked_subcommand is not None:
        return

    cp = open_checkpointer(db)
    all_runs = cp.runs(parent_run_id=None)

    if not all_runs:
        if as_json:
            print_json("runs", {"active": [], "recent": []}, output)
        else:
            print("No runs found.")
            print(f"\n  Database: {cp._path}")
            print("  To create runs, use: runner.run(graph, inputs, workflow_id='my-run')")
        return

    active = [r for r in all_runs if r.status.value == "active"]
    recent = [r for r in all_runs if r.status.value != "active"][:5]

    if as_json:
        data = {
            "active": [r.to_dict() for r in active],
            "recent": [r.to_dict() for r in recent],
        }
        print_json("runs", data, output)
        return

    if active:
        print(f"\nActive ({len(active)} running)\n")
        _print_run_table(active, cp)
    if recent:
        print(f"\nRecent (last {len(recent)})\n")
        _print_run_table(recent, cp)

    print_ctas(
        [
            "hypergraph runs ls                 for all runs",
            "hypergraph runs ls --status failed  for failures only",
            "hypergraph runs show <id>           to inspect a run",
        ]
    )


def _print_run_table(run_list, cp) -> None:
    """Print a table of runs with step count info."""
    step_counts = {r.id: len(cp.steps(r.id)) for r in run_list}

    headers = ["ID", "Graph", "Status", "Steps", "Duration", "Created"]
    rows = [
        [
            r.id,
            r.graph_name or "—",
            format_status(r.status.value),
            str(step_counts.get(r.id, 0)),
            format_duration(r.duration_ms),
            format_datetime(r.created_at),
        ]
        for r in run_list
    ]
    lines = print_table(headers, rows)
    print_lines(lines)


def _dump_cta(command: str, filename: str) -> str:
    """Standard CTA for dumping structured output to a file."""
    return f"{command} --json --output {filename}"


@app.command("ls")
def runs_ls(
    db: DbOption = None,
    status: Annotated[list[str] | None, typer.Option("--status", help="Filter by status (repeatable)")] = None,
    graph: Annotated[str | None, typer.Option("--graph", help="Filter by graph name")] = None,
    since: Annotated[str | None, typer.Option("--since", help="Filter by time (e.g. 1h, 7d, 2w)")] = None,
    view: Annotated[str, typer.Option("--view", help="Hierarchy view: parents or all")] = "parents",
    sort: Annotated[str, typer.Option("--sort", help="Sort: newest, oldest, duration, errors, id")] = "newest",
    traces: Annotated[bool, typer.Option("--traces", help="Show grouped run traces below the table")] = False,
    parent: Annotated[str | None, typer.Option("--parent", help="Show children of this run")] = None,
    show_all: Annotated[bool, typer.Option("--all", help="Show all runs (including children)")] = False,
    limit: LimitOption = DEFAULT_LIMIT,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """List runs with filters. Shows top-level runs by default."""
    cp = open_checkpointer(db)
    from hypergraph.checkpointers import WorkflowStatus

    view = view.lower()
    if view not in {"parents", "all"}:
        print(f"Error: Unknown view '{view}'. Use: parents, all")
        raise typer.Exit(1)

    sort = sort.lower()
    if sort not in {"newest", "oldest", "duration", "errors", "id"}:
        print(f"Error: Unknown sort '{sort}'. Use: newest, oldest, duration, errors, id")
        raise typer.Exit(1)

    filter_kwargs: dict = {"limit": limit}
    if graph:
        filter_kwargs["graph_name"] = graph
    if since:
        filter_kwargs["since"] = parse_since(since)

    # Hierarchy filtering: --parent takes precedence, --all/--view all shows everything,
    # default shows top-level only
    if parent:
        filter_kwargs["parent_run_id"] = parent
    elif not show_all and view == "parents":
        filter_kwargs["parent_run_id"] = None

    if status:
        # When status filter is given, query per-status and merge (preserving graph/since)
        run_list = []
        for s in status:
            try:
                ws = WorkflowStatus(s.lower())
            except ValueError as e:
                print(f"Error: Unknown status '{s}'. Use: active, completed, failed")
                raise typer.Exit(1) from e
            run_list.extend(cp.runs(status=ws, **filter_kwargs))
        # Deduplicate by ID before sorting/limiting.
        run_list = list({r.id: r for r in run_list}.values())
    else:
        run_list = cp.runs(**filter_kwargs)

    run_list = _sort_runs(run_list, sort)[:limit]

    if as_json:
        print_json("runs.ls", [r.to_dict() for r in run_list], output)
        return

    if not run_list:
        print("No runs found matching filters.")
        return

    print(f"\nRuns ({len(run_list)} total)\n")

    headers = ["ID", "Graph", "Status", "Duration", "Steps", "Errors", "Created"]
    rows = [
        [
            r.id,
            r.graph_name or "—",
            format_status(r.status.value),
            format_duration(r.duration_ms),
            str(r.node_count),
            str(r.error_count),
            format_datetime(r.created_at),
        ]
        for r in run_list
    ]
    lines = print_table(headers, rows)
    print_lines(lines)

    if traces:
        _print_run_traces(run_list)

    ctas = [
        "hypergraph runs show <id>           to inspect a run",
        "hypergraph runs ls --graph <name>   to narrow results",
    ]
    if not show_all and not parent and view == "parents":
        ctas.append("hypergraph runs ls --all            to include child runs")
    if not traces:
        ctas.append("hypergraph runs ls --traces         for grouped parent/child breakdown")
    print_ctas(ctas)


@app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run ID to show")],
    db: DbOption = None,
    step: Annotated[int | None, typer.Option("--step", help="Show specific step by index")] = None,
    show_values: Annotated[bool, typer.Option("--values", help="Show step output values")] = False,
    errors_only: Annotated[bool, typer.Option("--errors", help="Only show failed steps")] = False,
    node: Annotated[str | None, typer.Option("--node", help="Filter to specific node")] = None,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show run trace for a run."""
    cp = open_checkpointer(db)
    r = cp.get_run(run_id)
    if r is None:
        print(f"Error: Run '{run_id}' not found.")
        raise typer.Exit(1)

    step_list = cp.steps(run_id)

    # Single step mode
    if step is not None:
        matching = [s for s in step_list if s.index == step]
        if not matching:
            print(f"Error: Step {step} not found in run '{run_id}'.")
            raise typer.Exit(1)
        s = matching[0]
        if as_json:
            print_json("runs.show", s.to_dict(), output)
            return
        _print_step_detail(s, show_values)
        print_ctas(
            [
                f"hypergraph runs show {run_id} --step {step} --values  to see output values",
                f"hypergraph runs values {run_id}                       for accumulated state",
                _dump_cta(f"hypergraph runs show {run_id}", f"/tmp/{run_id}.show.step{step}.json"),
            ]
        )
        return

    # Apply filters
    if errors_only:
        step_list = [s for s in step_list if s.status.value == "failed"]
    if node:
        step_list = [s for s in step_list if s.node_name == node]

    if as_json:
        data = {
            "run": r.to_dict(),
            "steps": [s.to_dict() for s in step_list],
        }
        print_json("runs.show", data, output)
        return

    # Header
    total_ms = r.duration_ms or sum(s.duration_ms for s in step_list)
    status_str = format_status(r.status.value)
    graph_label = f" ({r.graph_name})" if r.graph_name else ""
    print(f"\nRun: {r.id}{graph_label} | {status_str} | {len(step_list)} steps | {format_duration(total_ms)}\n")
    if r.retry_of:
        retry_num = f"#{r.retry_index}" if r.retry_index is not None else "?"
        print(f"  Lineage: retry {retry_num} of {r.retry_of}")
    elif r.forked_from:
        at = f"@{r.fork_superstep}" if r.fork_superstep is not None else ""
        print(f"  Lineage: forked from {r.forked_from}{at}")
    if r.retry_of or r.forked_from:
        print()

    # Check for child runs (batch parent or nested graph parent)
    children = cp.runs(parent_run_id=run_id)

    if not step_list:
        print("  No steps recorded.")
    else:
        has_child_runs = any(s.child_run_id for s in step_list)
        headers = ["Step", "Node", "Type", "Duration", "Status", "Decision"]
        if has_child_runs:
            headers.append("Child Run")
        rows = []
        for s in step_list:
            decision = ""
            if s.decision is not None:
                decision = "→ " + ", ".join(s.decision) if isinstance(s.decision, list) else f"→ {s.decision}"

            status_display = format_status(s.status.value)
            if s.error and s.status.value == "failed":
                error_line = s.error if isinstance(s.error, str) else str(s.error)
                status_display = f"FAILED: {error_line[:60]}"

            row = [
                str(s.index),
                s.node_name,
                s.node_type or "—",
                format_duration(s.duration_ms),
                status_display,
                decision,
            ]
            if has_child_runs:
                row.append(s.child_run_id or "—")
            rows.append(row)

        lines = print_table(headers, rows)
        print_lines(lines)

    # Show children summary for batch parents
    if children:
        completed = sum(1 for c in children if c.status.value == "completed")
        failed = sum(1 for c in children if c.status.value == "failed")
        print(f"\n  Children: {len(children)} ({completed} completed, {failed} failed)")

    # CTAs
    ctas = [
        f"hypergraph runs values {run_id}            for output values",
        f"hypergraph runs stats {run_id}             for performance breakdown",
        _dump_cta(f"hypergraph runs show {run_id}", f"/tmp/{run_id}.show.json"),
    ]
    if children:
        ctas.insert(0, f"hypergraph runs ls --parent {run_id}   to list child runs")
    if any(s.status.value == "failed" for s in step_list):
        ctas.insert(0, f"hypergraph runs show {run_id} --errors   for failures only")
    if r.status.value == "active":
        ctas.insert(0, "Run is still active. Re-run to see new steps.")
    print_ctas(ctas)


def _print_step_detail(s, show_values: bool) -> None:
    """Print a single step's detail."""
    status_str = format_status(s.status.value)
    print(f"\nStep [{s.index}] {s.node_name} | {status_str} | {format_duration(s.duration_ms)}")
    if s.node_type:
        print(f"  type: {s.node_type}")
    print(f"  input_versions: {s.input_versions}")

    if s.values is not None:
        if show_values:
            import json

            print(f"  values: {json.dumps(s.values, indent=4, default=str)}")
        else:
            summaries = {}
            for k, v in s.values.items():
                type_str, size_str = describe_value(v)
                summaries[k] = f"<{type_str}, {size_str}>"
            print(f"  values: {summaries}")

    print(f"  cached: {s.cached}")
    if s.decision is not None:
        print(f"  decision: {s.decision}")
    if s.error:
        print(f"  error: {s.error}")
    if s.created_at:
        print(f"  created_at: {format_datetime(s.created_at)}")
    if s.completed_at:
        print(f"  completed_at: {format_datetime(s.completed_at)}")


@app.command("values")
def runs_values(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    db: DbOption = None,
    superstep: Annotated[int | None, typer.Option("--superstep", help="State through this superstep")] = None,
    key: Annotated[str | None, typer.Option("--key", help="Show single output value")] = None,
    full: Annotated[bool, typer.Option("--full", help="Don't truncate large values")] = False,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show accumulated output values for a run."""
    cp = open_checkpointer(db)
    r = cp.get_run(run_id)
    if r is None:
        print(f"Error: Run '{run_id}' not found.")
        raise typer.Exit(1)

    current_state = cp.state(run_id, superstep=superstep)
    step_list = cp.steps(run_id, superstep=superstep)

    if as_json:
        data = {"run_id": run_id, "through_superstep": superstep, "values": current_state}
        print_json("runs.values", data, output)
        return

    # Single key mode
    if key:
        if key not in current_state:
            print(f"Error: Key '{key}' not found. Available: {', '.join(current_state.keys())}")
            raise typer.Exit(1)
        value = current_state[key]
        if full:
            import json

            print(json.dumps(value, indent=2, default=str))
        else:
            print(truncate_value(value, max_chars=500))
        return

    # Status header
    step_label = f"through superstep {superstep}" if superstep is not None else f"through step {len(step_list) - 1}" if step_list else "no steps"
    status_note = f", {format_status(r.status.value)}" if r.status.value == "active" else ""
    print(f"\nValues: {run_id} ({step_label}{status_note})\n")

    if not current_state:
        print("  No output values.")
        return

    # Build output-to-step mapping
    output_step_map = {}
    for s in step_list:
        if s.values:
            for k in s.values:
                output_step_map[k] = (s.index, s.node_name)

    # Show type/size table with values column if --full
    headers = ["Output", "Type", "Size", "Step", "Node"]
    if full:
        headers.append("Value")
    rows = []
    for k, v in current_state.items():
        type_str, size_str = describe_value(v)
        step_idx, node_name = output_step_map.get(k, ("?", "?"))
        row = [k, type_str, size_str, str(step_idx), node_name]
        if full:
            row.append(truncate_value(v, max_chars=80))
        rows.append(row)
    lines = print_table(headers, rows)
    print_lines(lines)

    print_ctas(
        [
            f"hypergraph runs values {run_id} --key <name>  for a single value",
            f"hypergraph runs values {run_id} --full        to show values inline",
            f"hypergraph runs values {run_id} --json        for full JSON",
            _dump_cta(f"hypergraph runs values {run_id}", f"/tmp/{run_id}.values.json"),
        ]
    )


@app.command("steps")
def runs_steps(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    db: DbOption = None,
    node: Annotated[str | None, typer.Option("--node", help="Filter to specific node")] = None,
    show_values: Annotated[bool, typer.Option("--values", help="Show actual values")] = False,
    full: Annotated[bool, typer.Option("--full", help="Don't truncate values/errors")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="Show all steps")] = False,
    limit: LimitOption = DEFAULT_LIMIT,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show detailed step records."""
    cp = open_checkpointer(db)
    r = cp.get_run(run_id)
    if r is None:
        print(f"Error: Run '{run_id}' not found.")
        raise typer.Exit(1)

    step_list = cp.steps(run_id)

    if node:
        step_list = [s for s in step_list if s.node_name == node]

    total = len(step_list)
    if not show_all:
        step_list = step_list[:limit]

    if as_json:
        print_json("runs.steps", [s.to_dict() for s in step_list], output)
        return

    if not step_list:
        if node:
            print(f"No steps found for node '{node}' in run '{run_id}'.")
        else:
            print(f"No steps found for run '{run_id}'.")
        return

    for s in step_list:
        _print_step_detail(s, show_values)

    shown = len(step_list)
    if shown < total:
        print(f"\n  Showing {shown} of {total} steps. Use --all to see all, --node <name> to filter.")

    print_ctas(
        [
            f"hypergraph runs steps {run_id} --values --full  for all values",
            f"hypergraph runs steps {run_id} --node <name>    for a specific node",
            f"hypergraph runs steps {run_id} --json           for JSON export",
            _dump_cta(f"hypergraph runs steps {run_id}", f"/tmp/{run_id}.steps.json"),
        ]
    )


@app.command("search")
def runs_search(
    query: Annotated[str, typer.Argument(help="Search query (matches node names and errors)")],
    db: DbOption = None,
    field: Annotated[str | None, typer.Option("--field", help="Limit search to field (node_name or error)")] = None,
    limit: LimitOption = DEFAULT_LIMIT,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Search step records using full-text search."""
    cp = open_checkpointer(db)
    results = cp.search(query, field=field, limit=limit)

    if as_json:
        print_json("runs.search", [s.to_dict() for s in results], output)
        return

    if not results:
        print(f"No steps matching '{query}'.")
        return

    print(f"\nSearch: '{query}' ({len(results)} matches)\n")

    headers = ["Run", "Step", "Node", "Status", "Error"]
    rows = [
        [
            s.run_id,
            str(s.index),
            s.node_name,
            format_status(s.status.value),
            (s.error[:50] if s.error else "—"),
        ]
        for s in results
    ]
    lines = print_table(headers, rows)
    print_lines(lines)

    print_ctas(
        [
            "hypergraph runs show <run-id>           to inspect a matching run",
            f'hypergraph runs search "{query}" --field error  to search errors only',
            _dump_cta(f'hypergraph runs search "{query}"', "/tmp/runs.search.json"),
        ]
    )


@app.command("stats")
def runs_stats(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    db: DbOption = None,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show per-node performance statistics for a run."""
    cp = open_checkpointer(db)
    r = cp.get_run(run_id)
    if r is None:
        print(f"Error: Run '{run_id}' not found.")
        raise typer.Exit(1)

    node_stats = cp.stats(run_id)

    if as_json:
        print_json("runs.stats", {"run_id": run_id, "nodes": node_stats}, output)
        return

    if not node_stats:
        print(f"No stats for run '{run_id}'.")
        return

    status_str = format_status(r.status.value)
    print(f"\nStats: {run_id} | {status_str}\n")

    headers = ["Node", "Type", "Steps", "Total", "Avg", "Max", "Errors", "Cached"]
    rows = [
        [
            name,
            stats.get("node_type") or "—",
            str(stats["steps"]),
            format_duration(stats["total_ms"]),
            format_duration(stats["avg_ms"]),
            format_duration(stats["max_ms"]),
            str(stats["errors"]),
            str(stats["cache_hits"]),
        ]
        for name, stats in node_stats.items()
    ]
    lines = print_table(headers, rows)
    print_lines(lines)

    print_ctas(
        [
            f"hypergraph runs show {run_id}     for full run trace",
            f"hypergraph runs values {run_id}   for output values",
            _dump_cta(f"hypergraph runs stats {run_id}", f"/tmp/{run_id}.stats.json"),
        ]
    )


@app.command("checkpoint")
def runs_checkpoint(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    db: DbOption = None,
    superstep: Annotated[int | None, typer.Option("--superstep", help="Checkpoint through this superstep")] = None,
    deep: Annotated[bool, typer.Option("--deep", help="Include full values + step list")] = False,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Inspect a checkpoint snapshot for a run."""
    cp = open_checkpointer(db)
    run = cp.get_run(run_id)
    if run is None:
        print(f"Error: Run '{run_id}' not found.")
        raise typer.Exit(1)

    checkpoint = cp.checkpoint(run_id, superstep=superstep)
    values = checkpoint.values
    steps = checkpoint.steps

    if as_json:
        data = {
            "source_run_id": checkpoint.source_run_id,
            "source_superstep": checkpoint.source_superstep,
            "value_count": len(values),
            "step_count": len(steps),
            "values": values if deep else None,
            "steps": [s.to_dict() for s in steps] if deep else None,
        }
        print_json("runs.checkpoint", data, output)
        return

    source = checkpoint.source_run_id or run_id
    source_at = f"{source}@{checkpoint.source_superstep}" if checkpoint.source_superstep is not None else source
    print(f"\nCheckpoint: {source_at}\n")
    print(f"  Values: {len(values)}")
    print(f"  Steps: {len(steps)}")

    if deep:
        print("\nValues:")
        for key in sorted(values.keys()):
            print(f"  - {key}")
        print("\nSteps:")
        for s in steps:
            status = "cached" if s.cached else s.status.value
            print(f"  - [{s.index}] {s.node_name} ({status}, superstep={s.superstep})")

    print_ctas(
        [
            f"hypergraph runs values {run_id} --superstep {superstep if superstep is not None else 0}   read state at a cut",
            f"hypergraph runs steps {run_id} --all                                         inspect full step log",
            f"hypergraph runs checkpoint {run_id} --deep                                   expand snapshot details",
            _dump_cta(f"hypergraph runs checkpoint {run_id}", f"/tmp/{run_id}.checkpoint.json"),
        ]
    )


@app.command("lineage")
def runs_lineage(
    run_id: Annotated[str, typer.Argument(help="Workflow ID to inspect lineage for")],
    db: DbOption = None,
    deep: Annotated[bool, typer.Option("--deep", help="Include step-level drilldown per run")] = False,
    max_runs: Annotated[int, typer.Option("--max-runs", help="Maximum lineage runs to traverse")] = 200,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show git-like fork lineage for a workflow."""
    cp = open_checkpointer(db)
    try:
        lineage = cp.lineage(run_id, include_steps=deep, max_runs=max_runs)
    except ValueError as e:
        print(f"Error: {e}")
        raise typer.Exit(1) from e

    if as_json:
        rows = []
        for row in lineage:
            item = {
                "lane": row.lane,
                "depth": row.depth,
                "is_selected": row.is_selected,
                "run": row.run.to_dict(),
            }
            if deep and row.run.id in lineage.steps_by_run:
                item["steps"] = [s.to_dict() for s in lineage.steps_by_run[row.run.id]]
            rows.append(item)
        print_json(
            "runs.lineage",
            {
                "selected_run_id": lineage.selected_run_id,
                "root_run_id": lineage.root_run_id,
                "rows": rows,
            },
            output,
        )
        return

    print(f"\nLineage: selected={lineage.selected_run_id} root={lineage.root_run_id}\n")
    for row in lineage:
        run = row.run
        marker = " <selected>" if row.is_selected else ""
        kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
        origin = ""
        if run.forked_from:
            at = f"@{run.fork_superstep}" if run.fork_superstep is not None else ""
            origin = f" <- {run.forked_from}{at}"
        print(f"{row.lane}{run.id} [{run.status.value}] ({kind}){origin}{marker}")
        if deep and run.id in lineage.steps_by_run:
            steps = lineage.steps_by_run[run.id]
            cached = sum(1 for s in steps if s.cached)
            failed = sum(1 for s in steps if s.status.value == "failed")
            print(f"   steps={len(steps)} cached={cached} failed={failed}")
            for s in steps[:5]:
                status = "cached" if s.cached else s.status.value
                print(f"     - [{s.index}] {s.node_name} ({status}, superstep={s.superstep})")
            if len(steps) > 5:
                print(f"     ... and {len(steps) - 5} more (use: hypergraph runs steps {run.id} --all)")

    print_ctas(
        [
            f"hypergraph runs lineage {run_id} --deep                                    include per-run step drilldown",
            f"hypergraph runs show {run_id}                                                inspect selected workflow trace",
            "hypergraph runs ls --all                                                     include child/fork runs",
            _dump_cta(f"hypergraph runs lineage {run_id}", f"/tmp/{run_id}.lineage.json"),
        ]
    )
