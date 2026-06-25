"""
HyperTable internals walkthrough.

Shows exactly what happens behind the scenes for each concept and operation.
This is pseudocode — it describes the target design, not runnable code.
"""

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.runners import SyncRunner

# ═══════════════════════════════════════════════════════════════════════════════
#  SETUP: Define nodes and build the table
# ═══════════════════════════════════════════════════════════════════════════════

# Nodes — plain functions, portable, no framework knowledge.
# Components (model, embedder) are injected by argument name via .bind().


@node(output_name="audio_path")
def extract_audio(path: str) -> str: ...


@node(output_name="json_data")
def transcribe(audio_path: str, model: WhisperModel) -> str: ...


@node(output_name="markdown")
def to_markdown(json_data: str) -> str: ...


class Utterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str
    start: float
    end: float


@node(output_name="utterances")
def split_utterances(json_data: str) -> list[Utterance]: ...


# Typed return lets HyperTable infer child schema at construction (no runtime execution needed)


@node(output_name="clean_text")
def clean(text: str) -> str: ...


@node(output_name="vector")
def embed(clean_text: str, embedder: Embedder) -> list[float]: ...


# Subgraph: processes ONE utterance (clean → embed). Standard Hypergraph Graph.
process_utterance = Graph([clean, embed], name="process_utterance")

# The table: a graph + identity + store. Runner is separate — set via .with_runner().
subtext = (
    HyperTable(
        [extract_audio, transcribe, to_markdown, split_utterances, process_utterance.as_node().map_over("utterances", identity="utterance_id")],
        identity="video_id",
        store="lancedb://./data",
    )
    .bind(model=Whisper("large-v3"), embedder=Embedder("v1"))
    .with_runner(SyncRunner())
)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTRUCTION: How HyperTable analyzes the graph to build table schemas
# ═══════════════════════════════════════════════════════════════════════════════

# When you write HyperTable([nodes], identity=..., store=...).bind(...),
# the constructor inspects the graph using Hypergraph's existing introspection API.

# Step 1: Read graph inputs (what the graph needs from the caller).
#
#   After .bind(model=Whisper(), embedder=Embedder()):
#     graph.inputs.required  → {"path"}               # unbound inputs the caller must provide
#     graph.inputs.bound     → {"model", "embedder"}  # components, injected via closures, not stored
#
#   "path" is required → it's a source column AND a content key (because a node consumes it).
#   "video_id" is the declared identity → also a source column, but with identity role.
#   Any extra kwargs at insert time (like "title") → metadata columns (discovered at first insert).

# Step 2: Walk nodes in topological order, classify each.
#
#
#   topo_order = list(nx.topological_sort(graph._nx_graph))
#   # → [extract_audio, transcribe, to_markdown, split_utterances, process_utterance_mapped]
#
#   For each node:
#     node.data_inputs   → what it consumes (column names or bound values)
#     node.data_outputs  → what it produces (these become derived columns)
#
#   extract_audio:   inputs=["path"],       outputs=["audio_path"]    → derived column "audio_path"
#   transcribe:      inputs=["audio_path", "model"], outputs=["json_data"] → derived column "json_data"
#                    ("model" is in graph.inputs.bound → component, not a column)
#   to_markdown:     inputs=["json_data"],  outputs=["markdown"]      → derived column "markdown"
#   split_utterances: inputs=["json_data"], outputs=["utterances"]    → list output, but NOT a column...

# Step 3: Detect grain boundaries at map_over nodes.
#
#   process_utterance.as_node().map_over("utterances", identity="utterance_id")
#   This node has map_config → it's a grain boundary.
#
#   Everything BEFORE this node: columns on the root (video-grain) table.
#   Everything INSIDE the subgraph: columns on a new child (utterance-grain) table.
#
#   The subgraph process_utterance is analyzed recursively:
#     process_utterance.inputs.required → {"text"}      # unbound input → child source column
#     process_utterance.inputs.bound   → {"embedder"}   # component
#     clean.data_outputs → ["clean_text"]                # child derived column
#     embed.data_outputs → ["vector"]                    # child derived column
#
#   Child table source columns = fields from the split_utterances output dicts
#   that the subgraph consumes ("text") + any extra fields ("speaker", "start", "end").

