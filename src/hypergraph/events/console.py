"""The console — a live, bounded view over one run or map, from its own events.

``ConsoleProcessor`` folds one run's event stream into a small payload;
``render_console`` turns a payload into one self-contained HTML frame; and
``LiveConsole`` is the notebook glue that renders the frame into ONE display
handle, updated in place while the run executes and settled at shutdown. In a
notebook, ``show_progress=True`` selects ``LiveConsole`` automatically — the
terminal keeps its progress bars, and an explicit
``RichProgressProcessor(force_mode=...)`` still selects bars anywhere.

Everything rendered is derived from the events of the run actually
underneath — span parent/child linkage, node names, map fan-outs, measured
durations, attempt and cache facts. Swap in a completely different graph —
other names, other depth, more nested maps — and the console renders it
correctly with no code change.

Host hooks (all optional, all defaulting to generic vocabulary):

- ``item_labels``: unit names per fan-out level (level 0 = the run/map's own
  items, level 1 = the first nested map's items, ...). The default speaks
  hypergraph's own word, "items" / "child items".
- ``capacity``: a callable returning a list of plain dicts
  ``{name, busy, cap, waiting, paused_seconds}`` describing shared work lanes.
  The Lanes section renders only when this hook is provided; hypergraph never
  imports a limiter library.
- ``title`` / ``subtitle``: header text; default to the root graph's name.

Durable work: attached via ``Host.serve(event_processors=[LiveConsole()])``
the console renders each durable Run THIS PROCESS'S worker executes, per-item
live — a new root run resets the fold, so the frame always shows the Run
currently underneath. A notebook that only SUBMITTED (``Host.submit`` /
``Host.submit_batch``) has no events to fold, because the executing process
may be another one: that is the read-model watch in
``hypergraph.host.watch`` (``watch_submissions``), which renders the same
design from Run Home truth.

Interaction is CSS-only (radio-selected detail pane, checkbox-collapsed
subtrees), so the settled frame stays fully inspectable in a saved notebook
without a kernel and without any JavaScript.
"""

from __future__ import annotations

import hashlib
import html as _html
import statistics
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from hypergraph._repr import theme_wrap, widget_state_key
from hypergraph.events.processor import TypedEventProcessor
from hypergraph.events.types import (
    InnerCacheEvent,
    NodeAttemptEndEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
)

_DURATION_CAP = 4096  # bounded per-step samples; totals accumulate separately


# ---------------------------------------------------------------------------
# folding state
# ---------------------------------------------------------------------------


@dataclass
class _RunRec:
    span: str
    kind: str  # "root" | "item" | "map_parent" | "child_item" | "nested"
    step_prefix: tuple[str, ...]
    level: int
    item_label: str = ""
    saw_node: bool = False
    ended: bool = False


@dataclass
class _NodeRec:
    path: tuple[str, ...]
    started_at: float
    level: int
    item_label: str


@dataclass
class _Step:
    path: tuple[str, ...]
    name: str
    level: int  # fan-out depth: counts on this row are level-N items
    children: list[tuple[str, ...]] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    total_ms: float = 0.0
    count: int = 0
    running: int = 0
    active: dict[str, tuple[float, str]] = field(default_factory=dict)
    retries: int = 0
    errors: int = 0
    inner_hits: int = 0
    node_cached: int = 0
    is_map: bool = False
    fan_sizes: list[int] = field(default_factory=list)
    child_expected: int = 0
    child_started: int = 0
    child_done: int = 0
    child_failed: int = 0
    #: Has anything entered this step yet? A row seeded from the run's plan
    #: stays False until its first ``on_node_start``.
    started: bool = False
    #: False when a gate or route controls this node: it MAY never run, so
    #: the row reads "possible" instead of promising it.
    certain: bool = True

    def add_duration(self, ms: float) -> None:
        self.total_ms += ms
        self.count += 1
        if len(self.durations) < _DURATION_CAP:
            self.durations.append(ms)

    @property
    def state(self) -> str:
        """What this row IS right now — the one word the styling keys off."""
        if self.running:
            return "running"
        if self.started:
            return "done"
        return "upcoming" if self.certain else "possible"


