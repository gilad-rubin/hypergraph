"""Guard against accidental PEP 420 namespace portions under `src/hypergraph/`.

Issue #349 (and #256 item 1, the same defect with a different subject):

Git does not track directories, and `__pycache__/` is gitignored, so switching
away from a branch that added `src/hypergraph/<pkg>/` deletes that package's
`.py` files but leaves the directory behind — non-empty, because the stale
bytecode is still in it. A directory with no `__init__.py` sitting on a regular
package's `__path__` is a PEP 420 namespace portion, so `import hypergraph.<pkg>`
then SUCCEEDS as an empty namespace module with `__file__ = None`, and
`pytest.importorskip("hypergraph.<pkg>")` does not skip. The real failure lands
much later as `ImportError: cannot import name 'X' from 'hypergraph.<pkg>'
(unknown location)` — or as nothing at all, if the probe only imports the package.

`hypergraph` intends no namespace packages anywhere: every directory under
`src/hypergraph/` ships an `__init__.py`, so any namespace portion that appears
is by definition an artifact. This pins that invariant so a leftover directory
fails loudly here instead of silently turning an `importorskip` probe into a lie.

See dev/CONTRIBUTING.md § "Stale bytecode after a branch switch" for the cleanup.
"""

from __future__ import annotations

import os
from pathlib import Path

SRC_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "hypergraph"


def namespace_portions(package_root: Path) -> list[Path]:
    """Return every directory under `package_root` that would import as a namespace portion.

    A directory qualifies when it has no `__init__.py` of its own. `__pycache__`
    directories are skipped because Python never imports them as subpackages —
    but a directory whose *only* remaining content is a `__pycache__` is exactly
    the leftover this guard is looking for. Once a directory is reported, its
    children are not walked: they all inherit the same defect and listing them
    would only bury the offending parent.
    """
    offenders: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(package_root):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        directory = Path(dirpath)
        if not (directory / "__init__.py").exists():
            offenders.append(directory)
            dirnames[:] = []
    return offenders


def test_src_tree_has_no_namespace_portions():
    assert SRC_PACKAGE.is_dir(), f"expected the package source at {SRC_PACKAGE}; this test must run from a repo checkout"

    offenders = namespace_portions(SRC_PACKAGE)
    assert not offenders, (
        f"Directories under src/hypergraph/ have no __init__.py: {[str(p) for p in offenders]}. "
        "Python imports such a directory as an empty PEP 420 namespace package, so "
        "`import hypergraph.<pkg>` succeeds with __file__=None and `pytest.importorskip` does not skip. "
        "How to fix: if this is a leftover from a branch switch, delete the stale bytecode with "
        "`find . -name __pycache__ -type d -prune -exec rm -rf {} +` (and prefer a per-branch worktree); "
        "if it is a new subpackage, give it an __init__.py."
    )


def test_guard_reports_a_pycache_only_leftover(tmp_path):
    # The exact #349 shape: the .py files are gone, only stale bytecode keeps the directory alive.
    package_root = tmp_path / "hypergraph"
    (package_root / "host" / "__pycache__").mkdir(parents=True)
    (package_root / "__init__.py").touch()
    (package_root / "host" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"stale")

    assert namespace_portions(package_root) == [package_root / "host"]


def test_guard_accepts_a_well_formed_tree_that_contains_pycache(tmp_path):
    # Control assertion: without it, the real check above could pass vacuously
    # because the walk never looks at anything.
    package_root = tmp_path / "hypergraph"
    (package_root / "host" / "__pycache__").mkdir(parents=True)
    (package_root / "__init__.py").touch()
    (package_root / "host" / "__init__.py").touch()
    (package_root / "host" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"fresh")

    assert namespace_portions(package_root) == []