# Step 4: Build the TableSpec for each grain.
#
#   root_table = TableSpec(
#       name="video",
#       identity="video_id",
#       columns=[
#           Column("video_id",   role="identity"),
#           Column("path",       role="source",  content_key=True),   # feeds extract_audio
#           # "title" will be added as metadata on first insert
#           Column("audio_path", role="derived",  produced_by=extract_audio),
#           Column("json_data",  role="derived",  produced_by=transcribe),
#           Column("markdown",   role="derived",  produced_by=to_markdown),
#           Column("_row_fingerprint",        role="internal"),
#           Column("_provenance_audio_path", role="internal"),
#           Column("_provenance_json_data",  role="internal"),
#           Column("_provenance_markdown",   role="internal"),
#       ],
#       children=[child_table],
#   )
#
#   child_table = TableSpec(
#       name="utterance",
#       identity="utterance_id",
#       parent_link="_parent_id",   # auto-added, equals parent's identity value
#       columns=[
#           Column("utterance_id", role="identity"),
#           Column("_parent_id",   role="parent_link"),
#           Column("text",         role="source",  content_key=True),   # feeds clean
#           Column("speaker",      role="source",  content_key=False),  # metadata (feeds no node)
#           Column("start",        role="source",  content_key=False),
#           Column("end",          role="source",  content_key=False),
#           Column("clean_text",   role="derived",  produced_by=clean),
#           Column("vector",       role="derived",  produced_by=embed),
#       ],
#   )

# Step 5: Create physical tables in the store (LanceDB).
#
#   Two LanceDB tables are created (or opened if they exist):
#     - "video" table with the root columns
#     - "utterance" table with the child columns
#   Schemas are inferred from the column types (str, bytes, list[float], etc.)
#   determined by node return type annotations.


# ═══════════════════════════════════════════════════════════════════════════════
#  WHAT THE TABLE LOOKS LIKE (two physical tables, one per grain)
# ═══════════════════════════════════════════════════════════════════════════════

# The result of construction — two physical tables:

# Table 1: "video" grain (root table)
# ┌────────────┬──────────────────┬───────┬────────────┬───────────┬──────────┬──────────────────────┐
# │ video_id   │ path             │ title │ audio_path │ json_data │ markdown │ (internal columns)   │
# │ (identity) │ (source/content) │ (src) │ (derived)  │ (derived) │ (derived)│ _row_fingerprint +   │
# │            │                  │       │            │           │          │ _provenance_* per col│
# ├────────────┼──────────────────┼───────┼────────────┼───────────┼──────────┼──────────────────────┤
# │ "v1"       │ "/data/mtg.mp4"  │ "Q3"  │ "/tmp/..." │ "{...}"   │ "# ..."  │ fingerprint + 3 prov │
# └────────────┴──────────────────┴───────┴────────────┴───────────┴──────────┴──────────────────────┘
#
# Column roles:
#   video_id   — identity (declared via identity="video_id")
#   path       — source, content key (feeds extract_audio → changes trigger re-derivation)
#   title      — source, metadata (feeds no node → changes are stored, no re-derivation)
#   audio_path — derived column (output of extract_audio node)
#   json_data  — derived column (output of transcribe node)
#   markdown   — derived column (output of to_markdown node)
#   _row_fingerprint — hash(all source content-key values + all node def hashes + all component configs)
#   _provenance_* — per-derived-column provenance hash (upstream values + node hash + component configs)

# Table 2: "utterance" grain (derived table, created at map_over boundary)
# ┌──────────────┬────────────┬─────────┬─────────┬────────┬────────┬────────────┬────────┐
# │ utterance_id │ _parent_id │ text    │ speaker │ start  │ end    │ clean_text │ vector │
# │ (identity)   │ (auto)     │ (src)   │ (src)   │ (src)  │ (src)  │ (derived)  │ (der.) │
# ├──────────────┼────────────┼─────────┼─────────┼────────┼────────┼────────────┼────────┤
# │ "v1:u0"      │ "v1"       │ "Hello" │ "Alice" │ 0.0    │ 3.5    │ "hello"    │ [0.1…] │
# │ "v1:u1"      │ "v1"       │ "..."   │ "Bob"   │ 3.5    │ 7.2    │ "..."      │ [0.3…] │
# └──────────────┴────────────┴─────────┴─────────┴────────┴────────┴────────────┴────────┘
#
# Column roles:
#   utterance_id — identity (declared via map_over(..., identity="utterance_id"))
#   _parent_id   — auto-stamped parent link (= video_id of the parent row)
#   text         — source column from split_utterances output; content key (feeds clean node)
#   speaker, start, end — source columns from split_utterances output; metadata
#   clean_text   — derived column (output of clean node in process_utterance subgraph)
#   vector       — derived column (output of embed node in process_utterance subgraph)


# ═══════════════════════════════════════════════════════════════════════════════
#  INSERT — what happens step by step
# ═══════════════════════════════════════════════════════════════════════════════

subtext.insert(video_id="v1", path="/data/meeting.mp4", title="Q3 Planning")

