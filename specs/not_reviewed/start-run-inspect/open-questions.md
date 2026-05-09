# Open Questions

Items left undecided at the end of session 4.

## 1. Should `handle.result()` raise by default?

Spec leaves this as an explicit dilemma. Two clean shapes:

```python
result = run.result(raise_on_failure=False)
# vs
result = run.result(); result.raise_if_failed()
```

The implementation in `handles.py` chose `result(raise_on_failure: bool = True)`. The dilemma section is still in the spec; revisit if the default feels wrong in real usage. The hard requirement (which is settled): the handle path must never force `try/except` just to inspect what happened.

## 2. Should `start_map()` accept `error_handling`?

Or should aggregate failure policy stay only on the blocking `map()`, with batch handles always non-raising? Current implementation pops `error_handling` and forces `error_handling="continue"` internally — effectively the second option, but not by explicit design decision.

## 3. By-id stop vs handle-local stop

Now that `workflow_id` is checkpoint-only, the external `runner.stop(workflow_id, info=...)` story needs clarification: is it checkpoint-backed `workflow_id` only, or should there be a separate ephemeral `run_id`-based control path? The handle-local `run.stop(...)` is the primary surface, but the by-id surface still exists in the runner today.

## 4. `anywidget` as a new dependency

`ipywidgets` is already a dep; `anywidget` is not. The viewer plan calls for `anywidget` to keep notebook transport small (avoid huge HTML payload churn). Adding it is a real decision — the alternative is rebuilding similar mechanics on top of `ipywidgets` directly or using whatever `viz/js` already does. Not yet committed.

## 5. Error-surface enrichment — when?

The "improve plain error messages" follow-on subtask is in the spec with a concrete before/after target and acceptance bar:

```
# after
ValueError: transient failure
Note: Hypergraph failed at graph=outer / node=embed / item=23
Note: run_id=run-abc123 workflow_id=job-123
Note: inspect with run.view() or rerun via retry()/fork(...)
```

It is not blocking the first inspect/start cut. No date or PR sequencing decided.

## 6. Live widget richness vs first-cut scope

Current implementation has `InspectWidget` and `MapInspectWidget` using `IPython.display` + `display_id`. The full waterfall + intermediate output panel + topology composition described in the spec's viewer plan is not yet built. Decision pending: ship the basic widget and iterate toward the full viewer, or block until the rich viewer is in place. The session-4 reviewer flagged that the live-view contract is "not yet demonstrated" against a real renderer.

## 7. Item-level drill-down for nested maps in the live view

The spec promises nested maps get item-level drill-down from the same inspect surface. The current `MapInspectWidget` shows batch progress but item drill-down inside the live widget is not wired yet. Open: at what level of fidelity does this need to land for the first cut to feel done?
