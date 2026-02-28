"""CLI commands for running and mapping graphs.

Provides `hypergraph run` and `hypergraph map` as top-level commands.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from hypergraph.cli._config import load_config
from hypergraph.cli._format import print_json, truncate_value
from hypergraph.cli.graph_cmd import load_graph

# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def _parse_literal(raw: str) -> Any:
    """Parse a string as a Python literal, falling back to str."""
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_kv_args(args: list[str]) -> dict[str, Any]:
    """Parse ['x=5', 'y=[1,2]'] into {'x': 5, 'y': [1, 2]}."""
    result: dict[str, Any] = {}
    for arg in args:
        if "=" not in arg:
            print(f"Error: Expected key=value, got '{arg}'")
            raise typer.Exit(1)
        key, _, raw_value = arg.partition("=")
        result[key] = _parse_literal(raw_value)
    return result


def _load_values_source(source: str) -> dict[str, Any]:
    """Load values from a JSON string or file path."""
    path = Path(source)
    if path.is_file():
        with open(path) as f:
            return json.load(f)
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        print(f"Error: '{source}' is not valid JSON and not a file path")
        raise typer.Exit(1) from None


def _resolve_values(values_option: str | None, extra_args: list[str]) -> dict[str, Any]:
    """Merge --values source with key=value positional args.

    Layering: --values is the base, key=value args override.
    """
    result: dict[str, Any] = {}
    if values_option:
        result.update(_load_values_source(values_option))
    if extra_args:
        result.update(_parse_kv_args(extra_args))
    return result


# ---------------------------------------------------------------------------
# Runner creation
# ---------------------------------------------------------------------------


def _create_runner(graph, runner_override: str | None, db: str | None):
    """Create the appropriate runner based on graph and options.

    Returns (runner, is_async, workflow_kwargs).
    """
    from hypergraph.runners import AsyncRunner, SyncRunner

    use_async = runner_override == "async" or db is not None or graph.has_async_nodes

    if runner_override == "sync" and (db or graph.has_async_nodes):
        reason = "--db requires async" if db else "graph has async nodes"
        print(f"Error: Cannot use --runner sync ({reason})")
        raise typer.Exit(1)

    if use_async:
        checkpointer = None
        if db:
            from hypergraph.checkpointers import SqliteCheckpointer

            checkpointer = SqliteCheckpointer(db)
        return AsyncRunner(checkpointer=checkpointer), True
    return SyncRunner(), False


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _result_to_dict(result) -> dict[str, Any]:
    """Convert RunResult to a JSON-friendly dict."""
    data: dict[str, Any] = {
        "status": result.status.value,
        "values": result.values,
        "run_id": result.run_id,
    }
    if result.workflow_id:
        data["workflow_id"] = result.workflow_id
    if result.error:
        data["error"] = str(result.error)
    return data


def _print_run_result(result, as_json: bool, output: str | None, verbose: bool) -> None:
    """Print a RunResult in human-readable or JSON format."""
    if as_json:
        print_json("run", _result_to_dict(result), output)
        return

    print(f"\nStatus: {result.status.value}")
    if result.error:
        print(f"Error: {result.error}")
    for key, value in result.values.items():
        display = truncate_value(value) if not verbose else json.dumps(value, default=str)
        print(f"  {key}: {display}")
    if verbose and result.log:
        print(f"\n  Duration: {result.log.total_duration_ms:.0f}ms")
        print(f"  Steps: {len(result.log.steps)}")


def _print_map_result(result, as_json: bool, output: str | None, verbose: bool) -> None:
    """Print a MapResult in human-readable or JSON format."""
    if as_json:
        data = {
            "map_over": list(result.map_over),
            "map_mode": result.map_mode,
            "total_duration_ms": result.total_duration_ms,
            "count": len(result),
            "results": [_result_to_dict(r) for r in result.results],
        }
        print_json("map", data, output)
        return

    print(f"\nMap over: {', '.join(result.map_over)} ({result.map_mode})")
    print(f"Results: {len(result)} | Duration: {result.total_duration_ms:.0f}ms\n")
    for i, r in enumerate(result.results):
        status = r.status.value
        vals = ", ".join(f"{k}={truncate_value(v, 60)}" for k, v in r.values.items())
        print(f"  [{i}] {status}: {vals}")
        if r.error:
            print(f"       Error: {r.error}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def register_commands(app: typer.Typer) -> None:
    """Register `run` and `map` as top-level commands on the app."""

    @app.command(
        "run",
        context_settings={"allow_extra_args": True, "allow_interspersed_args": True},
    )
    def run_cmd(
        ctx: typer.Context,
        target: Annotated[str, typer.Argument(help="Graph as 'module:attr' or registered name")],
        values: Annotated[str | None, typer.Option("--values", help="JSON string or file path")] = None,
        select: Annotated[str | None, typer.Option("--select", help="Comma-separated output names")] = None,
        entrypoint: Annotated[str | None, typer.Option("--entrypoint", help="Cycle entry point node")] = None,
        max_iterations: Annotated[int | None, typer.Option("--max-iterations", help="Max cycle iterations")] = None,
        error_handling: Annotated[str, typer.Option("--error-handling", help="'raise' or 'continue'")] = "raise",
        runner_type: Annotated[str | None, typer.Option("--runner", help="'sync' or 'async'")] = None,
        db: Annotated[str | None, typer.Option("--db", help="SQLite DB path for checkpointing")] = None,
        workflow_id: Annotated[str | None, typer.Option("--workflow-id", help="Workflow identifier")] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
        output: Annotated[str | None, typer.Option("--output", help="Write JSON to file")] = None,
        verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed output")] = False,
    ):
        """Run a graph with the given inputs."""
        graph = load_graph(target)
        input_values = _resolve_values(values, ctx.args)

        # Resolve --db from config if not explicit
        if db is None:
            config = load_config()
            db = config.db

        runner, is_async = _create_runner(graph, runner_type, db)

        run_kwargs: dict[str, Any] = {
            "error_handling": error_handling,
        }
        if select:
            run_kwargs["select"] = select.split(",")
        if entrypoint:
            run_kwargs["entrypoint"] = entrypoint
        if max_iterations is not None:
            run_kwargs["max_iterations"] = max_iterations
        if workflow_id:
            run_kwargs["workflow_id"] = workflow_id

        if is_async:
            result = asyncio.run(runner.run(graph, input_values or None, **run_kwargs))
        else:
            result = runner.run(graph, input_values or None, **run_kwargs)

        _print_run_result(result, as_json, output, verbose)

    @app.command(
        "map",
        context_settings={"allow_extra_args": True, "allow_interspersed_args": True},
    )
    def map_cmd(
        ctx: typer.Context,
        target: Annotated[str, typer.Argument(help="Graph as 'module:attr' or registered name")],
        map_over: Annotated[str, typer.Option("--map-over", help="Comma-separated param names to map over")],
        values: Annotated[str | None, typer.Option("--values", help="JSON string or file path")] = None,
        map_mode: Annotated[str, typer.Option("--map-mode", help="'zip' or 'product'")] = "zip",
        select: Annotated[str | None, typer.Option("--select", help="Comma-separated output names")] = None,
        error_handling: Annotated[str, typer.Option("--error-handling", help="'raise' or 'continue'")] = "continue",
        runner_type: Annotated[str | None, typer.Option("--runner", help="'sync' or 'async'")] = None,
        db: Annotated[str | None, typer.Option("--db", help="SQLite DB path for checkpointing")] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
        output: Annotated[str | None, typer.Option("--output", help="Write JSON to file")] = None,
        verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed output")] = False,
    ):
        """Map a graph over multiple input values."""
        graph = load_graph(target)
        input_values = _resolve_values(values, ctx.args)

        if db is None:
            config = load_config()
            db = config.db

        runner, is_async = _create_runner(graph, runner_type, db)

        map_kwargs: dict[str, Any] = {
            "map_over": map_over.split(","),
            "map_mode": map_mode,
            "error_handling": error_handling,
        }
        if select:
            map_kwargs["select"] = select.split(",")

        if is_async:
            result = asyncio.run(runner.map(graph, input_values or None, **map_kwargs))
        else:
            result = runner.map(graph, input_values or None, **map_kwargs)

        _print_map_result(result, as_json, output, verbose)
