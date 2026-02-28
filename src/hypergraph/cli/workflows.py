"""Workflow CLI commands: ls, show, state, steps."""

from __future__ import annotations

from typing import Annotated

import typer

from hypergraph.cli._db import _require_aiosqlite, open_checkpointer, run_async
from hypergraph.cli._format import (
    DEFAULT_LIMIT,
    describe_value,
    format_datetime,
    format_duration,
    format_status,
    print_json,
    print_lines,
    print_table,
    truncate_value,
)

app = typer.Typer(help="Inspect and manage workflow executions.")

# Common options
DbOption = Annotated[str, typer.Option("--db", help="Database path")]
JsonFlag = Annotated[bool, typer.Option("--json", help="Output as JSON")]
OutputOption = Annotated[str | None, typer.Option("--output", help="Write JSON to file")]
LimitOption = Annotated[int, typer.Option("--limit", help="Max results")]


@app.callback(invoke_without_command=True)
def workflows_dashboard(
    ctx: typer.Context,
    db: DbOption = "./workflows.db",
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Quick status dashboard: active + recent workflows."""
    if ctx.invoked_subcommand is not None:
        return  # Subcommand will handle it

    _require_aiosqlite()

    async def _run():
        cp = await open_checkpointer(db)
        try:
            all_workflows = await cp.list_workflows()

            if not all_workflows:
                if as_json:
                    print_json("workflows", {"active": [], "recent": []}, output)
                else:
                    print("No workflows found.")
                    print(f"\n  Database: {db}")
                    print("  To create workflows, run a graph with workflow_id parameter.")
                return

            active = [w for w in all_workflows if w.status.value == "active"]
            recent = [w for w in all_workflows if w.status.value != "active"][:5]

            if as_json:
                data = {
                    "active": [w.to_dict() for w in active],
                    "recent": [w.to_dict() for w in recent],
                }
                print_json("workflows", data, output)
                return

            # Human-readable dashboard
            if active:
                print(f"\nActive ({len(active)} running)\n")
                _print_workflow_table(active, cp)
            if recent:
                print(f"\nRecent (last {len(recent)})\n")
                _print_workflow_table(recent, cp)

            # Guidance footer
            print("\n  To inspect a workflow: hypergraph workflows show <id>")
            print("  To see all workflows: hypergraph workflows ls")
            print("  To filter failures:   hypergraph workflows ls --status failed")
        finally:
            await cp.close()

    run_async(_run())


def _print_workflow_table(workflows, cp) -> None:
    """Print a table of workflows with step count info."""

    async def _get_step_counts():
        counts = {}
        for wf in workflows:
            steps = await cp.get_steps(wf.id)
            counts[wf.id] = len(steps)
        return counts

    step_counts = run_async(_get_step_counts())

    headers = ["ID", "Status", "Steps", "Created"]
    rows = []
    for wf in workflows:
        rows.append(
            [
                wf.id,
                format_status(wf.status.value),
                str(step_counts.get(wf.id, 0)),
                format_datetime(wf.created_at),
            ]
        )
    lines = print_table(headers, rows)
    print_lines(lines)


@app.command("ls")
def workflows_ls(
    db: DbOption = "./workflows.db",
    status: Annotated[list[str] | None, typer.Option("--status", help="Filter by status (repeatable)")] = None,
    limit: LimitOption = DEFAULT_LIMIT,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """List workflows with filters."""
    _require_aiosqlite()

    async def _run():
        cp = await open_checkpointer(db)
        try:
            from hypergraph.checkpointers import WorkflowStatus

            if status:
                workflows = []
                for s in status:
                    try:
                        ws = WorkflowStatus(s.lower())
                    except ValueError as e:
                        print(f"Error: Unknown status '{s}'. Use: active, completed, failed")
                        raise typer.Exit(1) from e
                    workflows.extend(await cp.list_workflows(status=ws))
            else:
                workflows = await cp.list_workflows()

            workflows = workflows[:limit]

            if as_json:
                print_json("workflows.ls", [w.to_dict() for w in workflows], output)
                return

            if not workflows:
                print("No workflows found matching filters.")
                return

            print(f"\nWorkflows ({len(workflows)} total)\n")

            headers = ["ID", "Status", "Created"]
            rows = [[wf.id, format_status(wf.status.value), format_datetime(wf.created_at)] for wf in workflows]
            lines = print_table(headers, rows)
            print_lines(lines)

            print("\n  To inspect a workflow: hypergraph workflows show <id>")
            print("  To filter: --status, --limit N")
        finally:
            await cp.close()

    run_async(_run())


@app.command("show")
def workflows_show(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID to show")],
    db: DbOption = "./workflows.db",
    errors_only: Annotated[bool, typer.Option("--errors", help="Only show failed steps")] = False,
    node: Annotated[str | None, typer.Option("--node", help="Filter to specific node")] = None,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show execution trace for a workflow."""
    _require_aiosqlite()

    async def _run():
        cp = await open_checkpointer(db)
        try:
            wf = await cp.get_workflow(workflow_id)
            if wf is None:
                print(f"Error: Workflow '{workflow_id}' not found.")
                raise typer.Exit(1)

            steps = await cp.get_steps(workflow_id)

            # Apply filters
            if errors_only:
                steps = [s for s in steps if s.status.value == "failed"]
            if node:
                steps = [s for s in steps if s.node_name == node]

            if as_json:
                data = {
                    "workflow": wf.to_dict(),
                    "steps": [s.to_dict() for s in steps],
                }
                print_json("workflows.show", data, output)
                return

            # Header
            total_ms = sum(s.duration_ms for s in steps)
            status_str = format_status(wf.status.value)
            print(f"\nWorkflow: {wf.id} | {status_str} | {len(steps)} steps | {format_duration(total_ms)}\n")

            if not steps:
                print("  No steps recorded.")
            else:
                headers = ["Step", "Node", "Duration", "Status", "Decision"]
                rows = []
                for s in steps:
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
                            format_duration(s.duration_ms),
                            status_display,
                            decision,
                        ]
                    )

                lines = print_table(headers, rows)
                print_lines(lines)

            # Guidance footer
            if wf.status.value == "active":
                print("\n  Workflow is still running. Re-run this command to see new steps.")
            print(f"\n  To see values at a step: hypergraph workflows state {workflow_id} --step N")
            print(f"  To see error details:    hypergraph workflows steps {workflow_id} --node <name>")
            print(f"  To save full trace:      hypergraph workflows show {workflow_id} --json --output trace.json")
        finally:
            await cp.close()

    run_async(_run())


