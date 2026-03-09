"""Smoke tests for standalone Daft examples."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

pytest.importorskip("daft", reason="daft extra is not installed")


EXAMPLE_PATHS = [
    Path("examples/daft/quickstart_customer_enrichment.py"),
    Path("examples/daft/hierarchical_document_batches.py"),
    Path("examples/daft/nested_review_objects.py"),
    Path("examples/daft/text_processing.py"),
    Path("examples/daft/async_api_calls.py"),
    Path("examples/daft/ml_embeddings.py"),
    Path("examples/daft/batch_normalization.py"),
    Path("examples/daft/nested_document_scoring.py"),
]


@pytest.mark.parametrize("example_path", EXAMPLE_PATHS, ids=lambda path: path.stem)
def test_daft_examples_run(example_path: Path) -> None:
    runpy.run_path(str(example_path), run_name="__main__")