class ConsoleProcessor(TypedEventProcessor):
    """Folds one run's event stream into a bounded console payload.

    Attach to any ``runner.run(...)`` or ``runner.map(...)`` via
    ``event_processors=[...]``. The payload is a plain dict; ``render_console``
    turns it into the console HTML. A new root run resets the fold (the same
    rule the progress tracker applies), so one instance may be reused across
    runs — including the durable Runs a ``Host`` worker executes.
    """

    def __init__(
        self,
        *,
        item_labels: Sequence[str] | None = None,
        capacity: Callable[[], list[dict[str, Any]]] | None = None,
        title: str | None = None,
        subtitle: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._labels = tuple(item_labels) if item_labels else ()
        self._capacity = capacity
        self._title = title
        self._subtitle = subtitle
        self._clock = clock
        self.uid = "hgc" + uuid.uuid4().hex[:8]
        self._reset_fold()

    def _reset_fold(self) -> None:
        self._runs: dict[str, _RunRec] = {}
        self._nodes: dict[str, _NodeRec] = {}
        self._steps: dict[tuple[str, ...], _Step] = {}
        self._top_order: list[tuple[str, ...]] = []
        self.graph_name = ""
        self.mode = "map"  # "map" | "run"
        self.total_items = 0
        self.started_items = 0
        self.done_items = 0
        self.failed_items = 0
        self.item_durations: list[float] = []
        self.failures: list[dict[str, str]] = []
        self.started_at: float | None = None
        self.wall: float | None = None
        self.terminal = False
        self.status = ""
        self.last_event_at: float | None = None

    # -- vocabulary ---------------------------------------------------------

    def unit(self, level: int) -> str:
        if level < len(self._labels):
            return self._labels[level]
        if level == 0:
            return "items"
        if level == 1:
            return "child items"
        return f"level-{level} items"

    def unit_one(self, level: int) -> str:
        word = self.unit(level)
        return word[:-1] if word.endswith("s") else word

    # -- event folding ------------------------------------------------------

    def on_run_start(self, e: RunStartEvent) -> None:
        self._touch(e.timestamp)
        parent = e.parent_span_id or ""
        if not parent:
            if self._runs and e.span_id not in self._runs:
                # A NEW root run through a reused processor: fold it fresh,
                # exactly as the progress tracker resets for a new root.
                self._reset_fold()
            self.graph_name = e.graph_name or "run"
            self.started_at = self._clock()
            if e.is_map:
                self.mode = "map"
                self.total_items = int(e.map_size or 0)
                self._runs[e.span_id] = _RunRec(e.span_id, "root", (), 0)
            else:
                self.mode = "run"
                self.total_items = 1
                self.started_items = 1
                self._runs[e.span_id] = _RunRec(e.span_id, "root", (), 0, item_label=f"{self.unit_one(0)} #0")
            self._seed_plan(e, (), 0)
            return
        node = self._nodes.get(parent)
        if node is not None:
            step = self._step(node.path, node.level)
            if e.is_map:
                step.is_map = True
                size = int(e.map_size or 0)
                step.fan_sizes.append(size)
                step.child_expected += size
                self._runs[e.span_id] = _RunRec(
                    e.span_id,
                    "map_parent",
                    node.path,
                    node.level + 1,
                    item_label=node.item_label,
                )
                self._seed_plan(e, node.path, node.level + 1)
            else:
                self._runs[e.span_id] = _RunRec(
                    e.span_id,
                    "nested",
                    node.path,
                    node.level,
                    item_label=node.item_label,
                )
                self._seed_plan(e, node.path, node.level)
            return
        run = self._runs.get(parent)
        if run is None:
            return
        if run.kind == "root" and self.mode == "map":
            self.started_items += 1
            label = f"{self.unit_one(0)} #{e.item_index}"
            self._runs[e.span_id] = _RunRec(e.span_id, "item", (), 0, item_label=label)
        elif run.kind == "map_parent":
            step = self._step(run.step_prefix, run.level - 1)
            step.child_started += 1
            label = f"{run.item_label} · {self.unit_one(run.level)} #{e.item_index}"
            self._runs[e.span_id] = _RunRec(e.span_id, "child_item", run.step_prefix, run.level, item_label=label)

    def _seed_plan(self, e: RunStartEvent, prefix: tuple[str, ...], level: int) -> None:
        """Draw the run's whole graph before any of it has started.

        The topology is settled at construction, so the rows exist from the
        first frame instead of appearing one at a time as work reaches them
        — and in the graph's own execution order rather than arrival order.
        A node a gate controls is seeded as ``possible``, never as pending:
        it may never run, and the row must not promise otherwise.
        """
        for planned in getattr(e, "plan", ()) or ():
            step = self._step(prefix + (planned.name,), level)
            step.certain = step.certain and planned.certain
            if planned.fans_out:
                step.is_map = True

    def on_node_start(self, e: NodeStartEvent) -> None:
        self._touch(e.timestamp)
        run = self._runs.get(e.parent_span_id or "")
        if run is None:
            return
        run.saw_node = True
        path = run.step_prefix + (e.node_name,)
        step = self._step(path, run.level)
        step.started = True
        step.running += 1
        rec = _NodeRec(path, self._clock(), run.level, run.item_label)
        self._nodes[e.span_id] = rec
        step.active[e.span_id] = (rec.started_at, rec.item_label)

    def on_node_end(self, e: NodeEndEvent) -> None:
        self._touch(e.timestamp)
        rec = self._nodes.pop(e.span_id, None)
        if rec is None:
            return
        step = self._steps[rec.path]
        step.running = max(0, step.running - 1)
        step.active.pop(e.span_id, None)
        step.add_duration(e.duration_ms)
        if e.cached:
            step.node_cached += 1

    def on_node_error(self, e: NodeErrorEvent) -> None:
        self._touch(e.timestamp)
        rec = self._nodes.pop(e.span_id, None)
        if rec is None:
            return
        step = self._steps[rec.path]
        step.running = max(0, step.running - 1)
        step.active.pop(e.span_id, None)
        step.errors += 1
        message = e.error
        if e.error_detail is not None and getattr(e.error_detail, "message", ""):
            message = f"{e.error_type.rsplit('.', 1)[-1]}: {e.error_detail.message}"
        if len(self.failures) < 50:
            self.failures.append({"item": rec.item_label, "step": rec.path[-1], "message": message})

    def on_node_attempt_end(self, e: NodeAttemptEndEvent) -> None:
        self._touch(e.timestamp)
        rec = self._nodes.get(e.parent_span_id or "")
        if rec is not None and e.retry_scheduled:
            self._steps[rec.path].retries += 1

    def on_inner_cache(self, e: InnerCacheEvent) -> None:
        self._touch(e.timestamp)
        rec = self._nodes.get(e.parent_span_id or "")
        if rec is not None and e.hit:
            self._steps[rec.path].inner_hits += 1

    def on_run_end(self, e: RunEndEvent) -> None:
        self._touch(e.timestamp)
        run = self._runs.get(e.span_id)
        if run is None:
            return
        run.ended = True
        failed = str(getattr(e.status, "value", e.status)) == "failed"
        if run.kind == "root":
            self.terminal = True
            self.status = str(getattr(e.status, "value", e.status))
            self.wall = (self._clock() - self.started_at) if self.started_at else 0.0
            if self.mode == "run":
                if failed:
                    self.failed_items = 1
                else:
                    self.done_items = 1
                self.item_durations.append(e.duration_ms)
        elif run.kind == "item":
            if failed:
                self.failed_items += 1
            else:
                self.done_items += 1
            self.item_durations.append(e.duration_ms)
        elif run.kind == "child_item":
            step = self._steps.get(run.step_prefix)
            if step is not None:
                if failed:
                    step.child_failed += 1
                else:
                    step.child_done += 1

    # -- internals ----------------------------------------------------------

    def _touch(self, ts: float) -> None:
        self.last_event_at = self._clock()

    def _step(self, path: tuple[str, ...], level: int) -> _Step:
        step = self._steps.get(path)
        if step is None:
            step = _Step(path=path, name=path[-1], level=level)
            self._steps[path] = step
            parent = path[:-1]
            if parent:
                parent_step = self._steps.get(parent)
                if parent_step is not None and path not in parent_step.children:
                    parent_step.children.append(path)
                elif parent_step is None:
                    # parent not seen as a node (should not happen); float to top
                    self._top_order.append(path)
            else:
                self._top_order.append(path)
        return step

    # -- payload ------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        now = self._clock()
        elapsed = self.wall if self.wall is not None else (now - self.started_at if self.started_at else 0.0)
        leaf_total = sum(s.total_ms for s in self._steps.values() if not s.children)

        def share_of(path: tuple[str, ...]) -> float:
            step = self._steps[path]
            if step.children:
                return sum(share_of(c) for c in step.children)
            return 100.0 * step.total_ms / leaf_total if leaf_total > 0 else 0.0

        def row_of(path: tuple[str, ...], depth: int, start: float) -> dict[str, Any]:
            step = self._steps[path]
            share = share_of(path)
            unit = self.unit(step.level)
            med = statistics.median(step.durations) / 1000 if step.durations else None
            p95 = _percentile(step.durations, 95)
            mx = max(step.durations) / 1000 if step.durations else None
            flag = med is not None and p95 is not None and med >= 0.2 and len(step.durations) >= 8 and p95 >= 3 * med and not step.children
            slowest = None
            if step.active:
                started, label = min(step.active.values(), key=lambda t: t[0])
                slowest = {"label": label, "seconds": now - started}
            row = {
                "id": "n" + hashlib.sha1("/".join(path).encode()).hexdigest()[:6],
                "name": step.name,
                "depth": depth,
                "state": step.state,
                "unit": unit,
                "unit_one": self.unit_one(step.level),
                "share": share,
                "start": start,
                "median_s": None if step.children else med,
                "p95_s": p95,
                "max_s": mx,
                "count": step.count,
                "running": step.running,
                "retries": step.retries,
                "errors": step.errors,
                "cached_units": step.node_cached,
                "cached_calls": step.inner_hits,
                "flag": flag,
                "slowest": slowest,
                "is_map": step.is_map,
                "fan": (round(statistics.mean(step.fan_sizes)) if step.fan_sizes else None),
                "fan_unit": self.unit(step.level + 1) if step.is_map else None,
                "fan_unit_one": self.unit_one(step.level + 1) if step.is_map else None,
                "parent_unit_one": self.unit_one(step.level),
                "child_expected": step.child_expected,
                "child_started": step.child_started,
                "child_done": step.child_done,
                "child_failed": step.child_failed,
                "child_running": max(0, step.child_started - step.child_done - step.child_failed),
                "children": [],
            }
            cursor = start
            for child in step.children:
                child_row = row_of(child, depth + 1, cursor)
                row["children"].append(child_row)
                cursor += child_row["share"]
            return row

        root_med = statistics.median(self.item_durations) / 1000 if self.item_durations else None
        active_items = sum(1 for r in self._runs.values() if r.kind in ("item", "root") and r.saw_node and not r.ended)
        queued = max(0, self.total_items - self.done_items - self.failed_items - active_items)
        running_shown = max(0, self.total_items - self.done_items - self.failed_items - queued)

        children = []
        cursor = 0.0
        for path in self._top_order:
            child_row = row_of(path, 1, cursor)
            children.append(child_row)
            cursor += child_row["share"]
        cache_hits_total = sum(s.inner_hits + s.node_cached for s in self._steps.values())
        retries_total = sum(s.retries for s in self._steps.values())

        rate = self.done_items / elapsed if elapsed > 0 and self.done_items else None
        remaining = (self.total_items - self.done_items - self.failed_items) / rate if rate else None

        capacity: list[dict[str, Any]] = []
        if self._capacity is not None:
            try:
                capacity = [dict(entry) for entry in self._capacity()]
            except Exception:
                capacity = []

        return {
            "uid": self.uid,
            "mode": self.mode,
            "title": self._title or self.graph_name,
            "subtitle": self._subtitle or f"{_n_unit(self.total_items, self.unit(0))} · hypergraph " + ("map" if self.mode == "map" else "run"),
            "unit": self.unit(0),
            "unit_one": self.unit_one(0),
            "terminal": self.terminal,
            "status": self.status,
            "elapsed_s": elapsed,
            "remaining_s": None if self.terminal else remaining,
            "median_item_s": root_med,
            "total": self.total_items,
            "done": self.done_items,
            "failed": self.failed_items,
            "running": running_shown,
            "queued": queued,
            "tree": {
                "id": "root",
                "name": self.graph_name,
                "state": "done" if self.terminal else "running",
                "unit": self.unit(0),
                "unit_one": self.unit_one(0),
                "share": 100.0,
                "start": 0.0,
                "median_s": root_med,
                "children": children,
            },
            "capacity": capacity,
            "failures": self.failures,
            "cache_hits": cache_hits_total,
            "retries": retries_total,
            "updated_ago_s": (now - self.last_event_at) if self.last_event_at else 0.0,
        }


def _percentile(samples_ms: list[float], q: float) -> float | None:
    if not samples_ms:
        return None
    ordered = sorted(samples_ms)
    idx = min(len(ordered) - 1, int(round((q / 100.0) * (len(ordered) - 1))))
    return ordered[idx] / 1000


# ---------------------------------------------------------------------------
# rendering — CSS and markup from the approved console design
# ---------------------------------------------------------------------------

_CSS = """
#$UID{--surface:light-dark(#ffffff,#161a22);--surface-2:light-dark(#f8f9fb,#1b202a);
  --rule:light-dark(#e5e8ee,#2b313d);--rule-soft:light-dark(#eef0f4,#232936);
  --ink:light-dark(#151a23,#e8ebf2);--ink-2:light-dark(#3f4757,#b6bdcc);
  --ink-3:light-dark(#6a7288,#8a93a7);--accent:light-dark(#4f46e5,#a9a4f7);
  --accent-tint:light-dark(#f3f4fe,#272444);--accent-line:light-dark(#e0e1f8,#3b3766);
  --bar:light-dark(#8790aa,#7b8399);
  --bar-child:light-dark(#b4bacb,#5c6375);--bar-strong:light-dark(#6d7694,#98a1ba);
  --bar-sel:light-dark(#5b647f,#c2c9de);--bar-track:light-dark(#ecEEF3,#262c37);
  --ok:light-dark(#177a58,#3fbf90);--warn:light-dark(#8a5a08,#e0a53a);
  --warn-tint:light-dark(#fcf7ee,#332a17);--warn-line:light-dark(#f0e3cc,#4a3c1e);
  --warn-ink:light-dark(#6b5527,#e6cf9e);--warn-code:light-dark(#f2e6d0,#453721);
  --bad:light-dark(#b3283f,#ff8095);--bad-tint:light-dark(#fcf0f2,#3a1e24);
  --chip:light-dark(#f1f3f7,#232935);--chip-2:light-dark(#eef0f5,#252b38);
  --hover:light-dark(#e9ebf1,#2a3140);--dotted:light-dark(#c4cad6,#4a5262);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif;
  color:var(--ink);-webkit-font-smoothing:antialiased;text-align:left}
#$UID *,#$UID *::before,#$UID *::after{box-sizing:border-box}
#$UID h1,#$UID h2,#$UID h3,#$UID p,#$UID dl,#$UID dd,#$UID dt{margin:0;padding:0}
#$UID .n{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID code{font-family:var(--mono)}
#$UID>input{position:absolute;opacity:0;pointer-events:none}
#$UID .eyebrow{color:var(--ink-3);font-size:10.5px;font-weight:650;letter-spacing:.1em;text-transform:uppercase}
#$UID .bc{container-type:inline-size;container-name:bc;background:var(--surface);border:1px solid var(--rule);
  border-radius:10px;overflow:hidden;max-width:1280px}
#$UID .bc-head{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:16px;padding:22px 24px 18px}
#$UID .bc-title{display:block;margin-top:7px;font-size:19px;font-weight:650;letter-spacing:-.02em;line-height:1.2}
#$UID .bc-sub{display:block;margin-top:6px;color:var(--ink-3);font-size:12.5px}
#$UID .bc-agg{color:var(--ink-2)}
#$UID .bc-agg b{font-weight:650;color:var(--ink);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID .bc-status{display:flex;align-items:center;gap:12px;padding-top:5px}
#$UID .bc-live{display:inline-flex;align-items:center;gap:8px;color:var(--ink-3);font-size:12.5px;white-space:nowrap}
#$UID .bc-live b{color:var(--ink);font-weight:600}
#$UID .dot{width:6px;height:6px;border-radius:999px;background:var(--ink-3);flex:none}
#$UID .dot[data-tone=live]{background:var(--ok);animation:pulse-$UID 2.6s ease-out infinite}
#$UID .dot[data-tone=saved]{background:var(--ink-3)}
@keyframes pulse-$UID{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 36%,transparent)}70%{box-shadow:0 0 0 6px transparent}100%{box-shadow:0 0 0 0 transparent}}
#$UID .bc-chip{display:inline-flex;align-items:center;white-space:nowrap;padding:4px 9px;border-radius:5px;
  font-size:10.5px;font-weight:650;letter-spacing:.08em;text-transform:uppercase}
#$UID .bc-chip[data-tone=running]{background:var(--accent-tint);color:var(--accent)}
#$UID .bc-chip[data-tone=settled]{background:var(--surface-2);color:var(--ink-2);border:1px solid var(--rule)}
#$UID .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:0 24px;
  border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
#$UID .stat{min-width:0;padding:17px 22px 16px}
#$UID .stat + .stat{border-left:1px solid var(--rule-soft)}
#$UID .stat:first-child{padding-left:0}
#$UID .stat-v{display:flex;align-items:baseline;gap:6px;font-size:27px;font-weight:600;line-height:1.05;
  letter-spacing:-.03em;font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID .stat-v small{font-size:14px;font-weight:500;letter-spacing:-.01em;color:var(--ink-3)}
#$UID .stat[data-tone=bad] .stat-v{color:var(--bad)}
#$UID .stat[data-tone=warn] .stat-v{color:var(--warn)}
#$UID .stat-l{margin-top:9px;color:var(--ink-3);font-size:10.5px;font-weight:600;letter-spacing:.09em;
  text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#$UID .prog{padding:20px 24px 22px}
#$UID .segs{display:flex;gap:2px;height:7px}
#$UID .seg{min-width:4px;border-radius:2px}
#$UID .seg[data-k=done]{background:var(--ok)}
#$UID .seg[data-k=running]{background:var(--accent)}
#$UID .seg[data-k=failed]{background:var(--bad)}
#$UID .seg[data-k=paused]{background:var(--warn)}
#$UID .seg[data-k=queued]{background:var(--bar-track)}
#$UID .legend{display:flex;flex-wrap:wrap;gap:7px 22px;margin-top:13px}
#$UID .legend span{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;white-space:nowrap;color:var(--ink-2)}
#$UID .legend b{font-weight:650;font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1;color:var(--ink)}
#$UID .legend i{font-style:normal;color:var(--ink-3)}
#$UID .key{width:8px;height:8px;border-radius:2px;flex:none}
#$UID .notice{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0 24px 22px;padding:11px 14px;
  border-radius:6px;background:var(--warn-tint);border:1px solid var(--warn-line)}
#$UID .notice b{color:var(--warn);font-size:12.5px;font-weight:650;white-space:nowrap}
#$UID .notice-text{color:var(--warn-ink);font-size:12.5px;min-width:0}
#$UID .bc-body{display:grid;grid-template-columns:minmax(0,1fr) minmax(232px,264px);border-top:1px solid var(--rule)}
#$UID .bc-main{min-width:0;display:flex;flex-direction:column;padding:20px 20px 24px;border-right:1px solid var(--rule)}
#$UID .bc-side{min-width:0;padding:20px 22px 24px;background:var(--surface-2)}
#$UID .sec{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px}
#$UID .sec h3{font-size:13px;font-weight:650;letter-spacing:-.005em}
#$UID .sec .hint{color:var(--ink-3);font-size:11.5px;white-space:nowrap}
#$UID .tree{--nm:218px;--un:70px;--sig:242px;--gap:11px;
  --cols:var(--nm) var(--un) minmax(56px,1fr) 40px 54px var(--sig)}
#$UID .colhead,#$UID .row{display:grid;grid-template-columns:var(--cols);gap:var(--gap);align-items:center}
#$UID .colhead{padding:0 10px 8px;border-bottom:1px solid var(--rule-soft);color:var(--ink-3);
  font-size:10px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;align-items:end}
#$UID .colhead .r{text-align:right}
#$UID .row{position:relative;width:100%;min-height:40px;padding:7px 10px;border-radius:5px;text-align:left;cursor:pointer;
  border:1px solid transparent;transition:background .1s ease;margin:0}
#$UID .r-lab{display:flex;align-items:center;gap:9px;min-width:0;cursor:pointer}
#$UID .row + .row,#$UID .node + .node > .row{margin-top:1px}
#$UID .row:hover{background:var(--surface-2)}
#$UID .r-name{display:flex;align-items:center;gap:9px;min-width:0}
#$UID .caret{flex:none;width:18px;height:18px;display:grid;place-items:center;padding:0;line-height:0;
  border:0;border-radius:4px;background:transparent;color:var(--ink-3);cursor:pointer}
#$UID .caret:hover{background:var(--hover);color:var(--ink)}
#$UID .caret svg{transition:transform .15s ease}
#$UID .caret-gap{flex:none;width:18px}
#$UID .r-label{font-size:13.5px;font-weight:500;letter-spacing:-.005em;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;min-width:0}
#$UID .r-fan{flex:none;padding:2px 6px;border-radius:4px;background:var(--chip-2);color:var(--ink-2);
  font-size:11px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
#$UID .r-unit{color:var(--ink-3);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#$UID .r-track{position:relative;height:9px;border-radius:2px;background:var(--bar-track);overflow:hidden}
#$UID .r-bar{position:absolute;top:0;bottom:0;min-width:3px;border-radius:2px;background:var(--bar)}
#$UID .row[data-depth="0"] .r-bar{background:var(--bar-strong)}
#$UID .row[data-depth="2"] .r-bar{background:var(--bar-child)}
#$UID .r-pct,#$UID .r-med{font-size:12.5px;text-align:right;white-space:nowrap;
  font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID .r-pct{font-weight:600;color:var(--ink)}
#$UID .r-med{color:var(--ink-3)}
#$UID .r-sig{display:flex;gap:6px;justify-content:flex-end;align-items:center;flex-wrap:wrap;min-width:0}
#$UID .badge{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;flex:none;
  padding:3px 8px;border-radius:4px;font-size:11.5px;font-weight:500;line-height:1.3;
  background:var(--chip);color:var(--ink-2)}
#$UID .badge b{font-weight:650;font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID .badge[data-tone=cache]{background:var(--accent-tint);color:var(--accent)}
#$UID .badge[data-tone=error]{background:var(--bad-tint);color:var(--bad)}
#$UID .badge[data-tone=flag]{background:var(--warn-tint);color:var(--warn);box-shadow:inset 0 0 0 1px var(--warn-line)}
#$UID .badge[data-tone=gate]{background:var(--warn-tint);color:var(--warn)}
#$UID .badge[data-tone=queued]{background:transparent;color:var(--ink-3);box-shadow:inset 0 0 0 1px var(--rule)}
#$UID .badge[data-tone=possible]{background:transparent;color:var(--ink-3);
  box-shadow:inset 0 0 0 1px var(--rule);font-style:italic}
#$UID .row[data-state=upcoming] .r-label,#$UID .row[data-state=possible] .r-label{color:var(--ink-3);font-weight:400}
#$UID .row[data-state=upcoming] .r-unit,#$UID .row[data-state=possible] .r-unit{opacity:.7}
#$UID .row[data-state=upcoming] .r-track{background:transparent;box-shadow:inset 0 0 0 1px var(--rule-soft)}
#$UID .row[data-state=possible] .r-track{background:transparent;
  background-image:repeating-linear-gradient(90deg,var(--rule) 0 3px,transparent 3px 7px);
  background-size:100% 1px;background-position:0 50%;background-repeat:no-repeat}
#$UID .row[data-state=upcoming] .r-pct,#$UID .row[data-state=possible] .r-pct{color:var(--ink-3);font-weight:400}
#$UID .row[data-state=running] .r-label{font-weight:600}
#$UID .legend-marks{margin-top:auto;padding-top:20px;border-top:1px solid var(--rule-soft);
  max-width:80ch;color:var(--ink-3);font-size:11.5px;line-height:1.65}
#$UID .legend-marks b{color:var(--ink-2);font-weight:600}
#$UID .foot-note{border-bottom:1px dotted var(--dotted);cursor:help}
#$UID .lanes{margin-top:28px;padding-top:20px;border-top:1px solid var(--rule-soft);display:grid;gap:12px}
#$UID .lane{display:grid;grid-template-columns:84px minmax(56px,1fr) 126px 112px 94px;gap:14px;
  align-items:center;max-width:680px}
#$UID .lane-n{font-size:13px;font-weight:500}
#$UID .lane-t{position:relative;height:6px;border-radius:3px;background:var(--bar-track);overflow:hidden}
#$UID .lane-b{position:absolute;inset:0 auto 0 0;border-radius:3px;background:var(--bar)}
#$UID .lane-m{color:var(--ink-3);font-size:12px;white-space:nowrap}
#$UID .lane-m b{color:var(--ink);font-weight:650;font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID .lane-p{padding:2px 8px;border-radius:4px;background:var(--warn-tint);color:var(--warn);font-size:11px;font-weight:600}
#$UID .d-name{margin:10px 0 10px;font-size:16px;font-weight:600;letter-spacing:-.015em;overflow-wrap:anywhere}
#$UID .d-pills{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
#$UID .pill{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;padding:3px 8px;border-radius:4px;
  background:var(--surface);border:1px solid var(--rule);color:var(--ink-2);font-size:11.5px;font-weight:500}
#$UID .pill b{font-weight:650;font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1;color:var(--ink)}
#$UID .pill[data-tone=accent]{background:var(--accent-tint);border-color:var(--accent-line);color:var(--accent)}
#$UID .pill[data-tone=accent] b{color:var(--accent)}
#$UID .pill[data-tone=flag]{background:var(--warn-tint);border-color:var(--warn-line);color:var(--warn)}
#$UID .facts{margin-top:8px}
#$UID .fact{padding:11px 0;border-top:1px solid var(--rule)}
#$UID .fact dt{color:var(--ink-3);font-size:10px;font-weight:650;letter-spacing:.09em;text-transform:uppercase}
#$UID .fact dd{margin-top:5px;font-size:13px;line-height:1.45;color:var(--ink);overflow-wrap:anywhere}
#$UID .warnv{color:var(--warn);font-weight:600}
#$UID .callout{margin-top:16px;padding:12px 13px;border-radius:6px;background:var(--warn-tint);border:1px solid var(--warn-line)}
#$UID .callout .t{display:flex;align-items:center;gap:7px;margin-bottom:6px;color:var(--warn);
  font-size:10px;font-weight:650;letter-spacing:.09em;text-transform:uppercase}
#$UID .callout .t i{width:6px;height:6px;border-radius:999px;background:var(--warn);flex:none}
#$UID .callout p{font-size:12.5px;line-height:1.55;color:var(--warn-ink)}
#$UID .callout code{padding:1px 5px;border-radius:4px;background:var(--warn-code);font-size:11.5px}
#$UID [data-detail]{display:none}
#$UID .fails{padding:20px 24px 22px;border-top:1px solid var(--rule)}
#$UID .fail{display:grid;grid-template-columns:10px minmax(70px,auto) minmax(64px,auto) minmax(0,1fr);
  gap:14px;align-items:center;padding:11px 0;border-top:1px solid var(--rule-soft)}
#$UID .fail-dot{width:6px;height:6px;border-radius:999px;background:var(--bad);justify-self:center}
#$UID .fail-dot[data-tone=gate]{background:var(--warn)}
#$UID .fail-dot[data-tone=info]{background:var(--ink-3)}
#$UID .fail-doc{font-family:var(--mono);font-size:12.5px;font-weight:500;white-space:nowrap}
#$UID .fail-step{justify-self:start;color:var(--ink-3);font-size:12px;white-space:nowrap}
#$UID .fail-msg{font-size:12.5px;color:var(--ink-2);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#$UID .count{color:var(--bad)}
#$UID .flight{border-collapse:collapse;width:100%}
#$UID .flight th{text-align:left;padding:0 10px 8px;border-bottom:1px solid var(--rule-soft);color:var(--ink-3);
  font-size:10px;font-weight:650;letter-spacing:.09em;text-transform:uppercase}
#$UID .flight td{padding:9px 10px;border-bottom:1px solid var(--rule-soft);font-size:12.5px}
#$UID .flight td.n{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
#$UID .flight .k{font-family:var(--mono);font-weight:500}
#$UID .bc-foot{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;
  padding:12px 24px;border-top:1px solid var(--rule);background:var(--surface-2);color:var(--ink-3);font-size:11.5px}
@container bc (max-width:1000px){
  #$UID .tree{--sig:224px}
  #$UID .flag-long{display:none}
  #$UID .bc-body{grid-template-columns:minmax(0,1fr)}
  #$UID .bc-main{border-right:0;border-bottom:1px solid var(--rule)}
  #$UID .bc-side{padding:20px 24px 24px}
  #$UID .facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:0 26px}
  #$UID .d-pills{margin-bottom:2px}
}
@container bc (max-width:760px){
  #$UID .tree{--nm:150px;--un:62px;--sig:204px;--gap:8px}
  #$UID .r-unit{font-size:11px}
  #$UID .flag-long{display:none}
}
@container bc (max-width:640px){
  #$UID .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  #$UID .stat:nth-child(3){padding-left:0;border-left:0}
  #$UID .stat:nth-child(n+3){border-top:1px solid var(--rule-soft)}
}
@media (prefers-reduced-motion:reduce){#$UID *{animation:none!important;transition:none!important}}
"""

_CARET = (
    '<svg width="9" height="9" viewBox="0 0 10 10" aria-hidden="true">'
    '<path d="M1 3.3l4 3.8 4-3.8" fill="none" stroke="currentColor" stroke-width="1.7"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _esc(text: Any) -> str:
    return _html.escape(str(text), quote=True)


def _n_unit(n: int, plural: str) -> str:
    """``4 items`` / ``1 item`` — never ``1 items``."""
    one = plural[:-1] if plural.endswith("s") else plural
    return f"{n:,} {one if n == 1 else plural}"


def _fmt_s(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 120:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    rest = int(seconds % 60)
    return f"{minutes}m {rest:02d}s" if rest else f"{minutes}m"


def _fmt_pct(share: float) -> str:
    if share <= 0:
        return "0%"
    return f"{max(1, round(share))}%"


def _walk(row: dict[str, Any]) -> list[dict[str, Any]]:
    out = [row]
    for child in row["children"]:
        out.extend(_walk(child))
    return out


def _badges(row: dict[str, Any]) -> str:
    parts = []
    unit, one = row["unit"], row["unit_one"]
    if row.get("cached_units"):
        n = row["cached_units"]
        parts.append(
            f'<span class="badge" data-tone="cache" title="{n} of {row["count"]} {unit} were served from cache."><b>⚡ {n:,}</b> {unit} cached</span>'
        )
    if row.get("cached_calls"):
        n = row["cached_calls"]
        parts.append(
            f'<span class="badge" data-tone="cache" title="{n} cached calls '
            f'inside {_esc(row["name"])} were served from the cache — no work was repeated.">'
            f"<b>⚡ {n:,}</b> cached</span>"
        )
    if row.get("retries"):
        n = row["retries"]
        parts.append(
            f'<span class="badge" title="{n} {one} attempts were retried and then succeeded — the run absorbed them."><b>{n:,}</b> retried</span>'
        )
    if row.get("errors"):
        n = row["errors"]
        parts.append(
            f'<span class="badge" data-tone="error" title="{n} {unit if n > 1 else one} '
            f'failed for good; the affected {unit} are listed under Failures.">'
            f"<b>{n:,}</b> failed</span>"
        )
    if row.get("flag"):
        parts.append(
            '<span class="badge" data-tone="flag" title="p95 is 3× the median — '
            'one slow straggler, not the whole run.">p95 3×'
            '<span class="flag-long"> median</span></span>'
        )
    return "".join(parts)


def _to_come(row: dict[str, Any]) -> str:
    """The row for a step nothing has entered yet.

    Drawn from the run's PLAN, so the shape of the work is visible from the
    first frame. It carries no numbers, because it has earned none — an
    empty track, a dash for the median, and one word for what it is.
    """
    possible = row.get("state") == "possible"
    word = "may run" if possible else "queued"
    hint = (
        f"{row['name']} sits behind a route or gate — it may never run, so nothing here promises it will."
        if possible
        else f"{row['name']} has not started yet. It runs once per {row['unit_one']}."
    )
    return f'<span class="badge" data-tone="{"possible" if possible else "queued"}" title="{_esc(hint)}">{word}</span>'


def _row_html(uid: str, row: dict[str, Any], depth: int) -> str:
    kids = bool(row["children"])
    rid = row["id"]
    state = row.get("state", "done")
    if state in ("upcoming", "possible"):
        sel = f"{uid}-s-{rid}"
        caret = '<span class="caret-gap"></span>'
        return f"""
  <div class="row" role="treeitem" aria-level="{depth + 1}"
       data-row="{rid}" data-depth="{depth}" data-state="{state}">
    <span class="r-name" style="padding-left:{depth * 24}px">
      {caret}<label class="r-lab" for="{sel}"><span class="r-label" title="{_esc(row["name"])}">{_esc(row["name"])}</span></label>
    </span>
    <label class="r-unit n" for="{sel}" title="{_esc(row["unit"])}">{row["unit"]}</label>
    <label class="r-track" for="{sel}" title="Nothing measured yet"></label>
    <label class="r-pct n" for="{sel}">—</label>
    <label class="r-med n" for="{sel}">—</label>
    <label class="r-sig" for="{sel}">{_to_come(row)}</label>
  </div>"""
    caret = (
        f'<label class="caret" for="{uid}-c-{rid}" title="Collapse or expand {_esc(row["name"])}"'
        f' aria-label="Collapse or expand {_esc(row["name"])}">{_CARET}</label>'
        if kids
        else '<span class="caret-gap"></span>'
    )
    share_title = f"{_esc(row['name'])} takes {_fmt_pct(row['share'])} of the run's measured work"
    if row.get("is_map"):
        unit_cell = f"{row['child_done']:,}/{max(row['child_expected'], row['child_started']):,}"
        unit_title = (
            f"{row['child_done']:,} of {max(row['child_expected'], row['child_started']):,} "
            f"{row['fan_unit']} done · {row['child_running']:,} in flight — this step "
            f"fans out, so it counts its own {row['fan_unit']}"
        )
    else:
        unit_cell = row["unit"]
        unit_title = f"{_esc(row['name'])} runs once per {row['unit_one']} — every count on this row is {row['unit']}"
    fan = ""
    if row.get("fan"):
        fan = (
            f'<span class="r-fan" title="Each {row["parent_unit_one"]} fans out to '
            f"about {row['fan']} {row['fan_unit']}; the steps below run once per "
            f'{row["fan_unit_one"]}.">×~{row["fan"]}</span>'
        )
    med = _fmt_s(row["median_s"]) if row.get("median_s") is not None else "—"
    med_title = f"Median duration of one {row['unit_one']}: {med}" if row.get("median_s") is not None else "Timed through the steps it contains"
    sel = f"{uid}-s-{rid}"
    return f"""
  <div class="row" role="treeitem" aria-level="{depth + 1}"
       data-row="{rid}" data-depth="{depth}" data-state="{state}">
    <span class="r-name" style="padding-left:{depth * 24}px">
      {caret}<label class="r-lab" for="{sel}"><span class="r-label" title="{_esc(row["name"])}">{_esc(row["name"])}</span>
      {fan}</label>
    </span>
    <label class="r-unit n" for="{sel}" title="{_esc(unit_title)}">{unit_cell}</label>
    <label class="r-track" for="{sel}" title="{share_title}"><span class="r-bar" style="left:{row["start"]:.2f}%;width:{row["share"]:.2f}%"></span></label>
    <label class="r-pct n" for="{sel}" title="{share_title}">{_fmt_pct(row["share"])}</label>
    <label class="r-med n" for="{sel}" title="{_esc(med_title)}">{med}</label>
    <label class="r-sig" for="{sel}">{_badges(row)}</label>
  </div>"""


def _tree_html(uid: str, rows: list[dict[str, Any]], depth: int) -> str:
    out = []
    for row in rows:
        kids = (
            f'<div class="kids" data-kids="{row["id"]}" role="group">{_tree_html(uid, row["children"], depth + 1)}</div>' if row["children"] else ""
        )
        out.append(f'<div class="node" data-node="{row["id"]}">{_row_html(uid, row, depth)}{kids}</div>')
    return "".join(out)


def _detail_html(payload: dict[str, Any], row: dict[str, Any]) -> str:
    if row.get("state") in ("upcoming", "possible"):
        possible = row["state"] == "possible"
        pills = [f'<span class="pill" data-tone="{"flag" if possible else ""}">{"may run" if possible else "queued"}</span>']
        pills.append(f'<span class="pill">counts <b>{row["unit"]}</b></span>')
        why = (
            "A route or gate decides whether this node runs at all, so the console lists it as possible and promises nothing."
            if possible
            else "This node is in the graph and has not started yet. It is drawn from the run's plan, not from a measurement."
        )
        facts = [("Status", "Not started"), ("Why it is listed", why)]
        facts_html = "".join(f'<div class="fact"><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>' for k, v in facts)
        return f'<div class="d-name">{_esc(row["name"])}</div><div class="d-pills">{"".join(pills)}</div><dl class="facts">{facts_html}</dl>'
    pills = [f'<span class="pill" data-tone="accent" title="Share of the run\'s measured work"><b>{_fmt_pct(row["share"])}</b> of wall</span>']
    pills.append(f'<span class="pill" title="{_esc(row["name"])} counts {row["unit"]}">counts <b>{row["unit"]}</b></span>')
    if row.get("flag"):
        pills.append('<span class="pill" data-tone="flag" title="p95 is 3× the median">p95 3× median</span>')
    facts: list[tuple[str, str]] = []
    if row["id"] == "root":
        facts.append(
            (
                payload["unit"].capitalize(),
                f"{payload['done']:,} done · {payload['running']:,} running · {payload['queued']:,} queued",
            )
        )
        if row.get("median_s") is not None:
            facts.append(("Median", f"{_fmt_s(row['median_s'])} per {payload['unit_one']}"))
        facts.append(("Elapsed", _fmt_s(payload["elapsed_s"])))
        if payload.get("remaining_s") is not None:
            facts.append(("Remaining", f"~{_fmt_s(payload['remaining_s'])} at current pace"))
        if payload["terminal"] and payload["failed"]:
            facts.append(("Failures", f"{_n_unit(payload['failed'], payload['unit'])} — see Failures"))
    elif row.get("is_map"):
        facts.append(
            (
                row["fan_unit"].capitalize(),
                f"{row['child_done']:,} done · {row['child_running']:,} in flight",
            )
        )
        facts.append(("Share of wall", _fmt_pct(row["share"])))
        if row.get("fan"):
            facts.append(("Fan-out", f"~{row['fan']} {row['fan_unit']} per {row['parent_unit_one']}"))
    else:
        if row.get("median_s") is not None:
            facts.append(("Median", f"{_fmt_s(row['median_s'])} per {row['unit_one']}"))
        if row.get("p95_s") is not None:
            p95 = _fmt_s(row["p95_s"])
            p95_html = f'<span class="warnv">{p95}</span>' if row.get("flag") else p95
            facts.append(("p95 · max", f"{p95_html} · {_fmt_s(row.get('max_s'))}"))
        cached = row.get("cached_units") or row.get("cached_calls") or 0
        facts.append(
            (
                "Cached",
                f"{cached:,} of {row['count']:,} {row['unit']}" if cached else f"0 {row['unit']}",
            )
        )
        facts.append(
            (
                "Retries",
                f"{row['retries']:,} {row['unit_one']} calls retried" if row.get("retries") else "none",
            )
        )
        facts.append(
            (
                "Errors",
                f"{_n_unit(row['errors'], row['unit'])} — see Failures" if row.get("errors") else "none",
            )
        )
        if row.get("slowest"):
            facts.append(
                (
                    "Active now",
                    f"{row['running']:,} {row['unit']} · slowest: {_esc(row['slowest']['label'])}, {_fmt_s(row['slowest']['seconds'])}",
                )
            )
        elif row.get("running"):
            facts.append(("Active now", f"{row['running']:,} {row['unit']}"))
    facts_html = "".join(f'<div class="fact"><dt>{_esc(k)}</dt><dd>{v}</dd></div>' for k, v in facts)
    callout = ""
    if row.get("flag"):
        callout = (
            '<div class="callout"><div class="t"><i></i>Why this is flagged</div>'
            f"<p>p95 is 3× the median. Deep-dive ONE {row['unit_one']}, not the "
            f"whole run: <code>inspect=True</code> on the slowest {row['unit_one']}.</p></div>"
        )
    return f'<div class="d-name">{_esc(row["name"])}</div><div class="d-pills">{"".join(pills)}</div><dl class="facts">{facts_html}</dl>{callout}'


def render_console(payload: dict[str, Any]) -> str:
    """Render one console frame from a processor payload. Pure function."""
    uid = payload["uid"]
    unit, one = payload["unit"], payload["unit_one"]
    root = payload["tree"]
    all_rows = _walk(root)

    # -- header -------------------------------------------------------------
    if payload["terminal"]:
        state = (
            '<span class="bc-live"><i class="dot" data-tone="saved"></i>'
            f"<b>Saved snapshot</b> · {_fmt_s(payload['elapsed_s'])} wall clock</span>"
            '<span class="bc-chip" data-tone="settled">Settled</span>'
        )
    else:
        remaining = f" · ~{_fmt_s(payload['remaining_s'])} left" if payload.get("remaining_s") is not None else ""
        state = (
            '<span class="bc-live"><i class="dot" data-tone="live"></i>'
            f"<b>Live</b> · {_fmt_s(payload['elapsed_s'])} elapsed{remaining}</span>"
            '<span class="bc-chip" data-tone="running">Running</span>'
        )

    # -- run-level header line ---------------------------------------------
    header_line = (
        f'<span class="bc-sub bc-agg"><b>{payload["done"]:,}/{payload["total"]:,}</b> {unit}'
        f" · <b>{payload['failed']:,}</b> failed"
        f" · <b>{payload['retries']:,}</b> retries"
        f" · <b>{payload['cache_hits']:,}</b> cached</span>"
    )

    # -- stats --------------------------------------------------------------
    stats = [
        (
            f"{payload['done']:,}",
            f"/ {payload['total']:,}",
            f"{unit.capitalize()} done",
            "",
            f"{payload['done']} of {payload['total']} {unit} have finished end to end.",
        )
    ]
    if payload["terminal"]:
        stats.append(
            (
                _fmt_s(payload["elapsed_s"]),
                "",
                "Wall clock",
                "",
                "Total time from start to settle.",
            )
        )
        stats.append(
            (
                f"{payload['cache_hits']:,}",
                "",
                "Calls from cache",
                "",
                f"{payload['cache_hits']} calls were served from cache instead of being paid again.",
            )
        )
    else:
        stats.append(
            (
                _fmt_s(payload["elapsed_s"]),
                "",
                "Elapsed",
                "",
                "Wall-clock time since the run started.",
            )
        )
        stats.append(
            (
                f"~{_fmt_s(payload['remaining_s'])}" if payload.get("remaining_s") is not None else "—",
                "",
                "Remaining",
                "",
                "Projection at the current pace.",
            )
        )
    stats.append(
        (
            f"{payload['failed']:,}",
            "",
            f"{unit.capitalize()} failed",
            ' data-tone="bad"' if payload["failed"] else "",
            f"{payload['failed']} {unit} ended in failure. Every one is listed under Failures.",
        )
    )
    stats_html = "".join(
        f'<div class="stat"{tone} title="{_esc(title)}">'
        f'<div class="stat-v">{v}{f"<small>{suffix}</small>" if suffix else ""}</div>'
        f'<div class="stat-l">{label}</div></div>'
        for v, suffix, label, tone, title in stats
    )

    # -- progress -----------------------------------------------------------
    prog = {
        "done": payload["done"],
        "running": payload["running"],
        "failed": payload["failed"],
        "queued": payload["queued"],
    }
    seg = lambda k, n, label: (  # noqa: E731
        f'<span class="seg" data-k="{k}" style="flex:{n} 1 0" title="{label}"></span>' if n else ""
    )
    key = lambda color, word, n, title: (  # noqa: E731
        f'<span title="{title}"><i class="key" style="background:{color}"></i><b>{n:,}</b> <i>{one if n == 1 else unit}</i> {word}</span>'
    )
    progress_html = f"""
    <div class="segs" role="img" aria-label="{prog["done"]} {unit} done, {prog["running"]} running, {prog["failed"]} failed, {prog["queued"]} queued, of {payload["total"]} {unit}">
      {seg("done", prog["done"], f"{prog['done']} {unit} done")}
      {seg("running", prog["running"], f"{prog['running']} {unit} running right now")}
      {seg("failed", prog["failed"], f"{prog['failed']} {unit} failed")}
      {seg("queued", prog["queued"], f"{prog['queued']} {unit} queued")}
    </div>
    <div class="legend">
      {key("var(--ok)", "done", prog["done"], f"{prog['done']} {unit} finished end to end")}
      {key("var(--accent)", "running", prog["running"], f"{prog['running']} {unit} in flight right now") if prog["running"] else ""}
      {key("var(--bar-track)", "queued", prog["queued"], f"{prog['queued']} {unit} not started yet") if prog["queued"] else ""}
      {key("var(--bad)", "failed", prog["failed"], f"{prog['failed']} {unit} ended in failure")}
    </div>"""

    # -- notice (from capacity hook) ----------------------------------------
    notice_html = ""
    for lane in payload["capacity"]:
        paused = lane.get("paused_seconds") or 0
        if paused > 0:
            notice_html = (
                f'<div class="notice"><b>{_esc(lane["name"])} lane cooldown '
                f'{paused:.0f}s</b><span class="notice-text">The provider sent '
                "Retry-After; new calls wait. Obedience, not a hang.</span></div>"
            )
            break

    # -- tree ---------------------------------------------------------------
    tree_hint = f"share of the {_fmt_s(payload['elapsed_s'])} wall clock"
    tree_html = _tree_html(uid, [root], 0)

    # -- lanes --------------------------------------------------------------
    lanes_html = ""
    if payload["capacity"]:
        lane_rows = []
        for lane in payload["capacity"]:
            busy, cap = int(lane.get("busy", 0)), max(1, int(lane.get("cap", 1)))
            waiting = int(lane.get("waiting", 0))
            paused = lane.get("paused_seconds") or 0
            pill = f'<span class="lane-p">cooldown {paused:.0f}s</span>' if paused > 0 else ""
            title = f"{busy} of {cap} {lane['name']} permits are busy; " + (f"{waiting} calls are queued." if waiting else "no calls are waiting.")
            lane_rows.append(
                f'<div class="lane" title="{_esc(title)}">'
                f'<span class="lane-n">{_esc(lane["name"])}</span>'
                f'<span class="lane-t"><span class="lane-b" style="width:{100 * busy / cap:.0f}%"></span></span>'
                f'<span class="lane-m"><b>{busy}/{cap}</b> permits busy</span>'
                f'<span class="lane-m">{f"<b>{waiting}</b> calls waiting" if waiting else "no calls waiting"}</span>'
                f"<span>{pill}</span></div>"
            )
        lanes_html = (
            '<div class="lanes"><div class="sec"><h3>Work lanes</h3>'
            '<span class="hint">permits busy right now</span></div>' + "".join(lane_rows) + "</div>"
        )

    # -- legend-marks (derived fan-out sentence) ----------------------------
    fan_bits = []
    for row in all_rows:
        if row.get("is_map") and row.get("fan"):
            fan_bits.append(
                f"<b>{row['fan_unit']}</b> inside <b>{_esc(row['name'])}</b>, which "
                f"fans out to about {row['fan']} {row['fan_unit']} per {row['parent_unit_one']}"
            )
    fan_sentence = " Each step names the unit it counts — <b>" + unit + "</b> at the top level, " + "; ".join(fan_bits) + "." if fan_bits else ""
    legend_marks = (
        '<p class="legend-marks">Bars share one wall-clock axis, so a child sits '
        f"inside the span of its parent.{fan_sentence} "
        '<span class="foot-note" title="Shares are shares of measured work. Stages '
        "that overlap in time can add up to more than their parent; the numbers in "
        'this run tile cleanly.">Shares are wall-clock shares; concurrent stages '
        "need not tile exactly.</span></p>"
    )

    # -- detail pane --------------------------------------------------------
    details_html = "".join(f'<div data-detail="{row["id"]}">{_detail_html(payload, row)}</div>' for row in all_rows)

    # -- failures -----------------------------------------------------------
    fails_html = ""
    if payload["failures"]:
        shown = payload["failures"][:10]
        rows_html = "".join(
            f'<div class="fail"><span class="fail-dot" aria-hidden="true"></span>'
            f'<span class="fail-doc">{_esc(f["item"])}</span>'
            f'<span class="fail-step" title="Failed in the {_esc(f["step"])} step">{_esc(f["step"])}</span>'
            f'<span class="fail-msg" title="{_esc(f["message"])}">{_esc(f["message"])}</span></div>'
            for f in shown
        )
        more = f" · first {len(shown)} shown" if len(payload["failures"]) > len(shown) else ""
        fails_html = (
            f'<div class="fails"><div class="sec"><h3>Failures · <span class="count">'
            f'{_n_unit(payload["failed"], unit)}</span></h3><span class="hint">'
            f"{unit} kept for review — nothing is retried automatically{more}</span></div>" + rows_html + "</div>"
        )

    # -- footer -------------------------------------------------------------
    waiting_total = sum(int(lane.get("waiting", 0)) for lane in payload["capacity"])
    if payload["terminal"]:
        foot_left = "Snapshot saved · interactive without a kernel"
        foot_right = f"{payload['retries']:,} retries absorbed · {_n_unit(payload['failed'], unit)} kept"
    else:
        foot_left = f"Live · updated {payload['updated_ago_s']:.0f}s ago · streaming events"
        foot_right = f"{payload['running']:,} {unit} running" + (f" · {waiting_total:,} calls waiting" if payload["capacity"] else "")

    # -- selection / collapse inputs and their CSS --------------------------
    inputs = [
        f'<input type="radio" name="{uid}-sel" id="{uid}-s-{row["id"]}"' + (" checked" if row["id"] == "root" else "") + ">" for row in all_rows
    ]
    inputs += [f'<input type="checkbox" id="{uid}-c-{row["id"]}">' for row in all_rows if row["children"]]
    dyn_css = []
    for row in all_rows:
        rid = row["id"]
        dyn_css.append(f'#{uid}-s-{rid}:checked~.bc [data-row="{rid}"]{{background:var(--accent-tint);border-color:var(--accent-line)}}')
        dyn_css.append(f'#{uid}-s-{rid}:checked~.bc [data-row="{rid}"] .r-label{{font-weight:600}}')
        dyn_css.append(f'#{uid}-s-{rid}:checked~.bc [data-row="{rid}"] .r-bar{{background:var(--bar-sel)}}')
        dyn_css.append(f'#{uid}-s-{rid}:checked~.bc [data-detail="{rid}"]{{display:block}}')
        if row["children"]:
            dyn_css.append(f'#{uid}-c-{rid}:checked~.bc [data-kids="{rid}"]{{display:none}}')
            dyn_css.append(
                f'#{uid}-c-{rid}:checked~.bc [data-caret="{rid}"] svg,'
                f'#{uid}-c-{rid}:checked~.bc .caret[for="{uid}-c-{rid}"] svg'
                "{transform:rotate(-90deg)}"
            )

    eyebrow = "Hypergraph map" if payload["mode"] == "map" else "Hypergraph run"
    css = _CSS.replace("$UID", uid) + "".join(dyn_css)

    frame = f"""<div id="{uid}">
<style>{css}</style>
{"".join(inputs)}
<section class="bc" aria-label="Console">
  <div class="bc-head">
    <div>
      <div class="eyebrow">{eyebrow}</div>
      <span class="bc-title">{_esc(payload["title"])}</span>
      <span class="bc-sub">{_esc(payload["subtitle"])}</span>
      {header_line}
    </div>
    <div class="bc-status">{state}</div>
  </div>
  <div class="stats">{stats_html}</div>
  <div class="prog">{progress_html}</div>
  {notice_html}
  <div class="bc-body">
    <div class="bc-main">
      <div class="sec"><h3>Steps</h3><span class="hint">{tree_hint}</span></div>
      <div class="tree" role="tree" aria-label="Steps of {_esc(payload["title"])}">
        <div class="colhead" aria-hidden="true">
          <span>Step</span><span title="The unit every number on that row is counted in.">Counts</span><span title="One shared wall-clock axis: each step's segment sits inside its parent's span.">Share of wall</span><span class="r">%</span><span class="r">Median</span><span class="r">Signals</span>
        </div>
        {tree_html}
      </div>
      {lanes_html}
      {legend_marks}
    </div>
    <aside class="bc-side">
      <div class="eyebrow">Step details</div>
      {details_html}
    </aside>
  </div>
  {fails_html}
  <div class="bc-foot"><span>{foot_left}</span><span>{foot_right}</span></div>
</section>
</div>"""
    state_key = payload.get("state_key") or widget_state_key("console", payload["title"], payload["unit"])
    return theme_wrap(frame + _state_script(uid, state_key), state_key=state_key)


# ---------------------------------------------------------------------------
# surviving the refresh — the collapse a reader chose outlives every frame
# ---------------------------------------------------------------------------


def _state_script(uid: str, state_key: str) -> str:
    """Re-apply the reader's own expand/collapse and selection to a new frame.

    A live console replaces its whole frame a few times a second, and a
    replaced DOM node loses the checkbox state that IS the collapse. So each
    frame carries this: read what the reader last chose, apply it before
    paint, and record every later change.

    Deliberately not ``document.currentScript``. A notebook front end that
    injects output HTML and re-creates the script node leaves that null, so
    the container is found by the id the frame already has.

    Storage is ``localStorage`` when the front end allows it and a
    window-scoped map when it does not (some webviews partition or refuse
    it), so the state at worst lives as long as the page. Everything is in
    one try/catch: a front end that strips scripts entirely loses only the
    PERSISTENCE — the collapse itself is CSS, and keeps working.
    """
    return (
        "<script>(function(){try{"
        f"var U={uid!r},K={state_key!r};"
        "var R=document.getElementById(U);if(!R)return;"
        "var S=(function(){try{var t='hypergraph:probe';localStorage.setItem(t,'1');"
        "localStorage.removeItem(t);return localStorage;}catch(e){"
        "var m=(window.__hgConsoleState=window.__hgConsoleState||{});"
        "return{getItem:function(k){return k in m?m[k]:null;},setItem:function(k,v){m[k]=v;}};}})();"
        "var P='hypergraph:console:'+K+':';var n=U.length+3;"
        "var box=R.querySelectorAll('input[type=checkbox]');"
        "for(var i=0;i<box.length;i++){var c=box[i];var ck=P+'c:'+c.id.slice(n);"
        "var v=S.getItem(ck);if(v==='1')c.checked=true;else if(v==='0')c.checked=false;"
        "if(!c.__hgB){c.__hgB=1;c.addEventListener('change',function(ev){try{"
        "S.setItem(P+'c:'+ev.target.id.slice(n),ev.target.checked?'1':'0');}catch(_e){}});}}"
        "var rad=R.querySelectorAll('input[type=radio]');var want=S.getItem(P+'sel');"
        "for(var j=0;j<rad.length;j++){var r=rad[j];"
        "if(want&&r.id.slice(n)===want)r.checked=true;"
        "if(!r.__hgB){r.__hgB=1;r.addEventListener('change',function(ev){try{"
        "if(ev.target.checked)S.setItem(P+'sel',ev.target.id.slice(n));}catch(_e){}});}}"
        "R.setAttribute('data-hg-console-restored','1');"
        "}catch(e){}})()</script>"
    )


# ---------------------------------------------------------------------------
# live display glue (IPython display handle, in-place updates)
# ---------------------------------------------------------------------------


class LiveConsole(ConsoleProcessor):
    """The processor plus a live in-place notebook display.

    Pass an instance straight to ``event_processors=[console]`` — or let
    ``show_progress=True`` construct one for you in a notebook. On the first
    event it claims ONE display handle; while an asyncio loop is running a
    background ticker re-renders it in place a few times a second (the sync
    runner falls back to throttled renders on the events themselves); the
    settled frame is rendered once at shutdown into the same handle and stays
    in the saved notebook.
    """

    def __init__(self, *, refresh_seconds: float = 0.3, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._refresh = refresh_seconds
        self._handle = None
        self._ticker = None
        self._last_render = 0.0
        self.frames = 0

    def on_event(self, event: Any) -> None:
        super().on_event(event)
        self._ensure_live()

    def _ensure_live(self) -> None:
        if self._handle is None:
            try:
                from IPython.display import HTML, display

                self._handle = display(HTML(render_console(self.payload())), display_id=self.uid)
                self.frames += 1
                self._last_render = self._clock()
            except Exception:
                self._handle = False  # no notebook front end; fold silently
        if self._ticker is None:
            try:
                import asyncio

                self._ticker = asyncio.get_running_loop().create_task(self._tick())
            except RuntimeError:
                self._ticker = False  # no loop (sync runner); throttled renders
        if self._ticker is False:
            now = self._clock()
            if now - self._last_render >= self._refresh:
                self._render()

    async def _tick(self) -> None:
        import asyncio

        while not self.terminal:
            await asyncio.sleep(self._refresh)
            self._render()

    def _render(self) -> None:
        if not self._handle:
            return
        from IPython.display import HTML

        self._handle.update(HTML(render_console(self.payload())))
        self.frames += 1
        self._last_render = self._clock()

    def shutdown(self) -> None:
        if self._ticker not in (None, False):
            self._ticker.cancel()
        self._ticker = None
        self._render()
