"""Regression check for issue #214: `specs/*` must not silently hide new spec files.

`.gitignore` previously had a broad `specs/*` rule that hid any newly created
file under `specs/` (e.g. `specs/reviewed/new-contract.md`) from `git status`,
so it could be silently omitted from a PR. This test pins the fix by asserting
representative, non-existent spec paths are NOT ignored, and that an unrelated
generated-artifact path (`.venv/`) still is — so the check can't silently pass
because git itself is misbehaving.

Uses `git check-ignore` via subprocess against paths that do not exist on disk;
`check-ignore` evaluates purely by pattern matching, so this makes no repo
mutations and does no network access.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(relative_path: str) -> bool:
    """Return whether `relative_path` (need not exist) is git-ignored under ROOT."""
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", relative_path],
        cwd=ROOT,
        capture_output=True,
    )
    # git check-ignore exit codes: 0 = ignored, 1 = not ignored, >1 = fatal error.
    assert result.returncode in (0, 1), f"git check-ignore failed unexpectedly for {relative_path!r}: rc={result.returncode} stderr={result.stderr!r}"
    return result.returncode == 0


def test_new_reviewed_spec_is_not_ignored():
    assert not _is_ignored("specs/reviewed/_x.md")


def test_new_not_reviewed_spec_is_not_ignored():
    assert not _is_ignored("specs/not_reviewed/_x.md")


def test_unrelated_generated_artifact_is_still_ignored():
    # Control assertion: if this ever flips, `git check-ignore` itself is
    # broken (e.g. run outside a git checkout) and the two checks above
    # would be false negatives rather than a real fix.
    assert _is_ignored(".venv/_x")
