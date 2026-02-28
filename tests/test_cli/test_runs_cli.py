"""Integration tests for CLI runs commands.

Uses CliRunner to test command output without subprocess overhead.
"""

import json

import pytest

# Skip all if optional dependencies are not installed
typer = pytest.importorskip("typer")
aiosqlite = pytest.importorskip("aiosqlite")

from typer.testing import CliRunner  # noqa: E402

from hypergraph import AsyncRunner, Graph, node  # noqa: E402
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer  # noqa: E402
from hypergraph.cli import create_app  # noqa: E402


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


runner_cli = CliRunner()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
async def populated_db(db_path):
    """Create a DB with a completed run."""
    cp = SqliteCheckpointer(db_path)
    cp.policy = CheckpointPolicy(durability="sync", retention="full")
    await cp.initialize()

    r = AsyncRunner(checkpointer=cp)
    graph = Graph([double, triple])
    await r.run(graph, {"x": 5}, workflow_id="run-test")
    await cp.close()

    return db_path


class TestRunsShow:
    def test_show_output_format(self, populated_db):
        """CLI: `runs show` displays run trace."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "run-test" in result.output
        assert "completed" in result.output.lower() or "COMPLETED" in result.output
        assert "double" in result.output
        assert "triple" in result.output

    def test_show_json(self, populated_db):
        """CLI: --json returns valid JSON with envelope."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == 2
        assert data["command"] == "runs.show"
        assert "run" in data["data"]
        assert "steps" in data["data"]

    def test_show_nonexistent(self, populated_db):
        """CLI: show unknown run gives error."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "nope", "--db", populated_db])
        assert result.exit_code != 0 or "not found" in result.output.lower()

    def test_show_single_step(self, populated_db):
        """CLI: --step N shows a single step detail."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db, "--step", "0"])
        assert result.exit_code == 0
        assert "Step [0]" in result.output
        assert "double" in result.output

    def test_show_step_with_values(self, populated_db):
        """CLI: --step N --values shows step output values."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db, "--step", "0", "--values"])
        assert result.exit_code == 0
        assert "doubled" in result.output

    def test_show_has_node_type(self, populated_db):
        """CLI: show includes node type column."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "Type" in result.output
        assert "FunctionNode" in result.output


class TestRunsValues:
    def test_values_table(self, populated_db):
        """CLI: values shows type/size table by default."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "values", "run-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "doubled" in result.output
        assert "tripled" in result.output

    def test_values_single_key(self, populated_db):
        """CLI: values --key shows one value."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "values", "run-test", "--db", populated_db, "--key", "doubled"])
        assert result.exit_code == 0
        assert "10" in result.output

    def test_values_json(self, populated_db):
        """CLI: values --json returns full state data."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "values", "run-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["values"]["doubled"] == 10
        assert data["data"]["values"]["tripled"] == 30


class TestRunsLs:
    def test_ls_lists_runs(self, populated_db):
        """CLI: ls shows run list."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "ls", "--db", populated_db])
        assert result.exit_code == 0
        assert "run-test" in result.output

    def test_ls_json(self, populated_db):
        """CLI: ls --json returns valid envelope."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "ls", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == 2
        assert data["command"] == "runs.ls"
        assert len(data["data"]) == 1

    def test_ls_filter_status(self, populated_db):
        """CLI: ls --status filters correctly."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "ls", "--status", "completed", "--db", populated_db])
        assert "run-test" in result.output

        result = runner_cli.invoke(app, ["runs", "ls", "--status", "active", "--db", populated_db])
        assert "run-test" not in result.output or "No runs" in result.output

    def test_ls_filter_by_graph(self, populated_db):
        """CLI: ls --graph filters by graph name."""
        app = create_app()
        # Our graph is unnamed so graph_name is empty string
        result = runner_cli.invoke(app, ["runs", "ls", "--graph", "nonexistent", "--db", populated_db])
        assert "No runs" in result.output or "run-test" not in result.output


class TestRunsSteps:
    def test_steps_output(self, populated_db):
        """CLI: steps shows detailed records."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "steps", "run-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "Step [0]" in result.output
        assert "double" in result.output
        assert "input_versions" in result.output

    def test_steps_json(self, populated_db):
        """CLI: steps --json returns step records."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "steps", "run-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]) == 2
        assert data["data"][0]["node_name"] == "double"


class TestRunsSearch:
    def test_search_by_node_name(self, populated_db):
        """CLI: search finds steps by node name."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "search", "double", "--db", populated_db])
        assert result.exit_code == 0
        assert "double" in result.output

    def test_search_json(self, populated_db):
        """CLI: search --json returns step records."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "search", "double", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["command"] == "runs.search"
        assert len(data["data"]) >= 1


class TestRunsStats:
    def test_stats_output(self, populated_db):
        """CLI: stats shows per-node performance table."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "stats", "run-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "double" in result.output
        assert "triple" in result.output
        assert "Runs" in result.output

    def test_stats_json(self, populated_db):
        """CLI: stats --json returns per-node stats."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "stats", "run-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["command"] == "runs.stats"
        assert "double" in data["data"]["nodes"]


class TestJsonEnvelopeStructure:
    def test_envelope_has_schema_version_2(self, populated_db):
        """CLI: --json output has schema_version 2."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db, "--json"])
        data = json.loads(result.output)
        assert data["schema_version"] == 2
        assert "command" in data
        assert "generated_at" in data
        assert "data" in data


class TestCTAs:
    def test_show_has_ctas(self, populated_db):
        """CLI: show output includes → CTA lines."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "show", "run-test", "--db", populated_db])
        assert "→" in result.output

    def test_values_has_ctas(self, populated_db):
        """CLI: values output includes → CTA lines."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "values", "run-test", "--db", populated_db])
        assert "→" in result.output

    def test_ls_has_ctas(self, populated_db):
        """CLI: ls output includes → CTA lines."""
        app = create_app()
        result = runner_cli.invoke(app, ["runs", "ls", "--db", populated_db])
        assert "→" in result.output


class TestParseSince:
    def test_parse_since_hours(self):
        """parse_since converts '1h' to datetime ~1 hour ago."""
        from hypergraph.cli._format import parse_since

        result = parse_since("1h")
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        delta = (now - result).total_seconds()
        assert 3500 < delta < 3700  # ~1 hour

    def test_parse_since_days(self):
        """parse_since converts '7d' to datetime ~7 days ago."""
        from hypergraph.cli._format import parse_since

        result = parse_since("7d")
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        delta = (now - result).total_seconds()
        assert 604000 < delta < 605000  # ~7 days

    def test_parse_since_invalid(self):
        """parse_since raises ValueError on invalid input."""
        from hypergraph.cli._format import parse_since

        with pytest.raises(ValueError, match="Invalid --since"):
            parse_since("abc")
