"""Capture PNG screenshots of legacy vs IR rendering for visual parity.

Renders each fixture in headless Chromium at fixed dimensions, captures
the React Flow viewport. Outputs paired PNGs to outputs/ir_parity/.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

OUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "ir_parity"


def capture(html_path: Path, png_path: Path, click_sequence: list[str] = ()) -> None:
    """Render html_path in browser, optionally click named nodes, capture PNG."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 600})
        page.goto(f"file://{html_path}")
        page.wait_for_timeout(1500)
        for label in click_sequence:
            page.locator(f'.react-flow__node:has-text("{label}")').first.click()
            page.wait_for_timeout(500)
        page.locator(".react-flow").first.screenshot(path=str(png_path))
        browser.close()


def main() -> None:
    fixtures = ["simple", "workflow", "outer"]
    for fix in fixtures:
        for path in ("legacy", "ir"):
            html = OUT_DIR / f"{fix}_{path}.html"
            png = OUT_DIR / f"{fix}_{path}_collapsed.png"
            capture(html, png)
            print(f"  wrote {png.name}")

    # Outer expanded both levels for the most interesting comparison
    for path in ("legacy", "ir"):
        html = OUT_DIR / f"outer_{path}.html"
        png = OUT_DIR / f"outer_{path}_expanded.png"
        capture(html, png, click_sequence=["middle", "inner"])
        print(f"  wrote {png.name}")


if __name__ == "__main__":
    main()
