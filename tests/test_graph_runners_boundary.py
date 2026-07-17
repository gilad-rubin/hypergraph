"""Structural contract: the graph facade does not depend on runners at runtime.

Issue #264 (arch review U6): ``Graph.as_table`` used to import and construct
``SyncRunner`` itself — the one runtime graph -> runners dependency edge. The
default now lives in ``HyperTable`` (materialization already legitimately
imports runners), and this module pins the boundary so the edge cannot quietly
return.

Scoping note (why the assertion is not "runners.sync absent from sys.modules
after ``import hypergraph.graph.core``"): importing any submodule first
executes ``hypergraph/__init__.py``, which re-exports runners at module level,
so runners are ALWAYS in ``sys.modules`` by the time ``hypergraph.graph.core``
finishes importing — before and after this refactor alike. The honest,
sensitive contract is therefore scoped to the flagged edge itself:
``hypergraph/graph/core.py`` must contain no runtime import of
``hypergraph.runners`` (module level or inside function bodies), and calling
``as_table`` must not trigger one.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import hypergraph.graph.core as graph_core

GRAPH_CORE_PATH = Path(graph_core.__file__)


def _runtime_runners_imports(source: str) -> list[int]:
    """Line numbers of ``hypergraph.runners`` imports outside TYPE_CHECKING blocks."""

    def is_type_checking_guard(node: ast.AST) -> bool:
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    offenders: list[int] = []

    def check_import(node: ast.AST) -> None:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "hypergraph.runners" or module.startswith("hypergraph.runners."):
                offenders.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name == "hypergraph.runners" or alias.name.startswith("hypergraph.runners.") for alias in node.names):
                offenders.append(node.lineno)

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if is_type_checking_guard(child):
                # Runner imports are allowed for type hints only; still walk orelse,
                # checking each else-branch statement itself before its children.
                for else_child in child.orelse:
                    check_import(else_child)
                    visit(else_child)
                continue
            check_import(child)
            visit(child)

    visit(ast.parse(source))
    return offenders


def test_graph_core_has_no_runtime_runners_import():
    """graph/core.py imports hypergraph.runners only under TYPE_CHECKING.

    Sensitivity: restoring ``from hypergraph.runners import SyncRunner`` inside
    ``as_table`` (the pre-#264 code) makes this fail with that line number.
    """
    offenders = _runtime_runners_imports(GRAPH_CORE_PATH.read_text(encoding="utf-8"))
    assert offenders == [], (
        f"hypergraph/graph/core.py imports hypergraph.runners at runtime on lines {offenders}; the graph facade must not depend on runners (issue #264)."
    )


def test_as_table_does_not_import_runners_at_call_time(tmp_path):
    """Fresh-interpreter probe: calling ``as_table`` with an explicit runner
    triggers no (re-)import of ``hypergraph.runners``.

    Runners modules are evicted from ``sys.modules`` after the legitimate
    package-level imports, and a recording meta-path finder observes any new
    import attempt. Sensitivity: the pre-#264 ``as_table`` executed
    ``from hypergraph.runners import SyncRunner`` unconditionally — even with
    an explicit runner — which this probe reports as a failure.
    """
    script = tmp_path / "as_table_import_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import importlib.abc
            import sys
            import tempfile

            import hypergraph  # package facade: legitimately imports runners
            import hypergraph.graph.core
            import hypergraph.materialization  # legitimate runners consumer; pre-import

            # Documents the scoping: the package __init__ already pulled runners.
            assert "hypergraph.runners.sync" in sys.modules

            from hypergraph import Graph, SyncRunner, node
            from hypergraph.materialization import LanceDBStore

            runner = SyncRunner()  # constructed while runners is still loaded

            for name in [m for m in sys.modules if m == "hypergraph.runners" or m.startswith("hypergraph.runners.")]:
                del sys.modules[name]

            attempted = []

            class Recorder(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name == "hypergraph.runners" or name.startswith("hypergraph.runners."):
                        attempted.append(name)
                    return None  # record only; normal import machinery proceeds

            sys.meta_path.insert(0, Recorder())

            @node(output_name="doubled")
            def double(x: int) -> int:
                return x * 2

            with tempfile.TemporaryDirectory() as tmp:
                store = LanceDBStore(tmp + "/store")
                table = Graph([double]).as_table(identity="x", store=store, runner=runner)

            assert table is not None
            assert attempted == [], f"graph facade imported runners at as_table call time: {attempted}"
            print("PROBE-OK")
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROBE-OK" in result.stdout


def test_as_table_default_runner_is_sync_runner(tmp_path):
    """Behavior floor: omitting ``runner`` still yields a SyncRunner default,
    constructed eagerly at ``as_table`` call time (now inside HyperTable)."""
    from hypergraph import Graph, node
    from hypergraph.materialization import LanceDBStore
    from hypergraph.runners import SyncRunner

    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    table = Graph([double]).as_table(identity="x", store=LanceDBStore(str(tmp_path / "store")))
    assert isinstance(table._runner, SyncRunner)