@app.command("state")
def workflows_state(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID")],
    db: DbOption = "./workflows.db",
    step: Annotated[int | None, typer.Option("--step", help="State through this step (default: latest)")] = None,
    show_values: Annotated[bool, typer.Option("--values", help="Show actual values")] = False,
    key: Annotated[str | None, typer.Option("--key", help="Show single output value")] = None,
    full: Annotated[bool, typer.Option("--full", help="Don't truncate large values")] = False,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show accumulated values at a point in execution."""
    _require_aiosqlite()

    async def _run():
        cp = await open_checkpointer(db)
        try:
            wf = await cp.get_workflow(workflow_id)
            if wf is None:
                print(f"Error: Workflow '{workflow_id}' not found.")
                raise typer.Exit(1)

            state = await cp.get_state(workflow_id, superstep=step)
            steps = await cp.get_steps(workflow_id, superstep=step)

            if as_json:
                data = {"workflow_id": workflow_id, "through_step": step, "state": state}
                print_json("workflows.state", data, output)
                return

            # Single key mode
            if key:
                if key not in state:
                    print(f"Error: Key '{key}' not found in state. Available: {', '.join(state.keys())}")
                    raise typer.Exit(1)
                value = state[key]
                if full:
                    import json

                    print(json.dumps(value, indent=2, default=str))
                else:
                    print(truncate_value(value, max_chars=500))
                return

            # Status header
            step_label = f"through step {step}" if step is not None else f"through step {len(steps) - 1}" if steps else "no steps"
            status_note = f", {format_status(wf.status.value)}" if wf.status.value == "active" else ""
            print(f"\nState: {workflow_id} ({step_label}{status_note})\n")

            if not state:
                print("  No state values.")
                return

            # Build output-to-step mapping
            output_step_map = {}
            for s in steps:
                if s.values:
                    for k in s.values:
                        output_step_map[k] = (s.index, s.node_name)

            if show_values:
                # Show actual values (with truncation unless --full)
                for k, v in state.items():
                    step_idx, node_name = output_step_map.get(k, ("?", "?"))
                    if full:
                        import json as json_mod

                        formatted = json_mod.dumps(v, indent=2, default=str)
                        print(f"  {k} (step {step_idx}, {node_name}):")
                        for line in formatted.split("\n"):
                            print(f"    {line}")
                    else:
                        print(f"  {k}: {truncate_value(v)}")
            else:
                # Progressive disclosure: type + size only
                headers = ["Output", "Type", "Size", "Step", "Node"]
                rows = []
                for k, v in state.items():
                    type_str, size_str = describe_value(v)
                    step_idx, node_name = output_step_map.get(k, ("?", "?"))
                    rows.append([k, type_str, size_str, str(step_idx), node_name])
                lines = print_table(headers, rows)
                print_lines(lines)

                print("\n  Values hidden. Use --values to show, --key <name> for one value.")

            if wf.status.value == "active":
                print("\n  Workflow is still running. Re-run to see new outputs.")
            print("  To save to file: --output state.json")
        finally:
            await cp.close()

    run_async(_run())


@app.command("steps")
def workflows_steps(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID")],
    db: DbOption = "./workflows.db",
    node: Annotated[str | None, typer.Option("--node", help="Filter to specific node")] = None,
    show_values: Annotated[bool, typer.Option("--values", help="Show actual values")] = False,
    full: Annotated[bool, typer.Option("--full", help="Don't truncate values/errors")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="Show all steps")] = False,
    limit: LimitOption = DEFAULT_LIMIT,
    as_json: JsonFlag = False,
    output: OutputOption = None,
):
    """Show detailed step records."""
    _require_aiosqlite()

    async def _run():
        cp = await open_checkpointer(db)
        try:
            wf = await cp.get_workflow(workflow_id)
            if wf is None:
                print(f"Error: Workflow '{workflow_id}' not found.")
                raise typer.Exit(1)

            steps = await cp.get_steps(workflow_id)

            if node:
                steps = [s for s in steps if s.node_name == node]

            total = len(steps)
            if not show_all:
                steps = steps[:limit]

            if as_json:
                print_json("workflows.steps", [s.to_dict() for s in steps], output)
                return

            if not steps:
                if node:
                    print(f"No steps found for node '{node}' in workflow '{workflow_id}'.")
                else:
                    print(f"No steps found for workflow '{workflow_id}'.")
                return

            for s in steps:
                status_str = format_status(s.status.value)
                print(f"\nStep [{s.index}] {s.node_name} | {status_str} | {format_duration(s.duration_ms)}")
                print(f"  input_versions: {s.input_versions}")

                if s.values is not None:
                    if show_values:
                        if full:
                            import json

                            print(f"  values: {json.dumps(s.values, indent=4, default=str)}")
                        else:
                            print(f"  values: {truncate_value(s.values)}")
                    else:
                        # Show type/size summary
                        summaries = {}
                        for k, v in s.values.items():
                            type_str, size_str = describe_value(v)
                            summaries[k] = f"<{type_str}, {size_str}>"
                        print(f"  values: {summaries}")

                print(f"  cached: {s.cached}")

                if s.decision is not None:
                    print(f"  decision: {s.decision}")
                if s.error:
                    if full:
                        print(f"  error: {s.error}")
                    else:
                        error_str = s.error if isinstance(s.error, str) else str(s.error)
                        print(f"  error: {error_str[:100]}")
                if s.created_at:
                    print(f"  created_at: {format_datetime(s.created_at)}")
                if s.completed_at:
                    print(f"  completed_at: {format_datetime(s.completed_at)}")

            shown = len(steps)
            if shown < total:
                print(f"\n  Showing {shown} of {total} steps. Use --all to see all, --node <name> to filter.")

            print("  To see actual values: --values | To see full error traces: --full")
            print("  To save all step records: --json --output steps.json")
        finally:
            await cp.close()

    run_async(_run())
