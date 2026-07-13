# Notebook visualization transport: payload, compatibility, and live updates

- Date: 2026-07-13
- Issue: [#92](https://github.com/gilad-rubin/hypergraph/issues/92)
- Implementation: [Compress saved notebook graph output and add truthful static topology](https://github.com/gilad-rubin/hypergraph/issues/224)
- Measured revision: `1e597ebc3e314f872acbb5b67ccfbb757827cafe`
- Decision status: approved by the maintainer on 2026-07-13

## Answer

**Approved contract:** retain a self-contained iframe
as `graph.visualize()`'s primary notebook transport, and leave `filepath=` as
the current standalone offline HTML document. Do **not** adopt anywidget for
those settled graph outputs. Add a script-free static topology to the saved
notebook output for JupyterLab's
untrusted sanitizer and static/JavaScript-disabled hosts that preserve safe
HTML. The saved HTML must declare only safe semantic topology; a trusted
bootstrap script constructs the sandboxed iframe and hides the fallback after
the iframe becomes ready. VS Code Restricted Mode suppresses the entire rich
output area, including safe HTML and `text/plain`; that host policy needs an
explicit exception rather than an impossible fallback promise.
Saving a literal iframe beside safe markup is not sufficient: untrusted
JupyterLab removed that entire mixed output in the probe below.

Package the static iframe shell as pre-minified, pre-gzipped bytes and use a
native base64 wrapper in saved notebook cells. A real prototype reduced ten
representative saved cells from 9,124,861 to **2,187,441 bytes (76.0%)** without
meaningful paired render regression. Hosts without trusted JavaScript or
`DecompressionStream` keep the safe topology instead of a broken placeholder.
`filepath=` remains the current complete offline document rather than paying a
decode step or depending on notebook-host behavior.

A separate real-host prototype also settles #156's preferred live seam without
anywidget: initialize one inspect shell, then send small, versioned semantic
state through a payload-only display channel at a bounded rate. In JupyterLab,
41 refresh attempts became nine 967-1,752-byte updates while the iframe,
`contentWindow`, `srcdoc`, and selected tab stayed unchanged. A separate small
sentinel fixture proved terminal persistence on no-kernel JupyterLab. A third,
970,610-byte real-topology fixture passed a physical no-kernel VS Code witness:
one iframe retained the same runtime identity through terminal sequence 4,
while a user-selected Graph tab survived and showed the real two-node topology.
Run handles remain control-only. Reconsider anywidget only if the built-in
bridge later fails a required host contract that cannot be met safely.

Ten representative saved visualization cells are 9,125,256 bytes, so repeated
payload is real. However, anywidget is not automatically a page-scope asset
cache. Its current implementation creates a runtime per widget model and loads
inline ESM through a new blob URL per model. A size-only lower bound still made
a 7,636,696-byte ten-cell notebook (16.3% smaller) before adding the required
fallback. That gain does not justify a second transport and widget stack.

More urgently, the current compatibility contract is false in static hosts:
JavaScript-disabled output shows only `Rendering interactive view…`, and real
unsigned JupyterLab output loses the iframe entirely. The current test accepts
that placeholder instead of proving the documented visible-graph fallback.

```text
Before
  trusted host:       interactive iframe
  untrusted/JS-off:   empty output or "Rendering interactive view…"
  10 saved cells:     9,125,256 bytes

Approved after
  trusted host:       same interactive iframe from a compressed static shell
  untrusted/JS-off:   safe topology generated from the same IR
  filepath export:    unchanged offline interactive file
  10 saved cells:     2,187,441 bytes in the measured prototype (76.0% smaller)
  live inspection:    one stable shell + 967-1,752-byte semantic updates
  anywidget:          not adopted for graph output or the proven live seam
```

Claim labels used below: **Measured** is local numeric evidence; **Automated**
exercised current code in a real bounded host/browser; **Automated prototype**
exercised temporary candidate code in a real bounded host/browser; **Physical
witness** inspected a real desktop host through its user interface; **Manual /
unverified** makes no compatibility claim; **Inferred** follows from cited
implementation without a production prototype.

## Current transport and payload

`visualize()` builds `GraphIR`, places it in `graph_data`, and generates HTML
(`src/hypergraph/viz/widget.py:148-165`). The generator inlines seven vendor
assets and nine Hypergraph JS modules
(`src/hypergraph/viz/html/generator.py:57-97` and
`src/hypergraph/viz/assets/__init__.py:8-18`). `_VizCellOutput` then escapes that
complete document into iframe `srcdoc` (`src/hypergraph/viz/widget.py:36-54`).
Every saved cell is therefore an independent document.

The implementation must preserve the useful parts of the current boundary:
graph JSON escapes `</` before entering a script element
(`src/hypergraph/viz/html/generator.py:15-20,63-64`), the iframe document is
attribute-escaped, and the iframe is sandboxed
(`src/hypergraph/viz/widget.py:36-54`). The current sandbox also grants
`allow-popups` and `allow-forms` without a demonstrated consumer; the
implementation ticket must remove or justify those permissions and separately
justify retaining `allow-same-origin`. The safe fallback stays visible until a
ready message arrives from the expected iframe `contentWindow` with a
per-output nonce; iframe `load` alone is insufficient, and an initialization
error must leave the fallback visible. Jupyter separately sanitizes untrusted
HTML and never executes untrusted JS
([Jupyter security](https://jupyter-server.readthedocs.io/en/stable/operators/security.html)).

### Saved-output size

**Measured.** The representative cell is
`tests.viz.conftest.make_workflow()`—nested `preprocess` feeding `analyze`
(`tests/viz/conftest.py:106-123,161-164`). The harness formatted the real
`_VizCellOutput` MIME bundle and serialized it with `nbformat 5.10.4`; each
source-only control used identical cells with cleared outputs.

| Render cells | Source-only notebook | Saved notebook | Output increment |
|---:|---:|---:|---:|
| 1 | 627 B | 912,943 B | 912,316 B |
| 2 | 790 B | 1,825,422 B | 1,824,632 B |
| 10 | 2,094 B | 9,125,256 B | 9,123,162 B |

The increment is approximately 912,316 bytes per cell, plus small notebook
serialization variation. The tracked
`notebooks/visualization_examples.ipynb` is 11,546 bytes because it has zero
output objects; it does not represent a saved visualization notebook.

Per-cell composition below is an exact serialized-notebook byte delta after
removing each raw component before `_repr_html_()`:

| Component | Raw iframe document | Saved contribution | Share |
|---|---:|---:|---:|
| Vendor JS + CSS | 616,007 B | 728,716 B | 79.9% |
| Hypergraph JS (nine modules) | 126,405 B | 171,450 B | 18.8% |
| Representative `GraphIR` | 2,161 B | 3,619 B | 0.4% |
| Shell, options, tags, repr, serialization | 5,176 B | 8,531 B | 0.9% |
| **Total** | **749,749 B** | **912,316 B** | **100%** |

The generator's vendor list is authoritative
(`src/hypergraph/viz/html/generator.py:83-100`). IR reduction cannot solve this
problem; even deleting the representative IR saves under 0.4%. First-party
minification can affect at most 18.8%; the vendor bundle is the main
compression or deduplication target.

### First versus subsequent render

**Measured, machine-local baseline.** Seven fresh Chromium contexts each
inserted ten iframes sequentially and waited for
`window.__hypergraphVizReady`, which is set after layout and viewport fitting
(`src/hypergraph/viz/assets/viz.js:297-353,400-407`). Hardware: Apple M5 Pro,
48 GB; Python 3.12.13, Playwright 1.57.0, Chromium 143.0.7499.4.

| Metric | Result |
|---|---:|
| First cell median (range) | 68.4 ms (65.6-83.0) |
| Cells 2-10 pooled median / p95 | 39.9 / 40.74 ms |
| Subsequent / first median ratio | 0.583 |
| Ten sequential cells, median total | 425.1 ms |

Per-index medians were
`[68.4, 39.2, 40.0, 39.7, 40.0, 39.9, 40.3, 39.6, 39.3, 40.1]`.
Warm-browser benefit exists, but each iframe still pays about 40 ms.

## Compatibility matrix evidence

The binding fallback requires a visible graph, no console errors, and preserved
saved bytes (`dev/VIZ-COMPATIBILITY-MATRIX.md:9-18`).

| Surface | Subject | Evidence | Result |
|---|---|---|---|
| JupyterLab trusted, no kernel | Current output | **Automated.** Isolated JupyterLab 4.5.1 reported `GET /api/kernels => []`; a signed saved notebook rendered one iframe, two React Flow nodes, and zero page errors. | **Pass: interactive.** |
| JupyterLab untrusted | Current output | **Automated.** A signature-invalid notebook rendered zero iframe and zero nodes; the server logged `Notebook unsigned.ipynb is not trusted`. | **Fail: no fallback.** |
| JupyterLab signed and unsigned, no kernel | Compressed candidate | **Automated prototype.** The unsigned copy retained seven topology items, stripped the scripts, and created zero iframes. The signed copy reported kernels before/after `[]`, created one iframe with two nodes, and hid the topology only after the renderer-ready message. | **Pass for the candidate shape.** |
| VS Code Jupyter, normal/no kernel | Current graph output | **Physical witness.** VS Code 1.128.0 with Jupyter 2025.9.1 and Renderers 1.3.0 opened the saved notebook with `Select Kernel` still visible; the webview contained `about:srcdoc`, the graph node, input, edge, and controls. | **Pass: interactive.** |
| VS Code Jupyter, normal/no kernel | Built-in live candidate | **Physical witness.** A two-output saved fixture opened without selecting a kernel. The shell and channel reached terminal sequence 4 with the same runtime UUID; the user-selected Graph tab survived later updates and showed `load_customer` -> `rank_options`. | **Pass: stable live handshake.** |
| VS Code Jupyter, Restricted Mode | Current + safe candidate shape | **Physical witness.** An explicitly untrusted folder showed the Restricted Mode banner, but neither the current output nor a second safe-topology/bootstrap probe displayed any rich output. Microsoft's workspace-trust contract disables or limits features in Restricted Mode ([Workspace Trust](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust)). | **Host-policy exception: trust the workspace.** |
| `nbconvert --to html` | Compressed candidate | **Automated prototype.** nbconvert 7.16.6 produced a 487,871-byte document. Offline Chromium rendered two nodes with template requests blocked; JavaScript-disabled conversion retained the semantic fallback. | **Pass for the candidate shape.** |
| GitHub / JS disabled | Current output | **Automated simulation + official rule.** JavaScript-disabled output showed only boot text and zero nodes. GitHub renders notebooks as static HTML and does not run custom JS ([GitHub docs](https://docs.github.com/en/repositories/working-with-files/using-files/working-with-non-code-files#working-with-jupyter-notebook-files-on-github)). | **Fail: no graph.** |
| JavaScript disabled | Compressed candidate | **Automated prototype.** A local `file://` view showed seven topology items, zero iframes, and no broken placeholder. Exact GitHub-hosted rendering was not physically witnessed. | **Pass locally; GitHub row remains an implementation witness.** |
| Offline `filepath=` | Unchanged current export | **Automated.** A 749,749-byte export made only its `file://` request, reached ready with two nodes, expanded to five after a click, and emitted zero page errors. | **Pass; candidate does not alter this path.** |

The candidate rows still requiring implementation-stage evidence are JupyterLab
with a running kernel, VS Code with a running kernel, the compressed graph
candidate reopened in normal VS Code, Colab, nbviewer, and exact GitHub-hosted
rendering. The live VS Code bridge itself is no longer an evidence gap.

The 15 focused tests pass, but that does not prove the matrix:

```console
$ uv run pytest tests/viz/test_payload_size.py \
    tests/viz/test_compatibility_matrix.py \
    -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning' -q
...............                                                          [100%]
```

The JS-disabled test explicitly calls the boot placeholder contract (a)
(`tests/viz/test_compatibility_matrix.py:85-100,121-128`), contradicting the
visible-graph requirement. The committed matrix also cites an old test name and
leaves JupyterLab/VS Code deferred
(`dev/VIZ-COMPATIBILITY-MATRIX.md:22-33,49-59`).

## anywidget evidence and option cost

anywidget supports Jupyter, JupyterLab, Colab, and VS Code, and accepts inline
or file-backed `_esm` ([getting started](https://anywidget.dev/en/getting-started/),
[AFM](https://anywidget.dev/en/afm/)). At official revision
[`e538682`](https://github.com/manzt/anywidget/tree/e53868272aa950fd10d1d4a95d52e556d5d5a09c):

- `_esm`/`_css` become synchronized traits
  ([Python source](https://github.com/manzt/anywidget/blob/e53868272aa950fd10d1d4a95d52e556d5d5a09c/anywidget/widget.py#L40-L45));
- every model gets a runtime
  ([widget.ts](https://github.com/manzt/anywidget/blob/e53868272aa950fd10d1d4a95d52e556d5d5a09c/packages/anywidget/src/widget.ts#L22-L42)); and
- inline ESM is imported through a newly created blob URL
  ([load.ts](https://github.com/manzt/anywidget/blob/e53868272aa950fd10d1d4a95d52e556d5d5a09c/packages/anywidget/src/load.ts#L66-L114)).

**Measured experimental lower bound, not a working AFM:** one model per cell,
708,612 B JS + 33,903 B CSS + 2,161 B IR, complete widget defaults saved:

| Cells | Source-only | Saved widget notebook | Increment |
|---:|---:|---:|---:|
| 1 | 404 B | 764,015 B | 763,611 B |
| 2 | 567 B | 1,527,646 B | 1,527,079 B |
| 10 | 1,871 B | 7,636,696 B | 7,634,825 B |

Every visualization model carried `_esm`. A remote URL could cache code but
breaks offline use; a server/package URL does not survive GitHub, standalone
export, or another machine. Saved custom widgets also require widget-manager
state/module loading ([ipywidgets embedding](https://ipywidgets.readthedocs.io/en/latest/embedding.html),
[nbconvert widget state](https://nbconvert.readthedocs.io/en/v7.15.0/execute_api.html#widget-state)).

anywidget 0.11.0 has three direct dependencies—`ipywidgets`, `psygnal`, and
`typing-extensions`
([official metadata](https://github.com/manzt/anywidget/blob/e53868272aa950fd10d1d4a95d52e556d5d5a09c/pyproject.toml#L18)).
A clean base-to-widget resolver installed 24 non-pip distributions occupying
44,174,726 bytes. That is the relevant cost of making anywidget a base
dependency, not the marginal cost of an optional live-only path: existing
Hypergraph `[notebook]` users already have ipywidgets
(`pyproject.toml:42-47`). The marginal cost was therefore measured in that
notebook environment rather than used as an assumption. A clean local
`.[notebook]` environment installed only `anywidget==0.11.0` and
`psygnal==0.15.1`: their four package/dist-info trees contained 1,426,182
apparent bytes (1,544 KiB allocated). The optional live-only marginal cost is
therefore modest and cannot by itself decide #156.

| Option | Dependency / payload cost | Maintenance cost |
|---|---|---|
| **1. Compressed iframe + safe topology** | 0 new deps; measured prototype 2,187,441 B / 10 cells | One interactive transport + one small static projection; verify the existing 10 rows. |
| **2. Optional anywidget + iframe fallback** | 2 new distributions over `[notebook]`, 1,426,182 apparent bytes; lower bound 7,636,696 B / 10 cells before fallback | Doubles interactive transport paths across the matrix, plus a static fallback. |
| **3. Primary anywidget + static fallback** | 44,174,726-byte cold base-to-widget stack; same inline-state lower bound | Widget-manager state on every host; GitHub/untrusted still need fallback. |

## Compressed iframe feasibility

**Measured prototype.** The prototype separated the small graph JSON from the
static 747,379-byte renderer shell, minified the shell once, gzipped it at
package/build time, and placed its base64 form in the notebook output. The
trusted bootstrap uses native `atob()` plus `DecompressionStream("gzip")` to
construct the iframe. Compression happens when Hypergraph is packaged, not on
every `visualize()` call.

| Saved cells | Current iframe | Compressed iframe candidate | Reduction |
|---:|---:|---:|---:|
| 1 | 912,683 B | 218,941 B | 76.011% |
| 2 | 1,825,147 B | 437,663 B | 76.020% |
| 10 | 9,124,861 B | **2,187,441 B** | **76.028%** |

The paired prototype harness normalized a slightly different representation
field than the primary saved-output harness, making its ten-cell current
baseline 395 bytes smaller (9,124,861 versus 9,125,256). The approved
2,281,314-byte ceiling uses the larger primary baseline; the candidate clears
either comparison.

The size chain was 747,379 bytes for the static shell after separating the
2,400-byte graph JSON, 572,239 bytes after minification, 159,260 bytes after
gzip, and 212,348 bytes after base64. Graph data, safe semantic topology, and
the trusted loader made a 217,911-byte HTML wrapper and a 218,941-byte
serialized one-cell output. The ten-cell candidate cleared the proposed
2,281,314-byte ceiling by 93,873 bytes. A custom base91 variant reached
2,027,161 bytes for ten cells, but was rejected because native base64 clears the
gate without maintaining a custom codec.

The paired Chromium benchmark used three fresh launches with ten navigations
each:

| Metric | Matched current iframe | Compressed candidate | Candidate / current |
|---|---:|---:|---:|
| First render median | 82.4 ms | 80.2 ms | 97.3% |
| Subsequent render median | 42.9 ms | 44.3 ms | 103.3% |
| Visible React Flow nodes / errors / HTTP requests | 2 / 0 / 0 | 2 / 0 / 0 | -- |

The unsigned JupyterLab copy retained seven semantic topology items and no
scripts or iframe. The signed copy reopened with zero kernels, created one
ready iframe with two nodes, and hid the fallback only after an authenticated
ready message. JavaScript-disabled `file://` retained the seven-item fallback.
The actual `nbconvert` output rendered two nodes offline and retained the
fallback with JavaScript disabled.

`DecompressionStream("gzip")` is broadly available in modern browsers
([MDN](https://developer.mozilla.org/en-US/docs/Web/API/DecompressionStream));
absence or decode failure must leave the semantic fallback visible. This is
transport compression, not sanitization. Production code must preserve
hostile-label and JSON/script-end escaping tests, validate the packaged shell
against its source hash, retain the fallback on decode or initialization
failure, and either remove
`allow-same-origin` or explicitly document the same-origin iframe as trusted
first-party code. `allow-popups` and `allow-forms` should be removed unless a
real consumer is found.

## Live inspect feasibility

**Automated real-host prototype.** The recovered inspect branch already
contains a payload-only postMessage builder and a receiver, but
`InspectWidget.refresh()` does not use them: it replaces the complete output,
and its test codifies that replacement. The prototype instead emitted one
shell display and one small update-channel display.

| Observation | Result |
|---|---:|
| Real two-node one-time shell | 912,660 B |
| Semantic update / terminal update | 967-1,752 B / 1,752 B |
| Refresh attempts / actual sends | 41 / 9 at 4 Hz |
| Iframes after all updates | 1 |
| Shell element, `contentWindow`, and `srcdoc` identity | unchanged |
| User-selected Graph tab | preserved |
| Final status / page errors | `COMPLETED` / 0 |
| VS Code saved no-kernel terminal sequence | 4, same runtime UUID, Graph tab preserved |

The saved persistence fixture contained one 74,826-byte sentinel shell plus
one 1,769-byte terminal channel output. Reopened trusted in JupyterLab with
`/api/kernels == []`, it replayed terminal sequence 41, showed `COMPLETED`, and
retained all three sentinel nodes. JupyterLab therefore needed no terminal
shell replacement.

The wire contract needs more hardening than the recovered branch: validate a
channel version, monotonic sequence, exact widget ID, per-instance nonce, and
`event.source === parent`. Terminal/error updates bypass throttling and must be
durably saved. One forced terminal shell replacement is permitted only as a
measured host-capability fallback; it may reset UI state once at completion,
but it cannot be the ordinary update path.

The VS Code witness opened a 970,610-byte notebook with one saved cell, exactly
two display outputs, and no kernelspec. Without running the cell or selecting a
kernel, the update channel reached the existing iframe. The same runtime UUID
survived sequences 1-4; after sequence 1 the user selected Graph, and terminal
sequence 4 still showed `selected-tab=graph`, `COMPLETED`, and the real
`load_customer` -> `rank_options` topology. The shell and channel sentinels
agreed, so normal VS Code does not isolate these outputs in a way that breaks
the bridge.

The implementation must still expose handshake failure and degrade explicitly
to a supported snapshot path rather than presenting a live view that silently
stops updating. Reconsider anywidget only if a required host later defeats this
built-in bridge and cannot meet the contract safely.

## Numeric adoption gate

**Approved production gate.** The measured size/runtime prototype justified the
compressed iframe direction. The production implementation may ship only if it
satisfies all of:

1. The exact ten-cell fixture is at most **2,281,314 bytes** (75% below current).
2. In a paired same-machine run under the same harness, first and subsequent
   readiness medians are each at most **110%** of the matched current-iframe
   baseline. This relative margin absorbs ordinary headless-host variation
   while rejecting a material transport regression.
3. All ten compatibility rows pass, including a visible script-free graph in
   unsigned JupyterLab and JS-disabled output.
4. `visualize()` and `filepath=` remain compatible; saved reopen needs no
   kernel, network, manual widget-state action, or Hypergraph server.

The measured compressed candidate passes the numeric, paired-runtime,
JupyterLab, static, nbconvert, and offline proof above. Production adoption
still requires the full compatibility matrix in the dedicated implementation
ticket. The anywidget lower bound saves only 16.3%, misses the byte gate before
adding a fallback, and would add a second interactive transport.

## Approved contract

Split the answer at the real ownership boundary.

### Settled graph output

The graph-output implementation lives in
[Compress saved notebook graph output and add truthful static topology](https://github.com/gilad-rubin/hypergraph/issues/224).
Do not enlarge #213's already broad render-path cleanup; the work necessarily
overlaps `src/hypergraph/viz/widget.py`, so #224 is an explicit dependency of
#213.

1. Keep the trusted `graph.visualize()` experience as a self-contained
   sandboxed iframe. No widget MIME or anywidget dependency.
2. Generate a small script-free semantic topology from the same IR and initial
   expansion state as the only declarative saved markup. It has no remote
   assets, handlers, or unsafe interpolation. A trusted bootstrap script
   constructs the iframe and hides the topology after readiness; sanitization
   or disabled JS removes/prevents the bootstrap and leaves the topology
   visible. Do not save a literal iframe beside the fallback. Preserve script
   end-marker and attribute-embedding protections when packaging the runtime.
   Document VS Code Restricted Mode separately because it suppresses all rich
   output instead of sanitizing to the safe representation.
3. Leave the standalone `filepath=` output unchanged.
4. Replace placeholder assertions with graph-content assertions; automate
   signed and unsigned JupyterLab with zero kernels, and retain real
   nbconvert/offline browser checks. Physically recheck the compressed graph
   candidate in normal/no-kernel VS Code and close or explicitly document the
   remaining kernel-running, Colab, nbviewer, and exact GitHub-hosted rows.
   Preserve the explicit VS Code Restricted Mode exception.
5. Package the existing iframe runtime as a pre-minified, pre-gzipped static
   shell with a native base64 loader. Keep graph JSON and safe topology outside
   the compressed shell. The exact ten-cell fixture must remain at most
   **2,281,314 bytes** (75% smaller), and paired readiness medians must remain
   within 110% of the matched current transport. Add the exact 1/2/10
   saved-output slope as a regression measurement. Do not silently fall back
   to anywidget.

### Live inspection handoff

This decision unblocks #156 with the built-in display/postMessage prototype as the
preferred seam; anywidget is not required by the measured design:

1. Hypergraph owns the live bridge; run handles remain control-only.
2. Initialize one immutable live shell and a versioned, nonce-bound update
   channel. Later updates carry serialized semantic inspect-artifact state
   only, never renderer/vendor assets, graph HTML, or another document shell.
3. Coalesce latest-state updates to at most 4-5 Hz. Terminal and error updates
   bypass throttling. Reject wrong widget IDs, versions, nonces, sources, and
   non-monotonic sequences.
4. Preserve iframe identity plus user selection, expansion, zoom, scroll, and
   table-page state across ordinary updates.
5. Persist the terminal view so it reopens with no kernel, network, manual
   widget-state action, or Hypergraph server. Permit one terminal full-shell
   replacement only as a measured host-capability fallback.
6. At #156's start, make the user inspect the decision-grade live prototype
   before the long implementation wave. Repeat the no-kernel JupyterLab and VS
   Code witnesses against production code; handshake failure must select and
   expose a supported fallback rather than silently stopping updates.

The recovered `feat/start-run-inspect-recovery` branch is contradictory intent,
not current behavior: `InspectWidget.refresh()` replaces the complete HTML,
`build_inspect_update_script()` defines an unused postMessage path, and its
design note separately recommends anywidget for small live diffs. The measured
prototype resolves that contradiction in favor of the existing lightweight
postMessage direction, including the VS Code witness above.

## Reproduction summary

Temporary notebooks, exports, Jupyter state, and browser files lived under the
OS temporary directory and were deleted. Core commands are listed first below;
the last two historical probe invocations reference temporary scripts that were
deleted after evidence capture:

```console
git rev-parse HEAD
wc -c notebooks/visualization_examples.ipynb
find src/hypergraph/viz/assets/vendor -maxdepth 1 -type f -print0 | xargs -0 wc -c
find src/hypergraph/viz/assets -maxdepth 1 -type f -name '*.js' -print0 | xargs -0 wc -c
uv run pytest tests/viz/test_payload_size.py tests/viz/test_compatibility_matrix.py \
  -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning' -q
UV_CACHE_DIR=/private/tmp/uv-cache uvx --from 'notebook==7.5.1' jupyter lab --version
PYTHONPATH=.:/private/tmp uv run --with zopfli --with rjsmin \
  --with csscompressor python /private/tmp/hg92_base64_probe.py
PYTHONPATH=. uv run python /private/tmp/hg92_jupyter_probe.py
```

The byte harness used `DisplayFormatter` on the real `_VizCellOutput`, normalized
only the nondeterministic `text/plain` address, and serialized with
`nbformat.writes`. The timing harness used seven fresh contexts, ten sequential
iframes, `__hypergraphVizReady`, and a 30-second per-cell deadline. The paired
compression benchmark separately used three fresh Chromium launches with ten
full navigations each. Host harnesses had 60/90-second deadlines and terminated
Jupyter in `finally`.

The live probe used the recovered branch's real inspect renderer with one shell
display and a second payload-only display. JupyterLab was configured with zero
allowed kernels, then the signed saved fixture was reopened after the update
sequence. The VS Code fixture had one cell, two already-saved display outputs,
and no kernelspec; it was opened without running the cell or selecting a kernel,
replayed semantic sequences 1-4, and had its Graph tab selected between
updates. Accessibility evidence recorded the same runtime UUID and
`selected-tab=graph` at terminal sequence 4.

The stale conclusion “13 MB tracked notebook, therefore anywidget” is rejected.
The maintainer approved the compressed iframe + safe topology contract, its
numeric gate, and the built-in live-update handoff. Implementation remains in
the graduated tickets; this research PR does not add a dependency or change
runtime behavior.
