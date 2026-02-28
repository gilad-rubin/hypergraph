"""Integration tests for CLI workflow commands.

Uses CliRunner to test command output without subprocess overhead.
"""

import json

import pytest
from typer.testing import CliRunner

from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer
from hypergraph.cli import create_app

# Skip all if aiosqlite is not installed
aiosqlite = pytest.importorskip("aiosqlite")


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
    """Create a DB with a completed workflow."""
    cp = SqliteCheckpointer(db_path)
    cp.policy = CheckpointPolicy(durability="sync", retention="full")
    await cp.initialize()

    r = AsyncRunner(checkpointer=cp)
    graph = Graph([double, triple])
    await r.run(graph, {"x": 5}, workflow_id="wf-test")
    await cp.close()

    return db_path


class TestWorkflowsShow:
    def test_show_output_format(self, populated_db):
        """CLI: `workflows show` matches plan's table format."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "show", "wf-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "wf-test" in result.output
        assert "completed" in result.output.lower() or "COMPLETED" in result.output
        # Step numbers should appear
        assert "double" in result.output
        assert "triple" in result.output

    def test_show_json(self, populated_db):
        """CLI: --json returns valid JSON with envelope."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "show", "wf-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == 1
        assert data["command"] == "workflows.show"
        assert "workflow" in data["data"]
        assert "steps" in data["data"]

    def test_show_nonexistent(self, populated_db):
        """CLI: show unknown workflow gives error."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "show", "nope", "--db", populated_db])
        assert result.exit_code != 0 or "not found" in result.output.lower()


class TestWorkflowsState:
    def test_state_progressive_disclosure(self, populated_db):
        """CLI: state shows type/size by default, not values."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "state", "wf-test", "--db", populated_db])
        assert result.exit_code == 0
        # Should show "Values hidden" guidance
        assert "values" in result.output.lower()
        # Should show output names
        assert "doubled" in result.output
        assert "tripled" in result.output

    def test_state_with_values(self, populated_db):
        """CLI: state --values shows actual data."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "state", "wf-test", "--db", populated_db, "--values"])
        assert result.exit_code == 0
        assert "10" in result.output  # doubled = 10
        assert "30" in result.output  # tripled = 30

    def test_state_single_key(self, populated_db):
        """CLI: state --key shows one value."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "state", "wf-test", "--db", populated_db, "--key", "doubled"])
        assert result.exit_code == 0
        assert "10" in result.output

    def test_state_json(self, populated_db):
        """CLI: state --json returns full state data."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "state", "wf-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["state"]["doubled"] == 10
        assert data["data"]["state"]["tripled"] == 30


class TestWorkflowsLs:
    def test_ls_lists_workflows(self, populated_db):
        """CLI: ls shows workflow list."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "ls", "--db", populated_db])
        assert result.exit_code == 0
        assert "wf-test" in result.output

    def test_ls_json(self, populated_db):
        """CLI: ls --json returns valid envelope."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "ls", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["schema_version"] == 1
        assert data["command"] == "workflows.ls"
        assert len(data["data"]) == 1

    def test_ls_filter_status(self, populated_db):
        """CLI: ls --status filters correctly."""
        app = create_app()
        # Should find completed
        result = runner_cli.invoke(app, ["workflows", "ls", "--status", "completed", "--db", populated_db])
        assert "wf-test" in result.output

        # Should not find active
        result = runner_cli.invoke(app, ["workflows", "ls", "--status", "active", "--db", populated_db])
        assert "wf-test" not in result.output or "No workflows" in result.output


class TestWorkflowsSteps:
    def test_steps_output(self, populated_db):
        """CLI: steps shows detailed records."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "steps", "wf-test", "--db", populated_db])
        assert result.exit_code == 0
        assert "Step [0]" in result.output
        assert "double" in result.output
        assert "input_versions" in result.output

    def test_steps_json(self, populated_db):
        """CLI: steps --json returns step records."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "steps", "wf-test", "--db", populated_db, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]) == 2
        assert data["data"][0]["node_name"] == "double"


class TestJsonEnvelopeStructure:
    def test_envelope_has_schema_version(self, populated_db):
        """CLI: --json output has schema_version, command, data envelope."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "show", "wf-test", "--db", populated_db, "--json"])
        data = json.loads(result.output)
        assert "schema_version" in data
        assert "command" in data
        assert "generated_at" in data
        assert "data" in data


class TestGuidanceFooters:
    def test_show_has_guidance(self, populated_db):
        """CLI: output includes 'To see more: ...' guidance."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "show", "wf-test", "--db", populated_db])
        assert "To see values" in result.output or "To see error" in result.output

    def test_state_has_guidance(self, populated_db):
        """CLI: state output includes guidance."""
        app = create_app()
        result = runner_cli.invoke(app, ["workflows", "state", "wf-test", "--db", populated_db])
        assert "--values" in result.output or "--key" in result.output
