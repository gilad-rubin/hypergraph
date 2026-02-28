"""Hypergraph CLI — AI agent-friendly debugging interface.

Entry point for the `hypergraph` command. Requires ``pip install hypergraph[cli]``.

Commands:
    workflows       Inspect and manage workflow executions
    workflows ls    List workflows with filters
    workflows show  Show execution trace for a workflow
    workflows state Show accumulated values at a point
    workflows steps Show detailed step records
    graph inspect   Show graph structure (nodes, edges, inputs)
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
    from hypergraph.cli.workflows import app as workflows_app

    app = typer.Typer(
        name="hypergraph",
        help="Hypergraph workflow debugging CLI.",
        no_args_is_help=True,
    )
    app.add_typer(workflows_app, name="workflows")
    app.add_typer(graph_app, name="graph")

    return app


def main():
    """CLI entry point."""
    app = create_app()
    app()
