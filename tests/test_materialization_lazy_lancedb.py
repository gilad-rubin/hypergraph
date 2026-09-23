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


def test_materialization_import_loads_no_pyarrow() -> None:
    """A fresh interpreter: importing the package (SqliteTableStore included) pulls in no pyarrow."""
    code = "import sys, hypergraph.materialization; assert 'pyarrow' not in sys.modules, 'pyarrow was imported'; print('PROBE-OK')"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROBE-OK" in result.stdout


def test_sqlite_table_store_runs_a_table_without_lancedb_or_aiosqlite(tmp_path: Path) -> None:
    """With lancedb and aiosqlite both blocked, a Table on SqliteTableStore appends,
    reads and compare-and-sets. Blocking aiosqlite proves the store's import of the
    checkpointer's SQLite helpers does not load the async driver."""
    script = tmp_path / "sqlite_store_no_lancedb_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import importlib.abc
            import sys

            BLOCKED = ("lancedb", "aiosqlite")

            class Block(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in BLOCKED:
                        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                    return None

            sys.meta_path.insert(0, Block())

            from hypergraph.materialization import SqliteTableStore, Table

            store = SqliteTableStore()
            table = Table(identity="k", store=store)
            table.append(k="a", v=1)
            assert table.get("a") == {"k": "a", "v": 1}
            assert table.compare_and_set("a", expected={"v": 1}, v=2) is True
            assert table.get("a") == {"k": "a", "v": 2}
            store.close()

            assert not {"lancedb", "aiosqlite"} & set(sys.modules), sorted({"lancedb", "aiosqlite"} & set(sys.modules))
            print("PROBE-OK")
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROBE-OK" in result.stdout


def test_sqlite_table_store_runs_a_table_without_numpy(tmp_path: Path) -> None:
    """numpy is not a dependency (pyarrow does not require it), so with numpy
    blocked a Table on SqliteTableStore still appends, reads and compare-and-sets:
    value normalization has no numpy value to convert when numpy is absent."""
    script = tmp_path / "sqlite_store_no_numpy_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import importlib.abc
            import sys

            class Block(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] == "numpy":
                        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                    return None

            sys.meta_path.insert(0, Block())

            from hypergraph.materialization import SqliteTableStore, Table

            store = SqliteTableStore()
            table = Table(identity="k", store=store)
            table.append(k="a", v=1, tags=["x"])
            assert table.get("a") == {"k": "a", "v": 1, "tags": ["x"]}
            assert table.compare_and_set("a", expected={"v": 1}, v=2) is True
            assert table.get("a") == {"k": "a", "v": 2, "tags": ["x"]}
            assert table.rows() == [{"k": "a", "v": 2, "tags": ["x"]}]
            store.close()

            assert "numpy" not in sys.modules
            print("PROBE-OK")
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROBE-OK" in result.stdout


def test_reading_rows_without_numpy_does_not_retry_the_import_per_value(tmp_path: Path) -> None:
    """With numpy absent, value normalization must not attempt ``import numpy``
    for every cell it reads: a failed import is not cached, so each attempt walks
    the import machinery again (the D42 path cost ~30 us per cell)."""
    script = tmp_path / "sqlite_store_no_numpy_import_count_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import importlib.abc
            import sys

            attempts = []

            class Block(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] == "numpy":
                        attempts.append(name)
                        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                    return None

            sys.meta_path.insert(0, Block())

            from hypergraph.materialization import SqliteTableStore, Table

            store = SqliteTableStore()
            table = Table(identity="k", store=store)
            for i in range(50):
                table.append(k=f"k{i}", v=i, score=i / 2, tags=["x"])

            attempts.clear()
            rows = table.rows()
            store.close()

            assert len(rows) == 50 and rows[3] == {"k": "k3", "v": 3, "score": 1.5, "tags": ["x"]}, rows[3]
            assert len(attempts) <= 1, f"numpy import attempted {len(attempts)} times reading 50 rows x 4 columns"
            print("PROBE-OK")
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROBE-OK" in result.stdout


def test_numpy_values_still_normalize_when_numpy_is_present() -> None:
    """Behavior floor: with numpy imported, its scalars and arrays read back as plain Python."""
    import numpy as np

    from hypergraph.materialization._provenance import normalize_value

    assert normalize_value(np.array([1.5, 2.5])) == [1.5, 2.5]
    assert type(normalize_value(np.int64(3))) is int
    assert type(normalize_value(np.float32(0.5))) is float
    assert isinstance(normalize_value(np.bool_(True)), np.bool_), "only floating and integer scalars convert"
    assert normalize_value("text") == "text"


def test_numpy_mid_import_in_another_thread_is_waited_for_not_read_half_loaded(tmp_path: Path) -> None:
    """While one thread imports numpy, ``sys.modules["numpy"]`` already holds the
    half-initialized module. Value normalization in another thread must wait for
    that import, not read ``np.ndarray`` off the partial module (AttributeError)."""
    script = tmp_path / "numpy_import_race_probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            import threading

            from hypergraph.materialization._provenance import normalize_value
            from hypergraph.materialization._sqlite_store import _plain

            assert "numpy" not in sys.modules, "numpy was already imported"
            errors, calls, done = [], {}, threading.Event()

            def reader(normalize, started):
                started.set()
                while not done.is_set():
                    try:
                        normalize("text")
                    except Exception as exc:
                        errors.append(f"{normalize.__name__}: {type(exc).__name__}: {exc}")
                        return
                    calls[normalize.__name__] = calls.get(normalize.__name__, 0) + 1

            threads = []
            for normalize in (normalize_value, _plain):  # one thread each, so neither waits behind the other
                started = threading.Event()
                threads.append(threading.Thread(target=reader, args=(normalize, started)))
                threads[-1].start()
                started.wait()
            import numpy

            done.set()
            for thread in threads:
                thread.join()
            assert not errors, errors
            assert set(calls) == {"normalize_value", "_plain"}, calls
            assert normalize_value(numpy.int64(3)) == 3 and _plain(numpy.int64(3)) == 3
            print("PROBE-OK")
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "PROBE-OK" in result.stdout
