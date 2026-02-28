"""Run inspection CLI commands: ls, show, values, steps, search, stats."""

from __future__ import annotations

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
DbOption = Annotated[str, typer.Option("--db", help="Database path")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Output as JSON")]
OutputOption = Annotated[str | None, typer.Option("--output", help="Write JSON to file")]
LimitOption = Annotated[int, typer.Option("--limit", help="Max results")]


@app.callback(invoke_without_command=True)
def runs_dashboard(
    ctx: typer.Context,
    db: DbOption = "./runs.db",
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Quick status dashboard: active + recent runs."""
    if ctx.invoked_subcommand is not None:
        return

    cp = open_checkpointer(db)
    all_runs = cp.runs()

    if not all_runs:
        if as_json:
            print_json("runs", {"active": [], "recent": []}, output)
        else:
            print("No runs found.")
            print(f"\n  Database: {db}")
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


@app.command("ls")
def runs_ls(
    db: DbOption = "./runs.db",
    status: Annotated[list[str] | None, typer.Option("--status", help="Filter by status (repeatable)")] = None,
    graph: Annotated[str | None, typer.Option("--graph", help="Filter by graph name")] = None,
    since: Annotated[str | None, typer.Option("--since", help="Filter by time (e.g. 1h, 7d, 2w)")] = None,
    limit: LimitOption = DEFAULT_LIMIT,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """List runs with filters."""
    cp = open_checkpointer(db)
    from hypergraph.checkpointers import WorkflowStatus

    filter_kwargs: dict = {"limit": limit}
    if graph:
        filter_kwargs["graph_name"] = graph
    if since:
        filter_kwargs["since"] = parse_since(since)

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
        run_list = sorted(run_list, key=lambda r: r.created_at or "", reverse=True)[:limit]
    else:
        run_list = cp.runs(**filter_kwargs)

    if as_json:
        print_json("runs.ls", [r.to_dict() for r in run_list], output)
        return

    if not run_list:
        print("No runs found matching filters.")
        return

    print(f"\nRuns ({len(run_list)} total)\n")

    headers = ["ID", "Graph", "Status", "Duration", "Created"]
    rows = [
        [
            r.id,
            r.graph_name or "—",
            format_status(r.status.value),
            format_duration(r.duration_ms),
            format_datetime(r.created_at),
        ]
        for r in run_list
    ]
    lines = print_table(headers, rows)
    print_lines(lines)

    print_ctas(
        [
            "hypergraph runs show <id>      to inspect a run",
            "hypergraph runs ls --graph <name> --since 1h  to narrow results",
        ]
    )


@app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run ID to show")],
    db: DbOption = "./runs.db",
    step: Annotated[int | None, typer.Option("--step", help="Show specific step by index")] = None,
    show_values: Annotated[bool, typer.Option("--values", help="Show step output values")] = False,
    errors_only: Annotated[bool, typer.Option("--errors", help="Only show failed steps")] = False,
    node: Annotated[str | None, typer.Option("--node", help="Filter to specific node")] = None,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show execution trace for a run."""
    cp = open_checkpointer(db)
    r = cp.run(run_id)
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

    if not step_list:
        print("  No steps recorded.")
    else:
        headers = ["Step", "Node", "Type", "Duration", "Status", "Decision"]
        rows = []
        for s in step_list:
            decision = ""
            if s.decision is not None:
                decision = "→ " + ", ".join(s.decision) if isinstance(s.decision, list) else f"→ {s.decision}"

            status_display = format_status(s.status.value)
            if s.error and s.status.value == "failed":
                error_line = s.error if isinstance(s.error, str) else str(s.error)
                status_display = f"FAILED: {error_line[:60]}"

            rows.append(
                [
                    str(s.index),
                    s.node_name,
                    s.node_type or "—",
                    format_duration(s.duration_ms),
                    status_display,
                    decision,
                ]
            )

        lines = print_table(headers, rows)
        print_lines(lines)

    # CTAs
    ctas = [
        f"hypergraph runs values {run_id}            for output values",
        f"hypergraph runs stats {run_id}             for performance breakdown",
    ]
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
    db: DbOption = "./runs.db",
    superstep: Annotated[int | None, typer.Option("--superstep", help="State through this superstep")] = None,
    key: Annotated[str | None, typer.Option("--key", help="Show single output value")] = None,
    full: Annotated[bool, typer.Option("--full", help="Don't truncate large values")] = False,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show accumulated output values for a run."""
    cp = open_checkpointer(db)
    r = cp.run(run_id)
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
        ]
    )


@app.command("steps")
def runs_steps(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    db: DbOption = "./runs.db",
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
    r = cp.run(run_id)
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
        ]
    )


@app.command("search")
def runs_search(
    query: Annotated[str, typer.Argument(help="Search query (matches node names and errors)")],
    db: DbOption = "./runs.db",
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
        ]
    )


@app.command("stats")
def runs_stats(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    db: DbOption = "./runs.db",
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show per-node performance statistics for a run."""
    cp = open_checkpointer(db)
    r = cp.run(run_id)
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

    headers = ["Node", "Type", "Runs", "Total", "Avg", "Max", "Errors", "Cached"]
    rows = [
        [
            name,
            stats.get("node_type") or "—",
            str(stats["executions"]),
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
            f"hypergraph runs show {run_id}     for full execution trace",
            f"hypergraph runs values {run_id}   for output values",
        ]
    )
