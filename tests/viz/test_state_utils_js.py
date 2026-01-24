"""JS unit tests for state_utils visibility logic (via Playwright)."""

from pathlib import Path

import pytest

from tests.viz.conftest import HAS_PLAYWRIGHT


STATE_UTILS_PATH = Path(__file__).resolve().parents[2] / "src" / "hypergraph" / "viz" / "assets" / "state_utils.js"


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_apply_visibility_hides_internal_data(page):
    """Internal-only DATA nodes should hide when parent container is collapsed."""
    js = STATE_UTILS_PATH.read_text(encoding="utf-8")
    page.set_content(f"<html><head><script>{js}</script></head><body></body></html>")
    page.wait_for_function("window.HypergraphVizState && window.HypergraphVizState.applyVisibility")

    hidden = page.evaluate(
        """() => {
            const nodes = [
              { id: "inner", data: { nodeType: "PIPELINE" } },
              { id: "data_inner_x", parentNode: "inner", data: { nodeType: "DATA", internalOnly: true } },
            ];
            const result = window.HypergraphVizState.applyVisibility(nodes, { inner: false });
            return result.find(n => n.id === "data_inner_x").hidden;
        }"""
    )
    assert hidden is True


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_apply_visibility_hides_owned_inputs_when_collapsed(page):
    """Owned INPUT nodes should hide when their container is collapsed."""
    js = STATE_UTILS_PATH.read_text(encoding="utf-8")
    page.set_content(f"<html><head><script>{js}</script></head><body></body></html>")
    page.wait_for_function("window.HypergraphVizState && window.HypergraphVizState.applyVisibility")

    hidden = page.evaluate(
        """() => {
            const nodes = [
              { id: "inner", data: { nodeType: "PIPELINE" } },
              {
                id: "input_query",
                data: { nodeType: "INPUT", ownerContainer: "inner", deepestOwnerContainer: "inner" }
              },
            ];
            const result = window.HypergraphVizState.applyVisibility(nodes, { inner: false });
            return result.find(n => n.id === "input_query").hidden;
        }"""
    )
    assert hidden is True


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_apply_visibility_keeps_owned_inputs_when_expanded(page):
    """Owned INPUT nodes should remain visible when their container is expanded."""
    js = STATE_UTILS_PATH.read_text(encoding="utf-8")
    page.set_content(f"<html><head><script>{js}</script></head><body></body></html>")
    page.wait_for_function("window.HypergraphVizState && window.HypergraphVizState.applyVisibility")

    hidden = page.evaluate(
        """() => {
            const nodes = [
              { id: "inner", data: { nodeType: "PIPELINE" } },
              {
                id: "input_query",
                data: { nodeType: "INPUT", ownerContainer: "inner", deepestOwnerContainer: "inner" }
              },
            ];
            const result = window.HypergraphVizState.applyVisibility(nodes, { inner: true });
            return result.find(n => n.id === "input_query").hidden;
        }"""
    )
    assert hidden is False
