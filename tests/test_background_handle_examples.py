"""Fresh-process coverage for the public background-handle examples."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script_name", "expected_lines"),
    [
        pytest.param(
            "background_handles_sync.py",
            (
                "Before release: caller regained control; done=False",
                "After release: charged order-100; done=True",
                "Inspected batch: requested=3, settled=3, failed=1",
                "Default retrieval raises: risk service rejected order-bad",
            ),
            id="sync",
        ),
        pytest.param(
            "background_handles_async.py",
            (
                "Before release: caller regained control; done=False",
                "After cancelling one waiter: execution still live; done=False",
                "After release: charged order-200; done=True",
                "Inspected batch: requested=3, settled=3, failed=1",
                "Default retrieval raises: risk service rejected order-bad",
            ),
            id="async",
        ),
    ],
)
def test_background_handle_example_in_fresh_process(
    script_name: str,
    expected_lines: tuple[str, ...],
) -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / script_name)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    for expected_line in expected_lines:
        assert expected_line in completed.stdout
