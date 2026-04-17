"""Render the same nested graph under the old (all four sep/ext variants)
and new (single variant) precompute paths, capture payload sizes and a
Playwright screenshot for each, and print a summary.

Usage:
    uv run python scripts/compare_payload_size.py
Output:
    outputs/compare_payload_size/before.png
    outputs/compare_payload_size/after.png
    outputs/compare_payload_size/before.html
    outputs/compare_payload_size/after.html
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

from hypergraph import Graph, node
from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.precompute import precompute_all_edges, precompute_all_nodes


@node(output_name="s0")
def _s0(x0: int) -> int:
    return x0


@node(output_name="s1")
def _s1(x1: int) -> int:
    return x1


@node(output_name="s2")
def _s2(x2: int) -> int:
    return x2


@node(output_name="s3")
def _s3(x3: int) -> int:
    return x3


@node(output_name="s4")
def _s4(x4: int) -> int:
    return x4


def build_graph() -> Graph:
    subs = [
        Graph(nodes=[_s0], name="sub_0"),
        Graph(nodes=[_s1], name="sub_1"),
        Graph(nodes=[_s2], name="sub_2"),
        Graph(nodes=[_s3], name="sub_3"),
        Graph(nodes=[_s4], name="sub_4"),
    ]
    return Graph(nodes=[sub.as_node() for sub in subs], name="root")


def render_after(graph: Graph) -> dict:
    flat = graph.to_flat_graph()
    return render_graph(flat, depth=0, separate_outputs=False, show_inputs=True)


def render_before(graph: Graph) -> dict:
    """Replicate the pre-fix behavior by merging all four (sep, ext)
    precomputed payloads into one meta.
    """
    flat = graph.to_flat_graph()
    input_spec = flat.graph.get("input_spec", {})

    data = render_graph(flat, depth=0, separate_outputs=False, show_inputs=True)
    data = deepcopy(data)

    merged_nodes: dict = {}
    merged_edges: dict = {}
    for sep in (False, True):
        for ext in (False, True):
            n_map, _ = precompute_all_nodes(
                flat,
                input_spec,
                show_types=True,
                theme="auto",
                separate_outputs=sep,
                show_inputs=ext,
            )
            e_map, _ = precompute_all_edges(
                flat,
                input_spec,
                show_types=True,
                theme="auto",
                separate_outputs=sep,
                show_inputs=ext,
            )
            merged_nodes.update(n_map)
            merged_edges.update(e_map)

    data["meta"]["nodesByState"] = merged_nodes
    data["meta"]["edgesByState"] = merged_edges
    return data


def summarize(label: str, data: dict) -> dict:
    nodes_json = json.dumps(data["meta"]["nodesByState"])
    edges_json = json.dumps(data["meta"]["edgesByState"])
    total = json.dumps(data)
    return {
        "label": label,
        "nodesByState_keys": len(data["meta"]["nodesByState"]),
        "edgesByState_keys": len(data["meta"]["edgesByState"]),
        "nodesByState_bytes": len(nodes_json),
        "edgesByState_bytes": len(edges_json),
        "total_payload_bytes": len(total),
    }


async def capture(html: str, png_path: Path, video_dir: Path) -> None:
    """Write a PNG screenshot and a .webm recording of the rendered viz."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed; skipping capture")
        return

    video_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1200, "height": 800},
            record_video_dir=str(video_dir),
            record_video_size={"width": 1200, "height": 800},
        )
        page = await context.new_page()
        await page.set_content(html)
        try:
            await page.wait_for_function(
                "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
                timeout=10000,
            )
        except Exception:
            await page.wait_for_timeout(2000)
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(png_path), full_page=False)

        # Exercise a couple of containers to show interactivity in the video.
        for node_id in ("sub_0", "sub_1", "sub_2"):
            locator = page.locator(f'[data-id="{node_id}"]').first
            if await locator.count() > 0:
                try:
                    await locator.click(timeout=2000)
                except Exception:
                    pass
                await page.wait_for_timeout(500)

        await page.wait_for_timeout(500)
        await page.close()
        await context.close()
        await browser.close()


async def main() -> None:
    out = Path("outputs/compare_payload_size")
    out.mkdir(parents=True, exist_ok=True)

    graph = build_graph()
    before = render_before(graph)
    after = render_after(graph)

    before_html = generate_widget_html(before)
    after_html = generate_widget_html(after)

    (out / "before.html").write_text(before_html, encoding="utf-8")
    (out / "after.html").write_text(after_html, encoding="utf-8")

    summaries = [summarize("before (all variants)", before), summarize("after (single variant)", after)]

    before_total = summaries[0]["total_payload_bytes"]
    after_total = summaries[1]["total_payload_bytes"]
    ratio = before_total / max(after_total, 1)

    print("=" * 72)
    print(f"{'':<28} {'keys':>12} {'bytes':>16}")
    print("-" * 72)
    for s in summaries:
        print(
            f"{s['label']:<28} "
            f"nodes={s['nodesByState_keys']:>4} edges={s['edgesByState_keys']:>4}  "
            f"{s['total_payload_bytes']:>12,} bytes"
        )
    print("-" * 72)
    print(f"Shrinkage: {ratio:.2f}x smaller ({before_total - after_total:,} bytes saved per cell output)")
    print("=" * 72)

    await capture(before_html, out / "before.png", out / "video_before")
    await capture(after_html, out / "after.png", out / "video_after")

    # Rename the auto-generated .webm files to stable names.
    for subdir, new_name in ((out / "video_before", "before.webm"), (out / "video_after", "after.webm")):
        for webm in subdir.glob("*.webm"):
            webm.rename(out / new_name)
        subdir.rmdir()

    print(f"\nArtifacts written to {out.resolve()}/")
    for f in sorted(out.iterdir()):
        if f.is_file():
            print(f"  {f.name} ({f.stat().st_size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