# Step 1: HyperTable receives the insert.
#   - Classifies inputs:
#       video_id → identity
#       path     → source, content key (because extract_audio consumes "path")
#       title    → source, metadata (no node consumes "title")
#   - Computes _row_fingerprint = hash(path + all node def hashes + all component configs)
#     (This is a fast-path check — if it matches on update/sync, skip re-derivation.)
#   - Per-column provenance is computed AFTER each node runs (not upfront),
#     because upstream values (audio_path, json_data) don't exist yet.

# Step 2: Check if identity "v1" already exists in the root table.
#   - It doesn't → this is a new row.

# Step 3: Write the source columns to the root table.
#   Root table row: {video_id: "v1", path: "/data/meeting.mp4", title: "Q3 Planning",
#                    audio_path: NULL, json_data: NULL, markdown: NULL}

# Step 4: Run the graph for this row, node by node (topological order).
#
#   Node: extract_audio
#     Input columns:  path = "/data/meeting.mp4"
#     Bound values:   (none for this node)
#     Execute:        extract_audio(path="/data/meeting.mp4") → "/tmp/v1_audio.wav"
#     Provenance:     _provenance_audio_path = hash(path_value + extract_audio.def_hash)
#     Write to sink:  UPDATE row v1 SET audio_path = "/tmp/v1_audio.wav",
#                                       _provenance_audio_path = <hash>
#
#   Node: transcribe
#     Input columns:  audio_path = "/tmp/v1_audio.wav"  (now available from previous step)
#     Bound values:   model = Whisper("large-v3")
#     Execute:        transcribe(audio_path="/tmp/v1_audio.wav", model=whisper) → '{"segments": [...]}'
#     Provenance:     _provenance_json_data = hash(audio_path_value + transcribe.def_hash + whisper.config_hash)
#     Write to sink:  UPDATE row v1 SET json_data = '{"segments": [...]}',
#                                       _provenance_json_data = <hash>
#
#   Node: to_markdown
#     Input columns:  json_data = '{"segments": [...]}'  (now available)
#     Execute:        to_markdown(json_data=...) → "# Q3 Planning\n\nAlice: Hello..."
#     Provenance:     _provenance_markdown = hash(json_data_value + to_markdown.def_hash)
#     Write to sink:  UPDATE row v1 SET markdown = "# Q3 Planning\n\nAlice: Hello...",
#                                       _provenance_markdown = <hash>
#
#   Node: split_utterances
#     Input columns:  json_data = '{"segments": [...]}'
#     Execute:        split_utterances(json_data=...) → [
#                       {"utterance_id": "v1:u0", "text": "Hello", "speaker": "Alice", "start": 0.0, "end": 3.5},
#                       {"utterance_id": "v1:u1", "text": "Let's begin", "speaker": "Bob", "start": 3.5, "end": 7.2},
#                     ]
#     This is the map_over boundary → creates child rows in the utterance table.

# Step 5: Process the map_over — for each utterance, run the subgraph.
#
#   Each utterance dict becomes a row in the utterance table.
#   _parent_id is auto-stamped from the root row's identity.
#
#   Utterance "v1:u0":
#     Write source columns: {utterance_id: "v1:u0", _parent_id: "v1",
#                            text: "Hello", speaker: "Alice", start: 0.0, end: 3.5}
#     Run process_utterance subgraph:
#       clean(text="Hello") → "hello"                     → SET clean_text = "hello"
#       embed(clean_text="hello", embedder=emb) → [0.1…]  → SET vector = [0.1…]
#
#   Utterance "v1:u1":
#     Write source columns: {utterance_id: "v1:u1", _parent_id: "v1",
#                            text: "Let's begin", speaker: "Bob", start: 3.5, end: 7.2}
#     Run process_utterance subgraph:
#       clean(text="Let's begin") → "let's begin"                     → SET clean_text = "let's begin"
#       embed(clean_text="let's begin", embedder=emb) → [0.3…]        → SET vector = [0.3…]

# Step 6: Done. Two tables populated:
#   Root table:      1 row  (v1 with all derived columns filled)
#   Utterance table: 2 rows (v1:u0, v1:u1 with clean_text + vector)


# ═══════════════════════════════════════════════════════════════════════════════
#  INSERT (batch) — multiple items
# ═══════════════════════════════════════════════════════════════════════════════

subtext.insert(
    [
        dict(video_id="v2", path="/data/standup.mp4", title="Daily"),
        dict(video_id="v3", path="/data/retro.mp4", title="Sprint Retro"),
    ]
)

# Same as single insert, but the runner processes multiple items:
#
# V1 (SyncRunner): one at a time, sequentially. Each row's results are written to
#                  the sink as they complete (map_iter → sink.write per result).
#
# [FUTURE — not v1] AsyncRunner: concurrent. Multiple videos processed in parallel.
# [FUTURE — not v1] DaftRunner: columnar DataFrame execution with native write.


