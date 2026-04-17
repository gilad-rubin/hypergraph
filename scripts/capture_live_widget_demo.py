"""Capture a GIF of the live HypergraphWidget round-trip for PR demos.

The live widget needs a Python kernel for real expansion; for a demo
we mock the kernel inside the page by listening for
`hypergraph-request-state` messages and posting back
`hypergraph-apply-state` with payloads pre-rendered by
`render_graph_single_state`. The viz.js side is the real thing —
what you see is exactly what a Jupyter user sees.

Usage:
    uv run python scripts/capture_live_widget_demo.py
Writes:
    docs/media/live_widget_demo.gif         (kernel-driven round-trip)
    docs/media/live_widget_kernel_hint.gif  (saved notebook, no kernel)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

from hypergraph import Graph, node
from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer import render_graph_single_state


@node(output_name="hits")
def retrieve(query: str) -> list[str]:
    return [query]


@node(output_name="docs")
def rerank(hits: list[str]) -> list[str]:
    return hits


@node(output_name="answer")
def llm(docs: list[str]) -> str:
    return docs[0]


def build_graph() -> Graph:
    inner = Graph(nodes=[retrieve, rerank], name="retrieve_and_rerank")
    outer = Graph(nodes=[inner.as_node(), llm], name="rag")
    return outer


HOST_MOCK = """
(payloads) => {
    window.__hgRequests = [];
    window.__hgPayloads = payloads;
    window.addEventListener('message', (ev) => {
        if (!ev.data || ev.data.type !== 'hypergraph-request-state') return;
        window.__hgRequests.push(ev.data);
        // Pick the response keyed on the expansion state signature.
        const exp = (ev.data.displayState && ev.data.displayState.expansion) || {};
        const key = Object.keys(exp).sort().map(k => k + ':' + (exp[k] ? '1' : '0')).join(',');
        const resp = payloads[key] || payloads['__default__'];
        if (resp) {
            setTimeout(() => {
                window.postMessage({
                    type: 'hypergraph-apply-state',
                    requestId: ev.data.requestId,
                    graphData: resp,
                }, '*');
            }, 120);
        }
    });

    // Synthetic cursor for the screencast.
    const cursor = document.createElement('div');
    cursor.id = '__hg_cursor';
    cursor.style.cssText = `
        position: fixed; width: 22px; height: 22px; pointer-events: none;
        left: 0; top: 0; z-index: 99999; transition: left 220ms ease, top 220ms ease;
        background: radial-gradient(circle at 30% 30%, #fff 0%, #fff 30%, #1e293b 30%, #1e293b 34%, transparent 35%);
        filter: drop-shadow(0 2px 4px rgba(0,0,0,0.5));
    `;
    document.body.appendChild(cursor);
    window.__hgMoveCursor = (x, y) => { cursor.style.left = (x - 6) + 'px'; cursor.style.top = (y - 2) + 'px'; };

    // Click ripple.
    window.__hgRipple = (x, y) => {
        const r = document.createElement('div');
        r.style.cssText = `
            position: fixed; left: ${x - 14}px; top: ${y - 14}px; width: 28px; height: 28px;
            border-radius: 50%; border: 2px solid #38bdf8; pointer-events: none;
            z-index: 99998; opacity: 0.9; transition: transform 400ms ease, opacity 400ms ease;
        `;
        document.body.appendChild(r);
        requestAnimationFrame(() => {
            r.style.transform = 'scale(2.2)';
            r.style.opacity = '0';
        });
        setTimeout(() => r.remove(), 500);
    };
}
"""


async def snap(page, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path), omit_background=False)


async def sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


async def record_frames(page, frames_dir: Path, count: int, interval_ms: int = 100) -> list[Path]:
    """Record N frames every interval_ms. Returns the ordered paths."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i in range(count):
        p = frames_dir / f"frame_{i:04d}.png"
        await page.screenshot(path=str(p))
        out.append(p)
        await sleep(interval_ms)
    return out


def frames_to_gif(frames: list[Path], out_path: Path, duration_ms: int = 100, scale: float = 1.0) -> None:
    """Stitch PNG frames into an optimized palette-GIF."""
    images = []
    for p in frames:
        img = Image.open(p).convert("RGB")
        if scale != 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        # Convert to palette mode for smaller GIF.
        images.append(img.quantize(colors=128, method=Image.Quantize.MAXCOVERAGE))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        out_path,
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )


