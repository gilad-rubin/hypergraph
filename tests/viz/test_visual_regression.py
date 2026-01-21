"""Visual regression tests for graph visualization.

Uses Playwright to render graphs and compare screenshots against baselines.
First run creates baselines, subsequent runs compare against them.
"""

from pathlib import Path
from typing import Optional

import pytest
from PIL import Image, ImageChops

# Get baseline directory relative to this test file
BASELINES_DIR = Path(__file__).parent / "baselines"


def compare_screenshots(
    actual_path: Path,
    baseline_path: Path,
    threshold: float = 0.01,
) -> tuple[bool, float]:
    """Compare two screenshots using pixel-by-pixel difference.

    Args:
        actual_path: Path to the screenshot to check
        baseline_path: Path to the baseline screenshot
        threshold: Maximum allowed difference (0.0 = identical, 1.0 = completely different)

    Returns:
        Tuple of (passed, difference_ratio) where:
        - passed: True if difference <= threshold
        - difference_ratio: Proportion of pixels that differ (0.0 to 1.0)

    Raises:
        AssertionError: If images have different dimensions
    """
    actual_img = Image.open(actual_path).convert("RGB")
    baseline_img = Image.open(baseline_path).convert("RGB")

    # Dimensions must match
    assert actual_img.size == baseline_img.size, (
        f"Image size mismatch: actual {actual_img.size} vs baseline {baseline_img.size}"
    )

    # Compute pixel-by-pixel difference
    diff = ImageChops.difference(actual_img, baseline_img)

    # Count non-zero pixels (pixels that differ)
    diff_pixels = sum(1 for pixel in diff.getdata() if pixel != (0, 0, 0))
    total_pixels = actual_img.size[0] * actual_img.size[1]

    difference_ratio = diff_pixels / total_pixels
    passed = difference_ratio <= threshold

    return passed, difference_ratio


@pytest.mark.slow
class TestVisualRegression:
    """Visual regression tests for graph rendering."""

    def _check_baseline(
        self,
        page,
        graph,
        page_with_graph,
        test_name: str,
        threshold: float = 0.01,
    ) -> None:
        """Helper to check visual regression for a graph.

        First run creates baseline, subsequent runs compare against it.

        Args:
            page: Playwright page fixture
            graph: Graph to render
            page_with_graph: Fixture to load graph into page
            test_name: Name for baseline file (e.g. "complex_rag")
            threshold: Maximum allowed pixel difference ratio
        """
        baseline_path = BASELINES_DIR / f"{test_name}.png"

        # Load graph in browser
        page_with_graph(page, graph)

        # Take screenshot
        screenshot_bytes = page.screenshot(full_page=True)

        if not baseline_path.exists():
            # First run: create baseline
            baseline_path.write_bytes(screenshot_bytes)
            pytest.skip(f"Created baseline: {baseline_path}")
        else:
            # Subsequent runs: compare against baseline
            # Save actual screenshot to temp location for comparison
            actual_path = baseline_path.parent / f"{test_name}_actual.png"
            actual_path.write_bytes(screenshot_bytes)

            try:
                passed, diff_ratio = compare_screenshots(
                    actual_path, baseline_path, threshold
                )

                # Clean up temp file if test passed
                if passed:
                    actual_path.unlink()

                assert passed, (
                    f"Visual regression failed: {diff_ratio:.2%} pixels differ "
                    f"(threshold: {threshold:.2%})\n"
                    f"Baseline: {baseline_path}\n"
                    f"Actual: {actual_path} (saved for inspection)"
                )
            except Exception:
                # Keep actual file on any error for debugging
                raise

    def test_complex_rag_visual(
        self, page, complex_rag_graph, page_with_graph
    ) -> None:
        """Visual regression test for complex RAG pipeline (19 nodes)."""
        try:
            self._check_baseline(
                page, complex_rag_graph, page_with_graph, "complex_rag"
            )
        except ImportError:
            pytest.skip("Playwright browsers not installed (run: playwright install chromium)")

    def test_nested_collapsed_visual(
        self, page, nested_graph, page_with_graph
    ) -> None:
        """Visual regression test for nested graph in collapsed state."""
        try:
            self._check_baseline(
                page, nested_graph, page_with_graph, "nested_collapsed"
            )
        except ImportError:
            pytest.skip("Playwright browsers not installed (run: playwright install chromium)")

    def test_double_nested_visual(
        self, page, double_nested_graph, page_with_graph
    ) -> None:
        """Visual regression test for double-nested graph."""
        try:
            self._check_baseline(
                page, double_nested_graph, page_with_graph, "double_nested"
            )
        except ImportError:
            pytest.skip("Playwright browsers not installed (run: playwright install chromium)")
