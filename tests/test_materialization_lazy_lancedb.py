"""Structural contract: ``hypergraph.materialization`` imports without lancedb.

``lancedb`` backs the optional ``[materialization]`` extra (see
``pyproject.toml``). Before this fix, ``hypergraph/materialization/__init__.py``
imported ``LanceDBStore`` eagerly, so ``import hypergraph.materialization``
(and transitively ``import hypergraph``) raised ``ModuleNotFoundError`` on any
host that installed hypergraph without the extra. ``LanceDBStore`` now
resolves lazily through module ``__getattr__`` (mirroring the existing
``check_store_conformance`` lazy export), so only code that actually touches
``LanceDBStore`` pays lancedb's import cost.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_materialization_import_succeeds_without_lancedb(tmp_path: Path) -> None:
    """Fresh-interpreter probe: block ``lancedb`` at the import-machinery level
    (simulating a host where it is not installed) and prove
    ``import hypergraph.materialization`` still succeeds, while touching
    ``LanceDBStore`` still raises the underlying ``ModuleNotFoundError``.
    """
    script = tmp_path / "materialization_no_lancedb_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import importlib.abc
            import sys

            class BlockLanceDB(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name == "lancedb" or name.startswith("lancedb."):
                        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                    return None

            sys.meta_path.insert(0, BlockLanceDB())

            import hypergraph.materialization  # must not raise

            assert "lancedb" not in sys.modules

            try:
                hypergraph.materialization.LanceDBStore
            except ModuleNotFoundError as exc:
                assert exc.name == "lancedb", f"unexpected missing module: {exc.name!r}"
            else:
                raise AssertionError("expected LanceDBStore access to raise ModuleNotFoundError")

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


def test_lancedb_store_still_resolves_when_available() -> None:
    """Behavior floor: with lancedb installed, ``LanceDBStore`` resolves as before."""
    from hypergraph.materialization import LanceDBStore
    from hypergraph.materialization._table_store import TableStore

    assert issubclass(LanceDBStore, TableStore)


def test_lancedb_store_in_module_all() -> None:
    """``LanceDBStore`` stays a public, lazily-resolved export."""
    import hypergraph.materialization as materialization

    assert "LanceDBStore" in materialization.__all__
