"""`import hypergraph` must work with only the declared base dependencies.

A top-level import of a module whose dependency sits in an optional extra passes
every other test (the dev environment has all extras) and fails for a user who
installs the bare package.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_import_in_clean_environment_without_extras():
    result = subprocess.run(
        ["uv", "run", "--no-project", "--isolated", "--with", str(ROOT), "python", "-I", "-c", "import hypergraph"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
