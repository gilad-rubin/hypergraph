"""Hypergraph CLI — execute graphs and inspect runs.

Entry point for the `hypergraph` command. Requires ``pip install hypergraph[cli]``.

Commands:
    run             Execute a graph with given inputs
    map             Map a graph over multiple input values
    graph ls        List registered graphs from pyproject.toml
    graph inspect   Show graph structure (nodes, edges, inputs)
    runs ls         List runs with filters
    runs show       Show execution trace for a run
    runs values     Show accumulated output values
    runs steps      Show detailed step records
    runs search     Full-text search across step records
    runs stats      Per-node performance statistics
"""

from __future__ import annotations


def _require_typer():
    """Check that typer is available."""
    try:
        import typer  # noqa: F401
    except ImportError:
        import sys

        print("Error: typer is required for the CLI. Install with: pip install hypergraph[cli]", file=sys.stderr)
        raise SystemExit(1) from None


def create_app():
    """Create the Typer app with all subcommands."""
    _require_typer()

    import typer

    from hypergraph.cli.graph_cmd import app as graph_app
    from hypergraph.cli.run_cmd import register_commands
    from hypergraph.cli.runs import app as runs_app

    app = typer.Typer(
        name="hypergraph",
        help="Hypergraph graph execution and debugging CLI.",
        no_args_is_help=True,
    )
    app.add_typer(runs_app, name="runs")
    app.add_typer(graph_app, name="graph")
    register_commands(app)

    return app


def main():
    """CLI entry point."""
    app = create_app()
    app()
