"""Tests for the plain display mode (set_display_mode / HYPERGRAPH_DISPLAY).

Plain mode makes every implicit ``_repr_html_`` return None so notebook
display falls back to compact text reprs. Explicit calls such as
``graph.visualize()`` stay rich.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import hypergraph
import hypergraph._repr as repr_module
from hypergraph import Graph, SyncRunner, get_display_mode, node, set_display_mode


@pytest.fixture(autouse=True)
def _isolated_display_mode(monkeypatch):
    """Reset the module-level mode and env var around every test."""
    monkeypatch.setattr(repr_module, "_display_mode", None)
    monkeypatch.delenv("HYPERGRAPH_DISPLAY", raising=False)


def make_graph() -> Graph:
    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    return Graph([double], name="pipeline")


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


class TestApi:
    def test_exported_from_root(self):
        assert "set_display_mode" in hypergraph.__all__
        assert "get_display_mode" in hypergraph.__all__
        assert callable(hypergraph.set_display_mode)
        assert callable(hypergraph.get_display_mode)

    def test_default_mode_is_rich(self):
        assert get_display_mode() == "rich"

    def test_setter_roundtrip(self):
        set_display_mode("plain")
        assert get_display_mode() == "plain"
        set_display_mode("rich")
        assert get_display_mode() == "rich"

    def test_invalid_setter_value_raises(self):
        with pytest.raises(ValueError, match="'rich'.*'plain'"):
            set_display_mode("fancy")

    def test_invalid_env_value_raises_at_display_time(self, monkeypatch):
        monkeypatch.setenv("HYPERGRAPH_DISPLAY", "compact")
        with pytest.raises(ValueError, match="HYPERGRAPH_DISPLAY"):
            make_graph()._repr_html_()


# ---------------------------------------------------------------------------
# Implicit display: rich by default, plain on demand, reversible
# ---------------------------------------------------------------------------


class TestImplicitDisplay:
    def test_default_rich_graph_html(self):
        html = make_graph()._repr_html_()
        assert isinstance(html, str)
        assert len(html) > 0

    def test_plain_mode_falls_back_to_text_reprs(self):
        """Plain mode: HTML is None, text repr still carries the payload."""
        graph = make_graph()
        result = SyncRunner().run(graph, {"x": 2})

        set_display_mode("plain")
        assert graph._repr_html_() is None
        assert result._repr_html_() is None
        # The fallback the agent actually reads: status + values.
        text = repr(result)
        assert "completed" in text
        assert "doubled" in text

        # Not one-way: flipping back restores the HTML path.
        set_display_mode("rich")
        assert isinstance(graph._repr_html_(), str)
        assert isinstance(result._repr_html_(), str)

    def test_env_var_read_dynamically_after_import(self, monkeypatch):
        graph = make_graph()
        assert isinstance(graph._repr_html_(), str)

        monkeypatch.setenv("HYPERGRAPH_DISPLAY", "plain")
        assert graph._repr_html_() is None

        monkeypatch.delenv("HYPERGRAPH_DISPLAY")
        assert isinstance(graph._repr_html_(), str)

    def test_setter_overrides_env(self, monkeypatch):
        monkeypatch.setenv("HYPERGRAPH_DISPLAY", "plain")
        set_display_mode("rich")
        assert get_display_mode() == "rich"
        assert isinstance(make_graph()._repr_html_(), str)


# ---------------------------------------------------------------------------
# Explicit display stays rich
# ---------------------------------------------------------------------------


class TestExplicitDisplayStaysRich:
    def test_visualize_output_stays_rich_in_plain_mode(self):
        set_display_mode("plain")
        widget = make_graph().visualize()
        html = widget._repr_html_()
        assert isinstance(html, str)
        assert len(html) > 0


# ---------------------------------------------------------------------------
# Sweep: every _repr_html_ in the package obeys the mode
# ---------------------------------------------------------------------------

# Classes whose _repr_html_ is only reachable via an explicit user call
# (`graph.visualize()`); the contract keeps these rich even in plain mode.
EXPLICIT_DISPLAY_EXCEPTIONS = {
    "hypergraph.viz.widget._VizCellOutput",
}


def _iter_repr_html_classes():
    """Yield every class in the package that defines its own _repr_html_."""
    seen: set[str] = set()
    for modinfo in pkgutil.walk_packages(hypergraph.__path__, prefix="hypergraph.", onerror=lambda name: None):
        try:
            module = importlib.import_module(modinfo.name)
        except Exception:
            continue  # optional dependency missing (e.g. daft, aiosqlite)
        for cls in vars(module).values():
            if not isinstance(cls, type) or cls.__module__ != module.__name__:
                continue
            if "_repr_html_" not in cls.__dict__:
                continue
            qualname = f"{cls.__module__}.{cls.__qualname__}"
            if qualname not in seen:
                seen.add(qualname)
                yield qualname, cls


def _instance_factories() -> dict[str, object]:
    """One constructed instance per known _repr_html_-defining class."""
    from hypergraph.checkpointers.types import (
        Checkpoint,
        LineageRow,
        LineageView,
        Run,
        RunTable,
        StepRecord,
        StepStatus,
        StepTable,
        WorkflowStatus,
    )
    from hypergraph.runners._shared.results import MapLog, MapResult, NodeRecord, RunLog, RunResult, RunStatus

    step = StepRecord(run_id="r", superstep=0, node_name="a", index=0, status=StepStatus.COMPLETED, input_versions={})
    run = Run(id="r-1", status=WorkflowStatus.COMPLETED)
    run_log = RunLog(
        graph_name="g",
        run_id="r-1",
        total_duration_ms=1.0,
        steps=(NodeRecord(node_name="a", superstep=0, duration_ms=1.0, status="completed", span_id="s"),),
    )
    run_result = RunResult(values={"x": 1}, status=RunStatus.COMPLETED, log=run_log)

    factories: dict[str, object] = {
        "hypergraph.graph.core.Graph": lambda: make_graph(),
        "hypergraph.runners._shared.results.RunResult": lambda: run_result,
        "hypergraph.runners._shared.results.MapResult": lambda: MapResult(
            results=(run_result,),
            run_id="r-1",
            total_duration_ms=1.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="g",
        ),
        "hypergraph.runners._shared.results.RunLog": lambda: run_log,
        "hypergraph.runners._shared.results.MapLog": lambda: MapLog(graph_name="g", total_duration_ms=1.0, items=(run_log,)),
        "hypergraph.checkpointers.types.StepRecord": lambda: step,
        "hypergraph.checkpointers.types.Run": lambda: run,
        "hypergraph.checkpointers.types.Checkpoint": lambda: Checkpoint(values={"x": 1}, steps=[step]),
        "hypergraph.checkpointers.types.RunTable": lambda: RunTable([run]),
        "hypergraph.checkpointers.types.StepTable": lambda: StepTable([step]),
        "hypergraph.checkpointers.types.LineageView": lambda: LineageView(
            [LineageRow(lane="", run=run, depth=0, is_selected=True)],
            selected_run_id="r-1",
            root_run_id="r-1",
        ),
    }

    try:
        from hypergraph.checkpointers.sqlite import SqliteCheckpointer
    except ImportError:
        pass  # aiosqlite not installed; the walk will not discover the class either
    else:
        factories["hypergraph.checkpointers.sqlite.SqliteCheckpointer"] = lambda: SqliteCheckpointer(":memory:")

    return factories


class TestPlainModeSweep:
    def test_every_implicit_repr_html_returns_none_in_plain_mode(self):
        """Pins future _repr_html_ additions to the display-mode contract."""
        classes = dict(_iter_repr_html_classes())
        # Sanity: the walk actually discovered the core surface.
        assert "hypergraph.graph.core.Graph" in classes
        assert "hypergraph.runners._shared.results.RunResult" in classes

        factories = _instance_factories()
        set_display_mode("plain")
        for qualname in sorted(classes):
            if qualname in EXPLICIT_DISPLAY_EXCEPTIONS:
                continue
            factory = factories.get(qualname)
            assert factory is not None, (
                f"{qualname} defines _repr_html_ but this sweep has no instance factory for it.\n\n"
                "How to fix: gate the new _repr_html_ with plain_reprs() from hypergraph._repr "
                "and register a factory in _instance_factories(); or, if it is only reachable "
                "via an explicit user call, add it to EXPLICIT_DISPLAY_EXCEPTIONS."
            )
            instance = factory()
            assert instance._repr_html_() is None, f"{qualname}._repr_html_ must return None in plain display mode"

    def test_same_instances_render_html_in_rich_mode(self):
        set_display_mode("rich")
        for qualname, factory in _instance_factories().items():
            html = factory()._repr_html_()
            assert isinstance(html, str) and len(html) > 0, f"{qualname}._repr_html_ must return HTML in rich mode"
