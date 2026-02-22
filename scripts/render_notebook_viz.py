#!/usr/bin/env python3
"""Render all visualize() calls from notebooks into HTML files.

Runs code cells in each notebook and intercepts visualize calls to write
HTML to an output directory. Builds an index.html and opens it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import traceback
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Callable

NOTEBOOKS_DEFAULT = [
    "notebooks/test_viz_layout.ipynb",
    "notebooks/visualization_examples.ipynb",
]


@dataclass
class VizRecord:
    notebook: str
    index: int
    filepath: Path
    graph_name: str | None
    kwargs: dict[str, Any]


class VizCapture:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.records: list[VizRecord] = []
        self._counter = 0
        self._notebook: Path | None = None
        self._orig_visualize: Callable[..., Any] | None = None

    def set_notebook(self, notebook_path: Path) -> None:
        self._notebook = notebook_path
        self._counter = 0

    def set_original(self, visualize_func: Callable[..., Any]) -> None:
        self._orig_visualize = visualize_func

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
        return slug or "graph"

    def _next_filepath(self, graph: Any) -> Path:
        if not self._notebook:
            notebook_part = "notebook"
        else:
            notebook_part = self._slugify(self._notebook.stem)

        graph_name = getattr(graph, "name", None) or getattr(graph, "_name", None)
        graph_part = self._slugify(graph_name) if graph_name else "graph"

        filename = f"{notebook_part}_{self._counter:03d}_{graph_part}.html"
        return self.output_dir / filename

    def _record(self, graph: Any, filepath: Path, kwargs: dict[str, Any]) -> None:
        graph_name = getattr(graph, "name", None) or getattr(graph, "_name", None)
        self.records.append(
            VizRecord(
                notebook=str(self._notebook) if self._notebook else "",
                index=self._counter,
                filepath=filepath,
                graph_name=graph_name,
                kwargs=dict(kwargs),
            )
        )

    def visualize(self, graph: Any, *args: Any, **kwargs: Any) -> Any:
        if self._orig_visualize is None:
            raise RuntimeError(
                "VizCapture not initialized with original visualize function"
            )

        # Notebooks sometimes call visualize(width=..., height=...) which is not supported.
        kwargs.pop("width", None)
        kwargs.pop("height", None)
        # Always disable debug overlays for gallery output.
        kwargs["_debug_overlays"] = False

        # Capture to gallery output dir, but also honor caller's filepath
        # (extract_debug_data needs its temp file to exist for Playwright)
        caller_filepath = kwargs.pop("filepath", None)
        filepath = self._next_filepath(graph)
        kwargs["filepath"] = str(filepath)

        result = self._orig_visualize(graph, *args, **kwargs)

        # If the caller specified a different filepath, copy there too
        if caller_filepath and str(Path(caller_filepath).resolve()) != str(filepath.resolve()):
            import shutil
            shutil.copy2(str(filepath), str(caller_filepath))

        self._record(graph, filepath, kwargs)
        self._counter += 1
        return result


def load_notebook(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def exec_notebook_cells(
    nb: dict[str, Any],
    globals_ns: dict[str, Any],
    nb_path: Path,
    *,
    verbose: bool,
) -> None:
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue
        try:
            if verbose:
                exec(  # noqa: S102  # intentional use of exec to execute notebook cells (trusted input)
                    compile(source, str(nb_path), "exec"),
                    globals_ns,
                )
            else:
                stdout_buffer = StringIO()
                stderr_buffer = StringIO()
                with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
                    exec(  # noqa: S102  # intentional use of exec to execute notebook cells (trusted input)
                        compile(source, str(nb_path), "exec"),
                        globals_ns,
                    )
        except Exception as exc:
            print(f"\n[ERROR] {nb_path} cell {i} failed:")
            print(source)
            print("\n" + "-" * 80)
            if not verbose:
                print(stdout_buffer.getvalue())
                print(stderr_buffer.getvalue())
            traceback.print_exception(exc)
            raise


def build_index(
    output_dir: Path, records: list[VizRecord], *, iframe_height: int
) -> Path:
    records_by_notebook: dict[str, list[VizRecord]] = {}
    for record in records:
        records_by_notebook.setdefault(record.notebook, []).append(record)

    lines = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "  <meta charset='UTF-8'>",
        "  <title>Hypergraph Viz Gallery</title>",
        "  <style>",
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
        " padding: 20px; padding-top: 80px; }",
        "    h1 { margin-bottom: 6px; }",
        "    h2 { margin-top: 28px; }",
        "    h3 { margin: 16px 0 6px; }",
        "    ul { line-height: 1.7; }",
        "    .meta { color: #666; font-size: 12px; }",
        "    .card { margin: 16px 0 28px; padding-bottom: 12px; border-bottom: 1px solid #eee; }",
        f"    iframe {{ width: 100%; height: {iframe_height}px; border: 1px solid #ddd;"
        " border-radius: 8px; }}",
        "    .dialkit { position: fixed; top: 0; left: 0; right: 0; z-index: 100;"
        " background: #1e293b; border-bottom: 1px solid #334155; padding: 10px 20px;"
        " display: flex; align-items: center; gap: 24px; font-size: 13px; color: #e2e8f0; }",
        "    .dialkit label { display: flex; align-items: center; gap: 6px; cursor: pointer; }",
        "    .dialkit .group { display: flex; align-items: center; gap: 8px; }",
        "    .dialkit .value { font-family: ui-monospace, monospace; font-size: 12px;"
        " color: #94a3b8; min-width: 36px; text-align: right; }",
        "    .dialkit input[type=range] { width: 120px; accent-color: #818cf8; }",
        "    .dialkit input[type=checkbox] { accent-color: #818cf8; }",
        "    .dialkit .title { font-weight: 600; color: #94a3b8; font-size: 11px;"
        " text-transform: uppercase; letter-spacing: 0.05em; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <div class='dialkit' id='dialkit'>",
        "    <span class='title'>Edge Routing</span>",
        "    <label>",
        "      <input type='checkbox' id='dk-converge' checked>",
        "      Converge to center",
        "    </label>",
        "    <div class='group' id='dk-offset-group'>",
        "      <span>Stem height</span>",
        "      <input type='range' id='dk-offset' min='0' max='60' step='1' value='20'>",
        "      <span class='value' id='dk-offset-value'>20px</span>",
        "    </div>",
        "  </div>",
        "  <h1>Hypergraph Viz Gallery</h1>",
        "  <div class='meta'>Generated by scripts/render_notebook_viz.py</div>",
    ]

    for notebook, items in records_by_notebook.items():
        lines.append(f"  <h2>{notebook}</h2>")
        for item in items:
            raw_rel = os.path.relpath(item.filepath, output_dir)
            rel = raw_rel.replace(os.sep, "/")
            details = []
            for key in (
                "depth",
                "theme",
                "show_types",
                "separate_outputs",
                "width",
                "height",
            ):
                if key in item.kwargs and item.kwargs[key] is not None:
                    details.append(f"{key}={item.kwargs[key]}")
            detail_str = " | ".join(details)
            label = item.graph_name or f"graph {item.index}"
            lines.append("  <div class='card'>")
            lines.append(f"    <h3>{label}</h3>")
            lines.append(
                f"    <div><a href='{rel}' target='_blank'>Open in new tab</a>"
                f" <span class='meta'>{detail_str}</span></div>"
            )
            lines.append(f"    <iframe src='{rel}' loading='lazy'></iframe>")
            lines.append("  </div>")

    lines.extend([
        "  <script>",
        "  (function() {",
        "    var convergeEl = document.getElementById('dk-converge');",
        "    var offsetEl = document.getElementById('dk-offset');",
        "    var offsetValueEl = document.getElementById('dk-offset-value');",
        "    var offsetGroup = document.getElementById('dk-offset-group');",
        "",
        "    function broadcast() {",
        "      var opts = {",
        "        convergeToCenter: convergeEl.checked,",
        "        convergenceOffset: Number(offsetEl.value),",
        "      };",
        "      offsetGroup.style.opacity = convergeEl.checked ? '1' : '0.3';",
        "      offsetGroup.style.pointerEvents = convergeEl.checked ? 'auto' : 'none';",
        "      offsetValueEl.textContent = offsetEl.value + 'px';",
        "      var msg = { type: 'hypergraph-set-options', options: opts };",
        "      var iframes = document.querySelectorAll('iframe');",
        "      iframes.forEach(function(iframe) {",
        "        try { iframe.contentWindow.postMessage(msg, '*'); } catch(e) {}",
        "      });",
        "    }",
        "",
        "    convergeEl.addEventListener('change', broadcast);",
        "    offsetEl.addEventListener('input', broadcast);",
        "    broadcast();",
        "  })();",
        "  </script>",
        "</body>",
        "</html>",
    ])
    index_path = output_dir / "index.html"
    index_path.write_text("\n".join(lines))
    return index_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render all visualize() calls from notebooks."
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/viz_gallery",
        help="Directory to write HTML files (default: outputs/viz_gallery)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the generated index.html",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show notebook output while executing cells",
    )
    parser.add_argument(
        "--iframe-height",
        type=int,
        default=1200,
        help="Iframe height for embedded previews (default: 1200)",
    )
    parser.add_argument(
        "notebooks",
        nargs="*",
        default=NOTEBOOKS_DEFAULT,
        help="Notebook paths to execute",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    capture = VizCapture(output_dir)

    # Patch visualize before executing any notebook cells.
    import hypergraph.viz as viz
    from hypergraph import Graph

    original_visualize = viz.visualize
    capture.set_original(original_visualize)
    viz.visualize = capture.visualize  # type: ignore[assignment]

    original_graph_visualize = Graph.visualize

    def graph_visualize_wrapper(self: Graph, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        kwargs.pop("width", None)
        kwargs.pop("height", None)
        kwargs.pop("_debug_overlays", None)
        return viz.visualize(self, *args, **kwargs)

    Graph.visualize = graph_visualize_wrapper  # type: ignore[assignment]

    # Also patch the widget module reference (Graph.visualize imports from hypergraph.viz)
    try:
        import hypergraph.viz.widget as widget
    except ImportError:
        widget = None

    if widget is not None:
        widget.visualize = capture.visualize  # type: ignore[assignment]

    # Run notebooks
    for notebook_path in args.notebooks:
        nb_path = Path(notebook_path)
        if not nb_path.exists():
            raise FileNotFoundError(f"Notebook not found: {nb_path}")
        capture.set_notebook(nb_path)
        nb = load_notebook(nb_path)

        globals_ns: dict[str, Any] = {
            "__file__": str(nb_path),
            "__name__": "__main__",
        }
        exec_notebook_cells(nb, globals_ns, nb_path, verbose=args.verbose)

    index_path = build_index(
        output_dir, capture.records, iframe_height=args.iframe_height
    )
    print(f"Wrote {len(capture.records)} visualizations to {output_dir}")
    print(f"Index: {index_path}")

    if not args.no_open:
        webbrowser.open(index_path.resolve().as_uri())


if __name__ == "__main__":
    main()
