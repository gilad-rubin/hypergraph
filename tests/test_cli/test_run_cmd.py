"""Tests for CLI run, map, and graph ls commands."""

from __future__ import annotations

import json
import textwrap

import pytest

typer = pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

from hypergraph import Graph, node  # noqa: E402
from hypergraph.cli import create_app  # noqa: E402
from hypergraph.cli._config import HypergraphConfig, find_pyproject, load_config  # noqa: E402
from hypergraph.cli.run_cmd import _parse_kv_args, _parse_literal, _resolve_values  # noqa: E402

# ---------------------------------------------------------------------------
# Test graphs defined at module scope for CLI to import
# ---------------------------------------------------------------------------


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


test_graph = Graph([double, triple])

runner_cli = CliRunner()


# ---------------------------------------------------------------------------
# Module path for the test graph (used by CLI commands)
# ---------------------------------------------------------------------------
MODULE_PATH = f"{__name__}:test_graph"


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfigResolution:
    def test_find_pyproject_exists(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")
        assert find_pyproject(tmp_path) == pyproject

    def test_find_pyproject_walks_up(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")
        child = tmp_path / "src" / "pkg"
        child.mkdir(parents=True)
        assert find_pyproject(child) == pyproject

    def test_find_pyproject_missing(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        # Walk up should eventually not find anything in tmp_path context
        # Use a path deep enough that it won't find the real project
        assert find_pyproject(empty) is None

    def test_load_config_with_graphs(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            textwrap.dedent("""\
            [project]
            name = "test"

            [tool.hypergraph.graphs]
            pipeline = "my_module:graph"
            etl = "etl.main:pipeline"

            [tool.hypergraph]
            db = "./workflows.db"
        """)
        )
        config = load_config(tmp_path)
        assert config.graphs == {"pipeline": "my_module:graph", "etl": "etl.main:pipeline"}
        assert config.db == "./workflows.db"

    def test_load_config_empty_section(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")
        config = load_config(tmp_path)
        assert config == HypergraphConfig()

    def test_load_config_no_pyproject(self, tmp_path):
        empty = tmp_path / "isolated"
        empty.mkdir()
        config = load_config(empty)
        assert config == HypergraphConfig()


# ---------------------------------------------------------------------------
# Value parsing tests
# ---------------------------------------------------------------------------


class TestValueParsing:
    def test_parse_literal_int(self):
        assert _parse_literal("5") == 5

    def test_parse_literal_float(self):
        assert _parse_literal("3.14") == 3.14

    def test_parse_literal_list(self):
        assert _parse_literal("[1,2,3]") == [1, 2, 3]

    def test_parse_literal_string_fallback(self):
        assert _parse_literal("hello") == "hello"

    def test_parse_literal_dict(self):
        assert _parse_literal("{'a': 1}") == {"a": 1}

    def test_parse_literal_bool(self):
        assert _parse_literal("True") is True

    def test_parse_kv_args(self):
        result = _parse_kv_args(["x=5", "y=[1,2]", "name=hello"])
        assert result == {"x": 5, "y": [1, 2], "name": "hello"}

    def test_parse_kv_args_invalid(self):
        from click.exceptions import Exit

        with pytest.raises(Exit):
            _parse_kv_args(["no_equals_sign"])

    def test_resolve_values_json_string(self, tmp_path):
        result = _resolve_values('{"x": 5, "y": 10}', [])
        assert result == {"x": 5, "y": 10}

    def test_resolve_values_file(self, tmp_path):
        f = tmp_path / "params.json"
        f.write_text('{"x": 42}')
        result = _resolve_values(str(f), [])
        assert result == {"x": 42}

    def test_resolve_values_layering(self, tmp_path):
        """key=value args override --values."""
        f = tmp_path / "params.json"
        f.write_text('{"x": 1, "y": 2}')
        result = _resolve_values(str(f), ["x=99"])
        assert result == {"x": 99, "y": 2}

    def test_resolve_values_kv_only(self):
        result = _resolve_values(None, ["x=5"])
        assert result == {"x": 5}


# ---------------------------------------------------------------------------
# Graph ls tests
# ---------------------------------------------------------------------------


class TestGraphLs:
    def test_ls_no_registry(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        app = create_app()
        result = runner_cli.invoke(app, ["graph", "ls"])
        assert result.exit_code == 0
        assert "No graphs registered" in result.output or "graphs" in result.output.lower()

    def test_ls_with_registry(self, tmp_path, monkeypatch):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            textwrap.dedent("""\
            [project]
            name = "test"

            [tool.hypergraph.graphs]
            pipeline = "my_module:graph"
        """)
        )
        monkeypatch.chdir(tmp_path)
        app = create_app()
        result = runner_cli.invoke(app, ["graph", "ls"])
        assert result.exit_code == 0
        assert "pipeline" in result.output
        assert "my_module:graph" in result.output

    def test_ls_json(self, tmp_path, monkeypatch):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            textwrap.dedent("""\
            [project]
            name = "test"

            [tool.hypergraph.graphs]
            pipeline = "my_module:graph"
        """)
        )
        monkeypatch.chdir(tmp_path)
        app = create_app()
        result = runner_cli.invoke(app, ["graph", "ls", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["command"] == "graph.ls"
        assert data["data"]["graphs"] == {"pipeline": "my_module:graph"}


# ---------------------------------------------------------------------------
# Run command tests
# ---------------------------------------------------------------------------


class TestRunCmd:
    def test_basic_run(self):
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "x=5"])
        assert result.exit_code == 0
        assert "doubled" in result.output
        assert "10" in result.output
        assert "tripled" in result.output
        assert "30" in result.output

    def test_run_json(self):
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "x=5", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["command"] == "run"
        assert data["data"]["status"] == "completed"
        assert data["data"]["values"]["doubled"] == 10
        assert data["data"]["values"]["tripled"] == 30

    def test_run_with_values_option(self):
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "--values", '{"x": 7}', "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["values"]["doubled"] == 14

    def test_run_values_file(self, tmp_path):
        f = tmp_path / "params.json"
        f.write_text('{"x": 3}')
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "--values", str(f), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["values"]["doubled"] == 6

    def test_run_kv_overrides_values(self, tmp_path):
        f = tmp_path / "params.json"
        f.write_text('{"x": 3}')
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "--values", str(f), "x=10", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["values"]["doubled"] == 20

    def test_run_verbose(self):
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "x=5", "--verbose"])
        assert result.exit_code == 0
        assert "completed" in result.output
        assert "Duration" in result.output
        assert "Steps" in result.output

    def test_run_select(self):
        app = create_app()
        result = runner_cli.invoke(app, ["run", MODULE_PATH, "x=5", "--select", "doubled", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "doubled" in data["data"]["values"]

    def test_run_bad_target(self):
        app = create_app()
        result = runner_cli.invoke(app, ["run", "nonexistent_module:graph", "x=5"])
        assert result.exit_code != 0

    def test_run_registry_name_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        app = create_app()
        result = runner_cli.invoke(app, ["run", "my_pipeline", "x=5"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# Map command tests
# ---------------------------------------------------------------------------


class TestMapCmd:
    def test_basic_map(self):
        app = create_app()
        result = runner_cli.invoke(
            app,
            ["map", MODULE_PATH, "--map-over", "x", "--values", '{"x": [1, 2, 3]}'],
        )
        assert result.exit_code == 0
        assert "Map over: x" in result.output
        assert "Results: 3" in result.output

    def test_map_json(self):
        app = create_app()
        result = runner_cli.invoke(
            app,
            ["map", MODULE_PATH, "--map-over", "x", "--values", '{"x": [1, 2, 3]}', "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["command"] == "map"
        assert data["data"]["count"] == 3
        assert len(data["data"]["results"]) == 3
        assert data["data"]["results"][0]["values"]["doubled"] == 2
        assert data["data"]["results"][1]["values"]["doubled"] == 4

    def test_map_kv_args(self):
        app = create_app()
        result = runner_cli.invoke(
            app,
            ["map", MODULE_PATH, "--map-over", "x", "x=[10,20]", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 2

    def test_map_error_handling_continue(self):
        """map defaults to error_handling='continue'."""
        app = create_app()
        result = runner_cli.invoke(
            app,
            ["map", MODULE_PATH, "--map-over", "x", "--values", '{"x": [1, 2]}', "--json"],
        )
        assert result.exit_code == 0

    def test_map_mode(self):
        app = create_app()
        result = runner_cli.invoke(
            app,
            ["map", MODULE_PATH, "--map-over", "x", "--map-mode", "zip", "--values", '{"x": [5]}', "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["map_mode"] == "zip"