# ═══════════════════════════════════════════════════════════════════════════════
#  UPDATE — change a source column
# ═══════════════════════════════════════════════════════════════════════════════

subtext.update("v1", path="/data/meeting_v2.mp4")

# Step 1: Look up row with identity "v1" in the root table.
#   Found: {video_id: "v1", path: "/data/meeting.mp4", title: "Q3 Planning", ...}

# Step 2: Apply the update. New source values:
#   {video_id: "v1", path: "/data/meeting_v2.mp4", title: "Q3 Planning"}

# Step 3: Check row fingerprint.
#   Old _row_fingerprint = hash("/data/meeting.mp4" + all_node_hashes + all_component_hashes)
#   New _row_fingerprint = hash("/data/meeting_v2.mp4" + all_node_hashes + all_component_hashes)
#   Different → per-column provenance checks needed.

# Step 4: Determine which nodes need to re-run.
#   path changed → extract_audio depends on path → re-run
#   audio_path will change → transcribe depends on audio_path → re-run
#   json_data will change → to_markdown depends on json_data → re-run
#   json_data will change → split_utterances depends on json_data → re-run
#   All downstream nodes re-run (the whole graph, because the root input changed).

# Step 5: Re-run the graph for row "v1" (same as insert step 4).
#   extract_audio("/data/meeting_v2.mp4") → new audio_path
#   transcribe(new_audio_path) → new json_data
#   to_markdown(new_json_data) → new markdown
#   split_utterances(new_json_data) → new list of utterances

# Step 6: Cascade to utterance table.
#   New utterances from split_utterances are matched to existing utterances by identity.
#   For each utterance:
#     - Same utterance_id, same text → skip (content key unchanged)
#     - Same utterance_id, different text → re-run subgraph (clean → embed)
#     - New utterance_id → insert new child row + run subgraph
#     - Missing utterance_id → delete old child row
#   Write-new-then-delete-old ordering (crash-safe).

# Step 7: Write updated source columns + re-derived columns to root table.


# ═══════════════════════════════════════════════════════════════════════════════
#  UPDATE — change a metadata column (no re-derivation)
# ═══════════════════════════════════════════════════════════════════════════════

subtext.update("v1", title="Q3 Planning Meeting — Revised")

# Step 1: Look up row "v1".
# Step 2: Apply update. title = "Q3 Planning Meeting — Revised".
# Step 3: Check content key.
#   title feeds NO node → content key is unchanged → NO re-derivation.
# Step 4: Write the new title to the root table. Done.
#   No nodes run. No cascade. Just a metadata update.


# ═══════════════════════════════════════════════════════════════════════════════
#  DELETE — remove a row and cascade
# ═══════════════════════════════════════════════════════════════════════════════

subtext.delete("v1")

# Step 1: Delete all child rows in the utterance table where _parent_id = "v1".
#   Deletes: v1:u0, v1:u1 (and any other utterances from v1).
#   No compute — just row deletion.

# Step 2: Delete the root row with identity "v1".
#   No compute — just row deletion.

# Order: children first, then parent (referential integrity).


# ═══════════════════════════════════════════════════════════════════════════════
#  SYNC — reconcile to a current corpus
# ═══════════════════════════════════════════════════════════════════════════════

current_videos = [
    dict(video_id="v1", path="/data/meeting_v3.mp4", title="Q3 Final"),  # changed (path different)
    dict(video_id="v2", path="/data/standup.mp4", title="Daily"),  # unchanged
    # v3 is missing → should be deleted
    dict(video_id="v4", path="/data/kickoff.mp4", title="Q4 Kickoff"),  # new
]

result = subtext.sync(current_videos)

# Step 1: Match incoming items to stored rows by identity.
#   v1 → exists, check content key:
#     path changed ("/data/meeting.mp4" → "/data/meeting_v3.mp4") → UPDATE
#   v2 → exists, check content key:
#     path unchanged, title unchanged → SKIP (no work)
#   v3 → NOT in incoming list → DELETE
#   v4 → does NOT exist in table → INSERT

# Step 2: Execute the actions:
#   UPDATE v1: same as subtext.update("v1", path="/data/meeting_v3.mp4", title="Q3 Final")
#   SKIP v2:   nothing happens
#   DELETE v3: same as subtext.delete("v3")
#   INSERT v4: same as subtext.insert(video_id="v4", path="/data/kickoff.mp4", title="Q4 Kickoff")

# Step 3: Return a SyncResult:
#   SyncResult(inserted=["v4"], updated=["v1"], deleted=["v3"], skipped=["v2"], errored=[])


# ═══════════════════════════════════════════════════════════════════════════════
#  RECOMPUTE — component config changed
# ═══════════════════════════════════════════════════════════════════════════════

subtext.recompute("vector", components={"embedder": Embedder("v2")})

