# Recovered Demos — start_run + Live Inspect Widget

Visual artifacts from the original detached-HEAD work (March 23–April 11, 2026).
Companion to `specs/not_reviewed/start-run-inspect/` (the design notes).

## Files

### Notebook walkthroughs (latest activity, April 11)

| File | Purpose |
|---|---|
| `inspect-and-start-run.html` | Rendered notebook: blocking `run(inspect=True)`, background `start_run()` with live view, cooperative `stop()`, sync + async parity. Open in a browser to see the embedded widget. |
| `inspect-and-start-run.ipynb` | Source notebook (outputs stripped by `nbstripout` on commit). Run with `uv run jupyter lab` to regenerate. |
| `start-map-failure-drilldown.html` | Rendered notebook: sync/async `start_map()`, per-item `FailureCase`, `failed_item.inspect()` drill-down. |
| `start-map-failure-drilldown.ipynb` | Source notebook. |

### Widget design previews (March 23–24)

| File | Purpose |
|---|---|
| `inspect_widget_failure_timeline_v4.html` | Latest of the rich-payload preview series. Embeds a multi-scenario showcase (subgraphs, failure, retry, double_doubled). The "waterfall in different situations" reference. |
| `inspect_widget_hex_redesign.html` | Final UI design comparison ("HEX-Style Redesign Comparison"). The most recent widget-preview iteration. |

## Where they came from

Pulled from the `output/` subtree of the detached-HEAD Codex worktree at
`~/.codex/worktrees/ad0d/hypergraph/output/`. Earlier iterations of the
failure-timeline widget (v1, v2, v3) and the theme-only drafts were not
preserved — v4 supersedes them and the polish drafts threw away the
multi-scenario payload to focus on visual style.
