"""Ticket #185 policy, resource, and browser falsifiers for presenters."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.resources import files

import pytest

from hypergraph._repr import STATUS_COLORS, STATUS_PALETTE, status_badge
from hypergraph.checkpointers.presenters import (
    _aggregate_workflow_status,
    _read_explorer_asset,
    render_checkpointer_explorer_html,
)
from hypergraph.checkpointers.types import Run, RunTable, StepRecord, StepStatus, WorkflowStatus


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], WorkflowStatus.COMPLETED),
        ([WorkflowStatus.COMPLETED], WorkflowStatus.COMPLETED),
        ([WorkflowStatus.STOPPED, WorkflowStatus.COMPLETED], WorkflowStatus.STOPPED),
        ([WorkflowStatus.FAILED], WorkflowStatus.FAILED),
        ([WorkflowStatus.FAILED, WorkflowStatus.COMPLETED], WorkflowStatus.PARTIAL),
        ([WorkflowStatus.PARTIAL, WorkflowStatus.STOPPED], WorkflowStatus.PARTIAL),
        ([WorkflowStatus.PAUSED, WorkflowStatus.FAILED], WorkflowStatus.PAUSED),
        ([WorkflowStatus.ACTIVE, WorkflowStatus.PAUSED], WorkflowStatus.ACTIVE),
    ],
)
def test_aggregate_workflow_status_has_one_explicit_precedence(
    statuses: list[WorkflowStatus],
    expected: WorkflowStatus,
) -> None:
    assert _aggregate_workflow_status(statuses) is expected


def test_run_table_synthetic_parent_consumes_canonical_aggregate_policy() -> None:
    table = RunTable(
        [
            Run(id="batch/0", status=WorkflowStatus.FAILED),
            Run(id="batch/1", status=WorkflowStatus.COMPLETED),
        ]
    )

    html = table._repr_html_()

    assert html is not None
    assert 'data-id="batch" data-status="partial"' in html


def test_status_palette_is_the_badge_and_explorer_color_owner() -> None:
    assert STATUS_PALETTE["stopped"] == ("#92400e", "#fff7ed")
    assert {status: pair[0] for status, pair in STATUS_PALETTE.items()} == STATUS_COLORS
    foreground, background = STATUS_PALETTE["stopped"]
    assert f"color:{foreground}" in status_badge("stopped")
    assert f"background:{background}" in status_badge("stopped")

    html = _explorer_html("palette", "safe")
    assert foreground in html and background in html
    asset = _read_explorer_asset()
    assert "#92400e" not in asset
    assert "#fff7ed" not in asset


def test_explorer_asset_is_a_real_package_resource() -> None:
    resource = files("hypergraph.checkpointers._assets").joinpath("explorer.js")
    assert resource.is_file()
    assert resource.read_text(encoding="utf-8") == _read_explorer_asset()


def test_missing_and_corrupt_explorer_assets_fail_loudly(monkeypatch) -> None:
    import hypergraph.checkpointers.presenters as presenters

    class _MissingResource:
        def joinpath(self, _name: str) -> _MissingResource:
            return self

        def read_text(self, *, encoding: str) -> str:
            raise FileNotFoundError("explorer.js missing")

    monkeypatch.setattr(presenters, "files", lambda _package: _MissingResource())
    with pytest.raises(FileNotFoundError, match="explorer.js missing"):
        presenters._read_explorer_asset()

    class _CorruptResource(_MissingResource):
        def read_text(self, *, encoding: str) -> str:
            return "not the explorer"

    monkeypatch.setattr(presenters, "files", lambda _package: _CorruptResource())
    with pytest.raises(RuntimeError, match="corrupt"):
        presenters._read_explorer_asset()


def _explorer_html(path: str, dangerous: str) -> str:
    created = datetime(2026, 7, 12, tzinfo=timezone.utc)
    root = Run(
        id=f"{path}-root",
        status=WorkflowStatus.COMPLETED,
        graph_name="pipeline",
        created_at=created,
    )
    child = Run(
        id=f"{path}-child",
        status=WorkflowStatus.STOPPED,
        graph_name="pipeline",
        forked_from=root.id,
        created_at=created,
    )
    steps = [
        StepRecord(
            run_id=root.id,
            superstep=0,
            node_name="first",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"safe": True},
        ),
        StepRecord(
            run_id=root.id,
            superstep=1,
            node_name="second",
            index=1,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"dangerous": dangerous},
        ),
    ]
    return render_checkpointer_explorer_html(
        title=f"Explorer {path}",
        path=path,
        state_key=f"state-{path}",
        runs=[root, child],
        steps_by_run={root.id: steps, child.id: []},
    )


def test_real_dom_proves_navigation_safety_bind_once_and_isolation() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    dangerous = "</script><img id='owned' src=x onerror='document.body.dataset.pwned=1'>"
    html_a = _explorer_html("alpha", dangerous)
    html_b = _explorer_html("beta", "other")

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(f"<!doctype html><html><body>{html_a}{html_b}</body></html>")
        roots = page.locator('[data-hg-explorer="checkpointer"]')
        assert roots.count() == 2
        first = roots.nth(0)
        second = roots.nth(1)

        assert "alpha-root" in first.locator("[data-hg-explorer-header]").inner_text()
        assert "beta-root" in second.locator("[data-hg-explorer-header]").inner_text()
        assert "Recent Steps" in first.locator("[data-hg-explorer-body]").inner_text()

        first.get_by_role("button", name="Steps", exact=True).click()
        first.locator('tr[data-step-index="1"]').click()
        detail = first.locator("[data-hg-explorer-body]").inner_text()
        assert "Step Detail" in detail and "second" in detail
        assert dangerous in detail
        assert page.locator("#owned").count() == 0
        assert page.locator("body").get_attribute("data-pwned") is None

        first.get_by_role("button", name="Lineage", exact=True).click()
        lineage = first.locator("[data-hg-explorer-body]").inner_text()
        assert "Ancestry" in lineage and "Descendants" in lineage

        first.get_by_role("button", name="alpha-child", exact=True).first.click()
        assert "alpha-child" in first.locator("[data-hg-explorer-header]").inner_text()
        assert "beta-root" in second.locator("[data-hg-explorer-header]").inner_text()

        asset = _read_explorer_asset()
        rebound = first.evaluate(
            """(root, source) => {
              let additions = 0;
              const original = root.addEventListener;
              root.addEventListener = function() {
                additions += 1;
                return original.apply(this, arguments);
              };
              const script = document.createElement('script');
              script.textContent = source;
              root.appendChild(script);
              root.addEventListener = original;
              return additions;
            }""",
            asset,
        )
        assert rebound == 0
        assert first.evaluate("root => root.__hgExplorerBound") is True
        browser.close()


def test_explorer_payload_and_config_are_script_safe_without_generated_ids() -> None:
    dangerous = "</script><script>globalThis.owned=true</script>"
    html = _explorer_html("safe", dangerous)
    assert dangerous not in html
    assert "\\u003c/script\\u003e" in html
    assert "data-hg-explorer-config" in html
    assert "document.getElementById" not in _read_explorer_asset()
