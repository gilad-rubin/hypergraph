"""What the public import surface promises, asserted instead of assumed.

Every case here was a real drift found by reading `__all__` against the docs
(issue #256): a private sentinel handed out by a star import, two `RunStatus`
enums whose members compared `False`, two independent `ErrorHandling` aliases
that could silently diverge, and user-facing types whose only defining module
was behind an underscore.
"""

from __future__ import annotations

import ast
import pickle
from pathlib import Path

import pytest

import hypergraph
import hypergraph.events
import hypergraph.nodes
import hypergraph.runners
from hypergraph import EventProcessor, Graph, RunStatus, SyncRunner, node
from hypergraph.events.types import RunEndEvent

SRC = Path(hypergraph.__file__).parent


class _Collector(EventProcessor):
    """Keeps every emitted event so a test can read the real RunEndEvent."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def on_event(self, event: object) -> None:
        self.events.append(event)

    def shutdown(self) -> None:
        return None


# === 1. A star import hands out no private name ===


class TestStarImportsCarryNoPrivateNames:
    def test_nodes_star_export_list_has_no_underscore_name(self):
        """``_EMIT_SENTINEL`` was listed in ``hypergraph.nodes.__all__``."""
        assert [name for name in hypergraph.nodes.__all__ if name.startswith("_")] == []

    def test_root_star_export_list_has_no_underscore_name(self):
        assert [name for name in hypergraph.__all__ if name.startswith("_")] == []

    def test_runners_star_export_list_has_no_underscore_name(self):
        assert [name for name in hypergraph.runners.__all__ if name.startswith("_")] == []

    def test_the_sentinel_still_lives_at_its_own_module(self):
        """Dropping the export is hygiene, not a removal: producers still import it."""
        from hypergraph.nodes.base import _EMIT_SENTINEL

        assert _EMIT_SENTINEL is not None

    def test_rename_entry_is_internal_but_did_not_stop_importing(self):
        """The docs call ``_rename_history`` internal; the export list now agrees."""
        assert "RenameEntry" not in hypergraph.nodes.__all__
        from hypergraph.nodes import RenameEntry

        assert RenameEntry("inputs", "a", "b").old == "a"


# === 2. One RunStatus, so a status comparison means something ===


class TestOneRunStatus:
    """The review's two one-liners, which both used to print ``False``."""

    def test_the_identity_and_the_equality_both_hold(self):
        assert hypergraph.RunStatus is hypergraph.events.RunStatus
        assert hypergraph.RunStatus.COMPLETED == hypergraph.events.RunStatus.COMPLETED

    def test_every_public_door_opens_on_the_same_object(self):
        from hypergraph.runners._shared.results import RunStatus as ResultsRunStatus
        from hypergraph.runners._shared.types import RunStatus as LegacyRunStatus

        assert hypergraph.runners.RunStatus is hypergraph.events.RunStatus
        assert ResultsRunStatus is hypergraph.events.RunStatus
        assert LegacyRunStatus is hypergraph.events.RunStatus

    def test_a_run_result_status_is_an_events_run_status(self):
        """What a consumer actually does: ``isinstance`` against the events enum."""

        @node(output_name="y")
        def add_one(x: int) -> int:
            return x + 1

        result = SyncRunner().run(Graph([add_one], name="g"), {"x": 1})
        assert isinstance(result.status, hypergraph.events.RunStatus)

    def test_a_result_status_compares_equal_to_its_own_run_end_event(self):
        """The footgun, end to end: one run, two surfaces, one answer."""

        @node(output_name="y")
        def add_one(x: int) -> int:
            return x + 1

        collector = _Collector()
        result = SyncRunner().run(Graph([add_one], name="g"), {"x": 1}, event_processors=[collector])
        ends = [event for event in collector.events if isinstance(event, RunEndEvent)]

        assert len(ends) == 1
        assert result.status == ends[0].status
        assert result.status is ends[0].status
        assert result.status is RunStatus.COMPLETED

    def test_the_string_bridge_is_gone_from_the_shared_helper(self):
        """``build_run_end_event`` took a ``str`` only because there were two enums."""
        import inspect

        from hypergraph.runners._shared.event_helpers import build_run_end_event

        annotation = inspect.signature(build_run_end_event).parameters["status"].annotation
        assert annotation == "RunStatus | None"

    def test_no_caller_hands_the_emit_helpers_a_status_value(self):
        """The ``.value`` round trip at every run-end emit site, by AST.

        A string ``status=`` was the bridge between the two enums. With one
        enum the bridge is dead weight, and a caller re-introducing it would
        re-introduce a str/enum mix on the event.
        """
        emitters = {"build_run_end_event", "_emit_run_end", "_emit_run_end_sync", "_emit_run_end_async"}
        offenders = []
        for source in SRC.rglob("*.py"):
            for call in (n for n in ast.walk(ast.parse(source.read_text())) if isinstance(n, ast.Call)):
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name not in emitters:
                    continue
                for keyword in call.keywords:
                    if keyword.arg != "status":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Attribute) and value.attr == "value":
                        offenders.append(f"{source.relative_to(SRC)}:{keyword.lineno}")
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        offenders.append(f"{source.relative_to(SRC)}:{keyword.lineno}")
        assert offenders == []

    def test_a_bogus_status_fails_loudly_with_the_enum_named(self):
        """One enum also means one place a wrong value is rejected."""
        with pytest.raises(ValueError, match="RunStatus"):
            RunEndEvent(run_id="r", span_id="s", status="not-a-status")

    def test_both_legacy_pickle_paths_still_load_the_one_enum(self):
        """A checkpoint written before the unification still unpickles."""
        for module in ("hypergraph.runners._shared.results", "hypergraph.runners._shared.types"):
            assert pickle.loads(f"c{module}\nRunStatus\n.".encode()) is RunStatus
        assert pickle.loads(pickle.dumps(RunStatus.PAUSED)) is RunStatus.PAUSED