async def drive_demo(page, flat, frames_dir: Path) -> list[Path]:
    """Scene: user expands `rag/retrieve_and_rerank`, then collapses it."""

    # Pre-compute the three expansion states the demo will bounce between.
    st_collapsed = render_graph_single_state(flat, depth=0)
    inner_container_id = st_collapsed["meta"]["expandableNodes"][0]
    st_expanded = render_graph_single_state(flat, expansion_state={inner_container_id: True})

    payloads = {
        "": st_collapsed,
        f"{inner_container_id}:0": st_collapsed,
        f"{inner_container_id}:1": st_expanded,
        "__default__": st_collapsed,
    }
    await page.evaluate(HOST_MOCK, payloads)

    # Wait for layout.
    await page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
        timeout=10000,
    )
    await sleep(400)

    frames: list[Path] = []

    async def capture_series(n: int, interval_ms: int = 90, prefix: str = "f") -> None:
        start = len(frames)
        for i in range(n):
            p = frames_dir / f"{prefix}_{start + i:04d}.png"
            await page.screenshot(path=str(p))
            frames.append(p)
            await sleep(interval_ms)

    # 1. Idle on the collapsed state.
    await capture_series(8, prefix="idle1")

    # 2. Move cursor to the container, capture frames showing the glide.
    # The container id is the unqualified name (it's a top-level child of the outer graph).
    self_container_selector = f'[data-id="{inner_container_id}"]'
    container = page.locator(self_container_selector).first
    box = await container.bounding_box()
    assert box is not None, "Inner container not found"
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.evaluate("([x,y]) => window.__hgMoveCursor(x, y)", [cx - 220, cy - 80])
    await sleep(60)
    await page.evaluate("([x,y]) => window.__hgMoveCursor(x, y)", [cx, cy])
    await capture_series(4, prefix="glide")

    # 3. Click + ripple animation: expand.
    await page.evaluate("([x,y]) => window.__hgRipple(x, y)", [cx, cy])
    await container.click()
    await capture_series(16, prefix="expand", interval_ms=80)

    # 4. Hold on the expanded view so viewers see the result of the round-trip.
    await capture_series(16, prefix="hold", interval_ms=110)

    return frames


async def drive_kernel_hint(page, frames_dir: Path) -> list[Path]:
    """Scene: saved notebook opened without a kernel. Host records
    requests but never replies → banner shows after timeout."""

    await page.evaluate(
        """() => {
            window.__hgRequests = [];
            window.addEventListener('message', (ev) => {
                if (ev.data && ev.data.type === 'hypergraph-request-state') {
                    window.__hgRequests.push(ev.data);
                }
            });
            const cursor = document.createElement('div');
            cursor.style.cssText = `
                position: fixed; width: 22px; height: 22px; pointer-events: none;
                left: 0; top: 0; z-index: 99999; transition: left 220ms ease, top 220ms ease;
                background: radial-gradient(circle at 30% 30%, #fff 0%, #fff 30%, #1e293b 30%, #1e293b 34%, transparent 35%);
                filter: drop-shadow(0 2px 4px rgba(0,0,0,0.5));
            `;
            document.body.appendChild(cursor);
            window.__hgMoveCursor = (x, y) => { cursor.style.left = (x - 6) + 'px'; cursor.style.top = (y - 2) + 'px'; };
        }"""
    )
    await page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
        timeout=10000,
    )
    await sleep(300)

    frames: list[Path] = []

    async def capture_series(n: int, interval_ms: int = 150, prefix: str = "h") -> None:
        start = len(frames)
        for i in range(n):
            p = frames_dir / f"{prefix}_{start + i:04d}.png"
            await page.screenshot(path=str(p))
            frames.append(p)
            await sleep(interval_ms)

    # Target the same expandable container as the live scene.
    collapsed = render_graph_single_state(
        Graph(nodes=[retrieve, rerank], name="retrieve_and_rerank").as_node().graph.to_flat_graph(), depth=0
    )
    # Simpler: just read the expandable node id off viz.js state.
    expandable = await page.evaluate("window.__hypergraphVizDebug.nodes.filter(n => n.nodeType === 'PIPELINE').map(n => n.id)")
    inner_container_id = expandable[0]
    container = page.locator(f'[data-id="{inner_container_id}"]').first
    box = await container.bounding_box()
    assert box is not None
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    await capture_series(4, prefix="idle")
    await page.evaluate("([x,y]) => window.__hgMoveCursor(x, y)", [cx, cy])
    await sleep(120)
    await container.click()
    # Capture during the ~2.5s timeout window.
    await capture_series(22, prefix="wait", interval_ms=150)
    # After timeout banner appears — hold on it.
    await capture_series(10, prefix="banner", interval_ms=160)

    return frames


async def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "docs" / "media"
    tmp_root = repo_root / "outputs" / "live_widget_demo"
    html_path = tmp_root / "demo.html"
    tmp_root.mkdir(parents=True, exist_ok=True)

    flat = build_graph().to_flat_graph()
    initial = render_graph_single_state(flat, depth=0)
    html_path.write_text(generate_widget_html(initial), encoding="utf-8")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # --- Scene 1: live round-trip ---
        ctx = await browser.new_context(
            viewport={"width": 860, "height": 520},
            device_scale_factor=1,
        )
        page = await ctx.new_page()
        await page.goto(f"file://{html_path}")
        frames = await drive_demo(page, flat, tmp_root / "frames_live")
        frames_to_gif(frames, out_dir / "live_widget_demo.gif", duration_ms=90, scale=0.75)
        print(f"Wrote {out_dir / 'live_widget_demo.gif'} "
              f"({(out_dir / 'live_widget_demo.gif').stat().st_size:,} bytes, {len(frames)} frames)")
        await ctx.close()

        # --- Scene 2: kernel-hint fallback ---
        ctx = await browser.new_context(
            viewport={"width": 860, "height": 520},
            device_scale_factor=1,
        )
        page = await ctx.new_page()
        await page.goto(f"file://{html_path}")
        frames = await drive_kernel_hint(page, tmp_root / "frames_hint")
        frames_to_gif(frames, out_dir / "live_widget_kernel_hint.gif", duration_ms=140, scale=0.75)
        print(f"Wrote {out_dir / 'live_widget_kernel_hint.gif'} "
              f"({(out_dir / 'live_widget_kernel_hint.gif').stat().st_size:,} bytes, {len(frames)} frames)")
        await ctx.close()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