# The embedder changed. Which columns are affected?
#
# The "vector" column is produced by the embed node.
# embed depends on: clean_text (input column) + embedder (component).
# The embedder's config changed → every row's vector column is stale.
#
# But clean_text didn't change, and json_data didn't change, and audio_path
# didn't change. Only the embed node needs to re-run.

# Step 1: For every row in the utterance table:
#   Re-run: embed(clean_text=row.clean_text, embedder=Embedder("v2")) → new vector
#   Write:  UPDATE row SET vector = new_vector
#
# No other nodes run. Root table untouched. clean_text untouched.
# Only the vector column is recomputed.

# This is scoped recompute — the content key for the embed node includes
# the embedder's config hash. When the config changes, every row's content
# key for that column misses, triggering re-derivation.


# ═══════════════════════════════════════════════════════════════════════════════
#  [FUTURE — v2] PIN / OVERRIDE — manually set a derived value
# ═══════════════════════════════════════════════════════════════════════════════
#
# Pin/override is deferred to v2. It requires a state-machine spec covering:
#   - Which columns can be pinned (derived only, not source)
#   - Unpin behavior (revert to computed value or leave manual value?)
#   - Content-key interaction (pinned value in downstream provenance?)
#   - Cascade rules (does unpinning trigger re-derivation?)
#   - Recovery behavior (what happens to pins on schema evolution?)
#
# Example of the intended v2 API (NOT implemented in v1):
#   subtext.pin("v1", json_data='{"corrected_segments": [...]}')
#   subtext.pin("v1:u3", text="corrected speaker attribution")


# ═══════════════════════════════════════════════════════════════════════════════
#  SEARCH / QUERY — read from any table
# ═══════════════════════════════════════════════════════════════════════════════

# Vector search on the utterance table (the derived table)
results = subtext.search("vector", query_vector=q, limit=5)
# → returns utterance rows: {utterance_id, text, speaker, clean_text, vector, _parent_id, ...}

# Filter on the root table
videos = subtext.filter(title="Q3 Planning")
# → returns video rows: {video_id, path, title, audio_path, json_data, markdown}

# Filter the utterance table by parent
utterances = subtext.children("v1")
# → returns all utterance rows where _parent_id = "v1"

# Count
subtext.count()  # root table row count
subtext.count("utterance")  # child table row count


# ═══════════════════════════════════════════════════════════════════════════════
#  RUNNER EXECUTION PATHS — same API, different engines
# ═══════════════════════════════════════════════════════════════════════════════

# --- SyncRunner (default, sequential) ---
#
# For insert/update, processes one row at a time:
#   for result in runner.map_iter(graph, [row_inputs], map_over="item"):
#       sink.write(result)       # write immediately, row by row
#
# At the map_over boundary (split_utterances → process_utterance):
#   utterances = result["utterances"]   # list from split_utterances
#   for utt_result in runner.map_iter(process_utterance, utterances, map_over="utterance"):
#       child_sink.write(utt_result)    # write each utterance immediately
#
# Simple, predictable, good for debugging.

# [FUTURE — not v1] AsyncRunner: concurrent map_iter, out-of-order sink writes.
# [FUTURE — not v1] DaftRunner: lazy DataFrame plan with native columnar writes.


# ═══════════════════════════════════════════════════════════════════════════════
#  VISUALIZATION — the table IS the graph
# ═══════════════════════════════════════════════════════════════════════════════

subtext.visualize()

# Renders something like:
#
# ┌─────────────────────────── video (root) ───────────────────────────────┐
# │                                                                        │
# │  [video_id]  path ──▸ extract_audio ──▸ transcribe ──┬──▸ to_markdown  │
# │              ↑                              │        │                 │
# │           (source)                     (bound:       └──▸ split_utterances
# │                                        model)             │            │
# └────────────────────────────────────────────────────────────┼────────────┘
#                                                              │ map_over
# ┌─────────────────── utterance (derived) ────────────────────┼────────────┐
# │                                                            ▼            │
# │  [utterance_id]  text ──▸ clean ──▸ embed ──▸ vector                   │
# │                                      ↑                                 │
# │                                 (bound: embedder)                      │
# └─────────────────────────────────────────────────────────────────────────┘
#
# The subgraph process_utterance has its own .visualize():
#   clean ──▸ embed


# ═══════════════════════════════════════════════════════════════════════════════
#  MULTIMODAL STORAGE — columns store whatever the node returns
# ═══════════════════════════════════════════════════════════════════════════════

# LanceDB is a multimodal database — it stores text, vectors, images, audio,
# and video as native column types. HyperTable leverages this: a node that
# returns bytes produces a binary column. The table doesn't care about the type.

# Example: store actual video and audio data, not file paths.