# === 3. One ErrorHandling ===


def test_error_handling_is_defined_exactly_once_in_src():
    """Two independent ``Literal`` aliases could gain a third member separately."""
    definitions = []
    for source in SRC.rglob("*.py"):
        tree = ast.parse(source.read_text())
        for stmt in tree.body:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else ([stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "ErrorHandling":
                    definitions.append(f"{source.relative_to(SRC)}:{stmt.lineno}")
    assert [definition.split(":")[0] for definition in definitions] == ["nodes/graph_node.py"], definitions


def test_every_error_handling_door_is_the_same_alias():
    from hypergraph.nodes.graph_node import ErrorHandling as NodesErrorHandling
    from hypergraph.runners._shared.results import ErrorHandling as ResultsErrorHandling

    assert hypergraph.ErrorHandling is NodesErrorHandling
    assert hypergraph.runners.ErrorHandling is NodesErrorHandling
    assert ResultsErrorHandling is NodesErrorHandling


# === 4. User-facing types have a public defining module ===


class TestNoPublicSymbolIsDefinedBehindAnUnderscore:
    """A user writes ``ctx: NodeContext``; its module cannot be private."""

    @pytest.mark.parametrize("name", ["NodeContext", "NodeSpanRef", "current_node_span"])
    def test_the_symbol_is_listed_in_the_runner_package(self, name):
        assert name in hypergraph.runners.__all__
        assert name in hypergraph.__all__

    @pytest.mark.parametrize("name", ["NodeContext", "NodeSpanRef", "current_node_span"])
    def test_its_defining_module_has_no_underscore_segment(self, name):
        symbol = getattr(hypergraph, name)
        assert all(not part.startswith("_") for part in symbol.__module__.split(".")), symbol.__module__

    def test_the_public_paths_resolve_to_the_exported_objects(self):
        from hypergraph.runners.context import NodeContext
        from hypergraph.runners.observability import NodeSpanRef, current_node_span

        assert hypergraph.NodeContext is NodeContext
        assert hypergraph.NodeSpanRef is NodeSpanRef
        assert hypergraph.current_node_span is current_node_span

    def test_the_old_internal_import_site_still_works(self):
        """Executors and existing tests import ``NodeContext`` from the builder module."""
        from hypergraph.runners._shared.node_context import NodeContext as InternalNodeContext

        assert InternalNodeContext is hypergraph.NodeContext

    def test_the_runner_internal_span_setters_stay_unexported(self):
        import hypergraph.runners.observability as observability

        assert hasattr(observability, "set_current_node_span")
        assert "set_current_node_span" not in hypergraph.runners.__all__
        assert "reset_current_node_span" not in hypergraph.runners.__all__


# === 5. A retired name is importable but not advertised ===


class TestTheRetiredNameIsNotStarExported:
    def test_worker_lock_error_is_out_of_the_root_export_list(self):
        assert "WorkerLockError" not in hypergraph.__all__

    def test_but_an_existing_except_clause_still_resolves(self):
        from hypergraph.host.errors import WorkerLockError

        assert hypergraph.WorkerLockError is WorkerLockError
        assert hypergraph.host.WorkerLockError is WorkerLockError


# === 6. The compatibility module names the release that removes it ===


def test_legacy_types_module_names_its_removal_release():
    """Without a named release, a compatibility module becomes permanent."""
    import hypergraph.runners._shared.types as legacy_types

    docstring = legacy_types.__doc__ or ""
    assert "REMOVED IN 0.3.0" in docstring
    assert "pickle" in docstring.lower()
