# Viz Compatibility Matrix (PR #88, Stage 6)

The viz pipeline is rebuilt around a compact IR + twin scene_builders
(see [`src/hypergraph/viz/CLAUDE.md`](../src/hypergraph/viz/CLAUDE.md)).
Each rendering surface has its own constraints (kernel availability,
JS execution policy, trust state). This document is the contract for
how each surface is expected to behave and how the contract is verified.

## Fallback contract

When a row says **"static fallback"** it must satisfy *all* of:

- **(a)** The graph is visible — initial expansion state rendered as static HTML.
- **(b)** Zero JS console errors — no broken script references, no thrown exceptions.
- **(c)** Saved output is preserved bytewise — re-opening the same file in a supported surface still reaches the interactive path.
- **(d)** *Optional:* a non-modal one-line hint indicating reduced interactivity.

Any fallback that fails (a), (b), or (c) is a regression — "documented fallback" never means "we documented that it breaks."

## Matrix

| # | Surface | Expected behavior | Gating | Status |
|---|---|---|---|---|
| 1 | JupyterLab, trusted, kernel running | Interactive | automated | ⚠ deferred — needs Jupyter-server Playwright harness ([#92](https://github.com/gilad-rubin/hypergraph/issues/92)) |
| 2 | JupyterLab, trusted, no kernel | Interactive | automated | ⚠ deferred — same |
| 3 | JupyterLab, untrusted | Static fallback | automated | ⚠ deferred — same |
| 4 | VSCode Jupyter, kernel running | Interactive | automated | ⚠ deferred |
| 5 | VSCode Jupyter, reopen no kernel | Interactive OR static fallback | automated *if reachable*, else manual | ☐ manual |
| 6 | Colab | Interactive OR static fallback | manual one-time per PR | ☐ manual |
| 7 | nbviewer | Static fallback OR interactive (document actual) | manual one-time per PR | ☐ manual |
| 8 | GitHub-style render (JS disabled) | Static fallback contract (a)+(b)+(c) | **automated** | ✅ `tests/viz/test_compatibility_matrix.py::test_github_render_with_js_disabled_fallback_contract` |
| 9 | `filepath=...` HTML, offline | Interactive (clicks work) | **automated** | ✅ `tests/viz/test_compatibility_matrix.py::test_filepath_html_opens_offline` + existing click-through tests in `test_interactive_expand.py` |
| 10 | `nbconvert --to html` | Static view, no broken scripts | **automated** | ✅ `tests/viz/test_compatibility_matrix.py::test_nbconvert_to_html_produces_valid_static_view` |

## Manual checklist (PR authors)

Before requesting review on a viz-touching PR, walk through the manual
rows that are reachable from your environment:

```markdown
- [ ] **Row 5 (VSCode Jupyter, no kernel reopen)** — open a saved notebook with a viz cell, kernel not running. Verify clicks work OR fallback contract holds. Attach screenshot.
- [ ] **Row 6 (Colab)** — upload a saved notebook to Colab, run the cells, save, reopen. Verify interactive OR fallback. Attach screenshot.
- [ ] **Row 7 (nbviewer)** — push the saved notebook to a public gist, view via https://nbviewer.org/. Verify rendered output. Attach screenshot.
```

Manual rows are *not* CI gates — they are PR-description checklist items.
The author runs the check once and posts evidence; reviewers don't re-verify.

## Deferred work

**Rows 1–4 (JupyterLab + VSCode kernel-running automation)** require
launching Jupyter Server / VSCode extension headlessly under
Playwright. That infrastructure does not yet exist in this repo and
landing it is a separate initiative — tracked in
[#92](https://github.com/gilad-rubin/hypergraph/issues/92) as part of
the anywidget shell deferral. Until then, rows 1–4 are covered
informally: the iframe path's contract (rows 8–10) plus the IR's
schema-version fallback (Stage 4a) keep regressions from those
surfaces from being silent failures.

## Why this is enough for PR #88

Rows 8, 9, 10 cover the three regression classes that the original
issue actually called out:

- **User story 1 (GitHub render)** → row 8, automated.
- **User story 6 (`filepath=...` offline)** → row 9, automated.
- **User stories 18, 25 (saved-output portability)** → row 10, automated.

Rows 1–4 require a Jupyter-server Playwright harness; rows 5–7 require
hosts we can't run headlessly. Those gaps are real but they exist
before this PR too — closing them belongs in a follow-up where the
Jupyter-server harness is the deliverable, not a side effect.