@node(output_name="video_data")
def load_video(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


@node(output_name="audio")
def extract_audio_bytes(video_data: bytes) -> bytes:  # takes video bytes, returns audio bytes
    return ffmpeg_extract_audio(video_data)


@node(output_name="thumbnail")
def extract_thumbnail(video_data: bytes) -> bytes:  # returns a JPEG image
    return ffmpeg_extract_frame(video_data, t=0)


@node(output_name="json_data")
def transcribe_bytes(audio: bytes, model: WhisperModel) -> str:
    return model.transcribe(audio)


# The root table now stores multimodal data directly:
#
# ┌──────────┬──────┬───────┬─────────────┬──────────────┬───────────────┬───────────┬──────────┐
# │ video_id │ path │ title │ video_data  │ audio        │ thumbnail     │ json_data │ markdown │
# │ identity │ src  │ meta  │ derived     │ derived      │ derived       │ derived   │ derived  │
# │ str      │ str  │ str   │ bytes       │ bytes        │ bytes         │ str       │ str      │
# │          │      │       │ (500MB vid) │ (50MB audio) │ (200KB JPEG)  │           │          │
# └──────────┴──────┴───────┴─────────────┴──────────────┴───────────────┴───────────┴──────────┘
#
# What this enables:
#   - The actual video is stored in the DB, not just a pointer to a file.
#   - If the file path changes but the video is identical, the content key
#     (hash of video_data bytes) is unchanged → no re-derivation.
#   - The thumbnail is queryable (LanceDB supports image similarity search).
#   - The audio is available for re-transcription without re-reading the file.

# Why streaming writes matter here:
#
# A batch insert of 100 videos at ~500MB each = 50GB of video_data.
# Buffering all 100 rows in memory before writing would require 50GB+ RAM.
#
# With write-as-ready (sink):
#   V1 (SyncRunner): each video is loaded, processed, and written to LanceDB
#                    one at a time. Peak memory = 1 video (~500MB).
#   [FUTURE] AsyncRunner: bounded concurrency, peak = N * 500MB.
#   [FUTURE] DaftRunner: lazy execution + streaming write, never all in memory.

# The content-key check is type-agnostic:
#   For str columns:   hash(value)
#   For bytes columns:  hash(value)   — same mechanism, works on any type
#   For list[float]:   hash(value)   — embedding vectors are just data
#
# Provenance is computed at insert time and stored per column (_provenance_*).
# On update, the row fingerprint is checked first (fast path). If it changed,
# per-column provenance is recomputed and compared.
# Same provenance → skip that column. Different → re-derive it and its downstream.


# ═══════════════════════════════════════════════════════════════════════════════
#  .with_runner() — set the default runner, override per-call
# ═══════════════════════════════════════════════════════════════════════════════

# The runner is NOT on the HyperTable constructor. Set it once via .with_runner().

subtext = HyperTable(
    [...],
    identity="video_id",
    store="lancedb://./data",
).bind(model=Whisper(), embedder=Embedder())

# At this point, subtext has no runner. Read operations work:
subtext.search("vector", query_vector=q, limit=5)  # OK — reads don't need a runner
subtext.filter(title="Q3")  # OK
subtext.count()  # OK

# But write operations fail without a runner:
# subtext.insert(video_id="v1", path="...")          # ERROR: no runner set

# Set a default runner (returns a new immutable instance):
subtext = subtext.with_runner(SyncRunner())

# Now write operations work:
subtext.insert(video_id="v1", path="/data/meeting.mp4")
subtext.sync(current_videos)

# [FUTURE — not v1] Override for a specific call:
# subtext.insert(large_batch, runner=AsyncRunner())

# Why separate?
#   1. Construction + .bind() + .with_runner() are three distinct concerns:
#      graph structure / component injection / execution strategy.
#   2. You don't always need a runner (read-only use cases).
#   3. V1 is sync-only. Future versions may support per-call runner override.


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEMA EVOLUTION — what happens when you change the graph
# ═══════════════════════════════════════════════════════════════════════════════

# Scenario: you add a "summary" node to the graph after the table already has data.


@node(output_name="summary")
def summarize(markdown: str, llm: LLM) -> str: ...


subtext_v2 = (
    HyperTable(
        [
            extract_audio,
            transcribe,
            to_markdown,
            summarize,  # <-- new node
            split_utterances,
            process_utterance.as_node().map_over("utterances", identity="utterance_id"),
        ],
        identity="video_id",
        store="lancedb://./data",
    )
    .bind(model=Whisper(), embedder=Embedder(), llm=GPT())
    .with_runner(SyncRunner())
)

# When subtext_v2 connects to the existing store, it compares:
#   New graph schema: [..., "summary"]
#   Stored table schema: [..., no "summary"]
#
# "summary" is a new derived column → SAFE change (add column).
#   - Column added to the table with NULL for existing rows.
#   - No automatic backfill — existing rows keep NULL until you ask.

# To backfill existing rows:
subtext_v2.backfill("summary")
#   For every row where summary IS NULL:
#     summarize(markdown=row.markdown, llm=gpt) → summary text
#     Write to store: UPDATE row SET summary = "..."

# Scenario: you change the clean() function body (fix a bug).
#   The content key includes a definition hash of clean().
#   Old hash != new hash → every utterance's content key mismatches.
#   On next sync, clean + embed re-run for all utterances. AUTOMATIC.
#   (No explicit call needed — the hash comparison drives it.)

# Scenario: you want to REMOVE the "markdown" column entirely.
#   This is destructive — explicit call required:
subtext_v2.drop_column("markdown")
#   - Removes the column from the table.
#   - Removes to_markdown from the derivation plan.
#   - Data is gone. This is why it's explicit.

# Scenario: you want to RENAME a column.
subtext_v2.rename_column("json_data", "transcript_json")
#   - Preserves data, updates the schema.
#   - Nodes that reference "json_data" by name need graph-level renaming too.


# ═══════════════════════════════════════════════════════════════════════════════
#  QUERY-TIME GRAPHS — hybrid search, reranking, etc.
# ═══════════════════════════════════════════════════════════════════════════════

# Simple search — use the table's built-in primitives:
results = subtext.search("vector", query_vector=q, limit=5)
results = subtext.search("text", query="quarterly revenue", mode="bm25", limit=20)

# But for hybrid search + reranking, you need a pipeline:
#   1. BM25 text search → candidate set A
#   2. Embed the query → query vector
#   3. Vector similarity search → candidate set B
#   4. RRF to merge A and B
#   5. Reranking with a cross-encoder
#
# That pipeline IS a Hypergraph graph. It reads from the table but doesn't write to it.


@node(output_name="bm25_hits")
def bm25_search(query: str, table: HyperTable) -> list[dict]:
    return table.search("text", query=query, mode="bm25", limit=20)


@node(output_name="query_vector")
def embed_query(query: str, embedder: Embedder) -> list[float]:
    return embedder.embed(query)


@node(output_name="vector_hits")
def vector_search(query_vector: list[float], table: HyperTable) -> list[dict]:
    return table.search("vector", query_vector=query_vector, limit=20)


@node(output_name="merged")
def rrf_merge(bm25_hits: list[dict], vector_hits: list[dict]) -> list[dict]:
    scores = {}
    for rank, hit in enumerate(bm25_hits):
        scores[hit["utterance_id"]] = scores.get(hit["utterance_id"], 0) + 1 / (60 + rank)
    for rank, hit in enumerate(vector_hits):
        scores[hit["utterance_id"]] = scores.get(hit["utterance_id"], 0) + 1 / (60 + rank)
    return sorted(
        {**{h["utterance_id"]: h for h in bm25_hits + vector_hits}}.values(),
        key=lambda h: scores[h["utterance_id"]],
        reverse=True,
    )


@node(output_name="results")
def rerank(merged: list[dict], query: str, reranker: CrossEncoder) -> list[dict]:
    return reranker.rerank(query, merged, limit=5)


hybrid_search = Graph(
    [embed_query, bm25_search, vector_search, rrf_merge, rerank],
    name="hybrid_search",
)

# --- General case: the table is just a bound component ---

results = SyncRunner().run(
    hybrid_search,
    query="quarterly revenue",
    table=subtext,  # the table is a component, like an LLM or embedder
    embedder=Embedder(),
    reranker=CrossEncoder(),
)
# The graph doesn't know or care that `table` is a HyperTable.
# It just calls table.search(...). Testable with any object that has .search().

# --- Convenience case: dynamic attachment via .queries namespace ---

# Assign any graph to any name on .queries:
subtext.queries.hybrid = hybrid_search.bind(embedder=Embedder(), reranker=CrossEncoder())
subtext.queries.simple = simple_vector_graph
subtext.queries.hybrid_v2 = better_hybrid.bind(...)

# Call like a method — runs the graph with table=self:
results = subtext.queries.hybrid(query="quarterly revenue")
results = subtext.queries.simple(query="hello")

# Under the hood, .queries is a namespace object. Assigning a graph wraps it:
#
#   class QueryNamespace:
#       def __setattr__(self, name, graph):
#           self._graphs[name] = graph
#
#       def __getattr__(self, name):
#           graph = self._graphs[name]
#           def run(**kwargs):
#               runner = self._table._runner or SyncRunner()
#               return runner.run(graph, table=self._table, **kwargs)
#           return run
#
# You pick the name. No fixed API. No collisions with table methods.

# The query graph is:
#   - A normal Hypergraph graph (testable, visualizable, composable)
#   - Completely separate from the materialization graph
#   - Attached under whatever name you choose


# ═══════════════════════════════════════════════════════════════════════════════
#  EPHEMERAL OUTPUTS — intermediate values that aren't stored
# ═══════════════════════════════════════════════════════════════════════════════

# By default, every node output becomes a column in the table.
# Sometimes you need a value to flow between nodes but NOT be stored.

# Example: an LLM returns a full response with usage metadata.
# You want the answer stored, but the raw response is just intermediate.


@node(output_name="raw_response", ephemeral=True)
def call_llm(prompt: str, llm: LLM) -> dict:
    return llm.generate(prompt)  # {"answer": "...", "usage": {"tokens": 150, "cost": 0.003}}


@node(output_name="answer")
def extract_answer(raw_response: dict) -> str:
    return raw_response["answer"]


@node(output_name="cost")
def extract_cost(raw_response: dict) -> float:
    return raw_response["usage"]["cost"]


# What HyperTable sees at construction:
#   call_llm    → output "raw_response", ephemeral=True  → NOT a column
#   extract_answer → output "answer"                      → derived column
#   extract_cost   → output "cost"                        → derived column
#
# The table has columns: [..., "answer", "cost"]
# No "raw_response" column exists.

# During execution (insert/update):
#   call_llm runs → raw_response = {"answer": "...", "usage": {...}}
#   raw_response flows through graph wiring to extract_answer and extract_cost
#   extract_answer(raw_response) → "the quarterly report..."  → stored as "answer"
#   extract_cost(raw_response)   → 0.003                     → stored as "cost"
#   raw_response is discarded after execution — never written to store.

# On re-derivation:
#   There's no stored raw_response to compare against.
#   The content key for downstream nodes includes call_llm's definition hash.
#   If call_llm's body changes, downstream content keys mismatch → re-derive.

# When you DON'T need ephemeral:
#   If the intermediate value isn't consumed by another node, just keep it
#   inside the function. One node calls the LLM and returns only the answer.
#   ephemeral=True is only for values that must flow between nodes.


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEMA MISMATCH — every edge case, step by step
# ═══════════════════════════════════════════════════════════════════════════════

# When you change the graph and connect to an existing table, HyperTable
# compares the new graph's TableSpec to the stored table's actual schema.

# --- Case 1: Node body edited (AUTO) ---
#
# You fix a bug in clean():
#   def clean(text: str) -> str:
#       return text.lower().strip()    # was: return text.lower()
#
# clean()'s definition hash changes.
# The content key for every utterance includes clean's hash.
# Old hash != new hash → on next sync, clean + embed re-run for all utterances.
# No explicit call needed.

# --- Case 2: Component config changed (AUTO) ---
#
# subtext = subtext.bind(embedder=Embedder("v2"))   # was "v1"
#
# Embedder's config hash changes.
# embed node's content key includes the embedder config hash.
# On next sync, embed re-runs for all utterances. clean does NOT re-run
# (its content key is unchanged).

# --- Case 3: New node added (AUTO) ---
#
# You add summarize() to the graph. Table has no "summary" column.
# HyperTable adds the column with NULL for existing rows. Safe.
# Call backfill("summary") to populate existing rows.

# --- Case 4: Node removed (EXPLICIT — error at construction) ---
#
# You remove to_markdown from the graph. Table still has "markdown" column.
# HyperTable raises:
#   SchemaEvolutionError:
#     Column "markdown" exists in the stored table but has no producing node
#     in the current graph. To remove it, call: drop_column("markdown")
#
# You must explicitly:
subtext_v2.drop_column("markdown")

# --- Case 5: Output type changed (EXPLICIT — error at construction) ---
#
# You change extract_audio to return bytes instead of str.
# HyperTable raises:
#   SchemaEvolutionError:
#     Column "audio_path" type changed from str to bytes.
#     To rebuild with the new type, call: rebuild_column("audio_path")
#
# You must explicitly:
subtext_v2.rebuild_column("audio_path")
#   Drops the old column, adds the new type, re-derives for all rows.

# --- Case 6: Node renamed / output_name changed (EXPLICIT + AUTO) ---
#
# You rename output_name="json_data" to output_name="transcript".
# HyperTable sees: "json_data" column has no producer (error, case 4)
#                   + "transcript" column is new (auto-add, case 3).
# You must handle the orphan explicitly:
subtext_v2.drop_column("json_data")
# Or migrate:
subtext_v2.rename_column("json_data", "transcript")

# --- Case 7: External table modification (NOT SUPPORTED) ---
#
# Someone adds a column to the LanceDB table outside HyperTable.
# Behavior is undefined. HyperTable owns the schema — don't modify it externally.
