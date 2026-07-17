"""Byte-frozen ``definition_hash`` / ``structural_hash`` baselines (issue #263).

These tests compare the EXACT hex digests emitted today against checked-in
fixture files (one JSON file per graph case). They are the byte-stability
tripwire for the graph-construction refactor (#208): a pure refactor must
leave every frozen digest untouched.

To regenerate after an INTENTIONAL change:

    HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines

then review the fixture diff and call out the compatibility impact in your
PR description (see the failure message below for what each hash protects).
"""

import json
import os
from pathlib import Path

import pytest

from tests.test_frozen_baselines.graph_cases import HASH_CASES

BASELINE_DIR = Path(__file__).parent / "baselines" / "hashes"

_UPDATE_MODE = os.environ.get("HYPERGRAPH_UPDATE_BASELINES") == "1"

_GUIDANCE = """
Why this matters and what to do:

- If you did NOT intend to change graph signatures or hashing, this is a BUG.
  definition_hash drift silently invalidates every cache entry keyed on it;
  structural_hash drift breaks checkpoint resume compatibility. Fix the
  regression -- do not update the fixture.

- If the change IS intentional (hash-input change, signature-format change,
  or an edit to tests/test_frozen_baselines/graph_cases.py), regenerate:

      HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines

  then review the fixture diff and add a changelog note in your PR
  explaining the cache/resume compatibility impact. #208 (graph-construction
  refactor) explicitly requires these bytes to stay IDENTICAL.
"""


def _baseline_path(case_name: str) -> Path:
    return BASELINE_DIR / f"{case_name}.json"


def _current_hashes(case_name: str) -> dict[str, str]:
    graph = HASH_CASES[case_name]()
    return {
        "definition_hash": graph.definition_hash,
        "structural_hash": graph.structural_hash,
    }


@pytest.mark.parametrize("case_name", sorted(HASH_CASES))
def test_frozen_hash_bytes(case_name: str) -> None:
    """The emitted hash bytes for this graph case match the frozen baseline."""
    current = _current_hashes(case_name)
    path = _baseline_path(case_name)

    if _UPDATE_MODE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    if not path.exists():
        pytest.fail(
            f"Missing hash baseline fixture: {path}\nGenerate it with: HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines"
        )

    frozen = json.loads(path.read_text())
    mismatches = [
        f"  {kind}:\n    frozen : {frozen[kind]}\n    current: {current[kind]}"
        for kind in ("definition_hash", "structural_hash")
        if frozen[kind] != current[kind]
    ]
    if mismatches:
        pytest.fail(f"Frozen hash bytes changed for graph case {case_name!r} (fixture: {path.name}):\n" + "\n".join(mismatches) + "\n" + _GUIDANCE)


def test_no_orphan_hash_fixtures() -> None:
    """Every checked-in hash fixture corresponds to a live graph case."""
    on_disk = {p.stem for p in BASELINE_DIR.glob("*.json")}
    orphans = on_disk - set(HASH_CASES)
    assert not orphans, (
        f"Hash baseline fixtures without a matching graph case: {sorted(orphans)}. Delete the stale file(s) or restore the case in graph_cases.py."
    )


def test_hashes_stable_within_process() -> None:
    """Rebuilding the same case twice yields identical digests (determinism)."""
    for case_name in HASH_CASES:
        first = _current_hashes(case_name)
        second = _current_hashes(case_name)
        assert first == second, (
            f"Graph case {case_name!r} produced different hashes across two "
            f"builds in the same process: {first} != {second}. Hashing must "
            "be deterministic -- this is a bug, not a fixture problem."
        )
