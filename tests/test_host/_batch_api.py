"""Submit a keyed manifest through the runner-shaped public Batch API.

Issue #342 removed the public mapping-of-item-key-to-inputs submission
shape: ``submit_batch`` now takes runner-shaped values plus ``map_over`` /
``map_mode`` / ``key_by``, and freezes the expansion into the same immutable
manifest. The Batch machinery the ticket-03/05/06 suites pin — atomic
acceptance, keyed outcomes, tolerance, stop, subset rerun, SIGKILL recovery
— is unchanged, so those suites keep their scenarios and come through the
new door instead.

``keyed_values`` is the adapter: it transposes ``{"p-0": {"x": 0}, ...}``
into ``{"item": ["p-0", ...], "x": [0, ...]}`` with ``key_by="item"``, so
every existing item key and per-item input survives verbatim. The graphs in
those suites declare an ``item: str`` input for exactly this reason — a
value no node consumes would raise an unrecognized-input warning, which the
warning-as-error CI treats as a failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The expanded input whose per-item value is the logical item key.
ITEM_KEY_INPUT = "item"


def graph_of(host, name: str):
    """The served Graph object for ``name``, for suites that kept only a name.

    Issue #342 made every new-work verb graph-first: ``host.submit(graph,
    ...)`` resolves the Definition from the Graph's own pinned identity, and
    a Definition-name string is no longer a selector. The pre-existing Host
    suites test the machinery BEHIND that door — identity, dedup, stop,
    recovery, tolerance, admission, pause slots — and most of them build
    their graph inline inside ``serve(...)``.

    Rather than restructure ~180 call sites that never cared which door they
    came through, they ask the Host for the object it already holds. The
    door itself is proven by ``test_batch_interrupt_matrix.py``, which
    submits real Graph objects and asserts an unserved one is refused.
    """
    return host._definitions[name].graph


def keyed_values(items: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, list[Any]], list[str]]:
    """Transpose a keyed manifest into runner-shaped ``(values, map_over)``.

    Manifest order is the caller's mapping order, exactly as before: it is
    the order ``key_by`` freezes and the order keyed outcomes report in.
    """
    keys = list(items)
    params = list(dict.fromkeys(name for value in items.values() for name in value))
    values: dict[str, list[Any]] = {ITEM_KEY_INPUT: keys}
    for name in params:
        values[name] = [items[key][name] for key in keys]
    return values, [ITEM_KEY_INPUT, *params]


def submit_keyed(host, graph, items: Mapping[str, Mapping[str, Any]], **kwargs):
    """``host.submit_batch`` for a keyed manifest (async)."""
    values, map_over = keyed_values(items)
    return host.submit_batch(graph, values, map_over=map_over, key_by=ITEM_KEY_INPUT, **kwargs)


def submit_keyed_sync(host, graph, items: Mapping[str, Mapping[str, Any]], **kwargs):
    """``host.submit_batch_sync`` for a keyed manifest."""
    values, map_over = keyed_values(items)
    return host.submit_batch_sync(graph, values, map_over=map_over, key_by=ITEM_KEY_INPUT, **kwargs)
