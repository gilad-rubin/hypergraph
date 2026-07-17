"""Exact-text Mermaid baselines (issue #263).

Each case freezes the FULL Mermaid source emitted today into a checked-in
``.mmd`` fixture (one file per case) and asserts byte-for-byte equality.
This is the before/after baseline for the Mermaid-on-GraphIR migration
(#212) and the container-entrypoint unification (#211).

KNOWN DELIBERATE CHANGE AHEAD: ``container_entrypoint_expanded.mmd`` freezes
today's self-INCLUSIVE container-entrypoint derivation (viz/renderer/scope.py).
When #211 switches the Mermaid path to the corrected self-EXCLUSIVE
derivation (as in viz/scene_builder.py), that fixture MUST be updated in the
same PR -- the fixture diff IS the visible evidence of the behavior change.

To regenerate after an INTENTIONAL change:

    HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines
"""

import difflib
import os
from pathlib import Path

import pytest

from tests.test_frozen_baselines.graph_cases import MERMAID_CASES

BASELINE_DIR = Path(__file__).parent / "baselines" / "mermaid"

_UPDATE_MODE = os.environ.get("HYPERGRAPH_UPDATE_BASELINES") == "1"

_GUIDANCE = """
Why this matters and what to do:

- If you did NOT intend to change visualization output, this is a BUG in the
  Mermaid/viz pipeline (or nondeterministic rendering). Fix the regression --
  do not update the fixture.

- If the change IS intentional (e.g. #211 entrypoint unification, #212
  Mermaid-on-GraphIR, a deliberate styling/layout change), regenerate:

      HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines

  then review the .mmd fixture diff -- it is the user-visible before/after
  evidence -- and describe the rendering change in your PR / changelog note.
"""


def _baseline_path(case_name: str) -> Path:
    return BASELINE_DIR / f"{case_name}.mmd"


def _current_source(case_name: str) -> str:
    builder, depth = MERMAID_CASES[case_name]
    graph = builder()
    # Trailing newline so the fixture is a well-formed text file.
    return graph.to_mermaid(depth=depth).source + "\n"


@pytest.mark.parametrize("case_name", sorted(MERMAID_CASES))
def test_frozen_mermaid_text(case_name: str) -> None:
    """The exact Mermaid source for this graph case matches the .mmd fixture."""
    current = _current_source(case_name)
    path = _baseline_path(case_name)

    if _UPDATE_MODE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current)

    if not path.exists():
        pytest.fail(
            f"Missing Mermaid baseline fixture: {path}\nGenerate it with: HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines"
        )

    frozen = path.read_text()
    if frozen != current:
        diff = "".join(
            difflib.unified_diff(
                frozen.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"frozen ({path.name})",
                tofile="current (graph.to_mermaid())",
            )
        )
        pytest.fail(f"Frozen Mermaid text changed for case {case_name!r}:\n\n{diff}\n{_GUIDANCE}")


def test_no_orphan_mermaid_fixtures() -> None:
    """Every checked-in .mmd fixture corresponds to a live Mermaid case."""
    on_disk = {p.stem for p in BASELINE_DIR.glob("*.mmd")}
    orphans = on_disk - set(MERMAID_CASES)
    assert not orphans, (
        f"Mermaid baseline fixtures without a matching case: {sorted(orphans)}. Delete the stale file(s) or restore the case in graph_cases.py."
    )


def test_mermaid_stable_within_process() -> None:
    """Rendering the same case twice yields identical text (determinism)."""
    for case_name in MERMAID_CASES:
        first = _current_source(case_name)
        second = _current_source(case_name)
        assert first == second, (
            f"Mermaid case {case_name!r} rendered differently across two "
            "builds in the same process. Rendering must be deterministic -- "
            "this is a bug, not a fixture problem."
        )
